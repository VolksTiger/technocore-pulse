#!/usr/bin/env python3
"""flop_faucet_claim — the FLOP Testnet faucet's signed agent claim (10 FLOP per wallet per 24 h).

Implements flop.network/llms.txt "Get Testnet FLOP" (25 Nov 2026), which no CLI release sends yet:
  1. GET <faucet>/chain must say network=testnet and carry the expected genesis hash
  2. GET <faucet>/limits must show faucet.status=open and faucet.agent.claimPath
  3. sign the UTF-8 text  flop-faucet-agent/v1|<genesis hash>|<address>|<nonce>  with the wallet's key
  4. POST <faucet>/v1/testnet/agent-claims {"address","nonce","signature":"0x…"}
  5. keep txHash; "included" is not final — spendable once finalized

Re-sending the same nonce returns the same transaction and never pays twice, so the nonce of a request
whose outcome is unknown is kept in --state and re-sent before any new nonce is used.

Key material: --keystore (the encrypted Polkadot JSON from `flop keys generate`, signed through
scripts/flop_sign.cjs with the password from $FLOP_KEY_PASSWORD), --seed-file (32-byte ed25519 seed hex,
0600), or --wallet (rehearsal JSON / encrypted PEM, as in flop_gateway.py). Never prints the key.

  python3 scripts/flop_faucet_claim.py --keystore /root/.flop/testnet-agent.json --address 5CY8… --json
  python3 scripts/flop_faucet_claim.py --seed-file … --address … --check-only     # limits + chain only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flop_address import ss58  # noqa: E402

UA = "technocore-pulse/flop-faucet-claim (github.com/VolksTiger/technocore-pulse)"
EXPECTED_GENESIS = "0x918abd58fb16306b30ef3886514cfaebffecb4de43e82b6f8ec4795c43cfd800"


def http(method: str, url: str, body=None, timeout: float = 60.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except json.JSONDecodeError:
            return e.code, {"error": raw[:400]}, dict(e.headers)


class KeystoreSigner:
    """Signs through scripts/flop_sign.cjs (the CLI's bundled @polkadot/keyring); the key stays in its
    encrypted Polkadot JSON file and the password is read from the environment by the node process."""

    def __init__(self, keystore: str, password_env: str):
        import subprocess  # noqa: PLC0415
        self.subprocess = subprocess
        self.keystore = keystore
        self.password_env = password_env
        self.signer = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flop_sign.cjs")
        probe = self._sign("flop-faucet-agent/v1|probe")
        self.address = probe["address"]
        self.public_hex = probe["publicKey"]

    def _sign(self, message: str) -> dict:
        p = self.subprocess.run(["node", self.signer, self.keystore, message, "--password-env", self.password_env],
                                capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise SystemExit(f"signer failed: {p.stderr.strip()[:300]}")
        return json.loads(p.stdout.strip().splitlines()[-1])

    def sign(self, data: bytes) -> bytes:
        return bytes.fromhex(self._sign(data.decode("utf-8"))["signature"][2:])


def load_key(a):
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: PLC0415
    if a.keystore:
        s = KeystoreSigner(a.keystore, a.password_env)
        return s, s.address
    if a.seed_file:
        seed = open(a.seed_file).read().strip()
        if seed.startswith("0x"):
            seed = seed[2:]
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
    else:
        from flop_gateway import load_wallet  # noqa: PLC0415
        _, key = load_wallet(a.wallet)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, ss58(pub, 42)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--faucet", default="https://faucet.flop.network")
    ap.add_argument("--network", default="testnet")
    ap.add_argument("--genesis", default=EXPECTED_GENESIS)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--seed-file")
    g.add_argument("--wallet")
    g.add_argument("--keystore", help="Polkadot JSON keystore from `flop keys generate`; signs via scripts/flop_sign.cjs")
    ap.add_argument("--password-env", default="FLOP_KEY_PASSWORD")
    ap.add_argument("--address", help="SS58 to fund; must be the key's own address (checked)")
    ap.add_argument("--state", default=os.path.expanduser("~/.technocore-pulse/faucet-claims.json"))
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--force-open", action="store_true", help="send even if faucet.status is not 'open'")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = {"faucet": a.faucet, "network": a.network}
    code, chain, _ = http("GET", f"{a.faucet}/chain")
    out["chain"] = chain
    if code != 200 or str(chain.get("network", "")).lower() != a.network or str(chain.get("genesisHash", "")).lower() != a.genesis.lower():
        out["error"] = f"faucet chain check failed: HTTP {code} network={chain.get('network')} genesis={str(chain.get('genesisHash'))[:14]}…"
        print(json.dumps(out, indent=1) if a.json else out["error"])
        return 2
    code, limits, _ = http("GET", f"{a.faucet}/limits")
    f = (limits or {}).get("faucet", {}) if isinstance(limits, dict) else {}
    agent = f.get("agent", {})
    out["limits"] = {"status": f.get("status"), "balanceFlop": f.get("balanceFlop"), "pendingGrants": f.get("pendingGrants"),
                     "agent": agent, "dailyCap": f.get("dailyCap"), "asOf": (limits or {}).get("asOf")}
    claim_path = agent.get("claimPath") or "/v1/testnet/agent-claims"
    if a.check_only:
        print(json.dumps(out, indent=1))
        return 0 if f.get("status") == "open" else 3
    if f.get("status") != "open" and not a.force_open:
        out["error"] = f"faucet.status is {f.get('status')!r}, not 'open' (use --force-open to try anyway)"
        print(json.dumps(out, indent=1) if a.json else out["error"])
        return 3

    key, derived = load_key(a)
    address = a.address or derived
    if address != derived:
        out["error"] = f"--address {address} is not this key's address ({derived})"
        print(json.dumps(out, indent=1) if a.json else out["error"])
        return 2
    out["address"] = address

    # idempotency: re-send an unresolved nonce before minting a new one
    state = {}
    if os.path.exists(a.state):
        try:
            state = json.load(open(a.state))
        except json.JSONDecodeError:
            state = {}
    pending = state.get(address, {}).get("pending_nonce")
    nonce = pending or str(int(time.time() * 1000))
    message = f"flop-faucet-agent/v1|{a.genesis}|{address}|{nonce}"
    sig = key.sign(message.encode("utf-8"))
    body = {"address": address, "nonce": nonce, "signature": "0x" + sig.hex()}
    code, res, hdrs = http("POST", f"{a.faucet}{claim_path}", body, timeout=90)
    out["claim"] = {"http": code, "nonce": nonce, "reused_nonce": bool(pending), "response": res,
                    "retry_after": hdrs.get("Retry-After")}

    final = code == 200 and isinstance(res, dict) and (res.get("success") or res.get("txHash") or res.get("status") in ("included", "finalized"))
    refused = code in (400, 401, 403, 404, 409, 422, 429) or (isinstance(res, dict) and res.get("reason") in ("cooldown", "cap", "refused"))
    entry = state.setdefault(address, {})
    if final:
        entry["last_nonce"] = nonce
        entry["last_tx"] = res.get("txHash")
        entry["last_at"] = int(time.time())
        entry.pop("pending_nonce", None)
    elif refused:
        entry.pop("pending_nonce", None)
    else:  # unknown outcome: keep the nonce and re-send it next time
        entry["pending_nonce"] = nonce
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    json.dump(state, open(a.state, "w"), indent=1)

    print(json.dumps(out, indent=1) if a.json else f"claim HTTP {code}: {json.dumps(res)[:300]}")
    return 0 if final else (4 if refused else 5)


if __name__ == "__main__":
    raise SystemExit(main())
