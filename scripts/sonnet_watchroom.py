#!/usr/bin/env python3
"""sonnet_watchroom — read-only watch of our team room that raises a macOS notification (and a line in
~/.technocore-pulse/COVER-ALERTS.txt) when a human needs to look: a teammate addresses our DID suffix in
plain text (usually a cover request), the referee rejects one of our words, or our own scheduled word has
been open for more than --stall-min minutes (the play loop or the signer is down). Posts nothing.
  (nohup python3 scripts/sonnet_watchroom.py --game prophet --schedule ~/.technocore-pulse/play-prophet.json --hours 48 >> ~/.technocore-pulse/watchroom-prophet.log 2>&1 &)
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sonnet_play import OUR, REFEREE, log, read  # noqa: E402

ALERTS = os.path.expanduser("~/.technocore-pulse/COVER-ALERTS.txt")
SUFFIX = OUR[-8:]


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


def state(room):
    """(version, last_sender, receipt_ts, top_seq) from the newest accepted referee receipt."""
    version, last, rts, top = None, None, 0, 0
    for m in read(room, limit=200):
        top = max(top, int(m.get("seq") or 0))
        if m.get("from") != REFEREE:
            continue
        try:
            j = json.loads(m.get("text") or "")
        except ValueError:
            continue
        if j.get("type") == "sonnet.receipt.v1" and j.get("status") == "accepted" and "version" in j:
            if version is None or j["version"] >= version:
                version, last, rts = j["version"], j.get("sender_did"), ts_of(m.get("ts", ""))
    return version, last, rts, top


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", required=True)
    ap.add_argument("--schedule", required=True)
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--stall-min", type=float, default=20)
    a = ap.parse_args()
    room = f"d-sonnet-2-team-{a.game}"
    sched = {int(k) for k in json.load(open(a.schedule))["words"]}
    version, _, _, cursor = state(room)
    log(f"watching {room} for {SUFFIX} from seq {cursor} (state v{version}); ours={sorted(sched)}")
    stalled = set()
    deadline = time.time() + a.hours * 3600
    while time.time() < deadline:
        for m in read(room, since=cursor, limit=50, wait=10):
            cursor = max(cursor, int(m.get("seq") or 0))
            text = m.get("text") or ""
            if m.get("from") == REFEREE:
                try:
                    j = json.loads(text)
                except ValueError:
                    continue
                if j.get("status") == "rejected" and j.get("sender_did") == OUR:
                    notify(f"referee odbio našu riječ ({j.get('request_id')}): {j.get('reason')!r}")
                continue
            if not text.startswith("{") and SUFFIX in text:
                notify(f"ping od {m.get('from', '')[-8:]}: {text[:140]}")
        version, last, rts, _ = state(room)
        if version is None:
            continue
        nxt = version + 1
        idle = time.time() - rts if rts else 0
        if nxt in sched and last != OUR and idle > a.stall_min * 60 and version not in stalled:
            stalled.add(version)
            notify(f"STALL: naša riječ {nxt} otvorena {idle / 60:.0f} min na v{version} — provjeri play loop i signer")
    log("deadline reached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
