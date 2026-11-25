#!/usr/bin/env python3
"""flop_net — discover a FLOP network's public endpoints and write them as one JSON config.

Reads (all read-only, stdlib):
  * docs.flop.network/networks/<name>   the operator hub's network card: chain spec, genesis, RPC,
                                        gateway, admission — every row says "Unavailable" until published
  * <faucet>/chain                      network name, genesis hash, ss58 prefix, decimals, RPC URL
  * <rpc> system_chain / system_properties / state_getRuntimeVersion / chain_getBlockHash(0)
and checks that the faucet's genesis equals the RPC's, as the docs ask before funding anything.

  python3 scripts/flop_net.py devnet
  python3 scripts/flop_net.py testnet --write devnet/net-testnet.json
  python3 scripts/flop_net.py testnet --rpc https://rpc.testnet.flop.network --faucet https://faucet.testnet.flop.network

Exit codes: 0 RPC answers and faucet genesis agrees; 2 RPC unreachable; 3 RPC live but no faucet yet;
4 faucet and RPC genesis differ (never fund); 5 RPC answers 429 (live, saturated).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request

UA = "technocore-pulse/flop-net (read-only; github.com/VolksTiger/technocore-pulse)"
GUESS = {
    # 25 Nov 2026: faucet.flop.network serves the TESTNET; the devnet faucet moved to faucet.devnet.flop.network
    "devnet": {"rpc": "https://rpc.devnet.flop.network", "faucet": "https://faucet.devnet.flop.network",
               "gateway": "https://api.flop.network", "explorer": "https://explorer.devnet.flop.network"},
    "testnet": {"rpc": "https://testnet.flop.network", "faucet": "https://faucet.flop.network",
                "gateway": None, "explorer": "https://testnet.flopscan.io"},
}


def get(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def rpc(url: str, method: str, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read().decode())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def to_text(raw: str) -> str:
    t = re.sub(r"<(script|style|nav|header)\b.*?</\1>", "", raw, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t))


def docs_card(name: str) -> dict:
    """The hub's network card as {row: value}; values are 'Unavailable…' until published."""
    out = {"source": f"https://docs.flop.network/networks/{name}"}
    try:
        _, raw = get(out["source"])
    except (urllib.error.URLError, OSError) as exc:
        out["error"] = str(exc)[:200]
        return out
    text = to_text(raw)
    m = re.search(r"Network: (\w+) \(([^)]+)\)", text)
    if m:
        out["network"], out["phase"] = m.group(1), m.group(2)
    for row in ("Chain spec", "Expected genesis", "Bootnodes", "RPC", "Gateway", "Admission", "Model catalog"):
        mm = re.search(re.escape(row) + r" (.*?)(?= Chain spec | Expected genesis | Bootnodes | RPC | Gateway | Admission | Model catalog | Supported releases |$)", text)
        if mm:
            out[row.lower().replace(" ", "_")] = mm.group(1).strip()[:160]
    out["urls"] = sorted(set(re.findall(r"(?:wss|https)://[a-zA-Z0-9./_-]+", text)))
    out["unavailable_rows"] = text.count("Unavailable")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("network", choices=sorted(GUESS))
    ap.add_argument("--rpc")
    ap.add_argument("--faucet")
    ap.add_argument("--gateway")
    ap.add_argument("--explorer")
    ap.add_argument("--write", help="write the resolved config JSON here")
    a = ap.parse_args()
    cfg = dict(GUESS[a.network])
    for k in ("rpc", "faucet", "gateway", "explorer"):
        if getattr(a, k):
            cfg[k] = getattr(a, k)
    cfg["network"] = a.network
    cfg["docs"] = docs_card(a.network)
    # the hub may name the real endpoints before our guesses resolve
    for u in cfg["docs"].get("urls", []):
        if "faucet" in u:
            cfg["faucet"] = u.rstrip("/")
        elif u.startswith("https://api.") or "gateway" in u:
            cfg["gateway"] = u.rstrip("/")
        elif u.startswith("wss://"):
            cfg["rpc"] = u.replace("wss://", "https://").rstrip("/") or cfg["rpc"]
        elif u.startswith("https://") and "rpc" in u:
            cfg["rpc"] = u.rstrip("/")
    published = cfg["docs"].get("unavailable_rows", 99) == 0 and bool(cfg["docs"].get("urls"))
    cfg["published_on_docs"] = published

    try:
        _, raw = get(f"{cfg['faucet']}/chain")
        cfg["faucet_chain"] = json.loads(raw)
        fnet = str(cfg["faucet_chain"].get("network", "")).lower()
        if fnet and fnet != a.network:
            cfg["faucet_chain"] = {"error": f"faucet at {cfg['faucet']} serves {fnet}, not {a.network}", "served": cfg["faucet_chain"]}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        cfg["faucet_chain"] = {"error": str(exc)[:200]}

    try:
        cfg["chain"] = {
            "name": rpc(cfg["rpc"], "system_chain"),
            "properties": rpc(cfg["rpc"], "system_properties"),
            "runtime": {k: v for k, v in rpc(cfg["rpc"], "state_getRuntimeVersion").items() if k in ("specName", "specVersion", "transactionVersion")},
            "genesis": rpc(cfg["rpc"], "chain_getBlockHash", [0]),
            "head": int(rpc(cfg["rpc"], "chain_getHeader")["number"], 16),
        }
    except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as exc:
        cfg["chain"] = {"error": str(exc)[:200]}

    fg = cfg["faucet_chain"].get("genesisHash")
    rg = cfg.get("chain", {}).get("genesis")
    cfg["genesis_match"] = bool(fg and rg and fg.lower() == rg.lower())
    if cfg.get("chain", {}).get("properties"):
        cfg["ss58_prefix"] = cfg["chain"]["properties"].get("ss58Format")

    print(json.dumps(cfg, indent=1, ensure_ascii=False))
    if a.write:
        with open(a.write, "w") as f:
            json.dump(cfg, f, indent=1, ensure_ascii=False)
        print(f"written: {a.write}", file=sys.stderr)
    if "error" in cfg.get("chain", {}):
        if "429" in cfg["chain"]["error"] or "Too many" in cfg["chain"]["error"]:
            print(f"{a.network}: RPC live but rate-limited right now ({cfg['chain']['error'][:80]})", file=sys.stderr)
            return 5
        print(f"{a.network}: RPC not reachable yet ({cfg['chain']['error']})", file=sys.stderr)
        return 2
    if "error" in cfg.get("faucet_chain", {}):
        print(f"{a.network}: RPC live (genesis {cfg['chain']['genesis'][:12]}…), faucet not published at {cfg['faucet']} — chain reads only", file=sys.stderr)
        return 3
    if not cfg["genesis_match"]:
        print(f"{a.network}: faucet and RPC genesis DIFFER — do not fund", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
