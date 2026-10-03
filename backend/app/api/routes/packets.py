from fastapi import APIRouter, HTTPException

from app.services.packet_trace import packet_trace_service
from app.services.scenario_store import scenario_store

router = APIRouter(prefix="/api/scenarios/{scenario_id}/packets", tags=["packets"])


@router.get("")
def list_packets(scenario_id: str, limit: int = 100) -> dict:
    if not scenario_store.get(scenario_id):
        raise HTTPException(404, "Scenario not found")
    limit = max(1, min(limit, 1000))
    txs = packet_trace_service.list_scenario_tx(scenario_id, limit)
    rxs = packet_trace_service.list_scenario_rx(scenario_id, limit)
    return {
        "transmissions": [t.model_dump(mode="json") for t in txs],
        "recent_attempts": [
            a.model_dump(mode="json")
            for a in packet_trace_service.list_scenario_attempts(scenario_id, limit)
        ],
        "deliveries": [r.model_dump(mode="json") for r in rxs],
    }


@router.get("/{packet_id}")
def packet_detail(scenario_id: str, packet_id: int) -> dict:
    if not scenario_store.get(scenario_id):
        raise HTTPException(404, "Scenario not found")
    attempts = [
        a.model_dump(mode="json")
        for a in packet_trace_service.list_attempts_for_packet(packet_id, scenario_id)
    ]
    return {"packet_id": packet_id, "link_attempts": attempts}
