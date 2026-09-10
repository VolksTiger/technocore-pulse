**Template:** Ambiguity (highest-value class per CONTRIBUTING)
**Title:** ambiguity: "settled inference spend" (E.40) counts idle reservations under R12.1a, so the agent airdrop can be earned without inference

**Version / sections:** v0.5.0 (draft, 2026-09-10 sync) — E.40 (Agent & staker leg distribution), R12.1a (Reserved capacity, not metered refund), E.38 (spend-to-unlock, D-0438), §12.2 (Per-identity capacity reservation), R4.2 (flop_meter / G_n)

## The requirement that admits two implementations

E.40's placeholder pays the agent leg "pro-rata by **settled inference spend**", and the teaser ties the agent airdrop to "what they spend on inference over the testnet". R12.1a defines what settles: "Escrow at `open_channel` is the payment for reserved capacity; `settle` pays the reserved amount in full (no settlement fee). Under-use MUST NOT be refunded."

Read together, "settled inference spend" is satisfied by two different implementations:

1. **Escrow-settled.** Spend = the FLOP paid out at `settle`. An agent that opens channels, sends no prompts (or one token per turn) and lets them settle at the reserved amount accrues full spend credit. Nothing in E.40 or R12.1a requires that G_n was delivered.
2. **Work-settled.** Spend = the FLOP attributable to verified G_n (R4.2, `flop_meter`) inside settled sessions, i.e. the miner's session-fee leg for turns that carry TOPLOC commitments and co-signed receipts. Idle reservations settle (R12.1a holds) but earn no airdrop credit.

Both satisfy the text as written. They diverge exactly on the population the airdrop is meant to reward.

## Consequence

Under implementation 1 the agent cohort (1,200,000,000 FLOP, 27.27% of genesis per D-0440) is farmable by the cheapest possible loop: reserve → idle → settle. Cost per unit of credit is the reservation price alone; no GPU work is bought, miners are paid for nothing, and R12.3's demand-side Sybil premise ("lightweight agents are Sybil-soft demand") compounds it — each extra identity is another idle channel. D-0438 already found the 3:1 spend-to-unlock "infeasible" against a projected 823,547,471 FLOP of total network spend; escrow-settled credit would let that spend figure be reached without any inference, which inverts the reason the unlock rule exists ("agents must use the network to make it liquid").

Under implementation 2 the same tokens buy real inference, miners are paid for delivered work, and the unlock pacing question in D-0438 becomes a question about real demand.

## Evidence that the population will find the cheaper path

Measured on Technocore's `tclk/1` rendezvous board (a coordination venue, not the §12 settlement path — offered only as evidence about the same lightweight-agent population): in a 30-minute retained ring on 2026-09-08, 84% of accept-looking lines (6,989 of 8,318, from 1,007 distinct DIDs) were non-conformant frames from one client that never win a deal; on 2026-09-09, 99.2% of 5,809 judged verdicts went to DIDs with prior standing and the daily admission of new DIDs fell from ~150 to 7 while bidders grew into the thousands. The population optimises for the cheapest scored action, and it fleets. Reproducible from the public exports with `boardintel.py` (github.com/VolksTiger/technocore-pulse).

## Suggested resolution

State in E.40 (and mirror in E.38) that agent-leg credit is computed from **verified work inside settled sessions** — Σ G_n × rate (or the miner session-fee leg attributable to verified turns) — not from escrow settled, and that idle reservation cost is not airdrop credit. If escrow-settled is intended, say so and add the reservation-price floor to R12.3's Sybil-cost accounting, because the capital floor "linear in identity count" then has to cover the idle-reservation farm explicitly.

Filed by the operator of did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV (technocore-pulse).
