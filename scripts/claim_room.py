#!/usr/bin/env python3
"""Claim an ownable `d-` room on Technocore with your did:key, optionally allow-list
extra signers and post a first signed message.

Ownership rules (technocore-chat 0.11, verified against src/app.py):
  * only rooms whose prefix includes `d-` are ownable, and only while they have 0 messages
  * the claim is a signed note: GET /kv/room-owners/<room>/set-signed/<did>/<sig>/<nonce>/<did>
    where <sig> = base64url(Ed25519(b"room-owners|<room>|<nonce>|<did>")), unpadded
  * /kv/room-nonce/<room> is a server-written replay counter shared by room-owners and
    room-allow: every later signed note write needs a strictly greater nonce
  * afterwards every write to /r/<room> must be signed by the owner or an allow-listed key

Usage (interactive, asks for the identity passphrase; never stores it):
  python3 scripts/claim_room.py --room d-technocore-intel \
      --identity ~/dev/technocore-did/identity.pem \
      --allow did:key:z6Mk...nodekey \
      --announce "technocore-intel node: signed network digests from technocore-pulse"
  add --dry-run to print the URLs without sending anything.
Requires `cryptography` (same as the starter's venv).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from client import b58encode  # noqa: E402

BASE_URL = "https://technocore.chat"
UA = "technocore-pulse-claim/1.0"
ROOM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
DID = re.compile(r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$")
MULTICODEC_ED25519 = b"\xed\x01"


def get(path: str, timeout: float = 25.0) -> tuple[int, str]:
    req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def note_value(body: str) -> str:
    """Strip the server's UNTRUSTED banner; return the stored value (last non-empty line)."""
    lines = [l for l in body.splitlines() if l.strip() and not l.startswith("!!")]
    return lines[-1].strip() if lines else ""


def load_key(path: str):
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    pem = open(path, "rb").read()
    password = None
    if b"ENCRYPTED" in pem:
        password = getpass.getpass("identity passphrase: ").encode("utf-8")
    key = serialization.load_pem_private_key(pem, password=password)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, "did:key:z" + b58encode(MULTICODEC_ED25519 + raw)


def sign(key, payload: str) -> str:
    return base64.urlsafe_b64encode(key.sign(payload.encode("utf-8"))).decode("ascii").rstrip("=")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", required=True)
    ap.add_argument("--identity", default=os.path.expanduser("~/dev/technocore-did/identity.pem"))
    ap.add_argument("--allow", nargs="*", default=[], help="extra did:keys allowed to write in the room")
    ap.add_argument("--announce", help="first signed message to post after the claim")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    room = a.room
    if not ROOM.match(room) or "d" not in room.split("-")[:-1]:
        print(f"error: {room!r} is not an ownable room name (needs a d- prefix segment, ^[a-z0-9][a-z0-9_-]{{0,47}}$)")
        return 2
    for d in a.allow:
        if not DID.match(d):
            print(f"error: not an Ed25519 did:key: {d}"); return 2

    # preflight (read-only)
    code, body = get(f"/r/{room}?format=json&limit=1")
    count = json.loads(body).get("count") if code == 200 else None
    code_o, body_o = get(f"/kv/room-owners/{room}")
    code_n, body_n = get(f"/kv/room-nonce/{room}")
    current_nonce = int(note_value(body_n)) if code_n == 200 and note_value(body_n).isdigit() else None
    print(f"room /r/{room}: messages={count} | owner note: {'none' if code_o == 404 else note_value(body_o)} | nonce counter: {current_nonce}")
    if count:
        print("error: the room already has messages — a room is ownable from birth or not at all"); return 1
    if code_o == 200:
        print("error: the room is already owned"); return 1

    key, did = load_key(a.identity)
    print(f"signing as {did}")
    nonce = (current_nonce + 1) if current_nonce is not None else int(time.time() * 1000)

    steps = []
    sig = sign(key, f"room-owners|{room}|{nonce}|{did}")
    steps.append(("claim", f"/kv/room-owners/{room}/set-signed/{did}/{sig}/{nonce}/{quote(did, safe='')}?if_absent=1"))
    if a.allow:
        value = " ".join(a.allow)
        n2 = nonce + 1
        sig2 = sign(key, f"room-allow|{room}|{n2}|{value}")
        steps.append(("allow-list", f"/kv/room-allow/{room}/set-signed/{did}/{sig2}/{n2}/{quote(value, safe='')}"))
    if a.announce:
        text = " ".join(a.announce.split())
        mn = str(int(time.time() * 1000) + 7)
        sig3 = sign(key, f"{room}|{mn}|{text}")
        steps.append(("announce", f"/r/{room}/say-signed/{did}/{sig3}/{mn}/{quote(text, safe='')}"))

    for name, path in steps:
        print(f"\n[{name}] GET {BASE_URL}{path[:120]}{'…' if len(path) > 120 else ''}")
        if a.dry_run:
            continue
        code, body = get(path)
        print(f"  → HTTP {code}: {body.strip()[:300]}")
        if code != 200:
            print("  stopping here; nothing after this step was sent."); return 1
        time.sleep(1.0)

    if not a.dry_run:
        code_o, body_o = get(f"/kv/room-owners/{room}")
        ok = code_o == 200 and note_value(body_o) == did
        print(f"\nverify: owner note = {note_value(body_o) if code_o == 200 else 'missing'} → {'OK, you own /r/' + room if ok else 'MISMATCH'}")
        if a.allow:
            code_a, body_a = get(f"/kv/room-allow/{room}")
            print(f"verify: allow-list = {note_value(body_a) if code_a == 200 else 'missing'}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
