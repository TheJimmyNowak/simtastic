"""T1: ocena modelu łączy na parach z lorastats (ten sam kod ITM i budynków co precompute).

Dla każdej pary liczy medianowe RSSI (teren ITM + budynki), a z niego prawdopodobieństwo
odbioru pojedynczego pakietu w dwóch wariantach: fizycznym (σ = fading 8 dB) i skalibrowanym
7.10.2026 (strata otoczenia 27 dB, σ² = 20² + 8²). Parametry nie są dopasowywane do tych danych.

    PYTHONPATH=backend:scripts/validation .venv/bin/python scripts/validation/link_eval.py \
        data/lorastats/2026-10-08/clusters_2026-10-07T1735.json
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np

import stats as st
from app.config import settings
from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig
from app.services.propagation.building_index import BuildingIndex
from app.services.propagation.building_model import resolve_node_mount
from app.services.propagation.itm_precompute import _pair_task
from app.services.propagation.srtm_profile import elevation_m
from app.services.rf_link_model import compute_rssi_db
from lorastats_pairs import PRESETS

FADING_SIGMA = 8.0
VARIANTS = {
    "fizyczny": {"clutter": 0.0, "sigma": FADING_SIGMA},
    "skalibrowany": {"clutter": 27.0, "sigma": math.hypot(20.0, FADING_SIGMA)},
}
ANTENNA_GAIN = 2.0


def node_cfg(nid: str, n: dict) -> NodeConfig:
    return NodeConfig(id=nid, lat=n["lat"], lon=n["lon"], agl_m=n["agl_m"])


def pair_losses(nodes: dict, pairs: list[dict]) -> list[dict]:
    """Strata ITM + budynki dla każdej pary (w kierunku nadawca → odbiorca)."""
    index = BuildingIndex(settings.buildings_index_path)
    index.build(settings.buildings_raw_dir)
    mounts = {}
    for nid in {p["tx"] for p in pairs} | {p["rx"] for p in pairs}:
        n = nodes[nid]
        ground = elevation_m(settings.srtm_cache_dir, n["lat"], n["lon"])
        m0 = resolve_node_mount(index, n["lat"], n["lon"], ground, 0.0)
        building_h = m0.surface_z_m - ground if m0.placement == "rooftop" else 0.0
        mast = max(1.0, n["agl_m"] - building_h) if building_h and n["agl_from_report"] else n["agl_m"]
        mounts[nid] = (mast, resolve_node_mount(index, n["lat"], n["lon"], ground, mast))
    tasks = []
    for p in pairs:
        a, b = nodes[p["tx"]], nodes[p["rx"]]
        env = EnvironmentConfig(**PRESETS[a["channel_hash"]])
        tasks.append(
            (
                str(settings.srtm_cache_dir.resolve()),
                str(settings.buildings_index_path.resolve()),
                env.model_dump(),
                0,
                1,
                node_cfg(p["tx"], {**a, "agl_m": mounts[p["tx"]][0]}).model_dump(),
                node_cfg(p["rx"], {**b, "agl_m": mounts[p["rx"]][0]}).model_dump(),
                asdict(mounts[p["tx"]][1]),
                asdict(mounts[p["rx"]][1]),
            )
        )
    with ProcessPoolExecutor(max_workers=8, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_pair_task, *zip(*tasks), chunksize=4))
    out = []
    for p, r in zip(pairs, results):
        env = EnvironmentConfig(**PRESETS[nodes[p["tx"]]["channel_hash"]])
        lora = env.resolve_lora()
        loss = r.terrain_loss_ij_db + r.buildings.loss_db
        out.append(
            {
                **p,
                "terrain_loss_db": r.terrain_loss_ij_db,
                "building_loss_db": r.buildings.loss_db,
                "median_rssi_db": compute_rssi_db(lora.tx_power_dbm, ANTENNA_GAIN, ANTENNA_GAIN, loss, 0.0),
                "sensitivity_dbm": lora.sensitivity_dbm,
                "noise_floor_dbm": lora.noise_floor_dbm,
            }
        )
    return out


def evaluate(rows: list[dict], group_key: str = "rx") -> dict:
    y = np.array([r["link"] for r in rows], bool)
    margin = np.array([r["median_rssi_db"] - r["sensitivity_dbm"] for r in rows])
    groups = [r[group_key] for r in rows]
    res: dict = {"n": len(rows), "n_link": int(y.sum()), "base_rate": float(y.mean())}
    res["auc"] = st.auc(margin, y)
    res["auc_ci"] = st.group_bootstrap(lambda idx: st.auc(margin[idx], y[idx]), groups)
    for name, v in VARIANTS.items():
        p = st.normal_cdf((margin - v["clutter"]) / v["sigma"])
        pred = margin - v["clutter"] >= 0
        a, b = st.calibration_fit(p, y)
        res[name] = {
            "brier": st.brier(p, y),
            "brier_ci": st.group_bootstrap(lambda idx: st.brier(p[idx], y[idx]), groups),
            "brier_skill": st.brier_skill(p, y),
            "log_loss": st.log_loss(p, y),
            "ece": st.ece(p, y),
            "calib_intercept": a,
            "calib_slope": b,
            "mean_p": float(p.mean()),
            **st.confusion(pred, y),
        }
        # Błąd RSSI łączy, które istnieją (RSSI z raportów bramki przy odbiorze bezpośrednim).
        sim, real = [], []
        for r in rows:
            if r.get("rssi_direct"):
                sim.append(r["median_rssi_db"] - v["clutter"])
                real.append(float(np.median(r["rssi_direct"])))
        res[name]["rssi"] = st.error_stats(sim, real)
    # SNR: rzeczywisty nasyca się ok. +12 dB, więc tylko łącza słabe (SNR < 5 dB).
    sim, real = [], []
    for r in rows:
        if r.get("snr_direct"):
            snr_real = float(np.median(r["snr_direct"]))
            if snr_real < 5:
                sim.append(min(12.0, r["median_rssi_db"] - VARIANTS["skalibrowany"]["clutter"] - r["noise_floor_dbm"]))
                real.append(snr_real)
    res["snr_weak_cal"] = st.error_stats(sim, real)
    return res


def by_distance(rows: list[dict]) -> list[dict]:
    out = []
    for lo, hi in ((0.2, 2), (2, 5), (5, 15), (15, 60)):
        sel = [r for r in rows if lo * 1000 <= r["distance_m"] < hi * 1000]
        if not sel:
            continue
        m = np.array([r["median_rssi_db"] - r["sensitivity_dbm"] for r in sel])
        out.append(
            {
                "km": f"{lo}–{hi}",
                "n": len(sel),
                "link_real": sum(r["link"] for r in sel),
                "pred_phys": int(np.sum(m >= 0)),
                "pred_cal": int(np.sum(m - 27 >= 0)),
                "p_cal_mean": float(np.mean(st.normal_cdf((m - 27) / VARIANTS["skalibrowany"]["sigma"]))),
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("clusters", type=Path)
    args = ap.parse_args()
    data = json.loads(args.clusters.read_text(encoding="utf-8"))
    nodes = data["nodes"]
    gw_rows = pair_losses(nodes, data["gateway_pairs"])
    nb_rows = pair_losses(nodes, data["neighbor_pairs"])

    res = {"gateway_pairs": evaluate(gw_rows), "by_distance": by_distance(gw_rows)}
    for name, v in VARIANTS.items():
        m = np.array([r["median_rssi_db"] - r["sensitivity_dbm"] - v["clutter"] for r in nb_rows])
        res.setdefault("neighbor_pairs", {"n": len(nb_rows)})[name] = {
            "tpr": float(np.mean(m >= 0)),
            "mean_p": float(np.mean(st.normal_cdf(m / v["sigma"]))),
        }
    by_preset = {}
    for ch in sorted({nodes[r["tx"]]["channel_hash"] for r in gw_rows}):
        sel = [r for r in gw_rows if nodes[r["tx"]]["channel_hash"] == ch]
        if sum(r["link"] for r in sel) and not all(r["link"] for r in sel):
            by_preset[ch] = {"n": len(sel), "n_link": sum(r["link"] for r in sel),
                             "auc": st.auc([r["median_rssi_db"] - r["sensitivity_dbm"] for r in sel], [r["link"] for r in sel])}
    res["by_channel_hash"] = by_preset
    out = args.clusters.with_name(args.clusters.stem.replace("clusters", "links") + ".json")
    out.write_text(json.dumps({"results": res, "gateway_rows": gw_rows, "neighbor_rows": nb_rows}, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
