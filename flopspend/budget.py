"""The testnet pacing model from docs/SPEND-PLAN.md: turn a faucet ration into
a daily spend target, pace toward it without ever dumping unspent days'
allowance later, size a session's escrow to the job (never more), and stay
inside the reservation-cap and circuit-breaker limits from the yellow paper
(§12.2, §6.2) so the runner never trips them.

Everything here is a pure function or a small, explicitly-stateful class over
plain numbers -- no I/O, no chain calls, no vector corpus to check against
(these are OUR pacing choices, not wire format; SPEND-PLAN.md is the only
source). Unit tests exercise the arithmetic, the day-rollover rule, and the
concurrency-cap formula directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Tuple

# Reference-only constants transcribed from the yellow paper's parameter
# tables (marked "enforce: false" there -- single-site/documented, not yet
# wired for cross-language enforcement on-chain). flopspend still respects
# them defensively so the runner doesn't manufacture a circuit-breaker trip.
CIRCUIT_BREAKER_WINDOW_BLOCKS = 60  # §6.2 circuit_breaker_window
CIRCUIT_BREAKER_MAX_TX = 100  # §6.2 circuit_breaker_tx_count
CIRCUIT_BREAKER_MAX_FLOP = 250.0  # §6.2 circuit_breaker_flop_cap
AGENT_DAILY_CAP_AUTONOMOUS = 500.0  # §6.2 agent_daily_cap_autonomous (FLOP)
AGENT_PER_TX_LIMIT = 100.0  # §6.2 agent_per_tx_limit (FLOP)

# §12.2 R12.2 per-identity in-flight reservation cap.
RESERVATION_CAP_BASE = 4  # max_active_reservations_base
RESERVATION_CAP_ESCROW_PER_SLOT = 50.0  # escrow_per_reservation_slot (FLOP)


@dataclass(frozen=True)
class Budget:
    """ration − stake − deposit − reserve_share·ration, spread over `days`
    (SPEND-PLAN.md step 6: "ration − stake − deposit − 5% reserve, divided by
    the testnet days left"). All amounts in FLOP; `reserve_share` is a
    fraction of `ration` (0.05 == "5% reserve"), not of the post-stake/deposit
    remainder.
    """

    ration: float
    stake: float
    deposit: float
    reserve_share: float
    days: int

    def __post_init__(self) -> None:
        if self.ration <= 0:
            raise ValueError("ration must be positive")
        if self.stake < 0 or self.deposit < 0:
            raise ValueError("stake/deposit cannot be negative")
        if not (0.0 <= self.reserve_share < 1.0):
            raise ValueError("reserve_share must be in [0, 1)")
        if self.days <= 0:
            raise ValueError("days must be positive")
        if self.spendable < 0:
            raise ValueError(
                f"ration {self.ration} cannot cover stake {self.stake} + deposit {self.deposit} "
                f"+ reserve {self.reserve} -- nothing left to spend"
            )

    @property
    def reserve(self) -> float:
        return self.ration * self.reserve_share

    @property
    def spendable(self) -> float:
        return self.ration - self.stake - self.deposit - self.reserve

    @property
    def daily_target(self) -> float:
        return self.spendable / self.days


@dataclass
class TokenBucket:
    """Paces spend toward `daily_target`. Within a day, catch-up is allowed:
    the allowance grows linearly with elapsed time, so under-spending the
    morning can be made up in the evening. Across a day boundary, whatever
    fraction of the target went unspent is simply dropped -- the new day
    starts at its own fresh target, never inflated by yesterday's shortfall
    ("no end-of-testnet dumping", SPEND-PLAN.md).
    """

    daily_target: float
    day_start: datetime
    spent_today: float = field(default=0.0)

    def __post_init__(self) -> None:
        if self.daily_target < 0:
            raise ValueError("daily_target cannot be negative")
        if self.day_start.tzinfo is None:
            raise ValueError("day_start must be timezone-aware")

    def _roll_day(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        elapsed_days = (now - self.day_start) // timedelta(days=1)
        if elapsed_days >= 1:
            self.day_start = self.day_start + elapsed_days * timedelta(days=1)
            self.spent_today = 0.0

    def allowance(self, now: datetime) -> float:
        """How much can be spent right now without exceeding today's target,
        net of what's already been recorded today. Never negative."""
        self._roll_day(now)
        elapsed_fraction = (now - self.day_start).total_seconds() / 86400.0
        elapsed_fraction = min(1.0, max(0.0, elapsed_fraction))
        return max(0.0, self.daily_target * elapsed_fraction - self.spent_today)

    def record_spend(self, amount: float, now: datetime) -> None:
        if amount < 0:
            raise ValueError("amount cannot be negative")
        self._roll_day(now)
        self.spent_today += amount


def size_session(job_flops: float, price_per_gflop: float, max_escrow: float) -> float:
    """Escrow for one session, sized to what the job needs -- never rounded
    up, because R12.1a makes escrow the payment for reserved capacity and
    under-use is never refunded (over-provisioning is pure loss on a
    cooperative settle). Capped at `max_escrow` (the caller's per-session
    ceiling, e.g. from the daily target or agent_per_tx_limit); if the job's
    true cost exceeds that cap, the returned escrow is the cap itself -- the
    caller must either accept a smaller/partial job or raise the cap, not get
    a bigger number back from this function."""
    if job_flops <= 0:
        raise ValueError("job_flops must be positive")
    if price_per_gflop <= 0:
        raise ValueError("price_per_gflop must be positive")
    if max_escrow <= 0:
        raise ValueError("max_escrow must be positive")
    needed = job_flops * price_per_gflop
    return min(needed, max_escrow)


def max_concurrent_reservations(
    escrowed_total: float, base: int = RESERVATION_CAP_BASE, escrow_per_slot: float = RESERVATION_CAP_ESCROW_PER_SLOT
) -> int:
    """§12.2 R12.2: base + one slot per `escrow_per_slot` FLOP currently
    escrowed across open reservations."""
    if escrowed_total < 0:
        raise ValueError("escrowed_total cannot be negative")
    return base + int(escrowed_total // escrow_per_slot)


@dataclass(frozen=True)
class CircuitBreakerLimits:
    window_blocks: int = CIRCUIT_BREAKER_WINDOW_BLOCKS
    max_tx: int = CIRCUIT_BREAKER_MAX_TX
    max_flop: float = CIRCUIT_BREAKER_MAX_FLOP


def _window_txs(
    limits: CircuitBreakerLimits, recent_txs: Iterable[Tuple[int, float]], now_block: int
) -> "list[Tuple[int, float]]":
    window_start = now_block - limits.window_blocks
    return [(b, f) for b, f in recent_txs if window_start < b <= now_block]


def would_trip(
    limits: CircuitBreakerLimits, recent_txs: Iterable[Tuple[int, float]], now_block: int, next_tx_flop: float
) -> bool:
    """Would one more transaction of `next_tx_flop` FLOP at `now_block` trip
    the agent_wallet circuit breaker (§6.2: circuit_breaker_window /
    circuit_breaker_tx_count / circuit_breaker_flop_cap)? `recent_txs` is
    (block_number, flop_amount) pairs already sent by the delegated session
    key; only entries within the trailing `window_blocks` count."""
    in_window = _window_txs(limits, recent_txs, now_block)
    tx_count = len(in_window) + 1
    flop_total = sum(f for _, f in in_window) + next_tx_flop
    return tx_count > limits.max_tx or flop_total > limits.max_flop


def circuit_breaker_headroom(
    limits: CircuitBreakerLimits, recent_txs: Iterable[Tuple[int, float]], now_block: int
) -> Tuple[int, float]:
    """(tx_headroom, flop_headroom) remaining in the trailing window before
    the circuit breaker trips -- how many more transactions, and how much
    more FLOP, the runner can still send right now."""
    in_window = _window_txs(limits, recent_txs, now_block)
    tx_headroom = max(0, limits.max_tx - len(in_window))
    flop_headroom = max(0.0, limits.max_flop - sum(f for _, f in in_window))
    return tx_headroom, flop_headroom
