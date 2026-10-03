from pathlib import Path
import math
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.models.scenario import ScenarioStatus
from app.services.propagation.link_matrix import load_link_matrix
from app.services.scenario_store import scenario_store
from app.services.shard_manager import ShardManager
from app.services.simulation_manager import simulation_manager

router = APIRouter(prefix="/api/scenarios/{scenario_id}", tags=["simulation"])


def _finite_matrix(values) -> list | None:
    if values is None:
        return None
    return [
        [float(value) if isinstance(value, (float, int)) and math.isfinite(float(value)) else None for value in row]
        for row in values.tolist()
    ]


class StartBody(BaseModel):
    traffic_config: Optional[str] = None


@router.post("/precompute-links")
def precompute_links(scenario_id: str) -> dict:
    s = scenario_store.get(scenario_id)
    if not s:
        raise HTTPException(404, "Scenario not found")
    try:
        simulation_manager.precompute_links(scenario_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "started"}


@router.get("/links")
def get_links(scenario_id: str) -> dict:
    matrix = load_link_matrix(scenario_id)
    if not matrix:
        raise HTTPException(404, "Link matrix not computed")
    n = matrix.n
    return {
        "node_ids": matrix.node_ids,
        "median_rssi_db": matrix.median_rssi_db.tolist(),
        "path_loss_median_db": matrix.path_loss_median_db.tolist(),
        "terrain_loss_db": matrix.terrain_loss_db.tolist() if matrix.terrain_loss_db is not None else None,
        "building_loss_db": matrix.building_loss_db.tolist() if matrix.building_loss_db is not None else None,
        "blocked_buildings_count": matrix.blocked_buildings_count.tolist() if matrix.blocked_buildings_count is not None else None,
        "fresnel_clearance_m": _finite_matrix(matrix.fresnel_clearance_m),
        "dominant_building_id": matrix.dominant_building_id.tolist() if matrix.dominant_building_id is not None else None,
        "building_data_year": matrix.building_data_year.tolist() if matrix.building_data_year is not None else None,
        "node_placement": matrix.node_placement or {},
        "propagation_model": "itm+p526_buildings" if matrix.building_model_enabled else "itm",
        "distance_m": matrix.distance_m.tolist(),
        "connectivity_neighbors": matrix.connectivity_neighbors,
        "n": n,
    }


@router.post("/start")
def start_simulation(scenario_id: str, body: StartBody = StartBody()) -> dict:
    s = scenario_store.get(scenario_id)
    if not s:
        raise HTTPException(404, "Scenario not found")
    traffic_path = Path(body.traffic_config) if body.traffic_config else None
    try:
        simulation_manager.start_async(scenario_id, traffic_path)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))
        raise HTTPException(500, str(exc)) from exc
    return {"status": "starting"}


@router.post("/stop")
def stop_simulation(scenario_id: str) -> dict:
    if not scenario_store.get(scenario_id):
        raise HTTPException(404, "Scenario not found")
    removed = simulation_manager.force_stop(scenario_id)
    return {"status": "stopped", "containers_removed": removed}


@router.post("/cleanup-containers")
def cleanup_scenario_containers(scenario_id: str) -> dict:
    """Force-remove Docker shards even when simulation is not tracked in memory."""
    if not scenario_store.get(scenario_id):
        raise HTTPException(404, "Scenario not found")
    removed = ShardManager.cleanup_scenario_containers(scenario_id)
    scenario_store.set_status(scenario_id, ScenarioStatus.STOPPED)
    return {"containers_removed": removed}


@router.get("/status")
def simulation_status(scenario_id: str) -> dict:
    s = scenario_store.get(scenario_id)
    if not s:
        raise HTTPException(404, "Scenario not found")
    st = simulation_manager.status()
    return {
        "scenario_status": s.status,
        "error_message": s.error_message,
        "itm_progress": s.itm_progress,
        "itm_total": s.itm_total,
        "dry_run": settings.dry_run,
        **st,
    }
