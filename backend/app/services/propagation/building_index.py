from __future__ import annotations

import fcntl
import json
import logging
import math
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Iterable, Iterator

logger = logging.getLogger(__name__)

GML = "{http://www.opengis.net/gml}"
BLDG = "{http://www.opengis.net/citygml/building/2.0}"
GEN = "{http://www.opengis.net/citygml/generics/2.0}"


@dataclass(frozen=True)
class Building:
    id: int
    source_id: str
    footprint: tuple[tuple[float, float], ...]
    ground_z_m: float
    roof_z_m: float
    height_m: float
    data_year: str | None


def wgs84_to_epsg2180(lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Convert WGS84/GRS80 latitude and longitude to Polish PL-1992 (EPSG:2180).

    The ellipsoids differ by less than a millimetre at this scale. This direct
    Transverse Mercator implementation avoids a heavyweight runtime GIS dependency.
    """
    a = 6_378_137.0
    f = 1.0 / 298.257222101
    e2 = f * (2.0 - f)
    ep2 = e2 / (1.0 - e2)
    k0 = 0.9993
    lat = math.radians(lat_deg)
    lon_delta = math.radians(lon_deg - 19.0)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    tan_lat = math.tan(lat)
    n = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    t = tan_lat * tan_lat
    c = ep2 * cos_lat * cos_lat
    aa = cos_lat * lon_delta
    e4, e6 = e2 * e2, e2 * e2 * e2
    m = a * (
        (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat
        - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat)
        - (35 * e6 / 3072) * math.sin(6 * lat)
    )
    x = 500_000.0 + k0 * n * (
        aa
        + (1 - t + c) * aa**3 / 6
        + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * aa**5 / 120
    )
    y = -5_300_000.0 + k0 * (
        m
        + n
        * tan_lat
        * (
            aa * aa / 2
            + (5 - t + 9 * c + 4 * c * c) * aa**4 / 24
            + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * aa**6 / 720
        )
    )
    return x, y


def _coordinates(pos_list: str) -> tuple[tuple[float, float, float], ...]:
    values = [float(value) for value in pos_list.split()]
    if len(values) < 9 or len(values) % 3:
        return ()
    return tuple(zip(values[0::3], values[1::3], values[2::3]))


def _building_from_element(element: ET.Element) -> tuple[str, tuple, float, float, float, str | None] | None:
    source_id = element.attrib.get(f"{GML}id", "unknown")
    measured = element.find(f"{BLDG}measuredHeight")
    height = float(measured.text) if measured is not None and measured.text else math.nan
    data_year = None
    for attribute in element.findall(f"{GEN}stringAttribute"):
        if attribute.attrib.get("name") == "aktZrodla":
            value = attribute.find(f"{GEN}value")
            data_year = value.text if value is not None else None
            break

    rings = []
    for pos in element.findall(f".//{GML}posList"):
        if pos.text:
            ring = _coordinates(pos.text)
            if ring:
                rings.append(ring)
    if not rings:
        return None
    ground_z = min(point[2] for ring in rings for point in ring)
    roof_z = max(point[2] for ring in rings for point in ring)
    horizontal = [
        ring
        for ring in rings
        if max(point[2] for point in ring) - min(point[2] for point in ring) < 0.05
    ]
    if not horizontal:
        return None
    footprint3d = min(horizontal, key=lambda ring: sum(point[2] for point in ring) / len(ring))
    footprint = tuple((point[0], point[1]) for point in footprint3d)
    if footprint[0] != footprint[-1]:
        footprint += (footprint[0],)
    if math.isnan(height):
        height = roof_z - ground_z
    return source_id, footprint, ground_z, roof_z, height, data_year


def iter_citygml(source: Path | IO[bytes]) -> Iterator[tuple[str, tuple, float, float, float, str | None]]:
    for _, element in ET.iterparse(source, events=("end",)):
        if element.tag == f"{BLDG}Building":
            building = _building_from_element(element)
            if building is not None:
                yield building
            element.clear()


class BuildingIndex:
    """Indeks SQLite/RTree budynków LoD1, uzupełniany powiatami (patrz `gugik.BuildingProvider`)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=60)

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            try:
                with self._connect(self.db_path) as db:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
                    columns = {row[1] for row in db.execute("PRAGMA table_info(districts)")}
                if "last_used" in columns:
                    return
            except sqlite3.DatabaseError:
                pass
            # Indeks w starym formacie — powiaty pobiorą się ponownie.
            logger.warning("Replacing building index without district table: %s", self.db_path)
            self.db_path.unlink()
        with self._connect(self.db_path) as db:
            # Bez auto_vacuum plik nie maleje po usunięciu powiatów (patrz `prune`).
            db.execute("PRAGMA auto_vacuum = FULL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS buildings (
                    id INTEGER PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    footprint TEXT NOT NULL,
                    ground_z_m REAL NOT NULL,
                    roof_z_m REAL NOT NULL,
                    height_m REAL NOT NULL,
                    data_year TEXT,
                    district TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS buildings_district ON buildings(district);
                CREATE VIRTUAL TABLE IF NOT EXISTS building_bounds USING rtree(
                    id, min_x, max_x, min_y, max_y
                );
                CREATE TABLE IF NOT EXISTS districts (
                    code TEXT PRIMARY KEY,
                    year TEXT NOT NULL,
                    building_count INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL,
                    last_used TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Blokada między procesami (kilka instancji backendu dzieli jeden indeks)."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.db_path.with_suffix(".lock"), "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def districts(self) -> set[str]:
        self._ensure_schema()
        with self._connect(self.db_path) as db:
            return {row[0] for row in db.execute("SELECT code FROM districts")}

    def add_district(self, code: str, year: str, sources: Iterable[Path | IO[bytes]]) -> int:
        """Zastępuje budynki powiatu `code` budynkami z plików CityGML; zwraca ich liczbę."""
        self._ensure_schema()
        count = 0
        with self._connect(self.db_path) as db:
            self._remove_district(db, code)
            for source in sources:
                for source_id, footprint, ground, roof, height, data_year in iter_citygml(source):
                    count += 1
                    cursor = db.execute(
                        "INSERT INTO buildings(source_id, footprint, ground_z_m, roof_z_m, height_m, "
                        "data_year, district) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (source_id, json.dumps(footprint), ground, roof, height, data_year, code),
                    )
                    xs = [point[0] for point in footprint]
                    ys = [point[1] for point in footprint]
                    db.execute(
                        "INSERT INTO building_bounds VALUES (?, ?, ?, ?, ?)",
                        (cursor.lastrowid, min(xs), max(xs), min(ys), max(ys)),
                    )
            now = datetime.now(timezone.utc).isoformat()
            db.execute(
                "INSERT OR REPLACE INTO districts(code, year, building_count, indexed_at, last_used) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, year, count, now, now),
            )
        return count

    def _remove_district(self, db: sqlite3.Connection, code: str) -> None:
        db.execute(
            "DELETE FROM building_bounds WHERE id IN (SELECT id FROM buildings WHERE district = ?)",
            (code,),
        )
        db.execute("DELETE FROM buildings WHERE district = ?", (code,))
        db.execute("DELETE FROM districts WHERE code = ?", (code,))

    def touch(self, codes: Iterable[str]) -> None:
        """Zapisuje użycie powiatów (kolejność usuwania w `prune`)."""
        self._ensure_schema()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect(self.db_path) as db:
            db.executemany("UPDATE districts SET last_used = ? WHERE code = ?", [(now, c) for c in codes])

    def prune(self, max_bytes: int, keep: set[str]) -> list[str]:
        """Usuwa najdawniej używane powiaty (poza `keep`), aż plik zmieści się w `max_bytes`."""
        self._ensure_schema()
        removed: list[str] = []
        with self._connect(self.db_path) as db:
            candidates = [
                row[0]
                for row in db.execute("SELECT code FROM districts ORDER BY last_used")
                if row[0] not in keep
            ]
        for code in candidates:
            if self.db_path.stat().st_size <= max_bytes:
                break
            with self._connect(self.db_path) as db:
                self._remove_district(db, code)
            removed.append(code)
        return removed

    def query(self, min_x: float, max_x: float, min_y: float, max_y: float) -> list[Building]:
        if not self.db_path.exists():
            return []
        with self._connect(self.db_path) as db:
            rows = db.execute(
                """
                SELECT b.id, b.source_id, b.footprint, b.ground_z_m,
                       b.roof_z_m, b.height_m, b.data_year
                FROM building_bounds r
                JOIN buildings b ON b.id = r.id
                WHERE r.max_x >= ? AND r.min_x <= ?
                  AND r.max_y >= ? AND r.min_y <= ?
                """,
                (min_x, max_x, min_y, max_y),
            ).fetchall()
        return [
            Building(
                id=row[0],
                source_id=row[1],
                footprint=tuple(tuple(point) for point in json.loads(row[2])),
                ground_z_m=row[3],
                roof_z_m=row[4],
                height_m=row[5],
                data_year=row[6],
            )
            for row in rows
        ]
