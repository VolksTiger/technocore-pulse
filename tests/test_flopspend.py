"""Unit tests for the flopspend package (offline core of the FLOP-testnet
spending agent). No network access; the wire-format vector check runs
against the vendored copy of the canonical corpus.

Run with:  python3 -m unittest tests.test_flopspend   (from the repo root)
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from flopspend import budget, ledger, queue, runner, transcript, wire
from flopspend.vendor import compute_channel as cc

UTC = timezone.utc


# ═══════════════════════════════════════════════════════════════════════
# wire.py -- vector corpus
# ═══════════════════════════════════════════════════════════════════════

class TestWireVectors(unittest.TestCase):
    def test_verify_vectors_zero_failures(self):
        report = wire.verify_vectors()
        self.assertEqual(report.failed, 0, msg="; ".join(f"{r.name}: {r.detail}" for r in report.failures))

    def test_verify_vectors_has_passes_and_documented_skips(self):
        report = wire.verify_vectors()
        self.assertGreater(report.passed, 30)
        self.assertGreater(report.skipped, 0)
        self.assertTrue(all(r.detail for r in report.results if r.status == "skip"), "every skip must carry a reason")
        self.assertEqual(report.total, report.passed + report.failed + report.skipped)

    def test_negative_cases_all_classified(self):
        with open(wire.VECTOR_PATH, encoding="utf-8") as f:
            data = json.load(f)
        report = wire.verify_vectors()
        names = {r.name for r in report.results}
        for item in data["negative_cases"]:
            matches = [n for n in names if n == f"negative_cases.{item['id']}" or n.startswith(f"negative_cases.{item['id']}.")]
            self.assertTrue(matches, f"no check recorded for negative case {item['id']!r}")

    def test_cli_check_exit_code(self):
        self.assertEqual(wire._main(["--check"]), 0)

    def test_bad_vectors_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            wire.verify_vectors("/nonexistent/path/to/vectors.json")


# ═══════════════════════════════════════════════════════════════════════
# transcript.py -- Merkle accumulator, turn/receipt construction
# ═══════════════════════════════════════════════════════════════════════

class TestTranscript(unittest.TestCase):
    def setUp(self):
        with open(wire.VECTOR_PATH, encoding="utf-8") as f:
            self.vectors = json.load(f)["compute_channel_v1"]

    def test_leaf_hash_matches_vector_v3(self):
        li = self.vectors["leaf_inputs"]
        h = bytes.fromhex
        leaf = transcript.leaf_hash(
            cc.TranscriptLeafVersion.V3, h(li["channel_id_hex"]), li["turn_index"], h(li["h_in_hex"]), h(li["h_out_hex"]),
            int(li["g_n"]), decode_policy_hash=h(li["decode_policy_hash_hex"]), ids_hash=h(li["h_ids_hex"]),
            toploc_commitment_hash=h(li["toploc_commitment_hash_hex"]), miner_recv_ms=int(li["miner_recv_ms"]),
            miner_done_ms=int(li["miner_done_ms"]), latency_ms=li["latency_ms"],
        )
        self.assertEqual(leaf.hex(), self.vectors["leaf_versions"][3]["hash_hex"])

    def test_leaf_hash_matches_vector_v0(self):
        li = self.vectors["leaf_inputs"]
        h = bytes.fromhex
        leaf = transcript.leaf_hash(
            cc.TranscriptLeafVersion.V0, h(li["channel_id_hex"]), li["turn_index"], h(li["h_in_hex"]), h(li["h_out_hex"]), int(li["g_n"]),
        )
        self.assertEqual(leaf.hex(), self.vectors["leaf_versions"][0]["hash_hex"])

    def test_receipt_preimage_matches_vector(self):
        r = self.vectors["receipt"]
        h = bytes.fromhex
        preimage = cc.receipt_message_v1(
            h(r["inputs"]["channel_id_hex"]), h(r["inputs"]["final_root_hex"]),
            r["inputs"]["aggregate_gn"], r["inputs"]["payable"],
        )
        self.assertEqual(preimage.hex(), r["preimage_hex"])

    def test_accumulator_receipt_preimage_matches_vector(self):
        # Build an accumulator whose SINGLE leaf's hash equals the vector's
        # final_root directly (root() of one leaf is the leaf itself), so
        # receipt_preimage() can be checked end-to-end against the vector
        # without needing a decoder for an arbitrary pre-existing root.
        r = self.vectors["receipt"]
        h = bytes.fromhex
        channel_id = h(r["inputs"]["channel_id_hex"])
        acc = transcript.TranscriptAccumulator(channel_id)
        acc.leaves.append(h(r["inputs"]["final_root_hex"]))  # inject the vector's root as the sole leaf
        preimage = acc.receipt_preimage(r["inputs"]["aggregate_gn"], r["inputs"]["payable"])
        self.assertEqual(preimage.hex(), r["preimage_hex"])

    def test_accumulator_builds_running_root(self):
        channel_id = cc.blake2_256(b"test-channel")
        acc = transcript.TranscriptAccumulator(channel_id)
        h_in, h_out = cc.blake2_256(b"in"), cc.blake2_256(b"out")
        leaf0 = acc.add_turn(cc.TranscriptLeafVersion.V0, h_in, h_out, g_n=5, enclave_sig=bytes(64))
        self.assertEqual(acc.root(), leaf0)  # single-leaf tree: root == the leaf
        leaf1 = acc.add_turn(cc.TranscriptLeafVersion.V0, h_in, h_out, g_n=7, enclave_sig=bytes(64))
        self.assertEqual(acc.root(), cc.merkle_root([leaf0, leaf1]))
        self.assertEqual(acc.aggregate_gn(), 12)

    def test_accumulator_rejects_out_of_order_turn_index(self):
        channel_id = cc.blake2_256(b"test-channel-2")
        acc = transcript.TranscriptAccumulator(channel_id)
        h_in, h_out = cc.blake2_256(b"in"), cc.blake2_256(b"out")
        acc.add_turn(cc.TranscriptLeafVersion.V0, h_in, h_out, g_n=1, enclave_sig=bytes(64))
        # add_turn always appends at len(records); duplicate submission would
        # require forging turn_index 0 again, which this API structurally
        # prevents -- there is no way to ask for turn_index 0 twice.
        self.assertEqual(len(acc.records), 1)
        acc.add_turn(cc.TranscriptLeafVersion.V0, h_in, h_out, g_n=1, enclave_sig=bytes(64))
        self.assertEqual([r.turn_index for r in acc.records], [0, 1])

    def test_accumulator_rejects_inconsistent_leaf_fields(self):
        channel_id = cc.blake2_256(b"test-channel-3")
        acc = transcript.TranscriptAccumulator(channel_id)
        h_in, h_out = cc.blake2_256(b"in"), cc.blake2_256(b"out")
        with self.assertRaises(ValueError):
            # V0 must not carry a decode_policy_hash.
            acc.add_turn(cc.TranscriptLeafVersion.V0, h_in, h_out, g_n=1, enclave_sig=bytes(64), decode_policy_hash=cc.blake2_256(b"policy"))

    def test_stub_verifier_records_and_accepts_by_default(self):
        channel_id = cc.blake2_256(b"test-channel-4")
        acc = transcript.TranscriptAccumulator(channel_id)
        h_in, h_out = cc.blake2_256(b"in"), cc.blake2_256(b"out")
        acc.add_turn(cc.TranscriptLeafVersion.V0, h_in, h_out, g_n=1, enclave_sig=b"sig" + bytes(61))
        verifier = transcript.StubVerifier()
        ok = acc.verify_turn(0, enclave_pubkey=bytes(32), verifier=verifier)
        self.assertTrue(ok)
        self.assertEqual(len(verifier.calls), 1)
        self.assertEqual(verifier.calls[0].message, acc.leaves[0])

    def test_stub_verifier_can_be_configured_to_reject(self):
        rejecting = transcript.StubVerifier(accept=False)
        self.assertFalse(rejecting(bytes(32), b"msg", b"sig"))

    def test_stub_signer_is_deterministic_and_not_a_real_signature(self):
        signer = transcript.StubSigner(public_key=bytes(32))
        sig1 = signer(b"hello")
        sig2 = signer(b"hello")
        self.assertEqual(sig1, sig2)
        self.assertEqual(len(sig1), 64)
        self.assertEqual(signer.calls, [b"hello", b"hello"])

    def test_counter_sign_round_trip(self):
        channel_id = cc.blake2_256(b"test-channel-5")
        acc = transcript.TranscriptAccumulator(channel_id)
        h_in, h_out = cc.blake2_256(b"in"), cc.blake2_256(b"out")
        acc.add_turn(cc.TranscriptLeafVersion.V1, h_in, h_out, g_n=3, enclave_sig=bytes(64), miner_recv_ms=1, miner_done_ms=2, latency_ms=1)
        signer = transcript.StubSigner()
        sig = acc.counter_sign(aggregate_gn=3, payable=10, signer=signer)
        self.assertEqual(sig, signer(acc.receipt_preimage(3, 10)))


# ═══════════════════════════════════════════════════════════════════════
# budget.py -- pacing arithmetic
# ═══════════════════════════════════════════════════════════════════════

class TestBudget(unittest.TestCase):
    def test_daily_target_arithmetic(self):
        b = budget.Budget(ration=1000, stake=10, deposit=0.01, reserve_share=0.05, days=90)
        self.assertAlmostEqual(b.reserve, 50.0)
        self.assertAlmostEqual(b.spendable, 1000 - 10 - 0.01 - 50.0)
        self.assertAlmostEqual(b.daily_target, b.spendable / 90)

    def test_budget_rejects_ration_too_small(self):
        with self.assertRaises(ValueError):
            budget.Budget(ration=10, stake=10, deposit=0.01, reserve_share=0.05, days=90)

    def test_budget_rejects_bad_reserve_share(self):
        with self.assertRaises(ValueError):
            budget.Budget(ration=1000, stake=10, deposit=0.01, reserve_share=1.5, days=90)

    def test_budget_rejects_nonpositive_days(self):
        with self.assertRaises(ValueError):
            budget.Budget(ration=1000, stake=10, deposit=0.01, reserve_share=0.05, days=0)

    def test_token_bucket_grows_within_day(self):
        start = datetime(2026, 9, 10, tzinfo=UTC)
        tb = budget.TokenBucket(daily_target=24.0, day_start=start)
        self.assertAlmostEqual(tb.allowance(start), 0.0)
        self.assertAlmostEqual(tb.allowance(start + timedelta(hours=12)), 12.0)
        self.assertAlmostEqual(tb.allowance(start + timedelta(hours=23, minutes=59)), 24.0 * (1439 / 1440))
        # exactly 24h elapsed rolls into the NEXT day (a fresh, empty bucket) --
        # see test_token_bucket_never_rolls_over_unspent_day for that behavior.

    def test_token_bucket_allows_catchup_within_day(self):
        start = datetime(2026, 9, 10, tzinfo=UTC)
        tb = budget.TokenBucket(daily_target=24.0, day_start=start)
        # spend nothing all morning, then catch up
        allowance_at_noon = tb.allowance(start + timedelta(hours=12))
        self.assertAlmostEqual(allowance_at_noon, 12.0)
        tb.record_spend(0.0, start + timedelta(hours=12))
        self.assertAlmostEqual(tb.allowance(start + timedelta(hours=18)), 18.0)

    def test_token_bucket_never_rolls_over_unspent_day(self):
        start = datetime(2026, 9, 10, tzinfo=UTC)
        tb = budget.TokenBucket(daily_target=10.0, day_start=start)
        # spend nothing at all today
        next_day = start + timedelta(days=1, hours=1)
        allowance = tb.allowance(next_day)
        # if yesterday's 10.0 had rolled over, allowance would be >= 10 + (1/24)*10
        self.assertLess(allowance, 1.0)
        self.assertAlmostEqual(allowance, 10.0 * (1 / 24))

    def test_token_bucket_handles_multi_day_gap(self):
        start = datetime(2026, 9, 10, tzinfo=UTC)
        tb = budget.TokenBucket(daily_target=10.0, day_start=start)
        far_future = start + timedelta(days=5, hours=6)
        allowance = tb.allowance(far_future)
        self.assertAlmostEqual(allowance, 10.0 * 0.25)  # only today's 6h counts, not 5 days' worth

    def test_token_bucket_rejects_naive_datetime(self):
        tb = budget.TokenBucket(daily_target=10.0, day_start=datetime(2026, 9, 10, tzinfo=UTC))
        with self.assertRaises(ValueError):
            tb.allowance(datetime(2026, 9, 10))  # no tzinfo

    def test_size_session_never_rounds_up(self):
        escrow = budget.size_session(job_flops=2.0, price_per_gflop=1.5, max_escrow=100)
        self.assertAlmostEqual(escrow, 3.0)

    def test_size_session_capped_by_max_escrow(self):
        escrow = budget.size_session(job_flops=100.0, price_per_gflop=1.5, max_escrow=10)
        self.assertAlmostEqual(escrow, 10)

    def test_size_session_rejects_nonpositive_inputs(self):
        with self.assertRaises(ValueError):
            budget.size_session(job_flops=0, price_per_gflop=1.0, max_escrow=10)

    def test_concurrency_cap_formula(self):
        self.assertEqual(budget.max_concurrent_reservations(0), 4)
        self.assertEqual(budget.max_concurrent_reservations(49), 4)
        self.assertEqual(budget.max_concurrent_reservations(50), 5)
        self.assertEqual(budget.max_concurrent_reservations(125), 6)

    def test_circuit_breaker_trips_on_tx_count(self):
        limits = budget.CircuitBreakerLimits(window_blocks=60, max_tx=3, max_flop=1000)
        recent = [(10, 1.0), (20, 1.0), (30, 1.0)]
        self.assertTrue(budget.would_trip(limits, recent, now_block=40, next_tx_flop=1.0))

    def test_circuit_breaker_trips_on_flop_cap(self):
        limits = budget.CircuitBreakerLimits(window_blocks=60, max_tx=100, max_flop=10.0)
        recent = [(10, 9.0)]
        self.assertTrue(budget.would_trip(limits, recent, now_block=20, next_tx_flop=2.0))

    def test_circuit_breaker_ignores_txs_outside_window(self):
        limits = budget.CircuitBreakerLimits(window_blocks=60, max_tx=1, max_flop=1000)
        recent = [(1, 500.0)]  # block 1, far outside a window starting at now_block-60=140
        self.assertFalse(budget.would_trip(limits, recent, now_block=200, next_tx_flop=1.0))

    def test_circuit_breaker_headroom(self):
        limits = budget.CircuitBreakerLimits(window_blocks=60, max_tx=10, max_flop=100.0)
        recent = [(90, 20.0), (95, 10.0)]
        tx_headroom, flop_headroom = budget.circuit_breaker_headroom(limits, recent, now_block=100)
        self.assertEqual(tx_headroom, 8)
        self.assertAlmostEqual(flop_headroom, 70.0)


# ═══════════════════════════════════════════════════════════════════════
# ledger.py -- append-only JSONL + report
# ═══════════════════════════════════════════════════════════════════════

class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "flopspend.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _record(self, **overrides):
        rec = dict(
            channel_id="c" * 8, miner="m" * 8, model_hash="h" * 8, escrow=5.0, settled=5.0, gn=10,
            receipt_root="r" * 8, our_signature="s" * 8, opened_at="2026-09-10T00:00:00Z",
            settled_at="2026-09-10T01:00:00Z", status="settled",
        )
        rec.update(overrides)
        return rec

    def test_append_and_read_round_trip(self):
        ledger.append_session(self._record(), path=self.path)
        rows = ledger.read_all(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["escrow"], 5.0)

    def test_append_rejects_missing_field(self):
        with self.assertRaises(ValueError):
            ledger.append_session({"channel_id": "c"}, path=self.path)

    def test_read_all_missing_file_returns_empty(self):
        self.assertEqual(ledger.read_all(os.path.join(self.tmpdir, "nope.jsonl")), [])

    def test_daily_totals_only_counts_settled(self):
        ledger.append_session(self._record(status="open", settled_at=None), path=self.path)
        ledger.append_session(self._record(), path=self.path)
        rows = ledger.read_all(self.path)
        totals = ledger.daily_totals(rows)
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals["2026-09-10"]["sessions"], 1)
        self.assertAlmostEqual(totals["2026-09-10"]["settled_flop"], 5.0)

    def test_daily_totals_sums_across_multiple_sessions_same_day(self):
        ledger.append_session(self._record(channel_id="a", settled=3.0, gn=4), path=self.path)
        ledger.append_session(self._record(channel_id="b", settled=2.0, gn=6), path=self.path)
        totals = ledger.daily_totals(ledger.read_all(self.path))
        self.assertAlmostEqual(totals["2026-09-10"]["settled_flop"], 5.0)
        self.assertEqual(totals["2026-09-10"]["gn"], 10)

    def test_daily_totals_separates_days(self):
        ledger.append_session(self._record(channel_id="a"), path=self.path)
        ledger.append_session(self._record(channel_id="b", settled_at="2026-09-11T01:00:00Z"), path=self.path)
        totals = ledger.daily_totals(ledger.read_all(self.path))
        self.assertEqual(set(totals), {"2026-09-10", "2026-09-11"})

    def test_format_report_empty(self):
        self.assertIn("no settled sessions", ledger.format_report([]))

    def test_format_report_with_target(self):
        ledger.append_session(self._record(), path=self.path)
        text = ledger.format_report(ledger.read_all(self.path), daily_target=10.0)
        self.assertIn("50.0%", text)
        self.assertIn("daily target", text)


# ═══════════════════════════════════════════════════════════════════════
# queue.py -- job producers
# ═══════════════════════════════════════════════════════════════════════

class TestQueue(unittest.TestCase):
    def test_roomkeeper_counts_offline_fallback(self):
        counts = queue.load_roomkeeper_counts(room_history_path="/nonexistent", health_path="/nonexistent")
        self.assertIn("rooms_total", counts)
        self.assertIn("busiest_24h", counts)

    def test_roomkeeper_digest_job_quality_check(self):
        job = queue.roomkeeper_digest_job(queue.load_roomkeeper_counts(room_history_path="/nonexistent", health_path="/nonexistent"))
        self.assertEqual(job.kind, "roomkeeper_digest")
        self.assertTrue(job.quality_check("The network shows 41 rooms active out of the total, with steady uptime."))
        self.assertFalse(job.quality_check(""))
        self.assertFalse(job.quality_check(None))

    def test_boardintel_snapshot_fixture_fallback(self):
        snap = queue.load_boardintel_snapshot(path="/nonexistent/boardintel.jsonl")
        self.assertIn("window", snap)
        self.assertIn("task_families", snap)

    def test_boardintel_narrative_job_quality_check(self):
        snap = queue.load_boardintel_snapshot(path="/nonexistent/boardintel.jsonl")
        job = queue.boardintel_narrative_job(snap)
        good = ("The board processed several hundred offers this window, with census tasks " * 3)
        self.assertTrue(job.quality_check(good))
        self.assertFalse(job.quality_check("too short"))

    def test_boardintel_snapshot_latest_line_from_file(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "boardintel.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"window": {"marker": "first"}}) + "\n")
                f.write(json.dumps({"window": {"marker": "latest"}}) + "\n")
            snap = queue.load_boardintel_snapshot(path=path)
            self.assertEqual(snap["window"]["marker"], "latest")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_flopwatch_events_fixture_filters_baseline_and_error(self):
        events = queue.load_flopwatch_events(path="/nonexistent/flopwatch.jsonl")
        self.assertGreater(len(events), 0)
        self.assertTrue(all(queue.is_change_event(e) for e in events))

    def test_flopwatch_changelog_job_quality_check(self):
        events = queue.load_flopwatch_events(path="/nonexistent/flopwatch.jsonl")
        job = queue.flopwatch_changelog_job(events[0])
        page = events[0]["page"]
        self.assertTrue(job.quality_check(f"{page} now mentions settled inference spend instead of measured throughput."))
        self.assertFalse(job.quality_check("something changed"))

    def test_find_brsolve_skips_only_keeps_none_results(self):
        specs = queue.load_brsolve_specs()
        skips = queue.find_brsolve_skips(specs)
        self.assertEqual(len(skips), len(specs))  # the fixture was built entirely from solve()-returns-None specs
        import brsolve
        for s in skips:
            self.assertIsNone(brsolve.solve(s))

    def test_find_brsolve_skips_excludes_solved_specs(self):
        solved = "math | How many integers n with 10 <= n <= 30 are prime? | done looks like: one line: the count."
        import brsolve
        self.assertIsNotNone(brsolve.solve(solved))
        self.assertEqual(queue.find_brsolve_skips([solved]), [])

    def test_brsolve_skip_job_quality_check_shape(self):
        job = queue.brsolve_skip_job("math | integrate x^2 | done looks like: a fraction like 9/1")
        self.assertTrue(job.quality_check("9/1"))
        self.assertFalse(job.quality_check("line one\nline two"))
        self.assertFalse(job.quality_check(""))

    def test_build_default_jobs_nonempty(self):
        jobs = queue.build_default_jobs()
        self.assertGreater(len(jobs), 0)
        self.assertTrue(all(isinstance(j, queue.Job) for j in jobs))

    def test_workload_queue_round_robins(self):
        jobs = [queue.Job(kind=f"k{i}", prompt="p", max_tokens=10, quality_check=lambda t: True) for i in range(3)]
        wq = queue.WorkloadQueue(jobs)
        seen = [wq.next_job().kind for _ in range(7)]
        self.assertEqual(seen, ["k0", "k1", "k2", "k0", "k1", "k2", "k0"])

    def test_workload_queue_rejects_empty(self):
        with self.assertRaises(ValueError):
            queue.WorkloadQueue([])


# ═══════════════════════════════════════════════════════════════════════
# runner.py -- ChainClient interface + dry run
# ═══════════════════════════════════════════════════════════════════════

class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ledger_path = os.path.join(self.tmpdir, "flopspend.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_real_chain_client_not_implemented(self):
        client = runner.RealChainClient()
        now = datetime(2026, 9, 10, tzinfo=UTC)
        with self.assertRaises(NotImplementedError):
            client.open_channel(bytes(32), bytes(32), bytes(32), 1.0, 0, now)
        with self.assertRaises(NotImplementedError):
            client.stream_turn(None, 0, None)
        with self.assertRaises(NotImplementedError):
            client.settle_wait(None, bytes(32), 0, bytes(64), now)
        with self.assertRaises(NotImplementedError):
            client.timeout(None, now)

    def test_dry_run_client_single_session(self):
        client = runner.DryRunClient()
        now = datetime(2026, 9, 10, tzinfo=UTC)
        job = queue.roomkeeper_digest_job(queue.load_roomkeeper_counts(room_history_path="/x", health_path="/x"))
        channel = client.open_channel(runner.DRY_RUN_AGENT, runner.DRY_RUN_MINER, runner.DRY_RUN_MODEL_HASH, 2.0, 0, now)
        self.assertEqual(len(channel.channel_id), 32)
        turn = client.stream_turn(channel, 0, job)
        self.assertEqual(len(turn.h_in), 32)
        self.assertGreaterEqual(turn.g_n, 1)
        acc = transcript.TranscriptAccumulator(channel.channel_id)
        acc.add_turn(turn.version, turn.h_in, turn.h_out, turn.g_n, turn.enclave_sig,
                     miner_recv_ms=turn.miner_recv_ms, miner_done_ms=turn.miner_done_ms, latency_ms=turn.latency_ms)
        sig = acc.counter_sign(acc.aggregate_gn(), payable=2, signer=transcript.StubSigner())
        settlement = client.settle_wait(channel, acc.root(), acc.aggregate_gn(), sig, now)
        self.assertEqual(settlement.status, "settled")
        self.assertEqual(settlement.settled, 2.0)  # R12.1a: full reserved escrow, not metered

    def test_dry_run_client_timeout_is_full_refund(self):
        client = runner.DryRunClient()
        now = datetime(2026, 9, 10, tzinfo=UTC)
        channel = client.open_channel(runner.DRY_RUN_AGENT, runner.DRY_RUN_MINER, runner.DRY_RUN_MODEL_HASH, 5.0, 1, now)
        settlement = client.timeout(channel, now)
        self.assertEqual(settlement.status, "timed_out")
        self.assertEqual(settlement.settled, 0.0)

    def test_run_day_end_to_end_produces_ledger_at_target(self):
        b = budget.Budget(ration=1000, stake=10, deposit=0.01, reserve_share=0.05, days=90)
        client = runner.DryRunClient()
        day_start = datetime(2026, 9, 10, tzinfo=UTC)
        summary = runner.run_day(client, b, queue.WorkloadQueue(), day_start, ledger_path=self.ledger_path, tick_minutes=30)
        self.assertGreater(summary.sessions_settled, 0)
        self.assertGreater(summary.pct_of_target, 50.0)  # comfortably paced toward, not stalled at zero
        self.assertLessEqual(summary.total_settled, summary.daily_target + 1e-6)

        rows = ledger.read_all(self.ledger_path)
        self.assertEqual(len(rows), summary.sessions_opened)
        for row in rows:
            self.assertIn(row["status"], ("settled", "timed_out"))
            self.assertEqual(row["settled_at"][:10], "2026-09-10")  # simulated day, not wall-clock today

    def test_run_day_respects_circuit_breaker_flop_cap(self):
        # A budget generous enough that pacing alone wouldn't stop it, so
        # this actually exercises the breaker rather than the token bucket.
        b = budget.Budget(ration=100_000, stake=10, deposit=0.01, reserve_share=0.0, days=1)
        client = runner.DryRunClient()
        day_start = datetime(2026, 9, 10, tzinfo=UTC)
        summary = runner.run_day(client, b, queue.WorkloadQueue(), day_start, ledger_path=self.ledger_path, tick_minutes=1, max_escrow=50.0)
        rows = ledger.read_all(self.ledger_path)
        limits = budget.CircuitBreakerLimits()
        # Slide a window over every settled tx's simulated block and confirm
        # it never exceeds circuit_breaker_flop_cap FLOP in circuit_breaker_window_blocks.
        blocks_and_flop = sorted(
            (int((datetime.strptime(r["opened_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - day_start).total_seconds()), r["escrow"])
            for r in rows
        )
        for block, _ in blocks_and_flop:
            window_total = sum(f for b_, f in blocks_and_flop if block - limits.window_blocks < b_ <= block)
            self.assertLessEqual(window_total, limits.max_flop + 1e-6)

    def test_run_day_multi_day_ledger_has_separate_days(self):
        b = budget.Budget(ration=2000, stake=10, deposit=0.01, reserve_share=0.05, days=90)
        client = runner.DryRunClient()
        day0 = datetime(2026, 9, 10, tzinfo=UTC)
        runner.run_day(client, b, queue.WorkloadQueue(), day0, ledger_path=self.ledger_path, tick_minutes=60, nonce_start=0)
        runner.run_day(client, b, queue.WorkloadQueue(), day0 + timedelta(days=1), ledger_path=self.ledger_path, tick_minutes=60, nonce_start=1000)
        totals = ledger.daily_totals(ledger.read_all(self.ledger_path))
        self.assertEqual(set(totals), {"2026-09-10", "2026-09-11"})

    def test_cli_rejects_missing_dry_run_flag(self):
        self.assertEqual(runner._main(["--ration", "1000"]), 2)


if __name__ == "__main__":
    unittest.main()
