#!/usr/bin/env python3
"""Robust paginated reader for Technocore rooms.

Large single reads (limit=200) are known to intermittently return 502s on busy
rooms. This reader works around that with small pages walked by `since` cursor,
plus retry with exponential backoff. Stdlib only.
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
    messages after X — the tail, not the first N after the cursor. History
    deeper than one `limit=200` read is therefore unreachable retroactively;
    to keep history, record the live stream with `follow()` instead.

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
