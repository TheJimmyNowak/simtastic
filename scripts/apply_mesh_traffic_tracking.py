#!/usr/bin/env python3
"""Restore UTF-8 services and enable full OTA traffic tracing."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

SIMULATION_MANAGER = r'''
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional, Union

from app.config import settings
from app.models.scenario import ScenarioStatus
from app.services.influx_writer import influx_writer
from app.services.meshtastic_tcp import open_tcp_interface, wait_for_tcp_port
from app.services.node_provisioner import provision_node
from app.services.packet_trace import packet_trace_service
from app.services.periodic_traffic import PeriodicTraffic
from app.services.propagation.itm_precompute import run_precompute
from app.services.propagation.link_matrix import load_link_matrix
from app.services.radio_gateway import RadioGateway
from app.services.scenario_store import scenario_store
from app.services.shard_manager import ShardManager
from app.services.traffic_generator import TrafficGenerator
from app.services.traffic_schedule import resolve_traffic_path, write_default_traffic_yaml

logger = logging.getLogger(__name__)


class SimulationManager:
    def __init__(self) -> None:
        self.active_scenario_id: Optional[str] = None
        self._gateway: Optional[RadioGateway] = None
        self._shards: Optional[ShardManager] = None
        self._traffic: Optional[Union[TrafficGenerator, PeriodicTraffic]] = None
        self._precompute_thread: Optional[threading.Thread] = None
        self._start_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def precompute_links(self, scenario_id: str) -> None:
        if not scenario_store.get(scenario_id):
            raise ValueError("Scenario not found")
        if self._precompute_thread and self._precompute_thread.is_alive():
            raise RuntimeError("ITM precompute already running")
        scenario_store.set_status(scenario_id, ScenarioStatus.STARTING)

        def worker() -> None:
            try:
                run_precompute(scenario_id)
            except Exception as exc:
                logger.exception("precompute failed: %s", exc)
                scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))

        self._precompute_thread = threading.Thread(target=worker, daemon=True)
        self._precompute_thread.start()

    def start_async(self, scenario_id: str, traffic_path: Path | None = None) -> None:
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

        def worker() -> None:
            try:
                self._start_blocking(scenario_id, traffic_path)
            except Exception as exc:
                logger.exception("Simulation start failed")
                scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))
                self.stop()

        self._start_thread = threading.Thread(target=worker, daemon=True)
        self._start_thread.start()

    def _start_blocking(self, scenario_id: str, traffic_path: Path | None) -> None:
        scenario = scenario_store.get(scenario_id)
        if not scenario:
            raise ValueError("Scenario not found")
        matrix = load_link_matrix(scenario_id)
        if matrix is None:
            raise RuntimeError("Link matrix not computed")

        env = scenario.environment
        lora = env.resolve_lora()
        packet_trace_service.clear()
        influx_writer.start()

        shards = ShardManager(scenario_id, env.shard_size)
        shards.start(scenario.nodes)
        with self._lock:
            self._shards = shards

        host = settings.meshtastic_tcp_host
        for i, node in enumerate(scenario.nodes):
            port = node.tcp_port or (settings.tcp_port_base + i)
            wait_for_tcp_port(host, port)

        gateway = RadioGateway(
            scenario_id, env.experiment_id, scenario.nodes, matrix, env.fading_sigma_db
        )
        gateway.set_lora(lora)
        for i, node in enumerate(scenario.nodes):
            port = node.tcp_port or (settings.tcp_port_base + i)
            iface = open_tcp_interface(host, port)
            provision_node(iface, node, i)
            gateway.attach_node(node.id, iface, i)
        gateway.activate()
        gateway.start()
        with self._lock:
            self._gateway = gateway
            self.active_scenario_id = scenario_id
        scenario_store.set_status(scenario_id, ScenarioStatus.RUNNING)

        self._start_traffic(scenario, gateway, traffic_path)

    def _start_traffic(self, scenario, gateway: RadioGateway, traffic_path: Path | None) -> None:
        path = resolve_traffic_path(
            scenario.id, settings.data_dir, traffic_path
        )
        nodes_by_id = {n.id: i for i, n in enumerate(scenario.nodes)}
        started = False

        if path and path.exists():
            try:
                gen = TrafficGenerator(path, gateway._ifaces, nodes_by_id)
                gen.start()
                with self._lock:
                    self._traffic = gen
                started = True
                logger.info("Traffic generator from %s", path)
            except ValueError as exc:
                logger.warning("Traffic YAML rejected: %s", exc)

        if not started:
            per = settings.data_dir / "scenarios" / scenario.id / "traffic.yaml"
            if not per.exists():
                write_default_traffic_yaml(per, scenario.nodes)
            try:
                gen = TrafficGenerator(per, gateway._ifaces, nodes_by_id)
                gen.start()
                with self._lock:
                    self._traffic = gen
                started = True
                logger.info("Traffic generator from %s", per)
            except ValueError as exc:
                logger.warning("Default traffic YAML rejected: %s; using periodic probes", exc)

        if not started:
            periodic = PeriodicTraffic(gateway, scenario.nodes, interval_s=10.0)
            periodic.start()
            with self._lock:
                self._traffic = periodic
            logger.info("PeriodicTraffic probes every 10s")

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
            influx_writer.stop()
            if sid:
                scenario_store.set_status(sid, ScenarioStatus.STOPPED)
            self.active_scenario_id = None

    def force_stop(self, scenario_id: str) -> None:
        with self._lock:
            if self.active_scenario_id not in (None, scenario_id):
                raise RuntimeError("Scenario is not active")
        if self._start_thread and self._start_thread.is_alive():
            logger.info("Stopping simulation while start thread is running")
        self.stop()

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
'''

NODE_PROVISIONER = r'''
from __future__ import annotations

import logging
import time

from meshtastic import admin_pb2
from meshtastic.tcp_interface import TCPInterface

from app.models.node import NodeConfig, NodeRole

logger = logging.getLogger(__name__)

_ROLE_MAP = {
    NodeRole.CLIENT: "CLIENT",
    NodeRole.ROUTER: "ROUTER",
    NodeRole.REPEATER: "REPEATER",
    NodeRole.CLIENT_MUTE: "CLIENT_MUTE",
}


def provision_node(iface: TCPInterface, node: NodeConfig, index: int) -> None:
    """Configure meshtasticd: hop_limit, role, GPS, disable UDP, OTA broadcasts."""
    local = iface.localNode
    if local is None:
        raise RuntimeError("No local node on interface")

    p = admin_pb2.AdminMessage()
    p.set_owner.long_name = node.name or node.id
    p.set_owner.short_name = (node.name or node.id)[:4]
    local._sendAdmin(p)
    time.sleep(0.05)

    network_cfg = local.localConfig.network
    network_cfg.enabled_protocols = 0
    p = admin_pb2.AdminMessage()
    p.set_config.network.CopyFrom(network_cfg)
    local._sendAdmin(p)
    time.sleep(0.05)

    lora_cfg = local.localConfig.lora
    lora_cfg.hop_limit = int(node.hop_limit)
    p = admin_pb2.AdminMessage()
    p.set_config.lora.CopyFrom(lora_cfg)
    local._sendAdmin(p)
    time.sleep(0.05)

    device_cfg = local.localConfig.device
    device_cfg.role = _ROLE_MAP.get(node.role, "CLIENT")
    device_cfg.node_info_broadcast_secs = 90
    p = admin_pb2.AdminMessage()
    p.set_config.device.CopyFrom(device_cfg)
    local._sendAdmin(p)
    time.sleep(0.05)

    pos_cfg = local.localConfig.position
    pos_cfg.fixed_position = True
    pos_cfg.position_broadcast_secs = 90
    p = admin_pb2.AdminMessage()
    p.set_config.position.CopyFrom(pos_cfg)
    local._sendAdmin(p)
    time.sleep(0.05)

    iface.sendPosition(node.lat, node.lon, int(node.agl_m))
    logger.info("Provisioned node %s on port %s", node.id, node.tcp_port)
'''


def write(path: Path, content: str) -> None:
    path.write_text(content.strip() + "\n", encoding="utf-8")
    print("wrote", path.relative_to(ROOT), path.stat().st_size)


def patch_packet_trace(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "\x00" in text:
        subprocess.run([sys.executable, str(ROOT / "scripts/bootstrap_services.py")], check=True)
        text = path.read_text(encoding="utf-8")
    needle = "    def list_attempts_for_packet(self, packet_id: int)"
    insert = """    def list_recent_rx(self, limit: int = 200) -> list[PacketOtaRx]:
        with self._lock:
            items = list(self._rx_buf)
        return items[-limit:]

"""
    if "def list_recent_rx" not in text:
        if needle not in text:
            raise SystemExit("packet_trace: anchor not found")
        text = text.replace(needle, insert + needle)
        path.write_text(text, encoding="utf-8")
        print("patched list_recent_rx in packet_trace.py")


def main() -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts/bootstrap_services.py")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/_write_radio_gateway.py")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/patch_docker_sim.py")], check=True)
    write(
        BACKEND / "app/services/meshtastic_tcp.py",
        (ROOT / "scripts/patch_tcp_connect.py")
        .read_text(encoding="utf-8")
        .split("MESHTASTIC_TCP = '''")[1]
        .split("'''")[0],
    )

    write(BACKEND / "app/services/simulation_manager.py", SIMULATION_MANAGER)
    write(BACKEND / "app/services/node_provisioner.py", NODE_PROVISIONER)
    patch_packet_trace(BACKEND / "app/services/packet_trace.py")

    # Re-apply full radio after patch_tcp (may overwrite with simulator-only).
    subprocess.run([sys.executable, str(ROOT / "scripts/_write_radio_gateway.py")], check=True)

    for rel in (
        "app/services/radio_gateway.py",
        "app/services/packet_trace.py",
        "app/services/simulation_manager.py",
        "app/services/node_provisioner.py",
    ):
        p = BACKEND / rel
        if b"\x00" in p.read_bytes():
            raise SystemExit(f"null bytes remain in {rel}")

    import py_compile

    for rel in (
        "app/services/radio_gateway.py",
        "app/services/packet_trace.py",
        "app/services/simulation_manager.py",
        "app/services/node_provisioner.py",
        "app/services/mesh_packet_util.py",
        "app/services/periodic_traffic.py",
    ):
        py_compile.compile(str(BACKEND / rel), doraise=True)
    print("all OK")


if __name__ == "__main__":
    main()
