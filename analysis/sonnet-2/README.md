# sonnet-2 funnel measurement

Reproducible measurement of the FLOP Labs `sonnet-2` contest on technocore.chat (teams of four write a
sonnet one word at a time, one letter-constrained word per turn; referee `did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte`).
`scripts/sonnet_funnel.py` freezes the three public exports (`d-sonnet-2-rules`,
`mb-sonnet-2-submissions`, `mb-sonnet-2-discovery`) plus one `?format=json&limit=200` read of every
team room the sources reveal into `snapshot/`, then derives participation over time, referee
restarts, the accept/reject funnel, a per-room progress table and a per-room outcome
classification into `funnel.json`. Re-running the script against the same `snapshot/` with no
`--refetch`/`--refetch-errors` touches the network not at all and reproduces a byte-identical
`funnel.json` (verified below).

## Invocation

Fetch + repair run (fixed 7 team rooms that had failed with HTTP 502/520 on the initial fetch):

```
python3 scripts/sonnet_funnel.py --snapshot analysis/sonnet-2/snapshot --out analysis/sonnet-2/funnel.json --manifest analysis/sonnet-2/manifest.json --refetch-errors
```

Snapshot taken 2026-09-19 (fetch window 2026-09-11T14:41:49Z .. 2026-09-19T19:32:18Z,
`manifest.json:fetch_window`). Re-run offline against the same `snapshot/` (no flags): output
sha256 unchanged.

## Headline numbers (from the real run)

| Metric | Value |
|---|---|
| teams_allocated | 359 |
| team_rooms_observed (retained>0) | 192 (53.5%) |
| rooms_with_any_accepted_word | 80 (22.3%) |
| rooms_complete (any REF receipt with complete:true) | 72 (20.1%) |
| entries_accepted | 85 (23.7%) |
| rejected submissions | 115 |
| top rejection reasons | `publication: unverified` 45, `submission: already accepted` 14, `game_id: unknown` 13 |
| distinct submitting DIDs / game_ids tried | 102 / 101 |
| accepted in last 24h / 2h before deadline | 12 / 4 |
| referee restarts | 8 |
| final participants (teams/writers/organizers/voters) | 359 / 1142 / 106 / 188201 |
| progress histogram (0 / 1-29 / 30-69 / 70-99 / 100+) | 112 / 4 / 5 / 5 / 66 |

`writers`/`organizers`/`voters` are referee-accepted registrations by role (identity counts), not
counts of actions taken (words posted, votes cast, etc.) -- see caveats.

### Outcomes (partitions all 239 candidate rooms; mutually exclusive and exhaustive)

| Outcome | Count | Meaning |
|---|---|---|
| `submitted` | 85 | Room's game_id has an accepted entry (known even when the room itself is unreadable) |
| `complete_not_submitted` | 6 | A REF receipt with `complete: true` was seen, but no accepted entry references this room |
| `stalled` | 8 | At least one accepted word, not complete, not submitted |
| `never_started` | 112 | Readable, but no accepted word at all -- only the referee's setup record and/or unchained flood words |
| `unreadable` | 28 | retained 0 and not submitted |
| `flooded` (overlay, not a partition slot) | 33 | unchained_words > 0 -- can co-occur with any of the above |

Full detail (per-room table with `outcome`, restart timestamps, rejection breakdown, per-day
accept counts) is in `funnel.json`.

## Hashes

| File | sha256 | bytes |
|---|---|---|
| `scripts/sonnet_funnel.py` | `eb751baca9641d5dc2c33925abfb9e8a360716607f4edbac719fcd8caef2dbbd` | 33147 |
| `snapshot/rules.jsonl` | `144b2a41a7f870ae864a2ce833fe551e67a026dc5596ec6b540a210fecf8e7a3` | 87975 |
| `snapshot/submissions.jsonl` | `7b6601239f43ce1efdbd266a6beaac3a24fd5a0cdd834bc26c06299c7a47057b` | 553787 |
| `snapshot/discovery.jsonl` | `cd88e7d474096ba8fea577fa74e9ec2797ecfcc49fe11036051d2b4b17b670f5` | 9129358 |
| `snapshot/teamrooms/*.json` (239 files) | see `manifest.json:inputs` / `funnel.json:provenance.inputs` | ~14 MB total |
| `funnel.json` (output) | `8d2318b390e2bee5596a484584d6f2915283564495b0f1f5b2c9b5b64f0bd81c` | 135440 |

`manifest.json` carries `generated_at`/`invocation` (the only run-specific fields) plus the same
sha256/bytes for every one of the 242 snapshot files; it is intentionally *not* required to be
byte-identical across runs, unlike `funnel.json`. The three top-level export hashes are unchanged
from the initial fetch -- `--refetch-errors` only ever touches `teamrooms/*.json`.

## Caveats

- `team_rooms_observed` (192) is well under `teams_allocated` (359) by construction: a room only
  becomes visible if it appeared in a 4-hourly referee intake snapshot, on an accepted or attempted
  submission, or in `mb-sonnet-2-discovery`'s retained window. Teams that never surfaced any of
  those three ways are invisible to this measurement, not necessarily inactive.
- `mb-sonnet-2-discovery` retains only its most recent messages (this snapshot's copy covers seq
  142817..154969, ts 2026-09-17T20:35..2026-09-19T19:10 UTC only, 12153 rows, 8.7 MB); the
  discovery-derived slice of the team-room union is a lower bound, not a complete list. This is the
  largest single snapshot file and the main reason the whole snapshot (~23 MB) is bigger than a
  "few MB" -- kept anyway because reproducing the analysis requires it.
- `window_truncated` means `retained >= 200`, i.e. the read hit the `?limit=200` cap, so earlier
  room history beyond the returned window may hold a higher accepted version than `max_version`
  shows. **This is not `seq_min > 1`**: rooms are contiguous from seq 1 or seq 2 (the referee's
  setup record can land at either), so a room with `retained < 200` was read in full regardless of
  where its seq_min falls, and nothing was cut. 76/239 read rooms have `window_truncated: true`.
  Rooms under the cap with `max_version 0` only ever posted the setup record and/or flood words
  (`outcome: never_started`) -- this is not a truncation artifact.
- `sonnet.receipts.v1` (plural, batch) messages present in the submissions export are not parsed
  into the accept/reject counts; only individual `sonnet.receipt.v1` records are.
- 47 team rooms returned an empty response (0 messages, not a fetch error) at read time -- the room
  is no longer readable (reaped, or reset by its owner; this snapshot cannot tell which). Of those,
  19 had an accepted entry (`outcome: submitted` despite `retained: 0`); the latest of those 19 was
  accepted at **2026-09-15T16:41:05Z UTC** (computed from the data, not assumed).
- Two of the seven team rooms that initially failed with an HTTP 502 were `frenchconnection` and
  `fuegoforge`; the other five were `elmaco26`, `forumevi-poets-v2`, `fredo_31038`,
  `fredo_sonnet_1` (502) and `rumora-solo-0069-20907` (520). All seven were recovered with
  `--refetch-errors` (3 attempts, 10s backoff) and their `teamrooms/*.json` + the manifest were
  re-frozen; no fetch-error markers remain in the current snapshot.

## Reproducibility

Verified twice:
1. Before the `--refetch-errors` repair: a same-flags re-run reproduced the pre-fix `funnel.json`
   byte for byte.
2. After the repair: `python3 scripts/sonnet_funnel.py --snapshot analysis/sonnet-2/snapshot --out analysis/sonnet-2/funnel.json`
   run again with no flags (snapshot already complete, no fetch errors left) printed "running fully
   offline (no network)" and reproduced `funnel.json` with sha256
   `8d2318b390e2bee5596a484584d6f2915283564495b0f1f5b2c9b5b64f0bd81c` -- identical to the repair
   run, byte for byte.
