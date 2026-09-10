"""The workload queue: real jobs pulled from our own tools, so the testnet
spend is never idle prompts (SPEND-PLAN.md, "The workload queue"). Every
producer here works with a small local fixture when the real local
state/file it prefers isn't present -- flopspend makes NO network calls of
its own (the repo-wide rule: never contact technocore.chat or an RPC), so
anything that in the *live* tool comes from a technocore.chat GET (roomkeeper
digest()'s /rooms and /r/tclk-offers/export calls, for instance) falls back
to a documented offline default here, never to a network request.

Four producers, one per SPEND-PLAN.md queue source we can actually feed
offline today:

  roomkeeper_digest_job(counts)      -- counts: see load_roomkeeper_counts()
  boardintel_narrative_job(snapshot) -- snapshot: see load_boardintel_snapshot()
  flopwatch_changelog_job(event)     -- event: one flopwatch.jsonl change event
  brsolve_skip_job(spec)             -- spec: a task brsolve.solve() returned None for

Quality checks are deliberately modest: exact-match correctness needs a
reference answer only the third-party judge (blockrewards) or our own
publication process holds. What flopspend can check offline is shape --
length, required keywords, "did it actually answer the question" -- and each
quality_check says so in its own construction below.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, List, Optional

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# brsolve.py is a sibling module at the repo root (not part of the flopspend
# package); imported read-only for its solve() dispatcher, never modified.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import brsolve  # noqa: E402


@dataclass
class Job:
    kind: str
    prompt: str
    max_tokens: int
    quality_check: Callable[[Optional[str]], bool]


def _rubric(text: Optional[str], required_keywords: Iterable[str] = (), min_len: int = 1, max_len: int = 4000) -> bool:
    """Length + keyword rubric: a floor, not a correctness proof. Used for the
    narrative/prose job kinds, where the check is "did it actually address
    the material", not "is it word-for-word right"."""
    if not text:
        return False
    t = text.strip()
    if not (min_len <= len(t) <= max_len):
        return False
    lower = t.lower()
    return all(str(kw).lower() in lower for kw in required_keywords)


# ── roomkeeper digest ────────────────────────────────────────────────────

ROOM_HISTORY_PATH = os.path.expanduser("~/.technocore-pulse/room-history.jsonl")
HEALTH_PATH = os.path.expanduser("~/.technocore-pulse/health.jsonl")

# Fields roomkeeper.py's digest() only ever fills from a technocore.chat GET
# (/rooms?format=json, /r/tclk-offers/export) -- flopspend makes no such
# calls, so these are documented offline defaults, not live numbers.
DEFAULT_ROOMKEEPER_COUNTS = {
    "rooms_total": 41, "rooms_capacity": 64, "active_rooms": 9, "listed_rooms": 41,
    "notes_total": 512, "notes_capacity": 1024,
    "tclk_offer_kinds": {"offer": 88, "accept": 81, "lock": 40, "reveal": 38},
}


def _local_busiest_24h(path: str, top_n: int = 3) -> Optional[List[tuple]]:
    """Same computation as roomkeeper.py's digest() "busiest 24h" line, over
    the same local file -- no network involved either there or here."""
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f.readlines()[-4000:]:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    by_room: "dict[str, list]" = {}
    for r in rows:
        by_room.setdefault(r["room"], []).append(r)
    growth = []
    for room, rs in by_room.items():
        rs.sort(key=lambda x: x["ts"])
        if len(rs) >= 2 and rs[-1]["ts"] > rs[0]["ts"]:
            hours = (datetime.fromisoformat(rs[-1]["ts"]) - datetime.fromisoformat(rs[0]["ts"])).total_seconds() / 3600
            if hours >= 1:
                growth.append((room, ((rs[-1].get("last_seq") or 0) - (rs[0].get("last_seq") or 0)) / hours))
    growth.sort(key=lambda x: x[1], reverse=True)
    return growth[:top_n] if growth else None


def _local_uptime(path: str, window: int = 288) -> Optional[dict]:
    """Same computation as roomkeeper.py's digest() "uptime 24h" line."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f.readlines()[-window:] if line.strip()]
    if not rows:
        return None
    ok = sum(1 for r in rows if r.get("ok"))
    return {"uptime_pct": round(100.0 * ok / len(rows), 1), "probes": len(rows)}


def load_roomkeeper_counts(room_history_path: Optional[str] = None, health_path: Optional[str] = None) -> dict:
    """The reader counts roomkeeper.py's digest() computes -- the LOCAL half
    (busiest-room growth from room-history.jsonl, uptime from health.jsonl)
    recomputed here read-only from the same files when they exist; the
    network-only half (live room/notes totals, tclk-offers frame mix) falls
    back to DEFAULT_ROOMKEEPER_COUNTS, since flopspend makes no technocore.chat
    calls. Always returns a complete dict, real numbers where available."""
    counts = dict(DEFAULT_ROOMKEEPER_COUNTS)
    busiest = _local_busiest_24h(os.path.expanduser(room_history_path or ROOM_HISTORY_PATH))
    counts["busiest_24h"] = busiest if busiest is not None else [("d-technocore-intel", 3.2), ("tclk-offers", 41.7)]
    uptime = _local_uptime(os.path.expanduser(health_path or HEALTH_PATH))
    counts.update(uptime or {"uptime_pct": 99.3, "probes": 288})
    return counts


def roomkeeper_digest_job(counts: dict, max_tokens: int = 220) -> Job:
    busiest = ", ".join(f"{room} ({rate:.1f}/h)" for room, rate in counts.get("busiest_24h", [])[:3])
    prompt = (
        "Write ONE plain-English sentence (no markdown, at most 350 characters) reporting this "
        "technocore.chat network digest -- this replaces a fixed template with model-written "
        "prose that gets posted and signed into d-technocore-intel:\n"
        f"- rooms: {counts.get('rooms_total')}/{counts.get('rooms_capacity')} "
        f"({counts.get('active_rooms')} of {counts.get('listed_rooms')} listed rooms active in the last 2 min)\n"
        f"- notes: {counts.get('notes_total')}/{counts.get('notes_capacity')}\n"
        f"- busiest rooms by 24h growth: {busiest or 'none notably busy'}\n"
        f"- uptime over the last {counts.get('probes')} probes: {counts.get('uptime_pct')}%\n"
        f"- recent tclk-offers frame mix: {counts.get('tclk_offer_kinds')}\n"
        "Be factual and specific to these numbers; do not speculate beyond them."
    )
    rooms_total = counts.get("rooms_total")
    required = [str(rooms_total)] if rooms_total is not None else []

    def check(text: Optional[str]) -> bool:
        return _rubric(text, required_keywords=required, min_len=20, max_len=400) and "room" in (text or "").lower()

    return Job(kind="roomkeeper_digest", prompt=prompt, max_tokens=max_tokens, quality_check=check)


# ── boardintel narrative ─────────────────────────────────────────────────

BOARDINTEL_JSONL = os.path.expanduser("~/.technocore-pulse/boardintel.jsonl")


def load_boardintel_snapshot(path: Optional[str] = None) -> dict:
    """The latest line of ~/.technocore-pulse/boardintel.jsonl (one line per
    boardintel.py --every run -- the same `analyze()` result dict it appends
    with --out) if present, else the saved fixture snapshot with the same
    schema (flopspend/fixtures/boardintel_snapshot.json)."""
    target = os.path.expanduser(path) if path else BOARDINTEL_JSONL
    if os.path.exists(target):
        last = None
        with open(target, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if last is not None:
            return json.loads(last)
    with open(os.path.join(FIXTURES_DIR, "boardintel_snapshot.json"), encoding="utf-8") as f:
        return json.load(f)


def boardintel_narrative_job(snapshot: dict, max_tokens: int = 260) -> Job:
    win = snapshot.get("window", {})
    families = snapshot.get("task_families", {}).get("by_job_id_prefix", [])[:3]
    fam_txt = "; ".join(
        f"{fam['family']}: {fam['offers']} offers" + (f", pass rate {fam['pass_share']:.0%}" if fam.get("pass_share") is not None else "")
        for fam in families
    )
    prompt = (
        "Write ONE paragraph (3-5 sentences, plain English, no markdown) narrating this tclk/1 "
        "deal-board snapshot for the public status page (board.html):\n"
        f"- window: {win.get('offers_on_board')} offers, {win.get('accepts_on_board_valid')} accepts, "
        f"{win.get('verdict_lines')} verdicts over {win.get('board_span_hours')} hours\n"
        f"- task families: {fam_txt or 'no family data available'}\n"
        f"- distinct verdict posters: {win.get('distinct_verdict_posters')}, "
        f"distinct payees: {win.get('distinct_verdict_payees_by_suffix')}\n"
        "Describe what is happening on the board; do not just restate the numbers as a list."
    )
    required = [families[0]["family"]] if families else []

    def check(text: Optional[str]) -> bool:
        return _rubric(text, required_keywords=required, min_len=120, max_len=900)

    return Job(kind="boardintel_narrative", prompt=prompt, max_tokens=max_tokens, quality_check=check)


# ── flopwatch changelog ──────────────────────────────────────────────────

FLOPWATCH_JSONL = os.path.expanduser("~/.technocore-pulse/flopwatch.jsonl")


def is_change_event(event: dict) -> bool:
    """flopwatch.py's check_page() also emits baseline ({"baseline": True})
    and error ({"error": ...}) events; only an actual diff is worth a
    changelog entry."""
    return "diff" in event and "error" not in event


def load_flopwatch_events(path: Optional[str] = None, limit: int = 20) -> List[dict]:
    """Real change events from ~/.technocore-pulse/flopwatch.jsonl if
    present, else the saved fixture (flopspend/fixtures/flopwatch_events.jsonl)."""
    target = os.path.expanduser(path) if path else FLOPWATCH_JSONL
    if not os.path.exists(target):
        target = os.path.join(FIXTURES_DIR, "flopwatch_events.jsonl")
    events = []
    with open(target, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return [e for e in events if is_change_event(e)][-limit:]


def flopwatch_changelog_job(event: dict, max_tokens: int = 150) -> Job:
    page = event.get("page", "an unknown page")
    prompt = (
        f"Write ONE changelog-entry sentence (at most 240 characters, plain English) summarizing "
        f"this change to {page} on flop.finance, for the testnet readiness doc:\n"
        f"- version: {event.get('version')}\n"
        f"- 'Updated' stamp: {event.get('updated')}\n"
        f"- keyword-count deltas: {event.get('kw_delta')}\n"
        f"- newly linked pages: {event.get('new_links')}\n"
        f"- sample diff lines: {event.get('diff')}\n"
        "State specifically what changed; do not just say 'something changed'."
    )

    def check(text: Optional[str]) -> bool:
        return _rubric(text, required_keywords=[page], min_len=15, max_len=300)

    return Job(kind="flopwatch_changelog", prompt=prompt, max_tokens=max_tokens, quality_check=check)


# ── brsolve skips ────────────────────────────────────────────────────────

BRSOLVE_SKIPS_FIXTURE = os.path.join(FIXTURES_DIR, "brsolve_skips.txt")
_DONE_LOOKS_LIKE = re.compile(r"done looks like:\s*(.+)$")


def load_brsolve_specs(path: Optional[str] = None) -> List[str]:
    """One spec string per line. brsolve's specs come from a live
    blockrewards board (network), which flopspend never fetches, so this
    always reads a local file -- the saved fixture by default."""
    target = os.path.expanduser(path) if path else BRSOLVE_SKIPS_FIXTURE
    with open(target, encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def find_brsolve_skips(specs: Iterable[str], solver: Callable[[str], Optional[str]] = brsolve.solve) -> List[str]:
    """The subset of `specs` brsolve's own solver returns None for -- exactly
    the "families brsolve skips" SPEND-PLAN.md wants fed to a model instead."""
    return [spec for spec in specs if solver(spec) is None]


def brsolve_skip_job(spec: str, max_tokens: int = 120) -> Job:
    prompt = (
        "Answer this blockrewards board task with EXACTLY one line matching the required output "
        "format below, and nothing else (no explanation, no extra lines):\n" + spec
    )

    def check(text: Optional[str]) -> bool:
        """Best-effort structural check ONLY: brsolve is conservative by design
        (a wrong answer costs -5 there; it answers only when a template is
        unambiguous) and the judge's reference answer is not available
        offline, so this cannot check correctness -- only shape: one
        non-empty line, a sane length, and (when the spec states a "done
        looks like: ..." example) a rough length match to that example."""
        if not text:
            return False
        t = text.strip()
        if not t or "\n" in t or len(t) > 300:
            return False
        m = _DONE_LOOKS_LIKE.search(spec)
        if m:
            example = m.group(1).strip()
            if example and len(t) > 4 * max(len(example), 1) + 20:
                return False
        return True

    return Job(kind="brsolve_skip", prompt=prompt, max_tokens=max_tokens, quality_check=check)


# ── combined queue ───────────────────────────────────────────────────────

def build_default_jobs(
    room_history_path: Optional[str] = None,
    health_path: Optional[str] = None,
    boardintel_path: Optional[str] = None,
    flopwatch_path: Optional[str] = None,
    brsolve_specs_path: Optional[str] = None,
) -> List[Job]:
    """One job per current roomkeeper digest and boardintel snapshot, plus one
    per pending flopwatch change event and per brsolve-skipped spec -- exactly
    the four SPEND-PLAN.md sources this module can feed offline."""
    jobs = [
        roomkeeper_digest_job(load_roomkeeper_counts(room_history_path, health_path)),
        boardintel_narrative_job(load_boardintel_snapshot(boardintel_path)),
    ]
    jobs.extend(flopwatch_changelog_job(ev) for ev in load_flopwatch_events(flopwatch_path))
    skips = find_brsolve_skips(load_brsolve_specs(brsolve_specs_path))
    jobs.extend(brsolve_skip_job(spec) for spec in skips)
    return jobs


class WorkloadQueue:
    """Round-robins over a fixed list of jobs. `next_job()` is what
    runner.py calls each tick ("every N minutes, pick a job from the
    workload queue", SPEND-PLAN.md) -- deterministic and offline, so a
    dry run is reproducible."""

    def __init__(self, jobs: Optional[List[Job]] = None) -> None:
        self.jobs = list(jobs) if jobs is not None else build_default_jobs()
        if not self.jobs:
            raise ValueError("workload queue has no jobs to offer")
        self._next_index = 0

    def next_job(self) -> Job:
        job = self.jobs[self._next_index % len(self.jobs)]
        self._next_index += 1
        return job
