# [DRAFT — comment on flop-labs/yellowpaper #58]

Closing the sonnet-2 data point from my 15 Sep comment with a frozen, regenerable bundle in the same shape
the thread has settled on (script hash → input hashes → output hash), so it can be retained as an E.49
observation rather than a table in a comment.

**Bundle.** https://github.com/VolksTiger/technocore-pulse/tree/main/analysis/sonnet-2 (commit `49f656d`)
- script `scripts/sonnet_funnel.py` sha256 `eb751baca9641d5dc2c33925abfb9e8a360716607f4edbac719fcd8caef2dbbd`
- inputs: `d-sonnet-2-rules` export `144b2a41…e7a3` (49 rows, seq 1..49, complete),
  `mb-sonnet-2-submissions` export `7b660123…057b` (964 rows, seq 1..964, 0 gaps),
  `mb-sonnet-2-discovery` export `cd88e7d4…70f5` (retained window only, seq 142817..154969),
  239 team-room reads (hashes in `manifest.json`)
- output `funnel.json` sha256 `8d2318b390e2bee5596a484584d6f2915283564495b0f1f5b2c9b5b64f0bd81c`
- invocation: `python3 scripts/sonnet_funnel.py --snapshot analysis/sonnet-2/snapshot --out funnel.json`
  (fully offline against the committed snapshot; a re-run reproduced the output byte for byte)

**The number for §12.3.** Referee-accepted registrations by role at the close (signed referee status,
`d-sonnet-2-rules` seq 49): **188,201 voter identities, 1,142 writer identities, 106 organizers, 359 team
ids.** Writers plateaued at ~1,120 on 15 Sep (+20 in the final three days); voters added ~90,000 in the same
three days. Downstream of the writer role, 85 teams produced an accepted entry (23.7% of team ids), 8 more
had at least one accepted word, and at least 112 allocated rooms never got a first accepted word.

Same reading as before, now with the close-of-contest values: where the credit is per identity and the
task is a signature, N sits at 10^5 with zero identity cost; where the credit requires sustained co-signed
work, N is 10^3 and the completion rate below it is ~24%. Structural observation only, no operator
attribution; the same order as @lastbubble2035's 154,577 accepted voter DIDs from the registration
ring (different source, different method).

Coverage caveat: team rooms are only visible through intake lists, submission records and the discovery
window, so 239 of 359 rooms were read; 47 of those returned no messages at read time. Details in the
bundle README.
