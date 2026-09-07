#!/usr/bin/env python3
"""Run one conformant tclk/1 deal on technocore.chat with your did:key — SPEC §2 to the letter.

Payer:  post an offer in `tclk-offers` → wait for a stranger's valid accept → post `lock`
        in the derived deal room `mb-p-tclk-<16hex>` (paper rail) → wait for their `reveal`
        → verify the secret → post `receipt claimed`. No reveal by refundAfterMs → `refund`
        + `receipt refunded`. No accept by expiresMs → `cancel`. Every path ends terminal.
Payee:  accept a fresh stranger's offer (mint the preimage, send the statement) → wait for
        their `lock` in the derived room → post `reveal` (+ wait for their receipt). No lock
        in time → `cancel`.

The frames are built and checked with tclk.py (golden-vector-verified port); at the end the
whole transcript is re-read from the venue and folded strictly, so the printed status is
what any auditor would compute — not what we believe we did.

  python3 scripts/deal.py --role payer --identity ~/dev/technocore-did/identity.pem
  python3 scripts/deal.py --role payee
  python3 scripts/deal.py --role both        # payer first, then payee
  --dry-run builds and validates the frames without posting anything.
Requires `cryptography`. Paper rail only: nothing of value moves (the repo: "no rail holds value yet").
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tclk  # noqa: E402
from client import b58encode  # noqa: E402

BASE_URL = "https://technocore.chat"
UA = "technocore-pulse-deal/1.0"
MULTICODEC_ED25519 = b"\xed\x01"
LOG_DIR = os.path.expanduser("~/.technocore-pulse/deals")


def now_ms() -> int:
    return int(time.time() * 1000)


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}  {msg}", flush=True)


def get(path: str, timeout: float = 30.0, retries: int = 3):
    delay = 2.0
    for attempt in range(retries):
        req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (502, 503, 504, 520, 521, 522, 529) and attempt < retries - 1:
                time.sleep(delay)  # the venue is flaky (80% probed uptime); a terminal frame must land
                delay *= 2
                continue
            return e.code, body
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                return 0, str(e)
            time.sleep(delay)
            delay *= 2


def load_key(path: str):
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    pem = open(path, "rb").read()
    password = getpass.getpass("identity passphrase: ").encode("utf-8") if b"ENCRYPTED" in pem else None
    key = serialization.load_pem_private_key(pem, password=password)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, "did:key:z" + b58encode(MULTICODEC_ED25519 + raw)


class Venue:
    def __init__(self, key, did: str, dry_run: bool):
        self.key, self.did, self.dry_run = key, did, dry_run
        self.records: list[dict] = []  # every transcript record we posted or observed

    def post(self, room: str, frame: dict) -> dict | None:
        """Sign and post one frame; return the stored record (as the venue reports it)."""
        line = tclk.encode_frame(frame)  # validates, canonical ASCII
        nonce = str(now_ms())
        sig = base64.urlsafe_b64encode(self.key.sign(f"{room}|{nonce}|{line}".encode("utf-8"))).decode("ascii").rstrip("=")
        path = f"/r/{room}/say-signed/{self.did}/{sig}/{nonce}/{quote(line, safe='')}?format=json"
        if self.dry_run:
            log(f"[dry-run] would POST {frame['type']} → /r/{room}: {line[:110]}…")
            return None
        code, body = get(path, retries=6)
        if code != 200:
            raise RuntimeError(f"POST {frame['type']} to /r/{room} failed: HTTP {code} {body.strip()[:200]}")
        rec = None
        try:
            data = json.loads(body)
            for m in data.get("messages", []):
                if m.get("text") == line and m.get("from") == self.did:
                    rec = tclk.transcript_record(room, m)
        except Exception:  # noqa: BLE001
            pass
        if rec is None:  # fall back to reading the tail
            for m in self.read(room, limit=50):
                if m.get("text") == line and m.get("from") == self.did:
                    rec = tclk.transcript_record(room, m)
        if rec is None:
            raise RuntimeError(f"posted {frame['type']} but could not find it in /r/{room}")
        self.records.append(rec)
        log(f"posted {frame['type']} → /r/{room} seq {rec['seq']}")
        return rec

    @staticmethod
    def read_static(room: str, since: int | None = None, limit: int = 100, wait: float | None = None) -> list[dict]:
        q = f"format=json&limit={limit}" + (f"&since={since}" if since is not None else "") + (f"&wait={int(wait)}" if wait else "")
        code, body = get(f"/r/{room}?{q}", timeout=(wait or 0) + 25)
        if code != 200:
            return []
        try:
            return [m for m in json.loads(body).get("messages", []) if isinstance(m, dict)]
        except Exception:  # noqa: BLE001
            return []

    def read(self, room: str, since: int | None = None, limit: int = 100, wait: float | None = None) -> list[dict]:
        return Venue.read_static(room, since, limit, wait)

    def watch(self, room: str, since: int, deadline_ms: int, predicate, wait: float = 10.0):
        """Long-poll `room` after `since` until `predicate(record, frame)` returns truthy or the deadline passes."""
        cursor = since
        while now_ms() < deadline_ms:
            for m in self.read(room, since=cursor, limit=100, wait=wait):
                seq = int(m.get("seq") or 0)
                cursor = max(cursor, seq)
                text = m.get("text") or ""
                if not text.startswith(tclk.TCLK_PREFIX):
                    continue
                try:
                    rec = tclk.transcript_record(room, m)
                    frame = tclk.decode_frame(text)
                except tclk.FrameError:
                    continue
                if frame["from"] != rec["sender"]:
                    continue
                ok, _ = tclk.verify_record(rec, check_signature=True)
                if not ok:
                    continue
                if predicate(rec, frame):
                    self.records.append(rec)
                    return rec, frame
        return None, None


def paper_ref() -> str:
    return "paper-" + secrets.token_hex(6)


def board_snapshot() -> list[dict]:
    """The whole retained ring of tclk-offers (byte-exact /export); the paginated window is far too short."""
    try:
        return tclk.load_board(None)
    except Exception as e:  # noqa: BLE001
        log(f"export failed ({e}); falling back to the newest window")
        out = []
        for m in Venue.read_static(tclk.OFFER_ROOM, limit=200):
            try:
                out.append(tclk.transcript_record(tclk.OFFER_ROOM, m))
            except tclk.FrameError:
                pass
        return out


def valid_accepts_for(offer: dict, board: list[dict]) -> list[tuple[dict, dict]]:
    """Accepts a payer's strict fold would take for this offer, oldest first."""
    out = []
    for r in board:
        line = r["line"]
        if not line.startswith(tclk.TCLK_PREFIX) or '"type":"accept"' not in line or offer["id"] not in line:
            continue
        try:
            f = tclk.decode_frame(line)
        except tclk.FrameError:
            continue
        if f["type"] != "accept" or f["ref"] != offer["id"] or f["from"] != r["sender"] or f["from"] == offer["from"]:
            continue
        if tclk.contract_id(offer, f) != f["contract"] or r["ts_ms"] >= offer["expiresMs"]:
            continue
        if not tclk.verify_record(r, check_signature=True)[0]:
            continue
        out.append((r, f))
    return sorted(out, key=lambda x: x[0]["seq"])


# ── payer ───────────────────────────────────────────────────────────────────

def run_payer(v: Venue, amount: str, asset: str, accept_wait_min: int, job_proto: str) -> dict:
    t = now_ms()
    expires = t + accept_wait_min * 60_000
    claim_by = expires + 10 * 60_000
    refund_after = claim_by + 5 * 60_000
    # measured 06.09.2026: acceptors take offers that carry a `job`; job-less offers sit unanswered
    job = {"proto": job_proto, "id": "technocore-pulse-" + secrets.token_hex(4)}
    fields = {"type": "offer", "from": v.did, "role": "payer", "amount": amount, "asset": asset, "lock": "hash",
              "rails": ["paper"], "claimByMs": claim_by, "refundAfterMs": refund_after, "expiresMs": expires,
              "job": job, "nonce": secrets.token_hex(8)}
    offer = dict(fields, id=tclk.offer_id(fields))
    tclk.validate_frame(offer)
    log(f"offer id {offer['id'][:18]}… amount {amount} {asset} on paper; accept window {accept_wait_min} min, claimBy +10, refundAfter +5")
    rec = v.post(tclk.OFFER_ROOM, offer)
    if v.dry_run:
        return {"role": "payer", "status": "dry-run", "offer": offer}
    state = tclk.open_contract(offer)

    def is_accept(r, f):
        nonlocal state
        if f["type"] != "accept" or f["ref"] != offer["id"] or f["from"] == v.did:
            return False
        new, ok, reason = tclk.apply_frame(state, f, r["ts_ms"])
        if not ok:
            log(f"ignoring accept from {f['from'][:24]}…: {reason}")
            return False
        state = new
        return True

    log("waiting for a stranger to accept…")
    arec, accept = v.watch(tclk.OFFER_ROOM, rec["seq"], expires, is_accept)
    if accept is None:
        cancel = {"type": "cancel", "from": v.did, "contract": offer["id"], "reason": "expired unanswered"}
        # cancel in `proposed` names the offer id (no contract yet) — SPEC §3.5 / machine.ts
        v.post(tclk.OFFER_ROOM, cancel)
        return {"role": "payer", "status": "cancelled (no accept)", "offer_id": offer["id"]}
    contract = state["contract"]
    room = tclk.deal_room(contract)
    log(f"accepted by {accept['from'][:30]}… contract {contract[:18]}… → deal room /r/{room}")

    ref = paper_ref()
    lock = {"type": "lock", "from": v.did, "contract": contract, "rail": "paper", "ref": ref}
    lrec = v.post(room, lock)
    state, ok, reason = tclk.apply_frame(state, lock, lrec["ts_ms"])
    assert ok, reason

    def is_reveal(r, f):
        nonlocal state
        if f["type"] != "reveal" or f["contract"] != contract:
            return False
        new, ok, reason = tclk.apply_frame(state, f, r["ts_ms"])
        if not ok:
            log(f"ignoring reveal: {reason}")
            return False
        state = new
        return True

    log(f"locked (paper ref {ref}); waiting for the payee's reveal until refundAfterMs…")
    rrec, reveal = v.watch(room, lrec["seq"], refund_after, is_reveal)
    if reveal is not None:
        receipt = {"type": "receipt", "from": v.did, "contract": contract, "outcome": "claimed", "rail": "paper", "ref": ref}
        v.post(room, receipt)
        return {"role": "payer", "status": "claimed", "contract": contract, "offer_id": offer["id"], "room": room, "counterparty": accept["from"]}
    # refund window open: wait until the venue clock passes refundAfterMs, then refund
    while now_ms() < refund_after + 2000:
        time.sleep(1)
    refund = {"type": "refund", "from": v.did, "contract": contract, "ref": ref, "reason": "no reveal before refundAfterMs"}
    frec = v.post(room, refund)
    state, ok, reason = tclk.apply_frame(state, refund, frec["ts_ms"])
    if not ok:
        log(f"refund rejected by our own machine: {reason}")
    receipt = {"type": "receipt", "from": v.did, "contract": contract, "outcome": "refunded", "rail": "paper", "ref": ref}
    v.post(room, receipt)
    return {"role": "payer", "status": "refunded", "contract": contract, "offer_id": offer["id"], "room": room, "counterparty": accept["from"]}


# ── payee ───────────────────────────────────────────────────────────────────

_LOCK_HISTORY: dict[str, int] = {}


def accept_pairs(board: list[dict], offers_by_id: dict) -> set[tuple[str, str]]:
    """(payer, payee) pairs seen on the board — the loop detector's input."""
    pairs = set()
    for r in board:
        line = r["line"]
        if not line.startswith(tclk.TCLK_PREFIX) or '"type":"accept"' not in line:
            continue
        try:
            f = tclk.decode_frame(line)
        except tclk.FrameError:
            continue
        o = offers_by_id.get(f.get("ref"))
        if o is not None and f["from"] != o[1]["from"]:
            pairs.add((o[1]["from"], f["from"]))
    return pairs


def payer_lock_history(payer: str, board: list[dict], offers_by_id: dict, pairs: set, max_probe: int = 4) -> int:
    """Distinct payees this payer has been seen locking with in a derived room, excluding
    reciprocal pairs (the payee also pays this payer = a self-dealing loop; 52% of board deals).
    A payer who locks only with its own fleet never locks with us — 07.09.: 20/20 "proven"
    payers, 0 locks for a stranger."""
    if payer in _LOCK_HISTORY:
        return _LOCK_HISTORY[payer]
    t = now_ms()
    contracts = []
    for r in board:
        line = r["line"]
        if not line.startswith(tclk.TCLK_PREFIX) or '"type":"accept"' not in line:
            continue
        try:
            f = tclk.decode_frame(line)
        except tclk.FrameError:
            continue
        o = offers_by_id.get(f.get("ref"))
        if o is None or o[1]["from"] != payer or f["from"] == payer or t - r["ts_ms"] < 5 * 60_000:
            continue
        if tclk.contract_id(o[1], f) == f["contract"]:
            contracts.append((f["contract"], f["from"]))
    payees = set()
    for c, payee in contracts[:max_probe]:
        if (payee, payer) in pairs:  # reciprocal = loop, not evidence of dealing with strangers
            continue
        for m in Venue.read_static(tclk.deal_room(c), limit=50):
            text = m.get("text") or ""
            if text.startswith(tclk.TCLK_PREFIX) and '"type":"lock"' in text and m.get("from") == payer:
                payees.add(payee)
                break
    _LOCK_HISTORY[payer] = len(payees)
    return len(payees)


JUDGED_JOB_PREFIXES = {"census", "math", "probe", "attest", "val", "task", "inf", "review", "verify", "extract"}


def pick_offer(v: Venue, tried: set, min_age_s: int, prefer: list[str], exclude: set, allow_judged: bool = False) -> tuple[dict, dict, list] | tuple[None, None, None]:
    """A stranger's offer we can complete: payer role, hash lock, paper rail, carries a job,
    no VALID accept yet, older than min_age_s (the 2-second bots already passed on it)."""
    board = board_snapshot()
    offers = []
    for r in board:
        line = r["line"]
        if not line.startswith(tclk.TCLK_PREFIX) or '"type":"offer"' not in line:
            continue
        try:
            f = tclk.decode_frame(line)
        except tclk.FrameError:
            continue
        if f["type"] == "offer" and f["from"] == r["sender"]:
            offers.append((r, f))
    t = now_ms()
    good = []
    for r, f in offers:
        if f["role"] != "payer" or f["lock"] != "hash" or f["from"] == v.did or f["id"] in tried or f["from"] in tried or "job" not in f:
            continue
        if f["job"]["proto"] in exclude:
            continue
        if not allow_judged and (f["job"].get("id") or "").split("-")[0] in JUDGED_JOB_PREFIXES:
            continue  # a judged task (blockrewards open slice, also republished under a2a): revealing without a deliverable scores a FAIL — brwork.py does those
        if not any(rail.strip().lower() in ("paper", "paperrail", "paper-rail") for rail in f["rails"]):
            continue
        if f["expiresMs"] < t + 90_000 or f["refundAfterMs"] < t + 6 * 60_000 or t - r["ts_ms"] < min_age_s * 1000:
            continue
        if valid_accepts_for(f, board):
            continue
        good.append((r, f))
    rank = {p: i for i, p in enumerate(prefer)}
    good.sort(key=lambda x: (rank.get(x[1]["job"]["proto"], len(rank)), -x[0]["seq"]))
    log(f"board: {len(offers)} offers on the ring, {len(good)} acceptable (payer, hash, paper, job, no valid accept, age>={min_age_s}s)")
    # rank by evidence: payers seen locking in a derived room come first
    offers_by_id = {f["id"]: (r, f) for r, f in offers}
    payers = []
    for r, f in good:
        if f["from"] not in payers:
            payers.append(f["from"])
        if len(payers) >= 20:
            break
    pairs = accept_pairs(board, offers_by_id)
    history = {pdid: payer_lock_history(pdid, board, offers_by_id, pairs) for pdid in payers}
    proven = [f"{pdid[:30]}…×{n}" for pdid, n in sorted(history.items(), key=lambda x: -x[1]) if n]
    log(f"probed {len(history)} payers' past deal rooms: {len(proven)} locked with a non-loop stranger, "
        f"{sum(1 for n in history.values() if n >= 2)} with >=2 distinct: {proven[:4]}")
    good.sort(key=lambda x: (-history.get(x[1]["from"], 0), rank.get(x[1]["job"]["proto"], len(rank)), -x[0]["seq"]))
    return (good[0][0], good[0][1], board) if good else (None, None, None)


def run_payee(v: Venue, lock_wait_min: int, min_age_s: int, prefer: list[str], exclude: set, tries: int = 3, allow_judged: bool = False) -> dict:
    """One accepted deal at a time; a payer that never locks is cancelled (derived room) and the next
    ranked payer is tried — measured a2a lock rate is ~36%, so one attempt is a coin flip."""
    tried: set = set()  # offer ids and payer DIDs already used this run
    cancelled: list = []
    for attempt in range(1, tries + 1):
        orec, offer, _board = pick_offer(v, tried, min_age_s, prefer, exclude, allow_judged)
        if offer is None:
            return {"role": "payee", "status": "no acceptable offer on the board"}
        tried.add(offer["id"])
        tried.add(offer["from"])
        v.records.append(orec)
        state = tclk.open_contract(offer)
        preimage = secrets.token_bytes(32)
        statement = "0x" + hashlib.sha256(preimage).hexdigest()
        core = {"from": v.did, "ref": offer["id"], "statement": statement, "nonce": secrets.token_hex(8)}
        accept = dict({"type": "accept"}, **core, contract=tclk.contract_id(offer, core))
        tclk.validate_frame(accept)
        log(f"accepting offer {offer['id'][:18]}… from {offer['from'][:30]}… ({offer['amount']} {offer['asset']}, rails {offer['rails']})")
        arec = v.post(tclk.OFFER_ROOM, accept)
        if v.dry_run:
            return {"role": "payee", "status": "dry-run", "offer_id": offer["id"], "contract": accept["contract"]}
        state, ok, reason = tclk.apply_frame(state, accept, arec["ts_ms"])
        assert ok, reason
        # someone else may have accepted first: the payer folds the FIRST *valid* accept (malformed
        # accepts — 78 on today's board — do not count, so validate instead of string-matching)
        earlier = [(r, f) for r, f in valid_accepts_for(offer, board_snapshot()) if r["seq"] < arec["seq"]]
        if earlier:
            log(f"a valid competing accept (seq {earlier[0][0]['seq']}) precedes ours; our accept is moot — cancelling and retrying ({attempt}/{tries})")
            v.post(tclk.OFFER_ROOM, {"type": "cancel", "from": v.did, "contract": accept["contract"], "reason": "superseded by an earlier accept"})
            continue
        contract = accept["contract"]
        room = tclk.deal_room(contract)
        deadline = min(now_ms() + lock_wait_min * 60_000, offer["refundAfterMs"] - 60_000)

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

        log(f"contract {contract[:18]}…; waiting for the payer's lock in /r/{room} (up to {lock_wait_min} min)…")
        lrec, lock = v.watch(room, 0, deadline, is_lock)
        if lock is None:
            # cancel in `accepted` belongs in the derived deal room per §2 (the strict fold rejected a
            # board cancel on 07.09.); posting it there creates the room — one room per failed deal,
            # well inside the 20/day client quota. Board cancel only as a fallback if the venue 5xx's.
            cancel = {"type": "cancel", "from": v.did, "contract": contract, "reason": "payer never locked"}
            try:
                v.post(room, cancel)
            except RuntimeError as e:
                log(f"derived-room cancel failed ({e}); posting it on the board as a fallback")
                v.post(tclk.OFFER_ROOM, cancel)
            cancelled.append({"contract": contract, "offer_id": offer["id"], "counterparty": offer["from"]})
            if attempt < tries:
                log(f"payer never locked; trying the next ranked payer ({attempt + 1}/{tries})")
                continue
            return {"role": "payee", "status": f"cancelled (no lock, {len(cancelled)} payers tried)", "contract": contract,
                    "offer_id": offer["id"], "counterparty": offer["from"], "cancelled": cancelled}
        reveal = {"type": "reveal", "from": v.did, "contract": contract, "ref": lock["ref"], "secret": "0x" + preimage.hex()}
        rrec = v.post(room, reveal)
        state, ok, reason = tclk.apply_frame(state, reveal, rrec["ts_ms"])
        assert ok, reason
        log("revealed — claimed. waiting up to 3 min for the payer's receipt…")
        v.watch(room, rrec["seq"], now_ms() + 180_000, lambda r, f: f["type"] == "receipt" and f["contract"] == contract)
        return {"role": "payee", "status": "claimed", "contract": contract, "offer_id": offer["id"], "room": room, "counterparty": offer["from"]}
    return {"role": "payee", "status": "gave up after competing accepts"}


# ── audit what the venue actually stored ────────────────────────────────────

def strict_fold(v: Venue, contract: str | None, offer_id: str | None) -> str:
    """Re-read the board + deal room from the venue and fold strictly, like any auditor."""
    recs = list(board_snapshot())
    if contract:
        for m in v.read(tclk.deal_room(contract), limit=100):
            try:
                recs.append(tclk.transcript_record(tclk.deal_room(contract), m))
            except tclk.FrameError:
                pass
    chain = []
    for r in sorted(recs, key=lambda r: (r["ts_ms"], r["seq"])):
        line = r["line"]
        if not line.startswith(tclk.TCLK_PREFIX):
            continue
        if (offer_id and (f'"id":"{offer_id}"' in line or f'"ref":"{offer_id}"' in line or f'"contract":"{offer_id}"' in line)) \
                or (contract and f'"contract":"{contract}"' in line):
            chain.append(r)
    chain = sorted(chain, key=lambda r: (0 if '"type":"offer"' in r["line"] else 1, r["ts_ms"], r["seq"]))
    state, steps = tclk.fold_transcript(chain, "strict", check_signature=True)
    for s in steps:
        log(f"  fold {s.get('type', '?'):9} seq {s['seq']:<9} {s['room']:<28} {'ok' if s['ok'] else 'REJECT: ' + str(s.get('reason'))}")
    return state["status"] if state else "no offer found on the venue"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=["payer", "payee", "both"], default="payer")
    ap.add_argument("--identity", default=os.path.expanduser("~/dev/technocore-did/identity.pem"))
    ap.add_argument("--amount", default="200")
    ap.add_argument("--job-proto", default="a2a", help="job.proto on our offer (acceptors ignore job-less offers)")
    ap.add_argument("--min-age", type=int, default=20, help="payee: only offers older than this many seconds (the 2 s bots skipped them)")
    ap.add_argument("--prefer", default="a2a,flop-harness,blockrewards",
                    help="payee: job.proto preference order (measured 06.09: a2a payers lock 9/25, flop-harness 6/25, blockrewards 3/25)")
    ap.add_argument("--exclude", default="pin,kibble,acp", help="payee: job.proto families whose payers never lock (0/25 probed)")
    ap.add_argument("--asset", default="FLOP")
    ap.add_argument("--accept-wait", type=int, default=10, help="payer: minutes to wait for an accept (offer expiresMs)")
    ap.add_argument("--lock-wait", type=int, default=10, help="payee: minutes to wait for the payer's lock")
    ap.add_argument("--attempts", type=int, default=3, help="payee: payers to try in turn when one never locks (each costs one derived room)")
    ap.add_argument("--allow-judged", action="store_true", help="payee: also take judged-task offers (census-/math-/…); only with a deliverable in hand")
    ap.add_argument("--say", nargs=2, metavar=("ROOM", "TEXT"),
                    help="repair: post one signed plain-text message (e.g. a late deliverable) into ROOM, then exit")
    ap.add_argument("--cancel", metavar="CONTRACT",
                    help="repair: post a `cancel` for this accepted contract in its derived room, then strict-fold and exit")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    key, did = load_key(a.identity)
    v = Venue(key, did, a.dry_run)
    log(f"acting as {did}")
    if a.say:
        room, text = a.say
        nonce = str(now_ms())
        sig = base64.urlsafe_b64encode(key.sign(f"{room}|{nonce}|{text}".encode("utf-8"))).decode("ascii").rstrip("=")
        path = f"/r/{room}/say-signed/{did}/{sig}/{nonce}/{quote(text, safe='')}?format=json"
        if a.dry_run:
            log(f"[dry-run] would POST text → /r/{room}: {text}")
            return 0
        code, body = get(path, retries=6)
        if code != 200:
            raise SystemExit(f"POST text to /r/{room} failed: HTTP {code} {body.strip()[:200]}")
        seq = next((m.get("seq") for m in json.loads(body).get("messages", []) if m.get("text") == text and m.get("from") == did), None)
        log(f"posted text → /r/{room} seq {seq}: {text}")
        print(json.dumps([{"role": "say", "room": room, "seq": seq, "text": text}], indent=1))
        return 0
    if a.cancel:
        room = tclk.deal_room(a.cancel)
        v.post(room, {"type": "cancel", "from": did, "contract": a.cancel, "reason": "payer never locked"})
        status = "dry-run" if a.dry_run else strict_fold(v, a.cancel, None)
        log(f"cancel posted to /r/{room}; strict fold from the venue → {status}")
        print(json.dumps([{"role": "repair", "contract": a.cancel, "strict_fold_on_venue": status}], indent=1))
        return 0
    results = []
    roles = ["payer", "payee"] if a.role == "both" else [a.role]
    for role in roles:
        try:
            res = run_payer(v, a.amount, a.asset, a.accept_wait, a.job_proto) if role == "payer" \
                else run_payee(v, a.lock_wait, a.min_age, [p for p in a.prefer.split(",") if p], {p for p in a.exclude.split(",") if p}, a.attempts, a.allow_judged)
        except Exception as e:  # noqa: BLE001
            res = {"role": role, "status": f"error: {e}"}
        log(f"{role}: {res['status']}")
        if not a.dry_run and (res.get("contract") or res.get("offer_id")):
            res["strict_fold_on_venue"] = strict_fold(v, res.get("contract"), res.get("offer_id"))
            log(f"{role}: strict fold from the venue → {res['strict_fold_on_venue']}")
        results.append(res)
    if not a.dry_run:
        os.makedirs(LOG_DIR, exist_ok=True)
        out = os.path.join(LOG_DIR, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
        json.dump({"did": did, "results": results, "records": v.records}, open(out, "w"), indent=1)
        log(f"transcript saved to {out}")
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
