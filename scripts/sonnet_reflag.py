"""sonnet_reflag — how far behind is the sonnet-2 referee?

Maps every referee receipt in the discovery export back to the message it answers (sender_did + request_id) and
reports the referee's intake position: the newest ORIGINAL message it has receipted, how old that message is, and
the rate at which the position advances versus the room's inflow. After the referee restarted on 2026-09-15 16:08
UTC it replayed intake strictly in order at ~400-500 seq/h while the room grew ~1000 seq/h, so a consent posted at
16:37 UTC could only be receipted many hours later. This tool makes that visible instead of guessing.

  python3 scripts/sonnet_reflag.py [--room mb-sonnet-2-discovery] [--window-min 60]
Exit 0 always; prints one summary line plus a short table. The export can take minutes under load.
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

REF = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"


def ts_of(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def export(room):
    req = urllib.request.Request(f"https://technocore.chat/r/{room}/export", headers={"User-Agent": "technocore-pulse reflag"})
    rows = []
    for line in urllib.request.urlopen(req, timeout=600).read().decode("utf-8", "replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    rows.sort(key=lambda m: int(m.get("seq") or 0))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", default="mb-sonnet-2-discovery")
    ap.add_argument("--window-min", type=float, default=60, help="rate window (minutes before the newest receipt)")
    a = ap.parse_args()
    rows = export(a.room)
    if not rows:
        print("empty export")
        return 0
    originals = {}
    for m in rows:
        if m.get("from") == REF:
            continue
        try:
            j = json.loads(m.get("text") or "")
        except ValueError:
            continue
        if j.get("request_id"):
            # first posting wins: an identical retry re-posts the same request_id later, but the receipt answers the original
            originals.setdefault((m["from"], j["request_id"]), (int(m.get("seq") or 0), ts_of(m.get("ts"))))
    points = []  # (receipt_ts, receipt_seq, original_seq, original_ts)
    for m in rows:
        if m.get("from") != REF:
            continue
        try:
            j = json.loads(m.get("text") or "")
        except ValueError:
            continue
        key = (j.get("sender_did"), j.get("request_id"))
        if key in originals:
            points.append((ts_of(m.get("ts")), int(m.get("seq") or 0), originals[key][0], originals[key][1]))
    if not points:
        # The export no longer reaches the originals the referee is answering (backlog deeper than the export).
        # Fallback: many request_ids embed the poster's epoch (10 or 13 digits) -- use the newest such receipt.
        import re
        stamps = []
        for m in rows:
            if m.get("from") != REF:
                continue
            try:
                j = json.loads(m.get("text") or "")
            except ValueError:
                continue
            for digits in re.findall(r"(?<!\d)(\d{13}|\d{10})(?!\d)", j.get("request_id") or ""):
                t = int(digits) / (1000 if len(digits) == 13 else 1)
                if 1789000000 < t < time.time():  # plausible: after 2026-09-10
                    stamps.append((ts_of(m.get("ts")), t, int(m.get("seq") or 0), j.get("request_id")))
        if not stamps:
            print("no receipt could be matched to an original in this export window, and no request_id carries an epoch")
            return 0
        stamps.sort(key=lambda x: x[1])
        rt, ot, rseq, rid = stamps[-1]
        fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%d %H:%M")
        print(f"export does not reach the originals any more (backlog deeper than the export window); "
              f"fallback from epochs embedded in request_ids: newest receipted original was posted {fmt(ot)} UTC "
              f"(receipt seq {rseq} at {fmt(rt)}, request_id {rid[:40]}) -> lag {(rt - ot) / 3600:.1f} h")
        recent = [x for x in stamps if x[0] >= stamps[-1][0] - a.window_min * 60]
        if len(recent) >= 2:
            recent.sort(key=lambda x: x[0])
            adv = (recent[-1][1] - recent[0][1]) / max((recent[-1][0] - recent[0][0]), 1)
            print(f"replay speed over the last {a.window_min:.0f} min of receipts: {adv:.2f}x wall-clock (room-time replayed per hour = {adv * 60:.0f} min)")
        return 0
    head_seq, head_ts = int(rows[-1].get("seq") or 0), ts_of(rows[-1].get("ts"))
    now = time.time()
    points = [p for p in points if p[0] >= p[3]]  # a receipt cannot precede its original; drop mismatches
    last = max(points, key=lambda p: p[2])  # newest original the referee has receipted
    lag_h = (last[0] - last[3]) / 3600
    window = [p for p in points if p[0] >= last[0] - a.window_min * 60]
    first = min(window, key=lambda p: p[0])
    dt_h = max((last[0] - first[0]) / 3600, 1e-6)
    ref_rate = (last[2] - first[2]) / dt_h
    inflow_rows = [m for m in rows if ts_of(m.get("ts")) >= head_ts - a.window_min * 60]
    inflow_rate = len(inflow_rows) / (a.window_min / 60)
    behind = head_seq - last[2]
    eta_h = behind / ref_rate if ref_rate > 0 else float("inf")
    fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M")
    print(f"referee position: originals up to seq {last[2]} (posted {fmt(last[3])} UTC), receipted at {fmt(last[0])} -> lag {lag_h:.1f} h")
    print(f"room head: seq {head_seq} at {fmt(head_ts)} UTC; referee is {behind} messages behind")
    print(f"rates (last {a.window_min:.0f} min): referee {ref_rate:.0f} seq/h vs inflow {inflow_rate:.0f} seq/h -> "
          + ("backlog SHRINKING" if ref_rate > inflow_rate else "backlog GROWING") + f"; catch-up to today's head ~{eta_h:.1f} h at the referee's rate")
    print("receipt_time  receipt_seq  original_seq  original_time")
    for p in sorted(points, key=lambda p: p[0])[-8:]:
        print(f"  {fmt(p[0])}        {p[1]}       {p[2]}        {fmt(p[3])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
