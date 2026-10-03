from __future__ import annotations

import json
import logging
import math
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

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


def iter_citygml(path: Path) -> Iterator[tuple[str, tuple, float, float, float, str | None]]:
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag == f"{BLDG}Building":
            building = _building_from_element(element)
            if building is not None:
                yield building
            element.clear()


class BuildingIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=60)

    def build(self, raw_dir: Path, *, force: bool = False) -> int:
        files = sorted(raw_dir.rglob("*.gml"))
        if not files:
            raise FileNotFoundError(f"No CityGML files in {raw_dir}")
        newest_source = max(path.stat().st_mtime_ns for path in files)
        if not force and self.db_path.exists():
            try:
                with self._connect(self.db_path) as db:
                    metadata = dict(db.execute("SELECT key, value FROM metadata"))
                    unchanged = (
                        int(metadata.get("newest_source_mtime_ns", 0)) == newest_source
                        and int(metadata.get("source_file_count", 0)) == len(files)
                    )
                    if unchanged:
                        return int(metadata.get("building_count", 0))
            except (sqlite3.DatabaseError, ValueError):
                logger.warning("Building index is invalid; rebuilding %s", self.db_path)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.db_path.with_suffix(".tmp.sqlite")
        temporary.unlink(missing_ok=True)
        with self._connect(temporary) as db:
            db.executescript(
                """
                CREATE TABLE buildings (
                    id INTEGER PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    footprint TEXT NOT NULL,
                    ground_z_m REAL NOT NULL,
                    roof_z_m REAL NOT NULL,
                    height_m REAL NOT NULL,
                    data_year TEXT
                );
                CREATE VIRTUAL TABLE building_bounds USING rtree(
                    id, min_x, max_x, min_y, max_y
                );
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            count = 0
            for file_index, path in enumerate(files, start=1):
                for source_id, footprint, ground, roof, height, year in iter_citygml(path):
                    count += 1
                    cursor = db.execute(
                        "INSERT INTO buildings(source_id, footprint, ground_z_m, roof_z_m, height_m, data_year) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (source_id, json.dumps(footprint), ground, roof, height, year),
                    )
                    xs = [point[0] for point in footprint]
                    ys = [point[1] for point in footprint]
                    db.execute(
                        "INSERT INTO building_bounds VALUES (?, ?, ?, ?, ?)",
                        (cursor.lastrowid, min(xs), max(xs), min(ys), max(ys)),
                    )
                if file_index % 20 == 0:
                    db.commit()
                    logger.info("Indexed CityGML files %s/%s", file_index, len(files))
            db.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("newest_source_mtime_ns", str(newest_source)),
                    ("building_count", str(count)),
                    ("source_file_count", str(len(files))),
                ],
            )
        temporary.replace(self.db_path)
        logger.info("Built building index with %s objects at %s", count, self.db_path)
        return count

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
