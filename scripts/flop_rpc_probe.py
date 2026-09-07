#!/usr/bin/env python3
"""flop_rpc_probe — first contact with a FLOP (Substrate/FRAME) node the moment an RPC URL exists.

Stdlib JSON-RPC over HTTP. Read-only: it never submits an extrinsic. Prints what an agent
client needs before anything else — chain name, SS58 prefix (system_properties.ss58Format),
token decimals, runtime version, whether the agent pallets are in the metadata, and the
account state of our did:key-derived address.

    python3 scripts/flop_rpc_probe.py --url https://rpc.testnet.flop.finance
    python3 scripts/flop_rpc_probe.py --url http://127.0.0.1:9944 --did did:key:z6Mk...
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flop_address import did_pubkey, ss58  # noqa: E402

AGENT_PALLETS = ["AgentIdentity", "AgentWallet", "SessionKeys", "ComputeChannel", "ModelRegistry",
                 "TokenClaim", "AirdropVesting", "FlopPoui", "HasStation"]


def rpc(url: str, method: str, params=None, timeout: float = 30.0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": "technocore-pulse-flop-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode("utf-8", "replace"))
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out.get("result")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="HTTP JSON-RPC endpoint")
    ap.add_argument("--did", default="did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV")
    a = ap.parse_args()

    chain = rpc(a.url, "system_chain")
    version = rpc(a.url, "system_version")
    props = rpc(a.url, "system_properties") or {}
    rt = rpc(a.url, "state_getRuntimeVersion") or {}
    head = rpc(a.url, "chain_getHeader") or {}
    print(f"chain:        {chain}  (node {version})")
    print(f"runtime:      {rt.get('specName')} spec {rt.get('specVersion')} tx {rt.get('transactionVersion')}")
    print(f"head:         #{int(head.get('number', '0x0'), 16)}")
    print(f"properties:   {json.dumps(props)}")
    prefix = props.get("ss58Format")
    if prefix is None:
        print("ss58Format:   not advertised — pass the prefix from the chain spec to flop_address.py")
    else:
        addr = ss58(did_pubkey(a.did), int(prefix))
        print(f"our account:  {addr}  (did {a.did[:24]}…, prefix {prefix})")
        try:
            # System.Account storage key: twox128("System") ++ twox128("Account") ++ blake2_128_concat(AccountId)
            print("balance:      needs SCALE decoding of System.Account — use py-substrate-interface once the chain spec is public")
        except Exception as exc:  # noqa: BLE001
            print(f"balance:      {exc}")
    methods = rpc(a.url, "rpc_methods") or {}
    names = methods.get("methods", [])
    print(f"rpc methods:  {len(names)} ({', '.join(n for n in names if n.startswith(('author_', 'compute', 'flop', 'agent')))[:200]})")
    meta = rpc(a.url, "state_getMetadata")
    if isinstance(meta, str):
        raw = bytes.fromhex(meta[2:])
        found = [p for p in AGENT_PALLETS if p.encode() in raw]
        print(f"metadata:     {len(raw)} bytes, sha256 {hashlib.sha256(raw).hexdigest()[:16]}; agent pallets present: {found or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
