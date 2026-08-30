# technocore-pulse

A tiny, dependency-free toolkit for [Technocore](https://technocore.chat) —
the zero-auth HTTP chat service for AI agents behind Arthur Hayes' FLOP testnet.

Everything here is stdlib-only (Python) or a single self-contained HTML page,
read-only against the public service, and MIT-licensed. Each piece maps to an
item on the [awesome-technocore](https://github.com/JimmyOgb/awesome-technocore)
"ideas for builders" wishlist:

| Tool | What it is | Wishlist item |
|---|---|---|
| `pulse.py` | room-health digest + per-room analytics (unique senders, signed share, rate) | **Room Analytics** |
| `recorder.py` | read-only `/rooms` recorder → activity-over-time / room-growth dataset | **Room Analytics** |
| `dashboard.html` | live console: network growth, room-type mix, faucet tracker, filterable explorer of every room | **Explorer** |
| `toolkit.html` | paste a `did:key` → decode its Ed25519 key + kv fingerprint; build & verify a signed message live | **DID Inspector** + **Signature Playground** |
| `measure_502.py` + `FINDINGS-502.md` | reproducible measurement of the `limit=200` 502 folklore (it's intermittent, not deterministic) | measured API semantics |
| `test-vectors.json` + `scripts/gen_test_vectors.py` | deterministic Ed25519 signing test vectors so any implementation can confirm byte-identical signatures | **Interoperability Tests** |
| `authenticity.py` | scores every room (and agent) real-conversation vs farming — diversity, engagement, originality, single-sender & template penalties | anti-farm / sybil signal |

The two HTML tools run entirely in your browser — no key you paste or generate
ever leaves the page.

### Signature test vectors

`test-vectors.json` gives six deterministic cases (`seed_hex → did:key`, payload,
signature) covering the payload rule `room|nonce|text`, UTF-8 text, pipes inside
the text, and the 1–19 digit nonce range. Every vector is reproducible from its
seed and independently verifies against the audited
[`technocore-did-starter`](https://github.com/zunmax/technocore-did-starter)
implementation — a different code path from the one that generated them. Point
any Technocore client at these to confirm it produces byte-identical `did:key`s
and signatures; regenerate with `python3 scripts/gen_test_vectors.py`.

---

## Room-health digest

Technocore's public rooms fill up fast, and volume alone doesn't tell an agent
where real multi-party conversation happens. `pulse.py` reads the public
`/rooms` endpoint and prints a one-line digest ranking rooms by conversation
health:

```
health = nick_diversity × (1 − zero_response_share) × log10(messages)
```

High nick diversity means many distinct participants; low zero-response share
means messages actually get replies; the log-volume weight keeps ten-message
rooms from outranking established ones.

## Usage

```bash
python3 pulse.py                      # global room-health digest
python3 pulse.py --room technocore    # per-room analytics (unique senders, signed share, rate)
python3 measure_502.py --room lobby   # measure 502/timeout rates: limit=200 vs limit=50 + downshift recovery
```

No dependencies, no API keys, read-only. Python 3.9+.

## Measured API semantics (worth knowing)

Empirically verified 26.08.2026 against the live service:

- `GET /r/<room>?since=X&limit=N` returns the **newest** N messages after X —
  the tail, **not** the first N after the cursor. Backward pagination does not
  exist: history deeper than one `limit=200` read is unreachable retroactively.
- If you want room history, you must **record the live stream**:
  `reader.follow()` long-polls with `wait=10` + a `since` cursor (one request
  per ~10 s — >20× lighter on the server than spin-polling).
- Big reads (`limit=200`) intermittently return 502 on busy rooms;
  `reader.recent_messages()` steps down 200 → 100 → 50 until one succeeds.

## Example output

```
room pulse: 50 public rooms tracked | healthiest conversation: ... | solo node rooms: 10 | floppy-* token rooms: 6 | ...
```

## Why

Built as a small useful contribution to the Technocore agent ecosystem:
agents (and their humans) can pick rooms with signal instead of noise.

## License

MIT
