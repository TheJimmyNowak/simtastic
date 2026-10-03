from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import yaml
from meshtastic import mesh_pb2, portnums_pb2
from meshtastic.tcp_interface import TCPInterface

from app.config import settings
from app.services.meshtastic_tcp import ensure_mesh_connected, is_mesh_connected

if TYPE_CHECKING:
    from app.services.radio_gateway import RadioGateway

from app.models.node import NodeConfig

logger = logging.getLogger(__name__)


class TrafficGenerator:
    def __init__(
        self,
        schedule_path: Path,
        gateway: RadioGateway,
        nodes: list[NodeConfig],
        nodes_by_id: dict[str, int],
    ) -> None:
        self.schedule_path = schedule_path
        self.gateway = gateway
        self.nodes = nodes
        self.nodes_by_id = nodes_by_id
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _dest(self, node_id: str) -> str:
        return f"!{self.gateway.node_num(node_id):08x}"

    def _iface_for(self, from_id: str) -> TCPInterface:
        idx = self.nodes_by_id[from_id]
        iface = self.gateway._ifaces.get(from_id)
        if iface is None:
            raise KeyError(from_id)
        if is_mesh_connected(iface):
            return iface
        logger.warning("Meshtastic session lost for %s, reconnecting", from_id)
        return self.gateway.reconnect_node(from_id, idx)

    def start(self) -> None:
        raw = yaml.safe_load(self.schedule_path.read_text(encoding="utf-8"))
        schedule = raw.get("schedule", []) if isinstance(raw, dict) else []
        self._validate_schedule(schedule)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(schedule,),
            name="traffic-generator",
            daemon=True,
        )
        self._thread.start()

    def _validate_schedule(self, schedule: Any) -> None:
        if not isinstance(schedule, list):
            raise ValueError("traffic schedule must be a list")
        previous_delay = 0.0
        for index, item in enumerate(schedule):
            if not isinstance(item, dict):
                raise ValueError(f"traffic item {index} must be an object")
            action = item.get("action", "broadcast")
            if action not in {"broadcast", "dm", "traceroute"}:
                raise ValueError(f"traffic item {index}: unknown action {action!r}")
            source = str(item.get("from", ""))
            if source not in self.nodes_by_id:
                raise ValueError(f"traffic item {index}: unknown source node {source!r}")
            if action in {"dm", "traceroute"}:
                destination = str(item.get("to", ""))
                if destination not in self.nodes_by_id:
                    raise ValueError(
                        f"traffic item {index}: unknown destination node {destination!r}"
                    )
                if destination == source:
                    raise ValueError(f"traffic item {index}: source and destination are equal")
            try:
                delay = float(item.get("delay_s", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"traffic item {index}: invalid delay_s") from exc
            if delay < 0 or delay < previous_delay:
                raise ValueError("traffic delay_s values must be non-negative and ordered")
            previous_delay = delay

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self, schedule: list[dict[str, Any]]) -> None:
        t0 = time.monotonic()
        for item in schedule:
            if self._stop.is_set():
                break
            delay = float(item.get("delay_s", 0))
            target_time = t0 + delay
            while time.monotonic() < target_time and not self._stop.is_set():
                time.sleep(0.05)
            if self._stop.is_set():
                break
            try:
                self._execute(item)
            except Exception:
                logger.exception("Traffic schedule item failed: %s", item)

    def _execute(self, item: dict[str, Any]) -> None:
        action = item.get("action", "broadcast")
        from_id = str(item.get("from", ""))
        if from_id not in self.nodes_by_id:
            logger.warning("Unknown traffic source node %s", from_id)
            return
        text = str(item.get("text", ""))
        if action == "broadcast":
            self._send_broadcast(from_id, text, bool(item.get("want_ack", False)))
        elif action == "dm":
            to_id = str(item.get("to", ""))
            self._send_dm(from_id, to_id, text, bool(item.get("want_ack", False)))
        elif action == "traceroute":
            to_id = str(item.get("to", ""))
            self._send_traceroute(from_id, to_id)
        else:
            logger.warning("Unknown traffic action %s", action)

    def _send_broadcast(self, from_id: str, text: str, want_ack: bool) -> None:
        try:
            iface = self._iface_for(from_id)
            ensure_mesh_connected(iface, timeout=settings.sim_mesh_connect_timeout_s)
            # Transmisję radiową zgłosi firmware (SIMULATOR_APP) — gateway ją przechwyci.
            iface.sendText(text, wantAck=want_ack)
        except Exception as exc:
            logger.warning("Meshtastic broadcast from %s failed: %s", from_id, exc)

    def _send_dm(self, from_id: str, to_id: str, text: str, want_ack: bool) -> None:
        try:
            iface = self._iface_for(from_id)
            ensure_mesh_connected(iface, timeout=settings.sim_mesh_connect_timeout_s)
            iface.sendText(
                text,
                destinationId=self._dest(to_id),
                wantAck=want_ack,
            )
        except Exception as exc:
            logger.warning("Meshtastic DM from %s failed: %s", from_id, exc)

    def _send_traceroute(self, from_id: str, to_id: str) -> None:
        try:
            iface = self._iface_for(from_id)
            ensure_mesh_connected(iface, timeout=settings.sim_mesh_connect_timeout_s)
            r = mesh_pb2.RouteDiscovery()
            iface.sendData(
                r,
                destinationId=self._dest(to_id),
                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                wantResponse=True,
            )
            return
        except Exception as exc:
            logger.warning("Meshtastic traceroute from %s failed: %s", from_id, exc)
