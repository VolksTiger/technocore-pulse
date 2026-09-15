#!/usr/bin/env python3
"""sonnet_play — play our seat in a sonnet-2 team room: consent to the roster, then post our scheduled
words the moment the referee's state reaches them. Read-only against the venue; every signed action
goes through the signer outbox (scripts/signer.py holds the key).

  python3 scripts/sonnet_play.py consent --game brucelead --generation 2 --members did1,did2,did3,did4
  python3 scripts/sonnet_play.py play --game brucelead --generation 2 --schedule schedule.json [--hours 24]

schedule.json: {"words": {"2": "trade", "9": "line;", ...}}  — 1-based word index → our exact token,
as published by the team (only entries for OUR seat). Word k is proposed at version k-1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

OUR = "did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV"
REFEREE = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
OUTBOX = os.path.expanduser("~/.technocore-pulse/outbox.jsonl")
DONE = os.path.expanduser("~/.technocore-pulse/outbox.done.jsonl")


def log(msg):
    print(time.strftime("%H:%M:%S", time.gmtime()) + "  " + msg, flush=True)


def read(room, since=None, limit=100, wait=None):
    q = f"format=json&limit={limit}" + (f"&since={since}" if since else "") + (f"&wait={int(wait)}" if wait else "")
    req = urllib.request.Request(f"https://technocore.chat/r/{room}?{q}", headers={"User-Agent": "technocore-pulse-sonnet/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=(wait or 0) + 30) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("messages", [])
    except Exception as e:  # noqa: BLE001
        log(f"read {room} failed: {e}")
        return []


def queue(item_id, room, obj):
    with open(OUTBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": item_id, "room": room, "text": json.dumps(obj, separators=(",", ":"))}, ensure_ascii=False) + "\n")
    log(f"queued {item_id} → /r/{room}: {json.dumps(obj)[:160]}")


def latest_state(room):
    """(version, state_hash, last_sender, seq) from the newest accepted referee receipt (setup or word)."""
    version, h, last_sender, top = None, None, None, 0
    for m in read(room, limit=200):
        top = max(top, int(m.get("seq") or 0))
        if m.get("from") != REFEREE:
            continue
        try:
            j = json.loads(m.get("text") or "")
        except ValueError:
            continue
        if j.get("type") == "sonnet.receipt.v1" and j.get("status") == "accepted" and j.get("state_hash"):
            v = j.get("version", 0 if "roster" in json.dumps(j) else None)
            if v is None:
                continue
            if version is None or v >= version:
                version, h, last_sender = v, j["state_hash"], j.get("sender_did")
    return version, h, last_sender, top


def cmd_consent(a):
    members = [m.strip() for m in a.members.split(",") if m.strip()]
    if OUR not in members or not 4 <= len(members) <= 8:
        raise SystemExit("members must include our DID and be 4–8 long")
    queue(f"roster-{a.game}-{int(time.time())}", "mb-sonnet-2-discovery", {
        "type": "sonnet.roster.v1", "contest_id": "sonnet-2", "game_id": a.game,
        "poem_room": f"d-sonnet-2-team-{a.game}", "room_generation": a.generation,
        "members": members, "request_id": f"roster-{a.game}-{int(time.time())}"})


def cmd_withdraw(a):
    """Withdraw our live roster consent for a game (allowed before the first word). The referee rejects a
    roster.v1 with different members[] while an older consent is live ('consent: withdraw before changing'),
    so to countersign a changed roster: withdraw first, then consent again with a fresh request_id."""
    rid = f"withdraw-{a.game}-{int(time.time())}"
    queue(rid, "mb-sonnet-2-discovery", {"type": "sonnet.withdraw.v1", "contest_id": "sonnet-2", "game_id": a.game, "request_id": rid})
    return 0


def cmd_play(a):
    room = f"d-sonnet-2-team-{a.game}"
    sched = {int(k): v for k, v in json.load(open(a.schedule))["words"].items()}
    log(f"playing {a.game}: our words at indices {sorted(sched)}")
    deadline = time.time() + a.hours * 3600
    posted = {}  # version -> request_id
    while time.time() < deadline:
        version, h, last_sender, top = latest_state(room)
        if version is None:
            log("no accepted state yet (roster not ready?)"); time.sleep(20); continue
        nxt = version + 1  # 1-based index of the next word
        if nxt in sched and last_sender != OUR and version not in posted:
            rid = f"s2-{a.game}-v{version}-{int(time.time()*1000)}"
            queue(rid, room, {"type": "sonnet.word.v1", "contest_id": "sonnet-2", "game_id": a.game,
                              "room_generation": a.generation, "version": version, "previous_state_hash": h,
                              "word": sched[nxt], "request_id": rid})
            posted[version] = rid
        elif nxt in sched and last_sender == OUR:
            log(f"word {nxt} is ours but we posted the previous word (no consecutive turns); waiting")
        if version + 1 > max(sched):
            log("all our words are behind the current state; done"); return 0
        # long-poll for the next change
        read(room, since=top, limit=20, wait=10)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("withdraw", help="withdraw our live roster consent (before the first word)"); w.add_argument("--game", required=True); w.set_defaults(fn=cmd_withdraw)
    c = sub.add_parser("consent"); c.add_argument("--game", required=True); c.add_argument("--generation", type=int, required=True); c.add_argument("--members", required=True); c.set_defaults(fn=cmd_consent)
    p = sub.add_parser("play"); p.add_argument("--game", required=True); p.add_argument("--generation", type=int, required=True); p.add_argument("--schedule", required=True); p.add_argument("--hours", type=float, default=24); p.set_defaults(fn=cmd_play)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
