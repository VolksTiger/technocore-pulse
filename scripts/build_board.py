#!/usr/bin/env python3
"""build_board — inject the latest boardintel result (and the recorder series) into board.html.

    python3 scripts/build_board.py --jsonl ~/.technocore-pulse/boardintel.jsonl > board.built.html
    python3 boardintel.py --json > latest.json && python3 scripts/build_board.py --json latest.json > board.built.html

board.html carries two placeholders, __DATA__ (one boardintel result) and __SERIES__ (a compact
time series from the recorder's JSONL: one point per run). Output is a self-contained page.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(os.path.dirname(HERE), "board.html")


def series_point(r: dict) -> dict:
    fb = r.get("first_bidder", {})
    v = fb.get("verdicted_offers", {})
    a = fb.get("all_offers_with_accept", {})
    b = r.get("bidders", {})
    tot = (b.get("valid_accepts") or 0) + (b.get("invalid_accepts") or 0)
    return {
        "t": r.get("generated_at"),
        "first_share": v.get("share_won_by_first_accept"),
        "win_median": (v.get("winning_accept_latency_s") or {}).get("median"),
        "within2": a.get("share_first_accept_within_2s"),
        "invalid_share": (b.get("invalid_accepts") or 0) / tot if tot else None,
        "offers": (r.get("window") or {}).get("offers_on_board"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", help="recorder output; the last line is the page's data, all lines the series")
    ap.add_argument("--json", help="a single boardintel --json result (no series)")
    ap.add_argument("--template", default=TEMPLATE)
    a = ap.parse_args()
    if not a.jsonl and not a.json:
        ap.error("--jsonl or --json required")
    rows = []
    if a.jsonl:
        with open(os.path.expanduser(a.jsonl), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    if a.json:
        with open(os.path.expanduser(a.json), encoding="utf-8") as f:
            rows.append(json.load(f))
    if not rows:
        print("no data", file=sys.stderr)
        return 1
    data = rows[-1]
    series = [series_point(r) for r in rows if r.get("first_bidder")]
    html = open(a.template, encoding="utf-8").read()
    # ensure_ascii keeps every non-ASCII code point escaped: the artifact renderer chokes on raw unicode in scripts
    html = html.replace("__DATA__", json.dumps(data, ensure_ascii=True, separators=(",", ":")))
    html = html.replace("__SERIES__", json.dumps(series, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write(asciify(html))
    return 0


def asciify(html: str) -> str:
    """Escape every non-ASCII code point: \\uXXXX inside <script>, &#N; in markup. The artifact
    renderer has rendered pages blank on raw unicode before (see reference_artifact_gotchas)."""
    out = []
    parts = html.split("<script>")
    out.append("".join(c if ord(c) < 128 else f"&#{ord(c)};" for c in parts[0]))
    for part in parts[1:]:
        script, _, rest = part.partition("</script>")
        out.append("<script>" + "".join(c if ord(c) < 128 else f"\\u{ord(c):04x}" for c in script) + "</script>"
                   + "".join(c if ord(c) < 128 else f"&#{ord(c)};" for c in rest))
    return "".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
