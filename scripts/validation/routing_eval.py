"""T2–T5: analiza przebiegów kampanii (`data/campaign/runs/*.json`).

Routing (T2–T4): dla każdej pary nadawca → bramka z lorastats i każdego pakietu wysłanego
przez nadawcę w symulacji bierze pierwszy skuteczny odbiór na bramce: liczbę skoków
(hop_start − hop_limit nadania) i węzeł, który go nadał. Porównanie z rozkładem z sieci.
Medium (T5): skuteczność odbioru w syntetycznej sieci „każdy słyszy każdego” w funkcji
obciążenia, z kolizjami i bez, na tle teorii ALOHA.

    PYTHONPATH=backend:scripts/validation .venv/bin/python scripts/validation/routing_eval.py \
        data/campaign data/lorastats/2026-10-08/clusters_2026-10-07T1735.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy import stats as sps

import stats as st
from app.models.environment import EnvironmentConfig
from app.services.radio_medium import PACKET_HEADER_BYTES, lora_airtime_s, slot_time_s

PROBE_PORTS = {"TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP", "TRACEROUTE_APP"}


RUNS_SUBDIR = "runs"


def load_runs(campaign: Path, kind: str) -> list[dict]:
    runs = []
    for p in sorted((campaign / RUNS_SUBDIR).glob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        if r["job"]["kind"] == kind:
            runs.append(r)
    return runs


# --- routing ---------------------------------------------------------------------


def sim_observations(run: dict, pairs: list[dict]) -> dict[tuple[str, str], dict]:
    """{(nadawca, bramka): {"sent": n, "obs": [(skoki, przekaźnik, opóźnienie)]}} dla jednego przebiegu."""
    tx_by_key = {(t["tx_node"], t["packet_id"]): t for t in run["tx"]}
    originals = [
        t for t in run["tx"]
        if not t["is_rebroadcast"] and t.get("portnum") in PROBE_PORTS and t.get("origin_node") == t["tx_node"]
    ]
    first_rx: dict[tuple[int, str], dict] = {}
    for a in run["attempts"]:
        if a["delivered"] != "True":
            continue
        key = (a["packet_id"], a["rx_node"])
        if key not in first_rx:  # wiersze są posortowane po czasie
            first_rx[key] = a
    out = {}
    for p in pairs:
        src, gw = p["tx"], p["rx"]
        sent = [t for t in originals if t["tx_node"] == src]
        obs = []
        for t in sent:
            a = first_rx.get((t["packet_id"], gw))
            if a is None:
                continue
            relay_tx = tx_by_key.get((a["tx_node"], t["packet_id"]))
            if relay_tx is None or relay_tx.get("hop_start") is None:
                continue
            hops = int(relay_tx["hop_start"]) - int(relay_tx.get("hop_limit") or 0)
            obs.append((hops, a["tx_node"], a["_time"] - t["_time"]))
        out[(src, gw)] = {"sent": len(sent), "obs": obs}
    return out


def real_relay(byte: int, cluster_ids: list[str]) -> str:
    match = [n for n in cluster_ids if int(n[-2:], 16) == byte]
    return match[0] if len(match) == 1 else "ext"


def pair_table(runs: list[dict], cluster: dict) -> list[dict]:
    """Jedna pozycja na parę: rozkład z sieci i zebrany ze wszystkich przebiegów wariantu."""
    ids = [n["id"] for n in cluster["nodes"]]
    rows = []
    per_run = [sim_observations(r, cluster["pairs"]) for r in runs]
    for p in cluster["pairs"]:
        key = (p["tx"], p["rx"])
        obs = [o for pr in per_run for o in pr[key]["obs"]]
        sent = sum(pr[key]["sent"] for pr in per_run)
        reached_runs = sum(1 for pr in per_run if pr[key]["obs"])
        real_hops = p["hops"]
        real_direct = p["n_direct"] / p["n_packets"]
        relays_real = Counter(real_relay(b, ids) for b in p["relays"])
        sim_hops = [o[0] for o in obs]
        sim_relays = Counter(o[1] for o in obs if o[1] != p["tx"])
        rows.append(
            {
                "cluster": cluster["id"],
                "tx": p["tx"],
                "rx": p["rx"],
                "distance_m": p["distance_m"],
                "real_link": p["link"],
                "real_direct_frac": real_direct,
                "real_mode_direct": real_direct >= 0.5,
                "real_hops": real_hops,
                "real_hop_mode": Counter(real_hops).most_common(1)[0][0],
                "real_relay": relays_real.most_common(1)[0][0] if relays_real else None,
                "sim_sent": sent,
                "sim_delivered": len(obs),
                "sim_pdr": len(obs) / sent if sent else float("nan"),
                "sim_reached_runs": reached_runs,
                "sim_runs": len(runs),
                "sim_direct_frac": float(np.mean([o[0] == 0 and o[1] == p["tx"] for o in obs])) if obs else float("nan"),
                "sim_hops": sim_hops,
                "sim_hop_mode": Counter(sim_hops).most_common(1)[0][0] if obs else None,
                "sim_relay": sim_relays.most_common(1)[0][0] if sim_relays else None,
                "sim_latency_s": float(np.median([o[2] for o in obs])) if obs else float("nan"),
            }
        )
    return rows


def routing_metrics(rows: list[dict]) -> dict:
    """Metryki zgodności routingu dla zestawu par (z CI bootstrapowym po klastrach)."""
    reached = [r for r in rows if r["sim_delivered"]]
    out: dict = {"pairs": len(rows), "reached": len(reached), "reach_rate": len(reached) / len(rows) if rows else float("nan")}
    if not reached:
        return out
    y_real = np.array([r["real_mode_direct"] for r in reached])
    p_sim = np.array([r["sim_direct_frac"] for r in reached])
    sim_mode = p_sim >= 0.5
    groups = [r["cluster"] for r in reached]
    out["mode_agreement"] = float(np.mean(sim_mode == y_real))
    out["mode_agreement_ci"] = st.group_bootstrap(lambda i: float(np.mean(sim_mode[i] == y_real[i])), groups)
    out["kappa"] = st.cohen_kappa(list(sim_mode), list(y_real))
    out["brier_direct"] = st.brier(p_sim, y_real)
    out["brier_direct_ci"] = st.group_bootstrap(lambda i: st.brier(p_sim[i], y_real[i]), groups)
    real_frac = np.array([r["real_direct_frac"] for r in reached])
    out["direct_frac_mae"] = float(np.mean(np.abs(p_sim - real_frac)))
    out["direct_frac_spearman"] = float(sps.spearmanr(p_sim, real_frac).statistic) if len(reached) > 2 else float("nan")
    out["n_direct_sim"] = int(sim_mode.sum())
    out["n_direct_real"] = int(y_real.sum())
    d = np.array([r["sim_hop_mode"] - r["real_hop_mode"] for r in reached], float)
    out["hop_bias"] = float(d.mean())
    out["hop_bias_ci"] = st.group_bootstrap(lambda i: float(d[i].mean()), groups)
    out["hop_mae"] = float(np.abs(d).mean())
    out["hop_exact"] = float(np.mean(d == 0))
    out["hop_wasserstein"] = float(np.mean([st.wasserstein(r["sim_hops"], r["real_hops"]) for r in reached]))
    rel = [r for r in reached if r["real_relay"] not in (None, "ext") and r["sim_relay"]]
    out["relay_known"] = len(rel)
    out["relay_agree"] = sum(r["sim_relay"] == r["real_relay"] for r in rel)
    out["sim_pdr_mean"] = float(np.mean([r["sim_pdr"] for r in rows if r["sim_sent"]]))
    out["latency_median_s"] = float(np.nanmedian([r["sim_latency_s"] for r in reached]))
    return out


def per_run_metrics(run: dict, cluster: dict) -> dict:
    rows = pair_table([run], cluster)
    m = routing_metrics(rows)
    loss = Counter(a.get("loss_reason") or "ok" for a in run["attempts"])
    total = sum(loss.values()) or 1
    m["attempt_outcomes"] = {k: v / total for k, v in loss.items()}
    m["tx_total"] = len(run["tx"])
    m["rebroadcast_share"] = float(np.mean([t["is_rebroadcast"] for t in run["tx"]])) if run["tx"] else float("nan")
    m["start_s"] = run["timing"]["running"] - run["timing"]["precomputed"]
    m["precompute_s"] = run["timing"]["precomputed"] - run["timing"]["t0"]
    return m


def paired_comparison(rows_a: list[dict], rows_b: list[dict], key: str) -> dict:
    """Test Wilcoxona dla par osiągniętych w obu wariantach: |błąd| metryki `key`."""
    def err(r):
        if key == "direct":
            return abs(r["sim_direct_frac"] - float(r["real_mode_direct"]))
        return abs(r["sim_hop_mode"] - r["real_hop_mode"])
    a = {(r["tx"], r["rx"]): r for r in rows_a if r["sim_delivered"]}
    b = {(r["tx"], r["rx"]): r for r in rows_b if r["sim_delivered"]}
    common = sorted(set(a) & set(b))
    ea = np.array([err(a[k]) for k in common])
    eb = np.array([err(b[k]) for k in common])
    if len(common) < 3 or np.all(ea == eb):
        return {"n": len(common), "mean_err_a": float(ea.mean()) if len(common) else float("nan"),
                "mean_err_b": float(eb.mean()) if len(common) else float("nan"), "p_wilcoxon": float("nan")}
    return {
        "n": len(common),
        "mean_err_a": float(ea.mean()),
        "mean_err_b": float(eb.mean()),
        "p_wilcoxon": float(sps.wilcoxon(ea, eb).pvalue),
    }


def repeatability(runs: list[dict], cluster: dict) -> dict:
    """Zgodność wyników przebiegów o identycznej konfiguracji i seedzie (różnią się tylko
    czasem działania firmware): odsetek par z tym samym trybem i tą samą liczbą skoków."""
    tables = [pair_table([r], cluster) for r in runs]
    same_mode, same_hops, n = 0, 0, 0
    for rows in zip(*tables):
        if not all(r["sim_delivered"] for r in rows):
            continue
        n += 1
        same_mode += len({r["sim_direct_frac"] >= 0.5 for r in rows}) == 1
        same_hops += len({r["sim_hop_mode"] for r in rows}) == 1
    pdr = [np.nanmean([r["sim_pdr"] for r in rows]) for rows in tables]
    return {"pairs": n, "same_mode": same_mode / n if n else float("nan"),
            "same_hop_mode": same_hops / n if n else float("nan"), "pdr_runs": [float(x) for x in pdr]}


def routing_report(campaign: Path, clusters_path: Path) -> dict:
    clusters = {c["id"]: c for c in json.loads(clusters_path.read_text(encoding="utf-8"))["clusters"]}
    runs = load_runs(campaign, "routing")
    by_variant: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_variant[r["job"]["variant"]].append(r)
    report: dict = {"runs": len(runs), "variants": {}, "per_seed": {}, "comparisons": {}, "repeatability": {}}
    pooled_rows: dict[str, list[dict]] = {}
    for variant, vruns in sorted(by_variant.items()):
        rows = []
        for cid, c in clusters.items():
            cr = [r for r in vruns if r["job"]["cluster"] == cid and r["job"]["rep"] == 0]
            if cr:
                rows += pair_table(cr, c)
        pooled_rows[variant] = rows
        report["variants"][variant] = routing_metrics(rows)
        report.setdefault("per_cluster", {})[variant] = {
            cid: routing_metrics([r for r in rows if r["cluster"] == cid])
            for cid in sorted({r["cluster"] for r in rows})
        }
        # Rozrzut metryk między seedami (każdy seed = inne cieniowanie i fading).
        seeds = sorted({r["job"]["seed"] for r in vruns})
        per_seed = []
        for s in seeds:
            srows = []
            for cid, c in clusters.items():
                sr = [r for r in vruns if r["job"]["cluster"] == cid and r["job"]["seed"] == s and r["job"]["rep"] == 0]
                if sr:
                    srows += pair_table(sr, c)
            per_seed.append(routing_metrics(srows))
        keys = ["reach_rate", "mode_agreement", "kappa", "brier_direct", "hop_bias", "hop_mae", "hop_exact", "sim_pdr_mean"]
        report["per_seed"][variant] = {
            k: {"mean": float(np.nanmean([m.get(k, np.nan) for m in per_seed])),
                "sd": float(np.nanstd([m.get(k, np.nan) for m in per_seed], ddof=1)) if len(per_seed) > 1 else float("nan")}
            for k in keys
        }
        report["variants"][variant]["run_level"] = {
            "attempt_outcomes": {
                k: float(np.mean([per_run_metrics(r, clusters[r["job"]["cluster"]])["attempt_outcomes"].get(k, 0.0) for r in vruns]))
                for k in ("ok", "weak", "collision", "half_duplex")
            },
            "start_s_mean": float(np.mean([r["timing"]["running"] - r["timing"]["precomputed"] for r in vruns])),
            "precompute_s_mean": float(np.mean([r["timing"]["precomputed"] - r["timing"]["t0"] for r in vruns])),
            "runs": len(vruns),
        }
    for a, b in (("phys", "cal"), ("cal", "calnc"), ("cal", "calnb"), ("phys", "physnb")):
        if a in pooled_rows and b in pooled_rows:
            rows_a = pooled_rows[a]
            keys_b = {(r["tx"], r["rx"]) for r in pooled_rows[b]}
            rows_a = [r for r in rows_a if (r["tx"], r["rx"]) in keys_b]
            report["comparisons"][f"{a}_vs_{b}"] = {
                "direct": paired_comparison(rows_a, pooled_rows[b], "direct"),
                "hops": paired_comparison(rows_a, pooled_rows[b], "hops"),
            }
    for variant in ("cal", "phys"):
        for cid, c in clusters.items():
            reps = [r for r in by_variant.get(variant, []) if r["job"]["cluster"] == cid and r["job"]["seed"] == 1]
            if len(reps) >= 2:
                report["repeatability"][f"{variant}-{cid}"] = repeatability(sorted(reps, key=lambda r: r["job"]["rep"]), c)
    rep = report["repeatability"]
    if rep:
        report["repeatability_summary"] = {
            "same_mode": float(np.nanmean([v["same_mode"] for v in rep.values()])),
            "same_hop_mode": float(np.nanmean([v["same_hop_mode"] for v in rep.values()])),
            "pdr_sd_between_reps": float(np.mean([np.std(v["pdr_runs"], ddof=1) for v in rep.values()])),
        }
    report["pair_rows"] = pooled_rows
    return report


# --- medium (T5) -----------------------------------------------------------------


def medium_report(campaign: Path) -> dict:
    runs = load_runs(campaign, "medium")
    rows = []
    for r in runs:
        env = EnvironmentConfig(**{k: v for k, v in r["job"]["environment"].items()})
        lora = env.resolve_lora()
        originals = [t for t in r["tx"] if not t["is_rebroadcast"] and t.get("origin_node") == t["tx_node"]]
        horizon = r["timing"]["stopped"] - r["timing"]["running"]
        airtimes = [
            lora_airtime_s(int(t.get("payload_len") or 0) + PACKET_HEADER_BYTES, lora.spreading_factor, lora.bandwidth_khz, lora.coding_rate)
            for t in r["tx"]
        ]
        ids = {t["packet_id"] for t in originals}
        att = [a for a in r["attempts"] if a["packet_id"] in ids]
        outcomes = Counter(a.get("loss_reason") or "ok" for a in att)
        n_att = sum(outcomes.values()) or 1
        # Sondy (TEXT, chwile z procesu Poissona) osobno od pakietów firmware (NodeInfo,
        # pozycja), które przy wspólnym starcie kontenerów są nadawane synchronicznie.
        text_ids = {t["packet_id"] for t in originals if t.get("portnum") == "TEXT_MESSAGE_APP"}
        text_out = Counter(a.get("loss_reason") or "ok" for a in att if a["packet_id"] in text_ids)
        fw_out = Counter(a.get("loss_reason") or "ok" for a in att if a["packet_id"] not in text_ids)
        rate_all = len(r["tx"]) / horizon if horizon > 0 else float("nan")
        tau = slot_time_s(lora.spreading_factor, lora.bandwidth_khz)
        # Obciążenie oferowane G: łączny czas nadawania / czas trwania (wszystkie pakiety,
        # także NodeInfo i pozycje firmware).
        g = sum(airtimes) / horizon if horizon > 0 else float("nan")
        rows.append(
            {
                "id": r["job"]["id"],
                "n_nodes": r["job"]["n_nodes"],
                "rate": r["job"]["rate"],
                "seed": r["job"]["seed"],
                "collisions": r["job"]["environment"]["collisions_enabled"],
                "tx": len(r["tx"]),
                "scheduled": len(r["job"]["schedule"]),
                "originals": len(originals),
                "airtime_mean_s": float(np.mean(airtimes)) if airtimes else float("nan"),
                "G": g,
                "pdr": outcomes.get("ok", 0) / n_att,
                "collision": outcomes.get("collision", 0) / n_att,
                "half_duplex": outcomes.get("half_duplex", 0) / n_att,
                "weak": outcomes.get("weak", 0) / n_att,
                "aloha_pure": math.exp(-2 * g),
                "aloha_slotted": math.exp(-g),
                "pdr_text": text_out.get("ok", 0) / (sum(text_out.values()) or 1),
                "pdr_firmware": fw_out.get("ok", 0) / (sum(fw_out.values()) or 1),
                # CSMA z oknem podatności τ (slot CAD): kolizja, gdy inny start wypadnie w ±τ.
                "csma_theory": math.exp(-2 * rate_all * tau),
                "tx_rate": rate_all,
            }
        )
    rows.sort(key=lambda x: (x["n_nodes"], x["collisions"], x["rate"], x["seed"]))
    return {"rows": rows}


# --- skalowalność (T7) ---------------------------------------------------------------


def scale_report(campaign: Path) -> dict:
    rows = []
    for r in load_runs(campaign, "scale"):
        n = r["job"]["n_nodes"]
        texts = [t for t in r["tx"] if t.get("portnum") == "TEXT_MESSAGE_APP" and not t["is_rebroadcast"]]
        reach, rebro = [], []
        for t in texts:
            got = {a["rx_node"] for a in r["attempts"] if a["packet_id"] == t["packet_id"] and a["delivered"] == "True"}
            got.discard(t["tx_node"])
            reach.append(len(got) / (n - 1))
            rebro.append(sum(1 for x in r["tx"] if x["packet_id"] == t["packet_id"] and x["is_rebroadcast"]))
        loss = Counter(a.get("loss_reason") or "ok" for a in r["attempts"])
        total = sum(loss.values()) or 1
        med = np.array(r["links"]["median_rssi_db"], float)  # już ze stratą otoczenia i cieniowaniem
        sens = EnvironmentConfig(**r["job"]["environment"]).resolve_lora().sensitivity_dbm
        rows.append(
            {
                "n_nodes": n,
                "precompute_s": r["timing"]["precomputed"] - r["timing"]["t0"],
                "start_s": r["timing"]["running"] - r["timing"]["precomputed"],
                "stop_s": r["timing"]["stopped"] - r["timing"]["running"] - r["job"]["duration_s"],
                "texts_sent": len(texts),
                "texts_scheduled": len(r["job"]["schedule"]),
                "reach_mean": float(np.mean(reach)) if reach else float("nan"),
                "reach_p10": float(np.quantile(reach, 0.1)) if reach else float("nan"),
                "rebroadcasts_per_msg": float(np.mean(rebro)) if rebro else float("nan"),
                "tx_total": len(r["tx"]),
                "attempts": len(r["attempts"]),
                "mean_degree": float(np.sum(med >= sens) / n),
                "outcomes": {k: v / total for k, v in loss.items()},
            }
        )
    rows.sort(key=lambda x: x["n_nodes"])
    return {"rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("campaign", type=Path)
    ap.add_argument("clusters", type=Path)
    ap.add_argument("--runs", default="runs", help="podkatalog z przebiegami (np. runs_v1)")
    args = ap.parse_args()
    global RUNS_SUBDIR
    RUNS_SUBDIR = args.runs
    rep = {
        "routing": routing_report(args.campaign, args.clusters),
        "medium": medium_report(args.campaign),
        "scale": scale_report(args.campaign),
    }
    (args.campaign / ("report.json" if args.runs == "runs" else f"report_{args.runs}.json")).write_text(json.dumps(rep, indent=1, default=float), encoding="utf-8")
    slim = {k: v for k, v in rep["routing"].items() if k != "pair_rows"}
    print(json.dumps(slim, indent=1, default=lambda x: round(x, 3) if isinstance(x, float) else str(x)))
    for row in rep["medium"]["rows"] + rep["scale"]["rows"]:
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()})


if __name__ == "__main__":
    main()
