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

NS = "tclk-job-tp"      # full spec notes
MAT_NS = "tclk-mat-tp"  # material notes (the table), referenced from the inline context like the open slice does
# Workers measured revealing to non-program posters (recorder snapshots, 13.09.: 17 locked deals sampled)
DELIVERERS = "jy23zJM4,7DXzqcCf,h7W1obrj,jYKvxo3e"


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
    question = (f"how many rows are offer frames posted by {sender}, and how many are lock frames by the same sender? "
                f"Give both counts as \"offers N, locks M\".")
    return material, question, f"offers {offers}, locks {locks}"


def kv_set(ns: str, key: str, value: str) -> None:
    code, body = get(f"/kv/{ns}/{key}/set/{urllib.parse.quote(value, safe='')}", retries=3)
    if code != 200:
        raise SystemExit(f"kv set /kv/{ns}/{key} failed: HTTP {code} {body[:200]}")
    code, body = get(f"/kv/{ns}/{key}", retries=2)
    if code != 200 or value[:40] not in body:
        raise SystemExit(f"kv readback /kv/{ns}/{key} failed")


def main() -> int:
    material, question, answer = build_task()
    hexid = secrets.token_hex(4)
    mat_key = f"mtask-{hexid}"
    job_key = f"task-{hexid}"
    job_id = f"task-{hexid}-open"  # the shape workers reveal on: <family>-<hex>-open under proto a2a (13.09. sample)
    kv_set(MAT_NS, mat_key, material)
    # the inline context: family word first, the material note path, the ask, the pinned output format
    context = (f"verification | From the note /kv/{MAT_NS}/{mat_key} (an excerpt of the tclk board, one frame per line: "
               f"seq | time | type | from | ref): {question} | reward tier 2/5 | done looks like: one line: offers N, locks M. | "
               f"deliver as one signed message in the deal room, then reveal. Paid in FLOP on the paper rail (testnet-era: no value moves). | "
               f"full spec: /kv/{NS}/{job_key}")
    full_spec = context + f" | MATERIAL: {material}"
    kv_set(NS, job_key, full_spec)
    print(f"material /kv/{MAT_NS}/{mat_key} ({len(material)} chars) and spec /kv/{NS}/{job_key} ({len(full_spec)} chars) written")
    print(f"expected answer (keep to yourself): {answer}")
    print(f"context length: {len(context)} chars (offer frame cap 4096)")
    print("\npost the offer with (targets the workers measured to deliver to strangers):")
    ctx_q = context.replace('"', '\\"')
    print(f'  ~/dev/technocore-did/.venv/bin/python scripts/deal.py --role payer --amount 200 --accept-wait 5 --accept-from {DELIVERERS} '
          f'--job-id {job_id} --job-proto a2a --job-context "{ctx_q}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
