# Testnet spend plan — what the agent does the hour the faucet opens

The agent airdrop is pro-rata by inference spend on the testnet (E.40 placeholder: "settled
inference spend"; teaser: "what they spend on inference over the testnet"). Everyone gets the same
faucet ration, so the only lever is spending the whole ration on sessions that settle, steadily,
with nothing idle. This is the plan that makes that automatic. Numbers marked (YP) are from the
Yellow Paper v0.5.0 as synced 2026-09-10; everything else is ours.

## Day-zero checklist (human, ~30 minutes, once)

1. `flopwatch --report` fires on the node/faucet announcement → read the chain spec, note
   `ss58Format`, decimals, RPC URL.
2. `scripts/flop_rpc_probe.py --url <rpc>` → confirm `AgentIdentity`, `ComputeChannel`,
   `ModelRegistry` in the metadata; derive our address (`scripts/flop_address.py --prefix N`).
3. Faucet claim with the **main DID** (the official format, whatever it is; our `/r/faucet` +
   `/kv/faucet` registration is the provisional one). Faucet ration = the budget for everything below.
4. `agent_identity` registration: 10 FLOP minimum stake (YP §6.2) + existential deposit 0.01.
5. Delegate a **session key** to the Contabo node under `agent_wallet` caps (YP §6.2: lifetime cap,
   per-tx and daily caps, destination allowlist = `compute_channel` only, circuit breaker
   60 blk / 100 tx / 250 FLOP). The main key never leaves the Mac; the node spends within the caps.
6. Set the budget: `ration − stake − deposit − 5% reserve`, divided by the testnet days left
   (~90, YP/teaser) = the daily spend target. The runner spends to that target every day, no more,
   no less; an unspent balance scores nothing ("balance is never a scoring term", E.38).

## The runner (`flopspend`, to be built on the day the RPC exists)

Design, so it is not designed under pressure:

- **Loop:** every N minutes, pick a job from the workload queue, pick a miner off-chain
  (`model_registry::find_best_miners`, YP App. C), `open_channel` sized to the job (escrow = the
  reserved capacity — under-use is not refunded, R12.1a — so size to what the job needs, never
  round up), stream the prompt turns, verify and counter-sign every receipt (R12.1b), let the
  miner `settle`, log settled FLOP and G_n per session.
- **Concurrency:** at most 4 active reservations base (+1 per 50 FLOP escrowed, §12.2). Keep 2–3 in
  flight so a stalled miner never blocks the daily target.
- **Sizing:** small, frequent sessions beat large ones: a stalled large session loses the whole
  escrow to the reserved-capacity rule; small ones cap the loss and keep the daily curve smooth.
- **Pacing:** a token-bucket toward the daily target; catch-up allowed within the day, never across
  days (no end-of-testnet dumping, which the 3:1 unlock pacing may not even count).
- **Disputes:** protocol fraud only (wrong measured root, inflated G_n, forged signatures,
  non-delivery, R12.1f); 100 FLOP challenger bond, so dispute only with the evidence in hand.
  Non-delivery timeout → full refund (φ=0), so a dead miner costs time, not FLOP.
- **Ledger:** every session's channel id, miner, model hash, escrow, settled amount, G_n, receipt
  root and our counter-signature, appended to `~/.technocore-pulse/flopspend.jsonl` — our own
  spend proof, independent of the explorer.
- **Client library:** py-substrate-interface against the published chain spec. Nothing here
  re-implements SCALE; `evidence/compute-channel.py` in the yellowpaper repo is the canonical
  encoder for the channel wire profile and will be reused, not rewritten.

## The workload queue — real work, so the spend is defensible

The credit is the same whether the prompts are useful or not, but "useful participation" is the
story we tell and the tokens buy real inference either way. The queue is fed by the tools we
already run, so it never runs dry:

| source | job | why it is real |
|---|---|---|
| roomkeeper (6 h) | write the `d-technocore-intel` digest from the reader's raw counts | replaces a template with a model-written digest that is posted and signed |
| boardintel (30 min) | one-paragraph narrative of each snapshot for `board.html` | goes onto the public page |
| authenticity / sybil | label ambiguous rooms and clusters that the heuristics score 40–60 | improves the published classifier |
| blockrewards | answer the families `brsolve` skips (review, extraction variants, validation edge cases) | judged by a third party; a wrong answer is visible |
| flopwatch | summarise every page/repo change into a changelog entry | keeps the readiness doc current |
| tclk | explain each fold rejection in plain language for the audit report | documentation others read |

Model choice: whatever the miners serve (any registered `(model, precision)` with a measured root;
the GPU market page tracks open-weight candidates). Prefer the cheapest model that passes a fixed
quality check per job type; the check is ours (exact-match on the templated families, rubric on
the narrative ones), run before the output is used.

## What decides "quality" in the airdrop sense

Nothing in the spec scores prompt quality. Credit is pro-rata by settled spend (E.40, unconfirmed)
and unlocks 1:3 against later inference spend or stake delegation (E.38, D-0438 says the 3:1
pacing is infeasible as written and will be re-specified). So quality is for us: it makes the
spend produce things we publish. The airdrop math only rewards spending the whole ration on
sessions that settle. Both are covered by the runner above.

## Open questions we cannot pre-answer (flopwatch watches for each)

- Faucet: amount, cadence, one-per-DID or renewable, whether Technocore DID note is the key.
- Reservation price formation (YP issue #12 asks exactly this) and whether idle reservation cost
  counts as "spend" (our issue draft in `posts/2026-09-10-yellowpaper-issue-agent-spend.md`).
- SOFT-lane session flow end to end (E.33 [TBD]); the first miners will be SOFT tier.
- Whether unlock/vesting pacing rewards steady spend over lumpy spend (D-0438 re-specification).
