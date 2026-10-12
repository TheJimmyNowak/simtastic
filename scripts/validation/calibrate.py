"""Kalibracja modelu łączy na zbiorze kalibracyjnym (największa wiarygodność, model dwumianowy).

Dla pary nadawca → bramka, z której bramka zaraportowała n pakietów nadawcy, w tym
n_direct odebranych bezpośrednio:

    X ~ N(0, σ_ℓ²)                               trwałe cieniowanie łącza (cecha pary)
    p(X) = (1 − ε) · Φ((m − L − X) / σ_f)        odbiór bezpośredni jednego pakietu
    n_direct ~ Binom(n, p(X))

gdzie m = RSSI_med − czułość z modelu fizycznego (NMT + budynki LoD1), L — strata otoczenia,
σ_f — fading kolejnych pakietów (zmierzony: rozrzut RSSI odbiorów bezpośrednich, nie
dopasowywany), ε — straty niezależne od propagacji (kolizje w sieci rzeczywistej, półdupleks
bramki). Gdy bramka podaje RSSI odbiorów bezpośrednich, cieniowanie jest prawie wyznaczone:
X̂ = RSSI_med − L − mediana(RSSI), co wiąże L i σ_ℓ z poziomem sygnału, a nie tylko z faktem
odbioru (efekt selekcji jest w tym modelu jawny).

Wcześniejsza kalibracja porównywała etykietę „≥ 1 odbiór z n pakietów” z prawdopodobieństwem
odbioru jednego pakietu, przyjmowała σ_f = 8 dB i liczyła straty bez budynków.

    PYTHONPATH=backend:scripts/validation python scripts/validation/calibrate.py \
        data/lorastats/2026-10-08/links_do2026-10-07T1735.json --fading 1.0
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy import optimize
from scipy import stats as sps

_GH_X, _GH_W = np.polynomial.hermite_e.hermegauss(64)
_GH_W = _GH_W / _GH_W.sum()
# Mniejsza kwadratura dla modelu z efektem bramki (całka podwójna): 24 × 24 węzły.
_GH24_X, _GH24_W = np.polynomial.hermite_e.hermegauss(24)
_GH24_W = _GH24_W / _GH24_W.sum()


def _p_packet(margin: np.ndarray, loss: float, x: np.ndarray, sigma_f: float, eps: float) -> np.ndarray:
    return (1.0 - eps) * sps.norm.cdf((margin - loss - x) / max(sigma_f, 1e-6))


def row_arrays(rows: list[dict]) -> dict[str, np.ndarray]:
    return {
        "margin": np.array([r["median_rssi_db"] - r["sensitivity_dbm"] for r in rows]),
        "median_rssi": np.array([r["median_rssi_db"] for r in rows]),
        "n": np.array([max(int(r.get("n_packets") or 0), 0) for r in rows]),
        "k": np.array([int(r.get("n_direct") or 0) for r in rows]),
        "rssi": np.array([float(np.median(r["rssi_direct"])) if r.get("rssi_direct") else np.nan for r in rows]),
        "link": np.array([bool(r["link"]) for r in rows]),
    }


def neg_log_lik(params: np.ndarray, a: dict, sigma_f: float, use_rssi: bool = True) -> float:
    loss, log_sigma, logit_eps = params
    sigma_l = float(np.exp(np.clip(log_sigma, -3.0, 5.0)))
    eps = float(1.0 / (1.0 + np.exp(-logit_eps)))
    total = 0.0
    has_rssi = use_rssi & ~np.isnan(a["rssi"]) & (a["k"] > 0)
    # Pary z RSSI: cieniowanie wyznaczone z poziomu sygnału.
    if has_rssi.any():
        x_hat = a["median_rssi"][has_rssi] - loss - a["rssi"][has_rssi]
        p = np.clip(_p_packet(a["margin"][has_rssi], loss, x_hat, sigma_f, eps), 1e-12, 1 - 1e-12)
        total += np.sum(sps.norm.logpdf(x_hat, scale=sigma_l))
        total += np.sum(sps.binom.logpmf(a["k"][has_rssi], a["n"][has_rssi], p))
    # Pozostałe: całkowanie po cieniowaniu (kwadratura Gaussa–Hermite'a).
    rest = ~has_rssi & (a["n"] > 0)
    if rest.any():
        x = sigma_l * _GH_X[None, :]
        p = np.clip(_p_packet(a["margin"][rest, None], loss, x, sigma_f, eps), 1e-12, 1 - 1e-12)
        lik = sps.binom.pmf(a["k"][rest, None], a["n"][rest, None], p) @ _GH_W
        total += np.sum(np.log(np.clip(lik, 1e-300, None)))
    return -float(total)


def neg_log_lik_nodes(params: np.ndarray, a: dict, gateway_index: np.ndarray, sigma_f: float) -> float:
    """Model z efektem instalacji bramki: X = U_bramki + X_pary, U ~ N(0, σ_w²), X_pary ~ N(0, σ_p²).

    Wiarygodność bramki to całka po U z iloczynu wiarygodności jej par (pary jednej bramki
    są zależne właśnie przez U). Efekt instalacji nadawcy nie jest tu osobno identyfikowalny
    i wchodzi do σ_p.
    """
    loss, log_sw, log_sp, logit_eps = params
    sigma_w, sigma_p = float(np.exp(log_sw)), float(np.exp(log_sp))
    eps = float(1.0 / (1.0 + np.exp(-logit_eps)))
    u = sigma_w * _GH24_X  # węzły kwadratury efektu bramki
    has_rssi = ~np.isnan(a["rssi"]) & (a["k"] > 0)
    log_f = np.zeros((len(a["n"]), len(u)))
    if has_rssi.any():
        x_hat = a["median_rssi"][has_rssi, None] - loss - u[None, :] - a["rssi"][has_rssi, None]
        p = np.clip((1.0 - eps) * sps.norm.cdf((a["rssi"][has_rssi] - (a["median_rssi"][has_rssi] - a["margin"][has_rssi])) / sigma_f), 1e-12, 1 - 1e-12)
        log_f[has_rssi] = sps.norm.logpdf(x_hat, scale=sigma_p) + sps.binom.logpmf(a["k"][has_rssi], a["n"][has_rssi], p)[:, None]
    rest = ~has_rssi
    if rest.any():
        x = sigma_p * _GH24_X
        shift = a["margin"][rest, None, None] - loss - u[None, :, None] - x[None, None, :]
        p = np.clip((1.0 - eps) * sps.norm.cdf(shift / sigma_f), 1e-12, 1 - 1e-12)
        lik = sps.binom.pmf(a["k"][rest, None, None], a["n"][rest, None, None], p) @ _GH24_W
        log_f[rest] = np.log(np.clip(lik, 1e-300, None))
    total = 0.0
    log_w = np.log(_GH24_W)
    for g in np.unique(gateway_index):
        per_u = log_w + log_f[gateway_index == g].sum(axis=0)
        total += float(np.logaddexp.reduce(per_u))
    return -total


def fit_nodes(rows: list[dict], sigma_f: float) -> dict:
    a = row_arrays(rows)
    gateways = {gw: i for i, gw in enumerate(sorted({r["rx"] for r in rows}))}
    gateway_index = np.array([gateways[r["rx"]] for r in rows])
    best = None
    for loss0 in (10.0, 40.0):
        for sw0 in (5.0, 20.0):
            for sp0 in (10.0,):
                res = optimize.minimize(
                    neg_log_lik_nodes, np.array([loss0, np.log(sw0), np.log(sp0), -3.0]),
                    args=(a, gateway_index, sigma_f), method="Nelder-Mead",
                    options={"xatol": 1e-2, "fatol": 1e-3, "maxiter": 3000},
                )
                if best is None or res.fun < best.fun:
                    best = res
    loss, log_sw, log_sp, logit_eps = best.x
    sigma_w, sigma_p = float(np.exp(log_sw)), float(np.exp(log_sp))
    return {
        "clutter_loss_db": float(loss),
        # Symulator losuje efekt instalacji dla każdego węzła (nadawcy i odbiorcy), więc
        # wariancja łącza σ_p² + σ_w² rozkłada się na σ_pary² = σ_p² − σ_w² i 2 × σ_w².
        "node_installation_sigma_db": sigma_w,
        "pair_residual_sigma_db": sigma_p,
        "link_shadowing_sigma_db": float(np.sqrt(max(sigma_p**2 - sigma_w**2, 0.0))),
        "eps": float(1.0 / (1.0 + np.exp(-logit_eps))),
        "neg_log_lik": float(best.fun),
        "fading_sigma_db": sigma_f,
        "n_pairs": len(rows),
        "n_gateways": len(gateways),
    }


def neg_log_lik_mixture(params: np.ndarray, a: dict, sigma_f: float) -> float:
    """Mieszanina: z prawdopodobieństwem π para jest zablokowana (żaden odbiór bezpośredni,
    np. antena bramki w budynku po stronie nadawcy), w przeciwnym razie model jak w `fit`."""
    loss, log_sigma, logit_eps, logit_pi = params
    sigma_l = float(np.exp(np.clip(log_sigma, -3.0, 5.0)))
    eps = float(1.0 / (1.0 + np.exp(-logit_eps)))
    pi = float(1.0 / (1.0 + np.exp(-logit_pi)))
    total = 0.0
    has_rssi = ~np.isnan(a["rssi"]) & (a["k"] > 0)
    if has_rssi.any():
        x_hat = a["median_rssi"][has_rssi] - loss - a["rssi"][has_rssi]
        p = np.clip(_p_packet(a["margin"][has_rssi], loss, x_hat, sigma_f, eps), 1e-12, 1 - 1e-12)
        total += np.sum(np.log1p(-pi) + sps.norm.logpdf(x_hat, scale=sigma_l)
                        + sps.binom.logpmf(a["k"][has_rssi], a["n"][has_rssi], p))
    rest = ~has_rssi & (a["n"] > 0)
    if rest.any():
        x = sigma_l * _GH_X[None, :]
        p = np.clip(_p_packet(a["margin"][rest, None], loss, x, sigma_f, eps), 1e-12, 1 - 1e-12)
        open_lik = sps.binom.pmf(a["k"][rest, None], a["n"][rest, None], p) @ _GH_W
        lik = (1.0 - pi) * open_lik + pi * (a["k"][rest] == 0)
        total += np.sum(np.log(np.clip(lik, 1e-300, None)))
    return -float(total)


def fit_mixture(rows: list[dict], sigma_f: float) -> dict:
    a = row_arrays(rows)
    best = None
    for loss0 in (0.0, 20.0):
        for sig0 in (5.0, 15.0):
            for pi0 in (-1.0, 1.0):
                res = optimize.minimize(
                    neg_log_lik_mixture, np.array([loss0, np.log(sig0), -3.0, pi0]), args=(a, sigma_f),
                    method="Nelder-Mead", options={"xatol": 1e-3, "fatol": 1e-4, "maxiter": 4000},
                )
                if best is None or res.fun < best.fun:
                    best = res
    loss, log_sigma, logit_eps, logit_pi = best.x
    return {
        "clutter_loss_db": float(loss),
        "link_shadowing_sigma_db": float(np.exp(np.clip(log_sigma, -3.0, 5.0))),
        "link_blocked_fraction": float(1.0 / (1.0 + np.exp(-logit_pi))),
        "eps": float(1.0 / (1.0 + np.exp(-logit_eps))),
        "neg_log_lik": float(best.fun),
        "fading_sigma_db": sigma_f,
        "n_pairs": len(rows),
    }


FITTERS = {"nodes": None, "pairs": None, "mixture": None}  # uzupełniane po definicjach


def total_sigma(f: dict) -> float:
    """Odchylenie straty łącza w modelu symulatora: cieniowanie pary + instalacje obu węzłów."""
    return float(np.sqrt(f["link_shadowing_sigma_db"] ** 2 + 2 * f.get("node_installation_sigma_db", 0.0) ** 2))


def fit(rows: list[dict], sigma_f: float, use_rssi: bool = True) -> dict:
    a = row_arrays(rows)
    best = None
    for loss0 in (0.0, 15.0, 30.0):
        for sig0 in (5.0, 15.0):
            for eps0 in (-3.0, 0.0):
                res = optimize.minimize(
                    neg_log_lik, np.array([loss0, np.log(sig0), eps0]), args=(a, sigma_f, use_rssi),
                    method="Nelder-Mead", options={"xatol": 1e-3, "fatol": 1e-4, "maxiter": 4000},
                )
                if best is None or res.fun < best.fun:
                    best = res
    loss, log_sigma, logit_eps = best.x
    return {
        "clutter_loss_db": float(loss),
        "link_shadowing_sigma_db": float(np.exp(log_sigma)),
        "eps": float(1.0 / (1.0 + np.exp(-logit_eps))),
        "neg_log_lik": float(best.fun),
        "fading_sigma_db": sigma_f,
        "n_pairs": len(rows),
    }


def link_probability(
    rows: list[dict], loss: float, sigma_l: float, sigma_f: float, eps: float, blocked: float = 0.0
) -> np.ndarray:
    """P(≥ 1 odbiór bezpośredni z n pakietów) — ta sama definicja co etykieta „łącze”."""
    a = row_arrays(rows)
    n = np.maximum(a["n"], 1)
    x = sigma_l * _GH_X[None, :]
    p = _p_packet(a["margin"][:, None], loss, x, sigma_f, eps)
    return (1.0 - blocked) * (1.0 - (1.0 - p) ** n[:, None] @ _GH_W)


def bootstrap(rows: list[dict], sigma_f: float, n_boot: int, seed: int = 0, model: str = "nodes") -> dict:
    """Przedziały 90% przez losowanie bramek ze zwracaniem (pary z jedną bramką są zależne)."""
    rng = np.random.default_rng(seed)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["rx"], []).append(r)
    keys = list(groups)
    fits = []
    for _ in range(n_boot):
        sample = [r for k in rng.choice(len(keys), size=len(keys)) for r in groups[keys[k]]]
        if sum(r["link"] for r in sample) < 3:
            continue
        fits.append(FITTERS[model](sample, sigma_f))
    out = {"n_boot": len(fits)}
    if not fits:
        return out
    for key in ("clutter_loss_db", "node_installation_sigma_db", "link_shadowing_sigma_db", "link_blocked_fraction", "eps"):
        vals = [f.get(key, 0.0) for f in fits]
        out[key] = [float(np.quantile(vals, 0.05)), float(np.quantile(vals, 0.95))]
    out["n_boot"] = len(fits)
    return out


def cross_validate(rows: list[dict], sigma_f: float, model: str = "nodes") -> dict:
    """Walidacja krzyżowa z pominięciem bramki: parametry z pozostałych bramek, predykcja
    P(łącze) dla par pominiętej. Zbiór testowy różni się od kalibracyjnego tylko oknem czasu
    (te same pary), więc to jedyny sprawdzian przeniesienia kalibracji na nowe miejsca."""
    import stats as st

    gateways = sorted({r["rx"] for r in rows})
    p_out = np.full(len(rows), np.nan)
    for gw in gateways:
        train = [r for r in rows if r["rx"] != gw]
        test_idx = [i for i, r in enumerate(rows) if r["rx"] == gw]
        if sum(r["link"] for r in train) < 3:
            continue
        f = FITTERS[model](train, sigma_f)
        p_out[test_idx] = link_probability(
            [rows[i] for i in test_idx], f["clutter_loss_db"], total_sigma(f), sigma_f, f["eps"],
            f.get("link_blocked_fraction", 0.0),
        )
    ok = ~np.isnan(p_out)
    y = np.array([r["link"] for r in rows], bool)[ok]
    return {
        "n": int(ok.sum()),
        "brier": st.brier(p_out[ok], y),
        "brier_skill": st.brier_skill(p_out[ok], y),
        "auc": st.auc(p_out[ok], y),
        "mean_p": float(p_out[ok].mean()),
        "base_rate": float(y.mean()),
    }


FITTERS.update(nodes=fit_nodes, pairs=fit, mixture=fit_mixture)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("links", type=Path, help="links_*.json z link_eval.py (zbiór kalibracyjny)")
    ap.add_argument("--fading", type=float, default=1.0, help="σ_f [dB], zmierzone osobno")
    ap.add_argument("--boot", type=int, default=40)
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args()
    rows = [r for r in json.loads(args.links.read_text(encoding="utf-8"))["gateway_rows"] if r.get("n_packets")]
    # Dwa modele; wybór z góry ustaloną regułą: mniejszy Brier w walidacji krzyżowej
    # z pominięciem bramki (przewidywanie łączy dla bramek, których kalibracja nie widziała).
    fits = {name: fitter(rows, args.fading) for name, fitter in FITTERS.items()}
    cv = {name: cross_validate(rows, args.fading, name) for name in fits}
    chosen = min(cv, key=lambda name: cv[name]["brier"])
    result = {
        "model": chosen,
        "fit": fits[chosen],
        "fits": fits,
        "fit_without_rssi": fit(rows, args.fading, use_rssi=False),
        "cv_leave_one_gateway_out": cv,
        "n_links": int(sum(r["link"] for r in rows)),
    }
    if args.boot:
        result["ci90"] = bootstrap(rows, args.fading, args.boot, model=chosen)
    out = args.out or args.links.with_name("calibration.json")
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
