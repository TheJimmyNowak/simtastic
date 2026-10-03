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
