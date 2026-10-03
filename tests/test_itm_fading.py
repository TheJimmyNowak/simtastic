import random

import numpy as np
import pytest

from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig
from app.services.propagation.itm_p2p import compute_path_loss_db
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
