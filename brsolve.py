#!/usr/bin/env python3
"""brsolve — pure-Python solver for "blockrewards" board tasks.

blockrewards posts small, objectively-judged jobs on a technocore chat board
for AI agents. A task is a `spec` string (the question, ending with the exact
output format after "done looks like:") plus optional `material` (a table
rendered as one long line — see `parse_table`).

The judge compares our one-line answer to a hidden reference EXACTLY
(whitespace-normalized), so this module is conservative by design: it
answers only when a template is recognized and unambiguous, and returns
None otherwise. A wrong answer costs -5; a skipped task costs nothing.

Stdlib only. May import the sibling `tclk` module (tclk/1 port + fold).
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime

from tclk import fold_transcript, transcript_record, decode_frame, FrameError  # noqa: F401

__all__ = ["classify", "split_material", "parse_table", "solve", "solve_review"]

# ── shared helpers ────────────────────────────────────────────────────────

_MATERIAL_SEP = " | MATERIAL: "
_RANGE_CAP = 2_000_000  # brute-force cap for math range templates


def classify(spec: str) -> str:
    """Family word before the first ' | ', lowercased."""
    return spec.split(" | ", 1)[0].strip().lower()


def split_material(spec: str) -> tuple:
    """(spec without the material, material text or None)."""
    idx = spec.find(_MATERIAL_SEP)
    if idx == -1:
        return spec, None
    return spec[:idx], spec[idx + len(_MATERIAL_SEP):]


def parse_table(material) -> list:
    """Parse a 'header then rows, rows separated by single spaces' table.

    Cells never contain spaces (except the header/row separators), so: split
    the whole material on ' | ' to get raw tokens. Every token is a clean
    cell EXCEPT the one at each header/row and row/row boundary, which is
    two cells glued by the single inter-row space (e.g. "role 405782" is the
    header's last cell "role" glued to row 1's first cell "405782"). The
    first such glued token reveals the column count; split it, then
    re-chunk the flattened cell stream by that column count.
    """
    if not material:
        return []
    material = material.strip()
    if not material:
        return []
    raw = material.split(" | ")
    col_count = None
    for i, tok in enumerate(raw):
        if " " in tok:
            col_count = i + 1
            break
    if col_count is None:
        return [raw]
    flat = []
    for tok in raw:
        if " " in tok:
            a, b = tok.split(" ", 1)
            flat.append(a)
            flat.append(b)
        else:
            flat.append(tok)
    return [flat[i:i + col_count] for i in range(0, len(flat), col_count)]


def _rows_as_dicts(material):
    """parse_table() -> list of {header: value} dicts (header row consumed)."""
    table = parse_table(material)
    if len(table) < 1:
        return None
    header = [h.strip().lower() for h in table[0]]
    return [dict(zip(header, row)) for row in table[1:]]


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


# ═══════════════════════════════════════════════════════════════════════
# attest
# ═══════════════════════════════════════════════════════════════════════

def _solve_attest() -> str:
    return "__ATTEST__"


# ═══════════════════════════════════════════════════════════════════════
# protocol — fold a tclk/1 transcript
# ═══════════════════════════════════════════════════════════════════════

_PROTOCOL_ROW_START = re.compile(r"(tclk-offers|mb-p-tclk-[0-9a-f]{16}) \| ")
_PROTOCOL_FOLD_MARK = "Fold this tclk/1 transcript"


def _split_protocol_rows(material: str) -> list:
    starts = [m.start() for m in _PROTOCOL_ROW_START.finditer(material)]
    if not starts:
        return []
    rows = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(material)
        rows.append(material[s:e].strip())
    return rows


def _iso_to_ms(ts: str):
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _frame_type_best_effort(line: str):
    """Validated type via decode_frame, else a best-effort raw JSON peek."""
    try:
        return decode_frame(line)["type"]
    except FrameError:
        pass
    if line.startswith("tclk1 "):
        try:
            return json.loads(line[6:]).get("type")
        except Exception:  # noqa: BLE001
            pass
    return "unknown"


def _solve_protocol(pre: str, material) -> str | None:
    if not material or _PROTOCOL_FOLD_MARK not in pre:
        return None
    rows = _split_protocol_rows(material)
    if not rows:
        return None
    records = []
    for i, row in enumerate(rows, start=1):
        parts = row.split(" | ", 3)
        if len(parts) != 4:
            return None
        room, ts, sender, line = parts
        ts_ms = _iso_to_ms(ts)
        if ts_ms is None:
            return None
        records.append({
            "room": room, "seq": i, "ts_ms": ts_ms, "sender": sender,
            "nonce": str(i), "sig": "A" * 85 + "A", "line": line,
        })
    state, steps = fold_transcript(records, "strict", check_signature=False)
    if state is None:
        return None
    status = state["status"]
    if status not in ("proposed", "accepted", "locked", "claimed", "refunded", "cancelled"):
        return None
    rejected = [s for s in steps if not s["ok"]]
    if not rejected:
        return f"{status} — no rejected records."
    first = rejected[0]
    ftype = first.get("type")
    if ftype is None:
        ftype = _frame_type_best_effort(records[first["index"]]["line"])
    reason = first.get("reason") or "unknown reason"
    return f"{status} — rejected {ftype}: {reason}."


# ═══════════════════════════════════════════════════════════════════════
# math
# ═══════════════════════════════════════════════════════════════════════

_NUM_LE = r"(\d+)\s*(?:≤|<=)\s*n\s*(?:≤|<=)\s*(\d+)"

_MATH_DIGIT_SUM = re.compile(r"How many integers n with " + _NUM_LE + r" have digit sum exactly (\d+)\?")
_MATH_DIVISIBLE = re.compile(r"How many integers n with " + _NUM_LE + r" are divisible by (\d+)\?")
_MATH_PRIME_COUNT = re.compile(r"How many integers n with " + _NUM_LE + r" are prime\?")
_MATH_PALINDROME_COUNT = re.compile(r"How many integers n with " + _NUM_LE + r" are palindromes\?")
_MATH_SUM_RANGE = re.compile(r"What is the sum of (?:all )?integers n with " + _NUM_LE + r"\?")
_MATH_SMALLEST_PRIME_GT = re.compile(r"What is the smallest prime strictly greater than (\d+)\?")
_MATH_GRAPH = re.compile(
    r"Undirected weighted graph on nodes 0\.\.(\d+), edges \(a-b:w\): "
    r"([0-9,: \-]+?)\.\s*What is the length of the shortest path from node (\d+) to node (\d+)\?"
)
_EDGE_RE = re.compile(r"(\d+)-(\d+):(\d+)")


def _digit_sum(n: int) -> int:
    return sum(int(c) for c in str(n))


def _is_prime_trial(n: int) -> bool:
    if n < 2:
        return False
    if n < 4:
        return True
    if n % 2 == 0:
        return False
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True


def _sieve_primes_upto(n: int):
    sieve = bytearray([1]) * (n + 1)
    sieve[0:2] = b"\x00\x00"
    for i in range(2, int(math.isqrt(n)) + 1):
        if sieve[i]:
            sieve[i * i:n + 1:i] = bytearray(len(range(i * i, n + 1, i)))
    return sieve


def _solve_math(pre: str):
    m = _MATH_DIGIT_SUM.search(pre)
    if m:
        a, b, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if b < a or (b - a + 1) > _RANGE_CAP:
            return None
        count = sum(1 for n in range(a, b + 1) if _digit_sum(n) == s)
        return str(count)

    m = _MATH_DIVISIBLE.search(pre)
    if m:
        a, b, k = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if b < a or k <= 0:
            return None
        first = a + (-a) % k
        if first > b:
            return "0"
        count = (b - first) // k + 1
        return str(count)

    m = _MATH_PRIME_COUNT.search(pre)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b < a or (b - a + 1) > _RANGE_CAP or b > _RANGE_CAP:
            return None
        sieve = _sieve_primes_upto(b)
        count = sum(1 for n in range(max(a, 2), b + 1) if sieve[n])
        return str(count)

    m = _MATH_PALINDROME_COUNT.search(pre)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b < a or (b - a + 1) > _RANGE_CAP:
            return None
        count = sum(1 for n in range(a, b + 1) if str(n) == str(n)[::-1])
        return str(count)

    m = _MATH_SUM_RANGE.search(pre)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b < a:
            return None
        total = (a + b) * (b - a + 1) // 2
        return str(total)

    m = _MATH_SMALLEST_PRIME_GT.search(pre)
    if m:
        n = int(m.group(1))
        candidate = n + 1
        if candidate <= 2:
            return "2"
        if candidate % 2 == 0:
            candidate += 1
        limit = candidate + 1_000_000
        while candidate <= limit:
            if _is_prime_trial(candidate):
                return str(candidate)
            candidate += 2
        return None

    m = _MATH_GRAPH.search(pre)
    if m:
        node_max = int(m.group(1))
        edges_str = m.group(2)
        src, dst = int(m.group(3)), int(m.group(4))
        edges = _EDGE_RE.findall(edges_str)
        if not edges:
            return None
        adj = {i: [] for i in range(node_max + 1)}
        for a, b, w in edges:
            a, b, w = int(a), int(b), int(w)
            if a not in adj or b not in adj:
                return None
            adj[a].append((b, w))
            adj[b].append((a, w))
        if src not in adj or dst not in adj:
            return None
        dist = {i: math.inf for i in adj}
        dist[src] = 0
        visited = set()
        import heapq  # noqa: PLC0415
        pq = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            if u == dst:
                break
            for v, w in adj[u]:
                nd = d + w
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        if dist[dst] == math.inf:
            return None
        return str(dist[dst])

    return None


# ═══════════════════════════════════════════════════════════════════════
# census
# ═══════════════════════════════════════════════════════════════════════

_CENSUS_MARK = "Census over the excerpt"


def done_format(spec: str) -> str:
    """The exact output format the judge pinned after 'done looks like:' (up to the next ' | ')."""
    m = re.search(r"done looks like:\s*(?:one line exactly:\s*|one line:\s*)?(.*?)(?:\s*\||$)", spec, flags=re.S)
    return _norm_ws(m.group(1)) if m else ""


def _solve_census(pre: str, material) -> str | None:
    """Dispatch on the pinned output format — the census family has several question variants
    (07.09.: the proto template was answered for an 'assets=…; top_asset=…' question → would FAIL)."""
    if _CENSUS_MARK not in pre or not material:
        return None
    rows = _rows_as_dicts(material)
    if not rows:
        return None
    fmt = done_format(pre)
    from collections import Counter  # noqa: PLC0415
    if fmt.startswith("proto=<value>:<count>; paper_only=<n>"):
        if "proto" not in rows[0] or "rails" not in rows[0]:
            return None
        if "most common proto" not in pre or "single rail" not in pre:
            return None
        protos = Counter(r.get("proto", "-") or "-" for r in rows)
        best_count = max(protos.values())
        best_proto = sorted(p for p, c in protos.items() if c == best_count)[0]
        paper_only = sum(1 for r in rows if r.get("rails", "") == "paper")
        return f"proto={best_proto}:{best_count}; paper_only={paper_only}"
    if fmt.startswith("assets=<n>; top_asset=<asset>:<total>"):
        if "asset" not in rows[0] or "amount" not in rows[0]:
            return None
        if "distinct assets" not in pre or "largest total amount" not in pre or "alphabetically first" not in pre:
            return None
        totals = Counter()
        for r in rows:
            if not r.get("amount", "").isdigit():
                return None
            totals[r["asset"]] += int(r["amount"])
        top = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        return f"assets={len(totals)}; top_asset={top[0]}:{top[1]}"
    return None


# ═══════════════════════════════════════════════════════════════════════
# extraction
# ═══════════════════════════════════════════════════════════════════════

_EX_DISTINCT = re.compile(
    r"how many distinct (\w+) values appear\? Give the count, then list them once each in order of first appearance\."
)
_EX_COUNT_BY_VALUE = re.compile(
    r"how many rows have (\w+) \"([^\"]+)\"\? List their ids in seq order\."
)
_EX_RAIL_COUNT = re.compile(
    r"how many rows list the rail \"([^\"]+)\"\? Give the count, then their ids in seq order\."
)
_EX_IDS_BY_AMOUNT = re.compile(
    r"list the ids of the rows whose amount is (below|above|at least|at most) (\d+), in seq order\."
)


def _cmp_amount(op: str, value: int, threshold: int) -> bool:
    if op == "below":
        return value < threshold
    if op == "above":
        return value > threshold
    if op == "at least":
        return value >= threshold
    if op == "at most":
        return value <= threshold
    return False


def _solve_extraction(pre: str, material) -> str | None:
    if not material:
        return None
    rows = _rows_as_dicts(material)
    if not rows:
        return None

    m = _EX_DISTINCT.search(pre)
    if m:
        col = m.group(1).lower()
        if col not in rows[0]:
            return None
        seen = []
        for r in rows:
            v = r.get(col, "")
            if v not in seen:
                seen.append(v)
        return f"{len(seen)}: {', '.join(seen)}" if seen else "0: none"

    m = _EX_COUNT_BY_VALUE.search(pre)
    if m:
        col, val = m.group(1).lower(), m.group(2)
        if col not in rows[0] or "id" not in rows[0] or "seq" not in rows[0]:
            return None
        try:
            matched = sorted(
                (r for r in rows if r.get(col) == val),
                key=lambda r: int(r["seq"]),
            )
        except (KeyError, ValueError):
            return None
        if not matched:
            return None
        ids = [r["id"] for r in matched]
        return f"{len(ids)}: {', '.join(ids)}"

    m = _EX_RAIL_COUNT.search(pre)
    if m:
        rail = m.group(1)
        if "rails" not in rows[0] or "id" not in rows[0] or "seq" not in rows[0]:
            return None
        try:
            matched = sorted(
                (r for r in rows if rail in (r.get("rails") or "").split(",")),
                key=lambda r: int(r["seq"]),
            )
        except (KeyError, ValueError):
            return None
        if not matched:
            return None
        ids = [r["id"] for r in matched]
        return f"{len(ids)}: {', '.join(ids)}"

    m = _EX_IDS_BY_AMOUNT.search(pre)
    if m:
        op, threshold = m.group(1), int(m.group(2))
        if "amount" not in rows[0] or "id" not in rows[0] or "seq" not in rows[0]:
            return None
        try:
            matched = sorted(
                (r for r in rows if _cmp_amount(op, int(r["amount"]), threshold)),
                key=lambda r: int(r["seq"]),
            )
        except (KeyError, ValueError):
            return None
        if not matched:
            return "none"
        return ", ".join(r["id"] for r in matched)

    return None


# ═══════════════════════════════════════════════════════════════════════
# verification
# ═══════════════════════════════════════════════════════════════════════

_VER_OFFERS_AND_LOCKS = re.compile(
    r"how many rows are offer frames posted by (did:key:\S+), and how many are lock frames "
    r"by the same sender\? Give both counts as \"offers N, locks M\"\."
)
_VER_COUNT_TYPE_BY_DID = re.compile(
    r"how many rows are (offer|accept|lock|reveal|refund|cancel|receipt|heartbeat) frames "
    r"posted by (did:key:\S+)\? Give the count\."
)


def _solve_verification(pre: str, material) -> str | None:
    if not material:
        return None
    rows = _rows_as_dicts(material)
    if not rows or "type" not in rows[0] or "from" not in rows[0]:
        return None

    m = _VER_OFFERS_AND_LOCKS.search(pre)
    if m:
        did = m.group(1).rstrip(",.?")
        offers = sum(1 for r in rows if r.get("type") == "offer" and r.get("from") == did)
        locks = sum(1 for r in rows if r.get("type") == "lock" and r.get("from") == did)
        return f"offers {offers}, locks {locks}"

    m = _VER_COUNT_TYPE_BY_DID.search(pre)
    if m:
        ftype, did = m.group(1), m.group(2).rstrip(",.?")
        count = sum(1 for r in rows if r.get("type") == ftype and r.get("from") == did)
        return str(count)

    return None


# ═══════════════════════════════════════════════════════════════════════
# inference
# ═══════════════════════════════════════════════════════════════════════

_INF_EVEN_SEQ = re.compile(
    r"output the seq values that are even numbers, in ascending order, comma-separated \(or 'none'\)\."
)
_INF_EARLIEST_LATEST = re.compile(
    r"output the seq of the row with the earliest time and the seq of the row with the latest time, "
    r"as \"<earliest_seq> <latest_seq>\" \(ties: lower seq\)\."
)
_INF_TOP_N_AMOUNT = re.compile(
    r"output the seq values of the (\d+) rows with the largest amount, highest first "
    r"\(ties broken by lower seq first\), comma-separated\."
)
_INF_SUM_PER_PAYER = re.compile(
    r"sum the amount per payer and output the payer with the largest total and that total, "
    r"as \"<payer> <total>\" \(ties: ASCII-smaller payer\)\."
)


def _parse_hms(t: str):
    parts = t.split(":")
    if len(parts) != 3:
        return None
    try:
        h, mi, s = (int(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + mi * 60 + s


def _solve_inference(pre: str, material) -> str | None:
    if not material:
        return None
    rows = _rows_as_dicts(material)
    if not rows:
        return None

    if _INF_EVEN_SEQ.search(pre):
        if "seq" not in rows[0]:
            return None
        try:
            evens = sorted(int(r["seq"]) for r in rows if int(r["seq"]) % 2 == 0)
        except ValueError:
            return None
        return ", ".join(str(v) for v in evens) if evens else "none"

    if _INF_EARLIEST_LATEST.search(pre):
        if "seq" not in rows[0] or "time" not in rows[0]:
            return None
        parsed = []
        for r in rows:
            t = _parse_hms(r["time"])
            if t is None:
                return None
            parsed.append((t, int(r["seq"])))
        earliest = min(parsed, key=lambda x: (x[0], x[1]))
        latest = max(parsed, key=lambda x: (x[0], -x[1]))
        return f"{earliest[1]} {latest[1]}"

    m = _INF_TOP_N_AMOUNT.search(pre)
    if m:
        n = int(m.group(1))
        if "seq" not in rows[0] or "amount" not in rows[0]:
            return None
        try:
            parsed = [(int(r["amount"]), int(r["seq"])) for r in rows]
        except ValueError:
            return None
        parsed.sort(key=lambda x: (-x[0], x[1]))
        top = parsed[:n]
        return ", ".join(str(seq) for _, seq in top)

    if _INF_SUM_PER_PAYER.search(pre):
        if "payer" not in rows[0] or "amount" not in rows[0]:
            return None
        totals = {}
        for r in rows:
            try:
                totals[r["payer"]] = totals.get(r["payer"], 0) + int(r["amount"])
            except ValueError:
                return None
        if not totals:
            return None
        best_total = max(totals.values())
        best_payer = min(p for p, t in totals.items() if t == best_total)
        return f"{best_payer} {best_total}"

    return None


# ═══════════════════════════════════════════════════════════════════════
# validation
# ═══════════════════════════════════════════════════════════════════════

_VAL_END_ANCHOR = '". Does the deliverable give the reference answer'
_VAL_SEP = '". DELIVERABLE submitted by a worker: "'
_VAL_REF_ANCHOR = "REFERENCE ANSWER the task's author holds (private to you as validator): \""


def _tokens(s: str) -> list:
    return re.findall(r"[A-Za-z0-9]+", s.lower())


def _contiguous_subseq(needle: list, hay: list) -> bool:
    if not needle:
        return False
    n, m = len(needle), len(hay)
    if n > m:
        return False
    for i in range(m - n + 1):
        if hay[i:i + n] == needle:
            return True
    return False


def _extract_fallback_value(ref: str):
    m = re.search(r"https?://[^\s\"'<>]+", ref)
    if m:
        return m.group(0).rstrip(".,;:!?)\"'")
    m = re.search(r'"([^"]{2,})"', ref)
    if m:
        return m.group(1)
    m = re.search(r"'([^']{2,})'", ref)
    if m:
        return m.group(1)
    nums = re.findall(r"\d+", ref)
    if len(nums) == 1:
        return nums[0]
    return None


def _decide_validation(reference: str, deliverable: str):
    ref = _norm_ws(reference)
    deliv = _norm_ws(deliverable)
    ref_tok = _tokens(ref)
    deliv_tok = _tokens(deliv)

    if ref_tok and (ref_tok == deliv_tok or _contiguous_subseq(ref_tok, deliv_tok)):
        return f'PASS — exact match: "{ref}"'

    if ref.strip().upper() in ("PASS", "FAIL") and deliv_tok:
        first = deliv_tok[0]
        if first in ("pass", "fail") and first != ref.strip().lower():
            return f'FAIL — deliverable says "{first.upper()}" but the reference is "{ref}"'

    val = _extract_fallback_value(ref)
    if val:
        val_tok = _tokens(val)
        if val_tok and _contiguous_subseq(val_tok, deliv_tok):
            return f'PASS — exact match: "{ref}"'
        nums_ref = re.findall(r"\d+", ref)
        nums_deliv = re.findall(r"\d+", deliv)
        if len(nums_ref) == 1 and len(nums_deliv) == 1 and nums_ref[0] != nums_deliv[0]:
            return f'FAIL — deliverable says "{nums_deliv[0]}" but the reference is "{ref}"'

    return None


def _solve_validation(spec: str):
    idx_end = spec.rfind(_VAL_END_ANCHOR)
    if idx_end == -1:
        return None
    idx_sep = spec.rfind(_VAL_SEP, 0, idx_end)
    if idx_sep == -1:
        return None
    deliverable = spec[idx_sep + len(_VAL_SEP):idx_end]
    idx_ref = spec.rfind(_VAL_REF_ANCHOR, 0, idx_sep)
    if idx_ref == -1:
        return None
    reference = spec[idx_ref + len(_VAL_REF_ANCHOR):idx_sep]
    if not reference or not deliverable:
        return None
    return _decide_validation(reference, deliverable)


# ═══════════════════════════════════════════════════════════════════════
# review — needs an injected fetch(url) -> text|None
# ═══════════════════════════════════════════════════════════════════════

_REVIEW_URL = re.compile(r"From (https?://\S+?):\s*(.+?)\?", re.DOTALL)


def _extract_review_subject(question: str):
    """Best-effort key phrase to search for near the answer in the doc."""
    m = re.search(r"`([^`]+)`", question)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z][A-Z0-9_]{3,})\b", question)
    if m:
        return m.group(1)
    m = re.search(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", question)
    if m:
        return m.group(1)
    m = re.search(
        r"(?:default value for|value for|purpose of|name of|status of|header must be set|"
        r"URL pattern for|URL for|maximum [\w ]+|prefix for|format for|algorithm[\w ]*for)\s+(.+)",
        question, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(" ?.")
    m = re.match(r"\s*What is (?:the |a |an )?(.+)", question, re.IGNORECASE)
    if m:
        return m.group(1).strip(" ?.`")
    return None


def _find_value_near(text: str, subject: str):
    """Look for `subject` in `text`, then a value that obviously sits next to it."""
    if not subject:
        return None
    idx = text.find(subject)
    if idx == -1:
        idx = text.lower().find(subject.lower())
    if idx == -1:
        return None
    window = text[idx: idx + 400]

    # markdown table row: | subject | ... | value |
    line_start = text.rfind("\n", 0, idx) + 1
    line_end = text.find("\n", idx)
    line = text[line_start: line_end if line_end != -1 else len(text)]
    if "|" in line:
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c]
        if len(cells) >= 2:
            for c in cells:
                if subject.lower() not in c.lower() and re.search(r"[\w./%-]", c):
                    return c.strip("` ")

    # "SUBJECT": value  or  SUBJECT: value  or  SUBJECT=value  or  SUBJECT is/= "value"
    m = re.search(
        re.escape(subject) + r'["\']?\s*[:=]\s*("(?:[^"]+)"|\'(?:[^\']+)\'|[^\s,;\n]+)', window
    )
    if m:
        return m.group(1)

    m = re.search(
        re.escape(subject) + r'.{0,60}?\bis\b\s*("(?:[^"]+)"|\'(?:[^\']+)\'|[^\s,;.\n]+)',
        window, re.IGNORECASE,
    )
    if m:
        return m.group(1)

    m = re.search(
        r'default(?:s to| is| of|:)?\s*("(?:[^"]+)"|\'(?:[^\']+)\'|[^\s,;.\n]+)\b.{0,60}?' + re.escape(subject),
        window, re.IGNORECASE,
    )
    if m:
        return m.group(1)

    return None


def solve_review(spec: str, fetch=None):
    """review family: fetch the cited URL, find the token, answer only when
    one obvious value sits next to it."""
    try:
        if fetch is None:
            return None
        m = _REVIEW_URL.search(spec)
        if not m:
            return None
        url, question = m.group(1), m.group(2).strip()
        text = fetch(url)
        if not text:
            return None
        subject = _extract_review_subject(question)
        if not subject:
            return None
        value = _find_value_near(text, subject)
        if not value:
            return None
        value = value.strip()
        if not value or len(value) > 200:
            return None
        return value
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════
# dispatcher
# ═══════════════════════════════════════════════════════════════════════

def solve(spec: str, material=None):
    """Return the one-line answer, or None when not certain."""
    try:
        fam = classify(spec)
        pre, mat_from_spec = split_material(spec)
        mat = material if material is not None else mat_from_spec

        if fam == "attest":
            return _solve_attest()
        if fam == "protocol":
            return _solve_protocol(pre, mat)
        if fam == "math":
            return _solve_math(pre)
        if fam == "census":
            return _solve_census(pre, mat)
        if fam == "extraction":
            return _solve_extraction(pre, mat)
        if fam == "verification":
            return _solve_verification(pre, mat)
        if fam == "inference":
            return _solve_inference(pre, mat)
        if fam == "validation":
            return _solve_validation(spec)
        if fam == "review":
            return None  # needs a fetch(); use solve_review() instead
        return None
    except Exception:  # noqa: BLE001
        return None
