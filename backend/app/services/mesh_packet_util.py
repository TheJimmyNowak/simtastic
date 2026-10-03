from __future__ import annotations

from typing import Any

from google.protobuf.json_format import MessageToDict
from meshtastic import mesh_pb2, portnums_pb2


def packet_as_dict(packet: Any, from_hw: int | None = None) -> dict[str, Any]:
    """Normalize MeshPacket protobuf or API dict to gateway packet dict."""
    if isinstance(packet, dict):
        return packet
    if not isinstance(packet, mesh_pb2.MeshPacket):
        raise TypeError(f"Unsupported packet type: {type(packet)!r}")

    out: dict[str, Any] = MessageToDict(packet)
    out["from"] = from_hw if from_hw is not None else int(getattr(packet, "from", 0) or 0)
    out["to"] = int(packet.to)
    out["id"] = int(packet.id)
    if packet.hop_limit:
        out["hopLimit"] = int(packet.hop_limit)
    if packet.hop_start:
        out["hopStart"] = int(packet.hop_start)
    out["wantAck"] = bool(packet.want_ack)
    out["channel"] = int(packet.channel)

    decoded = out.setdefault("decoded", {})
    decoded["payload"] = bytes(packet.decoded.payload) if packet.decoded.payload else b""
    if packet.decoded.portnum:
        decoded["portnum"] = portnums_pb2.PortNum.Name(packet.decoded.portnum)
    elif "portnum" not in decoded:
        decoded["portnum"] = portnums_pb2.PortNum.Name(portnums_pb2.PortNum.UNKNOWN_APP)
    if packet.decoded.request_id:
        decoded["requestId"] = int(packet.decoded.request_id)

    return out
