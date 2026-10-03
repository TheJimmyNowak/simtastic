#!/usr/bin/env python3
"""Patch simulation_manager + shard_manager for Docker sim (UTF-8 safe)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend"

SHARD = r'''
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field

from app.config import settings
from app.models.node import NodeConfig

logger = logging.getLogger(__name__)

MESHTASTICD = "meshtasticd"


@dataclass
class ShardInfo:
    shard_id: int
    container_name: str
    node_indices: list[int] = field(default_factory=list)


class ShardManager:
    def __init__(self, scenario_id: str, shard_size: int) -> None:
        self.scenario_id = scenario_id
        self.shard_size = max(1, shard_size)
        self.shards: list[ShardInfo] = []
        self._data_root = settings.data_dir / "scenarios" / scenario_id / "nodes"

    def _container_name(self, shard_id: int) -> str:
        return f"sim-{self.scenario_id[:8]}-shard-{shard_id}"

    def start(self, nodes: list[NodeConfig]) -> None:
        if settings.dry_run:
            logger.info("dry_run: skip Docker shard start")
            return
        self._data_root.mkdir(parents=True, exist_ok=True)
        num_shards = (len(nodes) + self.shard_size - 1) // self.shard_size
        for shard_id in range(num_shards):
            start = shard_id * self.shard_size
            chunk = nodes[start : start + self.shard_size]
            indices = list(range(start, start + len(chunk)))
            info = ShardInfo(
                shard_id=shard_id,
                container_name=self._container_name(shard_id),
                node_indices=indices,
            )
            self.shards.append(info)
            self._start_shard(info, chunk, start)

    def _start_shard(self, info: ShardInfo, chunk: list[NodeConfig], base_index: int) -> None:
        ports: list[str] = []
        for i, node in enumerate(chunk):
            global_i = base_index + i
            port = settings.tcp_port_base + global_i
            node.tcp_port = port
            (self._data_root / f"node_{global_i}").mkdir(parents=True, exist_ok=True)
            ports.extend(["-p", f"{port}:{port}/tcp"])

        host_data = self._data_root.resolve()
        subprocess.run(["docker", "rm", "-f", info.container_name], capture_output=True, check=False)
        subprocess.run(
            [
                "docker", "run", "-d", "--name", info.container_name,
                "-v", f"{host_data}:/data",
                *ports,
                settings.meshtastic_image,
                "sh", "-c", "while true; do sleep 3600; done",
            ],
            check=True,
        )
        time.sleep(1.0)
        for i, node in enumerate(chunk):
            global_i = base_index + i
            port = settings.tcp_port_base + global_i
            hw_id = settings.hw_id_offset + global_i
            cmd = (
                f"{MESHTASTICD} -e -s -d /data/node_{global_i} "
                f"--hwid=0x{hw_id:x} --port={port} >>/tmp/out_{global_i}.log 2>&1"
            )
            subprocess.run(
                ["docker", "exec", "-d", info.container_name, "sh", "-c", cmd],
                check=True,
            )
            logger.info("Started meshtasticd %s in %s", node.id, info.container_name)

    def stop(self) -> None:
        if settings.dry_run:
            return
        for info in self.shards:
            subprocess.run(["docker", "rm", "-f", info.container_name], capture_output=True, check=False)
        self.shards.clear()

    def status(self) -> list[dict]:
        out: list[dict] = []
        for info in self.shards:
            proc = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", info.container_name],
                capture_output=True, text=True,
            )
            state = proc.stdout.strip() if proc.returncode == 0 else "missing"
            out.append({"shard_id": info.shard_id, "container": info.container_name, "state": state, "nodes": info.node_indices})
        return out
'''

SIM = r'''
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from meshtastic.tcp_interface import TCPInterface

from app.config import settings
from app.models.scenario import ScenarioStatus
from app.services.influx_writer import influx_writer
from app.services.node_provisioner import provision_node
from app.services.packet_trace import packet_trace_service
from app.services.propagation.itm_precompute import run_precompute
from app.services.propagation.link_matrix import load_link_matrix
from app.services.radio_gateway import RadioGateway
from app.services.scenario_store import scenario_store
from app.services.shard_manager import ShardManager
from app.services.traffic_generator import TrafficGenerator

logger = logging.getLogger(__name__)


class SimulationManager:
    def __init__(self) -> None:
        self.active_scenario_id: Optional[str] = None
        self._gateway: Optional[RadioGateway] = None
        self._shards: Optional[ShardManager] = None
        self._traffic: Optional[TrafficGenerator] = None
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

        time.sleep(5.0)
        for i, node in enumerate(scenario.nodes):
            port = node.tcp_port or (settings.tcp_port_base + i)
            iface = TCPInterface(hostname=settings.meshtastic_tcp_host, portNumber=port, connectNow=True)
            try:
                provision_node(iface, node, i)
            finally:
                iface.close()

        gateway = RadioGateway(scenario_id, env.experiment_id, scenario.nodes, matrix, env.fading_sigma_db)
        gateway.set_lora(lora)
        gateway.connect()
        gateway.start()
        with self._lock:
            self._gateway = gateway

        if traffic_path and traffic_path.exists():
            self._traffic = TrafficGenerator(
                traffic_path, gateway._ifaces, {n.id: i for i, n in enumerate(scenario.nodes)}
            )
            self._traffic.start()

        with self._lock:
            self.active_scenario_id = scenario_id
        scenario_store.set_status(scenario_id, ScenarioStatus.RUNNING)

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

    def status(self) -> dict:
        return {
            "active_scenario_id": self.active_scenario_id,
            "gateway_running": self._gateway is not None,
            "shards": self._shards.status() if self._shards else [],
            "precompute_running": bool(self._precompute_thread and self._precompute_thread.is_alive()),
        }


simulation_manager = SimulationManager()
'''

if __name__ == "__main__":
    (ROOT / "app/services/shard_manager.py").write_text(SHARD.strip() + "\n", encoding="utf-8")
    (ROOT / "app/services/simulation_manager.py").write_text(SIM.strip() + "\n", encoding="utf-8")
    print("patched shard_manager.py and simulation_manager.py")
