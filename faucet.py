#!/usr/bin/env python3
"""Faucet integrity analytics for Technocore's /r/faucet.

The $FLOP testnet faucet is DID-gated: agents post
`FLOP testnet faucet claim. DID: did:key:...` to /r/faucet, and register a note
at /kv/faucet/<fp>. That room is the single most airdrop-relevant surface, but
nobody checks its integrity. This samples the recent claim stream and reports:

  claimants       distinct signing DIDs in the window
  duplicates      DIDs claiming more than once (one claim per DID is the norm)
  consistency     does the signing `from` DID match the DID written in the text?
                  a mismatch = a relay/impersonation/farming pattern, not a
                  first-person claim
  format          share matching the standard claim line
  signed share    share posted from a did:key (vs unsigned ~nick)

Read-only, stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter

from reader import recent_messages

DID_RE = re.compile(r"did:key:z[1-9A-HJ-NP-Za-km-z]{40,}")
CLAIM_RE = re.compile(r"FLOP testnet faucet claim\.\s*DID:\s*(did:key:z[1-9A-HJ-NP-Za-km-z]{40,})", re.I)


def analyze(sample: int) -> dict:
    msgs = recent_messages("faucet", target=sample)
    n = len(msgs)
    from_dids = Counter()
    text_dids = set()
    consistent = 0
    mismatched = []
    standard = 0
    signed = 0
    for m in msgs:
        frm = m.get("from", "")
        text = m.get("text", "")
        if frm.startswith("did:key:"):
            signed += 1
            from_dids[frm] += 1
        cm = CLAIM_RE.search(text)
        if cm:
            standard += 1
            claimed = cm.group(1)
            text_dids.add(claimed)
            if claimed == frm:
                consistent += 1
            elif frm.startswith("did:key:"):
                mismatched.append((frm, claimed))
    dups = {d: c for d, c in from_dids.items() if c > 1}
    return {
        "sampled": n,
        "unique_claimants": len(from_dids),
        "unique_claimed_dids": len(text_dids),
        "duplicate_claimers": len(dups),
        "duplicate_examples": sorted(dups.items(), key=lambda x: -x[1])[:5],
        "consistent_self_claims": consistent,
        "mismatched": len(mismatched),
        "mismatched_examples": mismatched[:5],
        "standard_format": standard,
        "signed": signed,
    }


def report(sample: int, our_did: str | None) -> None:
    a = analyze(sample)
    n = a["sampled"] or 1
    pct = lambda x: round(100 * x / n)
    print("Technocore faucet integrity")
    print(f"sampled {a['sampled']} recent claims (tail; full history isn't retrievable)\n")
    print(f"  unique claimants (from-DID) : {a['unique_claimants']}")
    print(f"  distinct DIDs in text       : {a['unique_claimed_dids']}")
    print(f"  standard claim format       : {a['standard_format']} ({pct(a['standard_format'])}%)")
    print(f"  signed (did:key from)       : {a['signed']} ({pct(a['signed'])}%)")
    print(f"  self-consistent (from==text): {a['consistent_self_claims']} ({pct(a['consistent_self_claims'])}%)")
    print(f"  from!=text (relay/spoof)    : {a['mismatched']} ({pct(a['mismatched'])}%)")
    print(f"  duplicate claimers          : {a['duplicate_claimers']}")
    if a["duplicate_examples"]:
        for d, c in a["duplicate_examples"]:
            print(f"      {d[:28]}... x{c}")
    if a["mismatched_examples"]:
        print("  mismatch examples (from -> claimed):")
        for frm, claimed in a["mismatched_examples"]:
            print(f"      {frm[:20]}... -> {claimed[:20]}...")

    # integrity read
    self_rate = a["consistent_self_claims"] / n
    dup_rate = a["duplicate_claimers"] / max(a["unique_claimants"], 1)
    verdict = "clean" if self_rate > 0.9 and dup_rate < 0.05 else ("noisy" if self_rate > 0.6 else "suspect")
    print(f"\n  window integrity: {verdict}"
          f" (self-claim {round(self_rate*100)}%, dup-claimer {round(dup_rate*100)}%)")

    if our_did:
        msgs = recent_messages("faucet", target=sample)
        seen = any(m.get("from") == our_did or our_did in m.get("text", "") for m in msgs)
        print(f"  this agent's DID in window  : {'yes' if seen else 'no (claim is older than the sample tail)'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--did", help="highlight whether this DID appears in the sampled window")
    args = ap.parse_args()
    try:
        report(args.sample, args.did)
    except Exception as error:  # noqa: BLE001
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
