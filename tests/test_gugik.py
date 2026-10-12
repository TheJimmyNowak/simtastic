import io
import sqlite3
import zipfile

import numpy as np
import pytest

from app.services.propagation import gugik
from app.services.propagation.building_index import BuildingIndex, wgs84_to_epsg2180
from app.services.propagation.gugik import BuildingProvider, TerrainProvider
from conftest import synthetic_terrain
from test_building_model import _citygml

# Rzeszów — kilka kilometrów między węzłami.
SEGMENTS = [
    (50.03, 21.98, 50.05, 22.02),
    (50.03, 21.98, 50.01, 22.05),
    (50.05, 22.02, 50.01, 22.05),
]


class CountingFetch:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, points: np.ndarray) -> np.ndarray:
        assert len(points) <= gugik.MAX_POINTS_PER_REQUEST
        self.calls.append(len(points))
        return synthetic_terrain(points)


def test_profiles_are_batched_and_cached() -> None:
    fetch = CountingFetch()
    terrain = TerrainProvider(fetch=fetch, step_m=30.0, workers=2)
    first = terrain.profiles(SEGMENTS)
    requested = sum(fetch.calls)
    assert requested > 0
    assert terrain.profiles(SEGMENTS) == first
    assert sum(fetch.calls) == requested  # drugi raz z pamięci, bez zapytań
    for (profile, distance), (lat1, lon1, _, _) in zip(first, SEGMENTS):
        assert len(profile) - 1 >= distance / 30.0
        start = np.array([wgs84_to_epsg2180(lat1, lon1)])
        assert profile[0] == pytest.approx(synthetic_terrain(start)[0], abs=1e-3)


def test_grid_and_point_sampling_agree_on_planar_terrain() -> None:
    # 100 krzyżujących się odcinków w kwadracie ok. 1 km: siatka ma mniej punktów niż profile.
    dense = [(50.030 + 0.0001 * k, 22.000, 50.039 - 0.0001 * k, 22.013) for k in range(100)]
    by_points = TerrainProvider(fetch=synthetic_terrain, step_m=30.0, workers=1)
    expected = by_points.profiles(dense[:1])
    fetch = CountingFetch()
    by_grid = TerrainProvider(fetch=fetch, step_m=30.0, workers=1)
    result = by_grid.profiles(dense)
    assert sum(fetch.calls) < sum(len(profile) for profile, _ in result) / 2
    assert np.allclose(result[0][0], expected[0][0], atol=1e-3)


def test_service_request_uses_geodetic_axis_order(monkeypatch) -> None:
    sent = {}

    class Response:
        text = "245867.06 714663.84 206.4,245900.00 714700.00 206.6"

        def raise_for_status(self) -> None:
            pass

    def post(url, data, timeout):
        sent.update(data)
        return Response()

    monkeypatch.setattr(gugik.requests, "post", post)
    heights = TerrainProvider._fetch_service(np.array([[714663.84, 245867.06], [714700.0, 245900.0]]))
    assert sent["request"] == "GetHByPointList"
    assert sent["list"].startswith("245867.06 714663.84,")  # północna, potem wschodnia
    assert heights.tolist() == [206.4, 206.6]


def test_truncated_service_response_is_an_error(monkeypatch) -> None:
    class Response:
        text = "1 2 3.0,4 5"

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(gugik.requests, "post", lambda *a, **k: Response())
    monkeypatch.setattr(gugik.time, "sleep", lambda s: None)
    with pytest.raises(gugik.GugikUnavailable):
        TerrainProvider._fetch_service(np.array([[1.0, 2.0], [4.0, 5.0]]))


def _package(target) -> bool:
    gml = _citygml([("b1", [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0)], 100.0, 112.0)])
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("1863/1863_test.gml", gml)
    return True


def test_buildings_are_downloaded_once_per_district(tmp_path) -> None:
    downloads: list[str] = []

    def download(code, target):
        downloads.append(code)
        return _package(target) if code == "1863" else False

    index = BuildingIndex(tmp_path / "b.sqlite")
    provider = BuildingProvider(index, district_lookup=lambda segments: {"1863", "1816"}, download=download)
    progress: list[tuple[int, int]] = []
    assert provider.ensure(SEGMENTS, lambda done, total: progress.append((done, total))) == {"1863", "1816"}
    assert sorted(downloads) == ["1816", "1863"]
    assert progress[-1] == (2, 2)
    assert index.districts() == {"1863", "1816"}  # 1816 bez paczki — zapisany jako pusty
    [building] = index.query(0, 30, 0, 30)
    assert building.height_m == pytest.approx(12.0)

    provider.ensure(SEGMENTS)
    assert len(downloads) == 2


def test_reimporting_a_district_replaces_its_buildings(tmp_path) -> None:
    index = BuildingIndex(tmp_path / "b.sqlite")
    for _ in range(2):
        buffer = io.BytesIO()
        _package(buffer)
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            index.add_district("1863", "2024", [archive.open(n) for n in archive.namelist()])
    assert len(index.query(0, 30, 0, 30)) == 1


def test_old_index_format_is_replaced(tmp_path) -> None:
    path = tmp_path / "b.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    index = BuildingIndex(path)
    assert index.districts() == set()
    assert index.query(0, 1, 0, 1) == []


def test_convex_hull_covers_all_nodes() -> None:
    points = sorted([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (5.0, 5.0), (2.0, 8.0)])
    assert sorted(gugik._convex_hull(points)) == [(0.0, 0.0), (0.0, 10.0), (10.0, 0.0), (10.0, 10.0)]
    assert gugik._convex_hull(sorted([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)])) == [(0.0, 0.0), (2.0, 2.0)]


def test_least_recently_used_districts_are_pruned(tmp_path, monkeypatch) -> None:
    index = BuildingIndex(tmp_path / "b.sqlite")
    downloads: list[str] = []

    def download(code, target):
        downloads.append(code)
        return _package(target)

    wanted = {"lookup": {"0001"}}
    provider = BuildingProvider(index, district_lookup=lambda s: wanted["lookup"], download=download)
    monkeypatch.setattr(gugik.settings, "buildings_index_max_mb", 1000)
    for code in ("0001", "0002", "0003"):
        wanted["lookup"] = {code}
        provider.ensure(SEGMENTS)
    assert index.districts() == {"0001", "0002", "0003"}

    monkeypatch.setattr(gugik.settings, "buildings_index_max_mb", 0)
    wanted["lookup"] = {"0002"}
    provider.ensure(SEGMENTS)
    # Limit 0 usuwa wszystko poza powiatami bieżącego scenariusza, bez ponownego pobrania.
    assert index.districts() == {"0002"}
    assert downloads == ["0001", "0002", "0003"]


def test_spurious_zero_heights_are_requested_again(monkeypatch) -> None:
    answers = iter(["1 2 0.0,3 4 50.0", "1 2 120.5"])

    class Response:
        def __init__(self) -> None:
            self.text = next(answers)

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(gugik.requests, "post", lambda *a, **k: Response())
    monkeypatch.setattr(gugik.time, "sleep", lambda s: None)
    heights = TerrainProvider._fetch_service(np.array([[2.0, 1.0], [4.0, 3.0]]))
    assert heights.tolist() == [120.5, 50.0]
