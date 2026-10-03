from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional, Union

from app.config import settings
from app.models.scenario import ScenarioStatus
from app.services.influx_writer import influx_writer
from app.services.meshtastic_tcp import (
    open_tcp_interface,
    wait_for_mesh_connected,
    wait_for_tcp_port,
)
from app.services.node_provisioner import provision_node, verify_provisioning
from app.services.packet_trace import packet_trace_service
from app.services.periodic_traffic import PeriodicTraffic
from app.services.propagation.itm_precompute import run_precompute
from app.services.propagation.link_matrix import (
    compute_scenario_fingerprint,
    delete_link_matrix,
    load_link_matrix,
)
from app.services.radio_gateway import RadioGateway
from app.services.scenario_store import scenario_store
from app.services.shard_manager import ShardManager
from app.services.traffic_generator import TrafficGenerator
from app.services.traffic_schedule import resolve_traffic_path, write_default_traffic_yaml

logger = logging.getLogger(__name__)


class SimulationCancelled(Exception):
    """Anulowanie startu symulacji (np. force_stop podczas _start_blocking)."""


_TRANSIENT_STATUSES = {
    ScenarioStatus.PRECOMPUTING,
    ScenarioStatus.STARTING,
    ScenarioStatus.RUNNING,
    ScenarioStatus.STOPPING,
}


def idle_status(scenario_id: str) -> ScenarioStatus:
    """Status bezczynnego scenariusza: LINKS_READY, gdy macierz ITM jest aktualna."""
    scenario = scenario_store.get(scenario_id)
    matrix = load_link_matrix(scenario_id)
    if (
        scenario is not None
        and matrix is not None
        and matrix.node_ids == [node.id for node in scenario.nodes]
        and matrix.scenario_fingerprint
        == compute_scenario_fingerprint(scenario.nodes, scenario.environment)
    ):
        return ScenarioStatus.LINKS_READY
    return ScenarioStatus.DRAFT


class SimulationManager:
    def __init__(self) -> None:
        self.active_scenario_id: Optional[str] = None
        self._gateway: Optional[RadioGateway] = None
        self._shards: Optional[ShardManager] = None
        self._traffic: Optional[Union[TrafficGenerator, PeriodicTraffic]] = None
        self._precompute_thread: Optional[threading.Thread] = None
        self._start_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cancel_start = threading.Event()

    def precompute_links(self, scenario_id: str) -> None:
        scenario = scenario_store.get(scenario_id)
        if not scenario:
            raise ValueError("Scenario not found")
        if len(scenario.nodes) < 2:
            raise ValueError("At least two nodes required for ITM precompute")
        if scenario.status in {
            ScenarioStatus.PRECOMPUTING,
            ScenarioStatus.STARTING,
            ScenarioStatus.RUNNING,
        }:
            raise RuntimeError("Scenario is already being prepared or running")
        if self._precompute_thread and self._precompute_thread.is_alive():
            raise RuntimeError("ITM precompute already running")
        delete_link_matrix(scenario_id)
        scenario_store.set_itm_progress(scenario_id, 0, len(scenario.nodes) * (len(scenario.nodes) - 1))
        scenario_store.set_status(scenario_id, ScenarioStatus.PRECOMPUTING)

        def worker() -> None:
            try:
                run_precompute(scenario_id)
            except Exception as exc:
                logger.exception("precompute failed: %s", exc)
                scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))

        self._precompute_thread = threading.Thread(target=worker, daemon=True)
        self._precompute_thread.start()

    def start_async(self, scenario_id: str, traffic_path: Path | None = None) -> None:
        if self._precompute_thread and self._precompute_thread.is_alive():
            raise RuntimeError("ITM precompute is still running")
        if self._start_thread and self._start_thread.is_alive():
            raise RuntimeError("Simulation start already in progress")
        with self._lock:
            if self.active_scenario_id:
                raise RuntimeError("Another simulation is already running")
        if not scenario_store.get(scenario_id):
            raise ValueError("Scenario not found")
        if load_link_matrix(scenario_id) is None:
            raise RuntimeError("Link matrix not computed; run Precompute ITM first")
        if settings.dry_run:
            raise RuntimeError("DRY_RUN=true — set DRY_RUN=false for Docker simulation")

        scenario_store.set_status(scenario_id, ScenarioStatus.STARTING)
        self._cancel_start.clear()

        def worker() -> None:
            try:
                self._start_blocking(scenario_id, traffic_path)
            except SimulationCancelled:
                logger.info("Simulation start cancelled for %s", scenario_id)
                scenario_store.set_status(scenario_id, idle_status(scenario_id), None)
                self.stop()
            except Exception as exc:
                logger.exception("Simulation start failed")
                scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))
                self.stop()
            finally:
                self._cancel_start.clear()

        self._start_thread = threading.Thread(target=worker, daemon=True)
        self._start_thread.start()

    def _sleep_interruptible(self, seconds: float) -> None:
        if self._cancel_start.wait(timeout=seconds):
            raise SimulationCancelled()

    def _bootstrap_shards(self, scenario_id: str, scenario) -> None:
        env = scenario.environment
        shards = ShardManager(scenario_id, env.shard_size)
        shards.start(scenario.nodes)
        with self._lock:
            self._shards = shards

    def _create_gateway(
        self, scenario_id: str, scenario, matrix, fading_sigma_db: float
    ) -> RadioGateway:
        env = scenario.environment
        gateway = RadioGateway(
            scenario_id,
            env.experiment_id,
            scenario.nodes,
            matrix,
            fading_sigma_db,
            random_seed=env.random_seed,
        )
        gateway.set_lora(env.resolve_lora())
        return gateway

    def _provision_and_attach_nodes(
        self, scenario_id: str, scenario, gateway: RadioGateway, matrix
    ) -> None:
        env = scenario.environment
        lora = env.resolve_lora()
        placement = matrix.node_placement or {}
        timeout = settings.sim_meshtastic_tcp_timeout_s
        connect_timeout = settings.sim_mesh_connect_timeout_s
        settle = settings.sim_post_provision_settle_s

        for i, node in enumerate(scenario.nodes):
            if self._cancel_start.is_set():
                raise SimulationCancelled()
            if self._shards:
                host, port = self._shards.tcp_endpoint(i)
            else:
                host, port = settings.meshtastic_tcp_host, settings.tcp_port_base + i
            scenario_store.set_itm_progress(scenario_id, i, len(scenario.nodes))
            wait_for_tcp_port(host, port, timeout=timeout)
            prov_iface = open_tcp_interface(host, port, timeout=timeout)
            antenna_z = (placement.get(node.id) or {}).get("antenna_z_m")
            provision_node(
                prov_iface,
                node,
                i,
                preset_name=env.preset,
                lora=lora,
                altitude_m=float(antenna_z) if antenna_z is not None else 0.0,
            )
            try:
                prov_iface.close()
            except Exception:
                pass
            if self._shards:
                self._shards.restart_meshtasticd(i)
            self._sleep_interruptible(settle)
            wait_for_tcp_port(host, port, timeout=timeout)
            iface = open_tcp_interface(host, port, timeout=timeout)
            wait_for_mesh_connected(iface, timeout=connect_timeout)
            verify_provisioning(iface, node, env.preset)
            gateway.attach_node(node.id, iface, i, endpoint=(host, port))
            logger.info("Attached gateway to node %s on port %s", node.id, port)

    def _start_traffic(
        self, scenario, gateway: RadioGateway, traffic_path: Path | None
    ) -> None:
        path = resolve_traffic_path(scenario.id, settings.data_dir, traffic_path)
        nodes_by_id = {n.id: i for i, n in enumerate(scenario.nodes)}

        if path and path.exists():
            try:
                gen = TrafficGenerator(path, gateway, scenario.nodes, nodes_by_id)
                gen.start()
                self._traffic = gen
                logger.info("TrafficGenerator from %s", path)
                return
            except ValueError as exc:
                logger.warning("Traffic YAML rejected: %s", exc)

        default_path = settings.data_dir / "scenarios" / scenario.id / "traffic.yaml"
        write_default_traffic_yaml(default_path, scenario.nodes)
        try:
            gen = TrafficGenerator(default_path, gateway, scenario.nodes, nodes_by_id)
            gen.start()
            self._traffic = gen
            logger.info("TrafficGenerator from default %s", default_path)
            return
        except ValueError as exc:
            logger.warning("Default traffic YAML rejected: %s; using periodic probes", exc)

        periodic = PeriodicTraffic(gateway, scenario.nodes, interval_s=10.0)
        periodic.start()
        self._traffic = periodic
        logger.info("PeriodicTraffic every 10s")

    def _start_blocking(self, scenario_id: str, traffic_path: Path | None) -> None:
        scenario = scenario_store.get(scenario_id)
        if not scenario:
            raise ValueError("Scenario not found")
        matrix = load_link_matrix(scenario_id)
        if matrix is None:
            raise RuntimeError("Link matrix not computed")
        if matrix.node_ids != [node.id for node in scenario.nodes]:
            raise RuntimeError("Link matrix is stale; run Precompute ITM again")
        if matrix.scenario_fingerprint != compute_scenario_fingerprint(
            scenario.nodes, scenario.environment
        ):
            raise RuntimeError("Link matrix does not match the scenario; run Precompute ITM again")

        env = scenario.environment
        packet_trace_service.clear()
        influx_writer.start()

        self._bootstrap_shards(scenario_id, scenario)
        self._sleep_interruptible(settings.sim_mesh_boot_grace_s)

        gateway = self._create_gateway(scenario_id, scenario, matrix, env.fading_sigma_db)
        self._provision_and_attach_nodes(scenario_id, scenario, gateway, matrix)

        gateway.activate()
        gateway.start()
        with self._lock:
            self._gateway = gateway

        self._sleep_interruptible(settings.sim_traffic_warmup_s)
        self._start_traffic(scenario, gateway, traffic_path)

        with self._lock:
            self.active_scenario_id = scenario_id
        scenario_store.set_status(scenario_id, ScenarioStatus.RUNNING)
        influx_writer.write_simulation_run(
            scenario_id=scenario_id,
            experiment_id=env.experiment_id,
            node_count=len(scenario.nodes),
            preset=env.preset,
            fading_sigma_db=env.fading_sigma_db,
            shard_size=env.shard_size,
            building_model_enabled=env.building_model_enabled,
            fresnel_clearance_ratio=env.fresnel_clearance_ratio,
        )
        influx_writer.write_link_precompute(
            scenario_id=scenario_id,
            experiment_id=env.experiment_id,
            matrix=matrix,
        )

    def start(self, scenario_id: str, traffic_path: Path | None = None) -> None:
        self.start_async(scenario_id, traffic_path)

    def stop(self) -> None:
        with self._lock:
            sid = self.active_scenario_id
            if self._traffic:
                self._traffic.stop()
                self._traffic = None
            if self._gateway:
                self._gateway.stop()
                self._gateway.disconnect()
                self._gateway = None
            if self._shards:
                self._shards.stop()
                self._shards = None
            influx_writer.set_run_context(None)
            influx_writer.stop()
            if sid:
                scenario_store.set_status(sid, idle_status(sid))
            self.active_scenario_id = None

    def force_stop(self, scenario_id: str) -> list[str]:
        self._cancel_start.set()
        if self._start_thread and self._start_thread.is_alive():
            self._start_thread.join(timeout=settings.sim_start_thread_join_s)
            if self._start_thread.is_alive():
                logger.warning(
                    "Start thread still running after join timeout (%ss)",
                    settings.sim_start_thread_join_s,
                )
        self.stop()
        removed = ShardManager.cleanup_scenario_containers(scenario_id)
        scenario_store.set_status(scenario_id, idle_status(scenario_id))
        self._cancel_start.clear()
        return removed

    def recover_after_restart(self) -> None:
        """Po restarcie procesu stan w pamięci przepadł: zwolnij scenariusze i kontenery."""
        for scenario in scenario_store.list_all():
            if scenario.status in _TRANSIENT_STATUSES:
                logger.warning(
                    "Scenario %s was %s before restart; resetting", scenario.id, scenario.status.value
                )
                scenario_store.set_status(scenario.id, idle_status(scenario.id))
        try:
            removed = ShardManager.cleanup_all_sim_containers()
        except Exception as exc:
            logger.warning("Orphan container cleanup skipped: %s", exc)
            return
        if removed:
            logger.warning("Removed orphan simulator containers: %s", ", ".join(removed))

    def status(self) -> dict:
        return {
            "active_scenario_id": self.active_scenario_id,
            "gateway_running": self._gateway is not None,
            "shards": self._shards.status() if self._shards else [],
            "precompute_running": bool(
                self._precompute_thread and self._precompute_thread.is_alive()
            ),
            "start_in_progress": bool(
                self._start_thread and self._start_thread.is_alive()
            ),
        }


simulation_manager = SimulationManager()
