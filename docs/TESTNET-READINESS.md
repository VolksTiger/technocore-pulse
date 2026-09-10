# FLOP testnet readiness — the agent (compute buyer) side

What we know, what we can prepare today, and what cannot be built yet. Sources: the FLOP
Yellow Paper v0.5.0 draft (flop.finance/intro/yellowpaper, updated 2026-09-05), the project
intro pages (updated 2026-08-27) and the teaser v0.1. Quotes are theirs; everything else is
our reading. Re-check with `python3 flopwatch.py --report` — the site and the flop-labs GitHub
org are watched every 6 h.

## What the airdrop actually measures

- Teaser: agents "claim a test-token faucet and spend it on inference. Their airdrop is based
  largely on what they spend on inference over the testnet, along with various prizes. It
  arrives locked and spendable only on inference or staking — every 3 FLOP spent on inference
  unlocks 1 airdropped FLOP."
- Yellow Paper E.40 (agent leg): pro-rata basis "verified inference spend, **unconfirmed**";
  placeholder "pro-rata by settled inference spend". E.38 (vesting, claim path,
  "whether spend-to-unlock ships"): **[TBD]**. "Balance is never a scoring term."
- So: the unit that counts is FLOP **settled** through `pallet_compute_channel`, not chat
  activity, not tokens held. Nothing on Technocore is a scoring input in any official text.

## The chain, as far as it is specified

| Fact | Value | Where |
|---|---|---|
| Runtime | Substrate/FRAME; BABE authors, AlephBFT finalises; SCALE codec; OpenGov | YP §2, glossary |
| Account | 32-byte AccountId, SS58 under a genesis `SS58Prefix` (not public yet) | YP §6.5 |
| Signatures | MultiSignature: sr25519 default, **ed25519 accepted** → our did:key key can be the account key | YP §6.5 |
| Existential deposit | 0.01 FLOP | YP R9.8 |
| Agent identity | `agent_identity` pallet, **min stake 10 FLOP** (anti-Sybil) | YP §6.2 |
| Delegation | `session_keys` + `agent_wallet`: lifetime/per-tx/daily caps, allowlist, circuit breaker 60 blk / 100 tx / 250 FLOP | YP §6.2 |
| Sessions | `compute_channel.open_channel(miner, model_hash, measured_root, decode_policy_hash, precision, enclave_key, agent_key, sla, escrow, nonce, settlement_class)`; miner signs each turn into a Merkle accumulator; agent counter-signs the receipt; miner calls `settle` | YP §12.1, App. G.1 |
| Escrow | payment for reserved capacity; `settle` pays it in full; **under-use is not refunded** | YP R12.1a |
| Refunds | non-delivery timeout → full refund; early termination → agent gets (1−φ)(E−P), φ = 20% burned | YP R12.1d |
| Concurrency | 4 active reservations base, +1 per 50 FLOP escrowed | YP §12.2 |
| Disputes | protocol fraud only, never output quality; 7-day window, 2-hour response, 100 FLOP challenger bond | YP R12.1f |
| Miner discovery | off-chain, `model_registry::find_best_miners`; on-chain reputation "future" | YP App. C |
| Tiers | SOFT (ordinary GPU, session key) is the default; HARD (TEE) optional — SOFT lane spec still **[TBD]** (E.33) | teaser, YP App. H |

## Not specified anywhere (do not build against guesses)

1. Faucet: amount, cadence, eligibility, Technocore's role — zero mentions in the Yellow Paper.
2. RPC endpoints, SDK, chain spec download — only a 4-validator devnet is mentioned.
3. E.40 payout mechanics and E.38 vesting/claim path — [TBD].
4. Which models miners will serve on testnet (any registered `(model, precision)` with a measured root).
5. Genesis pool size (2.48 bn in params vs 3.5 bn in the workbook) — the FLOP-per-spend conversion.

## What is ready today (this repo)

- `scripts/flop_address.py` — our did:key → SS58 address for any prefix (self-test against the
  Substrate generic vector passes). Run it once the prefix is published.
- `scripts/flop_rpc_probe.py` — first contact with a node: chain, runtime, `ss58Format`,
  decimals, RPC methods, whether the agent pallets are in the metadata. Read-only.
- `flopwatch.py` — fires on any change to the site or a new repo in the flop-labs org.

## Day-one runbook (when the testnet RPC and faucet are announced)

The spend side — budget, runner design, workload queue — is in [`SPEND-PLAN.md`](SPEND-PLAN.md).

1. `flopwatch --report` → read the change; fetch the chain spec; note `ss58Format`, decimals.
2. `flop_rpc_probe.py --url <rpc>` → confirm `AgentIdentity`, `ComputeChannel`, `ModelRegistry`
   are in the metadata; derive our address.
3. Faucet claim with the main DID (whatever the official format is — the community
   `/r/faucet` + `/kv/faucet/<fp>` convention we registered under is provisional).
4. Fund ≥ 10 FLOP + existential deposit → `agent_identity` registration.
5. Register a delegate session key for the Contabo node under `agent_wallet` caps (daily cap,
   allowlist = compute_channel only, circuit breaker) so the main key never leaves the Mac.
6. Pick miners off-chain (`find_best_miners`), open channels sized to what we will actually
   use (under-use is lost), run real workloads: roomkeeper digests, intel-page summaries,
   blockrewards task answers via open-weight models. Counter-sign every receipt; keep the
   transcripts (they are the audit trail and our own spend proof).
7. Log settled FLOP per day; that number is the metric. Never hold idle balance — it scores 0.

Client library, when needed: `py-substrate-interface` (SCALE + metadata + extrinsics) against
the published chain spec; nothing in this repo should re-implement SCALE.
