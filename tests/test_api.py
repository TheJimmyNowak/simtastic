import os
import uuid
import json

os.environ["DRY_RUN"] = "true"

import pytest
from pydantic import ValidationError

from app.api.routes.scenarios import create_scenario, get_scenario
from app.api.routes.simulation import get_links, precompute_links
from app.main import health
from app.models.scenario import ScenarioCreate
from app.services.scenario_store import scenario_store


def test_health() -> None:
    assert health()["status"] == "ok"


def test_scenario_crud() -> None:
    scenario_name = f"pytest-itm-{uuid.uuid4().hex[:8]}"
    scenario = create_scenario(
        ScenarioCreate.model_validate({
            "name": scenario_name,
            "environment": {"preset": "LONG_FAST", "experiment_id": "t1"},
            "nodes": [
                {"id": "a", "lat": 52.0, "lon": 21.0, "hop_limit": 3},
                {"id": "b", "lat": 52.05, "lon": 21.1, "hop_limit": 5},
            ],
        })
    )
    sid = scenario.id
    assert sid == scenario_name
    assert scenario.nodes[1].hop_limit == 5

    assert precompute_links(sid)["status"] == "started"

    import time

    for _ in range(60):
        s = get_scenario(sid)
        if s.status in ("links_ready", "error"):
            break
        time.sleep(0.5)
    assert s.status == "links_ready"

    link_response = get_links(sid)
    assert link_response["n"] == 2
    assert link_response["propagation_model"] == "itm+p526_buildings"
    json.dumps(link_response, allow_nan=False)
    assert scenario_store.delete(sid)


def test_scenario_validation_rejects_bad_preset_and_duplicate_nodes() -> None:
    with pytest.raises(ValidationError):
        ScenarioCreate.model_validate(
            {"name": "bad-preset", "environment": {"preset": "TYPO"}, "nodes": []}
        )

    with pytest.raises(ValidationError):
        ScenarioCreate.model_validate({
            "name": "duplicate-nodes",
            "nodes": [
                {"id": "same", "lat": 52.0, "lon": 21.0},
                {"id": "same", "lat": 52.1, "lon": 21.1},
            ],
        })
