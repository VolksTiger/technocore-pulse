#!/usr/bin/env python3
"""One read-only pass over close-1 (flop-labs/technocore-close-call-challenge) on technocore.chat.

Prints: owner growth per sweep (d-close1-state), how many keys tie at the top of the
PnL board per sweep (d-close1-pnl), the share of programmatic pair ids among the
listed settled trades (d-close1-flow), the symmetric pairs at the top of the
positions board (d-close1-positions), and the term-template concentration of a
window of `close1`. Stdlib only.

  python3 scripts/closecall_measure.py            # live
  python3 scripts/closecall_measure.py --sample 200 --flow 12 --state 80
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import urllib.request

BASE = "https://technocore.chat"
UA = "technocore-pulse/closecall-measure (+https://github.com/VolksTiger/technocore-pulse)"
LINE = re.compile(r"\[(\d+)\] (\S+) <([^>]+)> (.*)")
PAIR_ID = re.compile(r"p\d+x\d+-")


def read(room: str, limit: int) -> list[tuple[int, str, str, dict]]:
    req = urllib.request.Request(f"{BASE}/r/{room}?limit={limit}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8", "replace")
    out = []
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        try:
            body = json.loads(m.group(4))
        except json.JSONDecodeError:
            body = {"t": "nonjson"}
        out.append((int(m.group(1)), m.group(2), m.group(3), body))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", type=int, default=80, help="state posts to read (one per sweep)")
    ap.add_argument("--flow", type=int, default=12, help="flow/pnl posts to read")
    ap.add_argument("--sample", type=int, default=200, help="close1 messages to sample")
    a = ap.parse_args()

    states = [b for _, _, _, b in read("d-close1-state", a.state) if b.get("t") == "state"]
    print("# owners per sweep (n:owners/rooms)")
    print(" ".join(f"{b['n']}:{b['owners']}/{b.get('rooms')}" for b in states))

    print("\n# pnl board: sweep, mark, keys listed, tied-at-top, leader")
    for _, ts, _, b in read("d-close1-pnl", a.flow):
        if b.get("t") != "pnl":
            continue
        top = b["top"]
        scores = collections.Counter(v for _, v in top)
        leader = max(top, key=lambda x: float(x[1]))[1]
        print(f"  {b['n']} {b['mark']} {len(top)} {scores.most_common(1)[0]} leader={leader}")

    print("\n# flow: sweep, settled listed, pair ids, omitted settled, void listed, omitted mints, mints listed, void reasons")
    tot_settled = tot_pair = 0
    for _, ts, _, b in read("d-close1-flow", a.flow):
        if b.get("t") != "flow":
            continue
        settled = b.get("settled", [])
        pairs = sum(1 for i in settled if PAIR_ID.match(str(i)))
        tot_settled += len(settled)
        tot_pair += pairs
        om = b.get("omitted", {})
        reasons = collections.Counter(r for _, r in b.get("void", [])).most_common(3)
        print(f"  {b['n']} {len(settled)} {pairs} {om.get('settled', 0)} {len(b.get('void', []))} {om.get('mints', 0)} {len(b.get('mints', []))} {reasons}")
    print(f"  pair ids over listed settled: {tot_pair}/{tot_settled}")

    for _, ts, _, b in read("d-close1-positions", 1):
        if b.get("t") == "positions":
            sizes = collections.Counter(abs(float(v)) for _, v in b["top"])
            print(f"\n# positions n={b['n']} open={b['open']} longs={b['longs']} shorts={b['shorts']} top-10 |size| counts={sorted(sizes.items(), reverse=True)}")

    sample = read("close1", a.sample)
    if sample:
        kinds = collections.Counter(b.get("t", "?") for _, _, _, b in sample)
        senders = collections.Counter(s for _, _, s, _ in sample)
        templates: collections.Counter = collections.Counter()
        makers: dict = collections.defaultdict(set)
        for _, _, _, b in sample:
            if b.get("t") in ("trade", "offer") and isinstance(b.get("terms"), dict):
                t = b["terms"]
                key = (t.get("side"), t.get("px"), t.get("qty"), t.get("until"))
                templates[key] += 1
                makers[key].add(t.get("maker"))
        n_terms = sum(templates.values())
        print(f"\n# close1 sample: {len(sample)} msgs {sample[0][1][11:19]}..{sample[-1][1][11:19]}Z kinds={dict(kinds)} distinct senders={len(senders)}")
        for key, c in templates.most_common(3):
            print(f"  template {key}: {c}/{n_terms} messages from {len(makers[key])} distinct makers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
