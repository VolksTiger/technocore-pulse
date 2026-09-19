"""sonnet_funnel -- reproducible measurement of the FLOP Labs sonnet-2 contest funnel.

Freezes the three public exports (rules, submissions, discovery) plus one read of every team
room (`d-sonnet-2-team-<id>`) the sources reveal, into a snapshot directory, then derives:
participation over time (teams/writers/organizers/voters), referee restarts, the submission
funnel (accepted/rejected/reasons/timing), and a per-room progress table (word count reached,
completion, flooding). Re-running against the same snapshot with no --refetch touches the
network not at all and reproduces byte-identical output.

  python3 scripts/sonnet_funnel.py --snapshot analysis/sonnet-2/snapshot --out analysis/sonnet-2/funnel.json \
      [--manifest analysis/sonnet-2/manifest.json] [--refetch] [--refetch-errors]

If the snapshot directory already holds rules.jsonl, submissions.jsonl, discovery.jsonl and at
least one file under teamrooms/, the run is fully offline. Otherwise every input is fetched
fresh and saved verbatim (raw export bytes, raw per-room JSON bodies) before analysis; a failed
room read is saved as a small `{"__fetch_error__": ...}` marker so a later offline run sees the
exact same room set and error. --refetch forces a fresh fetch even over a complete snapshot.
--refetch-errors leaves everything else offline but re-reads (up to 3 attempts, 10s apart) only
the team rooms whose saved file is still a `__fetch_error__` marker, then re-freezes those files;
a room that legitimately returned 0 messages is not touched by this (that is not a fetch error).

funnel.json is written with `json.dump(..., indent=1, sort_keys=True, ensure_ascii=False)` and no
run-specific fields (generated_at, invocation live in manifest.json only), so it is
byte-identical across offline re-runs of the same snapshot. The manifest records every input
file's sha256 + size, the output's sha256, generated_at, invocation, and the ts window seen.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from urllib.error import URLError

BASE_URL = "https://technocore.chat"
USER_AGENT = "technocore-pulse sonnet_funnel"

REF = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
RULES_ROOM = "d-sonnet-2-rules"
SUBMISSIONS_ROOM = "mb-sonnet-2-submissions"
DISCOVERY_ROOM = "mb-sonnet-2-discovery"
TEAM_ROOM_PREFIX = "d-sonnet-2-team-"
DEADLINE_EPOCH = 1789732800  # 2026-09-18 12:00 UTC, per sonnet.launch.v1

ROOM_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")  # filename-safe room names only


# --------------------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------------------

def epoch_of(ts_str):
    """ISO-8601 (Z or +00:00) -> POSIX seconds, or 0.0 if unparsable/missing."""
    try:
        return datetime.fromisoformat((ts_str or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def iso_of(epoch_val):
    if epoch_val is None:
        return None
    return datetime.fromtimestamp(epoch_val, timezone.utc).isoformat().replace("+00:00", "Z")


def utc_date(epoch_val):
    return datetime.fromtimestamp(epoch_val, timezone.utc).strftime("%Y-%m-%d")


def jl(m):
    """Parse a message's `text` field as JSON, or None."""
    try:
        return json.loads(m.get("text") or "")
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# pure analysis functions (unit-tested against synthetic rows, no network)
# --------------------------------------------------------------------------------------

def parse_status_series(rows):
    """rows: messages from the d-sonnet-2-rules export (any order).

    Returns {launch, series, team_rooms, restarts}: launch is the seq-1 sonnet.launch.v1
    payload; series is the (ts, teams, writers, organizers, voters, uptime_seconds) rows from
    every REF "referee status" message, sorted by ts ascending -- writers/organizers/voters are
    referee-accepted registrations by role (identity counts), not actions taken (words posted,
    votes cast, etc.); team_rooms is the sorted union of d-sonnet-2-team-* names seen in any
    status's intake.rooms; restarts is the list of ts where uptime_seconds dropped versus the
    immediately preceding status (a referee restart).
    """
    rows = sorted(rows, key=lambda m: int(m.get("seq") or 0))
    launch = {}
    series = []
    team_rooms = set()
    for m in rows:
        j = jl(m)
        if j is None:
            continue
        if int(m.get("seq") or 0) == 1 or j.get("type") == "sonnet.launch.v1":
            launch = j
            continue
        if j.get("subject") != "referee status":
            continue
        participants = j.get("participants") or {}
        intake_rooms = (j.get("intake") or {}).get("rooms") or []
        for r in intake_rooms:
            if isinstance(r, str) and r.startswith(TEAM_ROOM_PREFIX):
                team_rooms.add(r)
        series.append({
            "ts": m.get("ts"),
            "teams": j.get("teams"),
            "writers": participants.get("writer"),
            "organizers": participants.get("organizer"),
            "voters": participants.get("voter"),
            "uptime_seconds": j.get("uptime_seconds"),
        })
    series.sort(key=lambda r: epoch_of(r["ts"]))
    restarts = []
    for prev, cur in zip(series, series[1:]):
        pu, cu = prev.get("uptime_seconds"), cur.get("uptime_seconds")
        if isinstance(pu, (int, float)) and isinstance(cu, (int, float)) and cu < pu:
            restarts.append(cur["ts"])
    return {"launch": launch, "series": series, "team_rooms": sorted(team_rooms), "restarts": restarts}


def aggregate_submissions(rows):
    """rows: messages from the mb-sonnet-2-submissions export (any order).

    Joins sonnet.submit.v1 (from the submitter) with the referee's sonnet.receipt.v1 by
    (sender_did, request_id). Returns accepted (entry_id -> first accept ts/final_version/
    submitter_did/game_id/poem_room), rejected_count, rejections_by_reason, distinct_submitting
    _dids, distinct_game_ids (every game_id that attempted a submission), accepted_per_day
    (UTC calendar date -> count) and completeness (seq_min/seq_max/rows/missing_seq).
    """
    submits_by_key = {}
    distinct_submitting_dids = set()
    distinct_game_ids = set()
    for m in rows:
        frm = m.get("from")
        if frm == REF:
            continue
        j = jl(m)
        if j is None or j.get("type") != "sonnet.submit.v1":
            continue
        distinct_submitting_dids.add(frm)
        if j.get("game_id"):
            distinct_game_ids.add(j["game_id"])
        rid = j.get("request_id")
        if rid:
            submits_by_key[(frm, rid)] = {
                "game_id": j.get("game_id"),
                "poem_room": j.get("poem_room"),
                "final_version": j.get("final_version"),
            }

    accepted = {}
    rejected_count = 0
    rejections_by_reason = Counter()
    for m in rows:
        if m.get("from") != REF:
            continue
        j = jl(m)
        if j is None or j.get("type") != "sonnet.receipt.v1":
            continue
        status = j.get("status")
        key = (j.get("sender_did"), j.get("request_id"))
        if status == "accepted":
            eid = j.get("entry_id")
            if eid is None:
                continue
            ts = m.get("ts")
            info = submits_by_key.get(key, {})
            existing = accepted.get(eid)
            if existing is None or epoch_of(ts) < epoch_of(existing["ts"]):
                accepted[eid] = {
                    "entry_id": eid,
                    "ts": ts,
                    "final_version": info.get("final_version"),
                    "submitter_did": j.get("sender_did"),
                    "game_id": info.get("game_id"),
                    "poem_room": info.get("poem_room"),
                }
        elif status == "rejected":
            rejected_count += 1
            rejections_by_reason[j.get("reason") or "unknown"] += 1

    accepted_per_day = Counter()
    for entry in accepted.values():
        e = epoch_of(entry["ts"])
        if e:
            accepted_per_day[utc_date(e)] += 1

    return {
        "accepted": accepted,
        "rejected_count": rejected_count,
        "rejections_by_reason": dict(rejections_by_reason),
        "distinct_submitting_dids": distinct_submitting_dids,
        "distinct_game_ids": distinct_game_ids,
        "accepted_per_day": dict(accepted_per_day),
        "completeness": seq_completeness(rows),
    }


def seq_completeness(rows):
    seqs = sorted({int(m.get("seq")) for m in rows if m.get("seq") is not None})
    if not seqs:
        return {"seq_min": None, "seq_max": None, "rows": len(rows), "missing_seq": 0}
    span = seqs[-1] - seqs[0] + 1
    return {"seq_min": seqs[0], "seq_max": seqs[-1], "rows": len(rows), "missing_seq": span - len(seqs)}


def discovery_window(rows):
    """poem_room values and the seq/ts window of the mb-sonnet-2-discovery export."""
    poem_rooms = set()
    seqs = []
    epochs = []
    for m in rows:
        if m.get("seq") is not None:
            try:
                seqs.append(int(m.get("seq")))
            except (TypeError, ValueError):
                pass
        e = epoch_of(m.get("ts"))
        if e:
            epochs.append(e)
        j = jl(m)
        if j is None:
            continue
        pr = j.get("poem_room")
        if isinstance(pr, str) and pr:
            poem_rooms.add(pr)
    return {
        "poem_rooms": poem_rooms,
        "seq_min": min(seqs) if seqs else None,
        "seq_max": max(seqs) if seqs else None,
        "ts_min": iso_of(min(epochs)) if epochs else None,
        "ts_max": iso_of(max(epochs)) if epochs else None,
    }


def derive_room_stats(messages):
    """messages: one team room's ?format=json&limit=200 messages (any order).

    Returns retained, seq_min/seq_max, max_version (highest `version` of a REF
    sonnet.receipt.v1 with status accepted; 0 if none seen), complete (any such REF receipt
    with complete: true), last_accept_ts (ts of the accepted receipt with the highest ts),
    distinct_word_posters (distinct `from` on sonnet.word.v1), unchained_words (sonnet.word.v1
    whose previous_state_hash is 64 zeros) and window_truncated (retained >= 200: the read hit
    the ?limit=200 cap, so there may be earlier room history this read did not reach and
    max_version may be a lower bound. retained < 200 means the read returned the room's whole
    history -- rooms are contiguous from seq 1 or 2 (the referee's setup record can land at
    either), so nothing was cut whenever the cap wasn't hit).
    """
    messages = sorted(messages, key=lambda m: int(m.get("seq") or 0))
    retained = len(messages)
    seqs = [int(m.get("seq") or 0) for m in messages]
    seq_min = seqs[0] if seqs else None
    seq_max = seqs[-1] if seqs else None

    max_version = None
    last_accept_ts = None
    complete = False
    word_posters = set()
    unchained = 0

    for m in messages:
        j = jl(m)
        if j is None:
            continue
        typ = j.get("type")
        if typ == "sonnet.word.v1":
            if m.get("from"):
                word_posters.add(m["from"])
            if j.get("previous_state_hash") == "0" * 64:
                unchained += 1
        if m.get("from") == REF and typ == "sonnet.receipt.v1":
            if j.get("complete") is True:
                complete = True
            v = j.get("version")
            if j.get("status") == "accepted" and isinstance(v, (int, float)):
                ts = m.get("ts")
                if max_version is None or v > max_version:
                    max_version = v
                if last_accept_ts is None or epoch_of(ts) > epoch_of(last_accept_ts):
                    last_accept_ts = ts

    return {
        "retained": retained,
        "seq_min": seq_min,
        "seq_max": seq_max,
        "max_version": max_version if max_version is not None else 0,
        "complete": complete,
        "last_accept_ts": last_accept_ts,
        "distinct_word_posters": len(word_posters),
        "unchained_words": unchained,
        "window_truncated": retained >= 200,
    }


def guess_game_id(room, accepted):
    """Best-effort game_id for a team room name, used only for the 'submitted' flag.

    Team rooms are canonically d-sonnet-2-team-<game_id>, but a room may instead have been
    guessed from an accepted entry_id (which is not always the same value); when the suffix
    matches an accepted entry whose record carries an explicit game_id, that wins.
    """
    if not room.startswith(TEAM_ROOM_PREFIX):
        return None
    suffix = room[len(TEAM_ROOM_PREFIX):]
    entry = accepted.get(suffix)
    if entry and entry.get("game_id"):
        return entry["game_id"]
    return suffix


def room_outcome(retained, complete, max_version, submitted):
    """Classify a team room into one of five mutually-exclusive, exhaustive outcomes.

    Priority order (matches how much each signal is worth trusting): submitted (known from the
    submissions export even when the room itself is unreadable) beats complete_not_submitted
    (we saw the finishing receipt but no accepted entry references this room) beats unreadable
    (retained 0, so stalled/never_started cannot be distinguished) beats stalled (has at least
    one accepted word, not complete, not submitted) beats never_started (readable, no accepted
    word at all -- only the setup record and/or unchained flood words). `flooded` (unchained_
    words > 0) is a separate overlay, not part of this partition.
    """
    if submitted:
        return "submitted"
    if complete:
        return "complete_not_submitted"
    if retained == 0:
        return "unreadable"
    if max_version >= 1:
        return "stalled"
    return "never_started"


# --------------------------------------------------------------------------------------
# network / snapshot I/O
# --------------------------------------------------------------------------------------

def http_get(url, timeout, retries=1, backoff=1.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (URLError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff)
    raise last_err


def is_fetch_error_marker(data):
    """True if a saved teamrooms/<room>.json holds our {"__fetch_error__": ...} sentinel
    (a failed read), as opposed to a real (possibly empty) response body."""
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return False
    return isinstance(obj, dict) and "__fetch_error__" in obj


def parse_jsonl(data):
    rows = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def load_teamroom_body(data):
    """Raw bytes of a saved teamrooms/<room>.json -> (messages, error)."""
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except ValueError as e:
        return [], f"unreadable snapshot file: {e}"
    if isinstance(obj, dict) and "__fetch_error__" in obj:
        return [], obj["__fetch_error__"]
    messages = obj.get("messages") if isinstance(obj, dict) else None
    if messages is None:
        return [], "unexpected response shape (no 'messages' key)"
    if len(messages) == 0:
        return [], "empty response (0 messages)"
    return messages, None


def snapshot_is_complete(snap):
    need = ["rules.jsonl", "submissions.jsonl", "discovery.jsonl"]
    if not all(os.path.isfile(os.path.join(snap, n)) for n in need):
        return False
    teamdir = os.path.join(snap, "teamrooms")
    return os.path.isdir(teamdir) and any(f.endswith(".json") for f in os.listdir(teamdir))


def file_record(data, rows):
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "rows": rows}


def collect_epochs(rows):
    return [e for e in (epoch_of(m.get("ts")) for m in rows) if e]


def script_sha256():
    with open(__file__, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default="analysis/sonnet-2/snapshot")
    ap.add_argument("--out", default="analysis/sonnet-2/funnel.json")
    ap.add_argument("--manifest", default="analysis/sonnet-2/manifest.json")
    ap.add_argument("--refetch", action="store_true", help="force re-download even if the snapshot looks complete")
    ap.add_argument("--refetch-errors", action="store_true",
                     help="re-read only team rooms whose saved file is a __fetch_error__ marker (3 attempts, 10s apart)")
    a = ap.parse_args()

    snap = a.snapshot
    rules_path = os.path.join(snap, "rules.jsonl")
    subs_path = os.path.join(snap, "submissions.jsonl")
    disc_path = os.path.join(snap, "discovery.jsonl")
    teamdir = os.path.join(snap, "teamrooms")

    offline = snapshot_is_complete(snap) and not a.refetch
    file_meta = {}
    all_epochs = []
    skipped_unsafe = []

    if offline:
        print(f"snapshot complete under {snap}; running fully offline (no network)")
        with open(rules_path, "rb") as f:
            rules_bytes = f.read()
        with open(subs_path, "rb") as f:
            subs_bytes = f.read()
        with open(disc_path, "rb") as f:
            disc_bytes = f.read()
        rules_rows, subs_rows, disc_rows = parse_jsonl(rules_bytes), parse_jsonl(subs_bytes), parse_jsonl(disc_bytes)
        teamroom_bytes = {}
        for fn in sorted(os.listdir(teamdir)):
            if fn.endswith(".json"):
                with open(os.path.join(teamdir, fn), "rb") as f:
                    teamroom_bytes[fn[:-5]] = f.read()
    else:
        print("snapshot missing or --refetch given; fetching fresh from technocore.chat")
        os.makedirs(teamdir, exist_ok=True)

        rules_bytes = http_get(f"{BASE_URL}/r/{RULES_ROOM}/export", timeout=600)
        with open(rules_path, "wb") as f:
            f.write(rules_bytes)
        subs_bytes = http_get(f"{BASE_URL}/r/{SUBMISSIONS_ROOM}/export", timeout=600)
        with open(subs_path, "wb") as f:
            f.write(subs_bytes)
        disc_bytes = http_get(f"{BASE_URL}/r/{DISCOVERY_ROOM}/export", timeout=600)
        with open(disc_path, "wb") as f:
            f.write(disc_bytes)
        rules_rows, subs_rows, disc_rows = parse_jsonl(rules_bytes), parse_jsonl(subs_bytes), parse_jsonl(disc_bytes)
        print(f"rules: {len(rules_rows)} rows | submissions: {len(subs_rows)} rows | discovery: {len(disc_rows)} rows")

        status_probe = parse_status_series(rules_rows)
        subs_probe = aggregate_submissions(subs_rows)
        disc_probe = discovery_window(disc_rows)

        candidates = set(status_probe["team_rooms"])
        candidates |= {f"{TEAM_ROOM_PREFIX}{eid}" for eid in subs_probe["accepted"]}
        candidates |= {f"{TEAM_ROOM_PREFIX}{gid}" for gid in subs_probe["distinct_game_ids"]}
        candidates |= disc_probe["poem_rooms"]
        candidates = sorted(candidates)
        print(f"probing {len(candidates)} candidate team rooms (0.15s spacing)")

        teamroom_bytes = {}
        for i, room in enumerate(candidates, 1):
            if not ROOM_NAME_RE.match(room):
                # Not expected for real contest room names (always d-sonnet-2-team-<id>-shaped); skip rather
                # than write under a hashed filename, which an offline re-run could not map back to this room.
                skipped_unsafe.append(room)
                continue
            try:
                body = http_get(f"{BASE_URL}/r/{room}?format=json&limit=200", timeout=60)
            except Exception as e:  # noqa: BLE001 -- record and move on, per-room failures are expected
                body = json.dumps({"__fetch_error__": str(e)}).encode("utf-8")
            with open(os.path.join(teamdir, f"{room}.json"), "wb") as f:
                f.write(body)
            teamroom_bytes[room] = body
            if i % 20 == 0 or i == len(candidates):
                print(f"  {i}/{len(candidates)} rooms fetched")
            time.sleep(0.15)
        if skipped_unsafe:
            print(f"skipped {len(skipped_unsafe)} candidate room name(s) not safe as a filename: {skipped_unsafe}")

    if a.refetch_errors:
        bad_rooms = sorted(r for r, data in teamroom_bytes.items() if is_fetch_error_marker(data))
        if bad_rooms:
            print(f"--refetch-errors: retrying {len(bad_rooms)} room(s) with a stored fetch-error marker: {bad_rooms}")
        for room in bad_rooms:
            try:
                body = http_get(f"{BASE_URL}/r/{room}?format=json&limit=200", timeout=60, retries=2, backoff=10.0)
                print(f"  {room}: recovered")
            except Exception as e:  # noqa: BLE001 -- record and move on, per-room failures are expected
                body = json.dumps({"__fetch_error__": str(e)}).encode("utf-8")
                print(f"  {room}: still failing ({e})")
            with open(os.path.join(teamdir, f"{room}.json"), "wb") as f:
                f.write(body)
            teamroom_bytes[room] = body

    # ---- record snapshot file provenance (both branches converge here) ----
    file_meta["rules.jsonl"] = file_record(rules_bytes, len(rules_rows))
    file_meta["submissions.jsonl"] = file_record(subs_bytes, len(subs_rows))
    file_meta["discovery.jsonl"] = file_record(disc_bytes, len(disc_rows))
    all_epochs += collect_epochs(rules_rows) + collect_epochs(subs_rows) + collect_epochs(disc_rows)

    teamroom_data = {}  # room -> (messages, error)
    for room, data in teamroom_bytes.items():
        messages, err = load_teamroom_body(data)
        file_meta[f"teamrooms/{room}.json"] = file_record(data, len(messages))
        all_epochs += collect_epochs(messages)
        teamroom_data[room] = (messages, err)

    # ---- analysis ----
    status = parse_status_series(rules_rows)
    subs = aggregate_submissions(subs_rows)
    disc = discovery_window(disc_rows)
    accepted = subs["accepted"]
    accepted_game_ids = {e["game_id"] for e in accepted.values() if e.get("game_id")}
    accept_ts_by_game_id = {e["game_id"]: e["ts"] for e in accepted.values() if e.get("game_id")}

    rooms = []
    for room, (messages, err) in teamroom_data.items():
        stats = derive_room_stats(messages)
        if err is None and stats["retained"] == 0:
            err = "empty response (0 messages)"
        submitted = guess_game_id(room, accepted) in accepted_game_ids
        entry = {"room": room, "error": err, "submitted": submitted}
        entry.update(stats)
        entry["outcome"] = room_outcome(stats["retained"], stats["complete"], stats["max_version"], submitted)
        rooms.append(entry)
    rooms.sort(key=lambda r: (-r["max_version"], r["room"]))

    observed = [r for r in rooms if r["retained"] > 0]
    rooms_with_any_word = sum(1 for r in observed if r["max_version"] >= 1)
    rooms_complete = sum(1 for r in observed if r["complete"])
    outcome_counts = Counter(r["outcome"] for r in rooms)
    outcomes = {k: outcome_counts.get(k, 0) for k in
                ("submitted", "complete_not_submitted", "stalled", "never_started", "unreadable")}
    outcomes["flooded"] = sum(1 for r in rooms if r["unchained_words"] > 0)
    # participants_series rows are (ts, teams, writers, organizers, voters); uptime_seconds is
    # internal to parse_status_series (restart detection) and not part of the emitted schema.
    series_fields = ("ts", "teams", "writers", "organizers", "voters")
    series_out = [{k: r[k] for k in series_fields} for r in status["series"]]
    final = series_out[-1] if series_out else {}
    teams_allocated = final.get("teams")

    def pct(part):
        return round(100 * part / teams_allocated, 1) if teams_allocated else None

    histogram = {"0": 0, "1-29": 0, "30-69": 0, "70-99": 0, "100+": 0}
    for r in observed:
        v = r["max_version"]
        if v <= 0:
            histogram["0"] += 1
        elif v < 30:
            histogram["1-29"] += 1
        elif v < 70:
            histogram["30-69"] += 1
        elif v < 100:
            histogram["70-99"] += 1
        else:
            histogram["100+"] += 1

    writers_final, voters_final = final.get("writers"), final.get("voters")
    voters_per_writer = round(voters_final / writers_final, 4) if writers_final else None

    accepted_ts_epochs = [epoch_of(e["ts"]) for e in accepted.values() if e.get("ts")]
    accepted_last_24h = sum(1 for e in accepted_ts_epochs if DEADLINE_EPOCH - 24 * 3600 <= e <= DEADLINE_EPOCH)
    accepted_last_2h = sum(1 for e in accepted_ts_epochs if DEADLINE_EPOCH - 2 * 3600 <= e <= DEADLINE_EPOCH)

    empty_rooms = [r for r in rooms if r["error"] == "empty response (0 messages)"]
    empty_submitted_epochs = []
    for r in empty_rooms:
        if r["submitted"]:
            ts = accept_ts_by_game_id.get(guess_game_id(r["room"], accepted))
            if ts:
                empty_submitted_epochs.append(epoch_of(ts))

    caveats = [
        "team_rooms_observed is smaller than teams_allocated by construction: the sources only reveal a room "
        "when it was listed in a 4-hourly referee intake snapshot, appears on an accepted or attempted "
        "submission, or was visible within the discovery room's retained window -- teams that never surfaced "
        "in any of those three ways are invisible to this measurement.",
        "mb-sonnet-2-discovery retains only its most recent few thousand messages; poem_room values from "
        "earlier in the contest are not recoverable from it, so the discovery-derived slice of the team-room "
        "union is a lower bound, not a complete list.",
        "per-room max_version is a lower bound whenever window_truncated is true (retained >= 200, i.e. the "
        "?limit=200 read cap was hit): earlier room history beyond the returned window may hold a higher "
        "accepted version. When retained < 200 the read is the room's whole history (rooms are contiguous "
        "from seq 1 or 2), so nothing was cut and max_version is exact.",
        f"{sum(1 for r in rooms if r['window_truncated'])}/{len(rooms)} read team rooms have window_truncated=true "
        "(hit the 200-message cap); rooms below the cap that show max_version 0 only ever posted the referee's "
        "setup record (and/or unchained flood words) -- see outcome=never_started, not a truncation artifact.",
        "sonnet.receipts.v1 (plural, batch) messages in the submissions export are not parsed into the "
        "accept/reject counts below, which are built from individual sonnet.receipt.v1 records only.",
        f"discovery.jsonl seq window {disc['seq_min']}..{disc['seq_max']}, ts window {disc['ts_min']}..{disc['ts_max']}, "
        f"{len(disc['poem_rooms'])} distinct poem_room values found in it.",
        "participants.* in each referee status (teams/writers/organizers/voters) are referee-accepted "
        "registrations by role (identity counts), not counts of actions taken; the launch record's own "
        "voters/writers arrays are empty and carry no additional information.",
        f"{len(empty_rooms)} team room(s) returned an empty response (0 messages, not a fetch error) at read "
        "time -- the room is no longer readable (reaped, or reset by its owner; this snapshot cannot tell "
        "which). Of those, "
        + (f"{len(empty_submitted_epochs)} had an accepted entry (outcome=submitted despite retained=0); the "
           f"latest was accepted at {iso_of(max(empty_submitted_epochs))} UTC."
           if empty_submitted_epochs else "none had an accepted entry."),
    ]
    if skipped_unsafe:
        caveats.append(f"{len(skipped_unsafe)} candidate room name(s) were not filename-safe and were not fetched: {skipped_unsafe}.")
    still_erroring = sorted(r for r, data in teamroom_bytes.items() if is_fetch_error_marker(data))
    if still_erroring:
        caveats.append(f"{len(still_erroring)} team room(s) still carry a fetch-error marker after the last run "
                        f"(retry with --refetch-errors): {still_erroring}.")
    launch = status.get("launch") or {}
    if isinstance(launch.get("deadline"), (int, float)) and int(launch["deadline"]) != DEADLINE_EPOCH:
        caveats.append(
            f"sonnet.launch.v1 deadline field ({launch['deadline']}) differs from the hardcoded DEADLINE_EPOCH "
            f"({DEADLINE_EPOCH}) used for all before-deadline math in this script."
        )

    funnel_obj = {
        "funnel": {
            "teams_allocated": teams_allocated,
            "team_rooms_observed": len(observed),
            "team_rooms_observed_pct": pct(len(observed)),
            "rooms_with_any_accepted_word": rooms_with_any_word,
            "rooms_with_any_accepted_word_pct": pct(rooms_with_any_word),
            "rooms_complete": rooms_complete,
            "rooms_complete_pct": pct(rooms_complete),
            "entries_accepted": len(accepted),
            "entries_accepted_pct": pct(len(accepted)),
            "outcomes": outcomes,
            "note": "team_rooms_observed counts only rooms this run could actually read (retained>0); see "
            "provenance.caveats for why that is smaller than teams_allocated. outcomes partitions ALL "
            "candidate rooms (submitted/complete_not_submitted/stalled/never_started/unreadable are "
            "mutually exclusive and exhaustive; flooded is a separate overlay, see room_outcome()).",
        },
        "progress_histogram": histogram,
        "participants_series": {
            "rows": series_out,
            "final": final,
            "voters_per_writer_final": voters_per_writer,
        },
        "referee_restarts": {"count": len(status["restarts"]), "ts": status["restarts"]},
        "submissions": {
            "accepted": len(accepted),
            "rejected": subs["rejected_count"],
            "rejections_by_reason": subs["rejections_by_reason"],
            "distinct_submitting_dids": len(subs["distinct_submitting_dids"]),
            "distinct_game_ids_tried": len(subs["distinct_game_ids"]),
            "accepted_per_day": subs["accepted_per_day"],
            "accepted_in_last_24h_before_deadline": accepted_last_24h,
            "accepted_in_last_2h_before_deadline": accepted_last_2h,
            "export_completeness": subs["completeness"],
        },
        "rooms": rooms,
        "provenance": {
            "script_sha256": script_sha256(),
            "deadline_utc": iso_of(DEADLINE_EPOCH),
            "inputs": file_meta,
            "caveats": caveats,
        },
    }

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(funnel_obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    with open(a.out, "rb") as f:
        out_bytes = f.read()
    out_sha = hashlib.sha256(out_bytes).hexdigest()

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "invocation": " ".join(sys.argv),
        "snapshot_dir": snap,
        "fetch_window": {
            "min_ts": iso_of(min(all_epochs)) if all_epochs else None,
            "max_ts": iso_of(max(all_epochs)) if all_epochs else None,
        },
        "inputs": {k: {"sha256": v["sha256"], "bytes": v["bytes"]} for k, v in file_meta.items()},
        "output": {"path": a.out, "sha256": out_sha, "bytes": len(out_bytes)},
    }
    if a.manifest:
        os.makedirs(os.path.dirname(a.manifest) or ".", exist_ok=True)
        with open(a.manifest, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, indent=1, sort_keys=True, ensure_ascii=False)
            f.write("\n")

    print(f"teams_allocated={teams_allocated} team_rooms_observed={len(observed)} "
          f"rooms_with_any_word={rooms_with_any_word} rooms_complete={rooms_complete} "
          f"entries_accepted={len(accepted)} referee_restarts={len(status['restarts'])}")
    print(f"outcomes={outcomes}")
    print(f"rejected={subs['rejected_count']} rejections_by_reason={subs['rejections_by_reason']}")
    print(f"wrote {a.out} (sha256 {out_sha}) and {a.manifest or '(no manifest)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
