from __future__ import annotations

import math
from typing import Any

import numpy as np
from itmlogic.misc.qerfi import qerfi
from itmlogic.preparatory_subroutines.qlrpfl import qlrpfl
from itmlogic.statistics.avar import avar

_DB_PER_NEPER = 8.685890
# Węzły o tych samych współrzędnych (np. pozycje skwantowane przez precision_bits) dają
# dystans 0, przy którym itmlogic dzieli przez zero. Poniżej 1 m ITM i tak sprowadza się
# do straty wolnej przestrzeni, która dla d < 1 m wychodzi niefizycznie ujemna.
MIN_PATH_DISTANCE_KM = 0.001


def compute_path_loss_db(
    surface_profile_m: list[float],
    distance_km: float,
    agl_tx_m: float,
    agl_rx_m: float,
    fmhz: float,
    *,
    climate_klim: int = 5,
    ipol: int = 0,
    reliability_percent: float = 50,
    confidence_percent: float = 50,
) -> float:
    """Longley-Rice (ITM, point-to-point) path loss [dB] via itmlogic.

    Ground: average soil (eps=15, sigma=0.005 S/m); surface refractivity 314 N-units;
    statistics in broadcast mode (mdvar=11) at the requested reliability/confidence.
    """
    samples = len(surface_profile_m) - 1
    if samples < 1:
        raise ValueError("terrain profile needs at least two samples")
    distance_km = max(distance_km, MIN_PATH_DISTANCE_KM)
    prop: dict[str, Any] = {
        "eps": 15,
        "sgm": 0.005,
        "klim": climate_klim,
        "ens0": 314,
        "lvar": 5,
        "gma": 157e-9,
        "kwx": 0,
        "fmhz": fmhz,
        "hg": [float(agl_tx_m), float(agl_rx_m)],
        "ipol": ipol,
        "d": distance_km,
        "klimx": 0,
        "mdvarx": 11,
        # itmlogic profile format: [n_intervals, interval_m, z0, z1, ...]
        "pfl": [samples, distance_km * 1000.0 / samples, *map(float, surface_profile_m)],
    }
    prop["wn"] = fmhz / 47.7
    prop["ens"] = prop["ens0"]
    prop["gme"] = prop["gma"] * (1 - 0.04665 * math.exp(prop["ens"] / 179.3))
    zq = complex(prop["eps"], 376.62 * prop["sgm"] / prop["wn"])
    prop["zgnd"] = np.sqrt(zq - 1)
    if ipol != 0:
        prop["zgnd"] = prop["zgnd"] / zq

    prop = qlrpfl(prop)
    free_space_db = _DB_PER_NEPER * np.log(2 * prop["wn"] * prop["dist"])
    z_reliability = qerfi([reliability_percent / 100])[0]
    z_confidence = qerfi([confidence_percent / 100])[0]
    variability_db, _ = avar(z_reliability, 0, z_confidence, prop)
    return float(free_space_db + variability_db)
