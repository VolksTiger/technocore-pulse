#!/usr/bin/env python3
"""flop_gateway — enrol a wallet with the FLOP public gateway and spend from its allowance.

Devnet rehearsal of the public-agent path (api.flop.network, "FLOP Public Inference Data Plane"):

  enroll   GET /v1/enroll/wallet/challenge?address=…  → sign the statement with the wallet's
           ed25519 key (raw bytes) → POST /v1/enroll/wallet → bearer key, saved to --key-file (0600)
  me       GET /v1/keys/me          (credential, remaining quota, expiry, scope)
  chat     POST /v1/chat/completions with one prompt, paid from the allowance
  receipts GET /v1/keys/me/receipts

Wallet: either a JSON file with `seed_hex` (32-byte ed25519 seed) and `address`, as written by the
rehearsal wallet generator, or the main identity's encrypted PEM (`~/dev/technocore-did/identity.pem`):
the passphrase is read from $FLOP_KEY_PASSWORD or asked for on the terminal, the key never leaves the
process and the SS58 address is derived from the public key (prefix 42). Needs `cryptography`;
everything else is stdlib. Never prints the bearer.

Networks: `--gateway`, `--faucet` and `--rpc` default to the devnet endpoints; pass the testnet ones
(or `--net devnet/net-testnet.json` written by scripts/flop_net.py) once they are published.

  python3 scripts/flop_gateway.py whoami --wallet ~/dev/technocore-did/identity.pem
  python3 scripts/flop_gateway.py fund   --wallet ~/dev/technocore-did/identity.pem [--faucet URL]
  python3 scripts/flop_gateway.py enroll --wallet ~/dev/technocore-did/identity.pem --key-file devnet/gateway-key.json
  python3 scripts/flop_gateway.py me --key-file devnet/gateway-key.json
  python3 scripts/flop_gateway.py chat --key-file devnet/gateway-key.json --prompt "Say hello." --max-tokens 32
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flop_address import ss58  # noqa: E402

DEFAULTS = {
    "gateway": "https://api.flop.network/v1",
    "faucet": "https://faucet.flop.network",
    "rpc": "https://rpc.devnet.flop.network",
    "ss58_prefix": 42,
}
BASE = DEFAULTS["gateway"]
FAUCET = DEFAULTS["faucet"]
RPC = DEFAULTS["rpc"]
UA = "technocore-pulse/flop-gateway (github.com/VolksTiger/technocore-pulse)"


def http(method: str, path: str, body=None, bearer: str | None = None, timeout: float = 60.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"error": raw[:500]}
        retry = e.headers.get("Retry-After")
        if retry and isinstance(body, dict):
            body["retry_after"] = retry
        return e.code, body


def load_wallet(path: str, prefix: int = DEFAULTS["ss58_prefix"]):
    """(address, key) from a rehearsal JSON wallet or an encrypted/plain Ed25519 PEM."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415
    raw = open(path, "rb").read()
    if raw.lstrip().startswith(b"-----BEGIN"):
        password = None
        if b"ENCRYPTED" in raw:
            password = (os.environ.get("FLOP_KEY_PASSWORD") or getpass.getpass("identity passphrase: ")).encode("utf-8")
        key = serialization.load_pem_private_key(raw, password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit("wallet: PEM is not an Ed25519 key")
    else:
        w = json.loads(raw)
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(w["seed_hex"]))
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return ss58(pub, prefix), key


def apply_net(a) -> None:
    """Resolve endpoint overrides: --net JSON file (from flop_net.py), then explicit flags."""
    global BASE, FAUCET, RPC
    net = {}
    if getattr(a, "net", None):
        net = json.load(open(a.net))
    BASE = getattr(a, "gateway", None) or net.get("gateway") or BASE
    FAUCET = getattr(a, "faucet", None) or net.get("faucet") or FAUCET
    RPC = getattr(a, "rpc", None) or net.get("rpc") or RPC
    if BASE.endswith("/"):
        BASE = BASE[:-1]
    if not BASE.endswith("/v1"):
        BASE = BASE + "/v1"


def cmd_whoami(a) -> int:
    address, _ = load_wallet(a.wallet)
    print(address)
    code, res = http("GET", f"{RPC_EXPLORER(RPC)}/api/accounts/{address}") if RPC_EXPLORER(RPC) else (0, {})
    if code == 200:
        print("explorer: " + json.dumps(res)[:300])
    return 0


def RPC_EXPLORER(rpc: str):
    # rpc.devnet.flop.network → explorer.devnet.flop.network (same naming on testnet, presumably)
    if "rpc." in rpc:
        return rpc.replace("rpc.", "explorer.").rstrip("/")
    return None


def cmd_fund(a) -> int:
    address, _ = load_wallet(a.wallet)
    code, res = http("POST", f"{FAUCET}/fund", {"address": address, "client": "technocore-pulse"}, timeout=90)
    print(f"faucet {FAUCET}/fund for {address}: HTTP {code} " + json.dumps(res)[:500])
    return 0 if code == 200 else 1


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
        if code == 503 and isinstance(res, dict) and res.get("error") == "wallet_enrollment_cap_reached":
            print("enroll: the gateway's daily cap on new wallet enrollments is used up; the signed statement was "
                  "accepted as far as that gate. Re-run later (Retry-After above if the gateway sent one) or open an "
                  "access request in flop-labs/network with the address and this error text.", file=sys.stderr)
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
    ap.add_argument("--net", default=None, help="endpoint JSON from scripts/flop_net.py (gateway/faucet/rpc)")
    ap.add_argument("--gateway", default=None)
    ap.add_argument("--faucet", default=None)
    ap.add_argument("--rpc", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("whoami")
    w.add_argument("--wallet", required=True)
    w.set_defaults(fn=cmd_whoami)
    f = sub.add_parser("fund")
    f.add_argument("--wallet", required=True)
    f.set_defaults(fn=cmd_fund)
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
    apply_net(a)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
