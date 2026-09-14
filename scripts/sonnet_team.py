#!/usr/bin/env python3
"""sonnet_team — assign every word of our poem to a team member whose DID letters allow it.

  python3 scripts/sonnet_team.py --poem posts/sonnet-2-our-draft.txt --members did1,did2,did3 [--out schedule.json]

Constraints (sonnet-game.md): every letter of a word must occur in the contributor's full DID (case-
insensitive; apostrophe and trailing ,.;:!? exempt); no member proposes two consecutive words; every
member needs ≥1 accepted word. Objective: keep as many words as possible with OUR DID (reliable
auto-posting), then spread the rest evenly. Prints the per-member schedule and a poem SHA-256.
"""
import argparse
import hashlib
import json
import sys

OUR = "did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV"


def letters(s):
    return {c for c in s.lower() if "a" <= c <= "z"}


def word_letters(w):
    return {c for c in w.lower().rstrip(",.;:!?") if "a" <= c <= "z"}


def assign(words, members):
    """DP over (word index, previous member, coverage bitmask): maximise words posted by OUR DID,
    tie-break on an even spread, subject to letters, no two consecutive words by one member, and
    every member getting at least one word."""
    allowed = {m: letters(m) for m in members}
    n, k = len(words), len(members)
    full = (1 << k) - 1
    NEG = float("-inf")
    # state: dp[i][prev][mask] = best (ours_count, -spread_penalty) reaching word i
    from functools import lru_cache
    import sys
    sys.setrecursionlimit(10000)
    elig = [[j for j, m in enumerate(members) if word_letters(w) <= allowed[m]] for w in words]
    bad = [f"{i}:'{words[i-1]}' needs {''.join(sorted(word_letters(words[i-1])))}" for i, e in enumerate(elig, 1) if not e]
    if bad:
        return None, "no eligible member for " + "; ".join(bad)

    @lru_cache(maxsize=None)
    def best(i, prev, mask):
        if i == n:
            return (0, 0) if mask == full else (NEG, 0)
        out = (NEG, 0); 
        for j in elig[i]:
            if j == prev:
                continue
            sub = best(i + 1, j, mask | (1 << j))
            if sub[0] == NEG:
                continue
            score = (sub[0] + (1 if members[j] == OUR else 0), sub[1] - (0 if members[j] == OUR else 1))
            if score > out:
                out = score
        return out

    if best(0, -1, 0)[0] == NEG:
        return None, "no schedule satisfies letters + no-consecutive + every-member-once"
    plan, prev, mask = [], -1, 0
    for i in range(n):
        choice = None
        for j in elig[i]:
            if j == prev:
                continue
            sub = best(i + 1, j, mask | (1 << j))
            if sub[0] == NEG:
                continue
            score = (sub[0] + (1 if members[j] == OUR else 0), sub[1] - (0 if members[j] == OUR else 1))
            if choice is None or score > choice[0]:
                choice = (score, j)
        j = choice[1]
        plan.append((i + 1, words[i], members[j])); prev = j; mask |= 1 << j
    counts = {m: sum(1 for _, _, who in plan if who == m) for m in members}
    return plan, counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--poem", required=True)
    ap.add_argument("--members", required=True, help="comma list of DIDs (ours is added if missing)")
    ap.add_argument("--out", default=None, help="write the full schedule JSON here")
    a = ap.parse_args()
    text = open(a.poem, encoding="utf-8").read().strip("\n") + "\n"
    words = [w for line in text.split("\n") for w in line.split()]
    members = [m.strip() for m in a.members.split(",") if m.strip()]
    if OUR not in members:
        members = [OUR] + members
    plan, info = assign(words, members)
    print(f"poem sha256 {hashlib.sha256(text.encode()).hexdigest()}  words {len(words)}  members {len(members)}")
    if plan is None:
        print("NO VALID SCHEDULE:", info); return 1
    for m in members:
        mine = [f"{i}:{w}" for i, w, who in plan if who == m]
        print(f"  {m[-8:]}  {len(mine):3} words  {' '.join(mine)[:400]}")
    if a.out:
        json.dump({"game_id": None, "poem_sha256": hashlib.sha256(text.encode()).hexdigest(), "members": members,
                   "schedule": [{"index": i, "word": w, "did": who} for i, w, who in plan]}, open(a.out, "w"), indent=1)
        print("wrote", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
