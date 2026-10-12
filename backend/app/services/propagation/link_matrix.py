from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
import math
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import settings


@dataclass(frozen=True)
class LinkDetails:
    """Medianowe (bez fadingu) parametry skierowanego łącza TX→RX."""

    path_loss_db: float
    terrain_loss_db: float
    building_loss_db: float
    link_shadowing_db: float
    median_rssi_db: float
    distance_m: float
    blocked_buildings_count: int
    fresnel_clearance_m: float | None
    dominant_building_id: str | None
    building_data_year: str | None


@dataclass
class LinkMatrix:
    node_ids: list[str]
    path_loss_median_db: np.ndarray
    median_rssi_db: np.ndarray
    distance_m: np.ndarray
    connectivity_neighbors: dict[str, list[str]]
    terrain_loss_db: np.ndarray | None = None
    building_loss_db: np.ndarray | None = None
    link_shadowing_db: np.ndarray | None = None
    blocked_buildings_count: np.ndarray | None = None
    fresnel_clearance_m: np.ndarray | None = None
    dominant_building_id: np.ndarray | None = None
    building_data_year: np.ndarray | None = None
    node_placement: dict[str, dict] | None = None
    building_model_enabled: bool = False
    scenario_fingerprint: str = ""

    @property
    def n(self) -> int:
        return len(self.node_ids)

    @property
    def propagation_model(self) -> str:
        return "itm+p526_buildings" if self.building_model_enabled else "itm"

    @cached_property
    def _index_by_id(self) -> dict[str, int]:
        return {node_id: i for i, node_id in enumerate(self.node_ids)}

    def index(self, node_id: str) -> int:
        return self._index_by_id[node_id]

    def link(self, tx: int, rx: int) -> LinkDetails:
        def optional(array: np.ndarray | None, cast, default):
            return cast(array[tx, rx]) if array is not None else default

        clearance = optional(self.fresnel_clearance_m, float, None)
        path_loss = float(self.path_loss_median_db[tx, rx])
        return LinkDetails(
            path_loss_db=path_loss,
            terrain_loss_db=optional(self.terrain_loss_db, float, path_loss),
            building_loss_db=optional(self.building_loss_db, float, 0.0),
            link_shadowing_db=optional(self.link_shadowing_db, float, 0.0),
            median_rssi_db=float(self.median_rssi_db[tx, rx]),
            distance_m=float(self.distance_m[tx, rx]),
            blocked_buildings_count=optional(self.blocked_buildings_count, int, 0),
            fresnel_clearance_m=clearance if clearance is not None and math.isfinite(clearance) else None,
            dominant_building_id=optional(self.dominant_building_id, str, "") or None,
            building_data_year=optional(self.building_data_year, str, "") or None,
        )


def matrix_path(scenario_id: str) -> Path:
    return settings.data_dir / "scenarios" / scenario_id / "link_matrix.npz"


def delete_link_matrix(scenario_id: str) -> None:
    """Invalidate precompute results after any scenario edit."""
    matrix_path(scenario_id).unlink(missing_ok=True)


def compute_scenario_fingerprint(nodes, environment) -> str:
    node_fields = (
        "id", "lat", "lon", "agl_m", "antenna_gain_tx_db", "antenna_gain_rx_db"
    )
    environment_fields = (
        "preset", "custom_frequency_mhz", "custom_bandwidth_khz",
        "custom_spreading_factor", "custom_coding_rate", "custom_tx_power_dbm",
        "custom_sensitivity_dbm", "itm_climate", "itm_polarization",
        "connectivity_threshold_dbm", "building_model_enabled", "fresnel_clearance_ratio",
        "clutter_loss_db", "link_shadowing_sigma_db", "link_shadowing_seed",
        "node_installation_sigma_db", "link_blocked_fraction",
    )
    payload = {
        "nodes": [
            {field: getattr(node, field) for field in node_fields}
            for node in nodes
        ],
        "environment": {
            field: getattr(environment, field) for field in environment_fields
        },
        # Źródło terenu jest ustawieniem backendu, a nie scenariusza — też unieważnia macierz.
        "terrain": {"source": "gugik-nmt", "step_m": settings.terrain_profile_step_m},
        "model_version": 6,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def save_link_matrix(scenario_id: str, matrix: LinkMatrix) -> Path:
    path = matrix_path(scenario_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        node_ids=np.array(matrix.node_ids, dtype=object),
        path_loss_median_db=matrix.path_loss_median_db,
        median_rssi_db=matrix.median_rssi_db,
        distance_m=matrix.distance_m,
        connectivity_neighbors=np.array(matrix.connectivity_neighbors, dtype=object),
        terrain_loss_db=matrix.terrain_loss_db,
        building_loss_db=matrix.building_loss_db,
        link_shadowing_db=matrix.link_shadowing_db,
        blocked_buildings_count=matrix.blocked_buildings_count,
        fresnel_clearance_m=matrix.fresnel_clearance_m,
        dominant_building_id=matrix.dominant_building_id,
        building_data_year=matrix.building_data_year,
        node_placement=np.array(matrix.node_placement or {}, dtype=object),
        building_model_enabled=np.array(matrix.building_model_enabled),
        scenario_fingerprint=np.array(matrix.scenario_fingerprint),
    )
    return path


def load_link_matrix(scenario_id: str) -> Optional[LinkMatrix]:
    path = matrix_path(scenario_id)
    if not path.exists():
        return None
    data = np.load(path, allow_pickle=True)
    neighbors = data["connectivity_neighbors"].item()
    if not isinstance(neighbors, dict):
        neighbors = dict(neighbors)
    path_loss = data["path_loss_median_db"]
    shape = path_loss.shape
    return LinkMatrix(
        node_ids=list(data["node_ids"]),
        path_loss_median_db=path_loss,
        median_rssi_db=data["median_rssi_db"],
        distance_m=data["distance_m"],
        connectivity_neighbors=neighbors,
        terrain_loss_db=data["terrain_loss_db"] if "terrain_loss_db" in data else path_loss.copy(),
        building_loss_db=data["building_loss_db"] if "building_loss_db" in data else np.zeros(shape),
        link_shadowing_db=data["link_shadowing_db"] if "link_shadowing_db" in data else np.zeros(shape),
        blocked_buildings_count=data["blocked_buildings_count"] if "blocked_buildings_count" in data else np.zeros(shape, dtype=int),
        fresnel_clearance_m=data["fresnel_clearance_m"] if "fresnel_clearance_m" in data else np.full(shape, np.inf),
        dominant_building_id=data["dominant_building_id"] if "dominant_building_id" in data else np.full(shape, "", dtype=object),
        building_data_year=data["building_data_year"] if "building_data_year" in data else np.full(shape, "", dtype=object),
        node_placement=data["node_placement"].item() if "node_placement" in data else {},
        building_model_enabled=bool(data["building_model_enabled"].item()) if "building_model_enabled" in data else False,
        scenario_fingerprint=str(data["scenario_fingerprint"].item()) if "scenario_fingerprint" in data else "",
    )


def build_connectivity_map(
    node_ids: list[str],
    median_rssi_db: np.ndarray,
    threshold_dbm: float,
) -> dict[str, list[str]]:
    """Sparse neighbor list per node where median RSSI meets threshold."""
    n = len(node_ids)
    neighbors: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            val = float(median_rssi_db[i, j])
            if val >= threshold_dbm:
                neighbors[node_ids[i]].append(node_ids[j])
    return neighbors
