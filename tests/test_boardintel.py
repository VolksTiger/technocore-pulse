"""Unit tests for boardintel.py.

Uses small synthetic exports (built with tclk.py's own offer_id/contract_id -- pure hashes,
no signing needed) so the first-bidder join, per-poster newcomer logic and the family
classifier are each exercised without any network access.

Run with:  python3 -m unittest tests.test_boardintel   (from the repo root)
"""

import unittest

import boardintel
import tclk

DID_A = "did:key:z6Mk" + "a" * 44  # a poster/payer
DID_B = "did:key:z6Mk" + "b" * 44  # an acceptor/payee
DID_C = "did:key:z6Mk" + "c" * 44  # a second acceptor/payee
DID_D = "did:key:z6Mk" + "d" * 44  # a second poster/payer


def make_offer(from_did, nonce, amount="100", job=None, seq=1, ts_ms=0):
    fields = {
        "type": "offer", "from": from_did, "role": "payer", "amount": amount, "asset": "FLOP",
        "lock": "hash", "rails": ["paper"], "claimByMs": 20_000, "refundAfterMs": 30_000,
        "expiresMs": 10_000, "nonce": nonce,
    }
    if job is not None:
        fields["job"] = job
    frame = dict(fields, id=tclk.offer_id(fields))
    rec = {"room": tclk.OFFER_ROOM, "seq": seq, "ts_ms": ts_ms, "sender": from_did,
           "nonce": "1", "sig": None, "line": ""}
    return rec, frame


def make_accept(offer_frame, from_did, nonce, seq, ts_ms, statement=None):
    core = {"from": from_did, "ref": offer_frame["id"],
            "statement": statement or ("0x" + "ab" * 32), "nonce": nonce}
    frame = dict({"type": "accept"}, **core, contract=tclk.contract_id(offer_frame, core))
    rec = {"room": tclk.OFFER_ROOM, "seq": seq, "ts_ms": ts_ms, "sender": from_did,
           "nonce": "1", "sig": None, "line": ""}
    return rec, frame


def make_board(offers, accepts, reveals=(), refunds=()):
    """Assemble the dict shape load_board_frames() returns, from hand-built (record, frame)
    pairs -- skips the JSONL/signature layer so tests exercise the join/aggregation logic
    directly and deterministically."""
    offers_by_id = {f["id"]: (r, f) for r, f in offers}
    accepts_by_contract, accepts_by_offer = {}, {}
    for r, f in accepts:
        accepts_by_contract[f["contract"]] = (r, f)
        accepts_by_offer.setdefault(f["ref"], []).append((r, f))
    for oid in accepts_by_offer:
        accepts_by_offer[oid].sort(key=lambda rf: (rf[0]["ts_ms"], rf[0]["seq"]))
    return {
        "records": [], "offers": offers_by_id, "accepts_by_contract": accepts_by_contract,
        "accepts_by_offer": accepts_by_offer, "reveals": list(reveals), "refunds": list(refunds),
        "accept_no_offer": 0, "accept_bad_contract": 0, "accept_self": 0,
    }


def make_verdict(offer_frame, accept_frame, outcome="PASS", score=1.0, reason="ok",
                  poster=None, seq=1, ts_ms=0):
    return {
        "seq": seq, "ts_ms": ts_ms, "poster": poster or offer_frame["from"],
        "offer_prefix": offer_frame["id"][:18], "contract_prefix": accept_frame["contract"][:18],
        "payee_suffix": accept_frame["from"][-8:], "outcome": outcome, "score": score,
        "reason": reason,
    }


class TestParseVerdicts(unittest.TestCase):
    def _rec(self, text, seq=1, ts_ms=0, sender=DID_A):
        return {"seq": seq, "ts_ms": ts_ms, "sender": sender, "text": text}

    def test_integer_score(self):
        text = ("review 0x6cb5a8217fa32cd4 contract 0xdadba0d48527b732 payee 8jbqLKqt "
                 "PASS 1 — exact match against the reference answer")
        out = boardintel.parse_verdicts([self._rec(text)])
        self.assertEqual(len(out), 1)
        v = out[0]
        self.assertEqual(v["offer_prefix"], "0x6cb5a8217fa32cd4")
        self.assertEqual(v["contract_prefix"], "0xdadba0d48527b732")
        self.assertEqual(v["payee_suffix"], "8jbqLKqt")
        self.assertEqual(v["outcome"], "PASS")
        self.assertEqual(v["score"], 1.0)

    def test_decimal_score(self):
        # regression: an earlier `\d+` regex mis-split "0.5" as score "0" + separator ".",
        # eating the leading digit of the reason. Must parse as one decimal score.
        text = ("review 0xabd4bed00b116b02 contract 0x296e2329f6fd7b32 payee BGB82uPD "
                 "FAIL 0.5 — Correct distinct asset count but incorrect total amount.")
        v = boardintel.parse_verdicts([self._rec(text)])[0]
        self.assertEqual(v["score"], 0.5)
        self.assertEqual(v["outcome"], "FAIL")
        self.assertTrue(v["reason"].startswith("Correct distinct"))

    def test_ignores_non_verdict_lines(self):
        recs = [self._rec("568346, 455815, 930711"), self._rec("no deliverable posted")]
        self.assertEqual(boardintel.parse_verdicts(recs), [])

    def test_poster_is_record_sender(self):
        text = "review 0x6cb5a8217fa32cd4 contract 0xdadba0d48527b732 payee 8jbqLKqt PASS 1 — ok"
        v = boardintel.parse_verdicts([self._rec(text, sender=DID_D)])[0]
        self.assertEqual(v["poster"], DID_D)


class TestJoinVerdict(unittest.TestCase):
    def setUp(self):
        self.orec, self.offer = make_offer(DID_A, "n" * 16, seq=1, ts_ms=1000)
        self.a1r, self.a1 = make_accept(self.offer, DID_B, "m" * 16, seq=2, ts_ms=1500)
        self.a2r, self.a2 = make_accept(self.offer, DID_C, "p" * 16, seq=3, ts_ms=5000)
        self.board = make_board([( self.orec, self.offer)], [(self.a1r, self.a1), (self.a2r, self.a2)])
        self.off_idx = boardintel.build_prefix_index(self.board["offers"].keys())
        self.con_idx = boardintel.build_prefix_index(self.board["accepts_by_contract"].keys())

    def test_contract_prefix_primary_match(self):
        v = make_verdict(self.offer, self.a2)
        hit = boardintel.join_verdict(v, self.off_idx, self.con_idx, self.board["offers"],
                                       self.board["accepts_by_contract"], self.board["accepts_by_offer"])
        self.assertIsNotNone(hit)
        oid, (r, f) = hit
        self.assertEqual(oid, self.offer["id"])
        self.assertEqual(f["from"], DID_C)

    def test_offer_prefix_fallback_when_contract_missing(self):
        v = make_verdict(self.offer, self.a2)
        hit = boardintel.join_verdict(v, self.off_idx, con_idx={}, offers=self.board["offers"],
                                       accepts_by_contract={}, accepts_by_offer=self.board["accepts_by_offer"])
        self.assertIsNotNone(hit)
        oid, (r, f) = hit
        self.assertEqual(oid, self.offer["id"])
        self.assertEqual(f["from"], DID_C)

    def test_payee_suffix_mismatch_returns_none(self):
        v = make_verdict(self.offer, self.a2)
        v["payee_suffix"] = "ZZZZZZZZ"  # doesn't match any accept.from[-8:]
        hit = boardintel.join_verdict(v, self.off_idx, self.con_idx, self.board["offers"],
                                       self.board["accepts_by_contract"], self.board["accepts_by_offer"])
        self.assertIsNone(hit)

    def test_ambiguous_contract_prefix_refuses_to_guess(self):
        v = make_verdict(self.offer, self.a1)
        ambiguous_con_idx = {v["contract_prefix"]: ["0x" + "1" * 64, "0x" + "2" * 64]}
        hit = boardintel.join_verdict(v, off_idx={}, con_idx=ambiguous_con_idx, offers=self.board["offers"],
                                       accepts_by_contract=self.board["accepts_by_contract"],
                                       accepts_by_offer=self.board["accepts_by_offer"])
        self.assertIsNone(hit)


class TestFirstBidderMetrics(unittest.TestCase):
    def test_first_accept_wins(self):
        orec, offer = make_offer(DID_A, "n" * 16, seq=1, ts_ms=0)
        a1r, a1 = make_accept(offer, DID_B, "m" * 16, seq=2, ts_ms=1400)   # first, latency 1.4s
        a2r, a2 = make_accept(offer, DID_C, "p" * 16, seq=3, ts_ms=5000)   # later
        board = make_board([(orec, offer)], [(a1r, a1), (a2r, a2)])
        verdicts = [make_verdict(offer, a1)]  # winner is the first accept
        out = boardintel.first_bidder_metrics(board, verdicts)
        self.assertEqual(out["verdicted_offers"]["n"], 1)
        self.assertEqual(out["verdicted_offers"]["share_won_by_first_accept"], 1.0)
        self.assertAlmostEqual(out["verdicted_offers"]["winning_accept_latency_s"]["median"], 1.4)

    def test_later_accept_wins(self):
        orec, offer = make_offer(DID_A, "n" * 16, seq=1, ts_ms=0)
        a1r, a1 = make_accept(offer, DID_B, "m" * 16, seq=2, ts_ms=1400)
        a2r, a2 = make_accept(offer, DID_C, "p" * 16, seq=3, ts_ms=5000)
        board = make_board([(orec, offer)], [(a1r, a1), (a2r, a2)])
        verdicts = [make_verdict(offer, a2)]  # winner is the LATER accept
        out = boardintel.first_bidder_metrics(board, verdicts)
        self.assertEqual(out["verdicted_offers"]["share_won_by_first_accept"], 0.0)
        self.assertEqual(out["verdicted_offers"]["share_won_by_later_accept"], 1.0)

    def test_all_offers_latency_and_2s_share(self):
        o1r, o1 = make_offer(DID_A, "1" * 16, seq=1, ts_ms=0)
        a1r, a1 = make_accept(o1, DID_B, "m" * 16, seq=2, ts_ms=1500)  # 1.5s: within 2s
        o2r, o2 = make_offer(DID_D, "2" * 16, seq=3, ts_ms=0)
        a2r, a2 = make_accept(o2, DID_C, "p" * 16, seq=4, ts_ms=6000)  # 6.0s: not within 2s
        board = make_board([(o1r, o1), (o2r, o2)], [(a1r, a1), (a2r, a2)])
        out = boardintel.first_bidder_metrics(board, [])
        self.assertEqual(out["all_offers_with_accept"]["n"], 2)
        self.assertAlmostEqual(out["all_offers_with_accept"]["median_first_accept_latency_s"], 3.75)
        self.assertAlmostEqual(out["all_offers_with_accept"]["share_first_accept_within_2s"], 0.5)

    def test_no_verdicts_no_accepts_is_empty_not_crashing(self):
        out = boardintel.first_bidder_metrics(make_board([], []), [])
        self.assertEqual(out["all_offers_with_accept"]["n"], 0)
        self.assertIsNone(out["all_offers_with_accept"]["median_first_accept_latency_s"])
        self.assertEqual(out["verdicted_offers"]["n"], 0)


class TestPerPosterMetrics(unittest.TestCase):
    def test_newcomer_credited_to_earliest_verdict_poster(self):
        # payee B's first-ever verdict in the window is from poster A (ts 100); poster D judges
        # the same payee later (ts 200) and must NOT also get newcomer credit for B.
        o1r, o1 = make_offer(DID_A, "1" * 16, seq=1, ts_ms=0)
        a1r, a1 = make_accept(o1, DID_B, "m" * 16, seq=2, ts_ms=100)
        board = make_board([(o1r, o1)], [(a1r, a1)])
        v1 = make_verdict(o1, a1, poster=DID_A, seq=1, ts_ms=100)
        v2 = make_verdict(o1, a1, poster=DID_D, seq=2, ts_ms=200)
        fb = boardintel.first_bidder_metrics(board, [v1, v2])
        pp = boardintel.per_poster_metrics(board, [v1, v2], fb, top_n=20)
        rows = {r["did"]: r for r in pp["top_by_verdicts"]}
        self.assertEqual(rows[DID_A]["newcomer_payees_brought_in"], 1)
        self.assertEqual(rows[DID_D]["newcomer_payees_brought_in"], 0)
        self.assertTrue(rows[DID_A]["ever_judged_a_newcomer"])
        self.assertFalse(rows[DID_D]["ever_judged_a_newcomer"])

    def test_never_judge_threshold(self):
        # DID_A: 10 offers-with-accept, 0 verdicts -> flagged. DID_D: 9 -> not flagged.
        offers, accepts = [], []
        for i in range(10):
            orec, offer = make_offer(DID_A, f"{i:016x}", seq=i * 2, ts_ms=0)
            arec, accept = make_accept(offer, DID_B, f"a{i:015x}", seq=i * 2 + 1, ts_ms=100)
            offers.append((orec, offer)); accepts.append((arec, accept))
        for i in range(9):
            orec, offer = make_offer(DID_D, f"d{i:015x}", seq=100 + i * 2, ts_ms=0)
            arec, accept = make_accept(offer, DID_C, f"b{i:015x}", seq=100 + i * 2 + 1, ts_ms=100)
            offers.append((orec, offer)); accepts.append((arec, accept))
        board = make_board(offers, accepts)
        fb = boardintel.first_bidder_metrics(board, [])
        pp = boardintel.per_poster_metrics(board, [], fb, top_n=20)
        never_judge_dids = {r["did"] for r in pp["posters_that_never_judge"]}
        self.assertIn(DID_A, never_judge_dids)
        self.assertNotIn(DID_D, never_judge_dids)

    def test_judge_rate_ratio(self):
        orec, offer = make_offer(DID_A, "1" * 16, seq=1, ts_ms=0)
        arec, accept = make_accept(offer, DID_B, "m" * 16, seq=2, ts_ms=100)
        board = make_board([(orec, offer)], [(arec, accept)])
        v1 = make_verdict(offer, accept, poster=DID_A, outcome="PASS", seq=1, ts_ms=100)
        v2 = make_verdict(offer, accept, poster=DID_A, outcome="FAIL", seq=2, ts_ms=200)
        fb = boardintel.first_bidder_metrics(board, [v1, v2])
        pp = boardintel.per_poster_metrics(board, [v1, v2], fb, top_n=20)
        row = next(r for r in pp["top_by_verdicts"] if r["did"] == DID_A)
        self.assertEqual(row["verdicts_pass"], 1)
        self.assertEqual(row["verdicts_fail"], 1)
        self.assertEqual(row["offers_with_accept"], 1)
        self.assertEqual(row["judge_rate_verdicts_per_accepted_offer"], 2.0)


class TestClassifyFamily(unittest.TestCase):
    def test_known_prefixes(self):
        cases = {
            "census-1234": "census", "math-1234": "math", "val-1234": "val",
            "task-1234": "task", "inf-1234": "inf", "probe-1234": "probe", "attest-1234": "attest",
        }
        for jid, fam in cases.items():
            self.assertEqual(boardintel.classify_family({"proto": "a2a", "id": jid}), fam)

    def test_realistic_ids_with_suffixes(self):
        # actual shapes observed on the live board: prefix, then a hex tag, then "-open"
        self.assertEqual(boardintel.classify_family({"proto": "a2a", "id": "probe-df1cb050-open"}), "probe")
        self.assertEqual(boardintel.classify_family({"proto": "a2a", "id": "task-fca7bc1f-open"}), "task")
        self.assertEqual(boardintel.classify_family({"proto": "blockrewards", "id": "val-5fe5450a"}), "val")

    def test_unmatched_prefix_is_other(self):
        self.assertEqual(boardintel.classify_family({"proto": "kibble", "id": "kibble-42"}), "other")

    def test_missing_job_is_no_job(self):
        self.assertEqual(boardintel.classify_family(None), "no-job")
        self.assertEqual(boardintel.classify_family({}), "no-job")


class TestRevealRefHazard(unittest.TestCase):
    def test_hazard_and_share(self):
        cid1, cid2 = "0x" + "1" * 64, "0x" + "2" * 64
        reveals = [
            ({}, {"type": "reveal", "contract": cid1, "secret": "0x" + "a" * 64, "ref": "x"}),  # has ref
            ({}, {"type": "reveal", "contract": cid2, "secret": "0x" + "b" * 64}),               # no ref
        ]
        refunds = [
            ({}, {"type": "refund", "contract": cid1, "reason": "no reveal before deadline"}),  # false claim
            ({}, {"type": "refund", "contract": "0x" + "3" * 64, "reason": "no reveal, timed out"}),  # no reveal on board for this contract -> not counted
        ]
        board = {"reveals": reveals, "refunds": refunds}
        out = boardintel.reveal_ref_hazard(board)
        self.assertEqual(out["reveal_frames_on_board"], 2)
        self.assertEqual(out["reveal_with_ref"], 1)
        self.assertAlmostEqual(out["share_reveal_with_ref"], 0.5)
        self.assertEqual(out["refunds_claiming_no_reveal_despite_board_reveal"], 1)

    def test_no_reveals_is_none_share(self):
        out = boardintel.reveal_ref_hazard({"reveals": [], "refunds": []})
        self.assertIsNone(out["share_reveal_with_ref"])
        self.assertEqual(out["refunds_claiming_no_reveal_despite_board_reveal"], 0)


class TestPercentileStats(unittest.TestCase):
    def test_empty(self):
        out = boardintel.percentile_stats([])
        self.assertEqual(out, {"median": None, "p25": None, "p75": None, "n": 0})

    def test_single_value(self):
        out = boardintel.percentile_stats([3.0])
        self.assertEqual(out, {"median": 3.0, "p25": 3.0, "p75": 3.0, "n": 1})

    def test_ordering_holds(self):
        out = boardintel.percentile_stats([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertLessEqual(out["p25"], out["median"])
        self.assertLessEqual(out["median"], out["p75"])
        self.assertEqual(out["n"], 5)


if __name__ == "__main__":
    unittest.main()
