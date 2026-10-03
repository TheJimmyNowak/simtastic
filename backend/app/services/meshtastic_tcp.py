from __future__ import annotations

import logging
import socket
import time

from meshtastic.mesh_interface import MeshInterface
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
    mesh_timeout = max(60, int(timeout))
    while time.monotonic() < deadline:
        try:
            return TCPInterface(
                hostname=host,
                portNumber=port,
                connectNow=True,
                timeout=mesh_timeout,
            )
        except (
            BrokenPipeError,
            ConnectionResetError,
            OSError,
            MeshInterface.MeshInterfaceError,
        ) as exc:
            last_exc = exc
            logger.debug("TCP connect %s:%s retry: %s", host, port, exc)
            time.sleep(0.5)
    raise TimeoutError(f"Could not connect to {host}:{port}: {last_exc}") from last_exc


def replace_tcp_interface(
    iface: TCPInterface | None,
    host: str,
    port: int,
    timeout: float = 90.0,
) -> TCPInterface:
    """Close a stale interface and open a fresh Meshtastic TCP session."""
    if iface is not None:
        try:
            iface.close()
        except Exception:
            pass
    new_iface = open_tcp_interface(host, port, timeout=timeout)
    wait_for_mesh_connected(new_iface, timeout=timeout)
    return new_iface


def wait_for_mesh_connected(iface: TCPInterface, timeout: float = 90.0) -> None:
    """Block until the meshtastic client finishes config sync (myInfo / node DB)."""
    if iface.noProto:
        return
    if iface.isConnected.wait(timeout=timeout):
        if iface.failure:
            raise iface.failure
        return
    raise TimeoutError(
        f"Meshtastic config handshake not complete after {timeout:.0f}s"
    )


def is_mesh_connected(iface: TCPInterface) -> bool:
    if iface.noProto or iface.failure:
        return False
    if not iface.isConnected.is_set():
        return False
    if iface.socket is None:
        return False
    return iface.localNode is not None and iface.myInfo is not None


def ensure_mesh_connected(iface: TCPInterface, timeout: float = 90.0) -> None:
    """Wait for connection; raises if the session never becomes ready."""
    if iface.noProto:
        return
    if is_mesh_connected(iface):
        return
    try:
        iface._waitConnected(timeout=min(timeout, 30.0))
    except MeshInterface.MeshInterfaceError:
        wait_for_mesh_connected(iface, timeout=timeout)
    if not is_mesh_connected(iface):
        raise TimeoutError("Meshtastic session not ready after handshake")
