"""Metryki statystyczne walidacji: klasyfikacja łączy, kalibracja prawdopodobieństw, błędy ciągłe.

Przedziały ufności liczone bootstrapem po grupach (np. po bramce albo klastrze), bo pary
z tą samą bramką nie są niezależne.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

import numpy as np
from scipy import stats as sps


def auc(scores: Sequence[float], labels: Sequence[bool]) -> float:
    """Pole pod krzywą ROC (statystyka Manna–Whitneya), remisy liczone po połowie."""
    s = np.asarray(scores, float)
    y = np.asarray(labels, bool)
    pos, neg = s[y], s[~y]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    ranks = sps.rankdata(np.concatenate([pos, neg]))
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def brier(p: Sequence[float], y: Sequence[bool]) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2))


def log_loss(p: Sequence[float], y: Sequence[bool], eps: float = 1e-4) -> float:
    p = np.clip(np.asarray(p, float), eps, 1 - eps)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier_skill(p: Sequence[float], y: Sequence[bool]) -> float:
    """1 − Brier/Brier_klimatologii; > 0 znaczy lepiej niż stała równa częstości łączy."""
    y = np.asarray(y, float)
    ref = brier(np.full_like(y, y.mean()), y)
    return float(1 - brier(p, y) / ref) if ref > 0 else float("nan")


def ece(p: Sequence[float], y: Sequence[bool], bins: int = 5) -> float:
    """Expected calibration error: średnia ważona |p̄ − ȳ| w przedziałach p."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        if m.any():
            total += m.sum() * abs(p[m].mean() - y[m].mean())
    return float(total / len(p))


def calibration_fit(p: Sequence[float], y: Sequence[bool]) -> tuple[float, float]:
    """Regresja logistyczna y ~ a + b·logit(p): idealnie a = 0, b = 1 (b < 1 — model zbyt pewny)."""
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    x = np.log(p / (1 - p))
    y = np.asarray(y, float)
    a, b = 0.0, 1.0
    for _ in range(50):  # Newton–Raphson
        z = a + b * x
        mu = 1 / (1 + np.exp(-z))
        w = mu * (1 - mu) + 1e-9
        g = np.array([np.sum(y - mu), np.sum((y - mu) * x)])
        h = np.array([[np.sum(w), np.sum(w * x)], [np.sum(w * x), np.sum(w * x * x)]])
        try:
            step = np.linalg.solve(h, g)
        except np.linalg.LinAlgError:
            break
        a, b = a + step[0], b + step[1]
        if np.abs(step).max() < 1e-8:
            break
    if not (np.isfinite(a) and np.isfinite(b)) or abs(b) > 50:
        return float("nan"), float("nan")  # rozbieżne: p bliskie 0/1 rozdziela klasy
    return float(a), float(b)


def confusion(pred: Sequence[bool], y: Sequence[bool]) -> dict:
    pred, y = np.asarray(pred, bool), np.asarray(y, bool)
    tp = int(np.sum(pred & y))
    tn = int(np.sum(~pred & ~y))
    fp = int(np.sum(pred & ~y))
    fn = int(np.sum(~pred & y))
    tpr = tp / (tp + fn) if tp + fn else float("nan")
    tnr = tn / (tn + fp) if tn + fp else float("nan")
    denom = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    return {
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "tpr": tpr, "tnr": tnr,
        "bal_acc": (tpr + tnr) / 2,
        "mcc": (tp * tn - fp * fn) / denom if denom else 0.0,
    }


def cohen_kappa(a: Sequence, b: Sequence) -> float:
    """Zgodność dwóch klasyfikacji skorygowana o zgodność przypadkową."""
    a, b = list(a), list(b)
    if not a:
        return float("nan")
    cats = sorted(set(a) | set(b), key=str)
    po = sum(x == y for x, y in zip(a, b)) / len(a)
    pe = sum((a.count(c) / len(a)) * (b.count(c) / len(b)) for c in cats)
    return float((po - pe) / (1 - pe)) if pe < 1 else float("nan")


def error_stats(sim: Sequence[float], real: Sequence[float]) -> dict:
    """Błąd ciągły (np. RSSI): bias, MAE, RMSE, granice zgodności Blanda–Altmana, korelacje."""
    s, r = np.asarray(sim, float), np.asarray(real, float)
    d = s - r
    out = {"n": int(len(d))}
    if len(d) == 0:
        return out
    out.update(
        bias=float(d.mean()),
        median=float(np.median(d)),
        mae=float(np.abs(d).mean()),
        rmse=float(np.sqrt((d ** 2).mean())),
    )
    if len(d) > 2:
        sd = float(d.std(ddof=1))
        out.update(loa_low=out["bias"] - 1.96 * sd, loa_high=out["bias"] + 1.96 * sd)
        out["pearson"] = float(np.corrcoef(s, r)[0, 1]) if s.std() and r.std() else float("nan")
        out["spearman"] = float(sps.spearmanr(s, r).statistic)
    return out


def group_bootstrap(
    metric: Callable[[list[int]], float],
    groups: Sequence,
    n_boot: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentylowy przedział ufności: losowanie grup ze zwracaniem, metryka na ich elementach."""
    rng = np.random.default_rng(seed)
    by_group: dict = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    keys = list(by_group)
    vals = []
    for _ in range(n_boot):
        idx = [i for k in rng.choice(len(keys), size=len(keys), replace=True) for i in by_group[keys[k]]]
        v = metric(idx)
        if v == v:  # pomija NaN (np. próba bez łączy dodatnich)
            vals.append(v)
    if not vals:
        return float("nan"), float("nan")
    return float(np.quantile(vals, alpha / 2)), float(np.quantile(vals, 1 - alpha / 2))


def wasserstein(a: Sequence[float], b: Sequence[float]) -> float:
    """Odległość Wassersteina (EMD) między rozkładami empirycznymi, np. liczby skoków."""
    if len(a) == 0 or len(b) == 0:
        return float("nan")
    return float(sps.wasserstein_distance(a, b))


def normal_cdf(x):
    return sps.norm.cdf(x)
