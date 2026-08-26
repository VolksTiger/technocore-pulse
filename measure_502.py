#!/usr/bin/env python3
"""Measure 502/timeout rates for large vs small Technocore room reads.

Answers the open question: do big reads (limit=200) fail more than small ones
(limit=50) on a busy room, and does a 200->100->50 downshift recover them?

Method: alternating paired reads of the busiest public room, spaced to stay
gentle on the server, recording HTTP status / timeout / latency per limit, plus
whether a downshift retry recovered a failed limit=200 read. Stdlib only.

Usage:
    python3 measure_502.py --room lobby --pairs 200 --spacing 9 --out results_502.json
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

BASE_URL = "https://technocore.chat"
USER_AGENT = "technocore-pulse-measure/1.0"


def timed_read(room: str, limit: int, timeout: float = 15.0) -> dict:
    """One read. Returns {ok, status, timeout, latency_ms}."""
    url = f"{BASE_URL}/r/{room}?format=json&limit={limit}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        return {"ok": True, "status": 200, "timeout": False, "latency_ms": round((time.monotonic() - start) * 1000)}
    except HTTPError as error:
        return {"ok": False, "status": error.code, "timeout": False, "latency_ms": round((time.monotonic() - start) * 1000)}
    except (URLError, TimeoutError) as error:
        is_timeout = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
        return {"ok": False, "status": None, "timeout": is_timeout, "latency_ms": round((time.monotonic() - start) * 1000)}


def blank_tally() -> dict:
    return {"attempts": 0, "ok": 0, "http_502": 0, "other_error": 0, "timeout": 0, "latency_ms": []}


def record(tally: dict, result: dict) -> None:
    tally["attempts"] += 1
    if result["ok"]:
        tally["ok"] += 1
        tally["latency_ms"].append(result["latency_ms"])
    elif result["status"] == 502:
        tally["http_502"] += 1
    elif result["timeout"]:
        tally["timeout"] += 1
    else:
        tally["other_error"] += 1


def pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def summarize(tally: dict) -> dict:
    lat = sorted(tally["latency_ms"])
    return {
        "attempts": tally["attempts"],
        "ok": tally["ok"],
        "success_pct": pct(tally["ok"], tally["attempts"]),
        "http_502": tally["http_502"],
        "http_502_pct": pct(tally["http_502"], tally["attempts"]),
        "timeout": tally["timeout"],
        "timeout_pct": pct(tally["timeout"], tally["attempts"]),
        "other_error": tally["other_error"],
        "median_latency_ms": lat[len(lat) // 2] if lat else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", default="lobby", help="room to probe (default: busiest public room)")
    parser.add_argument("--pairs", type=int, default=200, help="number of paired reads")
    parser.add_argument("--spacing", type=float, default=9.0, help="seconds between pairs")
    parser.add_argument("--out", default="results_502.json")
    args = parser.parse_args()

    big = blank_tally()
    small = blank_tally()
    downshift = {"triggered": 0, "recovered_at_100": 0, "recovered_at_50": 0, "still_failed": 0}
    started = datetime.now(timezone.utc)

    for i in range(args.pairs):
        # Alternate order each pair so neither limit is systematically favored by burst timing.
        order = [200, 50] if i % 2 == 0 else [50, 200]
        for limit in order:
            result = timed_read(args.room, limit)
            record(big if limit == 200 else small, result)
            if limit == 200 and not result["ok"]:
                downshift["triggered"] += 1
                if timed_read(args.room, 100)["ok"]:
                    downshift["recovered_at_100"] += 1
                elif timed_read(args.room, 50)["ok"]:
                    downshift["recovered_at_50"] += 1
                else:
                    downshift["still_failed"] += 1
            time.sleep(0.5)
        if i < args.pairs - 1:
            time.sleep(args.spacing)
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{args.pairs} pairs done", flush=True)

    ended = datetime.now(timezone.utc)
    recovered = downshift["recovered_at_100"] + downshift["recovered_at_50"]
    report = {
        "room": args.room,
        "utc_window": {"start": started.isoformat(), "end": ended.isoformat()},
        "pairs": args.pairs,
        "limit_200": summarize(big),
        "limit_50": summarize(small),
        "downshift_200_100_50": {
            **downshift,
            "recovery_pct": pct(recovered, downshift["triggered"]) if downshift["triggered"] else None,
        },
        "method": "alternating paired reads of one room, limit=200 vs limit=50, ~"
        f"{args.spacing:g}s spacing; on a failed 200 read, retry 100 then 50 and record recovery",
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    print("\n=== 502 measurement ===")
    print(f"room {args.room} | {args.pairs} pairs | {started.strftime('%H:%M')}-{ended.strftime('%H:%MZ')}")
    print(f"limit=200: {big['ok']}/{big['attempts']} ok ({report['limit_200']['success_pct']}%), "
          f"{big['http_502']} x502 ({report['limit_200']['http_502_pct']}%), {big['timeout']} timeout")
    print(f"limit= 50: {small['ok']}/{small['attempts']} ok ({report['limit_50']['success_pct']}%), "
          f"{small['http_502']} x502 ({report['limit_50']['http_502_pct']}%), {small['timeout']} timeout")
    ds = report["downshift_200_100_50"]
    print(f"downshift: {ds['triggered']} triggered, {recovered} recovered ({ds['recovery_pct']}%)")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
