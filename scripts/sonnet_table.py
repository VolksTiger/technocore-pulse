"""sonnet_table — extract OUR words from an organizer's index:word:signer table and write a play schedule.

The organizer of a sonnet-2 team publishes the full word table (which member posts which word) in the team room.
This tool reads it, keeps the entries assigned to our DID, checks every one with the contest validator
(frozen CMUdict + letters-in-DID rule) and writes the schedule file sonnet_play.py's `play` command consumes.

  python3 scripts/sonnet_table.py --game prophet --out ~/.technocore-pulse/play-prophet.json
  python3 scripts/sonnet_table.py --game prophet --text table.txt --out play.json   # from a pasted file

Accepted table shapes (tolerant): `12:word:did:key:z6Mk...`, `12:word:rxgHVjrV`, `12 word @rxgHVjrV`,
`12|word|did...`, or JSON with a list of {"index","word","signer"/"did"} objects under any key.
Exit codes: 0 schedule written; 2 some of our words fail validation (schedule still written for the valid ones);
3 no table found.
"""

import argparse
import json
import os
import re
import sys
import urllib.request

OUR = "did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV"
VALIDATOR_DIR = os.path.expanduser("~/.technocore-pulse/sonnet")  # local copy of the contest validator + cmudict
sys.path.insert(0, VALIDATOR_DIR)

ENTRY = re.compile(
    r"(?<![\w-])(\d{1,3})\s*[:|=\-]?\s+?([A-Za-z]+(?:'[A-Za-z]+)*[,.;:!?]?)\s*[:|=\-]?\s*@?"
    r"(did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}|[1-9A-HJ-NP-Za-km-z]{6,12})(?![\w-])"
)
ENTRY_COLON = re.compile(
    r"(?<![\w-])(\d{1,3}):([A-Za-z]+(?:'[A-Za-z]+)*[,.;:!?]?):@?(did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}|[1-9A-HJ-NP-Za-km-z]{6,12})(?![\w-])"
)


def read_room(room, limit=300):
    req = urllib.request.Request(f"https://technocore.chat/r/{room}?format=json&limit={limit}", headers={"User-Agent": "technocore-pulse"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("messages", [])


def is_ours(signer):
    return signer == OUR or (len(signer) >= 6 and OUR.endswith(signer))


def entries_from_json(obj, out):
    """Walk any JSON shape and collect {index, word, signer} objects."""
    if isinstance(obj, dict):
        keys = {k.lower() for k in obj}
        if {"index", "word"} <= keys:
            signer = obj.get("signer") or obj.get("did") or obj.get("member") or obj.get("by") or ""
            try:
                out.append((int(obj.get("index")), str(obj.get("word")), str(signer)))
            except (TypeError, ValueError):
                pass
        for v in obj.values():
            entries_from_json(v, out)
    elif isinstance(obj, list):
        for v in obj:
            entries_from_json(v, out)


def entries_from_text(text):
    out = []
    try:
        entries_from_json(json.loads(text), out)
    except (ValueError, TypeError):
        pass
    if out:
        return out
    seen = set()
    for rx in (ENTRY_COLON, ENTRY):
        for m in rx.finditer(text):
            key = (int(m.group(1)), m.group(2))
            if key in seen:
                continue
            seen.add(key)
            out.append((int(m.group(1)), m.group(2), m.group(3)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", required=True)
    ap.add_argument("--text", help="read the table from this file instead of the team room")
    ap.add_argument("--organizer", default=None, help="only trust table messages from this DID (or DID suffix)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    from sonnet_validate import read_lexicon, validate_word  # noqa: E402  (local copy of the contest validator)
    from pathlib import Path
    lexicon = read_lexicon(Path(VALIDATOR_DIR) / "cmudict.dict")

    texts = []
    if a.text:
        texts.append(open(a.text, encoding="utf-8").read())
    else:
        for m in read_room(f"d-sonnet-2-team-{a.game}"):
            frm = m.get("from") or ""
            if a.organizer and not (frm == a.organizer or frm.endswith(a.organizer)):
                continue
            texts.append(m.get("text") or "")

    table = {}
    for t in texts:
        for idx, word, signer in entries_from_text(t):
            table[idx] = (word, signer)  # later messages override earlier ones (organizer corrections)
    if not table:
        print("no index:word:signer table found", file=sys.stderr)
        return 3

    ours = {i: w for i, (w, s) in sorted(table.items()) if is_ours(s)}
    total = max(table)
    print(f"table: {len(table)} entries, highest index {total}; ours: {len(ours)} -> {sorted(ours)}")
    bad = []
    for i, w in sorted(ours.items()):
        try:
            syl = validate_word(w, OUR, lexicon)
            print(f"  {i:3d} {w:<14} ok ({syl} syl)")
        except ValueError as exc:
            bad.append(i)
            print(f"  {i:3d} {w:<14} INVALID: {exc}")
    consecutive = [i for i in ours if i + 1 in ours]
    if consecutive:
        print(f"WARNING: consecutive indices assigned to us (rule: never two in a row): {consecutive}")

    schedule = {"game_id": a.game, "words": {str(i): w for i, w in ours.items() if i not in bad}}
    with open(os.path.expanduser(a.out), "w", encoding="utf-8") as f:
        json.dump(schedule, f, indent=1, ensure_ascii=False)
    print(f"wrote {a.out} ({len(schedule['words'])} words)")
    return 2 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
