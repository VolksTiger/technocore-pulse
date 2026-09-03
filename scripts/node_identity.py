#!/usr/bin/env python3
"""Create (or show) a low-value *node* identity for automated signed writes.

The main DID key stays encrypted and offline; this separate key lives unencrypted on
the server that posts routine signed digests into a room the main DID owns and has
allow-listed it for. Losing it costs nothing but a revocation (rewrite the allow-list).

  python3 scripts/node_identity.py            # prints the DID (creates the key if missing)
  python3 scripts/node_identity.py --path ~/.technocore-pulse/node.pem
Requires `cryptography`.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from client import b58encode  # noqa: E402

MULTICODEC_ED25519 = b"\xed\x01"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=os.path.expanduser("~/.technocore-pulse/node.pem"))
    a = ap.parse_args()
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: PLC0415
    if os.path.exists(a.path):
        key = serialization.load_pem_private_key(open(a.path, "rb").read(), password=None)
        created = False
    else:
        os.makedirs(os.path.dirname(a.path), exist_ok=True)
        key = ed25519.Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd = os.open(a.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        created = True
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    did = "did:key:z" + b58encode(MULTICODEC_ED25519 + raw)
    print(f"{'created' if created else 'existing'} node key at {a.path}\n{did}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
