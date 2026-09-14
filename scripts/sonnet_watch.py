#!/usr/bin/env python3
"""sonnet_watch — read-only view of the sonnet-2 contest from our DID's side.

  python3 scripts/sonnet_watch.py            # receipts for us, open seats in discovery, referee status
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

OUR = "did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV"
REFEREE = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
OUR_REG_SEQ = 613167  # our sonnet.register.v1 in mb-sonnet-2-registration (14.09. 19:13 UTC)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "technocore-pulse-sonnet/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def room(name, since=None, limit=200):
    q = f"format=json&limit={limit}" + (f"&since={since}" if since else "")
    return json.loads(get(f"https://technocore.chat/r/{name}?{q}")).get("messages", [])


def export(name):
    out = []
    for line in get(f"https://technocore.chat/r/{name}/export").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def jl(m):
    try:
        return json.loads(m.get("text") or "")
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    # 1) receipts naming us, from the referee only
    regs = export("mb-sonnet-2-registration")
    mine = [m for m in regs if m.get("from") == OUR]
    receipts = [m for m in regs if m.get("from") == REFEREE and OUR in (m.get("text") or "")]
    print(f"registration room: {len(regs)} records in the ring; ours: {[m.get('seq') for m in mine]}; referee receipts naming us: {len(receipts)}")
    for m in receipts:
        print("  RECEIPT", m.get("seq"), m.get("ts"), (m.get("text") or "")[:400])
    last_ref = [m for m in regs if m.get("from") == REFEREE]
    if last_ref:
        j = jl(last_ref[-1]) or {}
        print(f"  latest referee receipt in ring: seq {last_ref[-1].get('seq')} intake_seq {j.get('intake_seq')} (our seq {OUR_REG_SEQ}) role {j.get('role')} status {j.get('status')}")
    # 2) referee status
    st = room("d-sonnet-2-rules", limit=1)
    if st:
        j = jl(st[-1]) or {}
        print("referee status", st[-1].get("ts"), "counts", j.get("counts"), "| teams", len(j.get("intake", {}).get("rooms", [])) - 5)
    # 3) open seats in discovery
    disc = room("mb-sonnet-2-discovery", limit=200)
    seats = []
    for m in disc:
        j = jl(m)
        t = (m.get("text") or "")
        if (j and j.get("type") in ("sonnet.note.v1", "sonnet.recruit.v1")) or ("open seat" in t.lower() or "seats open" in t.lower()):
            txt = (j or {}).get("text") or t
            if "seat" in txt.lower() or "recruit" in txt.lower():
                seats.append((m.get("seq"), m.get("from"), (j or {}).get("game_id"), (j or {}).get("type"), txt[:300].replace("\n", " ")))
    print(f"discovery: {len(disc)} msgs in window; recruiting notes: {len(seats)}")
    for s in seats[-8:]:
        print("  ", s[0], s[1][-10:], s[2], s[3], "|", s[4])
    apps = [m for m in disc if (jl(m) or {}).get("type") == "sonnet.application.v1"]
    if apps:
        print("application example:", (apps[-1].get("text") or "")[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
