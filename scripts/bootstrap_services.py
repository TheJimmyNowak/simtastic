#!/usr/bin/env python3
"""Generate backend service package files (UTF-8).

The checked-in `backend/` tree is the source of truth; this script overwrites
generated paths. See README (section on bootstrap_services) before re-running.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend"

FILES: dict[str, str] = {}


def w(path: str, content: str) -> None:
    FILES[path] = content


w(
    "app/services/scenario_store.py",
    '''
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from app.config import settings
from app.models.scenario import Scenario, ScenarioCreate, ScenarioStatus


class ScenarioStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self._base = base_dir or (settings.data_dir / "scenarios")
        self._base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, Scenario] = {}

    def _path(self, scenario_id: str) -> Path:
        return self._base / scenario_id / "scenario.yaml"

    def _load_file(self, scenario_id: str) -> Optional[Scenario]:
        path = self._path(scenario_id)
        if not path.exists():
            return None
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Scenario.model_validate(raw)

    def _save_file(self, scenario: Scenario) -> None:
        path = self._path(scenario.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                scenario.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def list_all(self) -> list[Scenario]:
        with self._lock:
            out: list[Scenario] = []
            if not self._base.exists():
                return out
            for d in sorted(self._base.iterdir()):
                if not d.is_dir():
                    continue
                s = self._cache.get(d.name) or self._load_file(d.name)
                if s:
                    self._cache[d.name] = s
                    out.append(s)
            out.sort(key=lambda x: x.created_at, reverse=True)
            return out

    def get(self, scenario_id: str) -> Optional[Scenario]:
        with self._lock:
            if scenario_id in self._cache:
                return self._cache[scenario_id]
            s = self._load_file(scenario_id)
            if s:
                self._cache[scenario_id] = s
            return s

    def create(self, body: ScenarioCreate) -> Scenario:
        scenario = Scenario(name=body.name, environment=body.environment, nodes=body.nodes)
        with self._lock:
            self._cache[scenario.id] = scenario
            self._save_file(scenario)
        return scenario

    def update(self, scenario: Scenario) -> Scenario:
        scenario.updated_at = datetime.now(timezone.utc)
        with self._lock:
            self._cache[scenario.id] = scenario
            self._save_file(scenario)
        return scenario

    def delete(self, scenario_id: str) -> bool:
        with self._lock:
            self._cache.pop(scenario_id, None)
            d = self._base / scenario_id
            if not d.exists():
                return False
            for p in sorted(d.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
            for p in sorted(d.rglob("*"), reverse=True):
                if p.is_dir():
                    p.rmdir()
            if d.exists():
                d.rmdir()
            return True

    def set_status(
        self,
        scenario_id: str,
        status: ScenarioStatus,
        error_message: str | None = None,
    ) -> None:
        s = self.get(scenario_id)
        if not s:
            return
        s.status = status
        s.error_message = error_message
        s.updated_at = datetime.now(timezone.utc)
        self.update(s)

    def set_itm_progress(self, scenario_id: str, done: int, total: int) -> None:
        s = self.get(scenario_id)
        if not s:
            return
        s.itm_progress = float(done)
        s.itm_total = total
        s.updated_at = datetime.now(timezone.utc)
        self.update(s)


scenario_store = ScenarioStore()
''',
)

w(
    "app/services/propagation/srtm_profile.py",
    '''
from __future__ import annotations

import math
import struct
from functools import lru_cache
from pathlib import Path

from geopy.distance import geodesic

_SAMPLES = 1201
_TILE_CACHE: dict[tuple[str, str], list[int]] = {}


def _tile_name(lat: float, lon: float) -> str:
    lat_i = math.floor(lat)
    lon_i = math.floor(lon)
    ns = f"N{lat_i:02d}" if lat >= 0 else f"S{abs(lat_i):02d}"
    ew = f"E{lon_i:03d}" if lon >= 0 else f"W{abs(lon_i):03d}"
    return f"{ns}{ew}"


@lru_cache(maxsize=128)
def _read_tile(cache_dir: str, tile: str) -> tuple[int, ...]:
    path = Path(cache_dir) / f"{tile}.hgt"
    if not path.exists():
        raise FileNotFoundError(f"SRTM tile missing: {path}")
    raw = path.read_bytes()
    if len(raw) < _SAMPLES * _SAMPLES * 2:
        raise ValueError(f"Invalid HGT size for {path}")
    return struct.unpack(f">{_SAMPLES * _SAMPLES}h", raw[: _SAMPLES * _SAMPLES * 2])


def _elevation_m(cache_dir: Path, lat: float, lon: float) -> float:
    tile = _tile_name(lat, lon)
    data = _read_tile(str(cache_dir.resolve()), tile)
    lat_sw = math.floor(lat)
    lon_sw = math.floor(lon)
    if lat >= 0:
        row = int(round((lat_sw + 1.0 - lat) * (_SAMPLES - 1)))
    else:
        row = int(round((lat - lat_sw) * (_SAMPLES - 1)))
    col = int(round((lon - lon_sw) * (_SAMPLES - 1)))
    row = max(0, min(_SAMPLES - 1, row))
    col = max(0, min(_SAMPLES - 1, col))
    val = data[row * _SAMPLES + col]
    if val == -32768:
        return 0.0
    return float(val)


def build_terrain_profile(
    cache_dir: Path,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    num_segments: int = 50,
) -> tuple[list[float], float]:
    """Sample terrain AMSL (m) along a geodesic; return elevations and distance_m."""
    if num_segments < 1:
        num_segments = 1
    dist_m = float(geodesic((lat1, lon1), (lat2, lon2)).meters)
    profile: list[float] = []
    for i in range(num_segments + 1):
        f = i / num_segments
        lat = lat1 + f * (lat2 - lat1)
        lon = lon1 + f * (lon2 - lon1)
        profile.append(_elevation_m(cache_dir, lat, lon))
    return profile, dist_m
''',
)

w(
    "app/services/propagation/itm_p2p.py",
    '''
from __future__ import annotations

import math
from typing import Any

import numpy as np
from itmlogic.misc.qerfi import qerfi
from itmlogic.preparatory_subroutines.qlrpfl import qlrpfl
from itmlogic.statistics.avar import avar


def compute_path_loss_db(
    surface_profile_m: list[float],
    distance_km: float,
    agl_tx_m: float,
    agl_rx_m: float,
    fmhz: float,
    *,
    climate_klim: int = 5,
    ipol: int = 0,
    reliability_percent: int = 50,
    confidence_percent: int = 50,
) -> float:
    """Longley-Rice median path loss (dB) via itmlogic qlrpfl."""
    prop: dict[str, Any] = {}
    prop["eps"] = 15
    prop["sgm"] = 0.005
    prop["klim"] = climate_klim
    prop["ens0"] = 314
    qc = [50, 90, 10]
    qr = [1, 10, 50, 90, 99]
    pfl: list[float] = [len(surface_profile_m) - 1, 0.0]
    pfl.extend(float(x) for x in surface_profile_m)
    zsys = 0
    prop["lvar"] = 5
    prop["gma"] = 157e-9
    db = 8.685890
    nr, nc = len(qr), len(qc)
    dkm = distance_km
    xkm = 0.0
    if dkm <= 0:
        dkm = xkm * pfl[0]
    if xkm <= 0:
        xkm = dkm / max(pfl[0], 1)
    pfl[1] = dkm * 1000.0 / max(pfl[0], 1)
    prop["pfl"] = pfl
    prop["kwx"] = 0
    prop["fmhz"] = fmhz
    prop["hg"] = [float(agl_tx_m), float(agl_rx_m)]
    prop["ipol"] = ipol
    prop["d"] = dkm
    prop["wn"] = prop["fmhz"] / 47.7
    prop["ens"] = prop["ens0"]
    if zsys != 0:
        prop["ens"] = prop["ens"] * math.exp(-zsys / 9460.0)
    prop["gme"] = prop["gma"] * (1 - 0.04665 * math.exp(prop["ens"] / 179.3))
    zq = complex(prop["eps"], 376.62 * prop["sgm"] / prop["wn"])
    prop["zgnd"] = np.sqrt(zq - 1)
    if prop["ipol"] != 0:
        prop["zgnd"] = prop["zgnd"] / zq
    prop["klimx"] = 0
    prop["mdvarx"] = 11
    zr = qerfi([x / 100 for x in qr])
    zc = qerfi([x / 100 for x in qc])
    prop = qlrpfl(prop)
    fs = db * np.log(2 * prop["wn"] * prop["dist"])
    chosen: float | None = None
    for jr in range(nr):
        for jc in range(nc):
            avar1, prop = avar(zr[jr], 0, zc[jc], prop)
            loss = float(fs + avar1)
            if qr[jr] == reliability_percent and qc[jc] == confidence_percent:
                chosen = loss
    if chosen is None:
        avar1, prop = avar(zr[2], 0, zc[0], prop)
        chosen = float(fs + avar1)
    return chosen
''',
)

w(
    "app/services/propagation/link_matrix.py",
    '''
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import settings


@dataclass
class LinkMatrix:
    node_ids: list[str]
    path_loss_median_db: np.ndarray
    median_rssi_db: np.ndarray
    distance_m: np.ndarray
    connectivity_neighbors: dict[str, list[str]]

    @property
    def n(self) -> int:
        return len(self.node_ids)

    def index(self, node_id: str) -> int:
        return self.node_ids.index(node_id)


def matrix_path(scenario_id: str) -> Path:
    return settings.data_dir / "scenarios" / scenario_id / "link_matrix.npz"


def save_link_matrix(scenario_id: str, matrix: LinkMatrix) -> Path:
    path = matrix_path(scenario_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        node_ids=np.array(matrix.node_ids, dtype=object),
        path_loss_median_db=matrix.path_loss_median_db,
        median_rssi_db=matrix.median_rssi_db,
        distance_m=matrix.distance_m,
        connectivity_neighbors=np.array(matrix.connectivity_neighbors, dtype=object),
    )
    return path


def load_link_matrix(scenario_id: str) -> Optional[LinkMatrix]:
    path = matrix_path(scenario_id)
    if not path.exists():
        return None
    data = np.load(path, allow_pickle=True)
    neighbors = data["connectivity_neighbors"].item()
    if not isinstance(neighbors, dict):
        neighbors = dict(neighbors)
    return LinkMatrix(
        node_ids=list(data["node_ids"]),
        path_loss_median_db=data["path_loss_median_db"],
        median_rssi_db=data["median_rssi_db"],
        distance_m=data["distance_m"],
        connectivity_neighbors=neighbors,
    )


def build_connectivity_map(
    node_ids: list[str],
    median_rssi_db: np.ndarray,
    threshold_dbm: float,
) -> dict[str, list[str]]:
    """Sparse neighbor list per node where median RSSI meets threshold."""
    n = len(node_ids)
    neighbors: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            val = float(median_rssi_db[i, j])
            if val >= threshold_dbm:
                neighbors[node_ids[i]].append(node_ids[j])
    return neighbors
''',
)

w(
    "app/services/propagation/itm_precompute.py",
    '''
from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from app.config import settings
from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig
from app.services.propagation.itm_p2p import compute_path_loss_db
from app.services.propagation.link_matrix import (
    LinkMatrix,
    build_connectivity_map,
    save_link_matrix,
)
from app.services.propagation.srtm_profile import build_terrain_profile
from app.services.scenario_store import scenario_store

logger = logging.getLogger(__name__)


def _pair_loss(
    srtm_dir: str,
    env_dict: dict,
    tx: dict,
    rx: dict,
) -> tuple[str, str, float, float, float]:
    env = EnvironmentConfig.model_validate(env_dict)
    tx_n = NodeConfig.model_validate(tx)
    rx_n = NodeConfig.model_validate(rx)
    lora = env.resolve_lora()
    profile, dist_m = build_terrain_profile(
        Path(srtm_dir),
        tx_n.lat,
        tx_n.lon,
        rx_n.lat,
        rx_n.lon,
    )
    pl = compute_path_loss_db(
        profile,
        dist_m / 1000.0,
        tx_n.agl_m,
        rx_n.agl_m,
        lora.frequency_mhz,
        climate_klim=env.itm_climate,
        ipol=env.itm_polarization,
    )
    rssi = (
        lora.tx_power_dbm
        + tx_n.antenna_gain_tx_db
        + rx_n.antenna_gain_rx_db
        - pl
    )
    return tx_n.id, rx_n.id, pl, rssi, dist_m


def precompute_link_matrix(
    scenario_id: str,
    nodes: list[NodeConfig],
    env: EnvironmentConfig,
    *,
    on_progress: object | None = None,
) -> LinkMatrix:
    n = len(nodes)
    node_ids = [nd.id for nd in nodes]
    idx = {nd.id: i for i, nd in enumerate(nodes)}
    path_loss = np.full((n, n), np.nan, dtype=np.float64)
    median_rssi = np.full((n, n), -999.0, dtype=np.float64)
    distance_m = np.zeros((n, n), dtype=np.float64)
    np.fill_diagonal(path_loss, 0.0)
    np.fill_diagonal(median_rssi, -999.0)

    srtm_dir = str(settings.srtm_cache_dir.resolve())
    env_dict = env.model_dump()
    node_dicts = [nd.model_dump() for nd in nodes]
    tasks: list[tuple[int, int]] = [(i, j) for i in range(n) for j in range(n) if i != j]
    total = len(tasks)
    done = 0

    workers = max(1, min(total, (__import__("os").cpu_count() or 4) - 1))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _pair_loss,
                srtm_dir,
                env_dict,
                node_dicts[i],
                node_dicts[j],
            ): (i, j)
            for i, j in tasks
        }
        for fut in as_completed(futures):
            i, j = futures[fut]
            tx_id, rx_id, pl, rssi, dist = fut.result()
            ii, jj = idx[tx_id], idx[rx_id]
            path_loss[ii, jj] = pl
            median_rssi[ii, jj] = rssi
            distance_m[ii, jj] = dist
            done += 1
            if on_progress:
                on_progress(done, total)

    neighbors = build_connectivity_map(
        node_ids, median_rssi, env.connectivity_threshold_dbm
    )
    matrix = LinkMatrix(
        node_ids=node_ids,
        path_loss_median_db=path_loss,
        median_rssi_db=median_rssi,
        distance_m=distance_m,
        connectivity_neighbors=neighbors,
    )
    save_link_matrix(scenario_id, matrix)
    return matrix


def run_precompute(scenario_id: str) -> None:
    scenario = scenario_store.get(scenario_id)
    if not scenario:
        raise ValueError(f"Scenario {scenario_id} not found")
    if len(scenario.nodes) < 2:
        raise ValueError("At least two nodes required for ITM precompute")

    def progress(done: int, total: int) -> None:
        scenario_store.set_itm_progress(scenario_id, done, total)

    try:
        precompute_link_matrix(
            scenario_id,
            scenario.nodes,
            scenario.environment,
            on_progress=progress,
        )
        from app.models.scenario import ScenarioStatus

        scenario_store.set_status(scenario_id, ScenarioStatus.LINKS_READY)
    except Exception as exc:
        logger.exception("ITM precompute failed")
        from app.models.scenario import ScenarioStatus

        scenario_store.set_status(scenario_id, ScenarioStatus.ERROR, str(exc))
        raise
''',
)

w(
    "app/services/propagation/__init__.py",
    '''
from app.services.propagation.itm_p2p import compute_path_loss_db
from app.services.propagation.itm_precompute import precompute_link_matrix, run_precompute
from app.services.propagation.link_matrix import (
    LinkMatrix,
    build_connectivity_map,
    load_link_matrix,
    save_link_matrix,
)
from app.services.propagation.srtm_profile import build_terrain_profile

__all__ = [
    "LinkMatrix",
    "build_connectivity_map",
    "build_terrain_profile",
    "compute_path_loss_db",
    "load_link_matrix",
    "precompute_link_matrix",
    "run_precompute",
    "save_link_matrix",
]
''',
)

w(
    "app/services/packet_trace.py",
    '''
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
        self._by_packet: dict[int, list[LinkAttempt]] = {}
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
            self._by_packet.setdefault(event.packet_id, []).append(event)
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

    def list_attempts_for_packet(self, packet_id: int) -> list[LinkAttempt]:
        with self._lock:
            return list(self._by_packet.get(packet_id, []))

    def clear(self) -> None:
        with self._lock:
            self._tx_buf.clear()
            self._attempt_buf.clear()
            self._rx_buf.clear()
            self._by_packet.clear()


packet_trace_service = PacketTraceService()
''',
)

w(
    "app/services/influx_writer.py",
    '''
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Optional

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from app.config import settings
from app.models.packet_trace import LinkAttempt, PacketOtaTx

logger = logging.getLogger(__name__)


class InfluxWriter:
    def __init__(self) -> None:
        self._q: queue.Queue[Point] = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[InfluxDBClient] = None
        self._write_api = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        try:
            self._client = InfluxDBClient(
                url=settings.influx_url,
                token=settings.influx_token,
                org=settings.influx_org,
            )
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        except Exception as exc:
            logger.warning("InfluxDB unavailable: %s", exc)
            self._client = None
        self._thread = threading.Thread(target=self._run, name="influx-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        if self._client:
            self._client.close()
        self._client = None
        self._write_api = None

    def enqueue_point(self, point: Point) -> None:
        try:
            self._q.put_nowait(point)
        except queue.Full:
            logger.warning("Influx queue full, dropping point")

    def write_packet_tx(self, tx: PacketOtaTx) -> None:
        p = (
            Point("packet_ota_tx")
            .tag("scenario_id", tx.scenario_id)
            .tag("experiment_id", tx.experiment_id)
            .tag("tx_node", tx.tx_node)
            .field("packet_id", tx.packet_id)
            .field("payload_len", tx.payload_len)
            .field("is_broadcast", tx.is_broadcast)
            .field("is_rebroadcast", tx.is_rebroadcast)
            .time(tx.timestamp)
        )
        if tx.hop_limit is not None:
            p = p.field("hop_limit", tx.hop_limit)
        if tx.portnum:
            p = p.tag("portnum", tx.portnum)
        self.enqueue_point(p)

    def write_link_attempt(self, att: LinkAttempt) -> None:
        p = (
            Point("link_attempt")
            .tag("scenario_id", att.scenario_id)
            .tag("experiment_id", att.experiment_id)
            .tag("tx_node", att.tx_node)
            .tag("rx_node", att.rx_node)
            .tag("delivered", str(att.delivered))
            .field("packet_id", att.packet_id)
            .field("path_loss_median_db", att.path_loss_median_db)
            .field("shadow_db", att.shadow_db)
            .field("rssi_db", att.rssi_db)
            .field("snr_db", att.snr_db)
            .time(att.timestamp)
        )
        self.enqueue_point(p)

    def _run(self) -> None:
        batch: list[Point] = []
        while not self._stop.is_set():
            try:
                batch.append(self._q.get(timeout=0.5))
            except queue.Empty:
                pass
            while len(batch) < 200:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            if batch and self._write_api:
                try:
                    self._write_api.write(
                        bucket=settings.influx_bucket,
                        org=settings.influx_org,
                        record=batch,
                    )
                except Exception as exc:
                    logger.warning("Influx write failed: %s", exc)
            batch.clear()
        self._flush_remaining()

    def _flush_remaining(self) -> None:
        if not self._write_api:
            return
        rest: list[Point] = []
        while True:
            try:
                rest.append(self._q.get_nowait())
            except queue.Empty:
                break
        if rest:
            try:
                self._write_api.write(
                    bucket=settings.influx_bucket,
                    org=settings.influx_org,
                    record=rest,
                )
            except Exception as exc:
                logger.warning("Influx flush failed: %s", exc)


influx_writer = InfluxWriter()
''',
)

w(
    "app/services/node_provisioner.py",
    '''
from __future__ import annotations

import logging
import time

from meshtastic import admin_pb2
from meshtastic.tcp_interface import TCPInterface

from app.config import settings
from app.models.node import NodeConfig, NodeRole

logger = logging.getLogger(__name__)

_ROLE_MAP = {
    NodeRole.CLIENT: "CLIENT",
    NodeRole.ROUTER: "ROUTER",
    NodeRole.REPEATER: "REPEATER",
    NodeRole.CLIENT_MUTE: "CLIENT_MUTE",
}


def provision_node(iface: TCPInterface, node: NodeConfig, index: int) -> None:
    """Configure meshtasticd: hop_limit, role, GPS, disable UDP."""
    local = iface.localNode
    if local is None:
        raise RuntimeError("No local node on interface")

    p = admin_pb2.AdminMessage()
    p.set_owner.long_name = node.name or node.id
    p.set_owner.short_name = (node.name or node.id)[:4]
    local._sendAdmin(p)
    time.sleep(0.05)

    network_cfg = local.localConfig.network
    network_cfg.enabled_protocols = 0
    p = admin_pb2.AdminMessage()
    p.set_config.network.CopyFrom(network_cfg)
    local._sendAdmin(p)
    time.sleep(0.05)

    lora_cfg = local.localConfig.lora
    lora_cfg.hop_limit = int(node.hop_limit)
    p = admin_pb2.AdminMessage()
    p.set_config.lora.CopyFrom(lora_cfg)
    local._sendAdmin(p)
    time.sleep(0.05)

    device_cfg = local.localConfig.device
    device_cfg.role = _ROLE_MAP.get(node.role, "CLIENT")
    p = admin_pb2.AdminMessage()
    p.set_config.device.CopyFrom(device_cfg)
    local._sendAdmin(p)
    time.sleep(0.05)

    iface.sendPosition(node.lat, node.lon, int(node.agl_m))
    logger.info("Provisioned node %s on port %s", node.id, node.tcp_port)
''',
)

w(
    "app/services/shard_manager.py",
    '''
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.models.node import NodeConfig

logger = logging.getLogger(__name__)

MESHTASTICD = "/usr/bin/meshtasticd"


@dataclass
class ShardInfo:
    shard_id: int
    container_name: str
    node_indices: list[int] = field(default_factory=list)


class ShardManager:
    def __init__(self, scenario_id: str, shard_size: int) -> None:
        self.scenario_id = scenario_id
        self.shard_size = max(1, shard_size)
        self.shards: list[ShardInfo] = []
        self._data_root = settings.data_dir / "scenarios" / scenario_id / "nodes"

    def _container_name(self, shard_id: int) -> str:
        return f"sim-{self.scenario_id[:8]}-shard-{shard_id}"

    def start(self, nodes: list[NodeConfig]) -> None:
        if settings.dry_run:
            logger.info("dry_run: skip Docker shard start")
            return
        self._data_root.mkdir(parents=True, exist_ok=True)
        num_shards = (len(nodes) + self.shard_size - 1) // self.shard_size
        for shard_id in range(num_shards):
            start = shard_id * self.shard_size
            chunk = nodes[start : start + self.shard_size]
            indices = list(range(start, start + len(chunk)))
            info = ShardInfo(
                shard_id=shard_id,
                container_name=self._container_name(shard_id),
                node_indices=indices,
            )
            self.shards.append(info)
            self._start_shard(info, chunk, start)

    def _start_shard(self, info: ShardInfo, chunk: list[NodeConfig], base_index: int) -> None:
        ports: list[str] = []
        for i, node in enumerate(chunk):
            global_i = base_index + i
            port = settings.tcp_port_base + global_i
            node.tcp_port = port
            data_dir = self._data_root / f"node_{global_i}"
            data_dir.mkdir(parents=True, exist_ok=True)
            ports.extend(["-p", f"{port}:{port}/tcp"])

        subprocess.run(
            ["docker", "rm", "-f", info.container_name],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                info.container_name,
                *ports,
                settings.meshtastic_image,
                "sh",
                "-c",
                "while true; do sleep 3600; done",
            ],
            check=True,
        )
        time.sleep(0.5)
        for i, node in enumerate(chunk):
            global_i = base_index + i
            port = settings.tcp_port_base + global_i
            hw_id = settings.hw_id_offset + global_i
            data_dir = f"/data/node_{global_i}"
            cmd = (
                f"{MESHTASTICD} -e -s -d {data_dir} "
                f"--hwid=0x{hw_id:x} --port={port} >>/tmp/out_{global_i}.log 2>&1"
            )
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-d",
                    info.container_name,
                    "sh",
                    "-c",
                    cmd,
                ],
                check=True,
            )
            logger.info("Started meshtasticd %s in %s", node.id, info.container_name)

    def stop(self) -> None:
        if settings.dry_run:
            return
        for info in self.shards:
            subprocess.run(
                ["docker", "rm", "-f", info.container_name],
                capture_output=True,
                check=False,
            )
        self.shards.clear()

    def status(self) -> list[dict]:
        out: list[dict] = []
        for info in self.shards:
            proc = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Status}}",
                    info.container_name,
                ],
                capture_output=True,
                text=True,
            )
            state = proc.stdout.strip() if proc.returncode == 0 else "missing"
            out.append(
                {
                    "shard_id": info.shard_id,
                    "container": info.container_name,
                    "state": state,
                    "nodes": info.node_indices,
                }
            )
        return out
''',
)

w(
    "app/services/radio_gateway.py",
    '''
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Optional

from meshtastic import mesh_pb2, portnums_pb2
from meshtastic.tcp_interface import TCPInterface
from pubsub import pub

from app.config import settings
from app.models.node import NodeConfig
from app.models.packet_trace import LinkAttempt, PacketOtaRx, PacketOtaTx
from app.services.influx_writer import influx_writer
from app.services.packet_trace import packet_trace_service
from app.services.propagation.link_matrix import LinkMatrix

logger = logging.getLogger(__name__)

HW_ID_OFFSET = settings.hw_id_offset


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

    def set_lora(self, lora) -> None:
        self._lora = lora

    def connect(self) -> None:
        for i, node in enumerate(self.nodes):
            if node.tcp_port is None:
                node.tcp_port = settings.tcp_port_base + i
            host = settings.meshtastic_tcp_host
            iface = TCPInterface(
                hostname=host,
                portNumber=node.tcp_port,
                connectNow=True,
                noProto=False,
            )
            self._ifaces[node.id] = iface
            hw = HW_ID_OFFSET + i
            self._id_by_hw[hw] = node.id
            time.sleep(0.1)
        pub.subscribe(self._on_simulator_packet, "meshtastic.receive.simulator")

    def disconnect(self) -> None:
        try:
            pub.unsubscribe(self._on_simulator_packet, "meshtastic.receive.simulator")
        except Exception:
            pass
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

    def _on_simulator_packet(self, packet: dict[str, Any], interface: TCPInterface) -> None:
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
        self._handle_transmission(tx_id, packet)

    def _handle_transmission(self, tx_id: str, packet: dict[str, Any]) -> None:
        decoded = packet.get("decoded", {})
        sim = decoded.get("simulator", {})
        portnum = sim.get("portnum") or decoded.get("portnum", "UNKNOWN")
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
            delivered = snr >= lora.sensitivity_dbm

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

        mesh_packet = self._packet_from_packet(
            packet, data, portnums_pb2.PortNum.SIMULATOR_APP
        )
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
''',
)

w(
    "app/services/traffic_generator.py",
    '''
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

import yaml
from meshtastic import mesh_pb2, portnums_pb2
from meshtastic.tcp_interface import TCPInterface

from app.config import settings

logger = logging.getLogger(__name__)
HW_ID_OFFSET = settings.hw_id_offset


class TrafficGenerator:
    def __init__(
        self,
        schedule_path: Path,
        ifaces: dict[str, TCPInterface],
        nodes_by_id: dict[str, int],
    ) -> None:
        self.schedule_path = schedule_path
        self.ifaces = ifaces
        self.nodes_by_id = nodes_by_id
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _hw_id(self, node_id: str) -> int:
        idx = self.nodes_by_id[node_id]
        return HW_ID_OFFSET + idx

    def _dest(self, node_id: str) -> str:
        val = hex(self._hw_id(node_id)).strip("0x")
        return "!" + "0" * (8 - len(val)) + val

    def start(self) -> None:
        raw = yaml.safe_load(self.schedule_path.read_text(encoding="utf-8"))
        schedule = raw.get("schedule", []) if isinstance(raw, dict) else []
        self._thread = threading.Thread(
            target=self._run,
            args=(schedule,),
            name="traffic-generator",
            daemon=True,
        )
        self._thread.start()

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
            self._execute(item)

    def _execute(self, item: dict[str, Any]) -> None:
        action = item.get("action", "broadcast")
        from_id = str(item.get("from", ""))
        iface = self.ifaces.get(from_id)
        if iface is None:
            logger.warning("Unknown traffic source node %s", from_id)
            return
        if action == "broadcast":
            text = str(item.get("text", ""))
            want_ack = bool(item.get("want_ack", False))
            iface.sendText(text, wantAck=want_ack)
        elif action == "dm":
            to_id = str(item.get("to", ""))
            text = str(item.get("text", ""))
            iface.sendText(text, destinationId=self._dest(to_id), wantAck=bool(item.get("want_ack", False)))
        elif action == "traceroute":
            to_id = str(item.get("to", ""))
            r = mesh_pb2.RouteDiscovery()
            iface.sendData(
                r,
                destinationId=self._dest(to_id),
                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                wantResponse=True,
            )
        else:
            logger.warning("Unknown traffic action %s", action)
''',
)

w(
    "app/services/simulation_manager.py",
    '''
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from app.config import settings
from app.models.scenario import ScenarioStatus
from app.services.influx_writer import influx_writer
from app.services.node_provisioner import provision_node
from app.services.packet_trace import packet_trace_service
from app.services.propagation.itm_precompute import run_precompute
from app.services.propagation.link_matrix import load_link_matrix
from app.services.radio_gateway import RadioGateway
from app.services.scenario_store import scenario_store
from app.services.shard_manager import ShardManager
from app.services.traffic_generator import TrafficGenerator
from meshtastic.tcp_interface import TCPInterface

logger = logging.getLogger(__name__)


class SimulationManager:
    def __init__(self) -> None:
        self.active_scenario_id: Optional[str] = None
        self._gateway: Optional[RadioGateway] = None
        self._shards: Optional[ShardManager] = None
        self._traffic: Optional[TrafficGenerator] = None
        self._precompute_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def precompute_links(self, scenario_id: str) -> None:
        scenario = scenario_store.get(scenario_id)
        if not scenario:
            raise ValueError("Scenario not found")
        if self._precompute_thread and self._precompute_thread.is_alive():
            raise RuntimeError("ITM precompute already running")
        scenario_store.set_status(scenario_id, ScenarioStatus.STARTING)

        def worker() -> None:
            try:
                run_precompute(scenario_id)
            except Exception as exc:
                logger.exception("precompute failed: %s", exc)

        self._precompute_thread = threading.Thread(target=worker, daemon=True)
        self._precompute_thread.start()

    def start(self, scenario_id: str, traffic_path: Path | None = None) -> None:
        with self._lock:
            if self.active_scenario_id:
                raise RuntimeError("Another simulation is already running")
            scenario = scenario_store.get(scenario_id)
            if not scenario:
                raise ValueError("Scenario not found")
            matrix = load_link_matrix(scenario_id)
            if matrix is None:
                raise RuntimeError("Link matrix not computed; run precompute-links first")
            if len(scenario.nodes) < 2:
                raise ValueError("At least two nodes required")

            scenario_store.set_status(scenario_id, ScenarioStatus.STARTING)
            env = scenario.environment
            lora = env.resolve_lora()

            packet_trace_service.clear()
            influx_writer.start()

            shards = ShardManager(scenario_id, env.shard_size)
            shards.start(scenario.nodes)
            self._shards = shards

            if not settings.dry_run:
                time.sleep(2.0)
                for i, node in enumerate(scenario.nodes):
                    port = node.tcp_port or (settings.tcp_port_base + i)
                    iface = TCPInterface(
                        hostname=settings.meshtastic_tcp_host,
                        portNumber=port,
                        connectNow=True,
                    )
                    try:
                        provision_node(iface, node, i)
                    finally:
                        iface.close()

            gateway = RadioGateway(
                scenario_id,
                env.experiment_id,
                scenario.nodes,
                matrix,
                env.fading_sigma_db,
            )
            gateway.set_lora(lora)
            gateway.connect()
            gateway.start()
            self._gateway = gateway

            if traffic_path and traffic_path.exists():
                ifaces = gateway._ifaces
                nodes_by_id = {n.id: i for i, n in enumerate(scenario.nodes)}
                self._traffic = TrafficGenerator(traffic_path, ifaces, nodes_by_id)
                self._traffic.start()

            self.active_scenario_id = scenario_id
            scenario_store.set_status(scenario_id, ScenarioStatus.RUNNING)

    def stop(self) -> None:
        with self._lock:
            sid = self.active_scenario_id
            if self._traffic:
                self._traffic.stop()
                self._traffic = None
            if self._gateway:
                self._gateway.stop()
                self._gateway.disconnect()
                self._gateway = None
            if self._shards:
                self._shards.stop()
                self._shards = None
            influx_writer.stop()
            if sid:
                scenario_store.set_status(sid, ScenarioStatus.STOPPED)
            self.active_scenario_id = None

    def status(self) -> dict:
        return {
            "active_scenario_id": self.active_scenario_id,
            "gateway_running": self._gateway is not None,
            "shards": self._shards.status() if self._shards else [],
            "precompute_running": bool(
                self._precompute_thread and self._precompute_thread.is_alive()
            ),
        }


simulation_manager = SimulationManager()
''',
)

if __name__ == "__main__":
    for rel, content in FILES.items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.strip() + "\n", encoding="utf-8")
        print("wrote", rel)
    print(f"Done: {len(FILES)} files")
