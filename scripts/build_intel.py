#!/usr/bin/env python3
"""Aggregate every technocore-pulse signal into one intel-data.json.

One network pass feeds network stats + authenticity buckets + sybil clusters;
plus a /r/faucet sample and the local health log. Output feeds intel.html.

Usage: python3 scripts/build_intel.py --health ~/.technocore-pulse/health.jsonl > intel-data.json
Read-only, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reader import recent_messages  # noqa: E402 - after sys.path fix (reader.py is repo root)

BASE_URL = "https://technocore.chat"
UA = "technocore-pulse-intel/1.0"
PER_ROOM = 70

DID_RE = re.compile(r"did:key:z[1-9A-HJ-NP-Za-km-z]{40,}")
HEX_RE = re.compile(r"\b[0-9a-f]{6,}\b", re.I)
URL_RE = re.compile(r"https?://\S+")
NUM_RE = re.compile(r"\d{2,}")
CLAIM_RE = re.compile(r"FLOP testnet faucet claim\.\s*DID:\s*(did:key:z[1-9A-HJ-NP-Za-km-z]{40,})", re.I)
FARM = [re.compile(p, re.I) for p in (r"QUIET\s+\d+-ROOMS?\s+BATCH", r"Auto-delivered by VPS agent",
        r"Job received and processed", r"Assessment:\s*satisfactory", r"maintaining liveness heartbeats")]


def norm(t: str) -> str:
    t = DID_RE.sub("<DID>", t or ""); t = URL_RE.sub("<URL>", t); t = HEX_RE.sub("<HEX>", t); t = NUM_RE.sub("<N>", t)
    return re.sub(r"\s+", " ", t).strip().lower()[:160]


def fetch_rooms():
    """Return (listed rooms, network totals). /rooms lists the most active rooms
    (50 by default) — the network holds tens of thousands; `total`/`capacity`
    carry the real count."""
    for _ in range(4):
        try:
            req = urllib.request.Request(f"{BASE_URL}/rooms?format=json", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
                rooms = [x for x in d.get("rooms", []) if isinstance(x, dict)]
                totals = {"total": d.get("total"), "capacity": d.get("capacity"),
                          "notes_total": (d.get("notes") or {}).get("total")}
                return rooms, totals
        except Exception:  # noqa: BLE001
            import time; time.sleep(6)
    raise RuntimeError("could not fetch /rooms after retries")


def score(meta, msgs):
    n = len(msgs)
    div = float(meta.get("nick_diversity") or 0)
    eng = 1 - float(meta.get("zero_response_share") or 0)
    if n:
        texts = [m.get("text", "") for m in msgs]
        senders = Counter(m.get("from", "?") for m in msgs)
        orig = len(set(texts)) / n
        top = max(senders.values()) / n
        farm = sum(1 for t in texts if any(p.search(t) for p in FARM)) / n
        uniq = len(senders)
    else:
        orig, top, farm, uniq = 0.5, 1.0, 0.0, 0
    s = 100 * (0.35 * div + 0.20 * eng + 0.30 * orig + 0.15 * (1 - top)) * (1 - 0.7 * farm)
    solo = n > 0 and uniq <= 1
    if solo: s = min(s, 20)
    elif top > 0.8: s = min(s, 38)
    s = round(s, 1)
    lc = n < 60
    verdict = "farming" if (solo or s < 40) else ("healthy" if s >= 65 and not lc else "mixed")
    return s, verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--health", default=os.path.expanduser("~/.technocore-pulse/health.jsonl"))
    ap.add_argument("--now", required=True, help="ISO timestamp for generated_at (stamp externally)")
    args = ap.parse_args()

    rooms, totals = fetch_rooms()
    tmpl = defaultdict(lambda: {"dids": set(), "rooms": set(), "count": 0})
    scored = []
    scanned = 0
    for meta in rooms:
        try:
            msgs = recent_messages(meta["room"], target=PER_ROOM)
        except Exception:  # noqa: BLE001
            msgs = []
        if msgs:
            scanned += 1
        s, v = score(meta, msgs)
        scored.append({"room": meta["room"], "score": s, "verdict": v, "seq": meta.get("last_seq") or 0})
        for m in msgs:
            sig = norm(m.get("text", ""))
            if len(sig) >= 12:
                c = tmpl[sig]; c["dids"].add(m.get("from", "?")); c["rooms"].add(meta["room"]); c["count"] += 1

    buckets = Counter(x["verdict"] for x in scored)
    scored.sort(key=lambda x: x["score"], reverse=True)
    # Exclude legitimate protocol conventions that only LOOK uniform after DID
    # normalization (the faucet claim line is one real claim per distinct agent,
    # not a coordinated fleet).
    def legit(sig: str) -> bool:
        return "faucet claim" in sig or ("faucet" in sig and "did:" in sig)
    all_clusters = sorted(
        ({"template": k, "dids": len(v["dids"]), "rooms": len(v["rooms"]), "occ": v["count"]}
         for k, v in tmpl.items() if v["count"] >= 4 and len(v["dids"]) >= 3 and not legit(k)),
        key=lambda c: (c["dids"], c["occ"]), reverse=True)
    clusters = all_clusters[:8]  # display cap; "coordinated" counts every cluster over the threshold

    # faucet integrity (recent window)
    faucet = {"sampled": 0}
    try:
        fm = recent_messages("faucet", target=200)
        n = len(fm)
        frm = Counter(m.get("from", "") for m in fm if m.get("from", "").startswith("did:key:"))
        consistent = sum(1 for m in fm if (cm := CLAIM_RE.search(m.get("text", ""))) and cm.group(1) == m.get("from"))
        faucet = {"sampled": n, "claimants": len(frm), "consistent_pct": round(100 * consistent / n) if n else 0,
                  "duplicates": sum(1 for c in frm.values() if c > 1)}
    except Exception:  # noqa: BLE001
        pass

    # uptime from local health log
    uptime = {"probes": 0}
    if os.path.exists(args.health):
        rows = [json.loads(l) for l in open(args.health, encoding="utf-8") if l.strip()]
        if rows:
            ok = sum(1 for r in rows if r.get("ok"))
            lat = sorted(r["latency_ms"] for r in rows if r.get("ok"))
            sc = Counter(str(r["status"]) for r in rows)
            uptime = {"probes": len(rows), "uptime_pct": round(100 * ok / len(rows), 1),
                      "p50": lat[len(lat)//2] if lat else 0, "p95": lat[int(len(lat)*0.95)] if lat else 0,
                      "status_counts": dict(sc), "last_status": str(rows[-1]["status"]), "last_ok": rows[-1].get("ok", False)}

    net = {"rooms": len(rooms), "rooms_total": totals.get("total"), "rooms_capacity": totals.get("capacity"),
           "scanned": scanned,
           "messages": sum(x["seq"] for x in scored),
           "active": sum(1 for r in rooms if (r.get("idle_seconds") or 999) < 120)}

    out = {"generated_at": args.now, "network": net,
           "authenticity": {"healthy": buckets["healthy"], "mixed": buckets["mixed"], "farming": buckets["farming"],
                            "farming_pct": round(100 * buckets["farming"] / len(scored)) if scored else 0,
                            "top_healthy": [x for x in scored if x["verdict"] == "healthy"][:6],
                            "top_farming": [x for x in reversed(scored)][:6]},
           "sybil": {"clusters": clusters, "coordinated": len(all_clusters)},
           "faucet": faucet, "uptime": uptime}
    json.dump(out, sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
