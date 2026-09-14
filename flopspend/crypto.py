"""Real sr25519 signature verification and signing, via the vetted
`py-sr25519-bindings` package (schnorrkel/sr25519 bindings used by the
Substrate/Polkadot ecosystem -- the same curve the FLOP yellow paper
specifies for the session-enclave per-turn signature and the agent's
receipt counter-signature, §6.5 / Appendix F.3).

This module never re-implements sr25519 -- it only wraps the `sr25519`
package's `verify` / `pair_from_seed` / `sign` functions. The import is
lazy (deferred to first use, not module import time) so `import
flopspend.crypto` is always safe, including in environments where the
bindings aren't installed; callers check `available()` first, or construct
a Verifier/Signer and catch the `RuntimeError` this module raises instead.

Install:  pip install py-sr25519-bindings
"""

from __future__ import annotations

__all__ = ["Sr25519Verifier", "Sr25519Signer", "available", "INSTALL_HINT"]

INSTALL_HINT = "py-sr25519-bindings not installed: pip install py-sr25519-bindings"

_PUBKEY_LEN = 32
_SEED_LEN = 32
_SIG_LEN = 64


def _import_sr25519():
    try:
        import sr25519  # type: ignore
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    return sr25519


def available() -> bool:
    """True iff `py-sr25519-bindings` can be imported in this interpreter."""
    try:
        _import_sr25519()
    except RuntimeError:
        return False
    return True


class Sr25519Verifier:
    """Real sr25519 verifier matching `transcript.SignatureVerifier`'s
    callable shape: (public_key, message, signature) -> bool.

    Never raises on bad input -- a wrong-length public key or signature is
    just another way to be an invalid signature, so it returns False rather
    than propagating a ValueError from the bindings.
    """

    def __init__(self) -> None:
        self._sr25519 = _import_sr25519()

    def __call__(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        if len(public_key) != _PUBKEY_LEN or len(signature) != _SIG_LEN:
            return False
        try:
            return bool(self._sr25519.verify(signature, message, public_key))
        except Exception:  # noqa: BLE001 -- any bindings-level rejection means "not valid"
            return False

    def __repr__(self) -> str:
        return "Sr25519Verifier()"


class Sr25519Signer:
    """Real sr25519 signer matching `transcript.Signer`'s callable shape:
    message -> 64-byte signature. Holds a keypair derived from a 32-byte
    seed via `sr25519.pair_from_seed`; `.public_key` (32 bytes) is safe to
    share, the seed and private key never are.

    The seed is never logged, returned, or otherwise exposed after
    construction, and `__repr__` carries no key material at all (not even
    the public key) so accidental logging of a signer object can't leak
    anything.
    """

    def __init__(self, seed: bytes) -> None:
        if len(seed) != _SEED_LEN:
            raise ValueError(f"sr25519 seed must be exactly {_SEED_LEN} bytes, got {len(seed)}")
        sr25519 = _import_sr25519()
        self._sr25519 = sr25519
        pub, priv = sr25519.pair_from_seed(seed)
        self._keypair = (pub, priv)
        self.public_key = pub

    def __call__(self, message: bytes) -> bytes:
        return self._sr25519.sign(self._keypair, message)

    def __repr__(self) -> str:
        return "Sr25519Signer(...)"
