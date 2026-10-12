import numpy as np
import pytest

from app.config import settings
from app.services.propagation import gugik


def synthetic_terrain(points: np.ndarray) -> np.ndarray:
    """Teren zastępujący NMT w testach: płaszczyzna lekko wznosząca się na wschód."""
    return 150.0 + 0.002 * (points[:, 0] - 500_000.0)


@pytest.fixture(autouse=True)
def offline_gugik(monkeypatch, tmp_path):
    """Testy nie odpytują usług GUGiK ani nie dotykają prawdziwego indeksu budynków."""
    terrain = gugik.TerrainProvider(fetch=synthetic_terrain, workers=1)
    monkeypatch.setattr(gugik, "terrain_provider", terrain)
    monkeypatch.setattr("app.services.propagation.itm_precompute.terrain_provider", terrain)
    monkeypatch.setattr(gugik.BuildingProvider, "_districts_from_prg", staticmethod(lambda segments: set()))

    def no_network(*args, **kwargs):
        raise AssertionError("tests must not call GUGiK services")

    monkeypatch.setattr(gugik.requests, "get", no_network)
    monkeypatch.setattr(gugik.requests, "post", no_network)
    monkeypatch.setattr(settings, "buildings_index_path", tmp_path / "buildings" / "buildings.sqlite")
    return terrain
