"""The loop skeleton from SPEND-PLAN.md ("The runner"), with every chain call
behind `ChainClient` so the whole day-one flow is testable before the FLOP
testnet RPC exists.

  ChainClient      -- open_channel / stream_turn / settle_wait / timeout
  RealChainClient  -- every method raises NotImplementedError; this is the
                       only honest thing to ship before the chain spec is
                       public. See its docstring.
  DryRunClient     -- the only WORKING implementation. Simulates a
                       cooperative miner (and a rubber-stamp chain) entirely
                       offline: it signs turns with `transcript.StubSigner`
                       (not real cryptography, see transcript.py), estimates
                       G_n with a synthetic proxy (NOT R4.2's flop_meter,
                       which this repo doesn't have), and settles the FULL
                       reserved escrow per R12.1a. Good enough to exercise
                       every other module end-to-end; not a model of the
                       real network.

`run_day()` compresses one simulated 24h day into a virtual clock (no real
sleeping) so `--dry-run --days 1` finishes in seconds. What it actually
exercises, faithfully: Budget/TokenBucket pacing (budget.py), the
concurrency cap and circuit-breaker headroom (budget.py, §12.2/§6.2), the
transcript accumulator and receipt counter-signing (transcript.py), and the
append-only ledger (ledger.py). What it does NOT model: real network
latency, a real miner's behavior, real G_n accounting, or real signatures.

CLI:  python3 -m flopspend.runner --dry-run --days 1 --ration 1000
"""

from __future__ import annotations

import abc
import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from flopspend import budget as bmod
from flopspend import ledger as lmod
from flopspend import transcript as tmod
from flopspend.queue import Job, WorkloadQueue
from flopspend.vendor import compute_channel as cc

# Fixed, deterministic test identities for the dry run -- NOT real accounts,
# NOT derived from any real chain's genesis. Any run produces the same
# channel ids for the same nonce sequence, which makes the ledger output
# reproducible and easy to assert on in tests.
DRY_RUN_GENESIS = cc.blake2_256(b"flopspend-dry-run-genesis")
DRY_RUN_AGENT = cc.blake2_256(b"flopspend-dry-run-agent")
DRY_RUN_MINER = cc.blake2_256(b"flopspend-dry-run-miner")
DRY_RUN_MODEL_HASH = cc.blake2_256(b"flopspend-dry-run-model")

# Synthetic G_n proxy for the dry run ONLY: real reference-work accounting is
# R4.2's hp_poui::flop_meter, a deterministic formula over prompt/context
# length, generated-token count, active-parameter/MoE terms and the
# attention term -- not reproduced here (no published implementation to
# check against offline). This proxy exists only so size_session() and the
# ledger have SOME G_n-shaped number to pace against in a dry run.
_SYNTHETIC_GFLOP_PER_CHAR = 0.02


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class OpenChannel:
    channel_id: bytes
    agent: bytes
    miner: bytes
    model_hash: bytes
    escrow: float
    nonce: int
    opened_at: str


@dataclass(frozen=True)
class TurnResult:
    version: "cc.TranscriptLeafVersion"
    h_in: bytes
    h_out: bytes
    g_n: int
    enclave_sig: bytes
    miner_recv_ms: int
    miner_done_ms: int
    latency_ms: int
    output_text: str
    quality_passed: bool


@dataclass(frozen=True)
class Settlement:
    settled: float
    gn: int
    receipt_root: "Optional[bytes]"
    our_signature: "Optional[bytes]"
    settled_at: str
    status: str  # "settled" | "timed_out"


class ChainClient(abc.ABC):
    """The chain-call surface the runner needs. Turn signature verification
    and receipt counter-signing are deliberately NOT part of this interface
    -- those are agent-side steps (R12.1b) that run_day() performs itself via
    transcript.TranscriptAccumulator, exactly as a real agent would, whether
    or not the chain call underneath it is real."""

    @abc.abstractmethod
    def open_channel(self, agent: bytes, miner: bytes, model_hash: bytes, escrow: float, nonce: int, now: datetime) -> OpenChannel:
        ...

    @abc.abstractmethod
    def stream_turn(self, channel: OpenChannel, turn_index: int, job: Job) -> TurnResult:
        ...

    @abc.abstractmethod
    def settle_wait(self, channel: OpenChannel, final_root: bytes, aggregate_gn: int, agent_receipt_sig: bytes, now: datetime) -> Settlement:
        ...

    @abc.abstractmethod
    def timeout(self, channel: OpenChannel, now: datetime) -> Settlement:
        ...


class RealChainClient(ChainClient):
    """Not implemented. Needs the published chain spec + py-substrate-interface."""

    _NOTE = "needs the published chain spec + py-substrate-interface"

    def open_channel(self, agent, miner, model_hash, escrow, nonce, now):  # noqa: D102
        raise NotImplementedError(self._NOTE)

    def stream_turn(self, channel, turn_index, job):  # noqa: D102
        raise NotImplementedError(self._NOTE)

    def settle_wait(self, channel, final_root, aggregate_gn, agent_receipt_sig, now):  # noqa: D102
        raise NotImplementedError(self._NOTE)

    def timeout(self, channel, now):  # noqa: D102
        raise NotImplementedError(self._NOTE)


class DryRunClient(ChainClient):
    """Simulates ONE cooperative miner and a chain that always accepts a
    well-formed settle -- entirely offline, entirely deterministic given a
    fixed genesis/keys. Every session opened through this client is
    single-turn (SPEND-PLAN.md: "small, frequent sessions beat large ones")
    and settles cooperatively for the full reserved escrow (R12.1a)."""

    def __init__(self, genesis_hash: bytes = DRY_RUN_GENESIS) -> None:
        self.genesis_hash = genesis_hash
        self.enclave_signer = tmod.StubSigner(public_key=DRY_RUN_MINER)
        self._clock_ms = 0

    def open_channel(self, agent: bytes, miner: bytes, model_hash: bytes, escrow: float, nonce: int, now: datetime) -> OpenChannel:
        channel_id = cc.channel_id_v1(self.genesis_hash, agent, miner, nonce)
        return OpenChannel(channel_id, agent, miner, model_hash, escrow, nonce, _iso(now))

    def _simulate_output(self, job: Job, prompt: str) -> str:
        """Try a few plausible synthetic "miner completions" and keep the
        first that satisfies the job's OWN quality_check -- "the check is
        ours ... run before the output is used" (SPEND-PLAN.md). This is a
        plumbing double, not a model: it never calls out anywhere."""
        words = prompt.split()
        candidates = [
            " ".join(words[-8:]),
            " ".join(words[:40]) + f" -- simulated completion, dry run, target {job.max_tokens} tokens.",
            prompt[:600],
        ]
        for candidate in candidates:
            if job.quality_check(candidate):
                return candidate
        return candidates[-1]

    def stream_turn(self, channel: OpenChannel, turn_index: int, job: Job) -> TurnResult:
        output = self._simulate_output(job, job.prompt)
        h_in = cc.blake2_256(job.prompt.encode("utf-8"))
        h_out = cc.blake2_256(output.encode("utf-8"))
        g_n = max(1, int((len(job.prompt) + len(output)) * _SYNTHETIC_GFLOP_PER_CHAR))

        self._clock_ms += 5
        recv_ms = self._clock_ms
        self._clock_ms += 10 + min(len(output), 200)
        done_ms = self._clock_ms
        latency_ms = done_ms - recv_ms

        # V1: legacy leaf (no decode-policy/TOPLOC binding claimed) -- this
        # simulator does not model a real hp_poui::DecodePolicy, so it never
        # claims the V2/V3 evidence those require.
        version = cc.TranscriptLeafVersion.V1
        leaf = tmod.leaf_hash(
            version, channel.channel_id, turn_index, h_in, h_out, g_n,
            miner_recv_ms=recv_ms, miner_done_ms=done_ms, latency_ms=latency_ms,
        )
        enclave_sig = self.enclave_signer(leaf)
        return TurnResult(
            version, h_in, h_out, g_n, enclave_sig, recv_ms, done_ms, latency_ms,
            output, job.quality_check(output),
        )

    def settle_wait(self, channel: OpenChannel, final_root: bytes, aggregate_gn: int, agent_receipt_sig: bytes, now: datetime) -> Settlement:
        # R12.1a: settle pays the reserved escrow IN FULL on a cooperative
        # close, regardless of metered use.
        return Settlement(
            settled=channel.escrow, gn=aggregate_gn, receipt_root=final_root,
            our_signature=agent_receipt_sig, settled_at=_iso(now), status="settled",
        )

    def timeout(self, channel: OpenChannel, now: datetime) -> Settlement:
        # R12.1f: non-delivery timeout -> full refund (phi = 0).
        return Settlement(settled=0.0, gn=0, receipt_root=None, our_signature=None, settled_at=_iso(now), status="timed_out")


@dataclass
class DaySummary:
    day: str
    sessions_opened: int = 0
    sessions_settled: int = 0
    total_escrowed: float = 0.0
    total_settled: float = 0.0
    total_gn: int = 0
    daily_target: float = 0.0

    @property
    def pct_of_target(self) -> float:
        return 100.0 * self.total_settled / self.daily_target if self.daily_target else 0.0


def _job_flops_estimate(job: Job) -> float:
    """Synthetic proxy for the G_n a job is expected to cost, used only to
    size escrow before the session runs (the real settled G_n comes back
    from stream_turn / the accumulator afterwards). See the module-level
    note on _SYNTHETIC_GFLOP_PER_CHAR -- this is not R4.2 accounting."""
    return max(1.0, len(job.prompt) * _SYNTHETIC_GFLOP_PER_CHAR + job.max_tokens * _SYNTHETIC_GFLOP_PER_CHAR * 4)


def run_day(
    client: ChainClient,
    budget: bmod.Budget,
    queue: WorkloadQueue,
    day_start: datetime,
    ledger_path: "Optional[str]" = None,
    price_per_gflop: float = 1.0,
    tick_minutes: float = 15.0,
    max_escrow: "Optional[float]" = None,
    nonce_start: int = 0,
    agent: bytes = DRY_RUN_AGENT,
    miner: bytes = DRY_RUN_MINER,
    model_hash: bytes = DRY_RUN_MODEL_HASH,
    agent_verifier: "Optional[tmod.SignatureVerifier]" = None,
    agent_signer: "Optional[tmod.Signer]" = None,
) -> DaySummary:
    """Simulate one 24h day over a virtual clock (`day_start` + tick_minutes
    steps -- no real sleeping), pacing spend through `budget`'s TokenBucket,
    respecting the §12.2 reservation cap and the §6.2 circuit breaker, and
    appending every session to the ledger at `ledger_path`."""
    if max_escrow is None:
        max_escrow = min(bmod.AGENT_PER_TX_LIMIT, budget.daily_target)
    bucket = bmod.TokenBucket(daily_target=budget.daily_target, day_start=day_start)
    breaker_limits = bmod.CircuitBreakerLimits()
    recent_txs: "List[tuple]" = []  # (block, flop) within the trailing circuit-breaker window
    escrowed_open = 0.0  # escrow currently reserved by sessions this tick treats as "in flight"

    summary = DaySummary(day=day_start.strftime("%Y-%m-%d"), daily_target=budget.daily_target)
    nonce = nonce_start
    ticks = int(round(1440 / tick_minutes))

    for tick in range(ticks):
        now = day_start + timedelta(minutes=tick * tick_minutes)
        now_block = int((now - day_start).total_seconds())  # 1 simulated block/second, §6.3's 1s target cadence

        allowance = bucket.allowance(now)
        if allowance <= 0:
            continue

        cap = bmod.max_concurrent_reservations(escrowed_open)
        opened_this_tick = 0
        while allowance > 0 and opened_this_tick < cap:
            job = queue.next_job()
            job_flops = _job_flops_estimate(job)
            escrow_raw = bmod.size_session(job_flops, price_per_gflop, max_escrow=min(max_escrow, allowance))
            # Wire-level escrow/payable amounts are u128 (SCALE fixed-width),
            # i.e. an integer count of the chain's FLOP subunit -- round here,
            # at the chain-interfacing boundary; budget.py itself stays pure
            # float arithmetic (pacing math, not wire encoding).
            escrow = max(1, round(escrow_raw))
            if escrow > allowance + 1e-9:
                break  # rounding pushed it over what's left in today's allowance
            if bmod.would_trip(breaker_limits, recent_txs, now_block, escrow):
                break  # stay inside the circuit breaker; try again next tick

            channel = client.open_channel(agent, miner, model_hash, escrow, nonce, now)
            nonce += 1
            summary.sessions_opened += 1
            summary.total_escrowed += escrow
            escrowed_open += escrow
            opened_this_tick += 1

            accumulator = tmod.TranscriptAccumulator(channel.channel_id)
            turn = client.stream_turn(channel, 0, job)
            accumulator.add_turn(
                turn.version, turn.h_in, turn.h_out, turn.g_n, turn.enclave_sig,
                miner_recv_ms=turn.miner_recv_ms, miner_done_ms=turn.miner_done_ms, latency_ms=turn.latency_ms,
            )
            accumulator.verify_turn(0, miner, verifier=agent_verifier)  # recorded, see transcript.py on why this doesn't cryptographically verify

            aggregate_gn = accumulator.aggregate_gn()
            receipt_sig = accumulator.counter_sign(aggregate_gn, payable=escrow, signer=agent_signer)
            settlement = client.settle_wait(channel, accumulator.root(), aggregate_gn, receipt_sig, now)

            lmod.append_session(
                {
                    "channel_id": channel.channel_id.hex(), "miner": miner.hex(), "model_hash": model_hash.hex(),
                    "escrow": escrow, "settled": settlement.settled, "gn": settlement.gn,
                    "receipt_root": settlement.receipt_root.hex() if settlement.receipt_root else None,
                    "our_signature": settlement.our_signature.hex() if settlement.our_signature else None,
                    "opened_at": channel.opened_at, "settled_at": settlement.settled_at, "status": settlement.status,
                },
                path=ledger_path,
            )

            recent_txs.append((now_block, escrow))
            escrowed_open = max(0.0, escrowed_open - escrow)  # settled synchronously within the same tick
            if settlement.status == "settled":
                summary.sessions_settled += 1
                summary.total_settled += settlement.settled
                summary.total_gn += settlement.gn
                bucket.record_spend(settlement.settled, now)

            allowance = bucket.allowance(now)

    return summary


def _parse_args(argv: "Optional[List[str]]") -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python3 -m flopspend.runner")
    ap.add_argument("--dry-run", action="store_true", help="use DryRunClient (the only implemented client)")
    ap.add_argument("--days", type=int, default=1, help="number of simulated days to run")
    ap.add_argument("--ration", type=float, required=True, help="faucet ration (FLOP)")
    ap.add_argument("--stake", type=float, default=10.0, help="agent_identity_min_stake reserved (FLOP)")
    ap.add_argument("--deposit", type=float, default=0.01, help="existential deposit reserved (FLOP)")
    ap.add_argument("--reserve-share", type=float, default=0.05, help="fraction of ration held back (0.05 = 5%%)")
    ap.add_argument("--testnet-days", type=int, default=90, help="days left in the testnet, for the daily target")
    ap.add_argument("--tick-minutes", type=float, default=15.0, help="simulated minutes per loop tick")
    ap.add_argument("--price-per-gflop", type=float, default=1.0, help="synthetic price used by size_session")
    ap.add_argument("--ledger-path", default=None, help="ledger path (default: ~/.technocore-pulse/flopspend.jsonl)")
    ap.add_argument("--start", default=None, help="ISO date (UTC) for day 0, default: today")
    return ap.parse_args(argv)


def _main(argv: "Optional[List[str]]" = None) -> int:
    args = _parse_args(argv)
    if not args.dry_run:
        print("only --dry-run is implemented; RealChainClient " + RealChainClient._NOTE, file=sys.stderr)
        return 2

    budget = bmod.Budget(
        ration=args.ration, stake=args.stake, deposit=args.deposit,
        reserve_share=args.reserve_share, days=args.testnet_days,
    )
    if args.start:
        day0 = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    client = DryRunClient()
    nonce = 0
    for d in range(args.days):
        day_start = day0 + timedelta(days=d)
        queue = WorkloadQueue()  # fresh round-robin each day; jobs are cheap/idempotent to rebuild
        summary = run_day(
            client, budget, queue, day_start,
            ledger_path=args.ledger_path, price_per_gflop=args.price_per_gflop,
            tick_minutes=args.tick_minutes, nonce_start=nonce,
        )
        nonce += summary.sessions_opened
        print(
            f"{summary.day}: opened {summary.sessions_opened}, settled {summary.sessions_settled}, "
            f"{summary.total_settled:.4f}/{summary.daily_target:.4f} FLOP ({summary.pct_of_target:.1f}% of target), "
            f"G_n={summary.total_gn}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
