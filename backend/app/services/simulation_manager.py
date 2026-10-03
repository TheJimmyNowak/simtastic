from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from app.config import settings
from app.models.scenario import ACTIVE_STATUSES, Scenario, ScenarioStatus
from app.services.influx_writer import influx_writer
from app.services.meshtastic_tcp import open_tcp_interface, wait_for_mesh_connected, wait_for_tcp_port
from app.services.node_containers import NodeContainers
from app.services.node_provisioner import provision_node, verify_provisioning
from app.services.packet_trace import packet_trace_service
from app.services.propagation.itm_precompute import run_precompute
from app.services.propagation.link_matrix import (
    LinkMatrix,
    compute_scenario_fingerprint,
    delete_link_matrix,
    load_link_matrix,
)
from app.services.radio_gateway import RadioGateway
from app.services.scenario_store import scenario_store
from app.services.traffic_generator import TrafficGenerator
from app.services.traffic_schedule import resolve_traffic_path, write_default_traffic_yaml

logger = logging.getLogger(__name__)


class SimulationCancelled(Exception):
    """Start cancelled (force_stop while nodes were being prepared)."""


def matrix_matches(scenario: Scenario, matrix: Optional[LinkMatrix]) -> bool:
    """True when the precomputed link matrix was computed for this exact scenario."""
    return (
        matrix is not None
        and matrix.node_ids == [node.id for node in scenario.nodes]
        and matrix.scenario_fingerprint
        == compute_scenario_fingerprint(scenario.nodes, scenario.environment)
    )


def idle_status(scenario_id: str) -> ScenarioStatus:
    """Status of an idle scenario: LINKS_READY when its ITM matrix is still valid."""
    scenario = scenario_store.get(scenario_id)
    if scenario is not None and matrix_matches(scenario, load_link_matrix(scenario_id)):
        return ScenarioStatus.LINKS_READY
    return ScenarioStatus.DRAFT


class SimulationManager:
    """Runs one simulation at a time: node containers, RF gateway and traffic."""

    def __init__(self) -> None:
        self.active_scenario_id: Optional[str] = None
        self._gateway: Optional[RadioGateway] = None
        self._containers: Optional[NodeContainers] = None
        self._traffic: Optional[TrafficGenerator] = None
        self._precompute_thread: Optional[threading.Thread] = None
        self._start_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cancel_start = threading.Event()

    # --- ITM precompute -------------------------------------------------------

    def precompute_links(self, scenario_id: str) -> None:
        scenario = scenario_store.get(scenario_id)
        if not scenario:
            raise ValueError("Scenario not found")
        if len(scenario.nodes) < 2:
            raise ValueError("At least two nodes required for ITM precompute")
        if scenario.status in ACTIVE_STATUSES:
            raise RuntimeError("Scenario is already being prepared or running")
        if self._precompute_thread and self._precompute_thread.is_alive():
            raise RuntimeError("ITM precompute already running")
        delete_link_matrix(scenario_id)
        n = len(scenario.nodes)
        scenario_store.set_progress(scenario_id, 0, n * (n - 1))
        scenario_store.set_status(scenario_id, ScenarioStatus.PRECOMPUTING)

        def worker() -> None:
            try:
                run_precompute(scenario_id)
            except Exception:
                logger.exception("ITM precompute failed")  # status set by run_precompute

        self._precompute_thread = threading.Thread(target=worker, daemon=True)
        self._precompute_thread.start()

    # --- start ----------------------------------------------------------------

    def start_async(self, scenario_id: str, traffic_path: Path | None = None) -> None:
        if self._precompute_thread and self._precompute_thread.is_alive():
            raise RuntimeError("ITM precompute is still running")
        if self._start_thread and self._start_thread.is_alive():
            raise RuntimeError("Simulation start already in progress")
        with self._lock:
            if self.active_scenario_id:
                raise RuntimeError("Another simulation is already running")
        scenario = scenario_store.get(scenario_id)
        if not scenario:
            raise ValueError("Scenario not found")
        if not matrix_matches(scenario, load_link_matrix(scenario_id)):
            raise RuntimeError("Link matrix missing or outdated; run Precompute ITM first")
        # Validate the traffic file before any container is started.
        traffic_file = resolve_traffic_path(scenario_id, settings.data_dir, traffic_path)

        scenario_store.set_status(scenario_id, ScenarioStatus.STARTING)
        self._cancel_start.clear()

        def worker() -> None:
            try:
                self._start_blocking(scenario_id, traffic_file)
            except SimulationCancelled:
                logger.info("Simulation start cancelled for %s", scenario_id)
                self.stop()
                scenario_store.set_status(scenario_id, idle_status(scenario_id))
            except Exception as exc:
                logger.exception("Simulation start failed")
                self.stop()
                scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))
            finally:
                self._cancel_start.clear()

        self._start_thread = threading.Thread(target=worker, daemon=True)
        self._start_thread.start()

    def _check_cancelled(self) -> None:
        if self._cancel_start.is_set():
            raise SimulationCancelled()

    def _sleep_interruptible(self, seconds: float) -> None:
        if self._cancel_start.wait(timeout=seconds):
            raise SimulationCancelled()

    def _prepare_node(
        self,
        index: int,
        scenario: Scenario,
        matrix: LinkMatrix,
        containers: NodeContainers,
        gateway: RadioGateway,
    ) -> None:
        """Provision one node, restart it, verify the config and attach it to the gateway."""
        node = scenario.nodes[index]
        env = scenario.environment
        tcp_timeout = settings.sim_meshtastic_tcp_timeout_s
        antenna_z = (matrix.node_placement or {}).get(node.id, {}).get("antenna_z_m")

        self._check_cancelled()
        host, port = containers.endpoint(index)
        wait_for_tcp_port(host, port, timeout=tcp_timeout)
        iface = open_tcp_interface(host, port, timeout=tcp_timeout)
        try:
            wait_for_mesh_connected(iface, timeout=settings.sim_mesh_connect_timeout_s)
            provision_node(
                iface,
                node,
                index,
                preset_name=env.preset,
                lora=env.resolve_lora(),
                altitude_m=float(antenna_z) if antenna_z is not None else 0.0,
            )
        finally:
            iface.close()

        self._check_cancelled()
        containers.restart(index)
        host, port = containers.endpoint(index)
        wait_for_tcp_port(host, port, timeout=tcp_timeout)
        iface = open_tcp_interface(host, port, timeout=tcp_timeout)
        wait_for_mesh_connected(iface, timeout=settings.sim_mesh_connect_timeout_s)
        verify_provisioning(iface, node, env.preset)
        gateway.attach_node(node.id, iface, index, endpoint=(host, port))
        logger.info("Node %s ready (%s:%s)", node.id, host, port)

    def _prepare_nodes(
        self, scenario: Scenario, matrix: LinkMatrix, containers: NodeContainers, gateway: RadioGateway
    ) -> None:
        total = len(scenario.nodes)
        done = 0
        progress_lock = threading.Lock()
        scenario_store.set_progress(scenario.id, 0, total)

        def task(index: int) -> None:
            nonlocal done
            self._prepare_node(index, scenario, matrix, containers, gateway)
            with progress_lock:
                done += 1
                scenario_store.set_progress(scenario.id, done, total)

        workers = max(1, min(settings.sim_provision_workers, total))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="provision") as pool:
            futures = [pool.submit(task, index) for index in range(total)]
            try:
                for future in futures:
                    future.result()
            except BaseException:
                self._cancel_start.set()  # let the remaining tasks stop early
                raise

    def _start_traffic(self, scenario: Scenario, gateway: RadioGateway, traffic_file: Path | None) -> None:
        if traffic_file is None:
            traffic_file = settings.data_dir / "scenarios" / scenario.id / "traffic.yaml"
            write_default_traffic_yaml(traffic_file, scenario.nodes)
        nodes_by_id = {node.id: i for i, node in enumerate(scenario.nodes)}
        generator = TrafficGenerator(traffic_file, gateway, scenario.nodes, nodes_by_id)
        generator.start()
        self._traffic = generator
        logger.info("Traffic schedule %s", traffic_file)

    def _start_blocking(self, scenario_id: str, traffic_file: Path | None) -> None:
        scenario = scenario_store.get(scenario_id)
        matrix = load_link_matrix(scenario_id)
        if scenario is None or not matrix_matches(scenario, matrix):
            raise RuntimeError("Link matrix missing or outdated; run Precompute ITM again")
        env = scenario.environment

        packet_trace_service.clear()
        influx_writer.start()

        containers = NodeContainers(scenario_id)
        with self._lock:
            self._containers = containers
        containers.start(len(scenario.nodes))

        gateway = RadioGateway(
            scenario_id,
            env.experiment_id,
            scenario.nodes,
            matrix,
            env.fading_sigma_db,
            random_seed=env.random_seed,
        )
        gateway.set_lora(env.resolve_lora())
        with self._lock:
            self._gateway = gateway
        self._prepare_nodes(scenario, matrix, containers, gateway)

        gateway.activate()
        gateway.start()
        self._sleep_interruptible(settings.sim_traffic_warmup_s)
        self._start_traffic(scenario, gateway, traffic_file)

        with self._lock:
            self.active_scenario_id = scenario_id
        scenario_store.set_status(scenario_id, ScenarioStatus.RUNNING)
        influx_writer.write_simulation_run(
            scenario_id=scenario_id,
            experiment_id=env.experiment_id,
            node_count=len(scenario.nodes),
            preset=env.preset,
            fading_sigma_db=env.fading_sigma_db,
            building_model_enabled=env.building_model_enabled,
            fresnel_clearance_ratio=env.fresnel_clearance_ratio,
        )
        influx_writer.write_link_precompute(
            scenario_id=scenario_id, experiment_id=env.experiment_id, matrix=matrix
        )

    # --- stop -------------------------------------------------------------------

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
            if self._containers:
                self._containers.stop()
                self._containers = None
            influx_writer.set_run_context(None)
            influx_writer.stop()
            self.active_scenario_id = None
        if sid:
            scenario_store.set_status(sid, idle_status(sid))

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
        removed = NodeContainers.cleanup_scenario_containers(scenario_id)
        scenario_store.set_status(scenario_id, idle_status(scenario_id))
        self._cancel_start.clear()
        return removed

    def recover_after_restart(self) -> None:
        """After a process restart the in-memory state is gone: release scenarios and containers."""
        for scenario in scenario_store.list_all():
            if scenario.status in ACTIVE_STATUSES:
                logger.warning(
                    "Scenario %s was %s before restart; resetting", scenario.id, scenario.status.value
                )
                scenario_store.set_status(scenario.id, idle_status(scenario.id))
        try:
            removed = NodeContainers.cleanup_all_sim_containers()
        except Exception as exc:
            logger.warning("Orphan container cleanup skipped: %s", exc)
            return
        if removed:
            logger.warning("Removed orphan simulator containers: %s", ", ".join(removed))

    def status(self) -> dict:
        containers = self._containers
        return {
            "active_scenario_id": self.active_scenario_id,
            "gateway_running": self._gateway is not None,
            "containers": containers.status() if containers else [],
            "precompute_running": bool(self._precompute_thread and self._precompute_thread.is_alive()),
            "start_in_progress": bool(self._start_thread and self._start_thread.is_alive()),
        }


simulation_manager = SimulationManager()
