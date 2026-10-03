#!/usr/bin/env python3
"""End-to-end check on real meshtasticd: A - B - C in a line, A cannot hear C.

Uses the same code path as the backend (NodeContainers + SimulationManager node
preparation). A broadcast from A must reach C only through B's rebroadcast, and the
provisioned configuration (hop_limit, role, preset) must survive the node restart.

    PYTHONPATH=backend .venv/bin/python scripts/e2e_line_test.py
"""
from __future__ import annotations

import logging
import sys
import time

import numpy as np
from pubsub import pub

from app.config import settings
from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig, NodeRole
from app.models.scenario import Scenario
from app.services.node_containers import NodeContainers
from app.services.packet_trace import packet_trace_service
from app.services.propagation.link_matrix import LinkMatrix
from app.services.radio_gateway import RadioGateway
from app.services.simulation_manager import SimulationManager

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("meshtastic").setLevel(logging.WARNING)


def main() -> int:
    env = EnvironmentConfig(preset="SHORT_FAST", fading_sigma_db=0.0, experiment_id="e2e")
    scenario = Scenario(
        id="e2e-line",
        name="e2e",
        environment=env,
        nodes=[
            NodeConfig(id="a", lat=50.0, lon=20.00, hop_limit=3),
            NodeConfig(id="b", lat=50.0, lon=20.01, hop_limit=3, role=NodeRole.ROUTER),
            NodeConfig(id="c", lat=50.0, lon=20.02, hop_limit=3),
        ],
    )
    pl = np.array([[0, 100, 300], [100, 0, 100], [300, 100, 0]], dtype=float)
    matrix = LinkMatrix(
        node_ids=["a", "b", "c"],
        path_loss_median_db=pl,
        median_rssi_db=-pl,
        distance_m=np.full((3, 3), 700.0),
        connectivity_neighbors={"a": ["b"], "b": ["a", "c"], "c": ["b"]},
    )
    containers = NodeContainers(scenario.id)
    gateway = RadioGateway(scenario.id, "e2e", scenario.nodes, matrix, 0.0, random_seed=1)
    gateway.set_lora(env.resolve_lora())
    received: list[tuple[str, int, str]] = []
    ok = True
    try:
        t0 = time.monotonic()
        containers.start(len(scenario.nodes))
        SimulationManager()._prepare_nodes(scenario, matrix, containers, gateway)
        print(f"3 nodes provisioned and verified in {time.monotonic() - t0:.1f}s")

        def on_text(packet, interface):
            node_id = gateway._node_id_for_interface(interface)
            received.append((node_id, packet.get("from"), packet["decoded"].get("text", "")))

        pub.subscribe(on_text, "meshtastic.receive.text")
        gateway.activate()
        gateway.start()
        time.sleep(3)
        gateway._ifaces["a"].sendText("hello from A")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not any(r[0] == "c" for r in received):
            time.sleep(0.5)
        time.sleep(3)
        print("received:", received)
        for tx in packet_trace_service.list_recent_tx(50):
            print(
                f"  TX {tx.tx_node} origin={tx.origin_node} id={tx.packet_id} {tx.portnum} "
                f"hop {tx.hop_limit}/{tx.hop_start} rebroadcast={tx.is_rebroadcast}"
            )
        a_num = gateway.node_num("a")
        texts = [r for r in received if r[2] == "hello from A"]
        if not any(r[0] == "c" and r[1] == a_num for r in texts):
            print("FAIL: C did not receive A's message via B")
            ok = False
        if any(r[1] != a_num for r in texts):
            print("FAIL: message re-originated by another node")
            ok = False
    except Exception as exc:
        print(f"FAIL: {exc!r}")
        ok = False
    finally:
        gateway.stop()
        gateway.disconnect()
        containers.stop()
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
