#!/usr/bin/env python3
"""boardintel.py — read-only analyzer of the tclk/1 deal board + verdict feed on technocore.chat.

Turns two public JSONL exports into reproducible numbers about how deals actually behave:
who bids first and wins, who judges and who never does, which task families pay out, and
where the protocol's optional fields get abused. Every number is derived only from the two
exports (plus tclk.py's own strict-fold audit, which reads the same saved board file) —
nothing is invented, inferred from other rooms, or fetched beyond those two GETs.

Inputs (one venue message per line, keys seq/ts/from/text/nonce/sig):
  board       GET /r/tclk-offers/export       — tclk1-prefixed frames (offer/accept/lock/...)
  deliveries  GET /r/tclk-deliveries/export    — plain-text lines; a subset are verdicts:
              "review <offer-id-8B-prefix> contract <contract-id-8B-prefix> payee <8-char DID
              suffix> PASS|FAIL <score> — <reason>", posted by the offer's own payer DID.

Metric definitions (one line each):
  window            — record/frame/verdict counts and the two exports' time spans.
  first_bidder      — all_offers_with_accept: over every offer with >=1 accept on the board,
                       latency of the FIRST accept (accept.ts - offer.ts): median + share <2s.
                       verdicted_offers: over offers whose winning payee we can also see on
                       this board (join: verdict's contract/offer-id prefix -> board accept/
                       offer id, payee suffix -> accept.from[-8:]): share of wins taken by the
                       first accept vs a later one, and the winning accept's latency (p25/50/75).
  per_poster        — per offer-poster DID (== verdict-author DID, empirically 1:1 here):
                       offers posted / offers with >=1 accept (board window), verdicts posted
                       PASS/FAIL (full deliveries window), judge rate = verdicts / offers-with-
                       accept, distinct payees judged, "newcomer" payees (this poster gave that
                       payee's first-ever verdict in the window), median offer->winning-accept
                       latency where the board window lets us see it.
  task_families      — offers grouped by job.id prefix (census-/math-/val-/task-/inf-/probe-/
                       attest-/other/no-job) and by job.proto: counts, median amount, PASS/FAIL
                       share among the verdicts we can join to a family.
  reveal_ref_hazard  — share of board reveal frames carrying the optional `ref` field, and
                       refunds on the board claiming "no reveal" for a contract whose reveal
                       frame IS on the board (board-visible only — see the note in the output).
  strict_fold        — tclk.py's own audit() SPEC-§2-strict fold outcome over the saved board
                       file (probe=0: no derived-room reads — too slow for a <=3-minute run).
  authenticity       — frame.from != record sender, and bad Ed25519 record signatures, exactly
                       as tclk.py audit() computes them (reused, not reimplemented).

Usage:
  python3 boardintel.py --report                                   # live run, human report
  python3 boardintel.py --json                                     # live run, JSON to stdout
  python3 boardintel.py --report --json                             # both (JSON, then report)
  python3 boardintel.py --board b.jsonl --deliveries d.jsonl --report --top 30   # offline

Read-only: two GETs (board export, deliveries export) plus Ed25519 record verification.
Never POSTs. A live run saves both raw exports verbatim to
~/.technocore-pulse/boardintel/<UTC-timestamp>-{board,deliveries}.jsonl before parsing, so
every number above is reproducible later with --board/--deliveries pointed at those files.

Python 3.9+, stdlib only (tclk.py's Ed25519 record verification uses `cryptography` when
installed; frames are "unverified" otherwise — see tclk.py's own docstring).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import tclk  # noqa: E402
import deal  # noqa: E402 - scripts/deal.py, reused for its retrying HTTP GET

SNAPSHOT_DIR = os.path.expanduser("~/.technocore-pulse/boardintel")
DELIVERIES_ROOM = "tclk-deliveries"

# "review <8B offer prefix> contract <8B contract prefix> payee <8-char DID suffix> PASS|FAIL
# <score> — <reason>" — score is a plain integer in most lines (0/1) but sometimes a decimal
# partial-credit value (e.g. "0.5", "0.857"), so `\d+(?:\.\d+)?`, not `\d+`.
VERDICT_RE = re.compile(
    r"^review (0x[0-9a-f]{16}) contract (0x[0-9a-f]{16}) payee ([A-Za-z0-9]{8}) "
    r"(PASS|FAIL) (\d+(?:\.\d+)?) — (.*)$",
    re.DOTALL,
)

JOB_FAMILY_PREFIXES = ("census-", "math-", "val-", "task-", "inf-", "probe-", "attest-")


# ── fetch + save (live) ──────────────────────────────────────────────────────

def prune_snapshots(keep: int) -> None:
    """Delete all but the newest `keep` snapshot files in SNAPSHOT_DIR."""
    try:
        names = sorted(f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".jsonl"))
    except FileNotFoundError:
        return
    for f in names[:-keep]:
        try:
            os.remove(os.path.join(SNAPSHOT_DIR, f))
        except OSError:
            pass


def fetch_and_save(path: str, kind: str, ts: str) -> str:
    """GET a venue /export via deal.get (reused: retries/backoff already written there) and
    save the raw body verbatim, so a live run is byte-for-byte reproducible offline later."""
    code, body = deal.get(path, timeout=120, retries=2)
    if code != 200:
        raise RuntimeError(f"GET {path} failed: HTTP {code} {body[:200]!r}")
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    out = os.path.join(SNAPSHOT_DIR, f"{ts}-{kind}.jsonl")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(body)
    return out


# ── deliveries: load + verdict parsing ──────────────────────────────────────

def load_deliveries(path: str) -> list:
    """Parse a /r/tclk-deliveries/export JSONL dump into {seq, ts_ms, sender, text}. These are
    plain venue messages, not tclk frames -- no decode_frame/verify_record here."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(m, dict):
                continue
            seq, ts, sender, text = m.get("seq"), m.get("ts"), m.get("from"), m.get("text")
            if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(ts, str) \
                    or not isinstance(sender, str) or not isinstance(text, str):
                continue
            try:
                ts_ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                continue
            out.append({"seq": seq, "ts_ms": ts_ms, "sender": sender, "text": text})
    out.sort(key=lambda r: r["seq"])
    return out


def parse_verdicts(records: list) -> list:
    """Pull verdict lines out of the deliveries feed (most lines are raw deliverables, not
    verdicts). One line each: offer/contract are 8-byte prefixes of the board's full ids;
    payee is the accepting DID's last 8 chars; poster (record sender) == the offer's payer."""
    out = []
    for r in records:
        mm = VERDICT_RE.match(r["text"])
        if not mm:
            continue
        off_pref, con_pref, payee_suf, outcome, score, reason = mm.groups()
        out.append({
            "seq": r["seq"], "ts_ms": r["ts_ms"], "poster": r["sender"],
            "offer_prefix": off_pref, "contract_prefix": con_pref, "payee_suffix": payee_suf,
            "outcome": outcome, "score": float(score), "reason": reason.strip(),
        })
    return out


# ── board: load + decode + clean-frame index ────────────────────────────────

def load_board_frames(path: str, check_signature: bool = True) -> dict:
    """Load + decode the board export into offer/accept indices built only from structurally
    valid, signed, sender-matching frames (verify_record + decode_frame + frame.from ==
    record.sender) -- so the joins below aren't built on forged or malformed input. Returns a
    dict of {records, offers: {id -> (record, frame)}, accepts_by_contract: {contract -> (r,f)},
    accepts_by_offer: {offer_id -> [(r, f), ...] sorted by (ts_ms, seq)}, reveals, refunds,
    and reject counts for accepts that don't recompute or self-accept}."""
    records = tclk.load_board(path)
    clean = []
    for r in records:
        if not r["line"].startswith(tclk.TCLK_PREFIX):
            continue
        ok, _ = tclk.verify_record(r, check_signature=check_signature)
        if not ok:
            continue
        try:
            f = tclk.decode_frame(r["line"])
        except tclk.FrameError:
            continue
        if f["from"] != r["sender"]:
            continue
        clean.append((r, f))

    offers = {}
    for r, f in clean:
        if f["type"] == "offer":
            offers.setdefault(f["id"], (r, f))

    accepts_by_contract, accepts_by_offer = {}, defaultdict(list)
    accept_no_offer = accept_bad_contract = accept_self = 0
    for r, f in clean:
        if f["type"] != "accept":
            continue
        o = offers.get(f["ref"])
        if o is None:
            accept_no_offer += 1
            continue
        if f["from"] == o[1]["from"]:
            accept_self += 1
            continue
        if tclk.contract_id(o[1], f) != f["contract"]:
            accept_bad_contract += 1
            continue
        accepts_by_contract.setdefault(f["contract"], (r, f))
        accepts_by_offer[f["ref"]].append((r, f))
    for oid in accepts_by_offer:
        accepts_by_offer[oid].sort(key=lambda rf: (rf[0]["ts_ms"], rf[0]["seq"]))

    return {
        "records": records,
        "offers": offers,
        "accepts_by_contract": accepts_by_contract,
        "accepts_by_offer": dict(accepts_by_offer),
        "reveals": [(r, f) for r, f in clean if f["type"] == "reveal"],
        "refunds": [(r, f) for r, f in clean if f["type"] == "refund"],
        "accept_no_offer": accept_no_offer,
        "accept_bad_contract": accept_bad_contract,
        "accept_self": accept_self,
    }


# ── verdict -> board join ────────────────────────────────────────────────────

def build_prefix_index(full_ids) -> dict:
    """full 0x+64-hex id -> its 0x+16-hex (8-byte) prefix, the granularity a verdict line
    names; a list per prefix so an (astronomically unlikely) collision is detectable, not silent."""
    idx = defaultdict(list)
    for fid in full_ids:
        idx[fid[:18]].append(fid)
    return idx


def join_verdict(v: dict, off_idx: dict, con_idx: dict, offers: dict, accepts_by_contract: dict,
                  accepts_by_offer: dict):
    """Resolve one verdict to (offer_id, (accept_record, accept_frame)) using the board's join
    keys: verdict.contract_prefix -> full contract id (primary -- unique per accept), else
    verdict.offer_prefix -> full offer id + payee-suffix match among that offer's accepts
    (task's specified fallback). Returns None when the board window doesn't hold the needed
    records (expected: the board's retained window is far shorter than the deliveries one) or
    a prefix is ambiguous (multiple full ids share it -- refuse rather than guess)."""
    con_matches = con_idx.get(v["contract_prefix"], [])
    if len(con_matches) == 1:
        rec = accepts_by_contract.get(con_matches[0])
        if rec is not None:
            r, f = rec
            if f["from"][-8:] == v["payee_suffix"]:
                return f["ref"], (r, f)
    off_matches = off_idx.get(v["offer_prefix"], [])
    if len(off_matches) == 1:
        oid = off_matches[0]
        if oid in offers:
            for r, f in accepts_by_offer.get(oid, []):
                if f["from"][-8:] == v["payee_suffix"]:
                    return oid, (r, f)
    return None


# ── stats helper ─────────────────────────────────────────────────────────────

def percentile_stats(vals: list) -> dict:
    """median/p25/p75 over a list of floats; None fields when there's no data."""
    if not vals:
        return {"median": None, "p25": None, "p75": None, "n": 0}
    if len(vals) == 1:
        return {"median": vals[0], "p25": vals[0], "p75": vals[0], "n": 1}
    qs = statistics.quantiles(sorted(vals), n=4, method="inclusive")
    return {"median": statistics.median(vals), "p25": qs[0], "p75": qs[2], "n": len(vals)}


# ── metric 2: first-bidder rule ─────────────────────────────────────────────

def first_bidder_metrics(board: dict, verdicts: list) -> dict:
    offers, accepts_by_offer = board["offers"], board["accepts_by_offer"]
    con_idx = build_prefix_index(board["accepts_by_contract"].keys())
    off_idx = build_prefix_index(offers.keys())

    all_first_latencies, within_2s, offers_with_accept = [], 0, 0
    for oid, accs in accepts_by_offer.items():
        if not accs:
            continue
        offers_with_accept += 1
        o_rec = offers[oid][0]
        lat = (accs[0][0]["ts_ms"] - o_rec["ts_ms"]) / 1000.0
        all_first_latencies.append(lat)
        if lat < 2.0:
            within_2s += 1

    joined = []
    for v in verdicts:
        hit = join_verdict(v, off_idx, con_idx, offers, board["accepts_by_contract"], accepts_by_offer)
        if hit is None:
            continue
        oid, (wr, wf) = hit
        accs = accepts_by_offer.get(oid) or []
        rank = next((i for i, (r, f) in enumerate(accs) if r["seq"] == wr["seq"]), None)
        if rank is None:
            continue
        o_rec = offers[oid][0]
        joined.append({
            "offer_id": oid, "poster": v["poster"], "winner_is_first": rank == 0,
            "winner_rank": rank, "n_accepts": len(accs),
            "latency_s": (wr["ts_ms"] - o_rec["ts_ms"]) / 1000.0,
        })

    win_first = sum(1 for j in joined if j["winner_is_first"])
    lat_stats = percentile_stats([j["latency_s"] for j in joined])

    return {
        "all_offers_with_accept": {
            "n": offers_with_accept,
            "median_first_accept_latency_s": statistics.median(all_first_latencies) if all_first_latencies else None,
            "share_first_accept_within_2s": (within_2s / offers_with_accept) if offers_with_accept else None,
        },
        "verdicted_offers": {
            "n": len(joined),
            "share_won_by_first_accept": (win_first / len(joined)) if joined else None,
            "share_won_by_later_accept": ((len(joined) - win_first) / len(joined)) if joined else None,
            "winning_accept_latency_s": lat_stats,
        },
        "_joined": joined,  # internal: reused by per_poster_metrics, stripped before JSON output
    }


# ── metric 3: per poster ─────────────────────────────────────────────────────

def per_poster_metrics(board: dict, verdicts: list, first_bidder: dict, top_n: int) -> dict:
    offers, accepts_by_offer = board["offers"], board["accepts_by_offer"]

    verdicts_sorted = sorted(verdicts, key=lambda v: (v["ts_ms"], v["seq"]))
    per_poster = defaultdict(lambda: {"pass": 0, "fail": 0, "payees": set(), "newcomers": 0})
    first_verdict_for_payee = {}  # payee_suffix -> poster of that payee's earliest verdict
    for v in verdicts_sorted:
        p = per_poster[v["poster"]]
        p["pass" if v["outcome"] == "PASS" else "fail"] += 1
        p["payees"].add(v["payee_suffix"])
        first_verdict_for_payee.setdefault(v["payee_suffix"], v["poster"])
    for poster in first_verdict_for_payee.values():
        per_poster[poster]["newcomers"] += 1

    offers_posted, offers_with_accept = Counter(), Counter()
    for oid, (r, f) in offers.items():
        offers_posted[f["from"]] += 1
        if accepts_by_offer.get(oid):
            offers_with_accept[f["from"]] += 1

    lat_by_poster = defaultdict(list)
    for j in first_bidder["_joined"]:
        lat_by_poster[j["poster"]].append(j["latency_s"])

    rows = []
    for did in sorted(set(per_poster) | set(offers_posted)):
        vp = per_poster.get(did, {"pass": 0, "fail": 0, "payees": set(), "newcomers": 0})
        total = vp["pass"] + vp["fail"]
        n_acc = offers_with_accept.get(did, 0)
        lat = lat_by_poster.get(did, [])
        rows.append({
            "did": did,
            "offers_posted": offers_posted.get(did, 0),
            "offers_with_accept": n_acc,
            "verdicts_pass": vp["pass"], "verdicts_fail": vp["fail"], "verdicts_total": total,
            "judge_rate_verdicts_per_accepted_offer": (total / n_acc) if n_acc else None,
            "distinct_payees_judged": len(vp["payees"]),
            "newcomer_payees_brought_in": vp["newcomers"],
            "ever_judged_a_newcomer": vp["newcomers"] > 0,
            "median_offer_to_winning_accept_latency_s": statistics.median(lat) if lat else None,
            "n_offer_to_winning_accept_samples": len(lat),
        })

    top_by_verdicts = sorted(rows, key=lambda x: (-x["verdicts_total"], x["did"]))[:top_n]
    never_judge = sorted(
        (r for r in rows if r["offers_with_accept"] >= 10 and r["verdicts_total"] == 0),
        key=lambda x: (-x["offers_with_accept"], x["did"]),
    )
    return {
        "top_by_verdicts": top_by_verdicts,
        "posters_that_never_judge": never_judge,
        "n_posters_total": len(rows),
        "caveat": "verdicts_* are counted over the whole deliveries window; offers_with_accept "
                  "only over the much narrower board window (see window.board_span_hours vs "
                  "window.deliveries_span_hours) -- judge_rate can exceed 1 and is not a "
                  "same-window ratio.",
    }


# ── metric 4: task families ─────────────────────────────────────────────────

def classify_family(job) -> str:
    if not job:
        return "no-job"
    jid = job.get("id") or ""
    for prefix in JOB_FAMILY_PREFIXES:
        if jid.startswith(prefix):
            return prefix.rstrip("-")
    return "other"


def task_family_metrics(board: dict, verdicts: list) -> dict:
    offers = board["offers"]
    con_idx = build_prefix_index(board["accepts_by_contract"].keys())
    off_idx = build_prefix_index(offers.keys())

    fam_counts, fam_amounts, proto_counts = Counter(), defaultdict(list), Counter()
    fam_of_offer = {}
    for oid, (r, f) in offers.items():
        fam = classify_family(f.get("job"))
        fam_of_offer[oid] = fam
        fam_counts[fam] += 1
        fam_amounts[fam].append(int(f["amount"]))
        if "job" in f:
            proto_counts[f["job"]["proto"]] += 1

    fam_verdicts = defaultdict(Counter)
    for v in verdicts:
        hit = join_verdict(v, off_idx, con_idx, offers, board["accepts_by_contract"], board["accepts_by_offer"])
        if hit is None:
            continue
        oid, _ = hit
        fam_verdicts[fam_of_offer[oid]][v["outcome"]] += 1

    families = []
    for fam, n in fam_counts.most_common():
        p, fa = fam_verdicts[fam]["PASS"], fam_verdicts[fam]["FAIL"]
        families.append({
            "family": fam, "offers": n, "median_amount": statistics.median(fam_amounts[fam]),
            "joined_verdicts_pass": p, "joined_verdicts_fail": fa,
            "pass_share": (p / (p + fa)) if (p + fa) else None,
        })
    return {"by_job_id_prefix": families, "by_job_proto": dict(proto_counts.most_common())}


# ── metric 5: reveal-with-ref hazard ────────────────────────────────────────

def reveal_ref_hazard(board: dict) -> dict:
    reveals, refunds = board["reveals"], board["refunds"]
    n = len(reveals)
    with_ref = sum(1 for r, f in reveals if "ref" in f)
    reveal_contracts = {f["contract"] for r, f in reveals}
    refund_with_reason = hazard = 0
    for r, f in refunds:
        reason = f.get("reason") or ""
        if not reason:
            continue
        refund_with_reason += 1
        if f["contract"] in reveal_contracts and "no reveal" in reason.lower():
            hazard += 1
    return {
        "reveal_frames_on_board": n,
        "reveal_with_ref": with_ref,
        "share_reveal_with_ref": (with_ref / n) if n else None,
        "refund_frames_with_reason": refund_with_reason,
        "refunds_claiming_no_reveal_despite_board_reveal": hazard,
        "note": "counts only what tclk-offers itself shows -- reveals/refunds for a contract "
                "may also live in that contract's derived deal room, which this tool does not "
                "read, so this is a lower bound, not a full picture.",
    }


# ── metrics 6 + 7: reuse tclk.py's own audit() ──────────────────────────────

def run_tclk_audit(board_path: str, verify_sigs: bool) -> dict:
    """Call tclk.py's own audit() (probe=0: skip derived-room reads, too slow here) over the
    same saved board file and capture its JSON result -- reuses its decode/fold/signature
    logic verbatim instead of reimplementing it."""
    fd, tmp_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            tclk.audit(board_path, 0, tmp_path, verify_sigs)
        with open(tmp_path) as fh:
            return json.load(fh)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def strict_fold_metrics(audit_data: dict) -> dict:
    return {
        "fold_strict_by_status": audit_data["fold_strict"],
        "note": "post-accept frames (lock/reveal/refund/receipt) mostly live in derived deal "
                "rooms (mb-p-tclk-<16hex>) this tool does not read (one GET per contract -- too "
                "slow for a <=3-minute run), so almost every contract here reads back as "
                "'accepted': not because the deal failed, but because SPEC §2 puts the "
                "rest of its life in a room this export can't see.",
    }


def authenticity_metrics(audit_data: dict) -> dict:
    b = audit_data["board"]
    return {
        "frame_from_ne_record_sender": b["from_mismatch"],
        "signatures_checked": b["signatures_checked"],
        "signatures_bad": b["signatures_bad"],
        "signatures_verified": b["signatures_verified"],
    }


# ── metric 1: window ─────────────────────────────────────────────────────────

def window_metrics(board: dict, deliveries: list, verdicts: list, audit_data: dict) -> dict:
    b = audit_data["board"]
    d_ts = [r["ts_ms"] for r in deliveries]
    deliveries_span_h = ((max(d_ts) - min(d_ts)) / 3_600_000) if d_ts else 0.0
    return {
        "board_records": b["records"], "board_tclk_lines": b["tclk_lines"],
        "board_span_hours": b["span_hours"],
        "board_distinct_senders": len({r["sender"] for r in board["records"]}),
        "board_decoded": b["decoded"], "board_decode_failures": b["decode_failures"],
        "board_frame_types": b["types"],
        "offers_on_board": len(board["offers"]),
        "accepts_on_board_valid": len(board["accepts_by_contract"]),
        "accepts_rejected_no_matching_offer": board["accept_no_offer"],
        "accepts_rejected_bad_contract_id": board["accept_bad_contract"],
        "accepts_rejected_self_accept": board["accept_self"],
        "deliveries_records": len(deliveries),
        "deliveries_span_hours": round(deliveries_span_h, 1),
        "deliveries_distinct_senders": len({r["sender"] for r in deliveries}),
        "verdict_lines": len(verdicts),
        "distinct_verdict_posters": len({v["poster"] for v in verdicts}),
        "distinct_verdict_payees_by_suffix": len({v["payee_suffix"] for v in verdicts}),
    }


# ── report formatting ────────────────────────────────────────────────────────

def _pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _sec(x):
    return "n/a" if x is None else f"{x:.2f}s"


def _num(x, nd=1):
    return "n/a" if x is None else f"{x:.{nd}f}"


def format_report(result: dict) -> str:
    w, fb, pp = result["window"], result["first_bidder"], result["per_poster"]
    fam, haz = result["task_families"], result["reveal_ref_hazard"]
    sf, auth = result["strict_fold"], result["authenticity"]
    lines = [f"boardintel — {result['generated_at']}  ({result['seconds']}s)", ""]

    lines.append("WINDOW")
    lines.append(f"  board: {w['board_records']} records / {w['board_tclk_lines']} tclk lines over "
                 f"{w['board_span_hours']} h, {w['board_distinct_senders']} distinct senders; "
                 f"offers {w['offers_on_board']}, valid accepts {w['accepts_on_board_valid']}")
    lines.append(f"  frame types: {w['board_frame_types']}")
    lines.append(f"  deliveries: {w['deliveries_records']} records over {w['deliveries_span_hours']} h, "
                 f"{w['deliveries_distinct_senders']} distinct senders; verdict lines {w['verdict_lines']} "
                 f"({w['distinct_verdict_posters']} posters, {w['distinct_verdict_payees_by_suffix']} payees by suffix)")
    lines.append("")

    lines.append("FIRST-BIDDER RULE")
    a = fb["all_offers_with_accept"]
    lines.append(f"  all offers w/ >=1 accept: n={a['n']}  median first-accept latency "
                 f"{_sec(a['median_first_accept_latency_s'])}  share <2s {_pct(a['share_first_accept_within_2s'])}")
    v = fb["verdicted_offers"]
    lat = v["winning_accept_latency_s"]
    lines.append(f"  verdicted offers (winner visible on this board window): n={v['n']}  "
                 f"first-accept wins {_pct(v['share_won_by_first_accept'])}  median winning-accept "
                 f"latency {_sec(lat['median'])} (p25 {_sec(lat['p25'])}, p75 {_sec(lat['p75'])})")
    lines.append("")

    lines.append(f"PER-POSTER (top {len(pp['top_by_verdicts'])} by verdicts, of {pp['n_posters_total']} posters seen)")
    for r in pp["top_by_verdicts"]:
        lines.append(f"  {r['did'][:28]:<28}  verdicts {r['verdicts_total']:>4} "
                     f"(P{r['verdicts_pass']}/F{r['verdicts_fail']})  offers-w/-accept "
                     f"{r['offers_with_accept']:>3}  judge_rate "
                     f"{_num(r['judge_rate_verdicts_per_accepted_offer'])}  payees "
                     f"{r['distinct_payees_judged']:>3}  newcomers "
                     f"{r['newcomer_payees_brought_in']:>3}  median lat "
                     f"{_sec(r['median_offer_to_winning_accept_latency_s'])}")
    never = pp["posters_that_never_judge"]
    lines.append(f"  posters that never judge (>=10 offers-w/-accept, 0 verdicts): {len(never)}")
    for r in never[:10]:
        lines.append(f"    {r['did'][:28]:<28}  offers-w/-accept {r['offers_with_accept']}")
    lines.append("")

    lines.append("TASK FAMILIES (by job.id prefix)")
    for f in fam["by_job_id_prefix"]:
        lines.append(f"  {f['family']:<10} offers {f['offers']:>5}  median amount {_num(f['median_amount'], 0):<8} "
                     f"joined verdicts P{f['joined_verdicts_pass']}/F{f['joined_verdicts_fail']}  "
                     f"pass_share {_pct(f['pass_share'])}")
    lines.append(f"  by job.proto: {fam['by_job_proto']}")
    lines.append("")

    lines.append("REVEAL-WITH-REF HAZARD")
    lines.append(f"  reveal frames on board {haz['reveal_frames_on_board']}, carrying ref "
                 f"{haz['reveal_with_ref']} ({_pct(haz['share_reveal_with_ref'])}); refunds claiming "
                 f"'no reveal' despite a board reveal: {haz['refunds_claiming_no_reveal_despite_board_reveal']} "
                 f"(of {haz['refund_frames_with_reason']} refunds w/ reason)")
    lines.append("")

    lines.append("STRICT FOLD (tclk.py audit(), SPEC §2, no derived-room probing)")
    lines.append(f"  {sf['fold_strict_by_status']}")
    lines.append("")

    lines.append("AUTHENTICITY")
    lines.append(f"  frame.from != record sender: {auth['frame_from_ne_record_sender']}  signatures "
                 f"checked {auth['signatures_checked']}, bad {auth['signatures_bad']} "
                 f"(verified={auth['signatures_verified']})")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", help="offline: path to a /r/tclk-offers/export JSONL dump (skips the live GET)")
    ap.add_argument("--deliveries", help="offline: path to a /r/tclk-deliveries/export JSONL dump (skips the live GET)")
    ap.add_argument("--json", action="store_true", help="print the full result as one JSON document to stdout")
    ap.add_argument("--report", action="store_true", help="print a human-readable report to stdout")
    ap.add_argument("--top", type=int, default=20, help="posters to rank by verdicts (default 20)")
    ap.add_argument("--no-verify", action="store_true", help="skip Ed25519 record-signature verification")
    ap.add_argument("--out", help="append the JSON result as one line to this JSONL file (the page builder reads it)")
    ap.add_argument("--every", type=float, default=0, help="minutes between runs; 0 = run once (pm2 mode: --every 30 --out ~/.technocore-pulse/boardintel.jsonl)")
    return ap


def bidder_populations(board_path: str) -> dict:
    """Two populations of bidders: accepts the reference decoder takes vs lines that say
    `"type":"accept"` but fail to decode (07.09.: 84% of accept-looking lines lack `contract`
    and share one non-canonical key order — one client, ~1,000 DIDs, never a winning bid)."""
    valid: Counter = Counter()
    invalid: Counter = Counter()
    reasons: Counter = Counter()
    key_orders: Counter = Counter()
    with open(board_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            text = m.get("text") or ""
            if not text.startswith(tclk.TCLK_PREFIX) or '"type":"accept"' not in text:
                continue
            try:
                tclk.decode_frame(text)
                valid[m.get("from")] += 1
            except tclk.FrameError as e:
                invalid[m.get("from")] += 1
                reasons[str(e)[:60]] += 1
                try:
                    key_orders[",".join(json.loads(text[len(tclk.TCLK_PREFIX):]).keys())] += 1
                except Exception:  # noqa: BLE001
                    key_orders["(not JSON)"] += 1
    return {
        "valid_accepts": sum(valid.values()),
        "valid_senders": len(valid),
        "invalid_accepts": sum(invalid.values()),
        "invalid_senders": len(invalid),
        "senders_in_both": len(set(valid) & set(invalid)),
        "invalid_reason": (reasons.most_common(1) or [("", 0)])[0][0],
        "invalid_reason_share": round((reasons.most_common(1) or [("", 0)])[0][1] / max(1, sum(invalid.values())), 3),
        "invalid_key_order": (key_orders.most_common(1) or [("", 0)])[0][0],
        "invalid_key_order_share": round((key_orders.most_common(1) or [("", 0)])[0][1] / max(1, sum(invalid.values())), 3),
        "top_invalid_sender_share": round((invalid.most_common(1) or [("", 0)])[0][1] / max(1, sum(invalid.values())), 3),
    }


def analyze(board_path: str, deliveries_path: str, top_n: int, verify_sigs: bool) -> dict:
    t0 = time.time()
    board = load_board_frames(board_path, check_signature=verify_sigs)
    deliveries = load_deliveries(deliveries_path)
    verdicts = parse_verdicts(deliveries)
    audit_data = run_tclk_audit(board_path, verify_sigs)

    fb = first_bidder_metrics(board, verdicts)
    pp = per_poster_metrics(board, verdicts, fb, top_n)
    fam = task_family_metrics(board, verdicts)
    haz = reveal_ref_hazard(board)
    win = window_metrics(board, deliveries, verdicts, audit_data)
    sf = strict_fold_metrics(audit_data)
    auth = authenticity_metrics(audit_data)
    bidders = bidder_populations(board_path)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs": {"board_path": board_path, "deliveries_path": deliveries_path},
        "window": win,
        "first_bidder": {k: v for k, v in fb.items() if not k.startswith("_")},
        "per_poster": pp,
        "task_families": fam,
        "reveal_ref_hazard": haz,
        "bidders": bidders,
        "strict_fold": sf,
        "authenticity": auth,
        "seconds": round(time.time() - t0, 1),
    }


def run_once(args) -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    board_path, deliveries_path = args.board, args.deliveries
    try:
        if board_path is None:
            board_path = fetch_and_save(f"/r/{tclk.OFFER_ROOM}/export", "board", ts)
        if deliveries_path is None:
            deliveries_path = fetch_and_save(f"/r/{DELIVERIES_ROOM}/export", "deliveries", ts)
        result = analyze(board_path, deliveries_path, args.top, not args.no_verify)
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.out:
        os.makedirs(os.path.dirname(os.path.expanduser(args.out)) or ".", exist_ok=True)
        with open(os.path.expanduser(args.out), "a", encoding="utf-8") as f:
            f.write(json.dumps(result, sort_keys=False) + "\n")
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=False))
    if args.report:
        print(format_report(result))
    return 0


def main() -> int:
    args = build_argparser().parse_args()
    if not args.json and not args.report and not args.out:
        args.report = True  # sensible default for an interactive run
    if not args.every:
        return run_once(args)
    while True:  # recorder mode: the board's retained window is ~0.5 h, so a series is the only history
        try:
            rc = run_once(args)
            prune_snapshots(keep=24)  # two exports of ~8 MB per run; keep the last 12 h reproducible
            print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} run done rc={rc}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} run failed: {e}", flush=True)
        time.sleep(args.every * 60)


if __name__ == "__main__":
    raise SystemExit(main())
