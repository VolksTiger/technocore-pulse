# flopspend

Offline core of the FLOP-testnet spending agent. Design: `../docs/SPEND-PLAN.md`.
The testnet RPC does not exist yet, so everything here is checked against the
published canonical wire-format corpus instead — never against a live chain.

## What is real now

- `wire.py` -- wraps the vendored `vendor/compute_channel.py` encoders and
  checks the ENTIRE `vendor/wire-format-v1.json` corpus. `python3 -m
  flopspend.wire --check` -> 52 total, **48 passed, 4 skipped, 0 failed**
  when `py-sr25519-bindings` is installed (42/10 without it -- see below).
- `crypto.py` -- real sr25519 signature verification and signing
  (`Sr25519Verifier`, `Sr25519Signer`), via the vetted `py-sr25519-bindings`
  package. Checked against every signature-bearing vector in the corpus,
  including that a current-channel verifier correctly rejects a genuine
  legacy-domain signature replayed against the canonical (domain-separated)
  receipt preimage.
- `transcript.py` -- the R12.1b agent-side transcript: leaf construction,
  running Merkle root, receipt preimage, turn-signature verification and
  receipt counter-signing, all checked against the corpus.
  `default_verifier()`/`default_signer(seed)` return the real crypto.py
  implementations when the bindings are installed, else the plumbing-only
  `StubVerifier`/`StubSigner`.
- `budget.py` -- the SPEND-PLAN.md pacing model (daily target, token-bucket
  allowance with no cross-day rollover, session sizing, the §12.2
  concurrency cap, §6.2 circuit-breaker awareness). Pure functions.
- `ledger.py` -- append-only JSONL spend proof + `--report`.
- `queue.py` -- four job producers (roomkeeper digest, boardintel narrative,
  flopwatch changelog, brsolve skips), each offline with a small fixture.
- `runner.py` -- the loop skeleton with chain calls behind `ChainClient`;
  `DryRunClient` is a real, working offline miner+chain simulator that signs
  turns with a real sr25519 signer and verifies them with a real sr25519
  verifier when the bindings are installed (ledger rows and the printed
  summary carry `verifier=sr25519`/`stub` accordingly).

Install the bindings to get all of the above (do this before running the
tests or CLIs above):

```
pip install py-sr25519-bindings
```

## What waits for the RPC

- `RealChainClient` (`runner.py`): every method raises `NotImplementedError`
  — needs the published chain spec + `py-substrate-interface`.
- **R4.2 `flop_meter` G_n accounting.** Real reference-work counting needs
  `hp_poui::flop_meter`, a deterministic formula over prompt/context length,
  generated tokens, and MoE/attention terms — no published implementation to
  check against offline. `runner.py`'s `_job_flops_estimate` and
  `DryRunClient`'s per-turn `g_n` are explicitly-labeled synthetic proxies,
  not R4.2 accounting.
- (No longer waiting: sr25519 signature verification/signing -- see
  `crypto.py` above.)
- Up to 4 corpus vectors `wire.verify_vectors()` skips when
  `py-sr25519-bindings` IS installed (10 when it is not -- the extra 6 are
  the sr25519 signature checks, each carrying the install hint), each with a
  printed reason: the Retention enum and duplicate-turn dedup live outside
  compute-channel.py's scope; the pinned-channel-policy cutoff is chain
  state, not a wire invariant; and `wrong_path_orientation` is a genuine
  finding — see `wire.py`'s comment on why a self-duplicate Merkle sibling
  makes that mutation a hash-level no-op offline (the chain must reject it
  via a check this module doesn't have).

## Running the dry run

```
python3 -m flopspend.wire --check                              # vector corpus
python3 -m flopspend.runner --dry-run --days 1 --ration 1000    # one simulated day, seconds
python3 -m flopspend.ledger --report                            # per-day settled FLOP vs. target
python3 -m unittest tests.test_flopspend tests.test_flopspend_crypto   # 73 cases (crypto ones skip without the bindings)
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
