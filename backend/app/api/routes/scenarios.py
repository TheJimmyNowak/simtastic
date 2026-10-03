from typing import List

from fastapi import APIRouter, HTTPException

from app.models.scenario import ACTIVE_STATUSES, Scenario, ScenarioCreate
from app.services.scenario_store import scenario_store
from app.services.propagation.link_matrix import compute_scenario_fingerprint, delete_link_matrix
from app.services.simulation_manager import idle_status

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.get("", response_model=List[Scenario])
def list_scenarios() -> list[Scenario]:
    return scenario_store.list_all()


@router.post("", response_model=Scenario, status_code=201)
def create_scenario(body: ScenarioCreate) -> Scenario:
    return scenario_store.create(body)


@router.get("/{scenario_id}", response_model=Scenario)
def get_scenario(scenario_id: str) -> Scenario:
    s = scenario_store.get(scenario_id)
    if not s:
        raise HTTPException(404, "Scenario not found")
    return s


@router.put("/{scenario_id}", response_model=Scenario)
def update_scenario(scenario_id: str, body: ScenarioCreate) -> Scenario:
    s = scenario_store.get(scenario_id)
    if not s:
        raise HTTPException(404, "Scenario not found")
    if s.status in ACTIVE_STATUSES:
        raise HTTPException(409, "Stop the simulation before editing the scenario")
    # Only inputs of the propagation model invalidate the ITM matrix; runtime settings
    # (experiment id, fading, seed, hop limit, role, names) keep the computed links.
    model_changed = compute_scenario_fingerprint(s.nodes, s.environment) != compute_scenario_fingerprint(
        body.nodes, body.environment
    )
    s.name = body.name
    s.environment = body.environment
    s.nodes = body.nodes
    s.progress_done = 0
    s.progress_total = 0
    s.error_message = None
    if model_changed:
        delete_link_matrix(scenario_id)
    s.status = idle_status(scenario_id)
    return scenario_store.update(s)


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: str) -> None:
    scenario = scenario_store.get(scenario_id)
    if not scenario:
        raise HTTPException(404, "Scenario not found")
    if scenario.status in ACTIVE_STATUSES:
        raise HTTPException(409, "Stop the simulation before deleting the scenario")
    scenario_store.delete(scenario_id)
