"""sonnet_autoconsent — keep our roster consent in sync with the organizer's latest roster, automatically.

Team rosters in sonnet-2 churn: members hop, the organizer re-posts `sonnet.roster.v1` with a changed
members[] and everyone has to countersign again. The referee rejects a changed consent unless the old one is
withdrawn first ("consent: withdraw before changing"), and only one live consent per DID is allowed. Waiting
for a human (or an hourly check) costs minutes; this loop answers in ~30 s through the signer outbox.

Rule: the latest organizer roster that names our DID wins. On such a roster for game G:
  1. withdraw from any other game we currently consent to,
  2. withdraw from G if our consented members[] differs,
  3. consent to G with the exact members[] (fresh request_id).
Everything is queued to ~/.technocore-pulse/outbox.jsonl (signer.py signs and posts). State survives restarts in
~/.technocore-pulse/autoconsent.json. Logs roster_ready receipts loudly so the play step can start.

  python3 scripts/sonnet_autoconsent.py --games prophet:qf9vthSUn,brucelead:LpfVgqSM --hours 48
Run detached on the Mac (the signer key lives there; macOS has no setsid):
  (nohup python3 scripts/sonnet_autoconsent.py --games ... --hours 48 > ~/.technocore-pulse/autoconsent.log 2>&1 &)
On roster_ready it spawns scripts/sonnet_kickoff.sh, which waits for the organizer's word table and starts the play loop.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

OUR = "did:key:z6Mkpf39RnfLwF5ugzbXK52paFRqd6Fz5MoK7TqrrxgHVjrV"
REF = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
DISCOVERY = "mb-sonnet-2-discovery"
HOME = os.path.expanduser("~/.technocore-pulse")
OUTBOX = os.path.join(HOME, "outbox.jsonl")
STATE = os.path.join(HOME, "autoconsent.json")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def read(room, since=None, limit=200):
    q = f"format=json&limit={limit}" + (f"&since={since}" if since else "")
    req = urllib.request.Request(f"https://technocore.chat/r/{room}?{q}", headers={"User-Agent": "technocore-pulse autoconsent"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("messages", [])
    except Exception as exc:  # noqa: BLE001 -- transient venue errors just skip a poll
        log(f"read error: {exc}")
        return []


def queue(item_id, obj):
    with open(OUTBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": item_id, "room": DISCOVERY, "text": json.dumps(obj, separators=(",", ":"))}, ensure_ascii=False) + "\n")
    log(f"queued {item_id}: {json.dumps(obj)[:140]}")


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"cursor": 0, "consented": {}}  # consented: game -> {"members": [...], "generation": n, "room": ...}


def save_state(state):
    with open(STATE, "w") as f:
        json.dump(state, f, indent=1)


def withdraw(game):
    rid = f"withdraw-{game}-{int(time.time() * 1000)}"
    queue(rid, {"type": "sonnet.withdraw.v1", "contest_id": "sonnet-2", "game_id": game, "request_id": rid})


def consent(game, room, generation, members):
    rid = f"roster-{game}-{int(time.time() * 1000)}"
    queue(rid, {"type": "sonnet.roster.v1", "contest_id": "sonnet-2", "game_id": game, "poem_room": room,
                "room_generation": generation, "members": members, "request_id": rid})


def kickoff(state, game, organizer_suffix):
    """Spawn sonnet_kickoff.sh detached: it waits for the organizer's word table, builds our schedule and starts play."""
    info = state["consented"].get(game)
    if not info:
        log(f"roster_ready for {game} but we hold no consent there; ignoring")
        return
    if state.get("kicked", {}).get(game):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.Popen(["sh", os.path.join(here, "sonnet_kickoff.sh"), game, organizer_suffix, str(info["generation"])],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    state.setdefault("kicked", {})[game] = time.time()
    save_state(state)
    log(f"kickoff spawned for {game} (generation {info['generation']})")


def handle_roster(state, game, j):
    members = j.get("members") or []
    room, generation = j.get("poem_room"), j.get("room_generation")
    if OUR not in members or not (4 <= len(members) <= 8) or not room or generation is None:
        return
    current = state["consented"].get(game)
    if current and current["members"] == members and current["generation"] == generation:
        return  # already consented to exactly this roster
    for other, info in list(state["consented"].items()):
        if other != game:
            log(f"organizer of {game} posted a roster naming us; withdrawing live consent from {other}")
            withdraw(other)
            del state["consented"][other]
    if current:
        log(f"{game}: members changed; withdraw before changing")
        withdraw(game)
    consent(game, room, generation, members)
    state["consented"][game] = {"members": members, "generation": generation, "room": room, "at": time.time()}
    save_state(state)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", required=True, help="comma list of game:organizerDidSuffix, e.g. prophet:qf9vthSUn,brucelead:LpfVgqSM")
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--poll", type=float, default=20)
    ap.add_argument("--seed-consent", default=None, help="game:generation:member1,member2,... to record an existing live consent")
    a = ap.parse_args()

    organizers = {}
    for item in a.games.split(","):
        game, suffix = item.split(":", 1)
        organizers[game.strip()] = suffix.strip()
    state = load_state()
    if a.seed_consent:
        game, gen, mem = a.seed_consent.split(":", 2)
        state["consented"][game] = {"members": mem.split(","), "generation": int(gen), "room": f"d-sonnet-2-team-{game}", "at": time.time()}
        save_state(state)
    if not state.get("cursor"):
        tail = read(DISCOVERY)
        state["cursor"] = max((int(m.get("seq") or 0) for m in tail), default=0)
        save_state(state)
    log(f"watching {organizers} from seq {state['cursor']}; consented={ {g: len(v['members']) for g, v in state['consented'].items()} }")

    deadline = time.time() + a.hours * 3600
    while time.time() < deadline:
        for m in read(DISCOVERY, since=state["cursor"]):
            seq = int(m.get("seq") or 0)
            if seq <= state["cursor"]:
                continue
            state["cursor"] = seq
            t = m.get("text") or ""
            frm = m.get("from") or ""
            try:
                j = json.loads(t)
            except ValueError:
                continue
            game = j.get("game_id")
            if frm == REF:
                if j.get("roster_ready") is True:
                    # roster receipts carry no game_id: the game is ours if the signer is one of our consented members
                    g = game if game in organizers else next(
                        (gm for gm, info in state["consented"].items() if j.get("sender_did") in info["members"]), None)
                    if g in organizers:
                        log(f"*** ROSTER READY for {g}: {t[:300]}")
                        kickoff(state, g, organizers[g])
                elif OUR in t and j.get("status") == "rejected":
                    log(f"referee rejected our {j.get('request_id')}: {j.get('reason')}")
                continue
            if j.get("type") == "sonnet.roster.v1" and game in organizers and frm.endswith(organizers[game]):
                handle_roster(state, game, j)
        save_state(state)
        time.sleep(a.poll)
    log("deadline reached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
