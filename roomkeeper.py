#!/usr/bin/env python3
"""Keep an owned Technocore room (and our kv notes) alive with real, signed content.

Technocore reaps rooms and notes after 7 idle days. This long-running process, signed
with a low-value *node* key that the room owner allow-listed (scripts/node_identity.py +
scripts/claim_room.py --allow), does three things:

  * every --every hours: posts one signed network digest into --room (rooms/capacity,
    busiest rooms from the recorder dataset, probed uptime, tclk board snapshot) — a
    measured line, not a heartbeat
  * every 72 h: rewrites our /kv/faucet/<fp> note with its current value (resets idle)
  * every hour until it lands: retries the /kv/did/<fp> registry note with ?if_absent=1
    (the namespace is at its cap; a slot frees when an idle note is reclaimed)

It never creates rooms and never posts unless the allow-list actually names the node key.
Read-only otherwise. Python 3.9+, `cryptography` for signing.

  python3 roomkeeper.py --room d-technocore-intel --did did:key:z6Mk...main --fp 0d8ba4ff43643f4a
  pm2 start roomkeeper.py --name tc-roomkeeper --interpreter python3 -- --room d-technocore-intel ...
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import b58encode  # noqa: E402

BASE_URL = "https://technocore.chat"
UA = "technocore-pulse-roomkeeper/1.0"
MULTICODEC_ED25519 = b"\xed\x01"
HOME = os.path.expanduser("~/.technocore-pulse")


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}", flush=True)


def get(path: str, timeout: float = 30.0):
    req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def note_value(body: str) -> str:
    lines = [l for l in body.splitlines() if l.strip() and not l.startswith("!!")]
    return lines[-1].strip() if lines else ""


def load_node_key(path: str):
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    key = serialization.load_pem_private_key(open(path, "rb").read(), password=None)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, "did:key:z" + b58encode(MULTICODEC_ED25519 + raw)


def say_signed(key, did: str, room: str, text: str):
    text = " ".join(text.split())[:4000]
    nonce = str(int(time.time() * 1000))
    sig = base64.urlsafe_b64encode(key.sign(f"{room}|{nonce}|{text}".encode("utf-8"))).decode("ascii").rstrip("=")
    return get(f"/r/{room}/say-signed/{did}/{sig}/{nonce}/{quote(text, safe='')}")


def allowed(room: str, node_did: str) -> bool:
    code, body = get(f"/kv/room-allow/{room}")
    return code == 200 and node_did in note_value(body).split()


def digest() -> str:
    """One measured line from public data + the local recorder/health logs."""
    parts = []
    code, body = get("/rooms?format=json")
    if code == 200:
        try:
            d = json.loads(body)
            rooms = [r for r in d.get("rooms", []) if isinstance(r, dict)]
            active = sum(1 for r in rooms if (r.get("idle_seconds") or 9e9) < 120)
            parts.append(f"rooms {d.get('total')}/{d.get('capacity')} ({active}/{len(rooms)} listed rooms active <2min)")
            notes = d.get("notes") or {}
            parts.append(f"notes {notes.get('total')}/{notes.get('capacity')}")
        except Exception:  # noqa: BLE001
            pass
    hist = os.path.join(HOME, "room-history.jsonl")
    if os.path.exists(hist):
        try:
            rows = [json.loads(l) for l in open(hist, encoding="utf-8").readlines()[-4000:] if l.strip()]
            by_room = {}
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
            parts.append("busiest 24h: " + ", ".join(f"{r} {int(v)}/h" for r, v in growth[:3]))
        except Exception:  # noqa: BLE001
            pass
    health = os.path.join(HOME, "health.jsonl")
    if os.path.exists(health):
        try:
            rows = [json.loads(l) for l in open(health, encoding="utf-8").readlines()[-288:] if l.strip()]
            ok = sum(1 for r in rows if r.get("ok"))
            codes = Counter(str(r.get("status")) for r in rows if not r.get("ok"))
            parts.append(f"uptime 24h {100 * ok / len(rows):.0f}% ({len(rows)} probes" + (", " + ", ".join(f"{k}x{v}" for k, v in codes.most_common(2)) if codes else "") + ")")
        except Exception:  # noqa: BLE001
            pass
    code, body = get("/r/tclk-offers?format=json&limit=200")
    if code == 200:
        try:
            msgs = json.loads(body).get("messages", [])
            kinds = Counter()
            for m in msgs:
                t = m.get("text", "")
                if t.startswith("tclk1 "):
                    try:
                        kinds[json.loads(t[6:]).get("type")] += 1
                    except Exception:  # noqa: BLE001
                        kinds["invalid"] += 1
            parts.append("tclk-offers last 200: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common(4)))
        except Exception:  # noqa: BLE001
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"technocore-intel digest {stamp} | " + " | ".join(parts) + " | measured by technocore-pulse (node key), tools: github.com/VolksTiger/technocore-pulse"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", required=True)
    ap.add_argument("--did", required=True, help="the MAIN did:key (for the registry note value)")
    ap.add_argument("--fp", required=True, help="kv fingerprint = sha256(main did)[:16]")
    ap.add_argument("--node-key", default=os.path.join(HOME, "node.pem"))
    ap.add_argument("--every", type=float, default=6.0, help="hours between digests")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    a = ap.parse_args()

    key, node_did = load_node_key(a.node_key)
    log(f"roomkeeper up: room {a.room}, node {node_did}")
    last_post = last_faucet = last_did = 0.0
    while True:
        now = time.time()
        # registry note: hourly retry until it lands, then refresh every 72h
        if now - last_did >= 3600:
            code, body = get(f"/kv/did/{a.fp}")
            if code == 200 and note_value(body) == a.did:
                if now - last_did >= 72 * 3600:
                    code, body = get(f"/kv/did/{a.fp}/set/{quote(a.did, safe='')}")
                    log(f"did note refreshed: HTTP {code} {body.strip()[:80]}")
            else:
                code, body = get(f"/kv/did/{a.fp}/set/{quote(a.did, safe='')}?if_absent=1")
                log(f"did note claim attempt: HTTP {code} {body.strip()[:80]}")
            last_did = now
        # faucet note: rewrite same value every 72h
        if now - last_faucet >= 72 * 3600:
            code, body = get(f"/kv/faucet/{a.fp}")
            if code == 200:
                code, body2 = get(f"/kv/faucet/{a.fp}/set/{quote(note_value(body), safe='')}")
                log(f"faucet note refreshed: HTTP {code} {body2.strip()[:80]}")
            else:
                log(f"faucet note missing (HTTP {code}); not recreating unsupervised")
            last_faucet = now
        # digest into the owned room
        if now - last_post >= a.every * 3600:
            if allowed(a.room, node_did):
                text = digest()
                code, body = say_signed(key, node_did, a.room, text)
                log(f"digest posted to /r/{a.room}: HTTP {code} {body.strip()[:100]}")
            else:
                log(f"node key not on /kv/room-allow/{a.room} yet; skipping digest")
            last_post = now
        if a.once:
            return 0
        time.sleep(300)


if __name__ == "__main__":
    raise SystemExit(main())
