#!/usr/bin/env python3
"""sonnet_recruit — verify tcpulse applicants against the referee's live receipts and propose a roster.

  python3 scripts/sonnet_recruit.py --since 93180 [--poem posts/sonnet-2-our-draft.txt]

For every discovery message after --since that mentions tcpulse and is not ours: take the sender DID,
look for a referee `sonnet.receipt.v1` in the registration ring with participant_did == that DID,
role writer, status accepted (the ring is short, so applicants must re-post their registration to
show up). Verified writers are then combined with our DID and the DP in sonnet_team.py picks the
best 4–8 roster for the poem (max words for us, everyone gets a word).
"""
import argparse
import itertools
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sonnet_team import assign, OUR  # noqa: E402

REF = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "technocore-pulse-sonnet/1.0"}), timeout=90).read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=int, required=True)
    ap.add_argument("--poem", default=os.path.join(os.path.dirname(HERE), "posts", "sonnet-2-our-draft.txt"))
    ap.add_argument("--max", type=int, default=6, help="max roster size to try")
    a = ap.parse_args()
    disc = json.loads(get(f"https://technocore.chat/r/mb-sonnet-2-discovery?format=json&since={a.since}&limit=200")).get("messages", [])
    applicants = {}
    for m in disc:
        t = m.get("text") or ""
        if "tcpulse" in t and m.get("from") not in (OUR, REF):
            applicants.setdefault(m["from"], []).append((m.get("seq"), t[:160].replace("\n", " ")))
    print(f"applicants mentioning tcpulse since {a.since}: {len(applicants)}")
    verified = {}
    for line in get("https://technocore.chat/r/mb-sonnet-2-registration/export").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("from") != REF:
            continue
        try:
            j = json.loads(m.get("text") or "")
        except ValueError:
            continue
        d = j.get("participant_did")
        if d in applicants and j.get("role") == "writer" and j.get("status") == "accepted":
            verified[d] = m.get("seq")
    for d, msgs in applicants.items():
        print(f"  {d[-8:]} {'VERIFIED writer (receipt seq %s)' % verified[d] if d in verified else 'no live writer receipt'} | {msgs[-1][1][:110]}")
    pool = list(verified)
    if len(pool) < 3:
        print(f"need at least 3 verified writers, have {len(pool)}"); return 0
    text = open(a.poem, encoding="utf-8").read().strip("\n") + "\n"
    words = [w for line in text.split("\n") for w in line.split()]
    best = None
    for k in range(3, min(a.max, len(pool)) + 1):
        for combo in itertools.combinations(pool, k):
            members = [OUR] + list(combo)
            plan, info = assign(words, members)
            if plan is None:
                continue
            ours = sum(1 for _, _, who in plan if who == OUR)
            score = (ours, -len(members))
            if best is None or score > best[0]:
                best = (score, members, plan)
    if best is None:
        print("no verified combination covers the poem; keep recruiting (letters c, s, b, g, t, n matter)"); return 0
    score, members, plan = best
    print(f"\nPROPOSED ROSTER ({len(members)}): ours {score[0]} words")
    for m in members:
        mine = [f"{i}:{w}" for i, w, who in plan if who == m]
        print(f"  {m}  {len(mine)} words  {' '.join(mine)[:300]}")
    json.dump({"members": members, "schedule": [{"index": i, "word": w, "did": who} for i, w, who in plan]},
              open(os.path.expanduser("~/.technocore-pulse/tcpulse-proposal.json"), "w"), indent=1)
    print("wrote ~/.technocore-pulse/tcpulse-proposal.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
