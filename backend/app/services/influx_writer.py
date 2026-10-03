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
        self._node_count: Optional[int] = None

    def set_run_context(self, node_count: Optional[int]) -> None:
        self._node_count = node_count

    def write_simulation_run(
        self,
        *,
        scenario_id: str,
        experiment_id: str,
        node_count: int,
        preset: str,
        fading_sigma_db: float,
        building_model_enabled: bool,
        fresnel_clearance_ratio: float,
    ) -> None:
        p = (
            Point("simulation_run")
            .tag("scenario_id", scenario_id)
            .tag("experiment_id", experiment_id)
            .tag("node_count", str(node_count))
            .tag("preset", preset)
            .field("started", 1)
            .field("fading_sigma_db", float(fading_sigma_db))
            .field("node_count_field", int(node_count))
            .field("building_model_enabled", building_model_enabled)
            .field("fresnel_clearance_ratio", float(fresnel_clearance_ratio))
        )
        self.enqueue_point(p)
        self._node_count = node_count

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
        self._node_count = None

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
        if tx.hop_start is not None:
            p = p.field("hop_start", tx.hop_start)
        if tx.portnum:
            p = p.tag("portnum", tx.portnum)
        if tx.origin_node:
            p = p.tag("origin_node", tx.origin_node)
        if self._node_count is not None:
            p = p.tag("node_count", str(self._node_count))
        self.enqueue_point(p)

    def write_link_precompute(
        self, *, scenario_id: str, experiment_id: str, matrix: Any
    ) -> None:
        """Persist every directed link, including links pruned from runtime RF."""
        for i, tx_node in enumerate(matrix.node_ids):
            for j, rx_node in enumerate(matrix.node_ids):
                if i == j:
                    continue
                link = matrix.link(i, j)
                point = (
                    Point("link_precompute")
                    .tag("scenario_id", scenario_id)
                    .tag("experiment_id", experiment_id)
                    .tag("tx_node", tx_node)
                    .tag("rx_node", rx_node)
                    .tag("propagation_model", matrix.propagation_model)
                    .field("terrain_loss_db", link.terrain_loss_db)
                    .field("building_diffraction_db", link.building_loss_db)
                    .field("total_path_loss_db", link.path_loss_db)
                    .field("median_rssi_db", link.median_rssi_db)
                    .field("distance_m", link.distance_m)
                    .field("blocked_buildings_count", link.blocked_buildings_count)
                )
                if link.fresnel_clearance_m is not None:
                    point = point.field("fresnel_clearance_m", link.fresnel_clearance_m)
                if link.dominant_building_id:
                    point = point.field("dominant_building_id", link.dominant_building_id)
                if link.building_data_year:
                    point = point.tag("building_data_year", link.building_data_year)
                if self._node_count is not None:
                    point = point.tag("node_count", str(self._node_count))
                self.enqueue_point(point)

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
            .field("terrain_loss_db", att.terrain_loss_db)
            .field("building_diffraction_db", att.building_diffraction_db)
            .field("blocked_buildings_count", att.blocked_buildings_count)
            .field("shadow_db", att.shadow_db)
            .field("rssi_db", att.rssi_db)
            .field("snr_db", att.snr_db)
            .time(att.timestamp)
        )
        p = p.tag("propagation_model", att.propagation_model)
        if att.fresnel_clearance_m is not None:
            p = p.field("fresnel_clearance_m", att.fresnel_clearance_m)
        if att.dominant_building_id:
            p = p.tag("dominant_building_id", att.dominant_building_id)
        if att.building_data_year:
            p = p.tag("building_data_year", att.building_data_year)
        if self._node_count is not None:
            p = p.tag("node_count", str(self._node_count))
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
