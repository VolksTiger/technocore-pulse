#!/usr/bin/env python3
"""brwork — a bounded blockrewards worker for one DID (payee side of tclk/1 paper deals).

blockrewards (community program, not Flop Labs) posts small, objectively judged tasks as tclk
offers on /r/tclk-offers with job.proto=blockrewards; the spec is inline in a /kv note and
mirrored at https://flop-market.pages.dev/open.json. The worker:

  1. reads open.json, decodes each offer frame, and SOLVES the task first (brsolve.py) —
     an offer is accepted only when the answer is already in hand (a wrong answer costs -5,
     a skipped task costs nothing);
  2. accepts on the board, posts a heartbeat in the derived deal room (creates it — one of the
     client's 20 rooms/day), waits for the payer's lock, delivers the answer as ONE signed
     message in the deal room, reveals, waits for the receipt/verdict;
  3. stops at --max-deals, --hours, or the daily caps (rooms, deals per payer).

Runs in the foreground with the passphrase entered once; nothing is stored. Paper rail only.

    python3 scripts/brwork.py --dry-run            # plan only: which offers we could answer
    python3 scripts/brwork.py --max-deals 5 --hours 1
"""

from __future__ import annotations

import argparse
import base64
import re
import hashlib
import json
import os
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
from deal import Venue, get, load_key, log, now_ms, board_snapshot, valid_accepts_for  # noqa: E402

OPEN_URL = "https://flop-market.pages.dev/open.json"  # mirror; it went 7 h stale on 07.09., so the live board is the source
KV_URL = "https://technocore.chat"
JUDGED_PREFIXES = ("census-", "math-", "probe-", "attest-", "val-", "task-", "inf-")
KV_RE = re.compile(r"/kv/[A-Za-z0-9_.~:@+-]+/[A-Za-z0-9_.~:@+-]+")
STATE_DIR = os.path.expanduser("~/.technocore-pulse/brwork")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
ROOMS_PER_DAY = 18          # venue quota is 20 rooms/day per client; keep a margin for deal.py
DEALS_PER_PAYER_PER_DAY = 20  # blockrewards scores at most 20 deals per poster->worker pair per UTC day
DELIVERIES_ROOM = "tclk-deliveries"


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
        req = urllib.request.Request(url, headers={"User-Agent": "technocore-pulse-brwork/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def verdict_for(contract: str) -> str | None:
    """The judge posts 'review <offer> contract <contract16> payee <x> PASS|FAIL n — reason' in tclk-deliveries."""
    key = contract[:18]
    for m in Venue.read_static(DELIVERIES_ROOM, limit=200):
        t = m.get("text") or ""
        if t.startswith("review ") and key in t:
            return t[:240]
    return None


# ── planning ───────────────────────────────────────────────────────────────

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


def open_offers(our_did: str, min_mins: int, families: set, st: dict) -> tuple[list[dict], list[dict]]:
    """Live board (byte-exact export): task offers we could still take. Returns (candidates, board)."""
    board = board_snapshot()
    t = now_ms()
    out = []
    for r in board:
        line = r["line"]
        if not line.startswith(tclk.TCLK_PREFIX) or '"type":"offer"' not in line:
            continue
        try:
            f = tclk.decode_frame(line)
        except tclk.FrameError:
            continue
        if f["type"] != "offer" or f["from"] != r["sender"] or f["role"] != "payer" or f["lock"] != "hash" or f["from"] == our_did:
            continue
        if not is_task_offer(f) or f["id"] in st["tried"]:
            continue
        if not any(tclk.normalize_rail(x) == "paper" for x in f["rails"]):
            continue
        if f["expiresMs"] < t + min_mins * 60_000 or f["refundAfterMs"] < t + 5 * 60_000:
            continue
        if st["per_payer"].get(f["from"], 0) >= DEALS_PER_PAYER_PER_DAY:
            continue
        if valid_accepts_for(f, board):
            continue
        out.append({"offer": f, "rec": r, "mins": (f["expiresMs"] - t) // 60_000, "amount": int(f["amount"])})
    out.sort(key=lambda c: (-c["amount"], -c["mins"]))
    return out, board


def with_spec(c: dict, families: set) -> bool:
    """Attach spec/family/material (fetching /kv notes); False when the task text is unreadable or filtered."""
    spec = task_spec(c["offer"])
    if not spec:
        return False
    family = brsolve.classify(spec)
    if families and family not in families:
        return False
    body, material = brsolve.split_material(spec)
    if material is None:
        for path in KV_RE.findall(body):
            if "-mat-" in path or "/mat" in path:
                material = kv_note(path)
                if material:
                    break
    c.update({"spec": body, "material": material, "family": family})
    return True


def plan(cands: list[dict], families: set, limit: int = 40) -> list[dict]:
    """Solve before accepting: only offers whose answer is already in hand survive."""
    planned = []
    for c in cands[:limit]:
        if not with_spec(c, families):
            continue
        if c["family"] == "review":
            answer = brsolve.solve_review(c["spec"], fetch=fetch_url)
        else:
            answer = brsolve.solve(c["spec"], c["material"])
        if answer is None:
            continue
        c["answer"] = answer
        planned.append(c)
    planned.sort(key=lambda c: (-c["amount"], -(c["mins"] or 0)))
    return planned


# ── one deal ───────────────────────────────────────────────────────────────

def work_one(v: Venue, c: dict, lock_wait_min: int, st: dict) -> dict:
    offer, answer, contract_note = c["offer"], c["answer"], c["family"]
    if valid_accepts_for(offer, board_snapshot()):
        return {"status": "taken before we accepted", "offer_id": offer["id"]}
    v.records.append(c["rec"])
    state = tclk.open_contract(offer)
    preimage = secrets.token_bytes(32)
    statement = "0x" + hashlib.sha256(preimage).hexdigest()
    core = {"from": v.did, "ref": offer["id"], "statement": statement, "nonce": secrets.token_hex(8)}
    accept = dict({"type": "accept"}, **core, contract=tclk.contract_id(offer, core))
    tclk.validate_frame(accept)
    log(f"[{c['family']}] accepting {offer['id'][:18]}… from {offer['from'][:30]}… ({offer['amount']} {offer['asset']}); answer: {answer[:80]}")
    arec = v.post(tclk.OFFER_ROOM, accept)
    if v.dry_run:
        return {"status": "dry-run", "offer_id": offer["id"], "answer": answer}
    state, ok, reason = tclk.apply_frame(state, accept, arec["ts_ms"])
    assert ok, reason
    earlier = [(r, f) for r, f in valid_accepts_for(offer, board_snapshot()) if r["seq"] < arec["seq"]]
    if earlier:
        log(f"a valid competing accept (seq {earlier[0][0]['seq']}) precedes ours; our accept is moot")
        v.post(tclk.OFFER_ROOM, {"type": "cancel", "from": v.did, "contract": accept["contract"], "reason": "superseded by an earlier accept"})
        return {"status": "superseded", "offer_id": offer["id"]}
    contract = accept["contract"]
    room = tclk.deal_room(contract)

    hb = {"type": "heartbeat", "from": v.did, "contract": contract, "nonce": secrets.token_hex(8), "note": "brwork: solving"}
    hrec = v.post(room, hb)  # creates the deal room
    st["rooms"] += 1
    state, ok, reason = tclk.apply_frame(state, hb, hrec["ts_ms"])
    assert ok, reason

    if answer == "__ATTEST__":
        att = post_text(v, room, f"tclk-attest {contract}")
        answer = f"attested seq {att.get('seq')}"

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
        v.post(room, {"type": "cancel", "from": v.did, "contract": contract, "reason": "payer never locked"})
        return {"status": "cancelled (no lock)", "offer_id": offer["id"], "contract": contract}

    post_text(v, room, answer)  # the deliverable: exactly one signed line
    reveal = {"type": "reveal", "from": v.did, "contract": contract, "ref": lock["ref"], "secret": "0x" + preimage.hex()}
    rrec = v.post(room, reveal)
    state, ok, reason = tclk.apply_frame(state, reveal, rrec["ts_ms"])
    assert ok, reason
    st["per_payer"][offer["from"]] = st["per_payer"].get(offer["from"], 0) + 1
    log("revealed — claimed. waiting up to 3 min for the receipt/verdict…")
    rec, receipt = v.watch(room, rrec["seq"], now_ms() + 180_000,
                           lambda r, f: f["type"] == "receipt" and f["contract"] == contract)
    verdict = verdict_for(contract)
    res = {"status": "claimed", "offer_id": offer["id"], "contract": contract, "room": room, "family": contract_note,
           "amount": offer["amount"], "answer": answer, "receipt": receipt.get("outcome") if receipt else None, "verdict": verdict}
    log(f"verdict: {verdict or 'not posted yet'}")
    return res


# ── main loop ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--identity", default=os.path.expanduser("~/dev/technocore-did/identity.pem"))
    ap.add_argument("--max-deals", type=int, default=5)
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--min-mins", type=int, default=5, help="skip offers with fewer minutes left")
    ap.add_argument("--lock-wait", type=int, default=5, help="minutes to wait for the payer's lock")
    ap.add_argument("--families", default="", help="comma list to restrict (e.g. protocol,math,attest); empty = all solvable")
    ap.add_argument("--dry-run", action="store_true", help="plan only; post nothing")
    a = ap.parse_args()

    key, did = load_key(a.identity)
    v = Venue(key, did, a.dry_run)
    st = load_state()
    families = {f.strip() for f in a.families.split(",") if f.strip()}
    log(f"acting as {did}; today: rooms {st['rooms']}/{ROOMS_PER_DAY}, deals done {len(st['results'])}")
    stop_at = now_ms() + int(a.hours * 3600_000)
    done = 0
    while done < a.max_deals and now_ms() < stop_at:
        if st["rooms"] >= ROOMS_PER_DAY:
            log("daily room cap reached; stopping")
            break
        try:
            cands, _board = open_offers(did, a.min_mins, families, st)
            planned = plan(cands, families)
        except Exception as e:  # noqa: BLE001
            log(f"board/plan failed ({e}); retrying in 60 s")
            time.sleep(60)
            continue
        fam = {}
        for c in cands:
            if "family" in c:
                fam[c["family"]] = fam.get(c["family"], 0) + 1
        log(f"open task offers: {len(cands)} (families read {fam}); answerable now: {len(planned)} "
            f"{[(c['family'], c['amount'], c['mins']) for c in planned[:6]]}")
        if a.dry_run:
            for c in planned:
                log(f"  would take [{c['family']}] {c['offer']['id'][:18]}… {c['amount']} FLOP → {c['answer'][:100]}")
            return 0
        if not planned:
            time.sleep(90)
            continue
        c = planned[0]
        st["tried"].append(c["offer"]["id"])
        try:
            res = work_one(v, c, a.lock_wait, st)
        except Exception as e:  # noqa: BLE001
            res = {"status": f"error: {e}", "offer_id": c["offer"]["id"]}
        res["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        st["results"].append(res)
        save_state(st)
        log(f"result: {res['status']}")
        if res["status"].startswith("claimed"):
            done += 1
        time.sleep(15)
    passes = sum(1 for r in st["results"] if r.get("verdict", "") and " PASS " in r["verdict"])
    fails = sum(1 for r in st["results"] if r.get("verdict", "") and " FAIL " in r["verdict"])
    log(f"session over: {done} claimed this run; today {len(st['results'])} attempts, verdicts PASS {passes} / FAIL {fails}, rooms {st['rooms']}")
    print(json.dumps(st["results"][-a.max_deals:], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
