#!/usr/bin/env python3
"""Technocore uptime / health prober.

Technocore is flaky under load (502/503, slow reads). Nothing publishes its
uptime, so this probes a real read endpoint on an interval and logs one JSONL
line per probe: timestamp, HTTP status, latency, ok. `--report` aggregates the
log into uptime %, latency percentiles, status breakdown, and incident windows.

Usage:
  python3 health.py --once                 # single probe, append + print
  python3 health.py --interval 300         # loop: probe every 5 min (run under pm2)
  python3 health.py --report               # aggregate the log
Read-only, stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

BASE_URL = "https://technocore.chat"
PROBE_PATH = "/r/lobby?format=json&limit=1"  # a real read — what agents actually hit
USER_AGENT = "technocore-pulse-health/1.0"
DEFAULT_LOG = os.path.expanduser("~/.technocore-pulse/health.jsonl")
TIMEOUT = 15.0


def probe() -> dict:
    ts = datetime.now(timezone.utc).isoformat()
    req = urllib.request.Request(f"{BASE_URL}{PROBE_PATH}", headers={"User-Agent": USER_AGENT})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read(2048)
        return {"ts": ts, "status": 200, "latency_ms": round((time.monotonic() - start) * 1000), "ok": True}
    except HTTPError as e:
        return {"ts": ts, "status": e.code, "latency_ms": round((time.monotonic() - start) * 1000), "ok": False}
    except (URLError, OSError) as e:
        reason = "timeout" if "timed out" in str(e).lower() else "neterror"
        return {"ts": ts, "status": reason, "latency_ms": round((time.monotonic() - start) * 1000), "ok": False}


def append(log_path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def report(log_path: str) -> None:
    if not os.path.exists(log_path):
        print(f"no health log yet at {log_path}")
        return
    rows = [json.loads(l) for l in open(log_path, encoding="utf-8") if l.strip()]
    n = len(rows)
    if not n:
        print("health log is empty")
        return
    ok = sum(1 for r in rows if r.get("ok"))
    lat = sorted(r["latency_ms"] for r in rows if r.get("ok"))
    status_counts: dict = {}
    for r in rows:
        status_counts[str(r["status"])] = status_counts.get(str(r["status"]), 0) + 1
    # incident windows: runs of consecutive failures
    incidents = []
    run_start = None
    for r in rows:
        if not r.get("ok"):
            run_start = run_start or r["ts"]
        elif run_start:
            incidents.append((run_start, r["ts"]))
            run_start = None
    if run_start:
        incidents.append((run_start, "ongoing"))

    def pct(p: float) -> int:
        return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else 0

    print("Technocore health report")
    print(f"window {rows[0]['ts'][:16]} -> {rows[-1]['ts'][:16]} | {n} probes")
    print(f"uptime {round(100*ok/n, 2)}% ({ok}/{n} ok)")
    print(f"latency ok-probes: p50 {pct(0.5)}ms · p95 {pct(0.95)}ms · max {lat[-1] if lat else 0}ms")
    print(f"status: {status_counts}")
    print(f"incidents (consecutive-failure windows): {len(incidents)}")
    for s, e in incidents[-8:]:
        print(f"  {s[:19]} -> {e[:19] if e != 'ongoing' else 'ongoing'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=300.0)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        report(args.log)
        return 0
    if args.once:
        row = probe()
        append(args.log, row)
        print(json.dumps(row))
        return 0 if row["ok"] else 1

    print(f"health prober: every {args.interval:g}s -> {args.log}", flush=True)
    while True:
        row = probe()
        append(args.log, row)
        print(f"{row['ts']} status={row['status']} {row['latency_ms']}ms {'ok' if row['ok'] else 'FAIL'}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
