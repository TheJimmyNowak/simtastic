"""Buduje plan kampanii (listę przebiegów) dla `campaign.py`.

Część „routing”: klastry z lorastats_pairs.py × warianty modelu łączy × seedy.
Część „medium”: syntetyczna sieć, w której każdy węzeł słyszy każdego, z rosnącym
obciążeniem — sprawdzenie modelu kolizji względem teorii ALOHA.

    PYTHONPATH=backend .venv/bin/python scripts/validation/make_plan.py routing \
        data/lorastats/2026-10-08/clusters_2026-10-07T1735.json -o data/campaign/plan_routing.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from app.config import settings
from app.services.propagation.building_index import BuildingIndex
from app.services.propagation.building_model import resolve_node_mount, wgs84_to_epsg2180
from app.services.propagation.gugik import BuildingProvider, terrain_provider

# Kalibracja: calibrate.py na zbiorze kalibracyjnym (raporty sprzed 7.10.2026 17:35);
# kampania używa jej bez dopasowywania. Ustawiana w main() z pliku --calibration.
CALIBRATED = {"clutter_loss_db": float("nan"), "link_shadowing_sigma_db": float("nan"),
              "node_installation_sigma_db": float("nan")}
PHYSICAL = {"clutter_loss_db": 0.0, "link_shadowing_sigma_db": 0.0, "node_installation_sigma_db": 0.0}
# Model mieszaniny z kalibracji (część par zablokowana): wariant wrażliwości T2, nie główny.
MIXTURE = {"clutter_loss_db": float("nan"), "link_shadowing_sigma_db": float("nan"), "link_blocked_fraction": float("nan")}
# Fading kolejnych pakietów zmierzony na łączach stacjonarnych (wcześniej przyjęte 8 dB).
FADING_SIGMA_DB = 1.0
SEEDS = list(range(1, 11))
ABLATION_SEEDS = SEEDS[:5]
PROBE_GAP_S = 5.0
FIRST_PROBE_S = 20.0
ROUNDS = 3
DRAIN_S = 45.0


def cluster_nodes(cluster: dict, index: BuildingIndex, buildings: bool) -> list[dict]:
    """Węzły scenariusza. Wysokość z raportu (n.p.m.) zawiera już budynek, więc dla węzła
    w obrysie budynku maszt liczony jest od dachu; bez budynków węzeł dostaje tę samą
    całkowitą wysokość nad terenem."""
    out = []
    for n in cluster["nodes"]:
        ground = terrain_provider.elevation(n["lat"], n["lon"])
        mount = resolve_node_mount(index, n["lat"], n["lon"], ground, 0.0)
        building_h = mount.surface_z_m - ground if mount.placement == "rooftop" else 0.0
        agl = n["agl_m"]
        if building_h and n["agl_from_report"]:
            mast = max(1.0, agl - building_h)
        else:
            mast = agl
        out.append(
            {
                "id": n["id"],
                "name": n["name"],
                "lat": n["lat"],
                "lon": n["lon"],
                "agl_m": round(mast if buildings else mast + building_h, 1),
                "hop_limit": n["hop_limit"],
                "role": n["role"],
            }
        )
    return out


def probe_schedule(node_ids: list[str], gateway: str) -> tuple[list[dict], float]:
    """ROUNDS rund broadcastów ze wszystkich węzłów co PROBE_GAP_S, potem traceroute do bramki."""
    sched = []
    t = FIRST_PROBE_S
    for r in range(ROUNDS):
        for nid in node_ids:
            sched.append({"delay_s": t, "action": "broadcast", "from": nid, "text": f"probe {r} {nid}", "want_ack": False})
            t += PROBE_GAP_S
    for nid in node_ids:
        if nid != gateway:
            sched.append({"delay_s": t, "action": "traceroute", "from": nid, "to": gateway})
            t += PROBE_GAP_S
    return sched, t + DRAIN_S


def routing_plan(clusters_path: Path) -> list[dict]:
    data = json.loads(clusters_path.read_text(encoding="utf-8"))
    index = BuildingIndex(settings.buildings_index_path)
    jobs = []
    for c in data["clusters"]:
        lat_lon = [(n["lat"], n["lon"]) for n in c["nodes"]]
        BuildingProvider(index).ensure([(*a, *b) for a in lat_lon for b in lat_lon if a < b])
        gw_counts: dict[str, int] = {}
        for p in c["pairs"]:
            gw_counts[p["rx"]] = gw_counts.get(p["rx"], 0) + 1
        main_gw = max(gw_counts, key=lambda g: (gw_counts[g], g))
        nodes_b = cluster_nodes(c, index, buildings=True)
        nodes_nb = cluster_nodes(c, index, buildings=False)
        x, y = wgs84_to_epsg2180(*c["center"])
        has_buildings = bool(index.query(x - 2000, x + 2000, y - 2000, y + 2000))
        sched, duration = probe_schedule([n["id"] for n in nodes_b], main_gw)

        def job(variant: str, model: dict, seed: int, rep: int = 0, **extra) -> dict:
            env = {
                **c["environment"],
                **model,
                "fading_sigma_db": FADING_SIGMA_DB,
                "random_seed": seed,
                "link_shadowing_seed": seed,
                "collisions_enabled": extra.pop("collisions", True),
                "building_model_enabled": extra.pop("buildings", True),
            }
            return {
                "id": f"r-{c['id']}-{variant}-s{seed}-r{rep}",
                "kind": "routing",
                "cluster": c["id"],
                "variant": variant,
                "seed": seed,
                "rep": rep,
                "main_gateway": main_gw,
                "environment": env,
                "nodes": extra.pop("nodes", nodes_b),
                "schedule": sched,
                "duration_s": duration,
            }

        for s in SEEDS:
            jobs.append(job("phys", PHYSICAL, s))
            jobs.append(job("cal", CALIBRATED, s))
        for s in ABLATION_SEEDS:
            jobs.append(job("calnc", CALIBRATED, s, collisions=False))
            jobs.append(job("mix", MIXTURE, s))
        for rep in (1, 2):
            jobs.append(job("cal", CALIBRATED, 1, rep))
            jobs.append(job("phys", PHYSICAL, 1, rep))
        if has_buildings:
            # Kalibracja jest liczona z budynkami, więc wariant bez nich sprawdza, ile
            # wnosi sama warstwa LoD1 (przy tej samej stracie otoczenia).
            for s in ABLATION_SEEDS:
                jobs.append(job("calnb", CALIBRATED, s, buildings=False, nodes=nodes_nb))
    # Najpierw podstawowe warianty wszystkich klastrów, potem ablacje i powtórzenia.
    order = {"phys": 0, "cal": 0, "mix": 1, "calnc": 1, "calnb": 2}
    jobs.sort(key=lambda j: (j["rep"] > 0, order[j["variant"]], j["seed"], j["cluster"]))
    return jobs


def medium_plan() -> list[dict]:
    """Syntetyczny test kolizji: N węzłów w promieniu 300 m (każdy słyszy każdego),
    hop_limit 0 (bez retransmisji), broadcasty w chwilach z procesu Poissona."""
    jobs = []
    center = (50.0413, 21.9990)  # Rzeszów, płaski teren
    for n_nodes in (10, 20):
        nodes = []
        for i in range(n_nodes):
            ang = 2 * math.pi * i / n_nodes
            nodes.append(
                {
                    "id": f"m{i:02d}",
                    "name": f"m{i:02d}",
                    "lat": center[0] + 0.0027 * math.sin(ang),
                    "lon": center[1] + 0.0042 * math.cos(ang),
                    "agl_m": 10.0,
                    "hop_limit": 0,
                    "role": "CLIENT",
                }
            )
        # Łączna intensywność zgłoszeń [pakiety/s] w całej sieci.
        for rate in (0.25, 0.5, 1.0, 2.0, 4.0):
            for seed in (1, 2):
                rng = random.Random(seed * 1000 + n_nodes)
                t, sched = 15.0, []
                horizon = 15.0 + 180.0
                while True:
                    t += rng.expovariate(rate)
                    if t > horizon:
                        break
                    src = nodes[rng.randrange(n_nodes)]["id"]
                    sched.append({"delay_s": round(t, 3), "action": "broadcast", "from": src, "text": "x" * 20, "want_ack": False})
                for coll in (True, False):
                    jobs.append(
                        {
                            "id": f"m-n{n_nodes}-l{rate}-s{seed}-{'c' if coll else 'nc'}",
                            "kind": "medium",
                            "n_nodes": n_nodes,
                            "rate": rate,
                            "seed": seed,
                            "environment": {
                                "preset": "MEDIUM_FAST",
                                "fading_sigma_db": 0.0,
                                "random_seed": seed,
                                "collisions_enabled": coll,
                                "building_model_enabled": False,
                            },
                            "nodes": nodes,
                            "schedule": sched,
                            "duration_s": horizon + 30.0,
                        }
                    )
    return jobs


def sweep_plan(clusters_path: Path) -> list[dict]:
    """T6: czułość routingu na stratę otoczenia (σ cieniowania 20 dB, 3 seedy)."""
    jobs = []
    base = CALIBRATED["clutter_loss_db"]
    for clutter in (max(0.0, base - 10.0), max(0.0, base - 5.0), base + 10.0):
        model = {**CALIBRATED, "clutter_loss_db": round(clutter, 1)}
        for j in routing_plan(clusters_path):
            if j["variant"] == "cal" and j["rep"] == 0 and j["seed"] <= 3:
                j = json.loads(json.dumps(j))
                j["environment"].update(model)
                j["variant"] = f"clut{int(clutter)}"
                j["id"] = j["id"].replace("-cal-", f"-clut{int(clutter)}-")
                jobs.append(j)
    return jobs


def scale_plan() -> list[dict]:
    """T7: skalowalność aplikacji — N węzłów losowo na 10 × 10 km, zalewanie wiadomościami."""
    jobs = []
    center = (50.0413, 21.9990)
    for n_nodes in (10, 25, 50, 80):
        rng = random.Random(n_nodes)
        nodes = [
            {
                "id": f"s{i:02d}",
                "name": f"s{i:02d}",
                "lat": center[0] + rng.uniform(-0.045, 0.045),
                "lon": center[1] + rng.uniform(-0.07, 0.07),
                "agl_m": 6.0,
                "hop_limit": 3,
                "role": "CLIENT",
            }
            for i in range(n_nodes)
        ]
        # Jedna wiadomość co 6 s, nadawcy po kolei; 30 wiadomości niezależnie od N.
        sched = [
            {"delay_s": 20.0 + 6.0 * k, "action": "broadcast", "from": nodes[k % n_nodes]["id"], "text": f"flood {k}", "want_ack": False}
            for k in range(30)
        ]
        jobs.append(
            {
                "id": f"s-n{n_nodes}",
                "kind": "scale",
                "n_nodes": n_nodes,
                "environment": {
                    "preset": "MEDIUM_FAST",
                    **CALIBRATED,
                    "fading_sigma_db": FADING_SIGMA_DB,
                    "random_seed": 1,
                    "link_shadowing_seed": 1,
                    "collisions_enabled": True,
                    "building_model_enabled": True,
                },
                "nodes": nodes,
                "schedule": sched,
                "duration_s": 20.0 + 6.0 * 30 + 45.0,
            }
        )
    return jobs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["routing", "medium", "sweep", "scale"])
    ap.add_argument("clusters", type=Path, nargs="?")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--calibration", type=Path, help="calibration.json z calibrate.py")
    args = ap.parse_args()
    if args.calibration:
        calibration = json.loads(args.calibration.read_text(encoding="utf-8"))
        fit = calibration["fit"]
        mix = calibration.get("fits", {}).get("mixture")
        if mix:
            MIXTURE.update(
                clutter_loss_db=round(mix["clutter_loss_db"], 1),
                link_shadowing_sigma_db=round(mix["link_shadowing_sigma_db"], 1),
                link_blocked_fraction=round(mix["link_blocked_fraction"], 3),
            )
        CALIBRATED.update(
            clutter_loss_db=round(fit["clutter_loss_db"], 1),
            link_shadowing_sigma_db=round(fit["link_shadowing_sigma_db"], 1),
            node_installation_sigma_db=round(fit.get("node_installation_sigma_db", 0.0), 1),
        )
    elif args.kind in ("routing", "sweep", "scale"):
        ap.error("--calibration is required for this plan")
    jobs = {
        "routing": lambda: routing_plan(args.clusters),
        "medium": medium_plan,
        "sweep": lambda: sweep_plan(args.clusters),
        "scale": scale_plan,
    }[args.kind]()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
    est = sum(j["duration_s"] + 150 for j in jobs) / 3600
    print(f"{len(jobs)} przebiegów, ok. {est:.1f} h pracy jednego backendu -> {args.out}")


if __name__ == "__main__":
    main()
