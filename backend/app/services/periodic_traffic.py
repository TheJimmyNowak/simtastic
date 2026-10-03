from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from app.config import settings
from app.models.node import NodeConfig
from app.services.meshtastic_tcp import ensure_mesh_connected, is_mesh_connected

if TYPE_CHECKING:
    from app.services.radio_gateway import RadioGateway

logger = logging.getLogger(__name__)


class PeriodicTraffic:
    """Send periodic broadcast probes so the packet trace has data."""

    def __init__(
        self,
        gateway: RadioGateway,
        nodes: list[NodeConfig],
        interval_s: float = 10.0,
    ) -> None:
        self.gateway = gateway
        self.nodes = nodes
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="periodic-traffic", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        n = 0
        while not self._stop.is_set():
            if self.nodes and self.gateway._running:
                node = self.nodes[n % len(self.nodes)]
                idx = n % len(self.nodes)
                iface = self.gateway._ifaces.get(node.id)
                if iface is not None:
                    try:
                        if not is_mesh_connected(iface):
                            iface = self.gateway.reconnect_node(node.id, idx)
                        ensure_mesh_connected(
                            iface, timeout=settings.sim_mesh_connect_timeout_s
                        )
                        iface.sendText(f"sim-probe-{n}", wantAck=False)
                    except Exception as exc:
                        logger.warning("Periodic traffic from %s failed: %s", node.id, exc)
                n += 1
            self._stop.wait(self.interval_s)
