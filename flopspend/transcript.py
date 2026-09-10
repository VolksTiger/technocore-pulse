"""Agent-side session transcript (R12.1b) -- the running Merkle accumulator
over turn leaves, turn-signature verification, and the receipt the agent
counter-signs.

What this module does, precisely (R12.1b, Appendix F.3):

  1. For each turn, build the leaf exactly as the vendored `compute_channel`
     encoder defines it for the channel's leaf version (V0-V3), append it to
     a running list, and fold the running Merkle root over the vendored
     `merkle_root` -- the SAME function `wire.verify_vectors()` checks against
     `compute_channel_v1.leaf_versions` / `compute_channel_v1.merkle` in the
     canonical corpus. Leaf construction and root folding are therefore fully
     checkable offline; see wire.py.
  2. Enforce the one invariant R12.1b states in prose and this module can
     actually enforce without a chain: "uniqueness is session_id‖turn_index;
     replay protection is the monotonic counter" -- turns must be appended in
     strictly increasing order starting at 0.
  3. Verify a miner-signed turn and counter-sign the agent's receipt over the
     cumulative root -- through a PLUGGABLE verifier/signer callback, never a
     built-in cryptographic implementation.

What this module deliberately does NOT do, and why:

  R12.1b's turn signature and the F.3 "agent receipt v1" are BOTH sr25519
  (yellow paper §6.5, "Session enclave key (per-turn transcript leaf): sr25519";
  Appendix F.3, "agent receipt v1: sr25519 over ..."). This module does not
  implement sr25519 -- there is no pure-Python/stdlib sr25519 in this
  environment, and rolling one for a testnet that will pay real tokens later
  is exactly the kind of thing that should come from a vetted library
  (py-substrate-interface / the published SDK), not be reinvented here.
  `SignatureVerifier` and `Signer` are plain callables; `StubVerifier` and
  `StubSigner` are the defaults, and they are explicit about doing NO
  cryptography -- they only record what was asked of them and return a fixed
  answer, so the surrounding plumbing (runner.py's DryRunClient, tests) can be
  exercised end-to-end before a real verifier exists. The wire-format-v1.json
  corpus's signature-bearing vectors (v3_leaf_signature, receipt.signature_hex,
  the two invalid_*_signature negative cases) are correspondingly SKIPPED, not
  faked, by wire.verify_vectors() -- see that module's docstring.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Optional

from flopspend.vendor import compute_channel as cc
# Reusing the vendored per-leaf version/field consistency check rather than
# duplicating it. It is a module-private name in compute-channel.py (a
# vendored file we do not edit), not a flopspend implementation detail.
from flopspend.vendor.compute_channel import _check_record  # noqa: E402

ZERO_HASH = bytes(32)
ZERO_SIG = bytes(64)

# (public_key, message, signature) -> is the signature valid over message?
SignatureVerifier = Callable[[bytes, bytes, bytes], bool]
# message -> signature
Signer = Callable[[bytes], bytes]


@dataclass(frozen=True)
class VerificationCall:
    """One record of "a verifier was asked about this" -- what StubVerifier
    and StubSigner log, so a dry run or a test can assert on intent without a
    real cryptographic result."""

    public_key: bytes
    message: bytes
    signature: bytes


class StubVerifier:
    """Default SignatureVerifier: does NOT check any cryptography. Records
    every call and returns a fixed verdict (default: accept). Use this to
    wire the plumbing; replace with a real sr25519/ed25519 verifier (a
    callable of the same shape) once one is available."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.calls: "list[VerificationCall]" = []

    def __call__(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        self.calls.append(VerificationCall(public_key, message, signature))
        return self.accept


class StubSigner:
    """Default Signer: does NOT perform real cryptographic signing. Returns a
    deterministic 64-byte placeholder (blake2b of the public key and message)
    so callers get a stable, reproducible non-signature to plumb through
    encoders and ledgers offline. NEVER treat this as a real signature."""

    def __init__(self, public_key: bytes = ZERO_HASH) -> None:
        self.public_key = public_key
        self.calls: "list[bytes]" = []

    def __call__(self, message: bytes) -> bytes:
        self.calls.append(message)
        return hashlib.blake2b(self.public_key + message, digest_size=64).digest()


def leaf_hash(
    version: cc.TranscriptLeafVersion,
    channel_id: bytes,
    turn_index: int,
    h_in: bytes,
    h_out: bytes,
    g_n: int,
    decode_policy_hash: "bytes | None" = None,
    ids_hash: "bytes | None" = None,
    toploc_commitment_hash: "bytes | None" = None,
    miner_recv_ms: int = 0,
    miner_done_ms: int = 0,
    latency_ms: int = 0,
) -> bytes:
    """The vendored leaf hash for one turn (Appendix F.3). Matches
    `compute_channel_v1.leaf_versions[*].hash_hex` in the corpus for these
    exact inputs -- see wire.py's `compute_channel_v1.leaf_versions.*` checks."""
    return cc.transcript_leaf(
        version,
        channel_id=channel_id,
        turn_index=turn_index,
        h_in=h_in,
        h_out=h_out,
        g_n=g_n,
        decode_policy_hash=decode_policy_hash or ZERO_HASH,
        ids_hash=ids_hash or ZERO_HASH,
        toploc_commitment_hash=toploc_commitment_hash or ZERO_HASH,
        miner_recv_ms=miner_recv_ms,
        miner_done_ms=miner_done_ms,
        latency_ms=latency_ms,
    )


@dataclass
class TranscriptAccumulator:
    """The agent side of R12.1b: a running Merkle accumulator over turn
    leaves for one open channel.

    `add_turn` appends exactly one leaf per call, in strict turn_index order
    starting at 0 (R12.1b: "uniqueness is session_id‖turn_index; replay
    protection is the monotonic counter" -- this is the one piece of that
    sentence an offline accumulator can actually enforce; the chain's
    duplicate/out-of-range rejection at settle is a separate, on-chain check
    this module does not reproduce, see wire.py's `negative_cases.duplicate_turn_index` skip).

    `root()` recomputes `merkle_root` over all leaves on each call -- O(n) per
    call, fine for n <= channel_max_settlement_turns (1,024, R12.1h).
    """

    channel_id: bytes
    records: "list[cc.TurnRecord]" = field(default_factory=list)
    leaves: "list[bytes]" = field(default_factory=list)

    def add_turn(
        self,
        version: cc.TranscriptLeafVersion,
        h_in: bytes,
        h_out: bytes,
        g_n: int,
        enclave_sig: bytes,
        decode_policy_hash: "bytes | None" = None,
        ids_hash: "bytes | None" = None,
        toploc_commitment_hash: "bytes | None" = None,
        miner_recv_ms: int = 0,
        miner_done_ms: int = 0,
        latency_ms: int = 0,
    ) -> bytes:
        """Append the next turn (turn_index is implicit: len(self.records)).
        Returns the new leaf hash. Raises ValueError on a version/field
        inconsistency (the same check encode_transcript_blob applies)."""
        turn_index = len(self.records)
        record = cc.TurnRecord(
            version, turn_index, h_in, h_out, g_n,
            decode_policy_hash, ids_hash, toploc_commitment_hash,
            miner_recv_ms, miner_done_ms, latency_ms, enclave_sig,
        )
        _check_record(record)
        leaf = leaf_hash(
            version, self.channel_id, turn_index, h_in, h_out, g_n,
            decode_policy_hash, ids_hash, toploc_commitment_hash,
            miner_recv_ms, miner_done_ms, latency_ms,
        )
        self.records.append(record)
        self.leaves.append(leaf)
        return leaf

    def root(self) -> bytes:
        return cc.merkle_root(self.leaves)

    def aggregate_gn(self) -> int:
        """Checked sum of distinct submitted g_n (Appendix F.3, aggregate_gn
        check). Turn uniqueness is already enforced by add_turn's strict
        ordering, so "distinct" reduces to "all of them"."""
        return sum(r.g_n for r in self.records)

    def verify_turn(
        self, index: int, enclave_pubkey: bytes, verifier: "SignatureVerifier | None" = None
    ) -> bool:
        """Verify turn `index`'s enclave_sig over its 32-byte leaf hash
        (F.3: "signature is over the 32 B leaf hash"). `verifier` defaults to
        a fresh StubVerifier(accept=True) -- see the module docstring for why
        real verification is out of scope here."""
        verifier = verifier or StubVerifier()
        record = self.records[index]
        return verifier(enclave_pubkey, self.leaves[index], record.enclave_sig)

    def receipt_preimage(self, aggregate_gn: int, payable: int) -> bytes:
        """The RECEIPT_DOMAIN preimage over the cumulative root (F.3 agent
        receipt v1) that the agent counter-signs. Matches
        `compute_channel_v1.receipt.preimage_hex` for these inputs -- see
        wire.py's `compute_channel_v1.receipt.preimage_and_structure` check."""
        return cc.receipt_message_v1(self.channel_id, self.root(), aggregate_gn, payable)

    def counter_sign(self, aggregate_gn: int, payable: int, signer: "Signer | None" = None) -> bytes:
        """Sign `receipt_preimage(aggregate_gn, payable)` and return the
        signature. `signer` defaults to a fresh StubSigner() -- NOT a real
        sr25519 signature, see the module docstring."""
        signer = signer or StubSigner()
        return signer(self.receipt_preimage(aggregate_gn, payable))
