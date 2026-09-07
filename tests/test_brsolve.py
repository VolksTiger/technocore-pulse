"""Unit tests for brsolve.py.

Run with:  python3 -m unittest tests.test_brsolve   (from the repo root)
"""

import json
import os
import unittest

import brsolve

FIXTURES_PATH = (
    "/private/tmp/claude-501/-Users-tomababic/ea6dfb4d-4b92-4149-97c8-fe10299a9051"
    "/scratchpad/harness/open.json"
)


def make_material(header, rows):
    """Build a 'header then rows, rows glued by a single space' material blob,
    exactly matching the format documented on parse_table()."""
    all_rows = [header] + rows
    return " ".join(" | ".join(str(c) for c in r) for r in all_rows)


def load_fixture(offer_id):
    with open(FIXTURES_PATH) as f:
        data = json.load(f)
    for o in data["offers"]:
        if o["id"] == offer_id:
            return o
    raise KeyError(offer_id)


HAVE_FIXTURES = os.path.exists(FIXTURES_PATH)


# ═══════════════════════════════════════════════════════════════════════
# classify / split_material / parse_table
# ═══════════════════════════════════════════════════════════════════════

class TestClassify(unittest.TestCase):
    def test_families(self):
        for fam in ("census", "validation", "review", "extraction", "attest",
                    "verification", "protocol", "inference", "math"):
            spec = f"{fam} | some question text | done looks like: one line"
            self.assertEqual(brsolve.classify(spec), fam)

    def test_case_and_whitespace(self):
        self.assertEqual(brsolve.classify("  Math | 2+2? "), "math")


class TestSplitMaterial(unittest.TestCase):
    def test_no_material(self):
        spec = "math | How many? | done looks like: one line"
        pre, mat = brsolve.split_material(spec)
        self.assertEqual(pre, spec)
        self.assertIsNone(mat)

    def test_with_material(self):
        spec = "extraction | question | done looks like: x | MATERIAL: seq | id 1 | a"
        pre, mat = brsolve.split_material(spec)
        self.assertEqual(pre, "extraction | question | done looks like: x")
        self.assertEqual(mat, "seq | id 1 | a")


class TestParseTable(unittest.TestCase):
    def test_basic(self):
        mat = make_material(["seq", "id", "amount"], [[1, "a1", 100], [2, "a2", 200]])
        table = brsolve.parse_table(mat)
        self.assertEqual(table[0], ["seq", "id", "amount"])
        self.assertEqual(table[1], ["1", "a1", "100"])
        self.assertEqual(table[2], ["2", "a2", "200"])

    def test_column_count_consistent(self):
        mat = make_material(
            ["seq", "payer", "amount", "asset", "proto", "time"],
            [[7, "PA", 100, "FLOP", "a2a", "01:00:00"],
             [4, "PB", 200, "FLOP", "a2a", "02:00:00"]],
        )
        table = brsolve.parse_table(mat)
        self.assertTrue(all(len(row) == len(table[0]) for row in table))

    def test_none_material(self):
        self.assertEqual(brsolve.parse_table(None), [])

    def test_empty_material(self):
        self.assertEqual(brsolve.parse_table(""), [])


# ═══════════════════════════════════════════════════════════════════════
# attest
# ═══════════════════════════════════════════════════════════════════════

class TestAttest(unittest.TestCase):
    def test_attest_sentinel(self):
        spec = ("attest | Post exactly one signed line ... | done looks like: "
                "one line: attested seq <seq>.")
        self.assertEqual(brsolve.solve(spec), "__ATTEST__")


# ═══════════════════════════════════════════════════════════════════════
# math
# ═══════════════════════════════════════════════════════════════════════

class TestMath(unittest.TestCase):
    def test_digit_sum(self):
        spec = "math | How many integers n with 35459 ≤ n ≤ 41260 have digit sum exactly 10? | done looks like: one line: the count."
        self.assertEqual(brsolve.solve(spec), "47")

    def test_digit_sum_small_hand_checked(self):
        # n in [1,20] with digit sum exactly 2: 2, 11, 20 -> 3
        spec = "math | How many integers n with 1 ≤ n ≤ 20 have digit sum exactly 2? | done looks like: one line: the count."
        self.assertEqual(brsolve.solve(spec), "3")

    def test_digit_sum_range_too_large_is_none(self):
        spec = "math | How many integers n with 1 ≤ n ≤ 5000000 have digit sum exactly 10? | done looks like: one line: the count."
        self.assertIsNone(brsolve.solve(spec))

    def test_divisible_by_k(self):
        # multiples of 7 in [10, 100]: 14..98 -> 13
        spec = "math | How many integers n with 10 ≤ n ≤ 100 are divisible by 7? | done looks like: one line: the count."
        self.assertEqual(brsolve.solve(spec), "13")

    def test_prime_count(self):
        # primes in [10,30]: 11,13,17,19,23,29 -> 6
        spec = "math | How many integers n with 10 ≤ n ≤ 30 are prime? | done looks like: one line: the count."
        self.assertEqual(brsolve.solve(spec), "6")

    def test_palindrome_count(self):
        # palindromes in [100,150]: 101,111,121,131,141 -> 5
        spec = "math | How many integers n with 100 ≤ n ≤ 150 are palindromes? | done looks like: one line: the count."
        self.assertEqual(brsolve.solve(spec), "5")

    def test_sum_of_range(self):
        spec = "math | What is the sum of all integers n with 1 ≤ n ≤ 10? | done looks like: one line: the sum."
        self.assertEqual(brsolve.solve(spec), "55")

    def test_smallest_prime_gt(self):
        spec = "math | What is the smallest prime strictly greater than 1643935848? | done looks like: one line: the prime."
        self.assertEqual(brsolve.solve(spec), "1643935889")

    def test_graph_shortest_path(self):
        spec = ("math | Undirected weighted graph on nodes 0..5, edges (a-b:w): "
                 "0-1:13, 0-2:4, 0-3:2, 0-4:3, 4-5:13, 1-4:20, 5-1:5, 2-5:20, 3-4:20. "
                 "What is the length of the shortest path from node 0 to node 5? | done looks like: one line: the length.")
        self.assertEqual(brsolve.solve(spec), "16")

    def test_unrecognized_template_is_none(self):
        spec = "math | What is the 10th Fibonacci number? | done looks like: one line: the number."
        self.assertIsNone(brsolve.solve(spec))


# ═══════════════════════════════════════════════════════════════════════
# census
# ═══════════════════════════════════════════════════════════════════════

class TestCensus(unittest.TestCase):
    def test_census_basic(self):
        mat = make_material(
            ["seq", "id", "payer", "amount", "asset", "rails", "proto", "role"],
            [[1, "aaa1", "P1", 100, "FLOP", "paper", "a2a", "payer"],
             [2, "aaa2", "P1", 200, "FLOP", "paper", "a2a", "payer"],
             [3, "aaa3", "P2", 100, "FLOP", "paper,x402", "blockrewards", "payer"],
             [4, "aaa4", "P2", 300, "PAPER", "paper", "-", "payer"],
             [5, "aaa5", "P3", 400, "FLOP", "flop-htlc", "a2a", "payer"]],
        )
        spec = ("census | Census over the excerpt: count offers per proto value "
                '("-" for none) and report the most common proto with its count, '
                'and how many offers list exactly the single rail "paper". '
                '| done looks like: one line exactly: proto=<value>:<count>; paper_only=<n> '
                f"| MATERIAL: {mat}")
        # proto counts: a2a=3, blockrewards=1, -=1 -> most common a2a:3
        # rails == exactly "paper": rows 1,2,4 -> 3
        self.assertEqual(brsolve.solve(spec), "proto=a2a:3; paper_only=3")

    def test_census_no_material_is_none(self):
        spec = ("census | Census over the excerpt: count offers per proto value "
                '("-" for none) ... | done looks like: one line exactly: proto=<value>:<count>; paper_only=<n>')
        self.assertIsNone(brsolve.solve(spec))


# ═══════════════════════════════════════════════════════════════════════
# extraction
# ═══════════════════════════════════════════════════════════════════════

class TestExtraction(unittest.TestCase):
    def test_distinct_payers(self):
        mat = make_material(
            ["seq", "id", "payer", "amount", "asset", "rails", "proto", "role"],
            [[10, "id1", "PAY_B", 100, "FLOP", "paper", "a2a", "payer"],
             [11, "id2", "PAY_A", 200, "FLOP", "paper", "a2a", "payer"],
             [12, "id3", "PAY_B", 300, "FLOP", "paper", "a2a", "payer"]],
        )
        spec = ("extraction | From the note the table at the end of this note (a table of "
                "tclk board offers, one per line: seq | id | payer | amount | asset | rails | "
                "proto | role): how many distinct payer values appear? Give the count, then "
                "list them once each in order of first appearance. | done looks like: one line: "
                "the count, then the distinct payers in order of first appearance. "
                f"| MATERIAL: {mat}")
        self.assertEqual(brsolve.solve(spec), "2: PAY_B, PAY_A")

    def test_ids_below_amount(self):
        mat = make_material(
            ["seq", "id", "payer", "amount", "asset", "rails", "proto", "role"],
            [[20, "idA", "PX", 50, "FLOP", "paper", "a2a", "payer"],
             [21, "idB", "PX", 500, "FLOP", "paper", "a2a", "payer"],
             [22, "idC", "PX", 10, "FLOP", "paper", "a2a", "payer"]],
        )
        spec = ("extraction | From the note the table at the end of this note (a table of "
                "tclk board offers, one per line: seq | id | payer | amount | asset | rails | "
                "proto | role): list the ids of the rows whose amount is below 100, in seq order. "
                "| done looks like: one line: comma-separated ids in seq order, or 'none'. "
                f"| MATERIAL: {mat}")
        self.assertEqual(brsolve.solve(spec), "idA, idC")

    def test_ids_below_amount_none(self):
        mat = make_material(
            ["seq", "id", "payer", "amount", "asset", "rails", "proto", "role"],
            [[20, "idA", "PX", 500, "FLOP", "paper", "a2a", "payer"]],
        )
        spec = ("extraction | From the note the table at the end of this note (a table of "
                "tclk board offers, one per line: seq | id | payer | amount | asset | rails | "
                "proto | role): list the ids of the rows whose amount is below 100, in seq order. "
                "| done looks like: one line: comma-separated ids in seq order, or 'none'. "
                f"| MATERIAL: {mat}")
        self.assertEqual(brsolve.solve(spec), "none")

    def test_unpinned_format_is_none(self):
        mat = make_material(
            ["seq", "id", "payer", "amount", "asset", "rails", "proto", "role"],
            [[1, "id1", "P1", 500000, "FLOP", "paper", "a2a", "payer"],
             [2, "id2", "P2", 500000, "FLOP", "paper", "a2a", "payer"]],
        )
        spec = ("extraction | From the note the table at the end of this note (a table of "
                "tclk board offers, one per line: seq | id | payer | amount | asset | rails | "
                "proto | role): how many rows tie for the largest amount in asset FLOP, and "
                "what is that amount? | done looks like: one line: the count of tied rows and "
                f"the amount. | MATERIAL: {mat}")
        self.assertIsNone(brsolve.solve(spec))

    def test_url_based_extraction_is_none(self):
        spec = ("extraction | From https://technocore.chat/.well-known/agent.json: What is "
                "the display_name? | done looks like: one line: the exact value or phrase from "
                "the cited document (quote it), nothing else")
        self.assertIsNone(brsolve.solve(spec))


# ═══════════════════════════════════════════════════════════════════════
# verification
# ═══════════════════════════════════════════════════════════════════════

class TestVerification(unittest.TestCase):
    DID1 = "did:key:z6MkTestDidAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    DID2 = "did:key:z6MkOtherDidBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"

    def _material(self):
        return make_material(
            ["seq", "time", "type", "from", "ref"],
            [[100, "00:01", "offer", self.DID1, "0xaaa1"],
             [101, "00:02", "lock", self.DID1, "0xaaa2"],
             [102, "00:03", "offer", self.DID2, "0xaaa3"],
             [103, "00:04", "lock", self.DID1, "0xaaa4"],
             [104, "00:05", "accept", self.DID2, "0xaaa5"]],
        )

    def test_offers_and_locks(self):
        spec = (f"verification | From the note the table at the end of this note (an excerpt "
                f"of the tclk board, one frame per line: seq | time | type | from | ref): how "
                f"many rows are offer frames posted by {self.DID1}, and how many are lock frames "
                f"by the same sender? Give both counts as \"offers N, locks M\". This recount is "
                f"used to verify the public payer feed in /r/d-fleet-feeds. | done looks like: "
                f"one line: offers N, locks M. | MATERIAL: {self._material()}")
        self.assertEqual(brsolve.solve(spec), "offers 1, locks 2")

    def test_lock_count_only(self):
        spec = (f"verification | From the note the table at the end of this note (an excerpt "
                f"of the tclk board, one frame per line: seq | time | type | from | ref): how "
                f"many rows are lock frames posted by {self.DID1}? Give the count. This recount "
                f"is used to verify the public payer feed in /r/d-fleet-feeds. | done looks like: "
                f"one line: the count. | MATERIAL: {self._material()}")
        self.assertEqual(brsolve.solve(spec), "2")

    def test_external_note_is_none(self):
        spec = (f"verification | From the note /kv/tclk-mat-fc/mtask-1d4ea1fc (an excerpt of "
                f"the tclk board, one frame per line: seq | time | type | from | ref): how many "
                f"rows are lock frames posted by {self.DID1}? Give the count. | done looks like: "
                f"one line: the count.")
        self.assertIsNone(brsolve.solve(spec))


# ═══════════════════════════════════════════════════════════════════════
# inference
# ═══════════════════════════════════════════════════════════════════════

class TestInference(unittest.TestCase):
    HEADER = ["seq", "payer", "amount", "asset", "proto", "time"]

    def test_even_seq(self):
        mat = make_material(
            self.HEADER,
            [[7, "PA", 100, "FLOP", "a2a", "01:00:00"],
             [4, "PB", 200, "FLOP", "a2a", "02:00:00"],
             [10, "PC", 300, "FLOP", "a2a", "03:00:00"],
             [3, "PD", 400, "FLOP", "a2a", "04:00:00"]],
        )
        spec = ("inference | From the note the table at the end of this note (rows: seq | "
                "payer | amount | asset | proto | time): output the seq values that are even "
                "numbers, in ascending order, comma-separated (or 'none'). | done looks like: "
                f"one line: comma-separated seq values or 'none'. | MATERIAL: {mat}")
        self.assertEqual(brsolve.solve(spec), "4, 10")

    def test_even_seq_none(self):
        mat = make_material(self.HEADER, [[1, "PA", 100, "FLOP", "a2a", "01:00:00"],
                                           [3, "PB", 200, "FLOP", "a2a", "02:00:00"]])
        spec = ("inference | From the note the table at the end of this note (rows: seq | "
                "payer | amount | asset | proto | time): output the seq values that are even "
                "numbers, in ascending order, comma-separated (or 'none'). | done looks like: "
                f"one line: comma-separated seq values or 'none'. | MATERIAL: {mat}")
        self.assertEqual(brsolve.solve(spec), "none")

    def test_earliest_latest(self):
        mat = make_material(
            self.HEADER,
            [[98731, "tfRCWxXw", 400, "FLOP", "blockrewards", "00:45:45"],
             [60788, "dSro7iDF", 3, "FLOP", "kibble", "12:37:46"],
             [25825, "Muxri3ss", 1000000, "PAPER", "a2a", "23:38:00"]],
        )
        spec = ("inference | From the note the table at the end of this note (rows: seq | "
                "payer | amount | asset | proto | time): output the seq of the row with the "
                "earliest time and the seq of the row with the latest time, as \"<earliest_seq> "
                "<latest_seq>\" (ties: lower seq). | done looks like: one line: two seq values. "
                f"| MATERIAL: {mat}")
        self.assertEqual(brsolve.solve(spec), "98731 25825")

    def test_top_n_amount(self):
        mat = make_material(
            self.HEADER,
            [[1, "PA", 500, "FLOP", "a2a", "01:00:00"],
             [2, "PB", 900, "FLOP", "a2a", "02:00:00"],
             [3, "PC", 900, "FLOP", "a2a", "03:00:00"],
             [4, "PD", 100, "FLOP", "a2a", "04:00:00"],
             [5, "PE", 700, "FLOP", "a2a", "05:00:00"]],
        )
        spec = ("inference | From the note the table at the end of this note (rows: seq | "
                "payer | amount | asset | proto | time): output the seq values of the 3 rows "
                "with the largest amount, highest first (ties broken by lower seq first), "
                "comma-separated. | done looks like: one line: three seq values, comma-separated. "
                f"| MATERIAL: {mat}")
        self.assertEqual(brsolve.solve(spec), "2, 3, 5")

    def test_sum_per_payer_tie_ascii_smaller(self):
        mat = make_material(
            self.HEADER,
            [[1, "ALPHA", 100, "FLOP", "a2a", "01:00:00"],
             [2, "BETA", 150, "FLOP", "a2a", "02:00:00"],
             [3, "ALPHA", 100, "FLOP", "a2a", "03:00:00"],
             [4, "BETA", 50, "FLOP", "a2a", "04:00:00"]],
        )
        spec = ("inference | From the note the table at the end of this note (rows: seq | "
                "payer | amount | asset | proto | time): sum the amount per payer and output "
                "the payer with the largest total and that total, as \"<payer> <total>\" "
                "(ties: ASCII-smaller payer). | done looks like: one line: payer then total. "
                f"| MATERIAL: {mat}")
        self.assertEqual(brsolve.solve(spec), "ALPHA 200")


# ═══════════════════════════════════════════════════════════════════════
# protocol
# ═══════════════════════════════════════════════════════════════════════

class TestProtocol(unittest.TestCase):
    @unittest.skipUnless(HAVE_FIXTURES, "harness fixtures not available in this environment")
    def test_fixture_claimed_no_rejections(self):
        o = load_fixture("0x13bad2b0af8a")
        self.assertEqual(brsolve.solve(o["spec"]), "claimed — no rejected records.")

    @unittest.skipUnless(HAVE_FIXTURES, "harness fixtures not available in this environment")
    def test_fixture_rejected_lock(self):
        o = load_fixture("0xdc130b94f08a")
        ans = brsolve.solve(o["spec"])
        self.assertEqual(
            ans,
            "accepted — rejected lock: lock must be posted in mb-p-tclk-4dc5b09b14f27194.",
        )

    def test_url_based_protocol_is_none(self):
        spec = ("protocol | From https://technocore.chat/patterns.md: What is the prefix for "
                "an unguessable room name that is never listed or announced? | done looks like: "
                "one line: the exact value or phrase from the cited document (quote it), nothing else")
        self.assertIsNone(brsolve.solve(spec))

    def test_no_material_is_none(self):
        spec = ("protocol | Fold this tclk/1 transcript with the reference rules "
                "(github.com/flop-labs/tclk, foldTranscript). | done looks like: one line")
        self.assertIsNone(brsolve.solve(spec))


# ═══════════════════════════════════════════════════════════════════════
# validation
# ═══════════════════════════════════════════════════════════════════════

def make_validation_spec(task, reference, deliverable):
    return (
        f'validation | Validate a deliverable. TASK that was posted: "{task}". '
        f"REFERENCE ANSWER the task's author holds (private to you as validator): "
        f'"{reference}". DELIVERABLE submitted by a worker: "{deliverable}". '
        "Does the deliverable give the reference answer (same values, order where order is "
        "asked, nothing invented)? Reply PASS or FAIL, then one sentence naming the exact "
        "match or the exact discrepancy. | done looks like: one line: PASS or FAIL, then one sentence."
    )


class TestValidation(unittest.TestCase):
    def test_pass_deliverable_contains_reference(self):
        spec = make_validation_spec(
            "What is X?",
            "An MCP server exposing the protocol as tool calls",
            "An MCP server exposing the protocol as tool calls. Stateless — see below.",
        )
        ans = brsolve.solve(spec)
        self.assertTrue(ans.startswith("PASS —"))

    def test_pass_short_numeric_reference(self):
        spec = make_validation_spec(
            "How many lock frames?",
            "1",
            '"215024 | 23:56 | lock | did:key:z6Mku3jTb7X9rBe1uE3wiJpvG5dhgy4a8Fv5oujHTXJi9nzX | '
            '56e83cbee4e1a757" 1',
        )
        ans = brsolve.solve(spec)
        self.assertTrue(ans.startswith("PASS —"))

    def test_pass_exact_token_match(self):
        spec = make_validation_spec(
            "How many rows have proto a2a?",
            "9: f9d70398, e9b9e186, e6f2b465",
            "9 f9d70398, e9b9e186, e6f2b465",
        )
        self.assertTrue(brsolve.solve(spec).startswith("PASS —"))

    def test_fuzzy_case_is_none(self):
        spec = make_validation_spec(
            "How many offers and locks?",
            "offers 1, locks 1",
            "Quoted from the table: the only two rows with this sender are an offer row and a "
            "lock row; all other rows have a different sender DID.",
        )
        self.assertIsNone(brsolve.solve(spec))

    def test_verdict_mismatch_fail(self):
        spec = make_validation_spec(
            "Does X equal Y?",
            "PASS",
            "FAIL — the values do not match at all.",
        )
        ans = brsolve.solve(spec)
        self.assertTrue(ans.startswith("FAIL —"))

    def test_numeric_mismatch_fail(self):
        spec = make_validation_spec(
            "What is the HTTP status?",
            "status 400",
            "status 404",
        )
        ans = brsolve.solve(spec)
        self.assertTrue(ans.startswith("FAIL —"))

    def test_refusal_deliverable_is_none(self):
        spec = make_validation_spec(
            "What is the HTTP status?",
            "status 400",
            "Could not fetch the cited source. Re-issue with a reachable copy and I will deliver.",
        )
        self.assertIsNone(brsolve.solve(spec))

    def test_malformed_spec_is_none(self):
        self.assertIsNone(brsolve.solve("validation | not a real validation payload at all"))


# ═══════════════════════════════════════════════════════════════════════
# review (solve_review)
# ═══════════════════════════════════════════════════════════════════════

class TestReview(unittest.TestCase):
    def test_solve_via_solve_is_always_none(self):
        spec = ("review | From https://raw.githubusercontent.com/flop-labs/technocore-chat/"
                "main/README.md: What is the default value for CHAT_RATE_READ? | done looks "
                "like: one line: the exact value or phrase from the cited document (quote it), "
                "nothing else")
        self.assertIsNone(brsolve.solve(spec))

    def test_solve_review_no_fetch_is_none(self):
        spec = ("review | From https://raw.githubusercontent.com/flop-labs/technocore-chat/"
                "main/README.md: What is the default value for CHAT_RATE_READ? | done looks "
                "like: one line: the exact value or phrase from the cited document (quote it), "
                "nothing else")
        self.assertIsNone(brsolve.solve_review(spec, fetch=None))

    def test_solve_review_env_var_table(self):
        spec = ("review | From https://raw.githubusercontent.com/flop-labs/technocore-chat/"
                "main/README.md: What is the default value for CHAT_RATE_READ? | done looks "
                "like: one line: the exact value or phrase from the cited document (quote it), "
                "nothing else")
        doc = "| Variable | Default |\n| --- | --- |\n| `CHAT_RATE_READ` | `5/s` |\n"

        def fetch(url):
            self.assertIn("technocore-chat", url)
            return doc

        ans = brsolve.solve_review(spec, fetch=fetch)
        self.assertEqual(ans, "5/s")

    def test_solve_review_json_kv(self):
        spec = ("review | From https://technocore.chat/skill.md: What is the "
                "ephemeral_ttl_seconds? | done looks like: one line: the exact value or phrase "
                "from the cited document (quote it), nothing else")
        doc = 'Config defaults include "ephemeral_ttl_seconds": 900 for scratch notes.'

        def fetch(url):
            return doc

        ans = brsolve.solve_review(spec, fetch=fetch)
        self.assertIsNotNone(ans)
        self.assertIn("900", ans)

    def test_solve_review_value_absent_is_none(self):
        spec = ("review | From https://technocore.chat/skill.md: What is the "
                "ephemeral_ttl_seconds? | done looks like: one line: the exact value or phrase "
                "from the cited document (quote it), nothing else")

        def fetch(url):
            return "This document does not mention that setting at all."

        self.assertIsNone(brsolve.solve_review(spec, fetch=fetch))

    def test_solve_review_fetch_returns_none(self):
        spec = ("review | From https://technocore.chat/skill.md: What is X? | done looks like: "
                "one line")

        def fetch(url):
            return None

        self.assertIsNone(brsolve.solve_review(spec, fetch=fetch))


# ═══════════════════════════════════════════════════════════════════════
# robustness
# ═══════════════════════════════════════════════════════════════════════

class TestRobustness(unittest.TestCase):
    def test_never_raises_on_garbage(self):
        for spec in ("", "   ", "not a valid spec at all", "protocol |", "math | | MATERIAL: |||",
                      "extraction | x | MATERIAL: "):
            try:
                brsolve.solve(spec)
            except Exception as e:  # noqa: BLE001
                self.fail(f"solve() raised on {spec!r}: {e!r}")

    def test_unknown_family_is_none(self):
        self.assertIsNone(brsolve.solve("mystery | some unknown family | done looks like: x"))


if __name__ == "__main__":
    unittest.main()
