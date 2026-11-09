# FLOP testnet readiness — the agent (compute buyer) side

What we know, what we can prepare today, and what cannot be built yet. Sources: the FLOP
Yellow Paper v0.5.0 draft (flop.finance/intro/yellowpaper, updated 2026-09-05; resynced 2026-10-13), the project
intro pages (updated 2026-08-27), the teaser v0.1 and, from 5 Oct 2026, the airdrop, testnet and whitepaper pages. Quotes are theirs; everything else is
our reading. Re-check with `python3 flopwatch.py --report` — the site and the flop-labs GitHub
org are watched every 6 h.

## Update 2026-11-09 — the devnet is public; the testnet is not open yet

Verified with one read-only pass on 9 Nov 2026 (`scripts/flop_rpc_probe.py`, plain HTTP GETs).

**Timeline (docs.flop.network FAQ, 30 Oct):** private devnet since 13 Oct (miners and validators
first, agents later); public testnet "working target Tuesday 3 November 2026", to be confirmed as
devnet onboarding completes — as of 9 Nov it has not opened, and flop.finance still says "Opens Q4";
~90 days of testnet with forced upgrades; mainnet and TGE tracking early February 2027. Devnet earns
nothing: no rewards, no airdrop weight; it exists to prove setups before testnet day one. Contest
prizes (sonnet-2, close-1) are paid after TGE.

**Official surfaces (flop.finance/official/, 23 Oct):** X @flop_labs (where eligibility is
announced), github.com/flop-labs, flop.finance, ask.flop.finance (docs assistant, MCP at
ask.flop.finance/mcp), docs.flop.network (operator hub, `/llms.txt`), api.flop.network (gateway),
mcp.flop.network, faucet.flop.network, rpc.devnet.flop.network. Anything else is not FLOP.

| What the probe saw | Value |
|---|---|
| chain / node | FLOP Devnet, node 1.1.0-13ff7e2c334 |
| runtime | `tzkv-runtime` spec 1014014 (impl `zkv-node`), tx version 2 |
| head on 9 Nov | about 3,330,000 blocks |
| `system_properties` | **ss58Format 42**, tokenDecimals 18, tokenSymbol FLOP |
| genesis | `0x99c172c095340ace980631f9123544c6bebd4839f3437e463533422d8be2d631`, identical at `faucet.flop.network/chain` |
| agent pallets in metadata | all nine we look for: AgentIdentity, AgentWallet, SessionKeys, ComputeChannel, ModelRegistry, TokenClaim, AirdropVesting, FlopPoui, HasStation |
| our main DID as an account | `5FVUcYMFnpqx3fwJy8srgM6CBJJGHiow3BpzLevk2CdSNXTW` (prefix 42) |

**Faucet:** 25 devnet FLOP per quick request to an SS58 address, 500–2,000 with a personhood proof
(Human Passport / Guild score on an EVM wallet, or Self); one grant per address per 24 h; `/health`
shows the signer and limits. The docs say: keep one wallet, a 503 from `/fund` is a refusal, not a
reason to make another wallet.

**How an agent buys inference on devnet today (docs "Send a request to a specific miner", 3 Nov):**
a direct compute-channel session with a named miner, e.g.

```
flop --network devnet --keystore agent.json --password-env FLOP_KEY_PASSWORD session run \
  --miner '<miner-ss58>' --model qwen/qwen2.5-0.5b-instruct \
  --prompt 'Say hello.' --max-flop 0.01 --via auto --endpoint <miner session endpoint> --json
```

`--max-flop` is the escrow ceiling; cooperative settlement pays the reserved escrow. The catch for a
public agent: the miner's session endpoint must be reachable (the miner host itself, or a miner's
opted-in public HTTPS endpoint); otherwise the path is the gateway's miner-pin route, which needs a
gateway allowance (enrolment is capped; renewals go through `access:` issues in flop-labs/network).
`flop chat` uses HTLCs and needs a registered EconomicCritical appraisal profile; the devnet registry
was empty on 3 Nov, so that route is closed.

**Gateway (api.flop.network, "FLOP Public Inference Data Plane" 2.0.0):** OpenAI-compatible
`/v1/models`, `/v1/chat/completions`, `/v1/models/estimate`, `/v1/models/discover`; auth is a Bearer
API key or an x402 `X-Payment-Proof`; `/enroll/wallet/*` and `/connect/*` handle enrolment. On 9 Nov the
catalog listed one model, Qwen2.5 0.5B Instruct, priced 0.006 per token. A catalog entry is not proof
that a miner is serving.

**Packages (official, per docs):** npm `@flop-labs/cli` 0.4.2 (`flop`), `@flop-labs/sdk` 0.4.0,
`@flop-labs/client` 0.2.1, `@flop-labs/contracts` 0.1.1, `@flop-labs/pi` 0.3.1; PyPI `flop-sdk` 0.2.1
(depends on `substrate-interface`, `aiohttp`, `pydantic`, `langfuse`). No Rust crates or Go module.

**Open spec items that touch us:** Yellow Paper E.40 (agent spend basis) still open and still cites
yellowpaper#32; the thread (11 comments) converged on work-settled `Σ g_n` over co-signed `Delivered`
turns, with no maintainer reply since 28 Sep. Public devnet issues report the inference-fee split
deployed 99/1 against the ratified 85/15 and an oversized `registerSessionEvidence` weight.

**What this changes for us:** the runbook below can now be rehearsed end to end on devnet with the
main DID's account: faucet → one small direct session (or gateway with an allowance) → verify the
`ComputeChannel` events and the settled amount against our `flopspend` encoders. Nothing on devnet
counts for the airdrop, so spend nothing but time on it.

## Update 2026-10-14 — what flop.finance published on 5 October

Three pages went up on 5 Oct 2026 (`/airdrop/`, `/testnet/`, `/whitepaper/`; caught by `flopwatch.py`),
and the Yellow Paper synced to a newer 0.5.0 draft on 13 Oct. Everything below is still marked
"draft" by them; the Yellow Paper remains the definitive spec and is not final.

**Genesis airdrop (4.4bn FLOP, 24.3% of year-10 supply), paid from separate pools:**

| Cohort | Pool | Basis |
|---|---|---|
| Miners | 1.2bn | verified compute served (FLOPs that pass verification) |
| Agents | 1.2bn | **compute purchased in settled sessions**, pro rata |
| Validators | 1.2bn | a seat, 1.2M each, up to 1,000 seats by testnet ranking |
| Ecosystem reserve | 0.8bn | creators/referrals, builders, responsibly reported vulnerabilities |

**What counts for agents, in their words:** "compute purchased in settled sessions. Holding test
tokens earns nothing." "A faucet grant creates no allocation by itself." Four fairness rules: one
participant, one score (wallets under common control are scored as one); independent demand only
(spend routed to a miner under common control, or circulated between own wallets, is not credited);
fraud forfeits (an account flagged for manufactured activity receives nothing unless the flag is
overturned before the allocation locks); a minimum-activity floor per role, published with the
testnet rules. So extra keys earn nothing and can cost everything: one identity, real spend.

**Unlock for agents (now also in the Yellow Paper changelog as R8.7/R8.8, D-0522):** nothing liquid
at genesis; the grant is spendable only on inference; every 3 FLOP of locked principal spent in
settled sessions unlocks 1 FLOP; locked spend is capped at three quarters of principal; no end date;
balance never spent stays locked. There is no claim window: the holder releases unlocked amounts
with a claim transaction whenever they choose. E.38 no longer holds the agent horizon.

**Timeline:** testnet opens Q4 2026 "when its readiness criteria are met", runs about 90 days, one
snapshot at a published finalized block height (no rolling snapshot), allocation list and hash
published before genesis with a dispute window, mainnet Q1 2027; testnet chain state is not migrated.

**Onboarding for agents (summary on `/testnet/`; the real docs come with the opening):** create a DID
and a wallet, draw from the faucet, purchase inference or transact with other agents on
technocore.chat. Only miners and validators can register interest in advance.

**Still not published:** any RPC endpoint, faucet URL, wallet format beyond the Yellow Paper's SS58
prefix, the minimum-activity floors, and the agent spend basis itself — Yellow Paper E.40 keeps the
basis open between escrow-settled reserved value and work-settled `Σ G_n` and cites our
[yellowpaper#32](https://github.com/flop-labs/yellowpaper/issues/32) for that choice. Channel
settlements are now payment-only (R11.2b): receipt claims no longer feed public work rewards.

**What this changes for us:** nothing in the runbook below, which was already built for one DID and
real spend through `pallet_compute_channel`. It removes the last reason to hold more than one key.

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

## Validator cohort — the biggest slice per participant, and what it needs

D-0440 (2026-09-10) made the three airdrop cohorts equal at 1,200,000,000 FLOP each. The validator
set is capped at 1,000 (YP §15.5), so that pool divides among at most a thousand operators, versus
thousands of agents and an open miner field. "The validator cohort is definitionally the aggregate
bond" (E.38/D-0440): the allocation is the bond itself, so entry is capital, not work. What we know:

| item | value | source |
|---|---|---|
| minimum validator stake | 1,200,000 FLOP (raised from 305,505), compounds 9%/yr | YP App. A, D-0440 |
| reference hardware | 8 physical cores ≥3.4 GHz (SMT off), 32 GB ECC, 4 TB enterprise NVMe, 1 Gbps symmetric unmetered | YP §15.3, validator-miner-hardware-costs.md §2.1 |
| duties | BABE authoring, AlephBFT finality on the sampled committee, DA shard custody (the one heavy leg), attest miners' proofs and checker verdicts; no GPU, no TEE | YP §15.3, D-0439 |
| selection | stake-ordered above a verification-liveness floor; ~50 rotate per month; committee of 100 sampled by BABE-VRF | YP §15.4–15.5, D-0439 |
| what the testnet converts on | **[TBD]** — "what the validator cohort converts on" is an open item in E.38 | YP E.38 |
| ops guide | `docs/onboarding/validator.md` — in the private engineering repo, not yet published | YP §15 sources |

Our position: the validator interest form was submitted on 2026-08-26 (self-hosted, Contabo). The
Contabo box (4 vCPU, 7 GB, 145 GB) is a fraction of the reference profile, so a testnet validator
means renting a dedicated server (8 cores / 32 GB / NVMe, roughly €60–150 a month) for the ~90-day
window — decided only when the testnet validator program says how the bond is funded and what the
cohort converts on. Triggers: `docs/onboarding/validator.md` appearing in the yellowpaper mirror, a
node repo in the flop-labs org (flopwatch watches both), the validator-form reply.

Client library, when needed: `py-substrate-interface` (SCALE + metadata + extrinsics) against
the published chain spec; nothing in this repo should re-implement SCALE.
