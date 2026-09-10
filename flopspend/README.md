# flopspend

Offline core of the FLOP-testnet spending agent. Design: `../docs/SPEND-PLAN.md`.
The testnet RPC does not exist yet, so everything here is checked against the
published canonical wire-format corpus instead — never against a live chain.

## What is real now

- `wire.py` -- wraps the vendored `vendor/compute_channel.py` encoders and
  checks the ENTIRE `vendor/wire-format-v1.json` corpus. `python3 -m
  flopspend.wire --check` -> 52 total, 42 passed, 10 skipped, **0 failed**.
- `transcript.py` -- the R12.1b agent-side transcript: leaf construction,
  running Merkle root, receipt preimage, all checked against the corpus.
- `budget.py` -- the SPEND-PLAN.md pacing model (daily target, token-bucket
  allowance with no cross-day rollover, session sizing, the §12.2
  concurrency cap, §6.2 circuit-breaker awareness). Pure functions.
- `ledger.py` -- append-only JSONL spend proof + `--report`.
- `queue.py` -- four job producers (roomkeeper digest, boardintel narrative,
  flopwatch changelog, brsolve skips), each offline with a small fixture.
- `runner.py` -- the loop skeleton with chain calls behind `ChainClient`;
  `DryRunClient` is a real, working offline miner+chain simulator.

## What waits for the RPC

- `RealChainClient` (`runner.py`): every method raises `NotImplementedError`
  — needs the published chain spec + `py-substrate-interface`.
- **sr25519 signature verification.** The yellow paper is explicit: the
  session-enclave per-turn signature and the agent's receipt counter-signature
  are BOTH sr25519 (§6.5, Appendix F.3). This package does not implement
  sr25519 anywhere. `transcript.SignatureVerifier`/`Signer` are plain
  callables; `StubVerifier`/`StubSigner` are the defaults and do no
  cryptography — they only record what was asked and return a fixed answer.
  Wire a real sr25519 verifier (the published SDK, or py-substrate-interface)
  in before this handles a session that pays real tokens.
- **R4.2 `flop_meter` G_n accounting.** Real reference-work counting needs
  `hp_poui::flop_meter`, a deterministic formula over prompt/context length,
  generated tokens, and MoE/attention terms — no published implementation to
  check against offline. `runner.py`'s `_job_flops_estimate` and
  `DryRunClient`'s per-turn `g_n` are explicitly-labeled synthetic proxies,
  not R4.2 accounting.
- 10 corpus vectors `wire.verify_vectors()` skips, each with a printed
  reason: 6 need the unimplemented sr25519 verification; the Retention enum
  and duplicate-turn dedup live outside compute-channel.py's scope; the
  pinned-channel-policy cutoff is chain state, not a wire invariant; and
  `wrong_path_orientation` is a genuine finding — see `wire.py`'s comment on
  why a self-duplicate Merkle sibling makes that mutation a hash-level no-op
  offline (the chain must reject it via a check this module doesn't have).

## Running the dry run

```
python3 -m flopspend.wire --check                              # vector corpus
python3 -m flopspend.runner --dry-run --days 1 --ration 1000    # one simulated day, seconds
python3 -m flopspend.ledger --report                            # per-day settled FLOP vs. target
python3 -m unittest tests.test_flopspend                        # 61 cases
```

`--dry-run` is required; `runner.py` refuses to guess at the real client.
The simulated day runs on a virtual clock (`--tick-minutes`, default 15) --
no real sleeping, so `--days 90` still finishes in well under a minute.

## Attribution

`vendor/compute_channel.py`, `vendor/wire-format-v1.json`, and
`vendor/wire-format-v1.schema.json` are fetched verbatim from
[flop-labs/yellowpaper](https://github.com/flop-labs/yellowpaper)
(`evidence/`), commit `3eaf2f2` (2026-09-10). CC BY 4.0, (c) 2026 FLOP Labs —
full text in `vendor/LICENSE-yellowpaper`. Attribution per that license:
FLOP Labs, "FLOP Network Yellow Paper", 0.5.0 (draft), 2026,
https://github.com/flop-labs/yellowpaper. Nothing in `vendor/` is edited;
`compute_channel.py` carries its own header with the exact source URL and
commit hash. `evidence/generate-wire-format-vectors.py` was fetched to READ
(how the vectors are produced) and is not vendored, per the task brief.
