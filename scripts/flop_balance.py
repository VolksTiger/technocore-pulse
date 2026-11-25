#!/usr/bin/env python3
"""flop_balance — an account's free/reserved/frozen FLOP straight from a Substrate RPC, stdlib only.

Reads `System.Account(<account>)` with state_getStorage and decodes the FRAME AccountInfo
(nonce u32, consumers u32, providers u32, sufficients u32, data { free u128, reserved u128,
frozen u128, flags u128 }). Storage key = twox128("System") ++ twox128("Account") ++
blake2_128(pubkey) ++ pubkey (Blake2_128Concat hasher). No CLI, no SDK, works whatever the
runtime spec is, so the spend loop can read balances while the CLI is behind the runtime.

  python3 scripts/flop_balance.py --rpc https://testnet.flop.network 5CY8LLGGzHiCHJnDpw3SgvjzY6AH8QMJQe6qSQC4Xq4SVwFP
  python3 scripts/flop_balance.py --rpc https://rpc.devnet.flop.network 5FVUcYMFnpqx3fwJy8srgM6CBJJGHiow3BpzLevk2CdSNXTW --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import urllib.request

UA = "technocore-pulse/flop-balance (read-only; github.com/VolksTiger/technocore-pulse)"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
P64_1, P64_2, P64_3, P64_4, P64_5 = 0x9E3779B185EBCA87, 0xC2B2AE3D27D4EB4F, 0x165667B19E3779F9, 0x85EBCA77C2B2AE63, 0x27D4EB2F165667C5
M64 = (1 << 64) - 1


def rotl64(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & M64


def xxh64(data: bytes, seed: int = 0) -> int:
    n = len(data)
    i = 0
    if n >= 32:
        v1 = (seed + P64_1 + P64_2) & M64
        v2 = (seed + P64_2) & M64
        v3 = seed & M64
        v4 = (seed - P64_1) & M64
        while i + 32 <= n:
            for k in range(4):
                lane = struct.unpack_from("<Q", data, i + 8 * k)[0]
                if k == 0:
                    v1 = (rotl64((v1 + lane * P64_2) & M64, 31) * P64_1) & M64
                elif k == 1:
                    v2 = (rotl64((v2 + lane * P64_2) & M64, 31) * P64_1) & M64
                elif k == 2:
                    v3 = (rotl64((v3 + lane * P64_2) & M64, 31) * P64_1) & M64
                else:
                    v4 = (rotl64((v4 + lane * P64_2) & M64, 31) * P64_1) & M64
            i += 32
        h = (rotl64(v1, 1) + rotl64(v2, 7) + rotl64(v3, 12) + rotl64(v4, 18)) & M64
        for v in (v1, v2, v3, v4):
            v = (rotl64((v * P64_2) & M64, 31) * P64_1) & M64
            h = ((h ^ v) * P64_1 + P64_4) & M64
    else:
        h = (seed + P64_5) & M64
    h = (h + n) & M64
    while i + 8 <= n:
        k1 = struct.unpack_from("<Q", data, i)[0]
        k1 = (rotl64((k1 * P64_2) & M64, 31) * P64_1) & M64
        h = (rotl64(h ^ k1, 27) * P64_1 + P64_4) & M64
        i += 8
    if i + 4 <= n:
        h ^= (struct.unpack_from("<I", data, i)[0] * P64_1) & M64
        h = (rotl64(h, 23) * P64_2 + P64_3) & M64
        i += 4
    while i < n:
        h ^= (data[i] * P64_5) & M64
        h = (rotl64(h, 11) * P64_1) & M64
        i += 1
    h ^= h >> 33
    h = (h * P64_2) & M64
    h ^= h >> 29
    h = (h * P64_3) & M64
    h ^= h >> 32
    return h


def twox128(data: bytes) -> bytes:
    return struct.pack("<Q", xxh64(data, 0)) + struct.pack("<Q", xxh64(data, 1))


def ss58_decode(address: str) -> bytes:
    """SS58 → 32-byte public key; checks the checksum."""
    num = 0
    for ch in address:
        num = num * 58 + B58.index(ch)
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big")
    # leading '1's encode leading zero bytes
    pad = len(address) - len(address.lstrip("1"))
    raw = b"\x00" * pad + raw
    if raw[0] & 0x40:  # two-byte prefix
        prefix_len = 2
    else:
        prefix_len = 1
    body, checksum = raw[:-2], raw[-2:]
    pub = body[prefix_len:]
    if len(pub) != 32:
        raise ValueError(f"not a 32-byte account: {address}")
    h = hashlib.blake2b(b"SS58PRE" + body, digest_size=64).digest()
    if h[:2] != checksum:
        raise ValueError(f"bad SS58 checksum: {address}")
    return pub


def rpc(url: str, method: str, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def account_info(url: str, address: str) -> dict:
    pub = ss58_decode(address)
    key = twox128(b"System") + twox128(b"Account") + hashlib.blake2b(pub, digest_size=16).digest() + pub
    raw = rpc(url, "state_getStorage", ["0x" + key.hex()])
    if raw is None:
        return {"address": address, "exists": False, "nonce": 0, "free": 0, "reserved": 0, "frozen": 0}
    b = bytes.fromhex(raw[2:])
    nonce, consumers, providers, sufficients = struct.unpack_from("<IIII", b, 0)
    free, reserved, frozen, flags = (int.from_bytes(b[16 + 16 * k:32 + 16 * k], "little") for k in range(4))
    return {"address": address, "exists": True, "nonce": nonce, "providers": providers,
            "free": free, "reserved": reserved, "frozen": frozen}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("address")
    ap.add_argument("--rpc", default="https://testnet.flop.network")
    ap.add_argument("--decimals", type=int, default=18)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    info = account_info(a.rpc, a.address)
    scale = 10 ** a.decimals
    info["free_flop"] = info["free"] / scale
    info["reserved_flop"] = info["reserved"] / scale
    if a.json:
        print(json.dumps(info))
    else:
        print(f"{a.address}: free {info['free_flop']:.6f} FLOP, reserved {info['reserved_flop']:.6f}, nonce {info['nonce']}, exists {info['exists']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
