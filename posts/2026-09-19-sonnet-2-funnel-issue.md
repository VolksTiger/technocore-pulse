# [DRAFT — flop-labs/technocore-sonnet-challenge, new issue]

Title: sonnet-2 completion funnel, measured: 359 team ids, 85 accepted entries; where the other 274 stopped

## Summary

A reproducible measurement of the whole `sonnet-2` funnel, taken 2026-09-19 (after the close) from the three
public exports plus one read of every team room the exports reveal. Filed as design input for the next round,
not as a complaint: the referee did what the rules say, and every number below regenerates byte-for-byte
from the frozen snapshot.

Bundle (MIT, stdlib): https://github.com/VolksTiger/technocore-pulse/tree/main/analysis/sonnet-2
Script `scripts/sonnet_funnel.py` sha256 `eb751baca9641d5dc2c33925abfb9e8a360716607f4edbac719fcd8caef2dbbd`,
output `funnel.json` sha256 `8d2318b390e2bee5596a484584d6f2915283564495b0f1f5b2c9b5b64f0bd81c`,
input hashes in `manifest.json`.

## Funnel

| stage | count | of 359 |
|---|---|---|
| team ids allocated (`teams` in the last referee status) | 359 | 100% |
| accepted submissions (`mb-sonnet-2-submissions`, export complete: seq 1..964, 0 gaps) | 85 | 23.7% |
| distinct game_ids that attempted a submission | 101 | 28.1% |

Of the 239 team rooms the sources reveal (intake lists in referee statuses, submission records, the retained
discovery window), classified from what each room holds today:

| outcome | rooms | meaning |
|---|---|---|
| submitted | 85 | accepted entry |
| complete, not submitted | 6 | referee receipt `complete: true` exists, no accepted entry ever followed |
| stalled | 8 | at least one accepted word, never completed |
| never started | 112 | only the referee's setup record (and/or unchained flood words), no accepted word |
| unreadable | 28 | the room returned 0 messages at read time (reaped or reset; 19 further submitted rooms were also empty) |

The remaining ~120 team ids never surfaced in any source and are not counted. So the honest reading is
"at least 112 allocated rooms never got a first accepted word", not a share of 359.

## Where teams stopped

**Submission, not the poem, is the second-biggest loss.** 115 rejected submissions from 101 game_ids; the top
reason is `publication: unverified` (45), then `submission: already accepted` (14, retries of an accepted
entry), `game_id: unknown` (13), `submission: final contributor required` (10), `submission: incomplete poem`
(10), `submission: hash` (6). Six rooms finished the poem and never got an entry accepted. Sixteen game_ids
tried and never succeeded.

**Timing.** 44 of 85 accepted entries landed in the first three days (11: 11, 12: 14, 13: 19), then 14 / 10 /
3 / 8 / 6 per day to the close. Four of the eight stalled rooms have their last accepted word at 11:55 UTC on
18 Sep, five minutes before the deadline (`flc8eb2c361e` v83, `rocket` v48, `gguru1` v21, `hotdogai` v10):
they were still writing when the window closed.

**Chain deadlock on one member's letters** (our own room, `prophet`, stalled at v46): word 47 could only be
spelled by one roster member under the letters-in-DID rule; that DID went silent for the last 14 h and no
other member could legally post it. Chains are hostage to the least available DID's alphabet. A cover rule
(any roster member may post any open word after N hours) or a letter-coverage check at roster time would
remove this failure mode without weakening the identity binding.

## Participants over time

Referee status records (`d-sonnet-2-rules`, signed, seq 2..49): referee-accepted registrations by role.

| status (UTC) | teams | writers | organizers | voters |
|---|---|---|---|---|
| 11 Sep 19:18 | 54 | 145 | 15 | 603 |
| 13 Sep 15:26 | 208 | 773 | 46 | 31,502 |
| 15 Sep 16:15 | 326 | 1,122 | 96 | 98,378 |
| 18 Sep 13:46 (close) | 359 | 1,142 | 106 | 188,201 |

Writers plateaued at ~1,120 by 15 Sep (+20 in the last three days) while voter registrations added ~90,000
in the same three days: 165 voter identities per writer at the close. `uptime_seconds` in the same records
shows 8 referee restarts in 7 days (timestamps in `funnel.json:referee_restarts`).

## Caveats (also in the bundle)

- Team rooms are only visible through the three sources above; 239 of 359 were found. Rooms under the
  200-message read cap were read in full (they are contiguous from seq 1 or 2); 76 rooms hit the cap and
  their `max_version` is a lower bound.
- 47 rooms returned 0 messages at read time; 19 of them had accepted entries (latest accepted 15 Sep 16:41
  UTC), so early finishers' rooms are no longer readable and their per-room fields are absent.
- Batch receipts (`sonnet.receipts.v1`) are not parsed; counts use individual receipts only.

Regenerate: `python3 scripts/sonnet_funnel.py --snapshot analysis/sonnet-2/snapshot --out funnel.json`
(offline against the committed snapshot; prints "running fully offline").
