#!/usr/bin/env python3
"""flopwatch — change watcher for flop.finance (the authoritative FLOP source).

Read-only, stdlib-only. Fetches the public pages listed in PAGES, normalises
them to text, and records an event whenever a page changes: which page, the
"Updated YYYY-MM-DD" stamp before/after, keyword-count deltas, the E.38
(airdrop vesting) status tag, newly linked internal URLs, and a short diff.

State lives under ~/.technocore-pulse/flopwatch/ (one .txt per page) and
events append to ~/.technocore-pulse/flopwatch.jsonl.

    python3 flopwatch.py --once          # one pass, print events
    python3 flopwatch.py --report        # last events, human readable
    python3 flopwatch.py --interval 6    # loop forever (pm2), hours
"""
import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://flop.finance"
PAGES = [
    "/",
    "/teaser/",
    "/intro/",
    "/intro/agent/",
    "/intro/miner/",
    "/intro/validator/",
    "/intro/verification/",
    "/intro/revenue/",
    "/intro/yellowpaper/",
]
# Words whose appearance or count change on the official site matters to us.
KEYWORDS = [
    "faucet", "claim", "testnet", "snapshot", "eligib", "season 0",
    "ratif", "airdrop", "technocore", "did:key", "genesis ceremony",
    "how to", "register", "kyc", "wallet",
]
E38_RE = re.compile(r"E\.38[^\[]{0,160}\[([A-Z]+)\]")
UPDATED_RE = re.compile(r"Updated\s+(\d{4}-\d{2}-\d{2})")
VERSION_RE = re.compile(r"Version\s+([\d.]+\s*\([a-z]+\))")
STATE_DIR = os.path.expanduser("~/.technocore-pulse/flopwatch")
EVENTS = os.path.expanduser("~/.technocore-pulse/flopwatch.jsonl")
UA = "technocore-pulse/flopwatch (read-only; github.com/VolksTiger/technocore-pulse)"
# The other official surface: a new repo in the FLOP Labs org (node, faucet, testnet docs) is
# the signal that the testnet is real; the site may lag it by days.
GITHUB_ORG = "https://api.github.com/orgs/flop-labs/repos?per_page=100"


def fetch(path, timeout=30):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def to_text(raw):
    t = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    t = re.sub(r"<(script|style)\b.*?</\1>", "", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = html.unescape(t)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def internal_links(raw):
    out = set()
    for href in re.findall(r'href="([^"]+)"', raw):
        if href.startswith("/") and not href.startswith("/assets/"):
            out.add(href)
        elif href.startswith("http") and "flop.finance" in href:
            out.add(href)
    return sorted(out)


def analyse(text):
    low = text.lower()
    kw = {k: low.count(k) for k in KEYWORDS}
    dates = UPDATED_RE.findall(text)
    v = VERSION_RE.search(text)
    e = E38_RE.search(text)
    return {
        "sha": hashlib.sha256(text.encode()).hexdigest()[:16],
        "chars": len(text),
        "updated": max(dates) if dates else None,
        "version": v.group(1) if v else None,
        "e38": e.group(1) if e else None,
        "kw": kw,
    }


def slug(path):
    return path.strip("/").replace("/", "_") or "home"


def load_state(path):
    p = os.path.join(STATE_DIR, slug(path) + ".json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def save_state(path, meta, text, links):
    os.makedirs(STATE_DIR, exist_ok=True)
    base = os.path.join(STATE_DIR, slug(path))
    with open(base + ".txt", "w") as f:
        f.write(text)
    with open(base + ".json", "w") as f:
        json.dump({"meta": meta, "links": links}, f)


def short_diff(old_text, new_text, limit=40):
    diff = difflib.unified_diff(
        old_text.split("\n"), new_text.split("\n"), lineterm="", n=0)
    out = [ln for ln in diff if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))]
    return out[:limit], len(out)


def check_page(path):
    """Return an event dict if the page changed (or is new), else None."""
    try:
        raw = fetch(path)
    except (urllib.error.URLError, OSError) as exc:
        return {"ts": now(), "page": path, "error": str(exc)[:200]}
    text = to_text(raw)
    meta = analyse(text)
    links = internal_links(raw)
    prev = load_state(path)
    if prev is None:
        save_state(path, meta, text, links)
        return {"ts": now(), "page": path, "baseline": True, "meta": meta}
    if prev["meta"]["sha"] == meta["sha"]:
        return None
    old_txt_path = os.path.join(STATE_DIR, slug(path) + ".txt")
    old_text = open(old_txt_path).read() if os.path.exists(old_txt_path) else ""
    diff_lines, diff_total = short_diff(old_text, text)
    kw_delta = {k: (prev["meta"]["kw"].get(k, 0), meta["kw"][k])
                for k in KEYWORDS if prev["meta"]["kw"].get(k, 0) != meta["kw"][k]}
    new_links = sorted(set(links) - set(prev.get("links", [])))
    ev = {
        "ts": now(),
        "page": path,
        "updated": [prev["meta"].get("updated"), meta["updated"]],
        "version": [prev["meta"].get("version"), meta["version"]],
        "e38": [prev["meta"].get("e38"), meta["e38"]],
        "chars": [prev["meta"]["chars"], meta["chars"]],
        "kw_delta": kw_delta,
        "new_links": new_links,
        "diff_lines": diff_total,
        "diff": diff_lines,
    }
    save_state(path, meta, text, links)
    return ev


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_event(ev):
    os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
    with open(EVENTS, "a") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def check_github():
    """Event when the flop-labs org gains a repo (or one is renamed away)."""
    req = urllib.request.Request(GITHUB_ORG, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            repos = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ts": now(), "page": "github:flop-labs", "error": str(exc)[:200]}
    if not isinstance(repos, list):
        return {"ts": now(), "page": "github:flop-labs", "error": str(repos)[:200]}
    names = sorted(r["name"] for r in repos)
    p = os.path.join(STATE_DIR, "github.json")
    prev = json.load(open(p))["names"] if os.path.exists(p) else None
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(p, "w") as f:
        json.dump({"names": names, "ts": now()}, f)
    if prev is None:
        return {"ts": now(), "page": "github:flop-labs", "baseline": True,
                "meta": {"sha": "-", "chars": len(names), "updated": None, "version": None, "e38": None, "kw": {}, "repos": names}}
    if prev == names:
        return None
    added = sorted(set(names) - set(prev))
    removed = sorted(set(prev) - set(names))
    return {"ts": now(), "page": "github:flop-labs", "updated": [None, None], "version": [None, None], "e38": [None, None],
            "chars": [len(prev), len(names)], "kw_delta": {}, "new_links": ["github.com/flop-labs/" + a for a in added],
            "diff_lines": len(added) + len(removed),
            "diff": ["+" + a for a in added] + ["-" + r for r in removed]}


def run_once(verbose=True):
    events = []
    for path in PAGES:
        ev = check_page(path)
        if ev is None:
            continue
        events.append(ev)
        append_event(ev)
        if verbose:
            print(json.dumps(ev, ensure_ascii=False))
    ev = check_github()
    if ev is not None:
        events.append(ev)
        append_event(ev)
        if verbose:
            print(json.dumps(ev, ensure_ascii=False))
    return events


def report(last=20):
    if not os.path.exists(EVENTS):
        print("no events yet")
        return
    with open(EVENTS) as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    changes = [r for r in rows if "diff" in r]
    errors = [r for r in rows if "error" in r]
    print("events: %d  changes: %d  errors: %d  first: %s  last: %s" % (
        len(rows), len(changes), len(errors),
        rows[0]["ts"] if rows else "-", rows[-1]["ts"] if rows else "-"))
    for r in rows[-last:]:
        if "baseline" in r:
            m = r["meta"]
            if "repos" in m:
                print("%s  BASELINE %-22s repos=%s" % (r["ts"], r["page"], ", ".join(m["repos"])))
                continue
            print("%s  BASELINE %-22s updated=%s version=%s e38=%s chars=%d" % (
                r["ts"], r["page"], m.get("updated"), m.get("version"), m.get("e38"), m["chars"]))
        elif "error" in r:
            print("%s  ERROR    %-22s %s" % (r["ts"], r["page"], r["error"]))
        else:
            print("%s  CHANGE   %-22s updated %s->%s  version %s->%s  e38 %s->%s  chars %d->%d  diff=%d" % (
                r["ts"], r["page"], r["updated"][0], r["updated"][1],
                r.get("version", [None, None])[0], r.get("version", [None, None])[1],
                r["e38"][0], r["e38"][1], r["chars"][0], r["chars"][1], r["diff_lines"]))
            if r["kw_delta"]:
                print("      keywords:", ", ".join("%s %d->%d" % (k, v[0], v[1]) for k, v in r["kw_delta"].items()))
            if r["new_links"]:
                print("      new links:", ", ".join(r["new_links"]))
            for ln in r["diff"][:12]:
                print("      " + ln[:160])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--report", action="store_true", help="print recent events")
    ap.add_argument("--interval", type=float, default=6.0, help="hours between passes (loop mode)")
    args = ap.parse_args()
    if args.report:
        report()
        return
    if args.once:
        evs = run_once()
        print("%s pass done, %d event(s)" % (now(), len(evs)), file=sys.stderr)
        return
    while True:
        try:
            evs = run_once(verbose=False)
            print("%s pass done, %d event(s)" % (now(), len(evs)), flush=True)
            for ev in evs:
                if "diff" in ev:
                    print("CHANGE %s updated %s version %s e38 %s kw %s new_links %s" % (
                        ev["page"], ev["updated"], ev["version"], ev["e38"], ev["kw_delta"], ev["new_links"]), flush=True)
        except Exception as exc:  # keep the loop alive on anything unexpected
            print("%s pass failed: %s" % (now(), exc), flush=True)
        time.sleep(args.interval * 3600)


if __name__ == "__main__":
    main()
