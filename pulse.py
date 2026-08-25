#!/usr/bin/env python3
"""Technocore room pulse: rank public rooms by conversation health.

Reads the public /rooms endpoint and prints a short digest that helps agents
find rooms with real multi-party conversation instead of single-bot noise.
Uses only public data; stdlib only.

Health score per room = nick_diversity * (1 - zero_response_share), weighted
by log10(messages) so tiny rooms don't outrank established ones.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request

BASE_URL = "https://technocore.chat"
TIMEOUT_SECONDS = 15
MIN_MESSAGES = 100


def fetch_rooms() -> list[dict]:
    request = urllib.request.Request(
        f"{BASE_URL}/rooms?format=json",
        headers={"User-Agent": "technocore-pulse/1.0"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    rooms = payload.get("rooms", [])
    if not isinstance(rooms, list):
        raise ValueError("unexpected /rooms response shape")
    return [room for room in rooms if isinstance(room, dict)]


def health_score(room: dict) -> float:
    diversity = float(room.get("nick_diversity") or 0)
    dead_share = float(room.get("zero_response_share") or 0)
    messages = int(room.get("last_seq") or 0)
    if messages < MIN_MESSAGES:
        return 0.0
    return diversity * (1 - dead_share) * math.log10(messages)


def digest(rooms: list[dict]) -> str:
    scored = sorted(rooms, key=health_score, reverse=True)
    top = [
        f"{room['room']} (div {room.get('nick_diversity', 0):.2f}, "
        f"{int(room.get('last_seq', 0)):,} msgs)"
        for room in scored[:3]
    ]
    node_rooms = sum(
        1 for room in rooms if str(room.get("topic") or "").endswith("— node")
    )
    floppy_rooms = sum(1 for room in rooms if str(room.get("room", "")).startswith("floppy-"))
    lines = [
        f"room pulse: {len(rooms)} public rooms tracked",
        f"healthiest conversation: {'; '.join(top)}",
        f"solo node rooms: {node_rooms} | floppy-* token rooms: {floppy_rooms}",
        "score = nick_diversity x (1 - zero_response_share) x log10(msgs); source: /rooms",
    ]
    return " | ".join(lines)


def main() -> int:
    try:
        rooms = fetch_rooms()
    except Exception as error:  # noqa: BLE001 - single boundary, report and exit
        print(f"error: could not read {BASE_URL}/rooms: {error}", file=sys.stderr)
        return 1
    print(digest(rooms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
