import numpy as np
import pytest
from google.protobuf.json_format import MessageToDict
from meshtastic import mesh_pb2, portnums_pb2

from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig
from app.services.mesh_packet_util import packet_as_dict
from app.services.packet_trace import packet_trace_service
from app.services.propagation.link_matrix import LinkMatrix
from app.services.radio_gateway import HW_ID_OFFSET, SIMULATOR_TOPIC, RadioGateway


def test_portnum_int_from_packet_dict() -> None:
    pkt = {
        "decoded": {"portnum": "POSITION_APP", "payload": b""},
    }
    assert RadioGateway._portnum_int(pkt) == portnums_pb2.PortNum.POSITION_APP


def test_packet_as_dict_from_mesh_packet() -> None:
    p = mesh_pb2.MeshPacket()
    p.id = 99
    p.to = 0xFFFFFFFF
    p.decoded.payload = b"hello"
    p.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    p.hop_limit = 3
    d = packet_as_dict(p, from_hw=0x10)
    assert d["id"] == 99
    assert d["from"] == 0x10
    assert d["decoded"]["payload"] == b"hello"
    assert "TEXT_MESSAGE" in d["decoded"]["portnum"]


class FakeIface:
    def __init__(self) -> None:
        self.sent: list[mesh_pb2.ToRadio] = []

    def _sendToRadio(self, to_radio: mesh_pb2.ToRadio) -> None:
        self.sent.append(to_radio)

    def close(self) -> None:
        pass


def sim_tx_packet(
    *, packet_id: int, origin_hw: int, hop_limit: int, hop_start: int = 3, text: bytes = b"hi"
) -> dict:
    """Packet dict as meshtastic publishes it for a SimRadio transmission."""
    inner = mesh_pb2.Compressed(portnum=portnums_pb2.PortNum.TEXT_MESSAGE_APP, data=text)
    raw = mesh_pb2.MeshPacket(id=packet_id, to=0xFFFFFFFF, hop_limit=hop_limit, hop_start=hop_start)
    setattr(raw, "from", origin_hw)
    raw.decoded.portnum = portnums_pb2.PortNum.SIMULATOR_APP
    raw.decoded.payload = inner.SerializeToString()
    packet = MessageToDict(raw)
    packet["decoded"]["payload"] = raw.decoded.payload
    packet["raw"] = raw
    return packet


def line_gateway() -> tuple[RadioGateway, dict[str, FakeIface]]:
    """A - B - C in a line: A and C cannot hear each other."""
    nodes = [NodeConfig(id=i, lat=52.0, lon=21.0 + k * 0.01) for k, i in enumerate("abc")]
    pl = np.array([[0, 100, 300], [100, 0, 100], [300, 100, 0]], dtype=float)
    matrix = LinkMatrix(
        node_ids=["a", "b", "c"],
        path_loss_median_db=pl,
        distance_m=np.full((3, 3), 1000.0),
        median_rssi_db=-pl,
        connectivity_neighbors={"a": ["b"], "b": ["a", "c"], "c": ["b"]},
    )
    gw = RadioGateway("sc-line", "exp1", nodes, matrix, fading_sigma_db=0.0)
    gw.set_lora(EnvironmentConfig().resolve_lora())
    ifaces = {node_id: FakeIface() for node_id in "abc"}
    for index, node_id in enumerate("abc"):
        gw.attach_node(node_id, ifaces[node_id], index)
    gw.start()
    return gw, ifaces


def test_simulator_tx_is_forwarded_as_simulator_app() -> None:
    packet_trace_service.clear()
    gw, ifaces = line_gateway()
    gw._on_mesh_packet(sim_tx_packet(packet_id=5, origin_hw=HW_ID_OFFSET, hop_limit=3), ifaces["a"])

    assert [len(ifaces[n].sent) for n in "abc"] == [0, 1, 0]
    injected = ifaces["b"].sent[0].packet
    assert injected.decoded.portnum == portnums_pb2.PortNum.SIMULATOR_APP
    assert getattr(injected, "from") == HW_ID_OFFSET
    tx = packet_trace_service.list_recent_tx(10)[0]
    assert (tx.tx_node, tx.origin_node, tx.portnum, tx.payload_len) == ("a", "a", "TEXT_MESSAGE_APP", 2)
    assert tx.is_rebroadcast is False


def test_rebroadcast_is_propagated_from_relay_position() -> None:
    """B relays A's packet (from=A, same id): RF must start at B and reach C."""
    packet_trace_service.clear()
    gw, ifaces = line_gateway()
    gw._on_mesh_packet(sim_tx_packet(packet_id=9, origin_hw=HW_ID_OFFSET, hop_limit=3), ifaces["a"])
    gw._on_mesh_packet(sim_tx_packet(packet_id=9, origin_hw=HW_ID_OFFSET, hop_limit=2), ifaces["b"])

    assert len(ifaces["c"].sent) == 1
    assert ifaces["c"].sent[0].packet.hop_limit == 2
    relay = packet_trace_service.list_recent_tx(10)[-1]
    assert (relay.tx_node, relay.origin_node, relay.is_rebroadcast) == ("b", "a", True)
    attempts = packet_trace_service.list_recent_attempts(10)
    assert {(a.tx_node, a.rx_node) for a in attempts} == {("a", "b"), ("b", "a"), ("b", "c")}


def test_last_hop_with_zero_hop_limit_is_rebroadcast() -> None:
    packet_trace_service.clear()
    gw, ifaces = line_gateway()
    gw._on_mesh_packet(sim_tx_packet(packet_id=3, origin_hw=HW_ID_OFFSET, hop_limit=0), ifaces["b"])
    tx = packet_trace_service.list_recent_tx(10)[-1]
    assert (tx.hop_limit, tx.hop_start, tx.is_rebroadcast) == (0, 3, True)


def test_unknown_interface_and_stopped_gateway_are_ignored() -> None:
    packet_trace_service.clear()
    gw, ifaces = line_gateway()
    gw._on_mesh_packet(sim_tx_packet(packet_id=1, origin_hw=HW_ID_OFFSET, hop_limit=3), FakeIface())
    gw.stop()
    gw._on_mesh_packet(sim_tx_packet(packet_id=2, origin_hw=HW_ID_OFFSET, hop_limit=3), ifaces["a"])
    assert packet_trace_service.list_recent_tx(10) == []


def test_gateway_listens_only_to_simulator_topic() -> None:
    assert SIMULATOR_TOPIC == "meshtastic.receive.simulator"


def test_non_simulator_packet_is_never_injected() -> None:
    """Injecting e.g. TEXT_MESSAGE_APP would make the firmware re-send it as its own."""
    gw, ifaces = line_gateway()
    p = mesh_pb2.MeshPacket(id=11, to=0xFFFFFFFF)
    p.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    p.decoded.payload = b"x"
    packet = packet_as_dict(p, from_hw=HW_ID_OFFSET)
    gw._forward_packet([gw.nodes[1]], packet, [-80.0], [10.0], 11)
    assert ifaces["b"].sent == []


def test_packets_api_returns_trace(tmp_path) -> None:
    from app.api.routes.packets import list_packets
    from app.services.scenario_store import scenario_store
    from app.models.scenario import Scenario, ScenarioStatus

    packet_trace_service.clear()
    gw = RadioGateway(
        "api-scenario",
        "exp1",
        [
            NodeConfig(id="a", lat=52.0, lon=21.0),
            NodeConfig(id="b", lat=52.05, lon=21.1),
        ],
        LinkMatrix(
            node_ids=["a", "b"],
            path_loss_median_db=np.full((2, 2), 90.0),
            distance_m=np.full((2, 2), 500.0),
            median_rssi_db=np.full((2, 2), -80.0),
            connectivity_neighbors={"a": ["b"], "b": ["a"]},
        ),
        fading_sigma_db=0.0,
    )
    gw.set_lora(EnvironmentConfig().resolve_lora())
    iface = FakeIface()
    gw.attach_node("a", iface, 0)
    gw.attach_node("b", FakeIface(), 1)
    gw.start()
    gw._on_mesh_packet(sim_tx_packet(packet_id=7, origin_hw=HW_ID_OFFSET, hop_limit=3), iface)
    gw.stop()

    scenario_store.update(
        Scenario(id="api-scenario", name="t", status=ScenarioStatus.RUNNING, nodes=[])
    )

    data = list_packets("api-scenario", limit=10)
    assert len(data["transmissions"]) >= 1
    assert data["transmissions"][0]["tx_node"] == "a"
    assert len(data["recent_attempts"]) >= 1
    assert scenario_store.delete("api-scenario")


def test_packet_attempt_lookup_isolated_by_scenario() -> None:
    from app.models.packet_trace import LinkAttempt

    packet_trace_service.clear()
    common = dict(
        experiment_id="exp",
        packet_id=42,
        tx_node="a",
        rx_node="b",
        path_loss_median_db=100,
        shadow_db=0,
        rssi_db=-70,
        snr_db=10,
        delivered=True,
    )
    packet_trace_service.record_link_attempt(LinkAttempt(scenario_id="one", **common))
    packet_trace_service.record_link_attempt(LinkAttempt(scenario_id="two", **common))
    attempts = packet_trace_service.list_attempts_for_packet(42, "one")
    assert len(attempts) == 1
    assert attempts[0].scenario_id == "one"
