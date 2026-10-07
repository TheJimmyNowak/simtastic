from __future__ import annotations

import logging
import random
import threading
from typing import Any, Optional

from meshtastic import mesh_pb2, portnums_pb2, protocols as meshtastic_protocols
from meshtastic.tcp_interface import TCPInterface
from pubsub import pub

from app.config import settings
from app.models.node import NodeConfig
from app.models.packet_trace import LinkAttempt, PacketOtaRx, PacketOtaTx
from app.services.influx_writer import influx_writer
from app.services.packet_trace import packet_trace_service
from app.services.propagation.link_matrix import LinkMatrix
from app.services.rf_link_model import (
    compute_rssi_db,
    compute_snr_db,
    is_packet_decoded_rssi,
)

logger = logging.getLogger(__name__)

HW_ID_OFFSET = settings.hw_id_offset

# W trybie `meshtasticd -s` (SimRadio) każda transmisja radiowa węzła jest raportowana
# jego klientowi jako pakiet SIMULATOR_APP (oryginalny portnum + dane w `Compressed`).
# Zwykłe odbiory (TEXT_MESSAGE_APP itp.) to pakiety już dostarczone — nie transmisje.
SIMULATOR_TOPIC = (
    f"meshtastic.receive.{meshtastic_protocols[portnums_pb2.PortNum.SIMULATOR_APP].name}"
)


class RadioGateway:
    def __init__(
        self,
        scenario_id: str,
        experiment_id: str,
        nodes: list[NodeConfig],
        link_matrix: LinkMatrix,
        fading_sigma_db: float,
        random_seed: int | None = None,
    ) -> None:
        self.scenario_id = scenario_id
        self.experiment_id = experiment_id
        self.nodes = nodes
        self.link_matrix = link_matrix
        self.fading_sigma_db = fading_sigma_db
        self._ifaces: dict[str, TCPInterface] = {}
        self._endpoints: dict[str, tuple[str, int]] = {}
        self._id_by_hw: dict[int, str] = {}
        self._rng = random.Random(random_seed)
        self._rng_lock = threading.Lock()
        self._running = False
        self._lora = None

    def set_lora(self, lora) -> None:
        self._lora = lora

    def attach_node(
        self,
        node_id: str,
        iface: TCPInterface,
        index: int,
        endpoint: tuple[str, int] | None = None,
    ) -> None:
        self._ifaces[node_id] = iface
        if endpoint is not None:
            self._endpoints[node_id] = endpoint
        # meshtasticd wyprowadza numer węzła z wygenerowanych kluczy, nie z --hwid,
        # więc numer bierzemy z handshake'u (myInfo); hwid to tylko fallback.
        my_info = getattr(iface, "myInfo", None)
        node_num = int(getattr(my_info, "my_node_num", 0) or (HW_ID_OFFSET + index))
        for num, known_id in list(self._id_by_hw.items()):
            if known_id == node_id:
                del self._id_by_hw[num]
        self._id_by_hw[node_num] = node_id

    def node_num(self, node_id: str) -> int:
        for num, known_id in self._id_by_hw.items():
            if known_id == node_id:
                return num
        raise KeyError(node_id)

    def reconnect_node(self, node_id: str, index: int) -> TCPInterface:
        """Replace a dropped Meshtastic TCP session (keeps pubsub subscriptions)."""
        from app.services.meshtastic_tcp import replace_tcp_interface

        host, port = self._endpoints[node_id]
        old = self._ifaces.get(node_id)
        iface = replace_tcp_interface(
            old,
            host,
            port,
            timeout=settings.sim_meshtastic_tcp_timeout_s,
        )
        self.attach_node(node_id, iface, index)
        logger.info("Reconnected gateway to node %s on port %s", node_id, port)
        return iface

    def activate(self) -> None:
        pub.subscribe(self._on_mesh_packet, SIMULATOR_TOPIC)

    def disconnect(self) -> None:
        try:
            pub.unsubscribe(self._on_mesh_packet, SIMULATOR_TOPIC)
        except Exception:
            pass
        for iface in self._ifaces.values():
            try:
                # close() wysyła też pakiety czekające w kolejce klienta i czeka na wolne
                # miejsce w kolejce TX firmware — przy zatkanym węźle bez końca.
                iface.queue.clear()
                iface.close()
            except Exception:
                pass
        self._ifaces.clear()

    def _node_id_for_interface(self, interface: Any) -> Optional[str]:
        for node_id, iface in self._ifaces.items():
            if iface is interface:
                return node_id
        return None

    def _on_mesh_packet(
        self, packet: dict[str, Any], interface: TCPInterface, topic: Any = None
    ) -> None:
        if not self._running:
            return
        # Nadajnikiem jest węzeł, którego klient zgłosił transmisję — NIE pole `from`,
        # które przy retransmisji wskazuje pierwotnego autora pakietu.
        tx_id = self._node_id_for_interface(interface)
        if tx_id is None:
            return
        self._handle_transmission(tx_id, packet)

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

    @staticmethod
    def _inner_payload(packet: dict[str, Any]) -> tuple[str, int]:
        """Oryginalny portnum i długość danych z opakowania SIMULATOR_APP."""
        decoded = packet.get("decoded") or {}
        if not isinstance(decoded, dict):
            decoded = {}
        payload = decoded.get("payload", b"")
        portnum = RadioGateway._portnum_int(packet)
        if portnum != portnums_pb2.PortNum.SIMULATOR_APP:
            return portnums_pb2.PortNum.Name(portnum), len(payload) if hasattr(payload, "__len__") else 0
        inner = mesh_pb2.Compressed()
        try:
            inner.ParseFromString(bytes(payload))
        except Exception:
            return portnums_pb2.PortNum.Name(portnums_pb2.PortNum.UNKNOWN_APP), 0
        return portnums_pb2.PortNum.Name(inner.portnum), len(inner.data)

    def _handle_transmission(self, tx_id: str, packet: dict[str, Any]) -> None:
        portnum, payload_len = self._inner_payload(packet)
        packet_id = int(packet.get("id", 0))
        hop_limit = packet.get("hopLimit")
        hop_start = packet.get("hopStart")
        to_raw = int(packet.get("to", 0))
        is_broadcast = to_raw == 0xFFFFFFFF
        origin_id = self._id_by_hw.get(int(packet.get("from", 0)))

        hop_start_val = int(hop_start) if hop_start is not None else None
        # MessageToDict pomija zera: brak hopLimit przy obecnym hopStart oznacza 0.
        if hop_limit is not None:
            hop_limit_val: Optional[int] = int(hop_limit)
        else:
            hop_limit_val = 0 if hop_start_val is not None else None
        is_rebroadcast = (origin_id is not None and origin_id != tx_id) or (
            hop_start_val is not None
            and hop_limit_val is not None
            and hop_start_val > hop_limit_val
        )

        tx_evt = PacketOtaTx(
            scenario_id=self.scenario_id,
            experiment_id=self.experiment_id,
            packet_id=packet_id,
            tx_node=tx_id,
            origin_node=origin_id,
            to_node=None if is_broadcast else self._id_by_hw.get(to_raw, f"!{to_raw:08x}"),
            is_broadcast=is_broadcast,
            hop_limit=hop_limit_val,
            hop_start=hop_start_val,
            portnum=portnum,
            payload_len=payload_len,
            is_rebroadcast=is_rebroadcast,
        )
        packet_trace_service.record_tx(tx_evt)
        influx_writer.write_packet_tx(tx_evt)

        if self._lora is None:
            return
        receivers: list[tuple[NodeConfig, float, float]] = []
        for rx_id in self.link_matrix.connectivity_neighbors.get(tx_id, []):
            if rx_id == tx_id:
                continue
            attempt = self._simulate_link(tx_id, rx_id, packet_id)
            packet_trace_service.record_link_attempt(attempt)
            influx_writer.write_link_attempt(attempt)
            if attempt.delivered:
                rx_node = self.nodes[self.link_matrix.index(rx_id)]
                receivers.append((rx_node, attempt.rssi_db, attempt.snr_db))
        if receivers:
            self._forward_packet(receivers, packet, packet_id)

    def _simulate_link(self, tx_id: str, rx_id: str, packet_id: int) -> LinkAttempt:
        """Jedna próba odbioru: medianowa strata z macierzy + losowy fading N(0, σ²)."""
        tx_idx, rx_idx = self.link_matrix.index(tx_id), self.link_matrix.index(rx_id)
        tx_node, rx_node = self.nodes[tx_idx], self.nodes[rx_idx]
        link = self.link_matrix.link(tx_idx, rx_idx)
        with self._rng_lock:
            shadow = self._rng.gauss(0.0, self.fading_sigma_db)
        rssi = compute_rssi_db(
            self._lora.tx_power_dbm,
            tx_node.antenna_gain_tx_db,
            rx_node.antenna_gain_rx_db,
            link.path_loss_db,
            shadow,
        )
        snr = compute_snr_db(rssi, rx_node.noise_floor_dbm)
        return LinkAttempt(
            scenario_id=self.scenario_id,
            experiment_id=self.experiment_id,
            packet_id=packet_id,
            tx_node=tx_id,
            rx_node=rx_id,
            path_loss_median_db=link.path_loss_db,
            terrain_loss_db=link.terrain_loss_db,
            building_diffraction_db=link.building_loss_db,
            blocked_buildings_count=link.blocked_buildings_count,
            fresnel_clearance_m=link.fresnel_clearance_m,
            dominant_building_id=link.dominant_building_id,
            building_data_year=link.building_data_year,
            propagation_model=self.link_matrix.propagation_model,
            shadow_db=shadow,
            rssi_db=rssi,
            snr_db=snr,
            delivered=is_packet_decoded_rssi(rssi, self._lora.sensitivity_dbm),
        )

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
        if packet.get("nextHop") is not None:
            mesh_packet.next_hop = int(packet["nextHop"])
        if packet.get("relayNode") is not None:
            mesh_packet.relay_node = int(packet["relayNode"])
        if "requestId" in packet.get("decoded", {}):
            mesh_packet.decoded.request_id = packet["decoded"]["requestId"]
        return mesh_packet

    def _air_packet(self, packet: dict[str, Any]) -> mesh_pb2.MeshPacket:
        """Pakiet „na eterze” do wstrzyknięcia odbiorcom (portnum SIMULATOR_APP)."""
        raw = packet.get("raw")
        if isinstance(raw, mesh_pb2.MeshPacket):
            # Pełna kopia zachowuje m.in. next_hop / relay_node (NextHopRouter, fw 2.6+).
            mesh_packet = mesh_pb2.MeshPacket()
            mesh_packet.CopyFrom(raw)
            for field in ("rx_time", "rx_snr", "rx_rssi"):
                mesh_packet.ClearField(field)
            return mesh_packet
        data = packet["decoded"]["payload"]
        if hasattr(data, "SerializeToString"):
            data = data.SerializeToString()
        elif not isinstance(data, (bytes, bytearray)):
            data = bytes(data) if data else b""
        return self._packet_from_packet(packet, data, self._portnum_int(packet))

    def _forward_packet(
        self,
        receivers: list[tuple[NodeConfig, float, float]],
        packet: dict[str, Any],
        packet_id: int,
    ) -> None:
        """Wstrzykuje pakiet odbiorcom; `receivers` to krotki (węzeł, RSSI, SNR)."""
        mesh_packet = self._air_packet(packet)
        if mesh_packet.decoded.portnum != portnums_pb2.PortNum.SIMULATOR_APP:
            # Inny portnum firmware traktuje jako wiadomość od klienta (from := 0)
            # i nadałby ją jako własną — to nie byłby odbiór radiowy.
            logger.warning("Not forwarding non-SIMULATOR_APP packet %s", packet_id)
            return
        for rx, rssi, snr in receivers:
            mesh_packet.rx_rssi = int(round(rssi))
            mesh_packet.rx_snr = float(snr)
            to_radio = mesh_pb2.ToRadio()
            to_radio.packet.CopyFrom(mesh_packet)
            iface = self._ifaces.get(rx.id)
            if iface is None:
                continue
            try:
                # Zapis bezpośrednio do strumienia, z pominięciem kontroli przepływu klienta:
                # _sendToRadio czeka (bez limitu) na wolne miejsce w kolejce TX firmware,
                # a odbiory wszystkich węzłów są wstrzykiwane z jednego wątku biblioteki,
                # więc jeden zatkany węzeł zatrzymywałby całą symulację. To odbiór
                # radiowy, a nie wysyłka klienta, więc kolejka TX go nie dotyczy.
                iface._sendToRadioImpl(to_radio)
            except Exception as exc:
                logger.warning("Forward to %s failed: %s", rx.id, exc)
                continue
            rx_evt = PacketOtaRx(
                scenario_id=self.scenario_id,
                experiment_id=self.experiment_id,
                packet_id=packet_id,
                rx_node=rx.id,
                rssi_db=rssi,
                snr_db=snr,
            )
            packet_trace_service.record_rx(rx_evt)

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False
