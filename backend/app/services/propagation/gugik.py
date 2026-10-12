"""Dane GUGiK pobierane na żądanie: numeryczny model terenu (NMT) i budynki LoD1.

NMT: usługa wysokości GUGiK (`GetHByPointList`, do 1000 punktów na zapytanie) zwraca
wysokość gruntu z siatki 1 m, bez roślinności i zabudowy. SRTM jest modelem powierzchni
(odbicie radaru od koron drzew i dachów, siatka 90 m), więc budynki LoD1 dokładane na nim
byłyby liczone podwójnie. Wysokości są trzymane wyłącznie w pamięci procesu.

Budynki: paczki powiatowe LoD1 z opendata.geoportal.gov.pl. Powiaty, przez które biegną
łącza, wskazuje usługa PRG (WFS z granicami powiatów). Paczka jest pobierana do pliku
tymczasowego, czytana strumieniowo prosto do indeksu SQLite i usuwana — na dysku zostaje
tylko indeks.

Układ współrzędnych obu usług to PL-1992 (EPSG:2180). Usługi GUGiK podają współrzędne
w kolejności geodezyjnej (x = północna, y = wschodnia), a `wgs84_to_epsg2180` i CityGML
w kolejności (wschodnia, północna).
"""

from __future__ import annotations

import logging
import math
import re
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Sequence

import numpy as np
import requests
from geopy.distance import geodesic

from app.config import settings
from app.services.propagation.building_index import BuildingIndex, wgs84_to_epsg2180

logger = logging.getLogger(__name__)

# (wykonane, wszystkie) — postęp pobierania w jednostkach danego etapu.
ProgressCallback = Callable[[int, int], None]
Segment = tuple[float, float, float, float]  # lat1, lon1, lat2, lon2

MAX_POINTS_PER_REQUEST = 1000
MIN_PROFILE_SEGMENTS = 50
_RETRIES = 4
_ZERO_RETRIES = 3
_TIMEOUT_S = 60
# Zapytania WFS idą metodą GET (POST z filtrem usługa PRG ignoruje), więc geometria musi
# zmieścić się w adresie (limit ok. 8 KB po zakodowaniu).
_MAX_HULL_VERTICES = 60
_LINES_PER_QUERY = 25
# Otoczka obejmująca więcej powiatów oznacza scenariusz rozrzucony po regionie.
_HULL_DISTRICT_LIMIT = 4
_DISTRICT_CODE = re.compile(r"<ms:JPT_KOD_JE>(\d{4})</ms:JPT_KOD_JE>")


class GugikUnavailable(RuntimeError):
    """Usługa GUGiK nie odpowiedziała poprawnie mimo ponowień."""


def _with_retries(what: str, call: Callable[[], object]) -> object:
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            return call()
        except (requests.RequestException, ValueError, IndexError) as exc:
            last = exc
            logger.warning("%s failed (attempt %s/%s): %s", what, attempt + 1, _RETRIES, exc)
            time.sleep(2**attempt)
    raise GugikUnavailable(f"{what}: usługa GUGiK niedostępna ({last})") from last


def _sample_points(segment: Segment, step_m: float) -> tuple[np.ndarray, float]:
    """Punkty profilu w EPSG:2180 (wschodnia, północna) i długość geodezyjna odcinka."""
    lat1, lon1, lat2, lon2 = segment
    distance = float(geodesic((lat1, lon1), (lat2, lon2)).meters)
    count = max(MIN_PROFILE_SEGMENTS, math.ceil(distance / step_m))
    e1, n1 = wgs84_to_epsg2180(lat1, lon1)
    e2, n2 = wgs84_to_epsg2180(lat2, lon2)
    t = np.linspace(0.0, 1.0, count + 1)
    return np.column_stack([e1 + t * (e2 - e1), n1 + t * (n2 - n1)]), distance


def _bilinear(grid: np.ndarray, origin: tuple[float, float], step: float, points: np.ndarray) -> np.ndarray:
    gx = np.clip((points[:, 0] - origin[0]) / step, 0, grid.shape[1] - 1)
    gy = np.clip((points[:, 1] - origin[1]) / step, 0, grid.shape[0] - 1)
    x0 = np.minimum(np.floor(gx).astype(int), grid.shape[1] - 2) if grid.shape[1] > 1 else np.zeros(len(gx), int)
    y0 = np.minimum(np.floor(gy).astype(int), grid.shape[0] - 2) if grid.shape[0] > 1 else np.zeros(len(gy), int)
    x1 = np.minimum(x0 + 1, grid.shape[1] - 1)
    y1 = np.minimum(y0 + 1, grid.shape[0] - 1)
    fx, fy = gx - x0, gy - y0
    top = grid[y0, x0] * (1 - fx) + grid[y0, x1] * fx
    bottom = grid[y1, x0] * (1 - fx) + grid[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


class TerrainProvider:
    """Wysokości gruntu z NMT GUGiK z pamięciową pamięcią podręczną profili.

    `fetch` przyjmuje tablicę (k, 2) punktów EPSG:2180 (wschodnia, północna), k ≤ 1000,
    i zwraca k wysokości; domyślnie odpytuje usługę GUGiK (testy podstawiają własną).
    """

    def __init__(
        self,
        fetch: Optional[Callable[[np.ndarray], np.ndarray]] = None,
        *,
        step_m: Optional[float] = None,
        workers: Optional[int] = None,
        cache_points: int = 5_000_000,
    ) -> None:
        self._fetch = fetch or self._fetch_service
        self.step_m = step_m or settings.terrain_profile_step_m
        self.workers = workers or settings.gugik_workers
        self._cache_points = cache_points
        self._profiles: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self._cached = 0
        self._lock = threading.Lock()

    # --- usługa -------------------------------------------------------------

    @staticmethod
    def _fetch_service(points: np.ndarray) -> np.ndarray:
        values = TerrainProvider._fetch_once(points)
        # Pod obciążeniem usługa potrafi oddać 0 zamiast wysokości; takie punkty są pytane
        # ponownie. Zero, które się powtarza, jest prawdziwe (Żuławy) albo leży poza Polską.
        for attempt in range(_ZERO_RETRIES):
            zeros = np.flatnonzero(values == 0.0)
            if not len(zeros):
                break
            time.sleep(1.0 + attempt)
            values[zeros] = TerrainProvider._fetch_once(points[zeros])
        return values

    @staticmethod
    def _fetch_once(points: np.ndarray) -> np.ndarray:
        body = ",".join(f"{north:.2f} {east:.2f}" for east, north in points)

        def call() -> np.ndarray:
            response = requests.post(
                settings.gugik_nmt_url,
                data={"request": "GetHByPointList", "list": body},
                timeout=_TIMEOUT_S,
            )
            response.raise_for_status()
            values = [float(item.split()[2]) for item in response.text.strip().split(",")]
            if len(values) != len(points):
                raise ValueError(f"expected {len(points)} heights, got {len(values)}")
            return np.array(values, dtype=np.float64)

        return _with_retries("NMT GetHByPointList", call)  # type: ignore[return-value]

    def heights(self, points: np.ndarray, on_progress: Optional[ProgressCallback] = None) -> np.ndarray:
        """Wysokości punktów EPSG:2180 (wschodnia, północna), pobierane równolegle partiami."""
        points = np.asarray(points, dtype=np.float64).reshape(-1, 2)
        if len(points) == 0:
            return np.zeros(0)
        chunks = [points[i : i + MAX_POINTS_PER_REQUEST] for i in range(0, len(points), MAX_POINTS_PER_REQUEST)]
        done = 0
        progress_lock = threading.Lock()

        def task(chunk: np.ndarray) -> np.ndarray:
            nonlocal done
            result = self._fetch(chunk)
            with progress_lock:
                done += 1
                if on_progress:
                    on_progress(done, len(chunks))
            return result

        if on_progress:
            on_progress(0, len(chunks))
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(chunks)))) as pool:
            return np.concatenate(list(pool.map(task, chunks)))

    # --- profile ------------------------------------------------------------

    def elevations(self, positions: Sequence[tuple[float, float]]) -> np.ndarray:
        """Wysokość gruntu [m n.p.m.] w punktach (lat, lon)."""
        points = np.array([wgs84_to_epsg2180(lat, lon) for lat, lon in positions], dtype=np.float64)
        values = self.heights(points)
        # Poza Polską usługa zwraca 0, czego nie da się odróżnić od Żuław — tylko ostrzeżenie.
        for (lat, lon), value in zip(positions, values):
            if value == 0.0:
                logger.warning("NMT returned 0 m at %.6f, %.6f (outside Poland?)", lat, lon)
        return values

    def elevation(self, lat: float, lon: float) -> float:
        return float(self.elevations([(lat, lon)])[0])

    def profiles(
        self, segments: Sequence[Segment], on_progress: Optional[ProgressCallback] = None
    ) -> list[tuple[list[float], float]]:
        """Profil terenu (próbka co `step_m`, co najmniej 50 odcinków) i długość każdego odcinka.

        Brakujące profile są pobierane jednym z dwóch sposobów, tańszym w liczbie punktów:
        wprost (punkty profili) albo jako siatka o oczku `step_m` nad obszarem scenariusza
        z interpolacją dwuliniową — przy wielu gęsto rozmieszczonych węzłach siatka jest
        wielokrotnie mniejsza niż suma profili.
        """
        plans = []
        for segment in segments:
            points, distance = _sample_points(segment, self.step_m)
            key = (*np.round(points[0], 1), *np.round(points[-1], 1), len(points))
            plans.append((key, points, distance))
        with self._lock:
            missing = {key: points for key, points, _ in plans if key not in self._profiles}
        if missing:
            self._fill(missing, on_progress)
        with self._lock:
            out = []
            for key, _, distance in plans:
                self._profiles.move_to_end(key)
                out.append((self._profiles[key].tolist(), distance))
        return out

    def _fill(self, missing: dict[tuple, np.ndarray], on_progress: Optional[ProgressCallback]) -> None:
        all_points = np.concatenate(list(missing.values()))
        step = self.step_m
        lo = np.floor(all_points.min(axis=0) / step) * step
        hi = np.ceil(all_points.max(axis=0) / step) * step
        nx = int(round((hi[0] - lo[0]) / step)) + 1
        ny = int(round((hi[1] - lo[1]) / step)) + 1
        if nx * ny < len(all_points):
            xs = lo[0] + step * np.arange(nx)
            ys = lo[1] + step * np.arange(ny)
            gx, gy = np.meshgrid(xs, ys)
            grid = self.heights(np.column_stack([gx.ravel(), gy.ravel()]), on_progress).reshape(ny, nx)
            fetched = {key: _bilinear(grid, (lo[0], lo[1]), step, points) for key, points in missing.items()}
            logger.info("NMT grid %sx%s for %s profiles", nx, ny, len(missing))
        else:
            values = self.heights(all_points, on_progress)
            fetched, offset = {}, 0
            for key, points in missing.items():
                fetched[key] = values[offset : offset + len(points)]
                offset += len(points)
            logger.info("NMT %s points for %s profiles", len(all_points), len(missing))
        with self._lock:
            for key, values in fetched.items():
                if key not in self._profiles:
                    self._profiles[key] = values.astype(np.float32)
                    self._cached += len(values)
            while self._cached > self._cache_points and self._profiles:
                _, dropped = self._profiles.popitem(last=False)
                self._cached -= len(dropped)


class BuildingProvider:
    """Uzupełnia indeks budynków o powiaty, przez które biegną łącza scenariusza."""

    def __init__(
        self,
        index: BuildingIndex,
        *,
        district_lookup: Optional[Callable[[Sequence[Segment]], set[str]]] = None,
        download: Optional[Callable[[str, "tempfile._TemporaryFileWrapper"], bool]] = None,
    ) -> None:
        self.index = index
        self._lookup = district_lookup or self._districts_from_prg
        self._download = download or self._download_package

    def ensure(self, segments: Sequence[Segment], on_progress: Optional[ProgressCallback] = None) -> set[str]:
        """Pobiera brakujące powiaty dla odcinków (lat1, lon1, lat2, lon2); zwraca ich kody."""
        codes = self._lookup(segments)
        missing = sorted(codes - self.index.districts())
        for done, code in enumerate(missing):
            if on_progress:
                on_progress(done, len(missing))
            # Blokada na jeden powiat: inne instancje backendu nie czekają na całą listę,
            # a powiat pobrany przez nie w międzyczasie nie jest pobierany drugi raz.
            with self.index.lock():
                if code not in self.index.districts():
                    self._import(code)
        if on_progress and missing:
            on_progress(len(missing), len(missing))
        with self.index.lock():
            self.index.touch(codes)
            removed = self.index.prune(settings.buildings_index_max_mb * 1_000_000, keep=codes)
            if removed:
                logger.info("Removed least recently used LoD1 districts: %s", ", ".join(removed))
        return codes

    def _import(self, code: str) -> None:
        year = str(settings.gugik_lod1_year)
        with tempfile.TemporaryFile() as package:
            if not self._download(code, package):
                logger.warning("No LoD1 package for district %s; indexing it as empty", code)
                self.index.add_district(code, year, [])
                return
            package.seek(0)
            with zipfile.ZipFile(package) as archive:
                names = [name for name in archive.namelist() if name.lower().endswith(".gml")]
                count = self.index.add_district(code, year, (archive.open(name) for name in names))
        logger.info("Indexed %s LoD1 buildings of district %s", count, code)

    @staticmethod
    def _download_package(code: str, target) -> bool:
        url = settings.gugik_lod1_url.format(
            year=settings.gugik_lod1_year, voivodeship=code[:2], district=code
        )

        def call() -> bool:
            target.seek(0)
            target.truncate()
            with requests.get(url, stream=True, timeout=_TIMEOUT_S) as response:
                if response.status_code == 404:
                    return False
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1 << 20):
                    target.write(chunk)
            return True

        logger.info("Downloading LoD1 buildings of district %s", code)
        return bool(_with_retries(f"LoD1 {code}", call))

    @staticmethod
    def _districts_from_prg(segments: Sequence[Segment]) -> set[str]:
        """Kody TERYT powiatów przecinanych przez odcinki (WFS PRG, filtr Intersects).

        Każdy odcinek leży w otoczce wypukłej węzłów, więc dla zwartego scenariusza jedno
        zapytanie z otoczką wystarcza i pobiera najwyżej kilka powiatów za dużo. Gdy otoczka
        obejmuje więcej powiatów (węzły rozrzucone po regionie), pytamy o linie łączy —
        inaczej pobierałyby się powiaty, przez które nie biegnie żadne łącze.
        """
        lines = []
        for lat1, lon1, lat2, lon2 in segments:
            e1, n1 = wgs84_to_epsg2180(lat1, lon1)
            e2, n2 = wgs84_to_epsg2180(lat2, lon2)
            if math.hypot(e2 - e1, n2 - n1) < 1.0:
                e2 += 1.0  # odcinek zerowej długości nie jest poprawną linią
            lines.append(((e1, n1), (e2, n2)))
        if not lines:
            return set()
        hull = _convex_hull(sorted({point for line in lines for point in line}))
        if len(hull) <= _MAX_HULL_VERTICES:
            codes = _prg_districts(_gml_polygon(hull) if len(hull) >= 3 else _gml_lines([lines[0]]))
            if len(codes) <= _HULL_DISTRICT_LIMIT:
                return codes
        batches = [lines[i : i + _LINES_PER_QUERY] for i in range(0, len(lines), _LINES_PER_QUERY)]
        with ThreadPoolExecutor(max_workers=max(1, min(settings.gugik_workers, len(batches)))) as pool:
            return set().union(*pool.map(lambda batch: _prg_districts(_gml_lines(batch)), batches))


def _gml_polygon(ring: list[tuple[float, float]]) -> str:
    coords = " ".join(f"{n:.1f} {e:.1f}" for e, n in [*ring, ring[0]])
    return (
        '<gml:Polygon srsName="urn:ogc:def:crs:EPSG::2180"><gml:exterior><gml:LinearRing>'
        f"<gml:posList>{coords}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>"
    )


def _gml_lines(lines: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> str:
    members = "".join(
        "<gml:curveMember><gml:LineString><gml:posList>"
        f"{n1:.0f} {e1:.0f} {n2:.0f} {e2:.0f}"
        "</gml:posList></gml:LineString></gml:curveMember>"
        for (e1, n1), (e2, n2) in lines
    )
    return f'<gml:MultiCurve srsName="urn:ogc:def:crs:EPSG::2180">{members}</gml:MultiCurve>'


def _prg_districts(geometry: str) -> set[str]:
    """Powiaty z PRG przecinające geometrię GML (współrzędne EPSG:2180, północna pierwsza)."""
    fes = (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" '
        'xmlns:gml="http://www.opengis.net/gml/3.2"><fes:Intersects>'
        f"<fes:ValueReference>msGeometry</fes:ValueReference>{geometry}"
        "</fes:Intersects></fes:Filter>"
    )

    def call() -> set[str]:
        response = requests.get(
            settings.gugik_prg_wfs_url,
            params={
                "SERVICE": "WFS",
                "VERSION": "2.0.0",
                "REQUEST": "GetFeature",
                "TYPENAMES": "ms:A02_Granice_powiatow",
                "PROPERTYNAME": "ms:JPT_KOD_JE",
                "FILTER": fes,
            },
            timeout=_TIMEOUT_S,
        )
        response.raise_for_status()
        if "ExceptionReport" in response.text:
            raise ValueError(response.text[:300])
        return set(_DISTRICT_CODE.findall(response.text))

    return _with_retries("PRG WFS", call)  # type: ignore[return-value]


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Otoczka wypukła (algorytm monotonicznego łańcucha) posortowanych punktów."""
    if len(points) <= 2:
        return points

    def cross(o, a, b) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    upper: list[tuple[float, float]] = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    hull = lower[:-1] + upper[:-1]
    # Punkty współliniowe dają „otoczkę” z dwóch wierzchołków — wtedy linia.
    return hull if len(hull) >= 3 else [points[0], points[-1]]


terrain_provider = TerrainProvider()
