#!/usr/bin/env python3
"""tclk/1 (flop-labs/tclk) — independent Python port + live-board audit.

flop-labs/tclk is FLOP Labs' HTLC/PTLC deal-making convention: offer / accept /
lock / reveal / refund as signed technocore room messages. This module is an
independent re-implementation of the wire rules (canonical JSON, offer id,
contract id, frame validation, state machine, transcript fold with room binding)
written from SPEC.md and cross-checked against the golden vectors in
tests/vectors.test.ts — plus an auditor that folds the live `tclk-offers` board.

    python3 tclk.py selftest                     # golden vectors must match byte-for-byte
    python3 tclk.py audit                        # export tclk-offers, decode, fold, report
    python3 tclk.py audit --board board.jsonl    # audit an /export dump offline
    python3 tclk.py audit --probe 80 --json out.json

Read-only. Stdlib only for decoding/ids/fold; Ed25519 record verification uses
`cryptography` when installed (records are marked "unverified" otherwise).
Python 3.9+.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_URL = "https://technocore.chat"
USER_AGENT = "technocore-pulse-tclk/1.0"
TCLK_PREFIX = "tclk1 "
TCLK_DOMAIN = "FLOP::tclk::v1"
MAX_FRAME_CHARS = 4096
OFFER_ROOM = "tclk-offers"

HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
HEX33 = re.compile(r"^0x[0-9a-f]{66}$")
DID = re.compile(r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$")
AMOUNT = re.compile(r"^[1-9][0-9]*$")
ASSET = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
LEGACY_RAIL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
NONCE = re.compile(r"^[0-9a-f]{8,64}$")
SCALAR_HEX = re.compile(r"^0x(?:[0-9a-f]{2}){1,32}$")
STATEMENT = re.compile(r"^0x(?:[0-9a-f]{64}|[0-9a-f]{66})$")
JOB_PROTO = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
ROOM_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
REC_NONCE = re.compile(r"^(?:0|[1-9][0-9]*)$")
REC_SIG = re.compile(r"^[A-Za-z0-9_-]{85}[AQgw]$")
CANONICAL_RAILS = {"btc-htlc", "evm-htlc", "flop-htlc", "memory", "near-htlc", "paper", "x402"}
RAIL_ALIASES = {"paperrail": "paper", "paper-rail": "paper"}
VALUE_RAILS = {"btc-htlc", "evm-htlc", "flop-htlc", "near-htlc", "x402"}

FRAME_FIELDS = {
    "offer": (["type", "from", "role", "amount", "asset", "lock", "rails", "claimByMs", "refundAfterMs",
               "expiresMs", "nonce", "id"], ["paymentKey", "job"]),
    "accept": (["type", "from", "ref", "statement", "contract", "nonce"], ["paymentKey"]),
    "lock": (["type", "from", "contract", "rail", "ref"], ["presig"]),
    "reveal": (["type", "from", "contract", "secret"], ["ref"]),
    "refund": (["type", "from", "contract"], ["ref", "reason"]),
    "cancel": (["type", "from", "contract"], ["reason"]),
    "receipt": (["type", "from", "contract", "outcome"], ["rail", "ref"]),
    "heartbeat": (["type", "from", "contract", "nonce"], ["note"]),
}
TERMINAL = {"claimed", "refunded", "cancelled"}


class FrameError(ValueError):
    pass


def fail(msg: str):
    raise FrameError(f"tclk: {msg}")


# ── secp256k1 (pure Python; only used for point locks / paymentKey checks) ───
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _is_valid_point(hex33: str) -> bool:
    try:
        prefix, x = int(hex33[2:4], 16), int(hex33[4:], 16)
    except ValueError:
        return False
    if prefix not in (2, 3) or x >= P:
        return False
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    return (y * y) % P == y2


def _ec_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = (3 * a[0] * a[0]) * pow(2 * a[1], P - 2, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], P - 2, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return (x, (lam * (a[0] - x) - a[1]) % P)


def _ec_mul(k: int, point=G):
    result, addend = None, point
    while k:
        if k & 1:
            result = _ec_add(result, addend)
        addend = _ec_add(addend, addend)
        k >>= 1
    return result


def _verify_point_witness(statement: str, secret: str) -> bool:
    try:
        y = int(secret, 16)
        if not (0 < y < N):
            return False
        pt = _ec_mul(y)
        if pt is None:
            return False
        compressed = ("02" if pt[1] % 2 == 0 else "03") + f"{pt[0]:064x}"
        return "0x" + compressed == statement.lower()
    except Exception:  # noqa: BLE001
        return False


# ── canonical encoding & ids (port of frames.ts) ─────────────────────────────

def canonical_json(value) -> str:
    """Sorted keys, compact separators, non-ASCII \\uXXXX-escaped (JSON.stringify + toAscii)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def domain_hash(tag: str, payload: str) -> str:
    return "0x" + hashlib.sha256(f"{TCLK_DOMAIN}|{tag}|{payload}".encode("utf-8")).hexdigest()


def offer_id(fields: dict) -> str:
    return domain_hash("offer", canonical_json({k: v for k, v in fields.items() if k != "id"}))


def contract_id(offer: dict, accept_core: dict) -> str:
    core = {k: accept_core[k] for k in ("from", "ref", "statement", "paymentKey", "nonce") if accept_core.get(k) is not None}
    return domain_hash("contract", canonical_json({"offer": offer, "accept": core}))


def encode_frame(frame: dict) -> str:
    return TCLK_PREFIX + canonical_json(frame)


def deal_room(contract: str) -> str:
    if not HEX32.match(contract):
        fail(f"malformed contract id: {contract}")
    return f"mb-p-tclk-{contract[2:18]}"


def normalize_rail(value: str) -> str:
    spelling = value.strip().lower()
    canonical = RAIL_ALIASES.get(spelling, spelling)
    if canonical not in CANONICAL_RAILS:
        fail(f"unregistered rail id: {value}")
    return canonical


# ── validation (fail-closed, port of validateFrame) ─────────────────────────

def _req_str(v, name, rx=None):
    if not isinstance(v, str) or not v:
        fail(f"{name} must be a non-empty string")
    if rx and not rx.match(v):
        fail(f"{name} is malformed")
    return v


def _req_ms(v, name):
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or v > 2**53 - 1:
        fail(f"{name} must be a positive unix-ms integer")
    return v


def _req_keys(rec: dict, allowed, required, what):
    for k in rec:
        if k not in allowed:
            fail(f"unknown field on {what}: {k}")
    for k in required:
        if k not in rec:
            fail(f"missing field on {what}: {k}")


def validate_frame(frame) -> dict:
    if not isinstance(frame, dict):
        fail("frame must be an object")
    t = frame.get("type")
    if t not in FRAME_FIELDS:
        fail(f"unknown frame type: {t}")
    req, opt = FRAME_FIELDS[t]
    _req_keys(frame, set(req) | set(opt), req, t)
    _req_str(frame.get("from"), "from", DID)
    if t == "offer":
        if frame.get("role") not in ("payer", "payee"):
            fail("role must be payer|payee")
        _req_str(frame.get("amount"), "amount", AMOUNT)
        _req_str(frame.get("asset"), "asset", ASSET)
        if frame.get("lock") not in ("hash", "point"):
            fail("lock must be hash|point")
        rails = frame.get("rails")
        if not isinstance(rails, list) or not rails:
            fail("rails must be a non-empty array")
        for r in rails:
            _req_str(r, "rail", LEGACY_RAIL)
        claim_by = _req_ms(frame.get("claimByMs"), "claimByMs")
        refund_after = _req_ms(frame.get("refundAfterMs"), "refundAfterMs")
        _req_ms(frame.get("expiresMs"), "expiresMs")
        if claim_by >= refund_after:
            fail("claimByMs must be strictly before refundAfterMs")
        if "paymentKey" in frame:
            _req_str(frame["paymentKey"], "paymentKey", HEX33)
            if not _is_valid_point(frame["paymentKey"]):
                fail("paymentKey is not a valid secp256k1 point")
        if frame["lock"] == "point" and "paymentKey" not in frame:
            fail("point locks require paymentKey")
        if "job" in frame:
            job = frame["job"]
            if not isinstance(job, dict):
                fail("job must be an object")
            _req_keys(job, {"proto", "id", "context"}, ["proto", "id"], "job")
            _req_str(job["proto"], "job.proto", JOB_PROTO)
            _req_str(job["id"], "job.id")
            if "context" in job:
                _req_str(job["context"], "job.context")
        _req_str(frame.get("nonce"), "nonce", NONCE)
        expected = offer_id(frame)
        if frame.get("id") != expected:
            fail("offer id mismatch")
    elif t == "accept":
        _req_str(frame.get("ref"), "ref", HEX32)
        _req_str(frame.get("statement"), "statement", STATEMENT)
        _req_str(frame.get("contract"), "contract", HEX32)
        if "paymentKey" in frame:
            _req_str(frame["paymentKey"], "paymentKey", HEX33)
            if not _is_valid_point(frame["paymentKey"]):
                fail("paymentKey is not a valid secp256k1 point")
        _req_str(frame.get("nonce"), "nonce", NONCE)
    elif t == "lock":
        _req_str(frame.get("contract"), "contract", HEX32)
        _req_str(frame.get("rail"), "rail", LEGACY_RAIL)
        _req_str(frame.get("ref"), "ref")
        if "presig" in frame:
            ps = frame["presig"]
            if not isinstance(ps, dict):
                fail("presig must be an object")
            _req_keys(ps, {"nonce", "s"}, ["nonce", "s"], "presig")
            _req_str(ps["nonce"], "presig.nonce", HEX33)
            _req_str(ps["s"], "presig.s", SCALAR_HEX)
    elif t == "reveal":
        _req_str(frame.get("contract"), "contract", HEX32)
        if "ref" in frame:
            _req_str(frame["ref"], "ref")
        _req_str(frame.get("secret"), "secret", HEX32)
    elif t == "refund":
        _req_str(frame.get("contract"), "contract", HEX32)
        for k in ("ref", "reason"):
            if k in frame:
                _req_str(frame[k], k)
    elif t == "cancel":
        _req_str(frame.get("contract"), "contract", HEX32)
        if "reason" in frame:
            _req_str(frame["reason"], "reason")
    elif t == "receipt":
        _req_str(frame.get("contract"), "contract", HEX32)
        if frame.get("outcome") not in ("claimed", "refunded", "cancelled"):
            fail("outcome must be claimed|refunded|cancelled")
        if "rail" in frame:
            _req_str(frame["rail"], "rail", LEGACY_RAIL)
        if "ref" in frame:
            _req_str(frame["ref"], "ref")
    elif t == "heartbeat":
        _req_str(frame.get("contract"), "contract", HEX32)
        _req_str(frame.get("nonce"), "nonce", NONCE)
        if "note" in frame:
            _req_str(frame["note"], "note")
    return frame


def decode_frame(text: str) -> dict:
    if not text.startswith(TCLK_PREFIX):
        fail("not a tclk/1 line")
    if len(text) > MAX_FRAME_CHARS:
        fail("frame exceeds the 4096-char room-message cap")
    try:
        parsed = json.loads(text[len(TCLK_PREFIX):])
    except json.JSONDecodeError:
        fail("frame is not valid JSON")
    return validate_frame(parsed)


def decode_reason(text: str) -> str:
    try:
        decode_frame(text)
    except FrameError as e:
        return str(e).replace("tclk: ", "")
    return "ok"


# ── state machine (port of machine.ts) ──────────────────────────────────────

def _rail_offered(offered, selected) -> bool:
    if selected in offered:
        return True
    try:
        target = normalize_rail(selected)
    except FrameError:
        return False
    for r in offered:
        try:
            if normalize_rail(r) == target:
                return True
        except FrameError:
            continue
    return False


def open_contract(offer: dict) -> dict:
    validate_frame(offer)
    payer = offer["role"] == "payer"
    return {"status": "proposed", "offer": offer,
            "payerDid": offer["from"] if payer else None, "payeeDid": None if payer else offer["from"],
            "contract": None, "statement": None, "rail": None, "railRef": None, "secret": None}


def apply_frame(state: dict, frame: dict, now_ms: int):
    """Return (new_state, ok, reason). Never throws on a bad frame."""
    def reject(reason):
        return state, False, reason
    try:
        validate_frame(frame)
    except FrameError as e:
        return reject(str(e))
    t, st, offer = frame["type"], state["status"], state["offer"]
    party = frame["from"] in (offer["from"], state["payerDid"], state["payeeDid"])
    if t == "offer":
        return reject("contract is already open")
    if t == "accept":
        if st != "proposed":
            return reject(f"accept in status {st}")
        if frame["ref"] != offer["id"]:
            return reject("accept.ref names a different offer")
        if frame["from"] == offer["from"]:
            return reject("cannot accept own offer")
        if now_ms >= offer["expiresMs"]:
            return reject("offer has expired")
        if frame["contract"] != contract_id(offer, frame):
            return reject("contract id mismatch")
        if offer["lock"] == "point" and "paymentKey" not in frame:
            return reject("point locks require the acceptor's paymentKey")
        if (offer["lock"] == "hash" and not HEX32.match(frame["statement"])) or \
           (offer["lock"] == "point" and not (HEX33.match(frame["statement"]) and _is_valid_point(frame["statement"]))):
            return reject(f"statement does not fit a {offer['lock']} lock")
        acceptor_is_payer = offer["role"] == "payee"
        new = dict(state, status="accepted", contract=frame["contract"], statement=frame["statement"],
                   payerDid=frame["from"] if acceptor_is_payer else state["payerDid"],
                   payeeDid=state["payeeDid"] if acceptor_is_payer else frame["from"])
        return new, True, None
    if t == "lock":
        if st != "accepted":
            return reject(f"lock in status {st}")
        if frame["contract"] != state["contract"]:
            return reject("lock names a different contract")
        if frame["from"] != state["payerDid"]:
            return reject("only the payer locks")
        if now_ms >= offer["refundAfterMs"]:
            return reject("refund window is already open")
        if not _rail_offered(offer["rails"], frame["rail"]):
            return reject(f"rail {frame['rail']} was not offered")
        return dict(state, status="locked", rail=frame["rail"], railRef=frame["ref"]), True, None
    if t == "reveal":
        if st != "locked":
            return reject(f"reveal in status {st}")
        if frame["contract"] != state["contract"]:
            return reject("reveal names a different contract")
        if "ref" in frame and frame["ref"] != state["railRef"]:
            return reject("reveal names a different rail ref")
        if frame["from"] != state["payeeDid"]:
            return reject("only the payee reveals")
        if now_ms >= offer["refundAfterMs"]:
            return reject("refund window is open")
        ok = (hashlib.sha256(bytes.fromhex(frame["secret"][2:])).hexdigest() == state["statement"][2:]) \
            if offer["lock"] == "hash" else _verify_point_witness(state["statement"], frame["secret"])
        if not ok:
            return reject("secret does not open the statement")
        return dict(state, status="claimed", secret=frame["secret"]), True, None
    if t == "refund":
        if st != "locked":
            return reject(f"refund in status {st}")
        if frame["contract"] != state["contract"]:
            return reject("refund names a different contract")
        if "ref" in frame and frame["ref"] != state["railRef"]:
            return reject("refund names a different rail ref")
        if frame["from"] != state["payerDid"]:
            return reject("only the payer refunds")
        if now_ms < offer["refundAfterMs"]:
            return reject("refund window not open yet")
        return dict(state, status="refunded"), True, None
    if t == "cancel":
        if st not in ("proposed", "accepted"):
            return reject(f"cancel in status {st}")
        if st == "accepted" and frame["contract"] != state["contract"]:
            return reject("cancel names a different contract")
        if not party:
            return reject("cancel from a non-party")
        return dict(state, status="cancelled"), True, None
    if t == "receipt":
        if st not in TERMINAL:
            return reject("receipt before a terminal status")
        if frame["contract"] != state["contract"]:
            return reject("receipt names a different contract")
        if not party:
            return reject("receipt from a non-party")
        if frame["outcome"] != st:
            return reject(f"receipt outcome {frame['outcome']} does not match {st}")
        if "rail" in frame and state["rail"] is not None and frame["rail"] != state["rail"]:
            return reject("receipt rail does not match contract rail")
        if "ref" in frame and state["railRef"] is not None and frame["ref"] != state["railRef"]:
            return reject("receipt ref does not match contract railRef")
        if st == "cancelled" and ("rail" in frame or "ref" in frame):
            return reject("receipt on cancelled contract cannot name a settlement rail")
        return state, True, None
    if t == "heartbeat":
        if st not in ("accepted", "locked"):
            return reject(f"heartbeat in status {st}")
        if frame["contract"] != state["contract"]:
            return reject("heartbeat names a different contract")
        if not party:
            return reject("heartbeat from a non-party")
        return state, True, None
    return reject("unreachable")


# ── transcript records + fold (port of transcript.ts) ───────────────────────

def _verify_ed25519(did: str, sig_b64url: str, message: str) -> bool | None:
    """True/False, or None when `cryptography` is unavailable (unverified)."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: PLC0415
        from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
        from client import public_key_bytes_from_did  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        key = Ed25519PublicKey.from_public_bytes(public_key_bytes_from_did(did))
        key.verify(base64.urlsafe_b64decode(sig_b64url + "=="), message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, Exception):  # noqa: BLE001
        return False


def transcript_record(room: str, m: dict) -> dict:
    """Normalize one /export or ?format=json message; keeps the exact stored line."""
    if not ROOM_NAME.match(room):
        fail(f"invalid transcript room {room!r}")
    seq = m.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        fail("transcript message seq must be a non-negative integer")
    ts = m.get("ts")
    if not isinstance(ts, str):
        fail("transcript message has no timestamp")
    try:
        ts_ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        fail("transcript message timestamp is invalid")
    nonce = m.get("nonce")
    nonce = str(nonce) if isinstance(nonce, int) and not isinstance(nonce, bool) else (nonce if isinstance(nonce, str) else None)
    sig = m.get("sig") if isinstance(m.get("sig"), str) else None
    return {"room": room, "seq": seq, "ts_ms": ts_ms, "sender": m.get("from") or "", "nonce": nonce,
            "sig": sig, "line": m.get("text") or ""}


def verify_record(rec: dict, check_signature: bool = True):
    """(ok, reason). Signature check is skipped (ok) when cryptography is missing."""
    if rec["nonce"] is None or rec["sig"] is None:
        return False, "record is unsigned"
    if not REC_NONCE.match(rec["nonce"]):
        return False, "record nonce is not canonical decimal"
    if not REC_SIG.match(rec["sig"]):
        return False, "record signature is not canonical base64url"
    if not rec["sender"].startswith("did:key:z"):
        return False, "record sender is not an Ed25519 did:key"
    if check_signature:
        v = _verify_ed25519(rec["sender"], rec["sig"], f"{rec['room']}|{rec['nonce']}|{rec['line']}")
        if v is False:
            return False, "record signature does not verify"
    return True, None


def fold_transcript(records: list, room_binding: str = "strict", check_signature: bool = True):
    """Fold records in order. room_binding: 'strict' (SPEC §2) or 'offer-room-fallback'
    (issue #61 proposal: post-accept frames accepted in either the derived deal room or
    tclk-offers). Returns (state|None, steps)."""
    steps, state = [], None
    for i, rec in enumerate(records):
        base = {"index": i, "room": rec["room"], "seq": rec["seq"]}
        ok, reason = verify_record(rec, check_signature)
        if not ok:
            steps.append(dict(base, ok=False, reason=reason)); continue
        try:
            frame = decode_frame(rec["line"])
        except FrameError:
            steps.append(dict(base, ok=False, reason=decode_reason(rec["line"]))); continue
        if frame["from"] != rec["sender"]:
            steps.append(dict(base, type=frame["type"], ok=False, reason=f"{frame['type']}.from does not match the record sender")); continue
        if state is None:
            if frame["type"] != "offer":
                steps.append(dict(base, type=frame["type"], ok=False, reason="no contract open yet")); continue
            if rec["room"] != OFFER_ROOM:
                steps.append(dict(base, type="offer", ok=False, reason=f"offer must be posted in {OFFER_ROOM}")); continue
            try:
                state = open_contract(frame); steps.append(dict(base, type="offer", ok=True))
            except FrameError as e:
                steps.append(dict(base, type="offer", ok=False, reason=str(e)))
            continue
        if frame["type"] in ("offer", "accept") or state["contract"] is None:
            allowed = {OFFER_ROOM}
        else:
            allowed = {deal_room(state["contract"])}
            if room_binding == "offer-room-fallback":
                allowed.add(OFFER_ROOM)
        if rec["room"] not in allowed:
            steps.append(dict(base, type=frame["type"], ok=False, reason=f"{frame['type']} must be posted in {sorted(allowed)[0]}")); continue
        state, ok, reason = apply_frame(state, frame, rec["ts_ms"])
        steps.append(dict(base, type=frame["type"], ok=ok, reason=reason))
    return state, steps


# ── golden vectors (tests/vectors.test.ts) ──────────────────────────────────

def selftest() -> int:
    payer, payee = "did:key:z6Mk" + "f" * 44, "did:key:z6Mk" + "g" * 44
    fields = {"type": "offer", "from": payer, "role": "payer", "amount": "1000000", "asset": "FLOP", "lock": "hash",
              "rails": ["flop-htlc", "x402"], "claimByMs": 1756703600000, "refundAfterMs": 1756707200000,
              "expiresMs": 1756700600000, "job": {"proto": "a2a", "id": "task-3f", "context": "ctx-1"},
              "nonce": "9f2c81d04c9e1f7a"}
    oid = offer_id(fields)
    offer = dict(fields, id=oid)
    core = {"from": payee, "ref": oid, "statement": "0x" + "ab" * 32, "nonce": "0011223344556677"}
    cid = contract_id(offer, core)
    accept = dict(type="accept", contract=cid, **core)
    expect_oid = "0xd001fbbf4fa36d9ab8ea88df02a8b3303539e9d59f7ff9d9bfeb679318e9ce75"
    expect_cid = "0x2768bf32b455317879796093ff2e5882371cbec238611ca71f555a7fcbe58e1c"
    offer_line = ('tclk1 {"amount":"1000000","asset":"FLOP","claimByMs":1756703600000,"expiresMs":1756700600000,'
                  f'"from":"{payer}","id":"{expect_oid}",'
                  '"job":{"context":"ctx-1","id":"task-3f","proto":"a2a"},"lock":"hash",'
                  '"nonce":"9f2c81d04c9e1f7a","rails":["flop-htlc","x402"],"refundAfterMs":1756707200000,'
                  '"role":"payer","type":"offer"}')
    accept_line = (f'tclk1 {{"contract":"{expect_cid}","from":"{payee}","nonce":"0011223344556677","ref":"{expect_oid}",'
                   '"statement":"0xabababababababababababababababababababababababababababababababab","type":"accept"}')
    non_ascii = dict(fields, amount="100", rails=["flop-htlc"], job={"proto": "a2a", "id": "tâche-1"})
    checks = [
        ("offer id", oid, expect_oid),
        ("offer line", encode_frame(offer), offer_line),
        ("contract id", cid, expect_cid),
        ("accept line", encode_frame(accept), accept_line),
        ("non-ASCII offer id", offer_id(non_ascii), "0xfdad69c602bef151596e3e914cc3ca05b1ccd009211b57c4fdbf0ba0e0d4635b"),
        ("decode(offer line) validates", decode_frame(offer_line)["id"], expect_oid),
    ]
    ok = True
    for name, got, want in checks:
        good = got == want
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {name}")
        if not good:
            print(f"        got  {got}\n        want {want}")
    # state machine round trip on the vectors
    recs = [{"room": OFFER_ROOM, "seq": 1, "ts_ms": 1756700000000, "sender": payer, "nonce": "1", "sig": "A" * 85 + "A", "line": offer_line},
            {"room": OFFER_ROOM, "seq": 2, "ts_ms": 1756700001000, "sender": payee, "nonce": "2", "sig": "A" * 85 + "A", "line": accept_line}]
    state, steps = fold_transcript(recs, check_signature=False)
    fold_ok = state is not None and state["status"] == "accepted" and all(s["ok"] for s in steps)
    ok &= fold_ok
    print(f"  {'PASS' if fold_ok else 'FAIL'}  fold offer+accept -> accepted (structure only, signatures not checked)")
    print("golden vectors:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


# ── live board audit ────────────────────────────────────────────────────────

def _get(url: str, timeout: float = 60.0, retries: int = 3):
    delay = 2.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            time.sleep(delay); delay *= 2


def load_board(path: str | None) -> list:
    raw = open(path, encoding="utf-8").read() if path else _get(f"{BASE_URL}/r/{OFFER_ROOM}/export", timeout=120)
    recs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(m, dict):
            try:
                recs.append(transcript_record(OFFER_ROOM, m))
            except FrameError:
                continue
    recs.sort(key=lambda r: r["seq"])
    return recs


def fingerprint(offer: dict) -> str:
    """Implementation fingerprint: the shape choices a codebase makes, not the deal."""
    gap = (offer["refundAfterMs"] - offer["claimByMs"]) // 60000
    exp = (offer["expiresMs"] - offer["claimByMs"]) // 60000
    return f"nonce{len(offer['nonce'])}|{','.join(sorted(offer['rails']))}|{offer['asset']}|{offer['lock']}|" \
           f"{'job:'+offer['job']['proto'] if 'job' in offer else 'nojob'}|{offer['role']}|gap{gap}m|exp{exp}m"


def audit(board_path: str | None, probe: int, json_out: str | None, verify_sigs: bool) -> int:
    t0 = time.time()
    recs = load_board(board_path)
    tclk = [r for r in recs if r["line"].startswith(TCLK_PREFIX)]
    span = (recs[0]["ts_ms"], recs[-1]["ts_ms"]) if recs else (0, 0)
    decoded, reasons, types = {}, Counter(), Counter()
    for r in tclk:
        try:
            f = decode_frame(r["line"]); decoded[r["seq"]] = f; types[f["type"]] += 1
        except FrameError as e:
            reasons[str(e).replace("tclk: ", "")] += 1
            try:
                types["invalid:" + str(json.loads(r["line"][6:]).get("type"))] += 1
            except Exception:  # noqa: BLE001
                types["invalid:unparseable"] += 1
    frames = [(r, decoded[r["seq"]]) for r in tclk if r["seq"] in decoded]
    from_mismatch = sum(1 for r, f in frames if f["from"] != r["sender"])
    sig_checked = sig_bad = 0
    if verify_sigs:
        for r, f in frames:
            v = _verify_ed25519(r["sender"], r["sig"] or "", f"{r['room']}|{r['nonce']}|{r['line']}") if r["sig"] else False
            if v is None:
                verify_sigs = False; break
            sig_checked += 1; sig_bad += 0 if v else 1

    offers = {f["id"]: (r, f) for r, f in frames if f["type"] == "offer"}
    accepts_by_contract, post = {}, defaultdict(list)
    accept_no_offer = accept_bad_contract = 0
    for r, f in frames:
        if f["type"] == "accept":
            o = offers.get(f["ref"])
            if o is None:
                accept_no_offer += 1; continue
            if contract_id(o[1], f) != f["contract"]:
                accept_bad_contract += 1; continue
            accepts_by_contract.setdefault(f["contract"], (r, f))
        elif f["type"] not in ("offer",):
            post[f["contract"]].append((r, f))

    strict, fallback, stuck_reasons = Counter(), Counter(), Counter()
    for c, (ar, af) in accepts_by_contract.items():
        o_rec = offers[af["ref"]][0]
        chain = sorted([o_rec, ar] + [r for r, _ in post.get(c, [])], key=lambda r: r["seq"])
        s1, _ = fold_transcript(chain, "strict", check_signature=False)
        s2, steps2 = fold_transcript(chain, "offer-room-fallback", check_signature=False)
        strict[s1["status"] if s1 else "none"] += 1
        fallback[s2["status"] if s2 else "none"] += 1
        for st in steps2:
            if not st["ok"] and st.get("type") in ("lock", "reveal", "refund"):
                stuck_reasons[st["reason"]] += 1

    # derived-room probe (read-only): do post-accept frames exist where §2 says they must?
    probed, room_has_lock, room_claimed, room_unreadable = 0, 0, 0, 0
    if probe > 0:
        # sample accepted contracts old enough to have had time to lock (>= 2 h), newest of those
        # a RANDOM sample of contracts old enough to have had time to lock (>= 2 h); the newest
        # contracts are dominated by whichever bot family is flooding the board right now
        import random  # noqa: PLC0415
        cutoff = span[1] - 2 * 3600 * 1000
        mature = [(c, v) for c, v in accepts_by_contract.items() if v[0]["ts_ms"] <= cutoff] or list(accepts_by_contract.items())
        random.seed(20260903)
        for c, (ar, af) in random.sample(mature, min(probe, len(mature))):
            probed += 1
            try:
                page = json.loads(_get(f"{BASE_URL}/r/{deal_room(c)}?format=json&limit=200", timeout=25, retries=2))
            except Exception:  # noqa: BLE001
                room_unreadable += 1; continue
            room_recs = []
            for m in page.get("messages", []):
                try:
                    room_recs.append(transcript_record(deal_room(c), m))
                except FrameError:
                    continue
            if any(r["line"].startswith(TCLK_PREFIX) and '"type":"lock"' in r["line"] for r in room_recs):
                room_has_lock += 1
            o_rec = offers[af["ref"]][0]
            s, _ = fold_transcript(sorted([o_rec, ar] + room_recs, key=lambda r: (r["ts_ms"], r["seq"])), "strict", check_signature=False)
            if s and s["status"] == "claimed":
                room_claimed += 1

    # who deals with whom: fingerprints, pairs, accept latency
    fps = Counter(fingerprint(f) for _, f in offers.values())
    fp_senders = defaultdict(set)
    for _, f in offers.values():
        fp_senders[fingerprint(f)].add(f["from"])
    pairs, latency, senders = Counter(), Counter(), set()
    for c, (ar, af) in accepts_by_contract.items():
        o_rec, of = offers[af["ref"]]
        pairs[(of["from"], af["from"])] += 1
        senders.update((of["from"], af["from"]))
        dt = (ar["ts_ms"] - o_rec["ts_ms"]) / 1000
        latency["<2s" if dt < 2 else "<10s" if dt < 10 else "<60s" if dt < 60 else "<10m" if dt < 600 else ">=10m"] += 1
    repeat_pairs = sum(1 for p, n in pairs.items() if n >= 3)
    repeat_deals = sum(n for p, n in pairs.items() if n >= 3)
    rails = Counter(rail for _, f in offers.values() for rail in f["rails"])
    lock_rails = Counter(f["rail"] for c in post for _, f in post[c] if f["type"] == "lock")

    out = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "board": {"records": len(recs), "tclk_lines": len(tclk), "span_hours": round((span[1] - span[0]) / 3600000, 1),
                  "decoded": len(frames), "decode_failures": sum(reasons.values()), "top_failure_reasons": reasons.most_common(8),
                  "types": dict(types), "from_mismatch": from_mismatch,
                  "signatures_checked": sig_checked, "signatures_bad": sig_bad, "signatures_verified": bool(verify_sigs)},
        "offers": len(offers), "accepts_valid": len(accepts_by_contract), "accept_no_offer": accept_no_offer,
        "accept_contract_mismatch": accept_bad_contract,
        "fold_strict": dict(strict), "fold_fallback": dict(fallback), "post_accept_rejections": stuck_reasons.most_common(8),
        "derived_rooms": {"probed": probed, "sampling": "random, contracts >=2h old, seed 20260903", "with_lock": room_has_lock, "folded_claimed": room_claimed, "unreadable": room_unreadable},
        "rails_offered": dict(rails), "rails_locked": dict(lock_rails),
        "value_rail_locks": sum(n for r, n in lock_rails.items() if r in VALUE_RAILS),
        "counterparties": {"distinct_dids_in_deals": len(senders), "pairs": len(pairs), "repeat_pairs_3plus": repeat_pairs,
                           "deals_in_repeat_pairs": repeat_deals, "accept_latency": dict(latency)},
        "fingerprints": [{"shape": k, "offers": n, "senders": len(fp_senders[k])} for k, n in fps.most_common(6)],
        "seconds": round(time.time() - t0, 1),
    }
    if json_out:
        json.dump(out, open(json_out, "w"), indent=1)
    b = out["board"]
    print(f"tclk/1 board audit — /r/{OFFER_ROOM} ({b['records']} records over {b['span_hours']} h, {b['tclk_lines']} tclk lines)")
    print(f"  decoded {b['decoded']} | rejected {b['decode_failures']}  top reasons: " + "; ".join(f"{r} ×{n}" for r, n in b['top_failure_reasons'][:4]))
    print(f"  frame types: {dict(types)}")
    print(f"  frame.from != record sender: {from_mismatch} | signatures: {'checked '+str(sig_checked)+', bad '+str(sig_bad) if b['signatures_verified'] else 'NOT verified (install cryptography)'}")
    print(f"  offers {len(offers)} | valid accepts {len(accepts_by_contract)} | accepts w/o offer on board {accept_no_offer} | contract-id mismatch {accept_bad_contract}")
    print(f"  fold (SPEC §2 strict):   {dict(strict)}")
    print(f"  fold (offer-room fallback): {dict(fallback)}")
    print(f"  post-accept rejections: " + "; ".join(f"{r} ×{n}" for r, n in stuck_reasons.most_common(5)))
    if probed:
        print(f"  derived deal rooms probed {probed}: lock present {room_has_lock}, strict-fold claimed {room_claimed}, unreadable {room_unreadable}")
    print(f"  rails offered {dict(rails)} | rails locked {dict(lock_rails)} | locks naming value rails (no rail holds value yet): {out['value_rail_locks']}")
    cp = out["counterparties"]
    print(f"  counterparties: {cp['distinct_dids_in_deals']} DIDs, {cp['pairs']} pairs, {cp['repeat_pairs_3plus']} pairs with >=3 deals ({cp['deals_in_repeat_pairs']} deals) | accept latency {cp['accept_latency']}")
    print("  implementation fingerprints (offer shape → offers / distinct senders):")
    for fp in out["fingerprints"]:
        print(f"    {fp['offers']:>5} offers / {fp['senders']:>4} senders  {fp['shape']}")
    print(f"  done in {out['seconds']}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest", help="check this port against the repo's golden vectors")
    a = sub.add_parser("audit", help="fold the live tclk-offers board")
    a.add_argument("--board", help="offline: path to a /r/tclk-offers/export JSONL dump")
    a.add_argument("--probe", type=int, default=40, help="derived deal rooms to read (newest N accepted contracts; 0 = skip)")
    a.add_argument("--json", help="write the full result as JSON")
    a.add_argument("--no-verify", action="store_true", help="skip Ed25519 record verification")
    args = ap.parse_args()
    if args.cmd == "selftest":
        return selftest()
    try:
        return audit(args.board, args.probe, args.json, not args.no_verify)
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
