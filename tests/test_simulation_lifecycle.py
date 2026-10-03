from types import SimpleNamespace

import pytest

from app.models.node import NodeConfig
from app.models.scenario import ScenarioCreate, ScenarioStatus
from app.services import shard_manager as shard_module
from app.services import simulation_manager as sim_module
from app.services.propagation.link_matrix import compute_scenario_fingerprint
from app.services.scenario_store import ScenarioStore
from app.services.shard_manager import LABEL_APP, LABEL_SCENARIO, ShardManager


@pytest.fixture
def store(tmp_path, monkeypatch) -> ScenarioStore:
    store = ScenarioStore(tmp_path)
    monkeypatch.setattr(sim_module, "scenario_store", store)
    monkeypatch.setattr(sim_module, "load_link_matrix", lambda scenario_id: None)
    monkeypatch.setattr(ShardManager, "cleanup_all_sim_containers", staticmethod(lambda: []))
    return store


def _scenario(store: ScenarioStore, name: str, status: ScenarioStatus):
    scenario = store.create(
        ScenarioCreate(
            name=name,
            nodes=[NodeConfig(id="a", lat=50, lon=20), NodeConfig(id="b", lat=50, lon=20.01)],
        )
    )
    store.set_status(scenario.id, status)
    return scenario


def _valid_matrix(scenario):
    return SimpleNamespace(
        node_ids=[node.id for node in scenario.nodes],
        scenario_fingerprint=compute_scenario_fingerprint(scenario.nodes, scenario.environment),
    )


def test_container_names_do_not_collide_for_common_prefix() -> None:
    one = ShardManager("scenariusz-1", 10)._container_name(0)
    ten = ShardManager("scenariusz-10", 10)._container_name(0)
    assert one == "sim-scenariusz-1-shard-0"
    assert one != ten


def test_cleanup_filters_by_scenario_label(monkeypatch) -> None:
    calls = []

    class Containers:
        def list(self, all, filters):
            calls.append(filters)
            return []

    monkeypatch.setattr(shard_module.settings, "dry_run", False)
    monkeypatch.setattr(shard_module, "_docker_client", lambda: SimpleNamespace(containers=Containers()))
    ShardManager.cleanup_scenario_containers("scenariusz-1")
    ShardManager.cleanup_all_sim_containers()
    assert calls == [{"label": f"{LABEL_SCENARIO}=scenariusz-1"}, {"label": LABEL_APP}]


def test_runtime_endpoint_is_not_persisted_in_node_config() -> None:
    legacy = NodeConfig.model_validate(
        {"id": "a", "lat": 50, "lon": 20, "tcp_host": "sim-old-shard-0", "tcp_port": 4403}
    )
    assert "tcp_host" not in legacy.model_dump() and "tcp_port" not in legacy.model_dump()


def test_restart_resets_transient_statuses(store: ScenarioStore, monkeypatch) -> None:
    running = _scenario(store, "running", ScenarioStatus.RUNNING)
    computing = _scenario(store, "computing", ScenarioStatus.PRECOMPUTING)
    done = _scenario(store, "done", ScenarioStatus.ERROR)
    monkeypatch.setattr(
        sim_module,
        "load_link_matrix",
        lambda scenario_id: _valid_matrix(running) if scenario_id == running.id else None,
    )

    sim_module.SimulationManager().recover_after_restart()

    assert ScenarioStore(store._base).get(running.id).status == ScenarioStatus.LINKS_READY
    assert ScenarioStore(store._base).get(computing.id).status == ScenarioStatus.DRAFT
    assert ScenarioStore(store._base).get(done.id).status == ScenarioStatus.ERROR


def test_idle_status_requires_matching_matrix(store: ScenarioStore, monkeypatch) -> None:
    scenario = _scenario(store, "s", ScenarioStatus.RUNNING)
    assert sim_module.idle_status(scenario.id) == ScenarioStatus.DRAFT
    stale = SimpleNamespace(node_ids=["a", "b"], scenario_fingerprint="old")
    monkeypatch.setattr(sim_module, "load_link_matrix", lambda _: stale)
    assert sim_module.idle_status(scenario.id) == ScenarioStatus.DRAFT
    monkeypatch.setattr(sim_module, "load_link_matrix", lambda _: _valid_matrix(scenario))
    assert sim_module.idle_status(scenario.id) == ScenarioStatus.LINKS_READY


def test_progress_is_kept_in_memory_only(store: ScenarioStore) -> None:
    scenario = _scenario(store, "progress", ScenarioStatus.PRECOMPUTING)
    store.set_progress(scenario.id, 4, 10)
    assert store.get(scenario.id).progress_done == 4
    saved = (store._base / scenario.id / "scenario.yaml").read_text()
    assert "progress_done" not in saved
