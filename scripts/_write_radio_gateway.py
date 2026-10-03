#!/usr/bin/env python3
"""One-off writer: full radio_gateway.py (UTF-8)."""
from pathlib import Path

CONTENT = r'''from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Optional

from meshtastic import mesh_pb2, portnums_pb2, protocols as meshtastic_protocols
from meshtastic.tcp_interface import TCPInterface
from pubsub import pub

from app.config import settings
from app.models.node import NodeConfig
from app.models.packet_trace import LinkAttempt, PacketOtaRx, PacketOtaTx
from app.services.influx_writer import influx_writer
from app.services.mesh_packet_util import packet_as_dict
from app.services.packet_trace import packet_trace_service
from app.services.propagation.link_matrix import LinkMatrix

logger = logging.getLogger(__name__)

HW_ID_OFFSET = settings.hw_id_offset

_RECEIVE_TOPICS: tuple[str, ...] = tuple(
    {f"meshtastic.receive.{proto.name}" for proto in meshtastic_protocols.values()}
)


class RadioGateway:
    def __init__(
        self,
        scenario_id: str,
        experiment_id: str,
        nodes: list[NodeConfig],
        link_matrix: LinkMatrix,
        fading_sigma_db: float,
    ) -> None:
        self.scenario_id = scenario_id
        self.experiment_id = experiment_id
        self.nodes = nodes
        self.link_matrix = link_matrix
        self.fading_sigma_db = fading_sigma_db
        self._ifaces: dict[str, TCPInterface] = {}
        self._id_by_hw: dict[int, str] = {}
        self._rng = random.Random()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lora = None
        self._seen_tx: dict[tuple[int, int], float] = {}
        self._dedupe_lock = threading.Lock()

    def set_lora(self, lora) -> None:
        self._lora = lora

    def attach_node(self, node_id: str, iface: TCPInterface, index: int) -> None:
        self._ifaces[node_id] = iface
        self._id_by_hw[HW_ID_OFFSET + index] = node_id

    def connect(self) -> None:
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
        for topic in _RECEIVE_TOPICS:
            pub.subscribe(self._on_mesh_packet, topic)

    def disconnect(self) -> None:
        for topic in _RECEIVE_TOPICS:
            try:
                pub.unsubscribe(self._on_mesh_packet, topic)
            except Exception:
                pass
        with self._dedupe_lock:
            self._seen_tx.clear()
        for iface in self._ifaces.values():
            try:
                iface.close()
            except Exception:
                pass
        self._ifaces.clear()

    def _node_id_from_packet(self, packet: dict[str, Any]) -> Optional[str]:
        frm = int(packet.get("from", 0))
        if frm in self._id_by_hw:
            return self._id_by_hw[frm]
        for i, node in enumerate(self.nodes):
            if HW_ID_OFFSET + i == frm:
                return node.id
        return None

    def process_local_tx(self, node_id: str, packet: Any) -> None:
        if not self._running:
            return
        idx = next((i for i, n in enumerate(self.nodes) if n.id == node_id), None)
        from_hw = HW_ID_OFFSET + idx if idx is not None else None
        self._handle_transmission(node_id, packet_as_dict(packet, from_hw=from_hw))

    def _on_mesh_packet(
        self, packet: dict[str, Any], interface: TCPInterface, topic: Any = None
    ) -> None:
        if not self._running:
            return
        tx_id = self._node_id_from_packet(packet)
        if tx_id is None:
            for node in self.nodes:
                if self._ifaces.get(node.id) is interface:
                    tx_id = node.id
                    break
        if tx_id is None:
            return
        self._handle_transmission(tx_id, packet_as_dict(packet))

    def _dedupe_seen(self, packet: dict[str, Any]) -> bool:
        key = (int(packet.get("id", 0)), int(packet.get("from", 0)))
        now = time.monotonic()
        with self._dedupe_lock:
            stale = [k for k, t in self._seen_tx.items() if now - t > 2.0]
            for k in stale:
                del self._seen_tx[k]
            if key in self._seen_tx:
                return True
            self._seen_tx[key] = now
        return False

    @staticmethod
    def _portnum_int(packet: dict[str, Any]) -> int:
        decoded = packet.get("decoded") or {}
        if not isinstance(decoded, dict):
            decoded = {}
        raw = decoded.get("portnum", portnums_pb2.PortNum.UNKNOWN_APP)
        if isinstance(raw, int):
            return raw
        name = str(raw)
        if name.isdigit():
            return int(name)
        try:
            return portnums_pb2.PortNum.Value(name)
        except ValueError:
            return portnums_pb2.PortNum.UNKNOWN_APP

    def _handle_transmission(self, tx_id: str, packet: dict[str, Any]) -> None:
        if self._dedupe_seen(packet):
            return
        decoded = packet.get("decoded") or {}
        if not isinstance(decoded, dict):
            decoded = {}
        raw_portnum = decoded.get("portnum", "UNKNOWN_APP")
        portnum = (
            portnums_pb2.PortNum.Name(raw_portnum)
            if isinstance(raw_portnum, int)
            else str(raw_portnum)
        )
        packet_id = int(packet.get("id", 0))
        hop_limit = packet.get("hopLimit")
        hop_start = packet.get("hopStart")
        to_raw = int(packet.get("to", 0))
        is_broadcast = to_raw == 0xFFFFFFFF or to_raw == 4294967295

        payload = decoded.get("payload", b"")
        if hasattr(payload, "__len__"):
            payload_len = len(payload)
        else:
            payload_len = 0

        hop_start_val = int(hop_start) if hop_start is not None else None
        hop_limit_val = int(hop_limit) if hop_limit is not None else None
        is_rebroadcast = (
            hop_start_val is not None
            and hop_limit_val is not None
            and hop_start_val > hop_limit_val
        )

        tx_evt = PacketOtaTx(
            scenario_id=self.scenario_id,
            experiment_id=self.experiment_id,
            packet_id=packet_id,
            tx_node=tx_id,
            to_node=None if is_broadcast else str(to_raw),
            is_broadcast=is_broadcast,
            hop_limit=hop_limit_val,
            hop_start=hop_start_val,
            portnum=str(portnum),
            payload_len=payload_len,
            is_rebroadcast=is_rebroadcast,
        )
        packet_trace_service.record_tx(tx_evt)
        influx_writer.write_packet_tx(tx_evt)

        tx_idx = self.link_matrix.index(tx_id)
        tx_node = self.nodes[tx_idx]
        lora = self._lora
        if lora is None:
            return

        candidates = self.link_matrix.connectivity_neighbors.get(tx_id, [])
        if not candidates:
            candidates = [n.id for n in self.nodes if n.id != tx_id]

        receivers: list[NodeConfig] = []
        rssis: list[float] = []
        snrs: list[float] = []

        for rx_id in candidates:
            if rx_id == tx_id:
                continue
            rx_idx = self.link_matrix.index(rx_id)
            rx_node = self.nodes[rx_idx]
            pl = float(self.link_matrix.path_loss_median_db[tx_idx, rx_idx])
            shadow = self._rng.gauss(0.0, self.fading_sigma_db)
            rssi = (
                lora.tx_power_dbm
                + tx_node.antenna_gain_tx_db
                + rx_node.antenna_gain_rx_db
                - pl
                + shadow
            )
            snr = rssi - rx_node.noise_floor_dbm
            delivered = rssi >= lora.sensitivity_dbm

            att = LinkAttempt(
                scenario_id=self.scenario_id,
                experiment_id=self.experiment_id,
                packet_id=packet_id,
                tx_node=tx_id,
                rx_node=rx_id,
                path_loss_median_db=pl,
                shadow_db=shadow,
                rssi_db=rssi,
                snr_db=snr,
                delivered=delivered,
            )
            packet_trace_service.record_link_attempt(att)
            influx_writer.write_link_attempt(att)

            if delivered:
                receivers.append(rx_node)
                rssis.append(rssi)
                snrs.append(snr)

        if receivers:
            self._forward_packet(receivers, packet, rssis, snrs, packet_id)

    @staticmethod
    def _packet_from_packet(packet: dict[str, Any], data: bytes, portnum: int) -> mesh_pb2.MeshPacket:
        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.decoded.payload = data
        mesh_packet.decoded.portnum = portnum
        mesh_packet.to = packet["to"]
        setattr(mesh_packet, "from", packet["from"])
        mesh_packet.id = packet["id"]
        mesh_packet.want_ack = packet.get("wantAck", mesh_packet.want_ack)
        if packet.get("hopLimit") is not None:
            mesh_packet.hop_limit = packet["hopLimit"]
        if packet.get("hopStart") is not None:
            mesh_packet.hop_start = packet["hopStart"]
        mesh_packet.channel = int(packet.get("channel", mesh_packet.channel))
        if "requestId" in packet.get("decoded", {}):
            mesh_packet.decoded.request_id = packet["decoded"]["requestId"]
        return mesh_packet

    def _forward_packet(
        self,
        receivers: list[NodeConfig],
        packet: dict[str, Any],
        rssis: list[float],
        snrs: list[float],
        packet_id: int,
    ) -> None:
        data = packet["decoded"]["payload"]
        if hasattr(data, "SerializeToString"):
            data = data.SerializeToString()
        elif not isinstance(data, (bytes, bytearray)):
            data = bytes(data) if data else b""

        mesh_packet = self._packet_from_packet(packet, data, self._portnum_int(packet))
        for i, rx in enumerate(receivers):
            mesh_packet.rx_rssi = int(rssis[i])
            mesh_packet.rx_snr = float(snrs[i])
            to_radio = mesh_pb2.ToRadio()
            to_radio.packet.CopyFrom(mesh_packet)
            iface = self._ifaces.get(rx.id)
            if iface is None:
                continue
            try:
                iface._sendToRadio(to_radio)
            except Exception as exc:
                logger.warning("Forward to %s failed: %s", rx.id, exc)
                continue
            rx_evt = PacketOtaRx(
                scenario_id=self.scenario_id,
                experiment_id=self.experiment_id,
                packet_id=packet_id,
                rx_node=rx.id,
                rssi_db=rssis[i],
                snr_db=snrs[i],
            )
            packet_trace_service.record_rx(rx_evt)

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False
'''

path = Path(__file__).resolve().parents[1] / "backend/app/services/radio_gateway.py"
path.write_text(CONTENT.strip() + "\n", encoding="utf-8")
print("wrote", path, path.stat().st_size)
