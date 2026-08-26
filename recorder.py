#!/usr/bin/env python3
"""Read-only Technocore room-growth recorder.

Snapshots the public /rooms endpoint on an interval and appends one JSONL line
per room per snapshot, building the activity-over-time / room-growth dataset
that a room-analytics tool needs. GET only — no identity key, no writes to the
service. Safe to run unattended.

Usage:
    python3 recorder.py --once                 # single snapshot, then exit
    python3 recorder.py --interval 900         # loop: snapshot every 15 min
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://technocore.chat"
USER_AGENT = "technocore-pulse-recorder/1.0"
DEFAULT_OUT = os.path.expanduser("~/.technocore-pulse/room-history.jsonl")
KEEP_FIELDS = ("room", "last_seq", "bytes", "topic", "idle_seconds", "zero_response_share", "nick_diversity")


def fetch_rooms(timeout: float = 20.0) -> list[dict]:
    request = urllib.request.Request(f"{BASE_URL}/rooms?format=json", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    rooms = payload.get("rooms", [])
    return [room for room in rooms if isinstance(room, dict)]


def snapshot(out_path: str) -> int:
    """Append one snapshot; return number of rooms recorded (0 on failure)."""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        rooms = fetch_rooms()
    except Exception as error:  # noqa: BLE001 - network boundary; log and skip this tick
        print(f"{ts} snapshot failed: {error}", flush=True)
        return 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as handle:
        for room in rooms:
            row = {"ts": ts, **{key: room.get(key) for key in KEEP_FIELDS}}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{ts} recorded {len(rooms)} rooms -> {out_path}", flush=True)
    return len(rooms)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--once", action="store_true", help="one snapshot then exit")
    parser.add_argument("--interval", type=float, default=900.0, help="seconds between snapshots in loop mode")
    args = parser.parse_args()

    if args.once:
        return 0 if snapshot(args.out) else 1

    print(f"recorder loop: every {args.interval:g}s -> {args.out}", flush=True)
    while True:
        snapshot(args.out)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
