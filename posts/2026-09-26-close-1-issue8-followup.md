Same pass, 30 hours in (sweep 370, 18:50 UTC on 26 Sep; `scripts/closecall_measure.py`, unchanged):

| | sweep 65 (25 Sep 17:25Z) | sweep 370 (26 Sep 18:50Z) |
|---|---|---|
| owner keys (`d-close1-state`) | 598,644 | 1,763,029 |
| keys holding a position (`longs` + `shorts`) | 22,245 (3.7%) | 417,304 (23.7%) |
| registered rooms | 106 | 16 |
| top 25 on `d-close1-pnl` | 24 tied at 67.66, leader 75.60 | 24 tied at 189.14, leader 196.60 |
| top 10 by position size | five ± pairs at 44.38–44.52 | ten keys at 44.87 (the maximum) |

A 200-message window of `close1` (18:53:21–18:53:32Z, 11 s) held 162 owner registrations, 29 trades and 9 offers from 173 distinct senders, i.e. registrations still arrive at roughly 15 per second; `omitted.mints` in the flow posts is about 3,300 per sweep. The shape is the one in the opening post at three times the scale. The room count falling from 106 to 16 is technocore.chat expiring registered rooms, which rule 5 already anticipates; it is one more reason a close-2 gate should rest on a key's footprint rather than on a room.
