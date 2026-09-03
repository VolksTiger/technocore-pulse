#!/usr/bin/env python3
"""Robust reader for Technocore rooms.

Two ways to read a room:

* `recent_messages()` — the newest window (max 200) via the paginated API,
  stepping the limit down (200/100/50) around intermittent 502s.
* `export_room()` — the FULL retained ring (~10 MiB per room, tens of thousands
  of messages) via `GET /r/<room>/export`, as the server stores it.

Stdlib only.
"""

from __future__ import annotations

import json
import time
import urllib.request
from urllib.parse import urlencode

BASE_URL = "https://technocore.chat"
USER_AGENT = "technocore-pulse/1.1"
DEFAULT_PAGE_LIMIT = 50
DEFAULT_TIMEOUT_SECONDS = 15


def fetch_page(
    room: str,
    *,
    since: int | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    wait: float | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = 3,
    backoff: float = 1.6,
) -> dict:
    """Fetch one room page as JSON, retrying transient failures with backoff."""
    query: dict[str, int | float | str] = {"format": "json", "limit": limit}
    if since is not None:
        query["since"] = since
    if wait is not None:
        query["wait"] = wait
    url = f"{BASE_URL}/r/{room}?{urlencode(query)}"
    delay = 1.0
    last_error: Exception | None = None
    for _ in range(retries):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as error:  # noqa: BLE001 - network boundary, retry then raise
            last_error = error
            time.sleep(delay)
            delay *= backoff
    raise RuntimeError(f"could not read /r/{room} after {retries} attempts: {last_error}")


def recent_messages(room: str, *, target: int = 200) -> list[dict]:
    """Return the newest messages (max 200), oldest first, degrading gracefully.

    Measured semantics (26.08.2026): `?since=X&limit=N` returns the NEWEST N
    messages after X — the tail, not the first N after the cursor, so this
    endpoint cannot page backwards. For history use `export_room()`, which
    returns the whole retained ring (correction 03.09.2026 — earlier versions
    of this file said deeper history was unreachable; it is, via /export).

    Big reads intermittently 502 on busy rooms, so this tries limit=200 and
    steps down (100, 50) until one succeeds.
    """
    for limit in (min(target, 200), 100, 50):
        try:
            page = fetch_page(room, limit=limit, retries=2)
        except RuntimeError:
            continue
        messages = [m for m in page.get("messages", []) if isinstance(m, dict)]
        if messages:
            return messages
    raise RuntimeError(f"could not read /r/{room} at any limit (200/100/50)")


def export_room(room: str, *, timeout: float = 90.0, retries: int = 3) -> list[dict]:
    """Return the room's full retained ring, oldest first, via GET /r/<room>/export.

    The server streams the ring as JSONL (one record per line: seq, ts, from,
    text, nonce, sig), byte-exact and snapshotted at open, so signed records can
    be re-verified from the dump alone. Measured 03.09.2026: /r/technocore = 8 MB,
    /r/meta = 22,861 records in one response. Unsigned/malformed lines are kept
    only if they parse as JSON objects.
    """
    url = f"{BASE_URL}/r/{room}/export"
    delay = 2.0
    last_error: Exception | None = None
    for _ in range(retries):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            records = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
            records.sort(key=lambda r: int(r.get("seq") or 0))
            return records
        except Exception as error:  # noqa: BLE001 - network boundary, retry then raise
            last_error = error
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"could not export /r/{room} after {retries} attempts: {last_error}")


def read_room(room: str, *, target: int = 200, full: bool = False) -> list[dict]:
    """Newest window (default) or the full ring (`full=True`), oldest first."""
    return export_room(room) if full else recent_messages(room, target=target)


def follow(room: str, *, since: int, wait: float = 10.0, page_limit: int = DEFAULT_PAGE_LIMIT):
    """Yield message batches via long-poll (server-friendly: one request per `wait`)."""
    cursor = since
    while True:
        page = fetch_page(room, since=cursor, limit=page_limit, wait=wait, timeout=wait + 10)
        page_messages = page.get("messages", [])
        if page_messages:
            top = max(int(m.get("seq") or cursor) for m in page_messages)
            if top <= cursor:
                raise RuntimeError(f"/r/{room} returned messages without advancing seq")
            cursor = top
            yield page_messages


def room_stats(messages: list[dict]) -> dict:
    """Aggregate a message window into room-analytics numbers."""
    senders: dict[str, int] = {}
    signed = 0
    timestamps: list[str] = []
    for message in messages:
        sender = str(message.get("from") or "?")
        senders[sender] = senders.get(sender, 0) + 1
        if sender.startswith("did:key:"):
            signed += 1
        ts = message.get("ts")
        if isinstance(ts, str):
            timestamps.append(ts)
    count = len(messages)
    span_hours = 0.0
    if len(timestamps) >= 2:
        from datetime import datetime

        first = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
        last = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
        span_hours = max((last - first).total_seconds() / 3600, 0.0)
    top_share = max(senders.values()) / count if count else 0.0
    return {
        "messages": count,
        "unique_senders": len(senders),
        "signed_share": signed / count if count else 0.0,
        "msgs_per_hour": count / span_hours if span_hours > 0 else float(count),
        "top_sender_share": top_share,
    }
