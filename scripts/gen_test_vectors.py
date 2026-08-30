#!/usr/bin/env python3
"""Generate canonical Technocore signed-message test vectors.

Deterministic: every vector is derived from a fixed 32-byte seed, so anyone can
reproduce both the did:key and the signature and confirm their Technocore
implementation produces byte-identical output. Covers the payload rule
`room|nonce|text` (UTF-8), the did:key (multicodec 0xed01 + base58btc), and the
unpadded base64url Ed25519 signature.

Run:  python3 scripts/gen_test_vectors.py > test-vectors.json
Needs the `cryptography` package (only for generation; the vectors themselves
are plain JSON verifiable with any Ed25519 implementation, incl. toolkit.html).
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MULTICODEC_ED25519 = b"\xed\x01"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def did_from_seed(seed: bytes) -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    raw_pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    did = "did:key:z" + b58encode(MULTICODEC_ED25519 + raw_pub)
    return key, did


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# (label, seed byte, room, nonce, text) — text chosen to exercise the tricky cases.
CASES = [
    ("basic", 0x01, "lobby", "1700000000000", "hello Technocore"),
    ("unicode-text", 0x02, "technocore", "1700000000001", "signé — ümlaut 日本語"),
    ("pipe-in-text", 0x03, "meta", "1700000000002", "a|b|c keeps only room and nonce as delimiters"),
    ("min-nonce", 0x04, "faucet", "1", "edge: single-digit nonce"),
    ("max-nonce-19", 0x05, "lobby", "9999999999999999999", "edge: 19-digit nonce ceiling"),
    ("empty-ish-text", 0x06, "lobby", "1700000000003", "."),
]


def main() -> None:
    vectors = []
    for label, seed_byte, room, nonce, text in CASES:
        seed = bytes([seed_byte]) + b"\x00" * 31
        key, did = did_from_seed(seed)
        payload = f"{room}|{nonce}|{text}".encode("utf-8")
        sig = key.sign(payload)
        vectors.append(
            {
                "label": label,
                "seed_hex": seed.hex(),
                "did": did,
                "kv_fingerprint": hashlib.sha256(did.encode()).hexdigest()[:16],
                "room": room,
                "nonce": nonce,
                "text": text,
                "payload_utf8": payload.decode("utf-8"),
                "signature_b64url": b64url(sig),
            }
        )
    doc = {
        "schema": "technocore-signature-test-vectors-v1",
        "description": "Deterministic Ed25519 signing test vectors for Technocore. "
        "payload = room|nonce|text (UTF-8); signature = unpadded base64url over payload; "
        "did:key = z + base58btc(0xed01 || raw_pubkey); kv_fingerprint = sha256(did)[:16].",
        "count": len(vectors),
        "vectors": vectors,
    }
    print(json.dumps(doc, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
