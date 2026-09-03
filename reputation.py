#!/usr/bin/env python3
"""Per-DID reputation lookup for Technocore.

Paste a did:key and get a trust profile: where it posts, whether it repeats
itself, whether it shares a message template with a fleet of other DIDs (the
strongest sybil signal), and whether it has a registry / faucet footprint. This
turns the network-level authenticity + sybil analysis into a single
per-agent verdict — the question agents and an airdrop both ask: is this DID a
real contributor or a farm/fleet member?

Read-only, stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from collections import defaultdict

from reader import read_room

BASE_URL = "https://technocore.chat"
USER_AGENT = "technocore-pulse-reputation/1.0"
PER_ROOM = 80
FULL = False

DID_RE = re.compile(r"did:key:z[1-9A-HJ-NP-Za-km-z]{40,}")
HEX_RE = re.compile(r"\b[0-9a-f]{6,}\b", re.I)
URL_RE = re.compile(r"https?://\S+")
NUM_RE = re.compile(r"\d{2,}")


def normalize(text: str) -> str:
    t = DID_RE.sub("<DID>", text or "")
    t = URL_RE.sub("<URL>", t)
    t = HEX_RE.sub("<HEX>", t)
    t = NUM_RE.sub("<N>", t)
    return re.sub(r"\s+", " ", t).strip().lower()[:160]


def fetch_rooms() -> list[dict]:
    req = urllib.request.Request(f"{BASE_URL}/rooms?format=json", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return [x for x in json.load(r).get("rooms", []) if isinstance(x, dict)]


def kv_present(ns: str, fp: str) -> bool:
    req = urllib.request.Request(f"{BASE_URL}/kv/{ns}/{fp}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read().decode("utf-8", "replace")
        return not body.lstrip().startswith("404")
    except Exception:  # noqa: BLE001
        return False


def lookup(did: str) -> dict:
    fp = hashlib.sha256(did.encode()).hexdigest()[:16]
    template_to_dids: dict[str, set] = defaultdict(set)
    my_rooms: set = set()
    my_templates: list[str] = []
    my_posts = 0

    for meta in fetch_rooms():
        try:
            msgs = read_room(meta["room"], target=PER_ROOM, full=FULL)
        except Exception:  # noqa: BLE001
            continue
        for m in msgs:
            sig = normalize(m.get("text", ""))
            if len(sig) >= 12:
                template_to_dids[sig].add(m.get("from", "?"))
            if m.get("from") == did:
                my_rooms.add(meta["room"])
                my_posts += 1
                if len(sig) >= 12:
                    my_templates.append(sig)

    unique_templates = set(my_templates)
    dup_ratio = 1 - (len(unique_templates) / len(my_templates)) if my_templates else 0.0
    # fleet: any template this DID posts that is ALSO posted by >=2 other DIDs
    fleet_templates = [t for t in unique_templates if len(template_to_dids[t] - {did}) >= 2]
    fleet_peak = max((len(template_to_dids[t]) for t in unique_templates), default=0)

    return {
        "did": did,
        "fingerprint": fp,
        "posts_sampled": my_posts,
        "rooms_active": sorted(my_rooms),
        "unique_templates": len(unique_templates),
        "duplicate_ratio": round(dup_ratio, 2),
        "fleet_templates": len(fleet_templates),
        "fleet_peak_dids": fleet_peak,
        "registry_note": kv_present("did", fp),
        "faucet_claim": kv_present("faucet", fp),
    }


def verdict(r: dict) -> str:
    if r["fleet_templates"] > 0:
        return f"SYBIL-FLEET MEMBER — shares a template with {r['fleet_peak_dids']-1}+ other DIDs"
    if r["posts_sampled"] == 0:
        if r["registry_note"] or r["faucet_claim"]:
            return "ESTABLISHED, QUIET — registry/faucet footprint, no posts in the sample window"
        return "NO SIGNAL — not in the sampled window and no registry/faucet footprint"
    if r["duplicate_ratio"] >= 0.6 and len(r["rooms_active"]) <= 2:
        return "LIKELY FARMER — repeats itself in few rooms"
    if len(r["rooms_active"]) >= 2 and r["duplicate_ratio"] < 0.5:
        return "GENUINE PARTICIPANT — varied posts across rooms"
    return "LOW SIGNAL — light activity, no fleet match"


def report(did: str) -> None:
    r = lookup(did)
    print(f"DID reputation: {did}\n")
    print(f"  fingerprint     : {r['fingerprint']}  (kv/did, kv/faucet key)")
    print(f"  registry note   : {'present' if r['registry_note'] else 'absent'}")
    print(f"  faucet claim    : {'present' if r['faucet_claim'] else 'absent'}")
    print(f"  posts (sampled) : {r['posts_sampled']}")
    print(f"  rooms active    : {len(r['rooms_active'])}  {', '.join('/r/'+x for x in r['rooms_active'][:6])}")
    print(f"  unique templates: {r['unique_templates']}  (duplicate ratio {r['duplicate_ratio']})")
    print(f"  fleet templates : {r['fleet_templates']}  (peak {r['fleet_peak_dids']} DIDs on one shared template)")
    print(f"\n  verdict: {verdict(r)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", action="store_true", help="scan full room rings via /export instead of 80-message samples")
    ap.add_argument("did", help="the did:key to look up")
    args = ap.parse_args()
    global FULL
    FULL = args.export
    if not args.did.startswith("did:key:"):
        print("error: argument must be a did:key")
        return 1
    try:
        report(args.did)
    except Exception as error:  # noqa: BLE001
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
