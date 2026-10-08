"""Rysunki do pracy z wyników kampanii testowej (PDF do latex/rysunki/).

    PYTHONPATH=backend:scripts/validation .venv/bin/python scripts/validation/figures.py \
        data/lorastats/2026-10-08/links_2026-10-07T1735.json data/campaign/report.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
import numpy as np

import stats as st
from link_eval import VARIANTS

OUT = Path(__file__).resolve().parents[2] / "latex" / "rysunki"
# Paleta zweryfikowana walidatorem (kolejność stała): fizyczny, skalibrowany, sieć.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, GRID, NEUTRAL = "#0b0b0b", "#52514e", "#e4e3df", "#8a8984"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.edgecolor": INK2,
        "axes.labelcolor": INK,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "legend.frameon": False,
        "lines.linewidth": 2,
        "savefig.bbox": "tight",
    }
)


def save(fig, name: str) -> None:
    # Przecinek dziesiętny na osiach liczbowych (bez zależności od locale systemu).
    comma = matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}".replace(".", ",").replace("-", "−"))
    for ax in fig.axes:
        for axis in (ax.xaxis, ax.yaxis):
            if isinstance(axis.get_major_formatter(), matplotlib.ticker.ScalarFormatter):
                axis.set_major_formatter(comma)
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.pdf")
    fig.savefig(OUT / f"{name}.png", dpi=150)
    plt.close(fig)
    print(OUT / f"{name}.pdf")


def reliability(links: dict) -> None:
    """Wykres niezawodności: przewidywane p łącza vs odsetek łączy w sieci (przedziały p)."""
    rows = links["gateway_rows"]
    y = np.array([r["link"] for r in rows], float)
    margin = np.array([r["median_rssi_db"] - r["sensitivity_dbm"] for r in rows])
    fig, ax = plt.subplots(figsize=(4.2, 3.6))
    ax.plot([0, 1], [0, 1], color=NEUTRAL, lw=1, ls="--", label="idealna kalibracja")
    edges = np.linspace(0, 1, 6)
    for (name, v), color, marker in zip(VARIANTS.items(), (BLUE, ORANGE), ("o", "s")):
        p = st.normal_cdf((margin - v["clutter"]) / v["sigma"])
        xs, ys, ns = [], [], []
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
            if m.sum():
                xs.append(p[m].mean())
                ys.append(y[m].mean())
                ns.append(int(m.sum()))
        ax.plot(xs, ys, color=color, marker=marker, ms=6, label=f"model {name}")
        for x_, y_, n in zip(xs, ys, ns):
            left = color == BLUE  # podpisy serii po przeciwnych stronach punktów
            ax.annotate(f"n={n}", (x_, y_), textcoords="offset points", xytext=(-4 if left else 4, 6),
                        ha="right" if left else "left", fontsize=7, color=INK2)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("przewidywane prawdopodobieństwo łącza")
    ax.set_ylabel("odsetek par z łączem w sieci")
    ax.legend(loc="upper left", fontsize=8)
    save(fig, "t1_kalibracja")


def by_distance(links: dict) -> None:
    """Odsetek par z łączem według odległości: sieć vs średnie p obu modeli."""
    rows = links["gateway_rows"]
    bins = [(0.2, 2), (2, 5), (5, 15), (15, 60)]
    labels, real, phys, cal, ns = [], [], [], [], []
    for lo, hi in bins:
        sel = [r for r in rows if lo * 1000 <= r["distance_m"] < hi * 1000]
        m = np.array([r["median_rssi_db"] - r["sensitivity_dbm"] for r in sel])
        labels.append(f"{lo:g}–{hi:g} km\nn = {len(sel)}".replace(".", ","))
        ns.append(len(sel))
        real.append(np.mean([r["link"] for r in sel]))
        phys.append(np.mean(st.normal_cdf(m / VARIANTS["fizyczny"]["sigma"])))
        cal.append(np.mean(st.normal_cdf((m - 27) / VARIANTS["skalibrowany"]["sigma"])))
    x = np.arange(len(bins))
    w = 0.26
    fig, ax = plt.subplots(figsize=(5.2, 3.2))
    for off, vals, color, name in ((-w, phys, BLUE, "model fizyczny"), (0, cal, ORANGE, "model skalibrowany"), (w, real, AQUA, "sieć (lorastats)")):
        ax.bar(x + off, vals, width=w - 0.03, color=color, label=name, zorder=2)
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 1)
    ax.set_ylabel("odsetek par z łączem")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper right", fontsize=8)
    save(fig, "t1_odleglosc")


def hops(report: dict) -> None:
    """Różnica liczby skoków (symulacja − sieć) dla par osiągniętych, wariant fizyczny vs skalibrowany."""
    rr = report["routing"]["pair_rows"]
    fig, ax = plt.subplots(figsize=(4.6, 3.0))
    width = 0.38
    for off, (variant, color, name) in zip((-width / 2, width / 2), (("phys", BLUE, "model fizyczny"), ("cal", ORANGE, "model skalibrowany"))):
        d = [r["sim_hop_mode"] - r["real_hop_mode"] for r in rr.get(variant, []) if r["sim_delivered"]]
        if not d:
            continue
        vals, counts = np.unique(d, return_counts=True)
        ax.bar(vals + off, counts / len(d), width=width - 0.04, color=color, label=f"{name} (n = {len(d)})", zorder=2)
    ax.axvline(0, color=NEUTRAL, lw=1, ls="--")
    ax.set_xlabel("liczba skoków: symulacja − sieć (moda dla pary)")
    ax.set_ylabel("odsetek par")
    ax.grid(axis="x", visible=False)
    ax.legend(fontsize=8)
    save(fig, "t2_skoki")


def medium(report: dict) -> None:
    """PDR w funkcji obciążenia G na tle ALOHA."""
    rows = report["medium"]["rows"]
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(4.6, 3.2))
    g = np.linspace(0, max(r["G"] for r in rows) * 1.05, 100)
    ax.plot(g, np.exp(-2 * g), color=NEUTRAL, lw=1, ls="--", label="ALOHA czysta $e^{-2G}$")
    coll_rows = sorted([r for r in rows if r["collisions"]], key=lambda r: r["G"])
    ax.plot([r["G"] for r in coll_rows], [r["csma_theory"] for r in coll_rows], color=INK2, lw=1, ls=":",
            label="CSMA, okno slotu CAD $e^{-2\\lambda\\tau}$")
    for n, color, marker in ((10, BLUE, "o"), (20, ORANGE, "s")):
        sel = sorted([r for r in rows if r["n_nodes"] == n and r["collisions"]], key=lambda r: r["G"])
        if sel:
            ax.plot([r["G"] for r in sel], [r["pdr_text"] for r in sel], color=color, marker=marker, ms=6, lw=1.2,
                    label=f"{n} węzłów: sondy")
            ax.plot([r["G"] for r in sel], [r["pdr_firmware"] for r in sel], color=color, marker=marker, ms=5, lw=0,
                    markerfacecolor="none", label=f"{n} węzłów: pakiety firmware")
    nc = [r for r in rows if not r["collisions"]]
    if nc:
        ax.plot([r["G"] for r in nc], [r["pdr"] for r in nc], color=AQUA, marker="^", ms=5, lw=0, label="bez kolizji")
    ax.set_xlabel("obciążenie kanału G (czas nadawania / czas)")
    ax.set_ylabel("odsetek udanych odbiorów")
    ax.set_ylim(0, 1.05)
    ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2)
    save(fig, "t5_medium")


def sweep(report: dict) -> None:
    """T6: metryki routingu w funkcji straty otoczenia (σ cieniowania 20 dB), średnia ± SD seedów."""
    ps = report["routing"]["per_seed"]
    points = [(int(v[4:]), v) for v in ps if v.startswith("clut")] + ([(27, "cal")] if "cal" in ps else [])
    points.sort()
    if len(points) < 2:
        return
    x = [c for c, _ in points]
    fig, axes = plt.subplots(1, 2, figsize=(6.4, 2.8))
    for ax, key, label, ref in (
        (axes[0], "mode_agreement", "zgodność trybu\n(bezpośrednio / przez pośrednika)", None),
        (axes[1], "hop_bias", "różnica liczby skoków\n(symulacja − sieć)", 0.0),
    ):
        mean = [ps[v][key]["mean"] for _, v in points]
        sd = [ps[v][key]["sd"] for _, v in points]
        ax.errorbar(x, mean, yerr=sd, color=ORANGE, marker="s", ms=6, capsize=3, lw=1.5)
        if ref is not None:
            ax.axhline(ref, color=NEUTRAL, lw=1, ls="--")
        ax.set_xlabel("strata otoczenia [dB]")
        ax.set_ylabel(label)
        ax.set_xticks(x)
    fig.tight_layout()
    save(fig, "t6_strata_otoczenia")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("links", type=Path)
    ap.add_argument("report", type=Path, nargs="?")
    args = ap.parse_args()
    links = json.loads(args.links.read_text(encoding="utf-8"))
    reliability(links)
    by_distance(links)
    if args.report and args.report.exists():
        report = json.loads(args.report.read_text(encoding="utf-8"))
        hops(report)
        medium(report)
        sweep(report)


if __name__ == "__main__":
    main()
