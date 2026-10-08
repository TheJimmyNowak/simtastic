"""Kampania testowa przez API aplikacji: wiele backendów, każdy prowadzi kolejne przebiegi.

Każdy przebieg: utworzenie scenariusza (POST /api/scenarios), harmonogram ruchu, precompute
ITM, start, odczekanie harmonogramu, stop, a potem eksport śladu z InfluxDB
(`packet_ota_tx`, `link_attempt`) do `<out>/runs/<id>.json`. Przebieg, którego wynik już
istnieje, jest pomijany, więc kampanię można wznowić.

    PYTHONPATH=backend .venv/bin/python scripts/validation/campaign.py plan.json \
        --out data/campaign --backends 8101,8102,8103
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
import traceback
from pathlib import Path

import httpx
import yaml
from influxdb_client import InfluxDBClient

from app.config import settings

log = logging.getLogger("campaign")


def influx_rows(
    client: InfluxDBClient, measurement: str, experiment_id: str, start: float, stop: float
) -> list[dict]:
    """Wiersze jednego przebiegu. Zakres czasu jest konieczny: powtórzony przebieg ma ten sam
    experiment_id co wcześniejszy."""
    flux = f'''
from(bucket: "{settings.influx_bucket}")
  |> range(start: {int(start)}, stop: {int(stop) + 1})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.experiment_id == "{experiment_id}")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
'''
    rows = []
    for table in client.query_api().query(flux, org=settings.influx_org):
        for rec in table.records:
            v = dict(rec.values)
            v["_time"] = rec.get_time().timestamp()
            for k in ("result", "table", "_start", "_stop", "_measurement"):
                v.pop(k, None)
            rows.append(v)
    rows.sort(key=lambda r: r["_time"])
    return rows


class Worker(threading.Thread):
    def __init__(self, port: int, jobs: queue.Queue, out: Path) -> None:
        super().__init__(name=f"worker-{port}", daemon=True)
        self.base = f"http://127.0.0.1:{port}"
        self.port = port
        self.jobs = jobs
        self.out = out
        self.http = httpx.Client(base_url=self.base, timeout=60)
        self.influx = InfluxDBClient(url=settings.influx_url, token=settings.influx_token, org=settings.influx_org)

    def wait_status(self, sid: str, wanted: set[str], timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.http.get(f"/api/scenarios/{sid}/status").json()
            if st["scenario_status"] in wanted:
                return st
            if st["scenario_status"] == "error":
                raise RuntimeError(f"{sid}: {st.get('error_message')}")
            time.sleep(2)
        raise TimeoutError(f"{sid}: status {wanted} not reached in {timeout}s")

    def run_job(self, job: dict) -> dict:
        rid = job["id"]
        t = {"t0": time.time()}
        env = {**job["environment"], "experiment_id": rid}
        resp = self.http.post(
            "/api/scenarios", json={"name": rid, "environment": env, "nodes": job["nodes"]}
        )
        resp.raise_for_status()
        sid = resp.json()["id"]
        sdir = settings.data_dir / "scenarios" / sid
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "traffic.yaml").write_text(
            yaml.safe_dump({"schedule": job["schedule"]}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self.http.post(f"/api/scenarios/{sid}/precompute-links").raise_for_status()
        self.wait_status(sid, {"links_ready"}, 900)
        t["precomputed"] = time.time()
        links = self.http.get(f"/api/scenarios/{sid}/links").json()
        self.http.post(f"/api/scenarios/{sid}/start", json={}).raise_for_status()
        self.wait_status(sid, {"running"}, 900)
        t["running"] = time.time()
        time.sleep(job["duration_s"])
        stop = self.http.post(f"/api/scenarios/{sid}/stop", timeout=180).json()
        t["stopped"] = time.time()
        time.sleep(3)  # zapis kolejki Influx
        result = {
            "job": job,
            "scenario_id": sid,
            "backend": self.port,
            "timing": t,
            "stop": stop,
            "links": {
                k: links[k]
                for k in ("node_ids", "median_rssi_db", "path_loss_median_db", "terrain_loss_db",
                          "building_loss_db", "link_shadowing_db", "distance_m", "node_placement")
            },
            "tx": influx_rows(self.influx, "packet_ota_tx", rid, t["t0"], time.time()),
            "attempts": influx_rows(self.influx, "link_attempt", rid, t["t0"], time.time()),
        }
        self.http.delete(f"/api/scenarios/{sid}")
        return result

    def run(self) -> None:
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                return
            path = self.out / "runs" / f"{job['id']}.json"
            started = time.time()
            try:
                result = self.run_job(job)
                path.write_text(json.dumps(result), encoding="utf-8")
                log.info(
                    "%s done on %s in %.0fs: %d tx, %d attempts",
                    job["id"], self.port, time.time() - started, len(result["tx"]), len(result["attempts"]),
                )
            except Exception as exc:
                log.error("%s failed on %s: %s", job["id"], self.port, exc)
                with (self.out / "failures.log").open("a") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {job['id']} {self.port}\n{traceback.format_exc()}\n")
                try:
                    sid = job["id"]
                    self.http.post(f"/api/scenarios/{sid}/stop", timeout=180)
                    self.http.delete(f"/api/scenarios/{sid}")
                except Exception:
                    pass
            finally:
                self.jobs.task_done()


def reexport(plan_path: Path, out: Path) -> None:
    influx = InfluxDBClient(url=settings.influx_url, token=settings.influx_token, org=settings.influx_org)
    for job in json.loads(plan_path.read_text(encoding="utf-8")):
        path = out / "runs" / f"{job['id']}.json"
        if not path.exists():
            continue
        r = json.loads(path.read_text(encoding="utf-8"))
        start, stop = r["timing"]["t0"], r["timing"]["stopped"] + 5
        before = len(r["tx"])
        r["tx"] = influx_rows(influx, "packet_ota_tx", job["id"], start, stop)
        r["attempts"] = influx_rows(influx, "link_attempt", job["id"], start, stop)
        path.write_text(json.dumps(r), encoding="utf-8")
        log.info("%s: %d -> %d tx", job["id"], before, len(r["tx"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--backends", required=True, help="porty backendów, np. 8101,8102")
    ap.add_argument("--only", default="", help="prefiks id przebiegów do wykonania")
    ap.add_argument(
        "--reexport",
        action="store_true",
        help="tylko ponowny eksport śladu z Influx dla zapisanych przebiegów planu (wg ich czasów)",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.reexport:
        reexport(args.plan, args.out)
        return

    (args.out / "runs").mkdir(parents=True, exist_ok=True)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    jobs: queue.Queue = queue.Queue()
    todo = [j for j in plan if j["id"].startswith(args.only) and not (args.out / "runs" / f"{j['id']}.json").exists()]
    for job in todo:
        jobs.put(job)
    log.info("%d przebiegów do wykonania (%d w planie)", len(todo), len(plan))
    workers = [Worker(int(p), jobs, args.out) for p in args.backends.split(",")]
    for w in workers:
        w.start()
        time.sleep(20)  # rozłożenie startów w czasie (precompute i provisioning)
    for w in workers:
        w.join()
    log.info("koniec kampanii")


if __name__ == "__main__":
    main()
