"""Thin wrapper around the vendored compute-channel wire encoders.

flopspend never re-implements SCALE, the domain-separated blake2 hashing, or the
transcript Merkle tree -- it imports `flopspend.vendor.compute_channel` (fetched
verbatim from flop-labs/yellowpaper, evidence/compute-channel.py; see
flopspend/vendor/ for the attribution header and CC BY 4.0 license text) and
checks it against the published corpus, `flopspend/vendor/wire-format-v1.json`.

`verify_vectors()` walks the ENTIRE corpus and classifies every vector it finds
into one of three buckets:

  * "pass"  -- the vendored encoder reproduces the expected bytes/hash, or (for
    a negative case) the vendored decoder/consistency-check rejects it, exactly
    as the vector says it must.
  * "fail"  -- a genuine mismatch. Zero of these is the pass bar.
  * "skip"  -- the vector exercises something outside what compute-channel.py
    implements: a cryptographic signature (sr25519 -- see transcript.py's
    docstring for why this module does not implement sr25519), a Retention/TEE
    enum decoder that lives in a different, non-vendored appendix family, or a
    chain-runtime check (duplicate-turn dedup, pinned-channel-policy cutoff)
    that has no wire-encoding counterpart to run offline. Every skip carries a
    reason; skips are never counted as failures, but they are never silently
    dropped either.

Two families in the corpus (decode_policy_v1's SHA-256 hash and data_ref_v1's
flat SCALE bytes) don't need the vendored module at all -- they're checked here
directly against the vector's own preimage, using only stdlib hashlib and the
retention-tag table from Appendix F.4 of the yellow paper text. That table
value (Ephemeral=0, Leased=1) is transcribed from the spec, not invented.

CLI:  python3 -m flopspend.wire --check
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

from flopspend.vendor import compute_channel as cc

VECTOR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "wire-format-v1.json")

# Domain constant for hp_poui::DecodePolicy::hash (Appendix F.1). Not part of
# compute-channel.py (a sibling module, not vendored here); transcribed verbatim
# from the canonical generator script we fetched to READ (not vendored) --
# evidence/generate-wire-format-vectors.py, `decode_policy_hash = sha256(b"FLOP_DECODE_POLICY_HASH_V1" + ...)`.
DECODE_POLICY_HASH_DOMAIN = b"FLOP_DECODE_POLICY_HASH_V1"

# Appendix F.4 DataRef v1 retention tags (transcribed from the yellow paper text):
# "retention tags Ephemeral=0, Leased=1; unknown retention fails SCALE decode".
RETENTION_TAGS = {"Ephemeral": 0, "Leased": 1}


def _h(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "skip"
    detail: str = ""


@dataclass
class VectorReport:
    results: "list[CheckResult]"

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "fail")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skip")

    @property
    def ok(self) -> bool:
        return self.failed == 0

    @property
    def failures(self) -> "list[CheckResult]":
        return [r for r in self.results if r.status == "fail"]

    def summary(self) -> str:
        return (
            f"vectors: {self.total} total, {self.passed} passed, "
            f"{self.skipped} skipped (documented, not failures), {self.failed} failed"
        )


class _Recorder:
    """Accumulates CheckResults; each check runs in isolation (one bad assert
    never stops the rest of the corpus from being checked)."""

    def __init__(self) -> None:
        self.results: "list[CheckResult]" = []

    def check(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except AssertionError as exc:
            self.results.append(CheckResult(name, "fail", str(exc) or "assertion failed"))
        except Exception as exc:  # noqa: BLE001 -- any exception during a "pass" attempt is a failure
            self.results.append(CheckResult(name, "fail", f"{type(exc).__name__}: {exc}"))
        else:
            self.results.append(CheckResult(name, "pass"))

    def expect_raises(self, name: str, exc_type: type, fn: Callable[[], None]) -> None:
        try:
            fn()
        except exc_type:
            self.results.append(CheckResult(name, "pass"))
        except Exception as exc:  # noqa: BLE001
            self.results.append(
                CheckResult(name, "fail", f"raised {type(exc).__name__} not {exc_type.__name__}: {exc}")
            )
        else:
            self.results.append(CheckResult(name, "fail", "did not raise; expected rejection"))

    def skip(self, name: str, reason: str) -> None:
        self.results.append(CheckResult(name, "skip", reason))


def _check_codec(data: dict, rec: _Recorder) -> None:
    codec = data["codec"]
    for i, item in enumerate(codec["scale_compact_u32"]):
        def go(item: dict = item) -> None:
            encoded = cc.compact_u32(item["value"])
            assert encoded.hex() == item["bytes_hex"], f"encode({item['value']}) = {encoded.hex()}"
            value, consumed = cc.decode_compact_u32(_h(item["bytes_hex"]))
            assert value == item["value"], f"decoded {value} != {item['value']}"
            assert consumed == len(encoded), "decode consumed wrong length"

        rec.check(f"codec.scale_compact_u32[{i}]", go)

    for i, item in enumerate(codec["malformed_compact"]):
        def go(item: dict = item) -> None:
            cc.decode_compact_u32(_h(item["bytes_hex"]))

        rec.expect_raises(f"codec.malformed_compact[{i}] ({item['reason']})", ValueError, go)

    def decode_scale_bool(byte_value: int) -> bool:
        # Generic SCALE bool (00/01, else reject) -- a one-line stdlib primitive,
        # not FLOP-specific, so it isn't in compute-channel.py; written here only
        # to exercise the corpus's codec.scale_bool entries.
        if byte_value == 0:
            return False
        if byte_value == 1:
            return True
        raise ValueError("invalid SCALE bool tag")

    def go_false() -> None:
        assert decode_scale_bool(0x00) is False, "0x00 must decode to False"

    def go_true() -> None:
        assert decode_scale_bool(0x01) is True, "0x01 must decode to True"

    rec.check("codec.scale_bool.false", go_false)
    rec.check("codec.scale_bool.true", go_true)
    rec.expect_raises("codec.scale_bool.other", ValueError, lambda: decode_scale_bool(0x02))


def _check_decode_policy(data: dict, rec: _Recorder) -> None:
    dp = data["decode_policy_v1"]

    def go() -> None:
        scale = _h(dp["scale_bytes_hex"])
        preimage = DECODE_POLICY_HASH_DOMAIN + scale
        assert preimage.hex() == dp["hash_preimage_hex"], "preimage mismatch"
        digest = hashlib.sha256(preimage).hexdigest()
        assert digest == dp["sha256_hex"], f"sha256 {digest} != {dp['sha256_hex']}"

    rec.check("decode_policy_v1.hash", go)


def _check_direct_rail(data: dict, rec: _Recorder) -> "bytes | None":
    dr = data["direct_rail_v1"]
    task_hash_holder: "list[bytes]" = []

    def go_task_hash() -> None:
        inp = dr["task_hash"]["inputs"]
        th = cc.task_hash_v1(
            _h(inp["genesis_hash_hex"]), _h(inp["agent_account_id32_hex"]), inp["nonce"],
            _h(inp["model_hash_hex"]), _h(inp["payload_hash_hex"]), _h(inp["commit_hash_hex"]),
        )
        preimage = (
            cc.TASK_HASH_DOMAIN_V1 + b"\x01" + _h(inp["genesis_hash_hex"]) + _h(inp["agent_account_id32_hex"])
            + inp["nonce"].to_bytes(8, "little") + _h(inp["model_hash_hex"]) + _h(inp["payload_hash_hex"])
            + _h(inp["commit_hash_hex"])
        )
        assert preimage.hex() == dr["task_hash"]["preimage_hex"], "task_hash preimage mismatch"
        assert th.hex() == dr["task_hash"]["hash_hex"], f"task_hash {th.hex()} != {dr['task_hash']['hash_hex']}"
        task_hash_holder.append(th)

    rec.check("direct_rail_v1.task_hash", go_task_hash)
    if not task_hash_holder:
        rec.skip("direct_rail_v1.report_data", "task_hash check failed; skipping dependent checks")
        rec.skip("direct_rail_v1.validator_attestation", "task_hash check failed; skipping dependent checks")
        return None
    task_hash = task_hash_holder[0]

    def go_report_data() -> None:
        inp = dr["inputs"]
        tee_tag = inp["tee_type"]["scale_tag"]
        report_preimage = (
            task_hash + inp["gn_weight"].to_bytes(8, "little") + inp["latency_ms"].to_bytes(8, "little")
            + _h(inp["model_hash_hex"]) + _h(inp["output_hash_hex"]) + _h(inp["decode_policy_hash_hex"])
            + bytes([tee_tag])
        )
        assert report_preimage.hex() == dr["report_data_preimage_hex"], "report_data preimage mismatch"
        report_data = hashlib.sha256(report_preimage).digest() + bytes(32)
        assert report_data.hex() == dr["report_data_hex"], "report_data mismatch"

        attestation_signable = (
            report_preimage + bytes([1 if inp["quote_verified"] else 0, 1 if inp["event_log_verified"] else 0])
            + _h(inp["hardware_id_hash_hex"])
        )
        assert attestation_signable.hex() == dr["validator_attestation_signable_hex"], "attestation_signable mismatch"

        attestation_scale = attestation_signable + _h(dr["validator_id_hex"]) + _h(dr["validator_signature_hex"])
        assert attestation_scale.hex() == dr["validator_attestation_scale_hex"], "attestation_scale mismatch"

    rec.check("direct_rail_v1.report_data_and_attestation_bytes", go_report_data)
    rec.skip(
        "direct_rail_v1.validator_signature",
        "sr25519 ValidatorAttestation signature (Appendix F.2/G.2) not implemented -- "
        "pluggable verifier only, see transcript.py",
    )
    return task_hash


def _check_data_ref(data: dict, rec: _Recorder) -> None:
    dref = data["data_ref_v1"]

    def go() -> None:
        inp = dref["input"]
        scale = _h(inp["commitment_hex"]) + bytes([inp["provider_id"]]) + bytes([RETENTION_TAGS[inp["retention"]]])
        assert scale.hex() == dref["scale_bytes_hex"], "data_ref scale bytes mismatch"

    rec.check("data_ref_v1.scale_bytes", go)


def _check_compute_channel(data: dict, rec: _Recorder) -> dict:
    """Returns a dict of intermediate values reused by the negative-case checks."""
    ccv = data["compute_channel_v1"]
    ctx: dict = {}

    ci = ccv["channel_id"]
    genesis, agent, miner = _h(ci["inputs"]["genesis_hash_hex"]), _h(ci["inputs"]["agent_account_id32_hex"]), _h(ci["inputs"]["miner_account_id32_hex"])
    nonce = ci["inputs"]["nonce"]
    ctx.update(genesis=genesis, agent=agent, miner=miner, nonce=nonce)

    def go_channel_id() -> None:
        cid = cc.channel_id_v1(genesis, agent, miner, nonce)
        assert cid.hex() == ci["hash_hex"], f"channel_id {cid.hex()} != {ci['hash_hex']}"
        preimage = cc.CHANNEL_ID_DOMAIN_V1 + b"\x01" + genesis + agent + miner + nonce.to_bytes(8, "little")
        assert preimage.hex() == ci["preimage_hex"], "channel_id preimage mismatch"
        ctx["channel_id"] = cid

    rec.check("compute_channel_v1.channel_id", go_channel_id)
    if "channel_id" not in ctx:
        return ctx
    channel_id = ctx["channel_id"]

    li = ccv["leaf_inputs"]
    fields = dict(
        channel_id=_h(li["channel_id_hex"]), turn_index=li["turn_index"], h_in=_h(li["h_in_hex"]), h_out=_h(li["h_out_hex"]),
        g_n=int(li["g_n"]), decode_policy_hash=_h(li["decode_policy_hash_hex"]), ids_hash=_h(li["h_ids_hex"]),
        toploc_commitment_hash=_h(li["toploc_commitment_hash_hex"]), miner_recv_ms=int(li["miner_recv_ms"]),
        miner_done_ms=int(li["miner_done_ms"]), latency_ms=li["latency_ms"],
    )
    ctx["fields"] = fields
    leaves: "dict[str, bytes]" = {}

    for entry in ccv["leaf_versions"]:
        def go(entry: dict = entry) -> None:
            version = cc.TranscriptLeafVersion[entry["version"]]
            assert int(version) == entry["scale_tag"], "scale_tag mismatch"
            preimage = cc.leaf_preimage(version, **fields)
            assert preimage.hex() == entry["preimage_hex"], f"{entry['version']} preimage mismatch"
            leaf = cc.blake2_256(preimage)
            assert leaf.hex() == entry["hash_hex"], f"{entry['version']} hash mismatch"
            leaves[entry["version"]] = leaf

        rec.check(f"compute_channel_v1.leaf_versions.{entry['version']}", go)

    ctx["leaves"] = leaves
    if len(leaves) < 4:
        return ctx

    m = ccv["merkle"]
    tree_leaves = [leaves[name] for name in m["leaf_order"]]
    ctx["tree_leaves"] = tree_leaves

    def go_root() -> None:
        root = cc.merkle_root(tree_leaves)
        assert root.hex() == m["root_hex"], f"root {root.hex()} != {m['root_hex']}"
        ctx["root"] = root

    rec.check("compute_channel_v1.merkle.root", go_root)

    def go_path() -> None:
        path = cc.merkle_path(tree_leaves, 2)
        expected = [(_h(p["sibling_hex"]), p["sibling_is_left"]) for p in m["path_for_index_2"]]
        assert path == expected, "merkle path mismatch"
        assert cc.root_from_path(tree_leaves[2], path) == ctx["root"], "root_from_path disagrees with merkle_root"
        ctx["path"] = path

    rec.check("compute_channel_v1.merkle.path_for_index_2", go_path)

    vs = ccv["v3_leaf_signature"]

    def go_v3_sig_structure() -> None:
        assert vs["leaf_hash_hex"] == leaves["V3"].hex(), "v3 leaf hash mismatch"
        assert len(_h(vs["public_key_hex"])) == 32, "public key must be 32 bytes"
        assert len(_h(vs["signature_hex"])) == 64, "signature must be 64 bytes"

    rec.check("compute_channel_v1.v3_leaf_signature.structure", go_v3_sig_structure)
    rec.skip(
        "compute_channel_v1.v3_leaf_signature.signature",
        "sr25519 signature over the leaf hash (Session enclave key, Appendix F.3) not implemented -- "
        "pluggable verifier only, see transcript.py",
    )

    if "path" in ctx:
        def go_verified_turn() -> None:
            record = cc.TurnRecord(
                cc.TranscriptLeafVersion.V3, fields["turn_index"], fields["h_in"], fields["h_out"], fields["g_n"],
                fields["decode_policy_hash"], fields["ids_hash"], fields["toploc_commitment_hash"],
                fields["miner_recv_ms"], fields["miner_done_ms"], fields["latency_ms"], _h(vs["signature_hex"]),
            )
            encoded = cc.encode_verified_turn(record, ctx["path"])
            assert encoded.hex() == ccv["verified_turn_v3_scale_hex"], "verified_turn encoding mismatch"
            ctx["v3_record"] = record

        rec.check("compute_channel_v1.verified_turn_v3_scale", go_verified_turn)

    if "v3_record" in ctx:
        def go_fcc4_encode() -> None:
            blob = cc.encode_transcript_blob(channel_id, [ctx["v3_record"]])
            assert blob.hex() == ccv["fcc4_transcript_blob_hex"], "fcc4 blob encoding mismatch"
            ctx["blob"] = blob

        rec.check("compute_channel_v1.fcc4_transcript_blob.encode", go_fcc4_encode)

        if "blob" in ctx:
            def go_fcc4_decode() -> None:
                decoded_channel_id, decoded_turns = cc.decode_transcript_blob(ctx["blob"])
                assert decoded_channel_id == channel_id, "decoded channel_id mismatch"
                assert decoded_turns == [ctx["v3_record"]], "decoded turn record mismatch"

            rec.check("compute_channel_v1.fcc4_transcript_blob.decode_roundtrip", go_fcc4_decode)

    r = ccv["receipt"]
    r_channel_id, r_root = _h(r["inputs"]["channel_id_hex"]), _h(r["inputs"]["final_root_hex"])
    r_agg, r_payable = r["inputs"]["aggregate_gn"], r["inputs"]["payable"]

    def go_receipt() -> None:
        msg = cc.receipt_message_v1(r_channel_id, r_root, r_agg, r_payable)
        assert msg.hex() == r["preimage_hex"], "receipt preimage mismatch"
        assert len(_h(r["public_key_hex"])) == 32, "public key must be 32 bytes"
        assert len(_h(r["signature_hex"])) == 64, "signature must be 64 bytes"
        ctx["receipt_message"] = msg
        ctx["receipt_inputs"] = (r_channel_id, r_root, r_agg, r_payable)

    rec.check("compute_channel_v1.receipt.preimage_and_structure", go_receipt)
    rec.skip(
        "compute_channel_v1.receipt.signature",
        "sr25519 agent-counter-signature over the receipt (R12.1b, Appendix F.3) not implemented -- "
        "pluggable verifier only, see transcript.py",
    )

    return ctx


def _check_negative_cases(data: dict, ctx: dict, rec: _Recorder) -> None:
    for item in data["negative_cases"]:
        cid, raw, exp = item["id"], _h(item["bytes_hex"]), item["expected"]
        name = f"negative_cases.{cid}"

        if cid == "unknown_leaf_enum":
            rec.expect_raises(name, ValueError, lambda raw=raw: cc.TranscriptLeafVersion(raw[0]))
        elif cid == "unknown_retention_enum":
            rec.skip(name, "Retention enum decode (Appendix F.4, data_ref_v1) is not in the vendored compute-channel encoders")
        elif cid in ("truncated_fcc4", "trailing_fcc4", "unknown_fcc_version"):
            rec.expect_raises(name, ValueError, lambda raw=raw: cc.decode_transcript_blob(raw))
        elif cid == "duplicate_turn_index":
            rec.skip(name, "verified_work_from_turns dedup is chain-runtime logic; the vendored module has no decoder for a bare Vec<VerifiedTurn>")
        elif cid == "wrong_path_orientation":
            # NOT reproducible offline, and worth spelling out why: index 2 is the
            # odd leaf out in this 3-leaf tree, so its first-level Merkle sibling is
            # the *duplicate of itself* (hash_pair(V3, V3)). hash_pair(a, a) is the
            # same bytes regardless of which side "sibling_is_left" claims it's on,
            # so flipping that flag and recomputing root_from_path reproduces the
            # TRUE root -- verified below. The chain must be rejecting this via a
            # canonical-orientation/parity check derived independently of the
            # hash fold (e.g. against turn_index), which merkle_path/root_from_path
            # in the vendored module do not implement (they only ever construct or
            # fold a path, never validate a submitted one's orientation against an
            # expected topology). So this vector's rejection genuinely cannot be
            # reproduced from the vendored wire encoders alone.
            if "path" not in ctx or "root" not in ctx or "tree_leaves" not in ctx:
                rec.skip(name, "prerequisite merkle checks did not run")
                continue

            def go(ctx: dict = ctx) -> None:
                wrong_path = list(ctx["path"])
                sib, is_left = wrong_path[0]
                wrong_path[0] = (sib, not is_left)
                bad_root = cc.root_from_path(ctx["tree_leaves"][2], wrong_path)
                # Documents the finding above, not a pass/fail: for THIS vector the
                # duplicate-last edge case makes the flip a hash-level no-op.
                assert bad_root == ctx["root"], "expected the self-duplicate-sibling no-op; got a different root instead"

            rec.check(name + " (hash-level no-op, confirmed -- see comment)", go)
            rec.skip(
                name,
                "orientation-vs-canonical-topology rejection (LeafNotInRoot) is not reproducible from "
                "root_from_path/merkle_path alone: index 2's first-level sibling is a duplicate of itself "
                "(odd-leaf-count padding), so flipping sibling_is_left is a hash-level no-op here "
                "(confirmed by the check above) -- the chain must reject via a canonical-orientation check "
                "this module does not implement",
            )
        elif cid == "wrong_genesis_network":
            if "channel_id" not in ctx:
                rec.skip(name, "prerequisite channel_id check did not run")
                continue

            def go(ctx: dict = ctx, raw: bytes = raw) -> None:
                mutated_genesis = bytes([1]) + ctx["genesis"][1:]
                new_cid = cc.channel_id_v1(mutated_genesis, ctx["agent"], ctx["miner"], ctx["nonce"])
                assert new_cid == raw, "recomputed mutated channel_id does not match the vector"
                assert new_cid != ctx["channel_id"], "mutated channel_id collided with the original"

            rec.check(name, go)
        elif cid == "wrong_session":
            if "channel_id" not in ctx:
                rec.skip(name, "prerequisite channel_id check did not run")
                continue

            def go(ctx: dict = ctx, raw: bytes = raw) -> None:
                mutated_agent = bytes([0x12]) * 32
                new_cid = cc.channel_id_v1(ctx["genesis"], mutated_agent, ctx["miner"], ctx["nonce"])
                assert new_cid == raw, "recomputed mutated channel_id does not match the vector"
                assert new_cid != ctx["channel_id"], "mutated channel_id collided with the original"

            rec.check(name, go)
        elif cid == "wrong_leaf_version":
            if "v3_record" not in ctx or "channel_id" not in ctx:
                rec.skip(name, "prerequisite verified_turn check did not run")
                continue

            def go(ctx: dict = ctx) -> None:
                bad_record = dataclasses.replace(ctx["v3_record"], leaf_version=cc.TranscriptLeafVersion.V2)
                cc.encode_transcript_blob(ctx["channel_id"], [bad_record])

            rec.expect_raises(name, ValueError, go)
        elif cid in ("invalid_receipt_signature", "invalid_validator_signature"):
            rec.skip(name, "signature verification not implemented -- pluggable verifier only, see transcript.py")
        elif cid == "legacy_leaf_current_channel":
            rec.skip(name, "pinned-channel decode-policy cutoff is chain state, not a wire-encoding invariant reproducible offline")
        elif cid == "legacy_receipt_current_channel":
            if "receipt_inputs" not in ctx:
                rec.skip(name, "prerequisite receipt check did not run")
                continue

            def go(ctx: dict = ctx, raw: bytes = raw) -> None:
                legacy_msg = cc.legacy_receipt_message(*ctx["receipt_inputs"])
                assert raw[:96] == legacy_msg, "legacy receipt preimage mismatch"
                assert len(raw) == 160, "expected 96-byte legacy preimage + 64-byte signature"

            rec.check(name + ".preimage", go)
            rec.skip(name + ".signature", "signature verification not implemented -- pluggable verifier only, see transcript.py")
        else:
            rec.skip(name, f"unrecognized negative-case id {cid!r}; add handling in wire.py")


def verify_vectors(path: "str | None" = None) -> VectorReport:
    """Check the entire wire-format-v1.json corpus. See the module docstring
    for what "pass" / "fail" / "skip" mean here."""
    with open(path or VECTOR_PATH, encoding="utf-8") as f:
        data = json.load(f)

    rec = _Recorder()
    _check_codec(data, rec)
    _check_decode_policy(data, rec)
    _check_direct_rail(data, rec)
    _check_data_ref(data, rec)
    ctx = _check_compute_channel(data, rec)
    _check_negative_cases(data, ctx, rec)
    return VectorReport(rec.results)


def _main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m flopspend.wire")
    ap.add_argument("--check", action="store_true", help="run verify_vectors() and print counts")
    ap.add_argument("--vectors", default=None, help="path to wire-format-v1.json (default: vendored copy)")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every check, not just failures")
    args = ap.parse_args(argv)

    report = verify_vectors(args.vectors)
    print(report.summary())
    if args.verbose:
        for r in report.results:
            print(f"  [{r.status:4}] {r.name}" + (f" -- {r.detail}" if r.detail else ""))
    else:
        for r in report.failures:
            print(f"  [FAIL] {r.name} -- {r.detail}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
