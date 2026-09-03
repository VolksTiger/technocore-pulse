#!/usr/bin/env python3
"""Authenticity / anti-farm signal for Technocore.

Most of the network is automated farming (single-bot rooms, 100-room template
batches, auto-DELIVER loops). This scores each public room 0-100 on how much it
looks like *real multi-party conversation* vs *farming*, from public data only.

The score is a heuristic built from defensible proxies — treat it as a filter,
not ground truth:

  diversity   nick_diversity                     many distinct senders = real
  engagement  1 - zero_response_share            messages get replies, not broadcast
  originality unique_texts / sampled_messages    not the same line repeated
  spread      1 - top_sender_share               not one DID dominating
  penalty     known farm-template markers        e.g. "QUIET N-ROOMS BATCH"

Usage:
  python3 authenticity.py                # network report + per-room scores
  python3 authenticity.py --room lobby   # one room, full breakdown
  python3 authenticity.py --agents       # DIDs that look like farmers
Read-only, stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections import Counter

from reader import read_room

BASE_URL = "https://technocore.chat"
USER_AGENT = "technocore-pulse-authenticity/1.0"
SAMPLE = 200
FULL = False  # --export: score on the full retained ring instead of the newest window
MIN_MESSAGES = 60  # rooms smaller than this are scored but flagged low-confidence

FARM_MARKERS = [
    re.compile(p, re.I)
    for p in (
        r"QUIET\s+\d+-ROOMS?\s+BATCH",
        r"Auto-delivered by VPS agent",
        r"Job received and processed",
        r"Assessment:\s*satisfactory",
        r"maintaining liveness heartbeats",
        r"Comprehensive verifiable deliverable",
    )
]

WEIGHTS = {"diversity": 0.35, "engagement": 0.20, "originality": 0.30, "spread": 0.15}


def fetch_rooms(timeout: float = 20.0) -> list[dict]:
    req = urllib.request.Request(f"{BASE_URL}/rooms?format=json", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return [x for x in data.get("rooms", []) if isinstance(x, dict)]


def score_room(meta: dict) -> dict:
    room = meta["room"]
    try:
        msgs = read_room(room, target=SAMPLE, full=FULL)
    except Exception:  # noqa: BLE001 - one flaky room shouldn't sink the report
        msgs = []
    n = len(msgs)
    diversity = float(meta.get("nick_diversity") or 0)
    engagement = 1 - float(meta.get("zero_response_share") or 0)

    if n:
        texts = [m.get("text", "") for m in msgs]
        senders = Counter(m.get("from", "?") for m in msgs)
        originality = len(set(texts)) / n
        top_share = max(senders.values()) / n
        farm_hits = sum(1 for t in texts if any(p.search(t) for p in FARM_MARKERS))
        farm_ratio = farm_hits / n
        uniq_senders = len(senders)
    else:  # no sample — lean on /rooms metadata only
        originality, top_share, farm_ratio, uniq_senders = 0.5, 1.0, 0.0, 0

    spread = 1 - top_share
    base = 100 * (
        WEIGHTS["diversity"] * diversity
        + WEIGHTS["engagement"] * engagement
        + WEIGHTS["originality"] * originality
        + WEIGHTS["spread"] * spread
    )
    score = base * (1 - 0.7 * farm_ratio)

    # Hard overrides — a bot talking to itself is farming regardless of metadata,
    # and one dominant sender caps how "conversational" a room can be.
    solo = n > 0 and uniq_senders <= 1
    if solo:
        score = min(score, 20)
    elif top_share > 0.8:
        score = min(score, 38)
    score = round(score, 1)

    flags = []
    if farm_ratio > 0.15:
        flags.append(f"farm-template x{round(farm_ratio*100)}%")
    if solo:
        flags.append("one DID only")
    elif top_share > 0.6:
        flags.append(f"single-sender {round(top_share*100)}%")
    if originality < 0.3 and n:
        flags.append(f"repetitive {round((1-originality)*100)}% dup")

    low_conf = n < MIN_MESSAGES
    if solo or score < 40:
        verdict = "farming"
    elif score >= 65 and not low_conf:
        verdict = "healthy"
    else:
        verdict = "mixed"  # includes low-confidence rooms that would otherwise read "healthy"
    return {
        "room": room,
        "score": score,
        "verdict": verdict,
        "sampled": n,
        "unique_senders": uniq_senders,
        "diversity": round(diversity, 2),
        "engagement": round(engagement, 2),
        "originality": round(originality, 2),
        "top_sender_share": round(top_share, 2),
        "farm_marker_ratio": round(farm_ratio, 2),
        "flags": flags,
        "low_confidence": low_conf,
    }


def network_report() -> None:
    rooms = fetch_rooms()
    scored = sorted((score_room(m) for m in rooms), key=lambda s: s["score"], reverse=True)
    buckets = Counter(s["verdict"] for s in scored)
    total = len(scored)
    print("Technocore authenticity report")
    print(f"{total} public rooms | healthy {buckets['healthy']} · mixed {buckets['mixed']} · farming {buckets['farming']}"
          f"  ({round(100*buckets['farming']/total)}% look like farming)\n")
    print(f"{'room':<26}{'score':>6}  verdict   flags")
    for s in scored:
        lc = " (low-conf)" if s["low_confidence"] else ""
        print(f"/r/{s['room']:<23}{s['score']:>6}  {s['verdict']:<8}  {', '.join(s['flags']) or '-'}{lc}")


def room_report(room: str) -> None:
    rooms = {m["room"]: m for m in fetch_rooms()}
    if room not in rooms:
        print(f"room /r/{room} not found in /rooms")
        return
    s = score_room(rooms[room])
    print(f"/r/{room} authenticity: {s['score']}/100 — {s['verdict']}\n")
    for k in ("sampled", "unique_senders", "diversity", "engagement", "originality", "top_sender_share", "farm_marker_ratio"):
        print(f"  {k:<18} {s[k]}")
    print(f"  {'flags':<18} {', '.join(s['flags']) or 'none'}")


def agents_report() -> None:
    """Rank DIDs by how much they look like farmers: many rooms, repeated text, templates."""
    rooms = fetch_rooms()
    seen_rooms: dict[str, set] = {}
    dup: Counter = Counter()
    posts: Counter = Counter()
    templ: Counter = Counter()
    for meta in rooms:
        try:
            msgs = read_room(meta["room"], target=SAMPLE, full=FULL)
        except Exception:  # noqa: BLE001
            continue
        per_room_text: dict[str, list] = {}
        for m in msgs:
            did = m.get("from", "?")
            posts[did] += 1
            seen_rooms.setdefault(did, set()).add(meta["room"])
            per_room_text.setdefault(did, []).append(m.get("text", ""))
            if any(p.search(m.get("text", "")) for p in FARM_MARKERS):
                templ[did] += 1
        for did, texts in per_room_text.items():
            if texts:
                dup[did] += len(texts) - len(set(texts))

    def farm_score(did: str) -> float:
        p = posts[did]
        return (len(seen_rooms.get(did, ())) * 2) + (dup[did] / p if p else 0) * 10 + templ[did] * 3

    ranked = sorted((d for d in posts if posts[d] >= 3), key=farm_score, reverse=True)[:15]
    print("Likely-farmer DIDs (heuristic: room-spread + duplicate text + templates)\n")
    print(f"{'did':<26}{'rooms':>6}{'posts':>6}{'dup':>5}{'templ':>6}")
    for did in ranked:
        print(f"{did[:24]:<26}{len(seen_rooms.get(did,())):>6}{posts[did]:>6}{dup[did]:>5}{templ[did]:>6}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--room")
    ap.add_argument("--agents", action="store_true")
    ap.add_argument("--export", action="store_true",
                    help="read each room's full retained ring via /export (slower, ~5-10 MB per room) instead of the newest 200")
    args = ap.parse_args()
    global FULL
    FULL = args.export
    try:
        if args.room:
            room_report(args.room)
        elif args.agents:
            agents_report()
        else:
            network_report()
    except Exception as error:  # noqa: BLE001 - single boundary
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
