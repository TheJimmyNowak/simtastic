import random

import numpy as np
import pytest

from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig
from app.services.propagation.itm_p2p import compute_path_loss_db
from app.services.propagation import itm_precompute
from app.services.propagation.itm_precompute import _pair_task
from app.services.propagation.link_matrix import build_connectivity_map
from app.services.propagation.srtm_profile import build_terrain_profile
from app.config import settings


@pytest.fixture
def poland_nodes() -> tuple[NodeConfig, NodeConfig]:
    a = NodeConfig(id="a", name="A", lat=52.0, lon=21.0, agl_m=10.0)
    b = NodeConfig(id="b", name="B", lat=52.05, lon=21.1, agl_m=5.0)
    return a, b


def test_srtm_profile_reads_cache(poland_nodes: tuple[NodeConfig, NodeConfig]) -> None:
    a, b = poland_nodes
    profile, dist_m = build_terrain_profile(settings.srtm_cache_dir, a.lat, a.lon, b.lat, b.lon)
    assert len(profile) >= 2
    assert dist_m > 0


def test_itm_path_loss_positive(poland_nodes: tuple[NodeConfig, NodeConfig]) -> None:
    a, b = poland_nodes
    profile, dist_m = build_terrain_profile(settings.srtm_cache_dir, a.lat, a.lon, b.lat, b.lon)
    pl = compute_path_loss_db(profile, dist_m / 1000.0, a.agl_m, b.agl_m, 915.0)
    assert pl > 0


def test_fading_delivery_threshold() -> None:
    env = EnvironmentConfig(fading_sigma_db=6.0)
    lora = env.resolve_lora()
    # Borderline median: roughly 50% delivery over many trials with shadowing
    median_rssi = lora.sensitivity_dbm + 2.0
    pl = lora.tx_power_dbm + 4.0 - median_rssi
    rng = random.Random(42)
    delivered = 0
    trials = 1000
    for _ in range(trials):
        shadow = rng.gauss(0, env.fading_sigma_db)
        rssi = lora.tx_power_dbm + 2.0 + 2.0 - pl + shadow
        if rssi >= lora.sensitivity_dbm:
            delivered += 1
    ratio = delivered / trials
    assert 0.2 < ratio < 0.8


def test_connectivity_map() -> None:
    ids = ["a", "b", "c"]
    rssi = np.array(
        [
            [-999, -100, -150],
            [-110, -999, -105],
            [-140, -108, -999],
        ]
    )
    neighbors = build_connectivity_map(ids, rssi, -130.0)
    assert "b" in neighbors["a"]
    assert "c" not in neighbors["a"]


def test_colocated_nodes_get_finite_path_loss() -> None:
    # Węzły z pozycją skwantowaną przez precision_bits często mają identyczne współrzędne.
    a = NodeConfig(id="a", lat=52.2297, lon=21.0122, agl_m=2.0)
    b = NodeConfig(id="b", lat=52.2297, lon=21.0122, agl_m=2.0)
    env = EnvironmentConfig(building_model_enabled=False)
    result = _pair_task(
        str(settings.srtm_cache_dir), None, env.model_dump(), 0, 1,
        a.model_dump(), b.model_dump(), None, None,
    )
    assert result.distance_m == 0
    # Strata wolnej przestrzeni na minimalnym dystansie 1 m przy 869,5 MHz.
    assert result.terrain_loss_ij_db == pytest.approx(31.2, abs=0.1)
    assert result.terrain_loss_ji_db == pytest.approx(31.2, abs=0.1)


def test_noise_floor_and_sensitivity_are_consistent() -> None:
    lora = EnvironmentConfig(preset="MEDIUM_FAST").resolve_lora()
    # 250 kHz, NF 6 dB: -174 + 54 + 6 = -114 dBm; SF9 demoduluje do SNR -12,5 dB.
    assert lora.noise_floor_dbm == pytest.approx(-114.0)
    assert lora.sensitivity_dbm - lora.noise_floor_dbm == pytest.approx(-12.5)
    assert lora.tx_power_dbm == pytest.approx(22.0)


def test_terrain_profile_follows_srtm_resolution(poland_nodes: tuple[NodeConfig, NodeConfig]) -> None:
    a, b = poland_nodes
    profile, dist_m = build_terrain_profile(settings.srtm_cache_dir, a.lat, a.lon, b.lat, b.lon)
    assert len(profile) - 1 >= dist_m / 90.0
    short, _ = build_terrain_profile(settings.srtm_cache_dir, a.lat, a.lon, a.lat, a.lon + 0.001)
    assert len(short) - 1 == 50


def test_clutter_loss_is_added_to_every_link(monkeypatch) -> None:
    monkeypatch.setattr(itm_precompute, "save_link_matrix", lambda scenario_id, matrix: None)
    nodes = [
        NodeConfig(id="a", lat=52.20, lon=20.95, agl_m=6.0),
        NodeConfig(id="b", lat=52.20, lon=20.97, agl_m=6.0),
    ]
    base = EnvironmentConfig(building_model_enabled=False)
    plain = itm_precompute.precompute_link_matrix("t", nodes, base)
    cluttered = itm_precompute.precompute_link_matrix(
        "t", nodes, base.model_copy(update={"clutter_loss_db": 20.0})
    )
    assert cluttered.path_loss_median_db[0, 1] == pytest.approx(plain.path_loss_median_db[0, 1] + 20.0)
    assert cluttered.median_rssi_db[1, 0] == pytest.approx(plain.median_rssi_db[1, 0] - 20.0)
    assert cluttered.terrain_loss_db[0, 1] == pytest.approx(plain.terrain_loss_db[0, 1])
