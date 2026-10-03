"""Jednolity budżet łącza RF używany w precompute (ITM) i w runtime (`RadioGateway`).

Wzór RSSI jest identyczny z `docs/simulation_model.md`: w precompute shadow_db=0,
w runtime shadow_db losowane z N(0, fading_sigma_db).
"""

from __future__ import annotations


def compute_rssi_db(
    tx_power_dbm: float,
    tx_antenna_gain_db: float,
    rx_antenna_gain_db: float,
    path_loss_median_db: float,
    shadow_fading_db: float,
) -> float:
    """Szacunkowy RSSI na wejściu odbiornika [dBm]."""
    return (
        tx_power_dbm
        + tx_antenna_gain_db
        + rx_antenna_gain_db
        - path_loss_median_db
        + shadow_fading_db
    )


def compute_snr_db(rssi_db: float, noise_floor_dbm: float) -> float:
    """SNR względem założonego szumu odbiornika [dB]."""
    return rssi_db - noise_floor_dbm


def is_packet_decoded_rssi(rssi_db: float, receiver_sensitivity_dbm: float) -> bool:
    """Uproszczony model odbioru: dekodowanie przy RSSI >= czułość (patrz dokumentacja modelu)."""
    return rssi_db >= receiver_sensitivity_dbm
