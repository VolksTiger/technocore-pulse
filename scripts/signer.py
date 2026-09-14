#!/usr/bin/env python3
"""signer — one passphrase, many signed posts: a local outbox the operator's agent fills and this
process signs and posts with the main did:key (POST lane, single-line text).

  python3 scripts/signer.py            # loads the key once, then watches ~/.technocore-pulse/outbox.jsonl

Outbox line:  {"id": "<unique>", "room": "<room>", "text": "<one line>"}
Result line (outbox.done.jsonl): the same plus {"seq", "http", "posted_at"} or {"error"}.
The key never leaves this process; the outbox is a plain file only the operator's tools write.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from deal import load_key, log, now_ms, BASE_URL, UA  # noqa: E402

OUTBOX = os.path.expanduser("~/.technocore-pulse/outbox.jsonl")
DONE = os.path.expanduser("~/.technocore-pulse/outbox.done.jsonl")


def post_signed(key, did: str, room: str, text: str) -> tuple[int, str]:
    nonce = str(now_ms())
    sig = base64.urlsafe_b64encode(key.sign(f"{room}|{nonce}|{text}".encode("utf-8"))).decode("ascii").rstrip("=")
    payload = json.dumps({"did": did, "sig": sig, "nonce": nonce, "text": text}).encode("utf-8")
    req = urllib.request.Request(f"{BASE_URL}/r/{room}?format=json", data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def main() -> int:
    identity = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/dev/technocore-did/identity.pem")
    key, did = load_key(identity)
    log(f"signer up as {did}; watching {OUTBOX} (Ctrl+C to stop)")
    os.makedirs(os.path.dirname(OUTBOX), exist_ok=True)
    open(OUTBOX, "a").close()
    done_ids = set()
    if os.path.exists(DONE):
        for line in open(DONE, encoding="utf-8"):
            try:
                done_ids.add(json.loads(line)["id"])
            except Exception:  # noqa: BLE001
                pass
    while True:
        try:
            for line in open(OUTBOX, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                iid = item.get("id")
                if not iid or iid in done_ids:
                    continue
                room, text = item["room"], " ".join(str(item["text"]).split())
                if len(text) > 4096:
                    res = dict(item, error=f"{len(text)} chars > 4096")
                else:
                    code, body = post_signed(key, did, room, text)
                    seq = None
                    try:
                        seq = next((m.get("seq") for m in json.loads(body).get("messages", []) if m.get("from") == did and (m.get("text") or "")[:80] == text[:80]), None)
                    except Exception:  # noqa: BLE001
                        pass
                    res = dict(item, http=code, seq=seq, posted_at=now_ms(), **({} if code == 200 else {"error": body.strip()[:200]}))
                    log(f"{'posted' if code == 200 else 'FAILED ' + str(code)} {iid} → /r/{room} seq {seq}: {text[:90]}")
                done_ids.add(iid)
                with open(DONE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(res, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            log(f"outbox pass failed: {e}")
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
