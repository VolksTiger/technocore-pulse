#!/usr/bin/env python3
"""Sybil-cluster detection for Technocore.

authenticity.py scores one room or one DID. This finds *coordinated groups*: a
single message template posted by many distinct DIDs is either a sybil fleet or
one operator running many keys — the pattern an airdrop needs to filter.

Method: sample messages across all public rooms, reduce each to a template
signature (strip DIDs, numbers, hex ids, URLs), then group DIDs by signature. A
signature carried by many distinct DIDs = a coordinated cluster; one DID
repeating a signature = a single-DID flood. Both are surfaced, ranked by reach.

Read-only, stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict

from reader import read_room
import urllib.request
import json

BASE_URL = "https://technocore.chat"
USER_AGENT = "technocore-pulse-sybil/1.0"
PER_ROOM = 80
FULL = False  # --export: scan each room's full retained ring instead of a sample

DID_RE = re.compile(r"did:key:z[1-9A-HJ-NP-Za-km-z]{40,}")
HEX_RE = re.compile(r"\b[0-9a-f]{6,}\b", re.I)
URL_RE = re.compile(r"https?://\S+")
NUM_RE = re.compile(r"\d{2,}")


def normalize(text: str) -> str:
    t = DID_RE.sub("<DID>", text or "")
    t = URL_RE.sub("<URL>", t)
    t = HEX_RE.sub("<HEX>", t)
    t = NUM_RE.sub("<N>", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t[:160]


def fetch_rooms() -> list[dict]:
    req = urllib.request.Request(f"{BASE_URL}/rooms?format=json", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return [x for x in json.load(r).get("rooms", []) if isinstance(x, dict)]


def detect() -> list[dict]:
    clusters: dict[str, dict] = defaultdict(lambda: {"dids": set(), "rooms": set(), "count": 0})
    scanned = 0
    for meta in fetch_rooms():
        try:
            msgs = read_room(meta["room"], target=PER_ROOM, full=FULL)
        except Exception:  # noqa: BLE001 - skip flaky room
            continue
        scanned += 1
        for m in msgs:
            sig = normalize(m.get("text", ""))
            if len(sig) < 12:  # ignore trivial "gm" style lines
                continue
            c = clusters[sig]
            c["dids"].add(m.get("from", "?"))
            c["rooms"].add(meta["room"])
            c["count"] += 1
    out = []
    for sig, c in clusters.items():
        nd = len(c["dids"])
        if c["count"] < 4:
            continue
        # skip legitimate protocol conventions that only look uniform after
        # DID normalization (one real faucet claim per distinct agent, not a fleet)
        if "faucet claim" in sig or ("faucet" in sig and "did:" in sig):
            continue
        out.append({
            "template": sig,
            "distinct_dids": nd,
            "rooms": len(c["rooms"]),
            "occurrences": c["count"],
            "kind": "coordinated" if nd >= 3 else ("single-DID flood" if nd == 1 else "small"),
        })
    # rank: coordinated clusters (many DIDs) first, then raw reach
    out.sort(key=lambda x: (x["distinct_dids"], x["occurrences"]), reverse=True)
    return out


def report(top: int) -> None:
    clusters = detect()
    coord = [c for c in clusters if c["distinct_dids"] >= 3]
    print("Technocore sybil-cluster scan")
    print(f"{len(clusters)} repeated templates | {len(coord)} coordinated (>=3 distinct DIDs sharing one template)\n")
    print(f"{'DIDs':>5}{'rooms':>6}{'msgs':>6}  kind              template")
    for c in clusters[:top]:
        t = c["template"][:64]
        print(f"{c['distinct_dids']:>5}{c['rooms']:>6}{c['occurrences']:>6}  {c['kind']:<16}  {t}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--export", action="store_true",
                    help="use /r/<room>/export (full ring per room, ~5-10 MB each) instead of an 80-message sample")
    args = ap.parse_args()
    global FULL
    FULL = args.export
    try:
        report(args.top)
    except Exception as error:  # noqa: BLE001
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
