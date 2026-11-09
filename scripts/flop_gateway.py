#!/usr/bin/env python3
"""flop_gateway — enrol a wallet with the FLOP public gateway and spend from its allowance.

Devnet rehearsal of the public-agent path (api.flop.network, "FLOP Public Inference Data Plane"):

  enroll   GET /v1/enroll/wallet/challenge?address=…  → sign the statement with the wallet's
           ed25519 key (raw bytes) → POST /v1/enroll/wallet → bearer key, saved to --key-file (0600)
  me       GET /v1/keys/me          (credential, remaining quota, expiry, scope)
  chat     POST /v1/chat/completions with one prompt, paid from the allowance
  receipts GET /v1/keys/me/receipts

Wallet file: JSON with `seed_hex` (32-byte ed25519 seed) and `address` (SS58), as written by the
rehearsal wallet generator; a wallet derived from the main did:key works the same way once its seed
is available. Needs `cryptography`; everything else is stdlib. Never prints the bearer.

  python3 scripts/flop_gateway.py enroll --wallet devnet/wallet-devnet.json --key-file devnet/gateway-key.json
  python3 scripts/flop_gateway.py me --key-file devnet/gateway-key.json
  python3 scripts/flop_gateway.py chat --key-file devnet/gateway-key.json --prompt "Say hello." --max-tokens 32
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.flop.network/v1"
UA = "technocore-pulse/flop-gateway (github.com/VolksTiger/technocore-pulse)"


def http(method: str, path: str, body=None, bearer: str | None = None, timeout: float = 60.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": raw[:500]}


def load_wallet(path: str):
    w = json.load(open(path))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(w["seed_hex"]))
    return w["address"], key


def load_key(path: str) -> dict:
    return json.load(open(path))


def cmd_enroll(a) -> int:
    address, key = load_wallet(a.wallet)
    q = f"/enroll/wallet/challenge?address={address}" + (f"&purpose={a.purpose}" if a.purpose else "")
    code, ch = http("GET", q)
    if code != 200:
        print(f"challenge: HTTP {code} {json.dumps(ch)[:400]}", file=sys.stderr)
        return 1
    message = ch["message"]
    print("challenge statement:\n" + "\n".join("  | " + l for l in message.split("\n")))
    sig = key.sign(message.encode("utf-8"))
    code, res = http("POST", "/enroll/wallet", {"address": address, "message": message, "signature": "0x" + sig.hex()})
    if code not in (200, 201):
        print(f"enroll: HTTP {code} {json.dumps(res)[:600]}", file=sys.stderr)
        return 1
    bearer = None
    for k in ("api_key", "key", "bearer", "token", "apiKey"):
        if isinstance(res.get(k), str):
            bearer = res.pop(k)
            break
    if bearer is None:
        print("enroll: response had no key field; fields = " + ", ".join(sorted(res)), file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(a.key_file) or ".", exist_ok=True)
    with open(os.open(a.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"address": address, "bearer": bearer, "enrolled_at": int(time.time()), "response": res}, f, indent=1)
    print(f"enrolled: HTTP {code}; bearer saved to {a.key_file} (not shown)")
    print("response (without key): " + json.dumps(res)[:900])
    return 0


def cmd_me(a) -> int:
    code, res = http("GET", "/keys/me", bearer=load_key(a.key_file)["bearer"])
    print(f"HTTP {code} " + json.dumps(res)[:900])
    return 0 if code == 200 else 1


def cmd_receipts(a) -> int:
    code, res = http("GET", "/keys/me/receipts", bearer=load_key(a.key_file)["bearer"])
    print(f"HTTP {code} " + json.dumps(res)[:1500])
    return 0 if code == 200 else 1


def cmd_chat(a) -> int:
    k = load_key(a.key_file)
    model = a.model or (k.get("response", {}).get("model")) or "qwen/qwen2.5-0.5b-instruct"
    body = {"model": model, "messages": [{"role": "user", "content": a.prompt}], "max_tokens": a.max_tokens}
    t0 = time.time()
    code, res = http("POST", "/chat/completions", body, bearer=k["bearer"], timeout=180)
    dt = time.time() - t0
    print(f"HTTP {code} in {dt:.1f}s model={model}")
    if code == 200:
        choice = (res.get("choices") or [{}])[0]
        print("reply: " + json.dumps(choice.get("message", {}).get("content"))[:600])
        print("usage: " + json.dumps(res.get("usage")))
        extra = {kk: v for kk, v in res.items() if kk.startswith("x-") or kk in ("id", "provider", "miner", "settlement", "receipt", "cost")}
        if extra:
            print("extra: " + json.dumps(extra)[:900])
    else:
        print(json.dumps(res)[:900])
    return 0 if code == 200 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll")
    e.add_argument("--wallet", required=True)
    e.add_argument("--key-file", required=True)
    e.add_argument("--purpose", default=None, help="e.g. hermes-tools-v1 for the v2 statement")
    e.set_defaults(fn=cmd_enroll)
    m = sub.add_parser("me")
    m.add_argument("--key-file", required=True)
    m.set_defaults(fn=cmd_me)
    r = sub.add_parser("receipts")
    r.add_argument("--key-file", required=True)
    r.set_defaults(fn=cmd_receipts)
    c = sub.add_parser("chat")
    c.add_argument("--key-file", required=True)
    c.add_argument("--prompt", required=True)
    c.add_argument("--model", default=None)
    c.add_argument("--max-tokens", type=int, default=32)
    c.set_defaults(fn=cmd_chat)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
