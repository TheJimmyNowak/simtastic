from __future__ import annotations

import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from app.config import settings
from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig
from app.models.scenario import ScenarioStatus
from app.services.propagation.building_index import BuildingIndex
from app.services.propagation.building_model import (
    BuildingPathResult,
    NodeMount,
    compute_building_path,
    resolve_node_mount,
)
from app.services.propagation.itm_p2p import compute_path_loss_db
from app.services.propagation.link_matrix import (
    LinkMatrix,
    build_connectivity_map,
    compute_scenario_fingerprint,
    save_link_matrix,
)
from app.services.propagation.srtm_profile import build_terrain_profile, elevation_m
from app.services.rf_link_model import compute_rssi_db
from app.services.scenario_store import scenario_store

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class PairResult:
    """Wynik dla nieuporządkowanej pary (i, j); ITM liczony w obu kierunkach."""

    i: int
    j: int
    distance_m: float
    terrain_loss_ij_db: float
    terrain_loss_ji_db: float
    buildings: BuildingPathResult


def _antenna_height_agl(
    node: NodeConfig, mount: Optional[NodeMount], ground_z_m: float
) -> float:
    if mount is None:
        return node.agl_m
    return max(0.1, mount.antenna_z_m - ground_z_m)


def _pair_task(
    srtm_dir: str,
    building_db: Optional[str],
    env_dict: dict,
    i: int,
    j: int,
    node_i: dict,
    node_j: dict,
    mount_i: Optional[dict],
    mount_j: Optional[dict],
) -> PairResult:
    """Run in a worker process: one terrain profile and building pass per pair.

    The building model is symmetric, so it is evaluated once. ITM (itmlogic) is not
    exactly reciprocal (differences of a few dB), so both directions are computed.
    """
    env = EnvironmentConfig.model_validate(env_dict)
    a = NodeConfig.model_validate(node_i)
    b = NodeConfig.model_validate(node_j)
    mount_a = NodeMount(**mount_i) if mount_i else None
    mount_b = NodeMount(**mount_j) if mount_j else None
    frequency = env.resolve_lora().frequency_mhz

    profile, distance_m = build_terrain_profile(Path(srtm_dir), a.lat, a.lon, b.lat, b.lon)
    height_a = _antenna_height_agl(a, mount_a, profile[0])
    height_b = _antenna_height_agl(b, mount_b, profile[-1])

    def itm(path: list[float], h_tx: float, h_rx: float) -> float:
        return compute_path_loss_db(
            path,
            distance_m / 1000.0,
            h_tx,
            h_rx,
            frequency,
            climate_klim=env.itm_climate,
            ipol=env.itm_polarization,
        )

    buildings = BuildingPathResult()
    if building_db and mount_a and mount_b:
        buildings = compute_building_path(
            BuildingIndex(Path(building_db)),
            mount_a,
            mount_b,
            frequency,
            env.fresnel_clearance_ratio,
            terrain_profile_m=profile,
        )
    return PairResult(
        i=i,
        j=j,
        distance_m=distance_m,
        terrain_loss_ij_db=itm(profile, height_a, height_b),
        terrain_loss_ji_db=itm(profile[::-1], height_b, height_a),
        buildings=buildings,
    )


def _resolve_mounts(
    nodes: list[NodeConfig], index: Optional[BuildingIndex]
) -> list[Optional[NodeMount]]:
    if index is None:
        return [None] * len(nodes)
    srtm_dir = settings.srtm_cache_dir
    return [
        resolve_node_mount(index, n.lat, n.lon, elevation_m(srtm_dir, n.lat, n.lon), n.agl_m)
        for n in nodes
    ]


def _placement(node: NodeConfig, mount: Optional[NodeMount]) -> dict:
    return {
        "placement": mount.placement if mount else "terrain",
        "building_id": mount.building_id if mount else None,
        "surface_z_m": mount.surface_z_m if mount else None,
        "antenna_z_m": mount.antenna_z_m if mount else None,
        "mast_height_m": node.agl_m,
    }


def precompute_link_matrix(
    scenario_id: str,
    nodes: list[NodeConfig],
    env: EnvironmentConfig,
    *,
    on_progress: Optional[ProgressCallback] = None,
) -> LinkMatrix:
    n = len(nodes)
    lora = env.resolve_lora()
    path_loss = np.zeros((n, n), dtype=np.float64)
    terrain_loss = np.zeros((n, n), dtype=np.float64)
    building_loss = np.zeros((n, n), dtype=np.float64)
    median_rssi = np.full((n, n), -999.0, dtype=np.float64)
    distance_m = np.zeros((n, n), dtype=np.float64)
    blocked_count = np.zeros((n, n), dtype=np.int32)
    fresnel_clearance = np.full((n, n), np.inf, dtype=np.float64)
    dominant_building = np.full((n, n), "", dtype=object)
    building_data_year = np.full((n, n), "", dtype=object)

    building_index: Optional[BuildingIndex] = None
    if env.building_model_enabled:
        building_index = BuildingIndex(settings.buildings_index_path)
        building_index.build(settings.buildings_raw_dir)
    mounts = _resolve_mounts(nodes, building_index)

    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    total_directions = 2 * len(pairs)
    done = 0
    srtm_dir = str(settings.srtm_cache_dir.resolve())
    building_db = str(settings.buildings_index_path.resolve()) if building_index else None
    env_dict = env.model_dump()
    node_dicts = [node.model_dump() for node in nodes]
    mount_dicts = [asdict(mount) if mount else None for mount in mounts]

    workers = max(1, min(len(pairs), (os.cpu_count() or 4) - 1))
    # FastAPI, Meshtastic and Influx use background threads. Forking such a process can
    # inherit locked synchronization primitives, so workers must start cleanly.
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = [
            pool.submit(
                _pair_task, srtm_dir, building_db, env_dict, i, j,
                node_dicts[i], node_dicts[j], mount_dicts[i], mount_dicts[j],
            )
            for i, j in pairs
        ]
        for future in as_completed(futures):
            r = future.result()
            b = r.buildings
            for tx, rx, terrain in ((r.i, r.j, r.terrain_loss_ij_db), (r.j, r.i, r.terrain_loss_ji_db)):
                terrain_loss[tx, rx] = terrain
                building_loss[tx, rx] = b.loss_db
                path_loss[tx, rx] = terrain + b.loss_db
                median_rssi[tx, rx] = compute_rssi_db(
                    lora.tx_power_dbm,
                    nodes[tx].antenna_gain_tx_db,
                    nodes[rx].antenna_gain_rx_db,
                    path_loss[tx, rx],
                    0.0,
                )
                distance_m[tx, rx] = r.distance_m
                blocked_count[tx, rx] = b.blocked_count
                fresnel_clearance[tx, rx] = b.min_fresnel_clearance_m
                dominant_building[tx, rx] = b.dominant_building_id or ""
                building_data_year[tx, rx] = b.data_year or ""
            done += 2
            if on_progress:
                on_progress(done, total_directions)

    node_ids = [node.id for node in nodes]
    matrix = LinkMatrix(
        node_ids=node_ids,
        path_loss_median_db=path_loss,
        median_rssi_db=median_rssi,
        distance_m=distance_m,
        connectivity_neighbors=build_connectivity_map(
            node_ids, median_rssi, env.connectivity_threshold_dbm
        ),
        terrain_loss_db=terrain_loss,
        building_loss_db=building_loss,
        blocked_buildings_count=blocked_count,
        fresnel_clearance_m=fresnel_clearance,
        dominant_building_id=dominant_building,
        building_data_year=building_data_year,
        node_placement={node.id: _placement(node, mount) for node, mount in zip(nodes, mounts)},
        building_model_enabled=env.building_model_enabled,
        scenario_fingerprint=compute_scenario_fingerprint(nodes, env),
    )
    save_link_matrix(scenario_id, matrix)
    return matrix


def run_precompute(scenario_id: str) -> None:
    scenario = scenario_store.get(scenario_id)
    if not scenario:
        raise ValueError(f"Scenario {scenario_id} not found")
    if len(scenario.nodes) < 2:
        raise ValueError("At least two nodes required for ITM precompute")
    try:
        precompute_link_matrix(
            scenario_id,
            scenario.nodes,
            scenario.environment,
            on_progress=lambda done, total: scenario_store.set_progress(scenario_id, done, total),
        )
        scenario_store.set_status(scenario_id, ScenarioStatus.LINKS_READY)
    except Exception as exc:
        logger.exception("ITM precompute failed")
        scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))
        raise

