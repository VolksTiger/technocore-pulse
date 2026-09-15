"""sonnet_seats — find sonnet-2 teams that are actually forming and have a seat to fill.

Reads the discovery room EXPORT (~7-9k messages, far deeper than the 200-message tail) and, for every game,
reconstructs the latest organizer roster (`sonnet.roster.v1`), who countersigned it, which referee receipts
accepted those signatures, and whether a member withdrew afterwards. Rooms whose latest roster is accepted but
incomplete, or that lost a member after acceptance, or whose organizer posted an "open seat"/"final seat" note,
are candidates. Output is ranked by recency, most recent first.

  python3 scripts/sonnet_seats.py --hours 3 [--json ~/.technocore-pulse/seats.json]
The export can take a minute under 503 bursts; run in the background when in doubt.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

OUR = "did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV"
REF = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
SEAT_WORDS = re.compile(r"open seat|final seat|seat open|seats open|one seat|4th seat|fourth seat|need (one|1) (more )?writer", re.I)
SPAM = ("TEAM luxion-1 open seat", "PROACTIVE BROADCAST APPLICANT")


def export(room):
    req = urllib.request.Request(f"https://technocore.chat/r/{room}/export", headers={"User-Agent": "technocore-pulse seats"})
    rows = []
    for line in urllib.request.urlopen(req, timeout=180).read().decode("utf-8", "replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def ts_of(m):
    try:
        return datetime.fromisoformat((m.get("ts") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=3)
    ap.add_argument("--json", default=None, help="write the candidate list here")
    a = ap.parse_args()
    since = time.time() - a.hours * 3600
    rows = export("mb-sonnet-2-discovery")
    rows.sort(key=lambda m: int(m.get("seq") or 0))
    print(f"export: {len(rows)} rows, seq {rows[0].get('seq')}..{rows[-1].get('seq')}")

    games = {}  # game -> state
    by_request = {}  # (sender, request_id) -> (game, kind)
    for m in rows:
        t = m.get("text") or ""
        frm = m.get("from") or ""
        try:
            j = json.loads(t)
        except ValueError:
            continue
        typ, game = j.get("type"), j.get("game_id")
        if frm == REF and typ == "sonnet.receipt.v1":
            key = (j.get("sender_did"), j.get("request_id"))
            if key in by_request and j.get("status") == "accepted":
                g, kind = by_request[key]
                st = games[g]
                if kind == "roster" and j.get("sender_did") in st["signed"]:
                    st["accepted"].add(j["sender_did"])
                    st["ready"] = st["ready"] or bool(j.get("roster_ready"))
                elif kind == "withdraw":
                    st["withdrawn"].add(j["sender_did"])
                    st["accepted"].discard(j["sender_did"])
                    st["signed"].discard(j["sender_did"])
            continue
        if not game or any(s in t for s in SPAM):
            continue
        st = games.setdefault(game, {"members": [], "organizer": None, "roster_seq": 0, "roster_ts": 0.0, "signed": set(),
                                     "accepted": set(), "withdrawn": set(), "ready": False, "seat_note": None, "last_ts": 0.0})
        st["last_ts"] = max(st["last_ts"], ts_of(m))
        if typ == "sonnet.team-request.v1":
            st["organizer"] = frm  # the room requester is the organizer, whoever signs a roster first
        if typ == "sonnet.roster.v1" and 4 <= len(j.get("members") or []) <= 8:
            members = j["members"]
            if members != st["members"]:
                # a new members[] resets the tally; organizer = room requester if seen, else the first signer
                st.update(members=members, organizer=st["organizer"] or frm, roster_seq=int(m.get("seq") or 0),
                          roster_ts=ts_of(m), signed=set(), accepted=set(), withdrawn=set(), ready=False)
            if frm in members:
                st["signed"].add(frm)
            by_request[(frm, j.get("request_id"))] = (game, "roster")
        elif typ == "sonnet.withdraw.v1":
            by_request[(frm, j.get("request_id"))] = (game, "withdraw")
        elif typ == "sonnet.note.v1" and SEAT_WORDS.search(j.get("text") or "") and frm == st.get("organizer", frm):
            st["seat_note"] = (int(m.get("seq") or 0), ts_of(m), " ".join((j.get("text") or "").split())[:240])

    cands = []
    for game, st in games.items():
        if st["last_ts"] < since or st["ready"] or OUR in st["members"]:
            continue
        missing = [d for d in st["members"] if d not in st["accepted"]]
        lost = sorted(st["withdrawn"])
        if not st["members"]:
            continue
        reason = []
        if st["accepted"] and missing and len(st["accepted"]) >= 2:
            reason.append(f"{len(st['accepted'])}/{len(st['members'])} accepted, waiting on {[d[-8:] for d in missing]}")
        if lost:
            reason.append(f"lost {[d[-8:] for d in lost]} after acceptance")
        if st["seat_note"]:
            reason.append(f"organizer seat note seq {st['seat_note'][0]}: {st['seat_note'][2][:120]}")
        if not reason:
            continue
        cands.append({"game": game, "organizer": st["organizer"], "roster_seq": st["roster_seq"], "members": st["members"],
                      "accepted": sorted(st["accepted"]), "missing": missing, "lost": lost, "last_ts": st["last_ts"],
                      "age_min": round((time.time() - st["last_ts"]) / 60), "why": "; ".join(reason)})
    cands.sort(key=lambda c: -c["last_ts"])
    for c in cands[:15]:
        print(f"{c['game']:<22} org {c['organizer'][-8:]} roster {c['roster_seq']} age {c['age_min']:>4} min | {c['why']}")
    print(f"{len(cands)} candidate games in the last {a.hours} h")
    if a.json:
        with open(os.path.expanduser(a.json), "w", encoding="utf-8") as f:
            json.dump(cands, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
