"""Append-only JSONL ledger of every compute-channel session the runner has
opened -- our own spend proof, independent of any block explorer
(SPEND-PLAN.md: "Ledger"). Stdlib only.

One line per session, written once at open (status "open") and rewritten (a
new line is appended, never edited in place -- append-only) whenever its
status changes, ending at "settled" / "timed_out" / "disputed" / "refunded".
`report()` reads the file and reduces it to one row per UTC day: settled FLOP
and settled G_n, so it can be compared against Budget.daily_target from
budget.py.

Required fields (KeyError if missing on append):
  channel_id, miner, model_hash, escrow, settled, gn, receipt_root,
  our_signature, opened_at, settled_at, status

`escrow`/`settled`/`gn` are numbers; `settled`/`gn`/`settled_at`/`receipt_root`/
`our_signature` may be None before the session settles. Timestamps are
ISO-8601 UTC strings ("...Z"), matching the rest of this repo's jsonl logs
(roomkeeper.py, flopwatch.py).

CLI:  python3 -m flopspend.ledger --report [--target N] [--path FILE]
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_LEDGER_PATH = os.path.expanduser("~/.technocore-pulse/flopspend.jsonl")

REQUIRED_FIELDS = (
    "channel_id", "miner", "model_hash", "escrow", "settled", "gn",
    "receipt_root", "our_signature", "opened_at", "settled_at", "status",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_session(record: Dict[str, Any], path: Optional[str] = None) -> None:
    """Append one ledger line. Raises ValueError if a required field is
    missing (present-but-None is fine for the settle-time fields)."""
    missing = [k for k in REQUIRED_FIELDS if k not in record]
    if missing:
        raise ValueError(f"ledger record missing required field(s): {missing}")
    target = os.path.expanduser(path or DEFAULT_LEDGER_PATH)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_all(path: Optional[str] = None) -> List[Dict[str, Any]]:
    target = os.path.expanduser(path or DEFAULT_LEDGER_PATH)
    if not os.path.exists(target):
        return []
    rows = []
    with open(target, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _day_key(iso_ts: str) -> str:
    # Tolerate both "...Z" and "...+00:00"; only the date part is used.
    return iso_ts[:10]


def daily_totals(rows: Iterable[Dict[str, Any]]) -> "Dict[str, Dict[str, float]]":
    """One row per UTC day (keyed by settled_at's date) with summed
    `settled` FLOP and `gn`, counting only sessions that actually settled
    (status == "settled" and settled_at is set). Per R12.1a, `settled` is
    the reserved escrow paid in full on a cooperative close -- not a
    metered/partial amount."""
    totals: "Dict[str, Dict[str, float]]" = defaultdict(lambda: {"settled_flop": 0.0, "gn": 0.0, "sessions": 0})
    for row in rows:
        if row.get("status") != "settled" or not row.get("settled_at"):
            continue
        day = _day_key(row["settled_at"])
        totals[day]["settled_flop"] += float(row.get("settled") or 0.0)
        totals[day]["gn"] += float(row.get("gn") or 0.0)
        totals[day]["sessions"] += 1
    return dict(totals)


def format_report(rows: Iterable[Dict[str, Any]], daily_target: Optional[float] = None) -> str:
    totals = daily_totals(rows)
    if not totals:
        return "no settled sessions in the ledger yet"
    lines = ["day          settled_FLOP     G_n  sessions" + ("   vs_target" if daily_target else "")]
    for day in sorted(totals):
        t = totals[day]
        line = f"{day}  {t['settled_flop']:12.4f}  {t['gn']:6.0f}  {int(t['sessions']):8d}"
        if daily_target:
            pct = 100.0 * t["settled_flop"] / daily_target if daily_target else 0.0
            line += f"   {pct:6.1f}%"
        lines.append(line)
    if daily_target:
        lines.append(f"\ndaily target: {daily_target:.4f} FLOP")
    return "\n".join(lines)


def _main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m flopspend.ledger")
    ap.add_argument("--report", action="store_true", help="print per-day settled FLOP and G_n")
    ap.add_argument("--path", default=None, help="ledger path (default: ~/.technocore-pulse/flopspend.jsonl)")
    ap.add_argument("--target", type=float, default=None, help="daily target FLOP to compare against")
    args = ap.parse_args(argv)

    rows = read_all(args.path)
    print(format_report(rows, args.target))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
