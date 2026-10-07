from __future__ import annotations

import numpy as np
import pytest

from app.services.propagation.link_matrix import build_connectivity_map
from app.services.rf_link_model import (
    compute_rssi_db,
    LORA_SNR_MAX_DB,
    compute_snr_db,
    is_packet_decoded_rssi,
)


def test_compute_rssi_matches_manual_budget() -> None:
    rssi = compute_rssi_db(
        tx_power_dbm=27.0,
        tx_antenna_gain_db=2.0,
        rx_antenna_gain_db=2.0,
        path_loss_median_db=100.0,
        shadow_fading_db=-3.0,
    )
    assert rssi == pytest.approx(-72.0)


def test_delivered_uses_rssi_threshold() -> None:
    assert is_packet_decoded_rssi(-120.0, -134.0) is True
    assert is_packet_decoded_rssi(-135.0, -134.0) is False


def test_snr() -> None:
    assert compute_snr_db(-110.0, -120.0) == pytest.approx(10.0)
    assert compute_snr_db(-125.0, -120.0) == pytest.approx(-5.0)


def test_snr_saturates_like_lora_receiver() -> None:
    # Bliskie węzły: RSSI -49 dBm dałoby SNR 71 dB, a firmware wylicza z niego
    # opóźnienie retransmisji — nienasycona wartość blokowała kolejkę nadawczą.
    assert compute_snr_db(-49.0, -120.0) == pytest.approx(LORA_SNR_MAX_DB)


def test_connectivity_neighbors_respect_threshold() -> None:
    node_ids = ["a", "b", "c"]
    median = np.array(
        [
            [-999.0, -100.0, -150.0],
            [-95.0, -999.0, -130.0],
            [-120.0, -125.0, -999.0],
        ],
        dtype=np.float64,
    )
    neigh = build_connectivity_map(node_ids, median, -140.0)
    assert "b" in neigh["a"]
    assert "c" not in neigh["a"]
    for nid in node_ids:
        assert nid not in neigh[nid]


def test_gateway_does_not_fallback_to_all_nodes_without_neighbors() -> None:
    from app.models.environment import EnvironmentConfig
    from app.models.node import NodeConfig
    from app.services.packet_trace import packet_trace_service
    from app.services.propagation.link_matrix import LinkMatrix
    from app.services.radio_gateway import RadioGateway

    nodes = [
        NodeConfig(id="a", lat=52.0, lon=21.0),
        NodeConfig(id="b", lat=52.1, lon=21.1),
    ]
    matrix = LinkMatrix(
        node_ids=["a", "b"],
        path_loss_median_db=np.full((2, 2), 100.0),
        median_rssi_db=np.full((2, 2), -150.0),
        distance_m=np.full((2, 2), 1000.0),
        connectivity_neighbors={"a": [], "b": []},
    )
    packet_trace_service.clear()
    gateway = RadioGateway("scenario", "experiment", nodes, matrix, 0.0)
    gateway.set_lora(EnvironmentConfig().resolve_lora())
    iface = object()
    gateway.attach_node("a", iface, 0)
    gateway.start()
    packet = {"id": 1, "from": 16, "to": 0xFFFFFFFF, "decoded": {"portnum": "SIMULATOR_APP", "payload": b""}}
    gateway._on_mesh_packet(packet, iface)
    assert len(packet_trace_service.list_recent_tx()) == 1
    assert packet_trace_service.list_recent_attempts() == []


def test_link_details_handle_missing_building_data() -> None:
    from app.services.propagation.link_matrix import LinkMatrix

    matrix = LinkMatrix(
        node_ids=["a", "b"],
        path_loss_median_db=np.array([[0, 120.0], [121.0, 0]]),
        median_rssi_db=np.array([[-999, -89.0], [-90.0, -999]]),
        distance_m=np.full((2, 2), 500.0),
        connectivity_neighbors={"a": ["b"], "b": ["a"]},
        fresnel_clearance_m=np.full((2, 2), np.inf),
    )
    link = matrix.link(matrix.index("b"), matrix.index("a"))
    assert (link.path_loss_db, link.terrain_loss_db, link.building_loss_db) == (121.0, 121.0, 0.0)
    assert link.fresnel_clearance_m is None and link.dominant_building_id is None
    assert matrix.propagation_model == "itm"
