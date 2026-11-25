#!/usr/bin/env python3
"""flop_spend_loop — buy inference on FLOP with our own wallet, in a loop, within hard limits.

The airdrop's agent leg is pro rata to compute *purchased in settled sessions* by the paying
wallet (flop.finance/airdrop, 5 Oct 2026). The public gateway spends broker funds, so only a
direct session — our funded wallet → a named miner — counts for us. This loop drives the official
CLI (`@flop-labs/cli`, `flop session run`) and never touches keys itself: the CLI reads the
encrypted keystore and its password from an environment variable.

Per round:
  1. balance (`flop balance <addr> --json`); below --min-balance → faucet
     (`flop --network N faucet request <addr> [--amount A] --json`), once per --faucet-cooldown
  2. pick the next miner from --miners (JSON list of {"address","endpoint","model"?}), skipping
     miners with --miner-strikes consecutive failures for --miner-cooldown seconds
  3. pick the next prompt from --prompts (JSON list of strings; a real workload, not noise)
  4. `flop --network N --keystore K --password-env E session run --miner M --model MODEL
      --prompt P --max-flop X --via auto --endpoint URL --json`, parse the JSON, append one line to
     --ledger (jsonl): time, miner, model, max_flop, settled fields the CLI reports, outcome
  5. sleep --interval; stop when any of --max-runs, --max-hours, --max-flop-per-day is reached,
     or after --max-consecutive-failures (then sleep --failure-sleep and continue)

Alerts (one line each to --alerts, default ~/.technocore-pulse/ALERTS.txt): first settled session,
every failure streak, faucet refusals, stop conditions. `--dry-run` prints the commands only.

  python3 scripts/flop_spend_loop.py --network testnet --keystore ~/.flop/testnet-agent.json \
    --address 5... --miners devnet/miners-testnet.json --prompts data/spend-prompts.json \
    --max-flop 0.05 --interval 90 --max-flop-per-day 20 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"{now()} {msg}", flush=True)


def alert(path: str, msg: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{now()[:16]} FLOPSPEND {msg}\n")


def run(cmd: list[str], timeout: float, dry: bool):
    """Run a CLI command; return (returncode, parsed_json_or_None, raw_text)."""
    if dry:
        log("DRY " + " ".join(shlex.quote(c) for c in cmd))
        return 0, None, ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, None, "timeout"
    except FileNotFoundError:
        return 127, None, "flop CLI not found on PATH (npm i -g @flop-labs/cli)"
    raw = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
    parsed = None
    for chunk in (p.stdout or "").strip().splitlines()[::-1]:
        chunk = chunk.strip()
        if chunk.startswith("{"):
            try:
                parsed = json.loads(chunk)
                break
            except json.JSONDecodeError:
                continue
    if parsed is None and (p.stdout or "").strip().startswith("{"):
        try:
            parsed = json.loads(p.stdout)
        except json.JSONDecodeError:
            pass
    return p.returncode, parsed, raw


def flop_base(a) -> list[str]:
    return [a.flop, "--network", a.network]


def balance_rpc(a) -> float | None:
    """stdlib fallback: System.Account via state_getStorage (scripts/flop_balance.py)."""
    here = os.path.dirname(os.path.abspath(__file__))
    rpc_url = a.rpc or {"testnet": "https://testnet.flop.network", "devnet": "https://rpc.devnet.flop.network"}.get(a.network)
    if not rpc_url:
        return None
    code, j, raw = run([sys.executable, os.path.join(here, "flop_balance.py"), a.address, "--rpc", rpc_url, "--json"], 60, False)
    if code == 0 and isinstance(j, dict) and "free_flop" in j:
        return float(j["free_flop"])
    log(f"balance(rpc): rc={code} {raw[-160:]}")
    return None


def balance(a) -> float | None:
    code, j, raw = run(flop_base(a) + ["balance", a.address, "--json"], 60, a.dry_run)
    if a.dry_run:
        return None
    if code != 0 or not isinstance(j, dict) or j.get("ok") is False:
        log(f"balance(cli): rc={code} {raw[:160]}; trying RPC")
        return balance_rpc(a)
    for k in ("free", "balance", "free_flop", "available"):
        v = j.get(k)
        if v is not None:
            try:
                v = float(v)
                return v / 1e18 if v > 1e12 else v  # raw plancks vs FLOP
            except (TypeError, ValueError):
                pass
    log("balance: unrecognised JSON " + json.dumps(j)[:200])
    return None


def faucet_claim(a) -> str:
    """Testnet signed agent claim through scripts/flop_faucet_claim.py (the CLI has no command for it yet)."""
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "flop_faucet_claim.py"), "--network", a.network, "--keystore", a.keystore,
           "--password-env", a.password_env, "--address", a.address, "--json"]
    code, j, raw = run(cmd, 240, a.dry_run)
    if a.dry_run:
        return "ok"
    claim = (j or {}).get("claim", {}) if isinstance(j, dict) else {}
    log(f"faucet claim: rc={code} " + json.dumps(claim.get("response") if claim else (j or raw[-300:]))[:300])
    if code == 0:
        return "ok"
    if code in (3, 5):  # faucet not open / outcome unknown — re-check later, same nonce is re-sent
        return "pending"
    if code == 4:
        alert(a.alerts, f"testnet faucet refused {a.address[:8]}…: {json.dumps(claim.get('response'))[:160]}")
        return "refused"
    alert(a.alerts, f"faucet claim error rc={code}: {(raw or json.dumps(j))[-160:]}")
    return "pending"


def faucet(a) -> str:
    """'ok' (grant accepted), 'pending' (queued or outcome unknown: re-check the balance soon,
    never re-ask in a loop), or 'refused'."""
    if a.network == "testnet":
        return faucet_claim(a)
    cmd = flop_base(a) + ["faucet", "request", a.address, "--json"]
    if a.faucet_amount:
        cmd += ["--amount", str(a.faucet_amount)]
    code, j, raw = run(cmd, 180, a.dry_run)
    log(f"faucet: rc={code} {json.dumps(j)[:300] if j else raw[:300]}")
    if a.dry_run:
        return "ok"
    j = j if isinstance(j, dict) else {}
    text = json.dumps(j).lower() + raw.lower()
    if code == 0 and j.get("ok", j.get("success", True)) and j.get("outcome", "accepted") == "accepted":
        return "ok"
    if any(s in text for s in ("outcome_unknown", "grant_pending", "retry_later", "retryafter", "capacity exhausted", "\"outcome\": \"unknown\"", "degraded")):
        return "pending"
    alert(a.alerts, f"faucet refused for {a.address[:8]}…: {json.dumps(j)[:160] if j else raw[:160]}")
    return "refused"


def session(a, miner: dict, prompt: str):
    model = miner.get("model") or a.model
    cmd = flop_base(a) + ["--keystore", a.keystore, "--password-env", a.password_env, "session", "run",
                          "--miner", miner["address"], "--model", model, "--prompt", prompt,
                          "--max-flop", str(a.max_flop), "--max-tokens", str(a.max_tokens), "--via", a.via, "--json"]
    if miner.get("endpoint"):
        cmd += ["--endpoint", miner["endpoint"]]
    code, j, raw = run(cmd, a.session_timeout, a.dry_run)
    return code, j, raw, model


def summarize(j: dict | None) -> dict:
    """Pull the settlement facts out of whatever shape the CLI's JSON has."""
    out = {}
    if not isinstance(j, dict):
        return out
    flat = {}

    def walk(o, prefix=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{prefix}{k}.")
        else:
            flat[prefix[:-1]] = o
    walk(j)
    for k, v in flat.items():
        kl = k.lower()
        if any(s in kl for s in ("channel", "settle", "minerpaid", "miner_paid", "paid", "escrow", "turn", "gn", "g_n", "tx", "block", "outcome", "status", "stage", "reply", "content", "miner")):
            if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 200:
                out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", default="testnet")
    ap.add_argument("--flop", default="flop", help="CLI binary")
    ap.add_argument("--rpc", default=None, help="HTTP JSON-RPC for the stdlib balance fallback (default per network)")
    ap.add_argument("--keystore", required=True)
    ap.add_argument("--password-env", default="FLOP_KEY_PASSWORD")
    ap.add_argument("--address", required=True, help="the keystore's SS58 address")
    ap.add_argument("--miners", required=True, help="JSON list of {address, endpoint, model?}")
    ap.add_argument("--prompts", required=True, help="JSON list of prompt strings")
    ap.add_argument("--model", default="qwen/qwen2.5-0.5b-instruct")
    ap.add_argument("--via", default="auto", choices=["auto", "offer", "force"])
    ap.add_argument("--max-flop", type=float, default=0.05, help="escrow ceiling per session, FLOP")
    ap.add_argument("--max-tokens", type=int, default=160, help="completion tokens per session (CLI/SDK default 256)")
    ap.add_argument("--min-balance", type=float, default=1.0, help="ask the faucet below this, FLOP")
    ap.add_argument("--faucet-amount", type=float, default=None)
    ap.add_argument("--faucet-cooldown", type=float, default=6 * 3600, help="seconds between faucet asks")
    ap.add_argument("--interval", type=float, default=120, help="seconds between sessions")
    ap.add_argument("--session-timeout", type=float, default=600)
    ap.add_argument("--max-runs", type=int, default=0, help="0 = unlimited")
    ap.add_argument("--max-hours", type=float, default=0, help="0 = unlimited")
    ap.add_argument("--max-flop-per-day", type=float, default=50.0, help="sum of max_flop escrows per UTC day")
    ap.add_argument("--max-consecutive-failures", type=int, default=6)
    ap.add_argument("--failure-sleep", type=float, default=1800)
    ap.add_argument("--miner-strikes", type=int, default=3)
    ap.add_argument("--miner-cooldown", type=float, default=3600)
    ap.add_argument("--ledger", default=os.path.expanduser("~/.technocore-pulse/flopspend.jsonl"))
    ap.add_argument("--alerts", default=os.path.expanduser("~/.technocore-pulse/ALERTS.txt"))
    ap.add_argument("--wait-for-network", action="store_true",
                    help="before anything, poll scripts/flop_net.py <network> every 5 min until it exits 0 (published, genesis match)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not a.dry_run and not os.environ.get(a.password_env):
        print(f"{a.password_env} is not set; the CLI cannot open the keystore", file=sys.stderr)
        return 2
    prompts = json.load(open(a.prompts))
    if not prompts:
        print("empty prompts", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(a.ledger), exist_ok=True)

    if a.wait_for_network and not a.dry_run:
        here = os.path.dirname(os.path.abspath(__file__))
        net_out = os.path.join(os.path.dirname(a.ledger), f"net-{a.network}.json")
        announced = False
        while True:
            rc = subprocess.run([sys.executable, os.path.join(here, "flop_net.py"), a.network, "--write", net_out],
                                capture_output=True, text=True, timeout=120).returncode
            if rc in (0, 3, 5):
                # 0: faucet known and consistent; 3: chain live, faucet not published — the CLI's own
                # `faucet request` is the real gate for funds, so start the loop and let it wait on the faucet
                log(f"{a.network} chain is live (flop_net exit {rc}; {'faucet consistent' if rc == 0 else 'faucet not published yet' if rc == 3 else 'RPC saturated (429)'}) ({net_out})")
                alert(a.alerts, f"{a.network} chain live ({'faucet ok' if rc == 0 else 'no faucet yet' if rc == 3 else 'rpc 429'}); spend loop starting")
                break
            if not announced:
                log(f"waiting for {a.network} to be published (flop_net exit {rc}); polling every 5 min")
                announced = True
            time.sleep(300)

    # funds first: a claim takes time to finalize and the faucet queue is long, so ask before the miner list exists
    if not a.dry_run:
        bal0 = balance(a)
        log(f"initial balance: {bal0 if bal0 is None else f'{bal0:.4f} FLOP'}")
        if bal0 is None or bal0 < a.min_balance:
            outcome0 = faucet(a)
            log(f"initial faucet outcome: {outcome0}")

    def load_miners():
        try:
            ms = json.load(open(a.miners))
        except (OSError, json.JSONDecodeError):
            return []
        return [m for m in ms if isinstance(m, dict) and m.get("address", "").startswith("5") and "<" not in m.get("address", "")]

    miners = load_miners()
    waited = False
    while not miners and not a.dry_run:
        if not waited:
            log(f"no usable miners in {a.miners}; fill it with {{address, endpoint}} pairs — polling every 5 min")
            alert(a.alerts, f"spend loop idle: {a.miners} has no miners yet")
            waited = True
        time.sleep(300)
        miners = load_miners()
    if not miners:
        print("empty miners (dry run)", file=sys.stderr)
        return 2

    log(f"start network={a.network} address={a.address} miners={len(miners)} prompts={len(prompts)} max_flop={a.max_flop} "
        f"interval={a.interval}s cap/day={a.max_flop_per_day} dry={a.dry_run}")
    t0 = time.time()
    runs = settled = 0
    consecutive_fail = 0
    last_faucet = 0.0
    faucet_wait = 0.0  # first ask is immediate; afterwards --faucet-cooldown, or 10 min while a grant is pending
    strikes = {m["address"]: 0 for m in miners}
    benched_until = {m["address"]: 0.0 for m in miners}
    spent_day = {"day": now()[:10], "flop": 0.0}
    first_settle_alerted = False
    mi = pi = 0

    while True:
        if a.max_runs and runs >= a.max_runs:
            log("stop: max runs"); alert(a.alerts, f"stopped: max runs {runs} (settled {settled})"); break
        if a.max_hours and time.time() - t0 > a.max_hours * 3600:
            log("stop: max hours"); alert(a.alerts, f"stopped: max hours (runs {runs}, settled {settled})"); break
        day = now()[:10]
        if spent_day["day"] != day:
            spent_day = {"day": day, "flop": 0.0}
        if spent_day["flop"] + a.max_flop > a.max_flop_per_day:
            log(f"daily cap reached ({spent_day['flop']:.3f} FLOP escrowed today); sleeping 15 min")
            time.sleep(900 if not a.dry_run else 0)
            if a.dry_run:
                break
            continue

        bal = balance(a)
        if (bal is None or bal < a.min_balance) and time.time() - last_faucet > faucet_wait:
            log(f"balance {bal:.4f} FLOP < {a.min_balance}; asking the faucet")
            outcome = faucet(a)
            last_faucet = time.time()
            # pending/unknown: the grant may still land, so re-check the balance in 10 min instead
            # of re-asking (the faucet guide says never retry a refusal or an unknown outcome in a loop)
            faucet_wait = 600 if outcome == "pending" else a.faucet_cooldown
            time.sleep(30 if not a.dry_run else 0)
            bal = balance(a)
        if bal is not None and bal < a.max_flop * 1.5:
            log(f"balance {bal:.4f} FLOP too low for a {a.max_flop} escrow; waiting {a.interval * 5:.0f}s")
            if a.dry_run:
                break
            time.sleep(a.interval * 5)
            continue

        # reload the miners file each round so entries can be added while running
        fresh = load_miners()
        if fresh:
            for m in fresh:
                strikes.setdefault(m["address"], 0)
                benched_until.setdefault(m["address"], 0.0)
            miners = fresh
        # choose a miner that is not benched
        chosen = None
        for _ in range(len(miners)):
            m = miners[mi % len(miners)]
            mi += 1
            if benched_until[m["address"]] <= time.time():
                chosen = m
                break
        if chosen is None:
            log("all miners benched; sleeping 10 min")
            if a.dry_run:
                break
            time.sleep(600)
            continue
        prompt = prompts[pi % len(prompts)]
        pi += 1

        runs += 1
        code, j, raw, model = session(a, chosen, prompt)
        facts = summarize(j)
        ok = code == 0 and (not isinstance(j, dict) or not j.get("failure"))
        rec = {"ts": now(), "network": a.network, "miner": chosen["address"], "endpoint": chosen.get("endpoint"),
               "model": model, "max_flop": a.max_flop, "rc": code, "ok": ok, "facts": facts,
               "err": None if ok else raw[-400:]}
        with open(a.ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        spent_day["flop"] += a.max_flop
        if ok:
            settled += 1
            consecutive_fail = 0
            strikes[chosen["address"]] = 0
            log(f"session ok #{settled} miner={chosen['address'][:8]}… {json.dumps(facts)[:240]}")
            if not first_settle_alerted and not a.dry_run:
                alert(a.alerts, f"first settled session on {a.network}: miner {chosen['address'][:12]}… {json.dumps(facts)[:160]}")
                first_settle_alerted = True
        else:
            consecutive_fail += 1
            strikes[chosen["address"]] += 1
            log(f"session FAILED rc={code} miner={chosen['address'][:8]}… {raw[-300:].strip()}")
            if strikes[chosen["address"]] >= a.miner_strikes:
                benched_until[chosen["address"]] = time.time() + a.miner_cooldown
                strikes[chosen["address"]] = 0
                log(f"benching miner {chosen['address'][:8]}… for {a.miner_cooldown:.0f}s")
            if consecutive_fail >= a.max_consecutive_failures:
                alert(a.alerts, f"{consecutive_fail} consecutive session failures on {a.network}; sleeping {a.failure_sleep:.0f}s. last: {raw[-160:].strip()}")
                consecutive_fail = 0
                if not a.dry_run:
                    time.sleep(a.failure_sleep)
        if a.dry_run and runs >= 2:
            log("dry run done")
            break
        time.sleep(a.interval if not a.dry_run else 0)
    log(f"end runs={runs} settled={settled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
