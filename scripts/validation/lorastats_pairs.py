"""Wybiera węzły z dokładną pozycją, zbiera dowody łączy nadawca → bramka i buduje klastry.

Kryteria są opisane w docs/validation_lorastats.md (precyzja 32 bity, węzeł stacjonarny,
bramki będące obserwatorami radiowymi, pary 200 m – 60 km). Wyjście: `clusters.json`
obok zbioru danych.

    PYTHONPATH=backend .venv/bin/python scripts/validation/lorastats_pairs.py \
        data/lorastats/2026-10-08/dataset_2026-10-07T1735.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from app.config import settings
from app.services.propagation.srtm_profile import elevation_m

# Hash kanału (domyślny klucz) → preset w symulatorze; ShortSlow nie ma presetu w backendzie.
PRESETS = {
    31: {"preset": "MEDIUM_FAST"},
    8: {"preset": "LONG_FAST"},
    112: {"preset": "SHORT_FAST"},
    119: {"preset": "custom", "custom_bandwidth_khz": 250, "custom_spreading_factor": 8},
}
PRESET_NAMES = {31: "MediumFast", 8: "LongFast", 112: "ShortFast", 119: "ShortSlow"}
# Config.DeviceConfig.Role → rola w symulatorze. Kampania z 8.10.2026 odwzorowywała
# ROUTER_LATE jako ROUTER i ten zapis zostaje, żeby wyniki z pracy dało się odtworzyć.
# Przestarzałe ROUTER_CLIENT i REPEATER firmware 2.7.15 zapisuje jako CLIENT, więc też ROUTER.
ROLE_MAP = {1: "CLIENT_MUTE", 2: "ROUTER", 3: "ROUTER", 4: "ROUTER", 11: "ROUTER"}
PL_BBOX = (49.0, 54.9, 14.1, 24.2)
MIN_PAIR_M, MAX_PAIR_M = 200.0, 60_000.0
MIN_RELAYED_FOR_NO_LINK = 5
DEFAULT_AGL_M = 6.0
MAX_CLUSTER = 10


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def good_nodes(ds: dict) -> dict[str, dict]:
    """Węzły z pełną precyzją we wszystkich raportach, stacjonarne, w Polsce."""
    out: dict[str, dict] = {}
    for nid, pos_list in ds["positions"].items():
        if not pos_list or any(p["bits"] != 32 for p in pos_list):
            continue
        lats = [p["lat_i"] * 1e-7 for p in pos_list]
        lons = [p["lon_i"] * 1e-7 for p in pos_list]
        center = (median(lats), median(lons))
        spread = max(haversine_m(center, (la, lo)) for la, lo in zip(lats, lons))
        if spread > 30:
            continue
        if not (PL_BBOX[0] <= center[0] <= PL_BBOX[1] and PL_BBOX[2] <= center[1] <= PL_BBOX[3]):
            continue
        lat_i, lon_i = round(center[0] * 1e7), round(center[1] * 1e7)
        if lat_i % 1000 == 0 and lon_i % 1000 == 0:
            continue  # „okrągłe” współrzędne — wpisane z grubsza
        hashes = Counter({int(k): v for k, v in ds["channel_hash"].get(nid, {}).items()})
        ch = hashes.most_common(1)[0][0] if hashes else None
        if ch not in PRESETS:
            continue
        ground = elevation_m(settings.srtm_cache_dir, *center)
        alts = [p["alt"] for p in pos_list if p["alt"] is not None]
        agl = median(alts) - ground if alts else None
        hop = Counter({int(k): v for k, v in ds["hop_start"].get(nid, {}).items()})
        meta = ds["nodes"].get(nid, {})
        out[nid] = {
            "lat": center[0],
            "lon": center[1],
            "agl_m": round(agl, 1) if agl is not None and 1.5 <= agl <= 80 else DEFAULT_AGL_M,
            "agl_from_report": agl is not None and 1.5 <= agl <= 80,
            "location_source": Counter(p["src"] for p in pos_list).most_common(1)[0][0],
            "channel_hash": ch,
            "hop_limit": min(7, hop.most_common(1)[0][0]) if hop else 3,
            "role": ROLE_MAP.get(meta.get("role"), "CLIENT"),
            "role_raw": meta.get("role"),
            "name": meta.get("short") or nid[-4:],
            "long_name": meta.get("name"),
        }
    return out


def first_copies(ds: dict) -> dict[tuple[str, str], list[dict]]:
    """Pierwszy raport każdej bramki dla każdego pakietu: {(nadawca, bramka): [odbiory]}."""
    cols = {c: i for i, c in enumerate(ds["report_columns"])}
    first: dict[tuple[str, int], list] = {}
    for r in ds["reports"]:
        key = (r[cols["gateway"]], r[cols["packet_id"]])
        if key not in first or r[cols["time"]] < first[key][cols["time"]]:
            first[key] = r
    pairs: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in first.values():
        src, gw = r[cols["from"]], r[cols["gateway"]]
        if src == gw or not r[cols["hop_start"]]:
            continue  # bez hop_start (stary firmware) nie da się policzyć skoków
        hops = r[cols["hop_start"]] - r[cols["hop_limit"]]
        if hops < 0:
            continue
        pairs[(src, gw)].append(
            {
                "hops": hops,
                "relay": r[cols["relay_node"]],
                "snr": r[cols["snr"]],
                "rssi": r[cols["rssi"]],
                # relay_node = 0: firmware sprzed 2.5 nie wypełnia pola. Przy hops = 0 i innym
                # relay pakiet przekazał router „zero-hop” (ulubiony węzeł, fw 2.6+).
                "direct": hops == 0 and r[cols["relay_node"]] in (0, int(src[-2:], 16)),
                "portnum": r[cols["portnum"]],
            }
        )
    return pairs


def radio_observers(ds: dict) -> set[str]:
    """Bramki, które raportują odbiory radiowe (nie tylko pakiety z MQTT/innych bramek)."""
    cols = {c: i for i, c in enumerate(ds["report_columns"])}
    total, direct, zero = Counter(), Counter(), Counter()
    for r in ds["reports"]:
        gw = r[cols["gateway"]]
        total[gw] += 1
        if r[cols["hop_start"]] and r[cols["hop_start"]] == r[cols["hop_limit"]]:
            direct[gw] += 1
        if r[cols["hop_limit"]] == 0:
            zero[gw] += 1
    return {
        gw
        for gw, n in total.items()
        if n >= 50 and direct[gw] / n >= 0.01 and zero[gw] / n <= 0.5
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument(
        "--meta",
        type=Path,
        help="zbiór (np. pełne 24 h), z którego brane są pozycje, hash kanału i hop_start węzłów; "
        "dowody łączy pochodzą zawsze z `dataset`",
    )
    args = ap.parse_args()
    ds = json.loads(args.dataset.read_text(encoding="utf-8"))
    meta = json.loads(args.meta.read_text(encoding="utf-8")) if args.meta else ds

    nodes = good_nodes(meta)
    observers = radio_observers(ds) & set(nodes)
    copies = first_copies(ds)
    neighbor_set = {tuple(p) for p in ds["neighbors"]}
    print(f"{len(nodes)} węzłów z pełną precyzją, {len(observers)} bramek-obserwatorów")

    evidence: dict[tuple[str, str], dict] = {}
    for (src, gw), obs in copies.items():
        if src not in nodes or gw not in observers:
            continue
        a, b = nodes[src], nodes[gw]
        if a["channel_hash"] != b["channel_hash"]:
            continue
        dist = haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        if not (MIN_PAIR_M <= dist <= MAX_PAIR_M):
            continue
        n_direct = sum(o["direct"] for o in obs)
        neighbor = (src, gw) in neighbor_set or (gw, src) in neighbor_set
        if n_direct or neighbor:
            link = True
        elif len(obs) >= MIN_RELAYED_FOR_NO_LINK:
            link = False
        else:
            continue
        direct_obs = [o for o in obs if o["direct"]]
        evidence[(src, gw)] = {
            "tx": src,
            "rx": gw,
            "distance_m": round(dist, 1),
            "link": link,
            "n_packets": len(obs),
            "n_direct": n_direct,
            "neighbor_info": neighbor,
            "rssi_direct": [o["rssi"] for o in direct_obs],
            "snr_direct": [o["snr"] for o in direct_obs],
            "hops": [o["hops"] for o in obs],
            "relays": [o["relay"] for o in obs if o["hops"] > 0],
        }
    print(f"{len(evidence)} par z dowodem: {sum(e['link'] for e in evidence.values())} z łączem")

    # Zachłanne, rozłączne klastry: do klastra trafia węzeł z największą liczbą par z dowodem.
    adj: dict[str, Counter] = defaultdict(Counter)
    for src, gw in evidence:
        adj[src][gw] += 1
        adj[gw][src] += 1
    free = set(adj)
    clusters = []
    while free:
        seed = max(free, key=lambda n: (sum(adj[n][m] for m in free), n))
        if sum(adj[seed][m] for m in free) == 0:
            break
        members = [seed]
        while len(members) < MAX_CLUSTER:
            cands = [
                (sum(adj[c][m] for m in members), c)
                for c in free - set(members)
                if all(
                    haversine_m((nodes[c]["lat"], nodes[c]["lon"]), (nodes[m]["lat"], nodes[m]["lon"]))
                    <= MAX_PAIR_M
                    for m in members
                )
            ]
            cands = [x for x in cands if x[0] > 0]
            if not cands:
                break
            members.append(max(cands)[1])
        free -= set(members)
        pairs = [e for (s, g), e in evidence.items() if s in members and g in members]
        if len(members) >= 4 and len(pairs) >= 5:
            clusters.append({"members": members, "pairs": pairs})

    out_clusters = []
    for k, c in enumerate(clusters):
        ch = nodes[c["members"][0]]["channel_hash"]
        lat = median(nodes[m]["lat"] for m in c["members"])
        lon = median(nodes[m]["lon"] for m in c["members"])
        out_clusters.append(
            {
                "id": f"k{k}",
                "preset": PRESET_NAMES[ch],
                "environment": PRESETS[ch],
                "center": [lat, lon],
                "nodes": [{"id": m, **nodes[m]} for m in c["members"]],
                "gateways": sorted({p["rx"] for p in c["pairs"]}),
                "pairs": c["pairs"],
            }
        )
        n_link = sum(p["link"] for p in c["pairs"])
        print(
            f"k{k}: {PRESET_NAMES[ch]:10s} ({lat:.2f}, {lon:.2f}) węzłów {len(c['members'])}, "
            f"par {len(c['pairs'])} (łącze {n_link}, brak {len(c['pairs']) - n_link})"
        )
    # Pary NeighborInfo między dokładnymi węzłami: tylko dowód łącza (bez par „brak łącza”),
    # więc służą wyłącznie do oceny czułości modelu, nie do AUC.
    neighbor_pairs = []
    for a_id, b_id in sorted({tuple(sorted(p)) for p in neighbor_set}):
        if a_id == b_id or a_id not in nodes or b_id not in nodes:
            continue
        a, b = nodes[a_id], nodes[b_id]
        dist = haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        if a["channel_hash"] == b["channel_hash"] and MIN_PAIR_M <= dist <= MAX_PAIR_M:
            neighbor_pairs.append({"tx": a_id, "rx": b_id, "distance_m": round(dist, 1), "link": True})

    path = args.dataset.with_name(args.dataset.stem.replace("dataset", "clusters") + ".json")
    path.write_text(
        json.dumps(
            {
                "dataset": args.dataset.name,
                "nodes": nodes,
                "gateway_pairs": list(evidence.values()),
                "neighbor_pairs": neighbor_pairs,
                "clusters": out_clusters,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"-> {path}")


if __name__ == "__main__":
    main()
