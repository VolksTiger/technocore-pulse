"""Unit tests for scripts/sonnet_funnel.py.

Synthetic message dicts only, no network -- exercises the pure analysis functions directly:
status-series parsing (incl. restart detection), submissions aggregation (accept/reject/reason
grouping, per-day count, seq-gap completeness), per-room derivation (max_version, complete,
unchained_words, window_truncated) and the room_outcome classifier.

Run with:  python3 -m unittest tests.test_sonnet_funnel   (from the repo root)
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import sonnet_funnel as sf  # noqa: E402

REF = sf.REF
DID_A = "did:key:z6Mk" + "a" * 44
DID_B = "did:key:z6Mk" + "b" * 44
DID_C = "did:key:z6Mk" + "c" * 44
DID_D = "did:key:z6Mk" + "d" * 44
ZERO_HASH = "0" * 64
REAL_HASH = "ab" * 32


def msg(seq, frm, obj, ts):
    return {"seq": seq, "from": frm, "ts": ts, "text": json.dumps(obj)}


class TestStatusSeries(unittest.TestCase):
    def test_series_team_rooms_and_restart(self):
        rows = [
            msg(1, REF, {"type": "sonnet.launch.v1", "deadline": sf.DEADLINE_EPOCH, "prize": 50000},
                "2026-09-11T14:41:00Z"),
            msg(2, REF, {
                "subject": "referee status", "teams": 10,
                "participants": {"organizer": 5, "writer": 20, "voter": 3},
                "intake": {"rooms": ["d-sonnet-2-team-a", "d-sonnet-2-team-b", "mb-sonnet-2-discovery"]},
                "uptime_seconds": 1000,
            }, "2026-09-11T15:00:00Z"),
            msg(3, REF, {
                "subject": "referee status", "teams": 12,
                "participants": {"organizer": 6, "writer": 22, "voter": 4},
                "intake": {"rooms": ["d-sonnet-2-team-a", "d-sonnet-2-team-c"]},
                "uptime_seconds": 1500,
            }, "2026-09-11T19:00:00Z"),
            msg(4, REF, {
                "subject": "referee status", "teams": 12,
                "participants": {"organizer": 6, "writer": 25, "voter": 5},
                "intake": {"rooms": ["d-sonnet-2-team-a"]},
                "uptime_seconds": 200,  # dropped versus the previous status -> restart
            }, "2026-09-11T23:00:00Z"),
        ]
        result = sf.parse_status_series(rows)
        self.assertEqual(result["launch"]["prize"], 50000)
        self.assertEqual(len(result["series"]), 3)
        self.assertEqual(
            result["team_rooms"], ["d-sonnet-2-team-a", "d-sonnet-2-team-b", "d-sonnet-2-team-c"]
        )
        self.assertEqual(result["restarts"], ["2026-09-11T23:00:00Z"])
        self.assertEqual(result["series"][-1]["teams"], 12)
        self.assertEqual(result["series"][-1]["writers"], 25)

    def test_no_restart_when_uptime_only_increases(self):
        rows = [
            msg(1, REF, {"subject": "referee status", "teams": 1,
                         "participants": {"organizer": 1, "writer": 1, "voter": 0},
                         "intake": {"rooms": []}, "uptime_seconds": 100}, "2026-09-11T15:00:00Z"),
            msg(2, REF, {"subject": "referee status", "teams": 1,
                         "participants": {"organizer": 1, "writer": 1, "voter": 0},
                         "intake": {"rooms": []}, "uptime_seconds": 200}, "2026-09-11T16:00:00Z"),
        ]
        result = sf.parse_status_series(rows)
        self.assertEqual(result["restarts"], [])


class TestAggregateSubmissions(unittest.TestCase):
    def test_accept_reject_and_completeness(self):
        rows = [
            msg(1, DID_A, {"type": "sonnet.submit.v1", "request_id": "r1", "game_id": "g1",
                           "poem_room": "d-sonnet-2-team-g1", "final_version": 130}, "2026-09-17T10:00:00Z"),
            msg(2, REF, {"type": "sonnet.receipt.v1", "sender_did": DID_A, "request_id": "r1",
                         "status": "accepted", "entry_id": "e1"}, "2026-09-17T10:05:00Z"),
            msg(3, DID_B, {"type": "sonnet.submit.v1", "request_id": "r2", "game_id": "g2",
                           "poem_room": "d-sonnet-2-team-g2"}, "2026-09-17T11:00:00Z"),
            msg(4, REF, {"type": "sonnet.receipt.v1", "sender_did": DID_B, "request_id": "r2",
                         "status": "rejected", "reason": "too_short"}, "2026-09-17T11:05:00Z"),
            msg(5, DID_C, {"type": "sonnet.submit.v1", "request_id": "r3", "game_id": "g3"},
                "2026-09-18T09:00:00Z"),
            msg(6, REF, {"type": "sonnet.receipt.v1", "sender_did": DID_C, "request_id": "r3",
                         "status": "rejected", "reason": "too_short"}, "2026-09-18T09:05:00Z"),
            # seq 7 intentionally missing -- gap for the completeness check
            msg(8, DID_D, {"type": "sonnet.submit.v1", "request_id": "r4", "game_id": "g4"},
                "2026-09-18T09:10:00Z"),
            msg(9, REF, {"type": "sonnet.receipt.v1", "sender_did": DID_D, "request_id": "r4",
                         "status": "rejected", "reason": "late"}, "2026-09-18T09:15:00Z"),
        ]
        result = sf.aggregate_submissions(rows)

        self.assertEqual(set(result["accepted"]), {"e1"})
        entry = result["accepted"]["e1"]
        self.assertEqual(entry["ts"], "2026-09-17T10:05:00Z")
        self.assertEqual(entry["final_version"], 130)
        self.assertEqual(entry["submitter_did"], DID_A)
        self.assertEqual(entry["game_id"], "g1")

        self.assertEqual(result["rejected_count"], 3)
        self.assertEqual(result["rejections_by_reason"], {"too_short": 2, "late": 1})
        self.assertEqual(result["distinct_submitting_dids"], {DID_A, DID_B, DID_C, DID_D})
        self.assertEqual(result["distinct_game_ids"], {"g1", "g2", "g3", "g4"})
        self.assertEqual(result["accepted_per_day"], {"2026-09-17": 1})

        comp = result["completeness"]
        self.assertEqual(comp["seq_min"], 1)
        self.assertEqual(comp["seq_max"], 9)
        self.assertEqual(comp["rows"], 8)
        self.assertEqual(comp["missing_seq"], 1)  # seq 7 is missing

    def test_first_accept_ts_wins_on_duplicate_entry_id(self):
        rows = [
            msg(1, DID_A, {"type": "sonnet.submit.v1", "request_id": "r1", "game_id": "g1"},
                "2026-09-17T10:00:00Z"),
            msg(2, REF, {"type": "sonnet.receipt.v1", "sender_did": DID_A, "request_id": "r1",
                         "status": "accepted", "entry_id": "e1"}, "2026-09-17T12:00:00Z"),
            msg(3, REF, {"type": "sonnet.receipt.v1", "sender_did": DID_A, "request_id": "r1",
                         "status": "accepted", "entry_id": "e1"}, "2026-09-17T10:30:00Z"),
        ]
        result = sf.aggregate_submissions(rows)
        self.assertEqual(result["accepted"]["e1"]["ts"], "2026-09-17T10:30:00Z")


class TestDeriveRoomStats(unittest.TestCase):
    def test_progress_completion_and_flooding(self):
        rows = [
            msg(5, DID_A, {"type": "sonnet.word.v1", "word": "the", "previous_state_hash": REAL_HASH},
                "2026-09-15T10:00:00Z"),
            msg(6, DID_B, {"type": "sonnet.word.v1", "word": "spam", "previous_state_hash": ZERO_HASH},
                "2026-09-15T10:01:00Z"),
            msg(7, REF, {"type": "sonnet.receipt.v1", "status": "accepted", "version": 5},
                "2026-09-15T10:02:00Z"),
            msg(8, DID_A, {"type": "sonnet.word.v1", "word": "moon", "previous_state_hash": REAL_HASH},
                "2026-09-15T10:03:00Z"),
            msg(9, REF, {"type": "sonnet.receipt.v1", "status": "accepted", "version": 6},
                "2026-09-15T10:04:00Z"),
            msg(10, REF, {"type": "sonnet.receipt.v1", "status": "accepted", "version": 130,
                          "complete": True}, "2026-09-15T10:05:00Z"),
        ]
        stats = sf.derive_room_stats(rows)
        self.assertEqual(stats["retained"], 6)
        self.assertEqual(stats["seq_min"], 5)
        self.assertEqual(stats["seq_max"], 10)
        self.assertEqual(stats["max_version"], 130)
        self.assertTrue(stats["complete"])
        self.assertEqual(stats["last_accept_ts"], "2026-09-15T10:05:00Z")
        self.assertEqual(stats["distinct_word_posters"], 2)
        self.assertEqual(stats["unchained_words"], 1)
        # retained (6) is well under the 200-message read cap, so nothing was cut even though
        # seq_min (5) > 1 -- window_truncated is about hitting the cap, not about seq_min.
        self.assertFalse(stats["window_truncated"])

    def test_window_not_truncated_when_seq_starts_above_one_but_under_cap(self):
        # Evidence from the real snapshot: rooms whose setup record lands at seq 2 (not 1) but
        # whose read stays under the 200-message cap are contiguous -- nothing was cut, so
        # window_truncated must be False despite seq_min > 1.
        rows = [
            msg(2, REF, {"type": "sonnet.room.v1"}, "2026-09-11T15:00:00Z"),
            msg(3, DID_A, {"type": "sonnet.word.v1", "previous_state_hash": REAL_HASH}, "2026-09-11T15:01:00Z"),
        ]
        stats = sf.derive_room_stats(rows)
        self.assertEqual(stats["seq_min"], 2)
        self.assertFalse(stats["window_truncated"])

    def test_window_truncated_when_read_hits_the_200_cap(self):
        rows = [msg(i, DID_A, {"type": "sonnet.word.v1", "previous_state_hash": REAL_HASH}, "2026-09-11T15:00:00Z")
                for i in range(1, 201)]
        stats = sf.derive_room_stats(rows)
        self.assertEqual(stats["retained"], 200)
        self.assertTrue(stats["window_truncated"])

    def test_no_accepted_receipt_yields_zero_version(self):
        rows = [msg(1, DID_A, {"type": "sonnet.word.v1", "previous_state_hash": REAL_HASH}, "2026-09-15T10:00:00Z")]
        stats = sf.derive_room_stats(rows)
        self.assertEqual(stats["max_version"], 0)
        self.assertEqual(stats["retained"], 1)


class TestRoomOutcome(unittest.TestCase):
    def test_submitted_wins_even_if_unreadable(self):
        self.assertEqual(sf.room_outcome(retained=0, complete=False, max_version=0, submitted=True), "submitted")

    def test_complete_not_submitted(self):
        self.assertEqual(
            sf.room_outcome(retained=200, complete=True, max_version=130, submitted=False), "complete_not_submitted"
        )

    def test_unreadable(self):
        self.assertEqual(sf.room_outcome(retained=0, complete=False, max_version=0, submitted=False), "unreadable")

    def test_stalled(self):
        self.assertEqual(sf.room_outcome(retained=50, complete=False, max_version=12, submitted=False), "stalled")

    def test_never_started(self):
        self.assertEqual(sf.room_outcome(retained=1, complete=False, max_version=0, submitted=False), "never_started")


if __name__ == "__main__":
    unittest.main()
