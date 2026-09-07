#!/usr/bin/env python3
"""flop_address — what our did:key would be as a FLOP/Substrate account (SS58), for testnet day one.

The FLOP yellow paper (v0.5.0 draft) says the chain is Substrate-lineage (SS58 addresses,
`SS58Prefix` fixed at genesis, ed25519 accepted next to the sr25519 default). The prefix is
not public yet, so this prints the address for any prefix you pass; re-run when the chain
spec lands. Read-only, stdlib only.

    python3 scripts/flop_address.py --did did:key:z6Mk... --prefix 42
"""
import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from client import b58encode  # noqa: E402

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + ALPHABET.index(ch)
    out = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + out


def did_pubkey(did: str) -> bytes:
    if not did.startswith("did:key:z"):
        raise ValueError("expected did:key:z...")
    raw = b58decode(did[len("did:key:z"):])
    if raw[:2] != b"\xed\x01" or len(raw) != 34:
        raise ValueError("not an Ed25519 did:key (multicodec 0xed01 + 32 bytes)")
    return raw[2:]


def ss58(pubkey: bytes, prefix: int) -> str:
    if prefix < 64:
        head = bytes([prefix])
    elif prefix < 16384:  # two-byte form per the SS58 registry
        head = bytes([((prefix & 0b0000_0000_1111_1100) >> 2) | 0b0100_0000,
                      (prefix >> 8) | ((prefix & 0b0000_0000_0000_0011) << 6)])
    else:
        raise ValueError("prefix out of range")
    checksum = hashlib.blake2b(b"SS58PRE" + head + pubkey, digest_size=64).digest()[:2]
    return b58encode(head + pubkey + checksum)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--did", default="did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV")
    ap.add_argument("--prefix", type=int, default=42, help="SS58 prefix (42 = generic Substrate until FLOP publishes its own)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        alice = bytes.fromhex("d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d")
        got = ss58(alice, 42)
        want = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        print("self-test", "PASS" if got == want else f"FAIL {got}")
        return 0 if got == want else 1
    pk = did_pubkey(a.did)
    print(f"did:      {a.did}")
    print(f"pubkey:   0x{pk.hex()}")
    print(f"ss58({a.prefix}): {ss58(pk, a.prefix)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
