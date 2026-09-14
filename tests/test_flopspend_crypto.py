"""Unit tests for flopspend/crypto.py -- real sr25519 verification/signing
via `py-sr25519-bindings`.

These tests exercise actual cryptography (not stubs), so the whole module
is skipped cleanly when the bindings aren't importable in this interpreter
(e.g. the system python3, which does not have them) -- see
`crypto.available()`.

Run with:  python3 -m unittest tests.test_flopspend_crypto   (from the repo root)
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from flopspend import budget, crypto, ledger, queue, runner, transcript, wire
from flopspend.vendor import compute_channel as cc

UTC = timezone.utc


@unittest.skipUnless(crypto.available(), "py-sr25519-bindings not installed: pip install py-sr25519-bindings")
class TestSr25519Crypto(unittest.TestCase):
    def setUp(self):
        with open(wire.VECTOR_PATH, encoding="utf-8") as f:
            self.data = json.load(f)
        self.verifier = crypto.Sr25519Verifier()

    def _h(self, hex_str: str) -> bytes:
        return bytes.fromhex(hex_str)

    # -- canonical positive vectors --------------------------------------

    def test_receipt_signature_verifies(self):
        r = self.data["compute_channel_v1"]["receipt"]
        ok = self.verifier(self._h(r["public_key_hex"]), self._h(r["preimage_hex"]), self._h(r["signature_hex"]))
        self.assertTrue(ok)

    def test_v3_leaf_signature_verifies(self):
        vs = self.data["compute_channel_v1"]["v3_leaf_signature"]
        ok = self.verifier(self._h(vs["public_key_hex"]), self._h(vs["leaf_hash_hex"]), self._h(vs["signature_hex"]))
        self.assertTrue(ok)

    def test_validator_attestation_signature_verifies(self):
        dr = self.data["direct_rail_v1"]
        ok = self.verifier(
            self._h(dr["validator_id_hex"]), self._h(dr["validator_attestation_signable_hex"]), self._h(dr["validator_signature_hex"])
        )
        self.assertTrue(ok)

    # -- negative vectors ---------------------------------------------------

    def test_invalid_receipt_signature_rejected(self):
        r = self.data["compute_channel_v1"]["receipt"]
        item = next(i for i in self.data["negative_cases"] if i["id"] == "invalid_receipt_signature")
        ok = self.verifier(self._h(r["public_key_hex"]), self._h(r["preimage_hex"]), self._h(item["bytes_hex"]))
        self.assertFalse(ok)

    def test_invalid_validator_signature_rejected(self):
        dr = self.data["direct_rail_v1"]
        item = next(i for i in self.data["negative_cases"] if i["id"] == "invalid_validator_signature")
        ok = self.verifier(
            self._h(dr["validator_id_hex"]), self._h(dr["validator_attestation_signable_hex"]), self._h(item["bytes_hex"])
        )
        self.assertFalse(ok)

    def test_legacy_receipt_signature_rejected_over_canonical_but_valid_over_legacy(self):
        r = self.data["compute_channel_v1"]["receipt"]
        item = next(i for i in self.data["negative_cases"] if i["id"] == "legacy_receipt_current_channel")
        raw = self._h(item["bytes_hex"])
        legacy_msg, sig = raw[:96], raw[96:]
        pubkey = self._h(r["public_key_hex"])
        canonical_preimage = self._h(r["preimage_hex"])
        # Proves the vector is a genuine legacy signature, not garbage bytes.
        self.assertTrue(self.verifier(pubkey, legacy_msg, sig))
        # A current-channel verifier checks against the canonical (domain-
        # separated) preimage instead, and must reject it.
        self.assertFalse(self.verifier(pubkey, canonical_preimage, sig))

    # -- signer/verifier plumbing --------------------------------------------

    def test_signer_verifier_roundtrip(self):
        seed = cc.blake2_256(b"test-roundtrip-seed")
        signer = crypto.Sr25519Signer(seed)
        message = b"a message the enclave would sign"
        sig = signer(message)
        self.assertEqual(len(sig), 64)
        self.assertEqual(len(signer.public_key), 32)
        self.assertTrue(self.verifier(signer.public_key, message, sig))

    def test_tampered_message_fails_verification(self):
        seed = cc.blake2_256(b"test-tamper-seed")
        signer = crypto.Sr25519Signer(seed)
        sig = signer(b"original message")
        self.assertFalse(self.verifier(signer.public_key, b"tampered message", sig))

    def test_wrong_length_inputs_return_false_not_raise(self):
        seed = cc.blake2_256(b"test-length-seed")
        signer = crypto.Sr25519Signer(seed)
        sig = signer(b"hello")
        self.assertFalse(self.verifier(b"short", b"hello", sig))  # bad pubkey length
        self.assertFalse(self.verifier(signer.public_key, b"hello", b"short"))  # bad signature length
        self.assertFalse(self.verifier(b"", b"hello", b""))

    def test_signer_repr_has_no_key_material(self):
        seed = cc.blake2_256(b"test-repr-seed")
        signer = crypto.Sr25519Signer(seed)
        rep = repr(signer)
        self.assertNotIn(seed.hex(), rep)
        self.assertNotIn(signer.public_key.hex(), rep)

    def test_signer_rejects_wrong_length_seed(self):
        with self.assertRaises(ValueError):
            crypto.Sr25519Signer(b"too short")

    # -- integration: dry run reports sr25519 --------------------------------

    def test_dry_run_reports_sr25519_verifier(self):
        tmpdir = tempfile.mkdtemp()
        try:
            ledger_path = os.path.join(tmpdir, "flopspend.jsonl")
            b = budget.Budget(ration=1000, stake=10, deposit=0.01, reserve_share=0.05, days=90)
            client = runner.DryRunClient()
            self.assertEqual(client.verifier_name, "sr25519")
            day_start = datetime(2026, 9, 10, tzinfo=UTC)
            summary = runner.run_day(client, b, queue.WorkloadQueue(), day_start, ledger_path=ledger_path, tick_minutes=60)
            self.assertEqual(summary.verifier, "sr25519")
            rows = ledger.read_all(ledger_path)
            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertEqual(row["verifier"], "sr25519")
                self.assertEqual(row["status"], "settled")  # a real, correctly-signed turn always verifies
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
