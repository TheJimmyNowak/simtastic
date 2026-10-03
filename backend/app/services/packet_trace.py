from __future__ import annotations

import threading
from collections import deque
from typing import Callable, Deque, Union

from app.models.packet_trace import LinkAttempt, PacketOtaRx, PacketOtaTx

TraceEvent = Union[PacketOtaTx, LinkAttempt, PacketOtaRx]


class PacketTraceService:
    def __init__(self, tx_capacity: int = 500, attempt_capacity: int = 5000) -> None:
        self._tx_buf: Deque[PacketOtaTx] = deque(maxlen=tx_capacity)
        self._attempt_buf: Deque[LinkAttempt] = deque(maxlen=attempt_capacity)
        self._rx_buf: Deque[PacketOtaRx] = deque(maxlen=attempt_capacity)
        self._by_packet: dict[tuple[str, int], list[LinkAttempt]] = {}
        self._subscribers: list[Callable[[TraceEvent], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[TraceEvent], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[TraceEvent], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _emit(self, event: TraceEvent) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception:
                pass

    def record_tx(self, event: PacketOtaTx) -> None:
        with self._lock:
            self._tx_buf.append(event)
        self._emit(event)

    def record_link_attempt(self, event: LinkAttempt) -> None:
        with self._lock:
            self._attempt_buf.append(event)
            key = (event.scenario_id, event.packet_id)
            self._by_packet.setdefault(key, []).append(event)
        self._emit(event)

    def record_rx(self, event: PacketOtaRx) -> None:
        with self._lock:
            self._rx_buf.append(event)
        self._emit(event)

    def list_recent_tx(self, limit: int = 100) -> list[PacketOtaTx]:
        with self._lock:
            items = list(self._tx_buf)
        return items[-limit:]

    def list_recent_attempts(self, limit: int = 200) -> list[LinkAttempt]:
        with self._lock:
            items = list(self._attempt_buf)
        return items[-limit:]

    def list_recent_rx(self, limit: int = 200) -> list[PacketOtaRx]:
        with self._lock:
            items = list(self._rx_buf)
        return items[-limit:]

    def list_scenario_tx(self, scenario_id: str, limit: int = 100) -> list[PacketOtaTx]:
        with self._lock:
            items = [item for item in self._tx_buf if item.scenario_id == scenario_id]
        return items[-limit:]

    def list_scenario_attempts(
        self, scenario_id: str, limit: int = 200
    ) -> list[LinkAttempt]:
        with self._lock:
            items = [item for item in self._attempt_buf if item.scenario_id == scenario_id]
        return items[-limit:]

    def list_scenario_rx(self, scenario_id: str, limit: int = 200) -> list[PacketOtaRx]:
        with self._lock:
            items = [item for item in self._rx_buf if item.scenario_id == scenario_id]
        return items[-limit:]

    def list_attempts_for_packet(
        self, packet_id: int, scenario_id: str | None = None
    ) -> list[LinkAttempt]:
        with self._lock:
            if scenario_id is not None:
                return list(self._by_packet.get((scenario_id, packet_id), []))
            return [
                item
                for (sid, pid), items in self._by_packet.items()
                if pid == packet_id
                for item in items
            ]

    def clear(self) -> None:
        with self._lock:
            self._tx_buf.clear()
            self._attempt_buf.clear()
            self._rx_buf.clear()
            self._by_packet.clear()


packet_trace_service = PacketTraceService()
