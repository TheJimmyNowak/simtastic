"""Dekoduje zrzut API lorastats.pl do jednego pliku z raportami odbioru, pozycjami i sąsiadami.

Wejście: katalog z plikami `<region>_PacketsRaw.json` i `<region>_Nodes.json` pobranymi z
https://lorastats.pl/API/<region>/<endpoint>/JSON. Pakiety zaszyfrowane domyślnym kluczem
kanału są odszyfrowywane (AES-128-CTR, jak w firmware). Wyjście: `dataset.json` w tym samym
katalogu.

    PYTHONPATH=backend .venv/bin/python scripts/validation/lorastats_dataset.py \
        data/lorastats/2026-10-08 --since 2026-10-07T17:35
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
from collections import Counter, defaultdict
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from meshtastic import mesh_pb2, portnums_pb2

# Domyślny klucz kanału Meshtastic (PSK „AQ==” rozwinięty do 128 bitów).
DEFAULT_KEY = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")
POSITION_APP = portnums_pb2.PortNum.POSITION_APP
NEIGHBORINFO_APP = portnums_pb2.PortNum.NEIGHBORINFO_APP


def decrypt(packet: mesh_pb2.MeshPacket) -> mesh_pb2.Data | None:
    """Odszyfrowuje `packet.encrypted` domyślnym kluczem; None, gdy wynik nie jest poprawnym Data."""
    nonce = struct.pack("<QI", packet.id, getattr(packet, "from")) + b"\0\0\0\0"
    dec = Cipher(algorithms.AES(DEFAULT_KEY), modes.CTR(nonce)).decryptor()
    plain = dec.update(packet.encrypted) + dec.finalize()
    data = mesh_pb2.Data()
    try:
        data.ParseFromString(plain)
    except Exception:
        return None
    if data.portnum == 0 or data.portnum not in portnums_pb2.PortNum.values():
        return None
    return data


def node_hex(num: int) -> str:
    return f"{num & 0xFFFFFFFF:08x}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump_dir", type=Path)
    ap.add_argument("--since", default="", help="pomija raporty starsze niż ten czas (lokalny, ISO)")
    ap.add_argument("--until", default="9999")
    args = ap.parse_args()

    reports: list[list] = []
    positions: dict[str, list[dict]] = defaultdict(list)
    neighbors: set[tuple[str, str]] = set()
    channel_hash: dict[str, Counter] = defaultdict(Counter)
    hop_start: dict[str, Counter] = defaultdict(Counter)
    seen_pos: set[tuple[str, int]] = set()

    for raw_file in sorted(args.dump_dir.glob("*_PacketsRaw.json")):
        region = raw_file.name.split("_")[0]
        rows = json.loads(raw_file.read_text(encoding="utf-8"))
        kept = 0
        for row in rows:
            t = row["Time"][:19]
            if not (args.since <= t < args.until):
                continue
            p = mesh_pb2.MeshPacket()
            try:
                p.ParseFromString(base64.b64decode(row["Data"]))
            except Exception:
                continue
            src = node_hex(getattr(p, "from"))
            gw = row["Gateway"]
            if p.HasField("decoded"):
                data = p.decoded
            elif p.encrypted:
                data = decrypt(p)
            else:
                data = None
            # Hash kanału ma sens tylko w pakietach zaszyfrowanych (w zdekodowanych bywa indeksem).
            if p.encrypted and p.channel:
                channel_hash[src][p.channel] += 1
            if p.hop_start:
                hop_start[src][p.hop_start] += 1
            reports.append(
                [
                    t,
                    region,
                    gw,
                    src,
                    node_hex(p.to),
                    p.id,
                    p.hop_limit,
                    p.hop_start,
                    p.relay_node,
                    round(p.rx_snr, 2),
                    p.rx_rssi,
                    data.portnum if data is not None else -1,
                ]
            )
            kept += 1
            if data is None:
                continue
            if data.portnum == POSITION_APP and (src, p.id) not in seen_pos:
                seen_pos.add((src, p.id))
                pos = mesh_pb2.Position()
                try:
                    pos.ParseFromString(data.payload)
                except Exception:
                    continue
                if not pos.latitude_i and not pos.longitude_i:
                    continue
                positions[src].append(
                    {
                        "lat_i": pos.latitude_i,
                        "lon_i": pos.longitude_i,
                        "alt": pos.altitude if pos.HasField("altitude") else None,
                        "bits": pos.precision_bits,
                        "src": mesh_pb2.Position.LocSource.Name(pos.location_source),
                    }
                )
            elif data.portnum == NEIGHBORINFO_APP:
                ni = mesh_pb2.NeighborInfo()
                try:
                    ni.ParseFromString(data.payload)
                except Exception:
                    continue
                for nb in ni.neighbors:
                    neighbors.add((src, node_hex(nb.node_id)))
        print(f"{region}: {kept} raportów")

    nodes_meta: dict[str, dict] = {}
    for nodes_file in sorted(args.dump_dir.glob("*_Nodes.json")):
        for n in json.loads(nodes_file.read_text(encoding="utf-8")):
            nodes_meta[n["NodeId"]] = {
                "short": n.get("ShortName"),
                "name": n.get("LongName"),
                "role": n.get("Role"),
                "hw": n.get("HwModel"),
            }

    out = {
        "since": args.since,
        "until": args.until,
        "report_columns": [
            "time", "region", "gateway", "from", "to", "packet_id", "hop_limit",
            "hop_start", "relay_node", "snr", "rssi", "portnum",
        ],
        "reports": reports,
        "positions": positions,
        "neighbors": sorted(neighbors),
        "channel_hash": {k: dict(v) for k, v in channel_hash.items()},
        "hop_start": {k: dict(v) for k, v in hop_start.items()},
        "nodes": nodes_meta,
    }
    suffix = ("_" + args.since.replace(":", "") if args.since else "") + (
        "_do" + args.until.replace(":", "") if args.until != "9999" else ""
    )
    path = args.dump_dir / f"dataset{suffix}.json"
    path.write_text(json.dumps(out), encoding="utf-8")
    print(f"{len(reports)} raportów, {len(positions)} węzłów z pozycją, {len(neighbors)} sąsiedztw -> {path}")


if __name__ == "__main__":
    main()
