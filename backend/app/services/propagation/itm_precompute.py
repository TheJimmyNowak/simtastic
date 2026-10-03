from __future__ import annotations

import logging
import math
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from app.config import settings
from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig
from app.services.propagation.itm_p2p import compute_path_loss_db
from app.services.propagation.building_index import BuildingIndex
from app.services.propagation.building_model import compute_building_path, resolve_node_mount
from app.services.propagation.link_matrix import (
    LinkMatrix,
    build_connectivity_map,
    compute_scenario_fingerprint,
    save_link_matrix,
)
from app.services.propagation.srtm_profile import build_terrain_profile
from app.services.rf_link_model import compute_rssi_db
from app.services.scenario_store import scenario_store

logger = logging.getLogger(__name__)


def _pair_loss(
    srtm_dir: str,
    building_db: str | None,
    env_dict: dict,
    tx: dict,
    rx: dict,
) -> tuple[str, str, float, float, float, float, int, float, str, str, dict, dict]:
    env = EnvironmentConfig.model_validate(env_dict)
    tx_n = NodeConfig.model_validate(tx)
    rx_n = NodeConfig.model_validate(rx)
    lora = env.resolve_lora()
    profile, dist_m = build_terrain_profile(
        Path(srtm_dir),
        tx_n.lat,
        tx_n.lon,
        rx_n.lat,
        rx_n.lon,
    )
    tx_height_agl = tx_n.agl_m
    rx_height_agl = rx_n.agl_m
    building_result = None
    tx_mount = None
    rx_mount = None
    if building_db:
        building_index = BuildingIndex(Path(building_db))
        tx_mount = resolve_node_mount(
            building_index, tx_n.lat, tx_n.lon, profile[0], tx_n.agl_m
        )
        rx_mount = resolve_node_mount(
            building_index, rx_n.lat, rx_n.lon, profile[-1], rx_n.agl_m
        )
        tx_height_agl = max(0.1, tx_mount.antenna_z_m - profile[0])
        rx_height_agl = max(0.1, rx_mount.antenna_z_m - profile[-1])
        building_result = compute_building_path(
            building_index,
            tx_mount,
            rx_mount,
            lora.frequency_mhz,
            env.fresnel_clearance_ratio,
            terrain_profile_m=profile,
        )
    terrain_loss = compute_path_loss_db(
        profile,
        dist_m / 1000.0,
        tx_height_agl,
        rx_height_agl,
        lora.frequency_mhz,
        climate_klim=env.itm_climate,
        ipol=env.itm_polarization,
    )
    building_loss = building_result.loss_db if building_result else 0.0
    pl = terrain_loss + building_loss
    rssi = compute_rssi_db(
        lora.tx_power_dbm,
        tx_n.antenna_gain_tx_db,
        rx_n.antenna_gain_rx_db,
        pl,
        0.0,
    )
    mount_dict = lambda mount, node: {
        "placement": mount.placement if mount else "terrain",
        "building_id": mount.building_id if mount else None,
        "surface_z_m": mount.surface_z_m if mount else None,
        "antenna_z_m": mount.antenna_z_m if mount else None,
        "mast_height_m": node.agl_m,
    }
    return (
        tx_n.id,
        rx_n.id,
        terrain_loss,
        building_loss,
        rssi,
        dist_m,
        building_result.blocked_count if building_result else 0,
        building_result.min_fresnel_clearance_m if building_result else math.inf,
        building_result.dominant_building_id or "" if building_result else "",
        building_result.data_year or "" if building_result else "",
        mount_dict(tx_mount, tx_n),
        mount_dict(rx_mount, rx_n),
    )


def precompute_link_matrix(
    scenario_id: str,
    nodes: list[NodeConfig],
    env: EnvironmentConfig,
    *,
    on_progress: object | None = None,
) -> LinkMatrix:
    n = len(nodes)
    node_ids = [nd.id for nd in nodes]
    idx = {nd.id: i for i, nd in enumerate(nodes)}
    path_loss = np.full((n, n), np.nan, dtype=np.float64)
    terrain_loss = np.full((n, n), np.nan, dtype=np.float64)
    building_loss = np.zeros((n, n), dtype=np.float64)
    median_rssi = np.full((n, n), -999.0, dtype=np.float64)
    distance_m = np.zeros((n, n), dtype=np.float64)
    blocked_count = np.zeros((n, n), dtype=np.int32)
    fresnel_clearance = np.full((n, n), np.inf, dtype=np.float64)
    dominant_building = np.full((n, n), "", dtype=object)
    building_data_year = np.full((n, n), "", dtype=object)
    node_placement: dict[str, dict] = {}
    np.fill_diagonal(path_loss, 0.0)
    np.fill_diagonal(terrain_loss, 0.0)
    np.fill_diagonal(median_rssi, -999.0)

    srtm_dir = str(settings.srtm_cache_dir.resolve())
    env_dict = env.model_dump()
    node_dicts = [nd.model_dump() for nd in nodes]
    building_db: str | None = None
    if env.building_model_enabled:
        index = BuildingIndex(settings.buildings_index_path)
        index.build(settings.buildings_raw_dir)
        building_db = str(settings.buildings_index_path.resolve())
    tasks: list[tuple[int, int]] = [(i, j) for i in range(n) for j in range(n) if i != j]
    total = len(tasks)
    done = 0

    workers = max(1, min(total, (__import__("os").cpu_count() or 4) - 1))
    # FastAPI, Meshtastic and Influx use background threads. Forking such a process can
    # inherit locked synchronization primitives, so workers must start cleanly.
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        futures = {
            pool.submit(
                _pair_loss,
                srtm_dir,
                building_db,
                env_dict,
                node_dicts[i],
                node_dicts[j],
            ): (i, j)
            for i, j in tasks
        }
        for fut in as_completed(futures):
            i, j = futures[fut]
            (
                tx_id, rx_id, terrain, buildings, rssi, dist, blocked,
                clearance, dominant, data_year, tx_mount, rx_mount,
            ) = fut.result()
            ii, jj = idx[tx_id], idx[rx_id]
            terrain_loss[ii, jj] = terrain
            building_loss[ii, jj] = buildings
            path_loss[ii, jj] = terrain + buildings
            median_rssi[ii, jj] = rssi
            distance_m[ii, jj] = dist
            blocked_count[ii, jj] = blocked
            fresnel_clearance[ii, jj] = clearance
            dominant_building[ii, jj] = dominant
            building_data_year[ii, jj] = data_year
            node_placement[tx_id] = tx_mount
            node_placement[rx_id] = rx_mount
            done += 1
            if on_progress:
                on_progress(done, total)

    neighbors = build_connectivity_map(
        node_ids, median_rssi, env.connectivity_threshold_dbm
    )
    matrix = LinkMatrix(
        node_ids=node_ids,
        path_loss_median_db=path_loss,
        median_rssi_db=median_rssi,
        distance_m=distance_m,
        connectivity_neighbors=neighbors,
        terrain_loss_db=terrain_loss,
        building_loss_db=building_loss,
        blocked_buildings_count=blocked_count,
        fresnel_clearance_m=fresnel_clearance,
        dominant_building_id=dominant_building,
        building_data_year=building_data_year,
        node_placement=node_placement,
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

    def progress(done: int, total: int) -> None:
        scenario_store.set_itm_progress(scenario_id, done, total)

    try:
        precompute_link_matrix(
            scenario_id,
            scenario.nodes,
            scenario.environment,
            on_progress=progress,
        )
        from app.models.scenario import ScenarioStatus

        scenario_store.set_status(scenario_id, ScenarioStatus.LINKS_READY)
    except Exception as exc:
        logger.exception("ITM precompute failed")
        from app.models.scenario import ScenarioStatus

        scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))
        raise
