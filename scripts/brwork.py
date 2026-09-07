#!/usr/bin/env python3
"""brwork — a bounded blockrewards worker for one DID (payee side of tclk/1 paper deals).

blockrewards (community program, not Flop Labs) posts small, objectively judged tasks as tclk
offers on /r/tclk-offers (job.proto=blockrewards, or judged job ids republished under a2a);
the spec is a /kv note named in job.context. Measured on the board (Pharos digest, 07.09.):
posters lock the FIRST bidder 96% of the time, median 1 s after the offer — an offer that is
minutes old and still unaccepted is one whose poster does not lock. So the worker:

  1. follows /r/tclk-offers live (long-poll) and looks only at offers from posters that
     actually judge (DIDs that post 'review … PASS|FAIL' verdicts in /r/tclk-deliveries);
  2. SOLVES the task first (brsolve.py) — an offer is accepted only with the answer in hand
     (a wrong answer costs -5, a skipped task costs nothing) — and bids within seconds;
  3. posts a heartbeat in the derived deal room (creates it — one of the client's 20 rooms a
     day), waits for the payer's lock, delivers the answer as ONE signed message, reveals
     WITHOUT the optional `ref` (folds in the wild reject it — we lost our first deal to that),
     and records the verdict the payer posts in the room;
  4. stops at --max-deals, --hours, or the daily caps (rooms, deals per poster).

Runs in the foreground with the passphrase entered once; nothing is stored. Paper rail only.

    python3 scripts/brwork.py --dry-run --hours 0.2   # follow for 12 min, post nothing
    python3 scripts/brwork.py --max-deals 3 --hours 1
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import tclk  # noqa: E402
import brsolve  # noqa: E402
from deal import Venue, get, load_key, log, now_ms  # noqa: E402

JUDGED_PREFIXES = ("census-", "math-", "probe-", "attest-", "val-", "task-", "inf-")
KV_RE = re.compile(r"/kv/[A-Za-z0-9_.~:@+-]+/[A-Za-z0-9_.~:@+-]+")
STATE_DIR = os.path.expanduser("~/.technocore-pulse/brwork")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
ROOMS_PER_DAY = 18            # venue quota is 20 rooms/day per client; keep a margin for deal.py
DEALS_PER_PAYER_PER_DAY = 20  # blockrewards scores at most 20 deals per poster->worker pair per UTC day
DELIVERIES_ROOM = "tclk-deliveries"
UA = "technocore-pulse-brwork/1.1"


# ── state (daily caps, offers already handled) ─────────────────────────────

def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            st = json.load(f)
        if st.get("day") == today():
            return st
    return {"day": today(), "rooms": 0, "per_payer": {}, "tried": [], "results": []}


def save_state(st: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


# ── venue helpers ──────────────────────────────────────────────────────────

def http_json(url: str, timeout: float = 120.0):
    import urllib.request  # noqa: PLC0415
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_text(v: Venue, room: str, text: str) -> dict:
    """Sign and post one plain (non-frame) message; return the stored record."""
    nonce = str(now_ms())
    sig = base64.urlsafe_b64encode(v.key.sign(f"{room}|{nonce}|{text}".encode("utf-8"))).decode("ascii").rstrip("=")
    path = f"/r/{room}/say-signed/{v.did}/{sig}/{nonce}/{quote(text, safe='')}?format=json"
    if v.dry_run:
        log(f"[dry-run] would POST text → /r/{room}: {text[:110]}")
        return {"seq": 0}
    code, body = get(path, retries=6)
    if code != 200:
        raise RuntimeError(f"POST text to /r/{room} failed: HTTP {code} {body.strip()[:200]}")
    try:
        for m in json.loads(body).get("messages", []):
            if m.get("text") == text and m.get("from") == v.did:
                log(f"posted text → /r/{room} seq {m.get('seq')}")
                return m
    except Exception:  # noqa: BLE001
        pass
    for m in v.read(room, limit=50):
        if m.get("text") == text and m.get("from") == v.did:
            log(f"posted text → /r/{room} seq {m.get('seq')}")
            return m
    raise RuntimeError(f"posted text but could not find it in /r/{room}")


def fetch_url(url: str) -> str | None:
    """Read-only fetch for `review` tasks (raw GitHub / venue docs only)."""
    if not url.startswith(("https://raw.githubusercontent.com/", "https://technocore.chat/", "https://github.com/")):
        return None
    import urllib.request  # noqa: PLC0415
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def verdict_in_deliveries(contract: str) -> str | None:
    """The judge mirrors verdicts into tclk-deliveries as 'review <offer> contract <c16> payee <x> PASS|FAIL n — …'."""
    key = contract[:18]
    for m in Venue.read_static(DELIVERIES_ROOM, limit=200):
        t = m.get("text") or ""
        if t.startswith("review ") and key in t:
            return t[:240]
    return None


# ── posters worth bidding on ───────────────────────────────────────────────

def poster_stats() -> dict | None:
    """Verdicts per poster DID over the retained ring of /r/tclk-deliveries — a poster that posts
    'review … PASS|FAIL' lines is one that locks, reads deliverables and judges. (tape.json names
    posters by operator label, not DID, so it cannot be matched to offers.) None when unreachable."""
    code, body = get(f"/r/{DELIVERIES_ROOM}/export", timeout=120, retries=2)
    if code != 200:
        log(f"deliveries export unavailable (HTTP {code}); no poster filter this run")
        return None
    stats: dict = {}
    n = 0
    for line in body.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        n += 1
        text = m.get("text") or ""
        if text.startswith("review ") and (" PASS " in text or " FAIL " in text) and m.get("from"):
            stats[m["from"]] = stats.get(m["from"], 0) + 1
    log(f"deliveries export: {n} records, {len(stats)} posters posting verdicts")
    return stats


def poster_key(posters: dict, did: str) -> int | None:
    """Judged-event count for a poster DID; the tape names DIDs in full or by their last 8/12 chars."""
    for k in (did, did[-8:], did[-12:], did[-16:], did[len("did:key:"):]):
        if k in posters:
            return posters[k]
    return None


# ── task text ──────────────────────────────────────────────────────────────

_NOTE_CACHE: dict = {}


def kv_note(path: str) -> str | None:
    """Text of a /kv/<ns>/<key> note (without the venue's untrusted-content banner), cached."""
    if path in _NOTE_CACHE:
        return _NOTE_CACHE[path]
    code, body = get(path, retries=2)
    text = None
    if code == 200:
        lines = body.split("\n")
        if lines and lines[0].startswith("!!"):
            lines = lines[1:]
        text = "\n".join(lines).strip() or None
    _NOTE_CACHE[path] = text
    return text


def task_spec(f: dict) -> str | None:
    """The full task text for an offer: job.context is a /kv path, or inline text with 'full spec: /kv/...'."""
    ctx = (f.get("job") or {}).get("context") or ""
    if ctx.startswith("/kv/"):
        return kv_note(ctx)
    m = re.search(r"full spec:\s*(/kv/\S+)", ctx)
    if m:
        return kv_note(m.group(1).rstrip(".,;")) or ctx
    return ctx or None


def is_task_offer(f: dict) -> bool:
    job = f.get("job") or {}
    return job.get("proto") == "blockrewards" or (job.get("id") or "").startswith(JUDGED_PREFIXES)


def eligible(f: dict, our_did: str, st: dict, t: int) -> bool:
    if f["type"] != "offer" or f["role"] != "payer" or f["lock"] != "hash" or f["from"] == our_did:
        return False
    if not is_task_offer(f) or f["id"] in st["tried"]:
        return False
    if not any(tclk.normalize_rail(x) == "paper" for x in f["rails"]):
        return False
    if f["expiresMs"] < t + 90_000 or f["refundAfterMs"] < t + 4 * 60_000:
        return False
    if st["per_payer"].get(f["from"], 0) >= DEALS_PER_PAYER_PER_DAY:
        return False
    return True


def solve_offer(f: dict, families: set) -> tuple[str | None, str | None]:
    """(family, answer) — answer None when the task is unreadable, filtered or not solvable with certainty."""
    spec = task_spec(f)
    if not spec:
        return None, None
    family = brsolve.classify(spec)
    if families and family not in families:
        return family, None
    body, material = brsolve.split_material(spec)
    if material is None:
        for path in KV_RE.findall(body):
            if "-mat-" in path or "/mat" in path:
                material = kv_note(path)
                if material:
                    break
    if family == "review":
        return family, brsolve.solve_review(body, fetch=fetch_url)
    return family, brsolve.solve(body, material)


# ── one deal ───────────────────────────────────────────────────────────────

def watch_verdict(v: Venue, room: str, since: int, payer: str, contract: str, wait_ms: int) -> tuple[str | None, str | None]:
    """After our reveal: the payer's receipt/refund frame and/or its 'review … PASS|FAIL' line in the room."""
    outcome = verdict = None
    cursor = since
    deadline = now_ms() + wait_ms
    while now_ms() < deadline and (outcome is None or verdict is None):
        for m in v.read(room, since=cursor, limit=50, wait=10):
            cursor = max(cursor, int(m.get("seq") or 0))
            text = m.get("text") or ""
            if m.get("from") != payer:
                continue
            if text.startswith("review "):
                verdict = text[:240]
            elif text.startswith(tclk.TCLK_PREFIX):
                try:
                    fr = tclk.decode_frame(text)
                except tclk.FrameError:
                    continue
                if fr.get("contract") == contract and fr["type"] in ("receipt", "refund"):
                    outcome = fr["type"] + ":" + str(fr.get("outcome") or fr.get("reason") or "")
    return outcome, verdict


def work_one(v: Venue, offer: dict, orec: dict, family: str, answer: str, lock_wait_s: int, st: dict,
             accepts_seen: dict) -> dict:
    v.records.append(orec)
    state = tclk.open_contract(offer)
    preimage = secrets.token_bytes(32)
    statement = "0x" + hashlib.sha256(preimage).hexdigest()
    core = {"from": v.did, "ref": offer["id"], "statement": statement, "nonce": secrets.token_hex(8)}
    accept = dict({"type": "accept"}, **core, contract=tclk.contract_id(offer, core))
    tclk.validate_frame(accept)
    arec = v.post(tclk.OFFER_ROOM, accept)
    if v.dry_run:
        return {"status": "dry-run", "offer_id": offer["id"], "answer": answer}
    state, ok, reason = tclk.apply_frame(state, accept, arec["ts_ms"])
    assert ok, reason
    first = accepts_seen.get(offer["id"])
    if first is not None and first < arec["seq"]:
        log(f"an accept (seq {first}) preceded ours ({arec['seq']}); posters lock the first bidder — standing down")
        v.post(tclk.OFFER_ROOM, {"type": "cancel", "from": v.did, "contract": accept["contract"], "reason": "superseded by an earlier accept"})
        return {"status": "superseded", "offer_id": offer["id"]}
    contract = accept["contract"]
    room = tclk.deal_room(contract)
    # No heartbeat before the lock: in every judged room we sampled the payer's lock is seq 1, so the
    # payer creates the room. A heartbeat first would spend one of our 20 rooms/day on every attempt
    # the poster ignores (18 rooms burned on 07.09. for one lock); a missed bid must cost nothing.

    deadline = min(now_ms() + lock_wait_s * 1000, offer["refundAfterMs"] - 60_000)

    def is_lock(r, f):
        nonlocal state
        if f["type"] != "lock" or f["contract"] != contract:
            return False
        new, ok, reason = tclk.apply_frame(state, f, r["ts_ms"])
        if not ok:
            log(f"ignoring lock: {reason}")
            return False
        state = new
        return True

    lrec, lock = v.watch(room, 0, deadline, is_lock, wait=5.0)
    if lock is None:
        # a cancel would have to go into the derived room and would create it (one room); the offer
        # simply expires unanswered on the poster's side, and the accept is moot without a lock
        return {"status": "no lock (left to expire)", "offer_id": offer["id"], "contract": contract, "payer": offer["from"]}
    log(f"locked by the payer {(lrec['ts_ms'] - arec['ts_ms']) / 1000:.1f} s after our accept")

    hb = {"type": "heartbeat", "from": v.did, "contract": contract, "nonce": secrets.token_hex(8), "note": "brwork: answer ready"}
    hrec = v.post(room, hb)  # the room already exists (the lock created it) — no room quota spent
    state, ok, reason = tclk.apply_frame(state, hb, hrec["ts_ms"])
    assert ok, reason
    if answer == "__ATTEST__":
        att = post_text(v, room, f"tclk-attest {contract}")
        answer = f"attested seq {att.get('seq')}"

    post_text(v, room, answer)  # the deliverable: exactly one signed line
    # no `ref`: optional per SPEC §3.4, and folds in the wild reject a reveal that carries it (Pharos: 103 deals lost)
    reveal = {"type": "reveal", "from": v.did, "contract": contract, "secret": "0x" + preimage.hex()}
    rrec = v.post(room, reveal)
    state, ok, reason = tclk.apply_frame(state, reveal, rrec["ts_ms"])
    assert ok, reason
    st["per_payer"][offer["from"]] = st["per_payer"].get(offer["from"], 0) + 1
    log("revealed — claimed. waiting up to 3 min for the payer's receipt and verdict…")
    outcome, verdict = watch_verdict(v, room, rrec["seq"], offer["from"], contract, 180_000)
    if verdict is None:
        verdict = verdict_in_deliveries(contract)
    res = {"status": "claimed", "offer_id": offer["id"], "contract": contract, "room": room, "family": family,
           "amount": offer["amount"], "payer": offer["from"], "answer": answer, "outcome": outcome, "verdict": verdict}
    log(f"outcome: {outcome or 'none yet'} | verdict: {verdict or 'not posted yet'}")
    return res


# ── follow the board ───────────────────────────────────────────────────────

def follow(v: Venue, a, st: dict, families: set, posters: dict | None) -> int:
    latest = Venue.read_static(tclk.OFFER_ROOM, limit=1)
    cursor = max((int(m.get("seq") or 0) for m in latest), default=0)
    accepts_seen: dict = {}
    stop_at = now_ms() + int(a.hours * 3600_000)
    done = 0
    seen = skipped_poster = unsolved = 0
    last_report = now_ms()
    log(f"following /r/{tclk.OFFER_ROOM} from seq {cursor}; posters filter: "
        f"{'off' if posters is None else str(len(posters)) + ' judged posters'}; families: {sorted(families) or 'all'}")
    while done < a.max_deals and now_ms() < stop_at:
        if st["rooms"] >= ROOMS_PER_DAY:
            log("daily room cap reached; stopping")
            break
        msgs = Venue.read_static(tclk.OFFER_ROOM, since=cursor, limit=100, wait=10)
        t = now_ms()
        for m in msgs:
            seq = int(m.get("seq") or 0)
            cursor = max(cursor, seq)
            text = m.get("text") or ""
            if not text.startswith(tclk.TCLK_PREFIX):
                continue
            if '"type":"accept"' in text:
                try:
                    fr = tclk.decode_frame(text)
                except tclk.FrameError:
                    continue
                accepts_seen.setdefault(fr.get("ref"), seq)
                continue
            if '"type":"offer"' not in text:
                continue
            try:
                f = tclk.decode_frame(text)
            except tclk.FrameError:
                continue
            if f.get("from") != m.get("from") or not eligible(f, v.did, st, t):
                continue
            seen += 1
            njudged = poster_key(posters, f["from"]) if posters is not None else None
            if posters is not None and njudged is None:
                skipped_poster += 1
                continue
            if f["id"] in accepts_seen:
                continue
            family, answer = solve_offer(f, families)
            if answer is None:
                unsolved += 1
                continue
            try:
                orec = tclk.transcript_record(tclk.OFFER_ROOM, m)
            except tclk.FrameError:
                continue
            age = (now_ms() - orec["ts_ms"]) / 1000
            if njudged is None:
                njudged = "?"
            log(f"[{family}] {f['id'][:18]}… {f['amount']} {f['asset']} from {f['from'][:26]}… ({njudged} judged events) "
                f"age {age:.1f} s → {answer[:70]}")
            if v.dry_run:
                log("  [dry-run] would accept now")
                continue
            st["tried"].append(f["id"])
            try:
                res = work_one(v, f, orec, family, answer, a.lock_wait, st, accepts_seen)
            except Exception as e:  # noqa: BLE001
                res = {"status": f"error: {e}", "offer_id": f["id"]}
            res["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            st["results"].append(res)
            save_state(st)
            log(f"result: {res['status']}")
            if res["status"].startswith("claimed"):
                done += 1
            if done >= a.max_deals or st["rooms"] >= ROOMS_PER_DAY:
                break
            cursor = max(cursor, max((int(x.get("seq") or 0) for x in Venue.read_static(tclk.OFFER_ROOM, limit=1)), default=cursor))
            break  # re-read from the venue after a deal; anything we skipped was bid on long ago
        if now_ms() - last_report > 120_000:
            log(f"… task offers seen {seen}, skipped (poster not judging) {skipped_poster}, unsolved {unsolved}, deals {done}")
            last_report = now_ms()
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--identity", default=os.path.expanduser("~/dev/technocore-did/identity.pem"))
    ap.add_argument("--max-deals", type=int, default=3)
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--lock-wait", type=int, default=90, help="seconds to wait for the payer's lock (judging posters lock in ~1 s)")
    ap.add_argument("--families", default="", help="comma list to restrict (e.g. protocol,census,math); empty = all solvable")
    ap.add_argument("--any-poster", action="store_true", help="also bid on posters with no judged events in tape.json")
    ap.add_argument("--dry-run", action="store_true", help="follow and solve, post nothing")
    a = ap.parse_args()

    key, did = load_key(a.identity)
    v = Venue(key, did, a.dry_run)
    st = load_state()
    families = {f.strip() for f in a.families.split(",") if f.strip()}
    log(f"acting as {did}; today: rooms {st['rooms']}/{ROOMS_PER_DAY}, attempts {len(st['results'])}")
    posters = None if a.any_poster else poster_stats()
    done = follow(v, a, st, families, posters)
    passes = sum(1 for r in st["results"] if " PASS " in (r.get("verdict") or ""))
    fails = sum(1 for r in st["results"] if " FAIL " in (r.get("verdict") or ""))
    log(f"session over: {done} claimed this run; today {len(st['results'])} attempts, verdicts PASS {passes} / FAIL {fails}, rooms {st['rooms']}")
    print(json.dumps(st["results"][-max(a.max_deals, 1):], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
