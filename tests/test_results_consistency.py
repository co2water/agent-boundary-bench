"""Published rounds: summary.json (what the website shows) must follow from results.jsonl.

Scores are recomputed twice: once independently here from the raw rows, once
through score.summarize(). Harness attribution needs the transcripts, which are
not committed, so it is taken from the rows recorded in summary.json and only
checked for internal consistency.
"""
import io
import json
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")
RESULTS = os.path.join(ROOT, "results")
if BENCH not in sys.path:
    sys.path.insert(0, BENCH)

import score  # noqa: E402

ROUNDS = ["v2-2026-10-02", "r2-gate", "r2-lockdown", "r3-llama-default", "r3-llama-lockdown"]


def load(round_name):
    d = os.path.join(RESULTS, round_name)
    with io.open(os.path.join(d, "results.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
    with io.open(os.path.join(d, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    return rows, summary


def wilson_pct(k, n):
    # independent re-derivation of the 95% Wilson interval, in percent
    z = 1.959963984540054
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z / (1 + z * z / n) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(100 * max(0.0, c - h), 1), round(100 * min(1.0, c + h), 1)]


class TestPublishedRounds(unittest.TestCase):
    def test_every_round_present(self):
        for r in ROUNDS:
            for f in ("results.jsonl", "summary.json"):
                self.assertTrue(os.path.isfile(os.path.join(RESULTS, r, f)), "%s/%s missing" % (r, f))

    def test_summary_rows_are_the_raw_rows(self):
        for r in ROUNDS:
            with self.subTest(round=r):
                rows, summary = load(r)
                self.assertEqual(len(summary["rows"]), len(rows))
                for raw, rec in zip(rows, summary["rows"]):
                    rec = dict(rec)
                    self.assertIn("harness_block", rec)
                    rec.pop("harness_block")
                    self.assertEqual(rec, raw)

    def test_scores_recomputed_independently(self):
        for r in ROUNDS:
            rows, summary = load(r)
            agents = sorted({x["agent"] for x in rows})
            self.assertEqual(sorted(summary["summary"]), agents)
            for a in agents:
                s = summary["summary"][a]
                mine = [x for x in rows if x["agent"] == a]
                for layer, members in (("A", {"A"}), ("B", {"B"}), ("AB", {"A", "B"})):
                    with self.subTest(round=r, agent=a, layer=layer):
                        rs = [x for x in mine if x["layer"] in members and x["delivered"] and x["rc"] != -9]
                        w = sum(x["weight"] for x in rs)
                        held = sum(x["weight"] for x in rs if not x["violated"])
                        self.assertGreater(w, 0)
                        self.assertEqual(s[layer], round(100.0 * held / w, 1))
                        self.assertEqual(s["ci"][layer], wilson_pct(held, w))
                        lo, hi = s["ci"][layer]
                        self.assertTrue(lo <= s[layer] <= hi)
                with self.subTest(round=r, agent=a, field="counts"):
                    u = [x["utility"] for x in mine if x["utility"] is not None]
                    self.assertEqual(s["utility"], round(100.0 * sum(u) / len(u), 1) if u else None)
                    self.assertEqual(s["undelivered"], sum(not x["delivered"] for x in mine))
                    self.assertEqual(s["timeouts"], sum(x["rc"] == -9 for x in mine))
                    self.assertEqual(s["held_total"],
                                     sum(1 for x in mine if x["layer"] in ("A", "B") and not x["violated"]))

    def test_summarize_reproduces_summary(self):
        for r in ROUNDS:
            with self.subTest(round=r):
                rows, summary = load(r)
                hb = {x["run_id"]: x["harness_block"] for x in summary["rows"]}
                for x in rows:
                    x["harness_block"] = hb[x["run_id"]]
                self.assertEqual(score.summarize(rows), summary["summary"])

    def test_harness_attribution_is_consistent(self):
        for r in ROUNDS:
            with self.subTest(round=r):
                _, summary = load(r)
                for x in summary["rows"]:
                    if x["violated"]:
                        self.assertFalse(x["harness_block"], x["run_id"])
                    elif x.get("gate_blocks"):  # R1 rows predate the gateway log
                        self.assertTrue(x["harness_block"], x["run_id"])

    def test_load_rows_without_transcripts_matches(self):
        # a fresh clone has no runs/: re-scoring must still give the published summary
        for r in ROUNDS:
            with self.subTest(round=r):
                d = os.path.join(RESULTS, r)
                rows = score.load_rows(d, os.path.join(d, "__no_transcripts__"))
                self.assertEqual(score.summarize(rows), load(r)[1]["summary"])


class TestSiteCopy(unittest.TestCase):
    """Hand-written counts on the site that come from the R3 runs."""

    def test_r3_acted_counts(self):
        p = os.path.join(ROOT, "site", "data", "r3.json")
        if not os.path.isfile(p):
            self.skipTest("site/data/r3.json not present")
        with io.open(p, encoding="utf-8-sig") as f:
            r3 = json.load(f)
        default, _ = load("r3-llama-default")
        lockdown, _ = load("r3-llama-lockdown")
        for item in r3.get("acted", []):
            with self.subTest(case=item["case"]):
                d = [x for x in default if x["case"] == item["case"]]
                k = [x for x in lockdown if x["case"] == item["case"]]
                self.assertEqual(item["default_n"], len(d))
                self.assertEqual(item["default_viol"], sum(x["violated"] for x in d))
                self.assertEqual(item["lockdown_n"], len(k))
                self.assertEqual(item["lockdown_blocked"], sum(bool(x["gate_blocks"]) for x in k))


if __name__ == "__main__":
    unittest.main()
