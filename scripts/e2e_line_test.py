#!/usr/bin/env python3
"""End-to-end check on real meshtasticd: A - B - C in a line, A cannot hear C.

A broadcast from A must reach C only through B's rebroadcast, and the provisioned
configuration (hop_limit, role, preset) must survive the restart after provisioning.

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
from app.services.meshtastic_tcp import open_tcp_interface, wait_for_mesh_connected, wait_for_tcp_port
from app.services.node_provisioner import provision_node, verify_provisioning
from app.services.propagation.link_matrix import LinkMatrix
from app.services.radio_gateway import RadioGateway
from app.services.shard_manager import ShardManager

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
HOST = "127.0.0.1"


def main() -> int:
    env = EnvironmentConfig(preset="SHORT_FAST", fading_sigma_db=0.0, experiment_id="e2e")
    nodes = [
        NodeConfig(id="a", lat=50.0, lon=20.00, hop_limit=3),
        NodeConfig(id="b", lat=50.0, lon=20.01, hop_limit=3, role=NodeRole.ROUTER),
        NodeConfig(id="c", lat=50.0, lon=20.02, hop_limit=3),
    ]
    pl = np.array([[0, 100, 300], [100, 0, 100], [300, 100, 0]], dtype=float)
    matrix = LinkMatrix(
        node_ids=["a", "b", "c"],
        path_loss_median_db=pl,
        median_rssi_db=-pl,
        distance_m=np.full((3, 3), 700.0),
        connectivity_neighbors={"a": ["b"], "b": ["a", "c"], "c": ["b"]},
    )
    shards = ShardManager("e2e-line", shard_size=3)
    received: list[tuple[str, int, str]] = []
    gateway = RadioGateway("e2e-line", "e2e", nodes, matrix, 0.0, random_seed=1)
    gateway.set_lora(env.resolve_lora())
    ok = True
    try:
        shards.start(nodes)
        for i, node in enumerate(nodes):
            port = settings.tcp_port_base + i
            prov = open_tcp_interface(HOST, port, timeout=60)
            wait_for_mesh_connected(prov, timeout=60)
            provision_node(prov, node, i, preset_name=env.preset, lora=env.resolve_lora())
            time.sleep(1.0)
            prov.close()
            shards.restart_meshtasticd(i)
            wait_for_tcp_port(HOST, port, timeout=60)
            iface = open_tcp_interface(HOST, port, timeout=60)
            wait_for_mesh_connected(iface, timeout=60)
            lora = iface.localNode.localConfig.lora
            print(f"{node.id}: nodenum=0x{iface.myInfo.my_node_num:x} hop_limit={lora.hop_limit} "
                  f"preset={lora.modem_preset} region={lora.region}")
            verify_provisioning(iface, node, env.preset)
            gateway.attach_node(node.id, iface, i)

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
        from app.services.packet_trace import packet_trace_service

        for tx in packet_trace_service.list_recent_tx(50):
            print(f"  TX {tx.tx_node} origin={tx.origin_node} id={tx.packet_id} {tx.portnum} "
                  f"hop {tx.hop_limit}/{tx.hop_start} rebroadcast={tx.is_rebroadcast}")
        a_hw = gateway.node_num("a")
        texts = [r for r in received if r[2] == "hello from A"]
        if not any(r[0] == "c" and r[1] == a_hw for r in texts):
            print("FAIL: C did not receive A's message via B")
            ok = False
        if any(r[1] != a_hw for r in texts):
            print("FAIL: message re-originated by another node")
            ok = False
    finally:
        gateway.stop()
        gateway.disconnect()
        shards.stop()
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
