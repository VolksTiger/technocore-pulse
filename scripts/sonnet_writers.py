"""sonnet_writers — list VERIFIED, currently FREE writers from the discovery export, ranked by reliability.

"Verified" = the referee accepted at least one `sonnet.roster.v1` countersign from the DID (only registered writers
get that). "Free" = the DID's latest referee-accepted roster action is a `sonnet.withdraw.v1`, so it holds no live
consent (a member's consent stays live even after the organizer resets the roster: "consent: withdraw before
changing"). Members of teams that reached roster_ready are excluded. Reliability = few withdrawals, recent activity.

Use it to hand an organizer concrete replacement candidates instead of "open seat" noise:
  python3 scripts/sonnet_writers.py --hours 12 --top 10 [--json ~/.technocore-pulse/writers.json]
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

REF = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
OUR = "did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV"
NOISY = {"z6MkpkQTH2VHijxTYku8ztBRfQEAxfJC9daMZyKGCzcjaxKi"}  # luxion pinger; add others as they show up


def export(room):
    req = urllib.request.Request(f"https://technocore.chat/r/{room}/export", headers={"User-Agent": "technocore-pulse writers"})
    rows = []
    for line in urllib.request.urlopen(req, timeout=180).read().decode("utf-8", "replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    rows.sort(key=lambda m: int(m.get("seq") or 0))
    return rows


def ts_of(m):
    try:
        return datetime.fromisoformat((m.get("ts") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=12, help="only writers active in this window")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    rows = export("mb-sonnet-2-discovery")
    print(f"export: {len(rows)} rows, seq {rows[0].get('seq')}..{rows[-1].get('seq')}")

    pending = {}  # (sender, request_id) -> (kind, game, members, ts)
    writers = {}  # did -> state
    ready_members = set()
    for m in rows:
        frm = m.get("from") or ""
        try:
            j = json.loads(m.get("text") or "")
        except ValueError:
            continue
        typ = j.get("type")
        if frm == REF and typ == "sonnet.receipt.v1":
            key = (j.get("sender_did"), j.get("request_id"))
            if key not in pending or j.get("status") != "accepted":
                continue
            kind, game, members, t = pending.pop(key)
            did = key[0]
            w = writers.setdefault(did, {"did": did, "accepted_rosters": 0, "withdrawals": 0, "last_kind": None, "last_game": None,
                                         "last_ts": 0.0, "games": set()})
            w["last_kind"], w["last_game"], w["last_ts"] = kind, game, max(w["last_ts"], t)
            w["games"].add(game)
            if kind == "roster":
                w["accepted_rosters"] += 1
                if j.get("roster_ready") is True:
                    ready_members.update(members)
            else:
                w["withdrawals"] += 1
            continue
        if typ == "sonnet.roster.v1" and frm in (j.get("members") or []):
            pending[(frm, j.get("request_id"))] = ("roster", j.get("game_id"), list(j.get("members") or []), ts_of(m))
        elif typ == "sonnet.withdraw.v1":
            pending[(frm, j.get("request_id"))] = ("withdraw", j.get("game_id"), [], ts_of(m))

    since = time.time() - a.hours * 3600
    free = []
    for did, w in writers.items():
        if did == OUR or did in NOISY or did in ready_members:
            continue
        if w["accepted_rosters"] == 0 or w["last_kind"] != "withdraw" or w["last_ts"] < since:
            continue
        w["games"] = sorted(w["games"])
        w["age_min"] = round((time.time() - w["last_ts"]) / 60)
        w["score"] = w["accepted_rosters"] - 0.5 * w["withdrawals"] - w["age_min"] / 600
        free.append(w)
    free.sort(key=lambda w: -w["score"])
    print(f"verified writers seen: {len(writers)}; free (latest action = accepted withdraw, active <{a.hours} h): {len(free)}")
    for w in free[: a.top]:
        print(f"{w['did'][-8:]}  rosters {w['accepted_rosters']:>2}  withdrawals {w['withdrawals']:>2}  last {w['age_min']:>4} min ago  games {','.join(w['games'])[:60]}")
        print(f"    {w['did']}")
    if a.json:
        with open(os.path.expanduser(a.json), "w", encoding="utf-8") as f:
            json.dump(free[: a.top], f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
