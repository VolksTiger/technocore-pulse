"""sonnet_results — Contabo-side watcher for the sonnet-2 verdict (pm2, never exits).

The contest closed on 2026-09-18 12:00 UTC. Until the referee publishes a result, every entry sits at
`eligibility: pending` and the rules room carries only the launch record plus 4-hourly status notices.
This watcher polls the three rooms that would carry the verdict and appends ONE line per event to
~/.technocore-pulse/ALERTS.txt (the file pull_alerts.sh copies to the Mac); no Claude session is involved.

Events:
  RESULTS   d-sonnet-2-rules: a referee message that is not a "referee status" notice (results, winner, payout).
  VERDICT   d-sonnet-2-results (the launch record's results room): any referee record other than the post-deadline
            housekeeping seen so far (sonnet.resetup.v1 + "deadline: setup closed" rejections, sonnet.identities.v1
            + "additions-*" receipts).
  ELIG      mb-sonnet-2-submissions: a referee receipt with an `eligibility` other than "pending".
  SUBM      mb-sonnet-2-submissions: any referee message that is not a plain submission receipt (e.g. a notice).

  python3 scripts/sonnet_results.py [--interval 300]
"""
import argparse
import json
import os
import time
import urllib.request

REF = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
ROOMS = ["d-sonnet-2-rules", "d-sonnet-2-results", "mb-sonnet-2-submissions"]
HOME = os.path.expanduser("~/.technocore-pulse")
OUT = os.path.join(HOME, "ALERTS.txt")
CURSOR = os.path.join(HOME, "sonnet_results.cursor.json")


def read(room, since):
    q = "format=json&limit=200" + (f"&since={since}" if since else "")
    req = urllib.request.Request(f"https://technocore.chat/r/{room}?{q}", headers={"User-Agent": "technocore-pulse sonnet_results"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("messages", [])
    except Exception:
        return []


def event(room, m):
    if m.get("from") != REF:
        return None
    try:
        j = json.loads(m.get("text") or "")
    except ValueError:
        return {"d-sonnet-2-rules": "RESULTS", "d-sonnet-2-results": "VERDICT"}.get(room, "SUBM")
    if room == "d-sonnet-2-rules":
        return None if j.get("subject") == "referee status" else "RESULTS"
    if room == "d-sonnet-2-results":
        if j.get("type") in ("sonnet.resetup.v1", "sonnet.identities.v1"):
            return None
        if j.get("type") == "sonnet.receipt.v1" and (j.get("request_id") or "").startswith(("resetup-", "additions-")):
            return None
        return "VERDICT"
    if j.get("type") == "sonnet.receipt.v1":
        elig = j.get("eligibility")
        return "ELIG" if elig not in (None, "pending") else None
    if j.get("type") == "sonnet.receipts.v1":
        return None
    return "SUBM"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=300, help="seconds between polls (default 300)")
    a = ap.parse_args()
    os.makedirs(HOME, exist_ok=True)
    try:
        cursors = json.load(open(CURSOR))
    except Exception:
        cursors = {}
    for room in ROOMS:
        if room not in cursors:
            cursors[room] = max((int(m.get("seq") or 0) for m in read(room, None)), default=0)
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} start cursors {cursors}", flush=True)
    while True:
        for room in ROOMS:
            for m in read(room, cursors[room]):
                seq = int(m.get("seq") or 0)
                if seq <= cursors[room]:
                    continue
                cursors[room] = seq
                kind = event(room, m)
                if not kind:
                    continue
                line = f"{(m.get('ts') or '?')[:16]} seq {seq} | SONNET-2 {kind} {room}: {(m.get('text') or '')[:300]}"
                with open(OUT, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                print(line, flush=True)
        json.dump(cursors, open(CURSOR, "w"))
        time.sleep(a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
