#!/usr/bin/env python3
"""Fix TCP connect flow: keep provisioned ifaces open, wait for meshtasticd ports."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend"

MESHTASTIC_TCP = '''
from __future__ import annotations

import logging
import socket
import time

from meshtastic.tcp_interface import TCPInterface

logger = logging.getLogger(__name__)


def wait_for_tcp_port(host: str, port: int, timeout: float = 90.0) -> None:
    """Block until something accepts TCP on host:port (meshtasticd ready)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"meshtasticd not listening on {host}:{port} after {timeout:.0f}s")


def open_tcp_interface(host: str, port: int, timeout: float = 90.0) -> TCPInterface:
    """Wait for port, then connect with retries (handles brief resets after admin)."""
    wait_for_tcp_port(host, port, timeout=timeout)
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return TCPInterface(hostname=host, portNumber=port, connectNow=True)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            last_exc = exc
            logger.debug("TCP connect %s:%s retry: %s", host, port, exc)
            time.sleep(0.5)
    raise TimeoutError(f"Could not connect to {host}:{port}: {last_exc}") from last_exc
'''

RADIO_GATEWAY_CONNECT = """
    def attach_node(self, node_id: str, iface: TCPInterface, index: int) -> None:
        # Register an already-connected interface and keep it open for the simulation.
        self._ifaces[node_id] = iface
        self._id_by_hw[HW_ID_OFFSET + index] = node_id

    def connect(self) -> None:
        # Connect nodes not already attached during provisioning.
        from app.services.meshtastic_tcp import open_tcp_interface

        host = settings.meshtastic_tcp_host
        for i, node in enumerate(self.nodes):
            if node.id in self._ifaces:
                continue
            if node.tcp_port is None:
                node.tcp_port = settings.tcp_port_base + i
            iface = open_tcp_interface(host, node.tcp_port)
            self.attach_node(node.id, iface, i)
        self.activate()

    def activate(self) -> None:
        pub.subscribe(self._on_simulator_packet, "meshtastic.receive.simulator")
"""

SIM_START_BLOCK = """
        host = settings.meshtastic_tcp_host
        for i, node in enumerate(scenario.nodes):
            port = node.tcp_port or (settings.tcp_port_base + i)
            wait_for_tcp_port(host, port)

        gateway = RadioGateway(scenario_id, env.experiment_id, scenario.nodes, matrix, env.fading_sigma_db)
        gateway.set_lora(lora)
        for i, node in enumerate(scenario.nodes):
            port = node.tcp_port or (settings.tcp_port_base + i)
            iface = open_tcp_interface(host, port)
            provision_node(iface, node, i)
            gateway.attach_node(node.id, iface, i)
        gateway.activate()
        gateway.start()
"""


def patch_radio_gateway(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    old = """    def connect(self) -> None:
        for i, node in enumerate(self.nodes):
            if node.tcp_port is None:
                node.tcp_port = settings.tcp_port_base + i
            host = settings.meshtastic_tcp_host
            iface = TCPInterface(
                hostname=host,
                portNumber=node.tcp_port,
                connectNow=True,
                noProto=False,
            )
            self._ifaces[node.id] = iface
            hw = HW_ID_OFFSET + i
            self._id_by_hw[hw] = node.id
            time.sleep(0.1)
        pub.subscribe(self._on_simulator_packet, "meshtastic.receive.simulator")"""
    if old not in text:
        if "def attach_node" in text:
            print("radio_gateway already patched")
            return
        raise SystemExit("radio_gateway connect block not found")
    path.write_text(text.replace(old, RADIO_GATEWAY_CONNECT.strip() + "\n"), encoding="utf-8")


def patch_simulation_manager(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if "gateway.attach_node" in text:
        print("simulation_manager already patched")
        return
    # patch_docker_sim template
    old = """        time.sleep(5.0)
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
        gateway.start()"""
    if old not in text:
        raise SystemExit("simulation_manager start block not found")
    text = text.replace(old, SIM_START_BLOCK.strip())
    text = text.replace(
        "from meshtastic.tcp_interface import TCPInterface\n\nfrom app.config",
        "from app.config",
    )
    text = text.replace(
        "from app.services.node_provisioner import provision_node",
        "from app.services.meshtastic_tcp import open_tcp_interface, wait_for_tcp_port\n"
        "from app.services.node_provisioner import provision_node",
    )
    path.write_text(text, encoding="utf-8")


def patch_shard_manager(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    needle = '            logger.info("Started meshtasticd %s in %s", node.id, info.container_name)\n'
    if "time.sleep(0.3)" in text:
        print("shard_manager already patched")
        return
    if needle not in text:
        raise SystemExit("shard_manager meshtasticd log line not found")
    path.write_text(
        text.replace(needle, needle + "            time.sleep(0.3)\n"),
        encoding="utf-8",
    )


if __name__ == "__main__":
    (ROOT / "app/services/meshtastic_tcp.py").write_text(MESHTASTIC_TCP.strip() + "\n", encoding="utf-8")
    print("wrote meshtastic_tcp.py")

    rg = ROOT / "app/services/radio_gateway.py"
    sm = ROOT / "app/services/simulation_manager.py"
    sh = ROOT / "app/services/shard_manager.py"

    # Restore from patch_docker_sim if corrupted
    for p in (rg, sm):
        if p.read_bytes()[:2] != b"fr" and b"from __future__" not in p.read_bytes()[:200]:
            import runpy
            runpy.run_path(str(Path(__file__).resolve().parent / "patch_docker_sim.py"))

    patch_radio_gateway(rg)
    patch_simulation_manager(sm)
    patch_shard_manager(sh)
    print("patched radio_gateway, simulation_manager, shard_manager")
