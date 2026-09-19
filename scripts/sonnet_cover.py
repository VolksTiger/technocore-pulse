#!/usr/bin/env python3
"""sonnet_cover — keep a sonnet-2 team chain moving while the operator is away: cover open words that
nobody posts, using the organizer's rule that any non-previous member may post any word they can spell.

Covers a word when either
  * a teammate addresses our DID suffix in plain text with `word N ('token')` (explicit request), or
  * the open word has had no attempt by anyone for --cover-after minutes since the last referee receipt,
and always only if the contest validator accepts the token for OUR DID, we did not post the previous word
(no consecutive turns) and the word after it is not ours (we would block ourselves). One attempt per version;
a rejection whose reason starts with "word" retires that version. Our own scheduled words are the play
loop's job; they are covered here only when the play loop has been silent past the same threshold.
Every action raises a macOS notification and a line in ~/.technocore-pulse/COVER-ALERTS.txt.

  python3 scripts/sonnet_cover.py --game prophet --generation 2 --table ~/.technocore-pulse/table-prophet.json [--cover-after 10] [--dry-run]
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.expanduser("~/.technocore-pulse/sonnet"))
from sonnet_play import OUR, REFEREE, log, queue, read  # noqa: E402
from sonnet_validate import read_lexicon, validate_word  # noqa: E402

ALERTS = os.path.expanduser("~/.technocore-pulse/COVER-ALERTS.txt")
SUFFIX = OUR[-8:]
REQUEST = re.compile(r"word\s+(\d+)\s*\(\s*['\"‘“]([^'\"’”\s]+)['\"’”]\s*\)")


def notify(msg):
    log("ALERT " + msg)
    with open(ALERTS, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M ", time.gmtime()) + msg + "\n")
    safe = msg.replace('"', "'").replace("\\", "")[:200]
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "sonnet-2"'], check=False)


def ts_of(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:  # noqa: BLE001
        return 0


def parse(m):
    try:
        return json.loads(m.get("text") or "")
    except ValueError:
        return None


def snapshot(room):
    """Latest accepted state plus what happened after it: (version, hash, last_sender, receipt_ts, attempts, top).
    attempts = senders of word.v1 messages at the current version posted after the receipt (someone is on it)."""
    msgs = read(room, limit=200)
    version, h, last, rts, rseq, top = None, None, None, 0, 0, 0
    for m in msgs:
        top = max(top, int(m.get("seq") or 0))
        if m.get("from") != REFEREE:
            continue
        j = parse(m)
        if j and j.get("type") == "sonnet.receipt.v1" and j.get("status") == "accepted" and j.get("state_hash") and "version" in j:
            if version is None or j["version"] >= version:
                version, h, last, rts, rseq = j["version"], j["state_hash"], j.get("sender_did"), ts_of(m.get("ts", "")), int(m.get("seq") or 0)
    attempts, rejected = {}, set()
    for m in msgs:
        if int(m.get("seq") or 0) <= rseq:
            continue
        j = parse(m)
        if not j:
            continue
        if m.get("from") == REFEREE:
            if j.get("status") == "rejected" and j.get("request_id"):
                rejected.add(j["request_id"])
        elif j.get("type") == "sonnet.word.v1" and j.get("version") == version:
            attempts[j.get("request_id") or str(m.get("seq"))] = m.get("from")
    live = [who for rid, who in attempts.items() if rid not in rejected]
    return version, h, last, rts, live, top, msgs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", required=True)
    ap.add_argument("--generation", type=int, required=True)
    ap.add_argument("--table", required=True, help="index -> {word, signer, ours} as written by the table dump")
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--cover-after", type=float, default=10, help="minutes without any attempt before we cover")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    room = f"d-sonnet-2-team-{a.game}"
    table = {int(k): v for k, v in json.load(open(a.table)).items()}
    ours = {i for i, v in table.items() if v.get("ours")}
    lexicon = read_lexicon(Path(os.path.expanduser("~/.technocore-pulse/sonnet/cmudict.dict")))

    def spellable(token):
        try:
            validate_word(token, OUR, lexicon)
            return True
        except ValueError:
            return False

    version, _, _, _, _, cursor, _ = snapshot(room)
    log(f"covering {room} as {SUFFIX} from seq {cursor} (state v{version}); table {len(table)} words, "
        f"{sum(spellable(v['word']) for v in table.values())} spellable by us; cover after {a.cover_after:g} min")
    requested = {}  # index -> token from an explicit request
    posted = {}  # version -> request_id
    retired = set()
    deadline = time.time() + a.hours * 3600
    while time.time() < deadline:
        version, h, last, rts, attempts, top, msgs = snapshot(room)
        for m in msgs:
            seq = int(m.get("seq") or 0)
            if seq <= cursor:
                continue
            cursor = seq
            text = m.get("text") or ""
            j = parse(m)
            if m.get("from") == REFEREE:
                if j and j.get("status") == "rejected" and j.get("request_id") in posted.values():
                    v = next(v for v, r in posted.items() if r == j["request_id"])
                    notify(f"referee odbio naš cover v{v}: {j.get('reason')!r}")
                    if str(j.get("reason", "")).startswith("word"):
                        retired.add(v)
                continue
            if j is None and SUFFIX in text:
                notify(f"ping od {m.get('from', '')[-8:]}: {text[:140]}")
                for n, token in REQUEST.findall(text):
                    n = int(n)
                    if n in ours:
                        continue  # the play loop posts our own words
                    if spellable(token):
                        requested[n] = token
                        log(f"explicit request accepted: word {n} {token!r}")
                    else:
                        notify(f"traženo pokrivanje riječi {n} {token!r}, ali ne prolazi validator")
        if version is None:
            read(room, since=top, limit=20, wait=10)
            continue
        nxt = version + 1
        entry = table.get(nxt)
        token = requested.get(nxt) or (entry or {}).get("word")
        idle_min = (time.time() - rts) / 60 if rts else 0
        reason = None
        if token is None:
            reason = "no word in table"
        elif version in posted or version in retired:
            reason = "already attempted"
        elif last == OUR:
            reason = "we posted the previous word"
        elif nxt + 1 in ours:
            reason = f"word {nxt + 1} is ours; covering {nxt} would block us"
        elif not spellable(token):
            reason = "we cannot spell it"
        elif nxt not in requested and (attempts or idle_min < a.cover_after):
            reason = None  # simply not yet
        if token is not None and reason is None and (nxt in requested or (not attempts and idle_min >= a.cover_after)):
            rid = f"s2-{a.game}-cover-v{version}-{int(time.time() * 1000)}"
            obj = {"type": "sonnet.word.v1", "contest_id": "sonnet-2", "game_id": a.game, "room_generation": a.generation,
                   "version": version, "previous_state_hash": h, "word": token, "request_id": rid}
            who = (entry or {}).get("signer", "?")
            if a.dry_run:
                log(f"DRY: would cover word {nxt} {token!r} (assigned {who}) at v{version}, idle {idle_min:.0f} min")
            else:
                queue(rid, room, obj)
                notify(f"COVER: riječ {nxt} {token!r} (dodijeljena {who}) poslana na v{version} nakon {idle_min:.0f} min")
            posted[version] = rid
        elif reason and reason != "already attempted" and idle_min >= a.cover_after and not attempts and version not in retired:
            log(f"v{version} open {idle_min:.0f} min, not covering: {reason}")
            retired.add(version)  # log once per version
        read(room, since=top, limit=20, wait=10)
    log("deadline reached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
