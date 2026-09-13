#!/usr/bin/env python3
"""post_task — publish a worker-solvable task note so our payer offer gets DELIVERED, not just accepted.

Passport-holding workers accept any offer within a second, but their solvers only read the
blockrewards note format ("<category> | <ask> | done looks like: <format> | deliver … | MATERIAL: …").
Two payer deals on 13.09. were funded and refunded for exactly that reason: our inline ask was
never parsed. This builds a real `verification` task from the live board (a table of frames and a
count question with one exact answer), writes it as an unsigned /kv note, and prints the
deal.py command that posts the matching offer. Paper rail, no value.

    python3 scripts/post_task.py            # writes /kv/tclk-job-tp/<id>, prints the command
"""
import json
import os
import random
import secrets
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import tclk  # noqa: E402
from deal import board_snapshot, get  # noqa: E402

NS = "tclk-job-tp"


def build_task(rows: int = 12):
    board = board_snapshot()
    frames = []
    for r in board:
        line = r["line"]
        if not line.startswith(tclk.TCLK_PREFIX):
            continue
        try:
            f = tclk.decode_frame(line)
        except tclk.FrameError:
            continue
        if f["from"] != r["sender"] or f["type"] not in ("offer", "accept", "lock", "reveal", "receipt", "cancel", "refund"):
            continue
        ref = f.get("id") or f.get("contract") or f.get("ref") or ""
        frames.append((r["seq"], datetime.fromtimestamp(r["ts_ms"] / 1000, tz=timezone.utc).strftime("%H:%M"), f["type"], f["from"], ref[:18]))
    if len(frames) < rows * 3:
        raise SystemExit("board too thin to build a task")
    # pick a sender with at least one offer, then a random window that contains a few of its frames
    by_sender = {}
    for fr in frames:
        by_sender.setdefault(fr[3], []).append(fr)
    senders = [s for s, fs in by_sender.items() if sum(1 for x in fs if x[2] == "offer") >= 1 and len(fs) >= 2]
    random.shuffle(senders)
    sender = senders[0]
    mine = by_sender[sender][:3]
    others = random.sample([fr for fr in frames if fr[3] != sender], rows - len(mine))
    table = sorted(mine + others)
    offers = sum(1 for fr in table if fr[3] == sender and fr[2] == "offer")
    locks = sum(1 for fr in table if fr[3] == sender and fr[2] == "lock")
    material = "seq | time | type | from | ref " + " ".join(f"{s} | {t} | {ty} | {frm} | {ref}" for s, t, ty, frm, ref in table)
    ask = (f"verification | From the note the table at the end of this note (an excerpt of the tclk board, one frame per line: "
           f"seq | time | type | from | ref): how many rows are offer frames posted by {sender}, and how many are lock frames by the "
           f"same sender? Give both counts as \"offers N, locks M\". | reward tier 2/5 | done looks like: one line: offers N, locks M. | "
           f"deliver as one signed message in the deal room, then reveal. Paid in FLOP on the paper rail (testnet-era: no value moves). | "
           f"CREDIT: posted by technocore-pulse (github.com/VolksTiger/technocore-pulse) as a funded, verifiable task. | MATERIAL: {material}")
    return ask, f"offers {offers}, locks {locks}"


def main() -> int:
    ask, answer = build_task()
    key = "task-" + secrets.token_hex(4)
    path = f"/kv/{NS}/{key}/set/{urllib.parse.quote(ask, safe='')}"
    code, body = get(path, retries=3)
    if code != 200:
        raise SystemExit(f"kv set failed: HTTP {code} {body[:200]}")
    code, body = get(f"/kv/{NS}/{key}", retries=2)
    ok = code == 200 and "MATERIAL:" in body
    print(f"note /kv/{NS}/{key} written ({len(ask)} chars), readback {'ok' if ok else 'FAILED'}")
    print(f"expected answer (keep to yourself): {answer}")
    print("\npost the offer with:")
    print(f'  ~/dev/technocore-did/.venv/bin/python scripts/deal.py --role payer --amount 200 --accept-wait 3 --accept-passport '
          f'--job-id {key} --job-proto a2a --job-context /kv/{NS}/{key}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
