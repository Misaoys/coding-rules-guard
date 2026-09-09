"""Regressions for information lost by bounded, non-gating handoff views."""

import copy
import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "projection_contracts_under_test", ROOT / "scripts" / "guardlib" / "projections.py"
)
assert SPEC and SPEC.loader
projections = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(projections)


def evidence(index, result="pass", observed="checked"):
    return {
        "evidence_id": f"e{index:04d}",
        "result": result,
        "observed": observed,
        "execution": {"output_ref": f"logs/check-{index}.txt"},
    }


def history_state(items, review=None):
    return {"loop": {"attempt_history": [{
        "attempt_id": "a0001", "plan_revision": 1, "hypothesis": "bounded correction",
        "outcome": "fail", "result": "fail", "closure_reason": "use the diagnosis",
        "evidence_snapshot": items, "review_snapshot": review,
        "diagnosis": {"classification": "implementation", "source_refs": []},
    }]}}


class ProjectionContractTests(unittest.TestCase):
    def history(self, state, max_refs=8, max_chars=512):
        return projections.compact_context_history(state, 3, max_refs, max_chars)

    def test_failed_or_blocked_review_survives_full_evidence_list(self):
        for result in ("fail", "blocked"):
            with self.subTest(result=result):
                state = history_state([evidence(i) for i in range(8)],
                                      {"review_id": "r1", "result": result})
                item = self.history(state)["items"][0]
                self.assertIn(f"review: {result}", item["key_conclusions"])
                self.assertEqual(item["review_result"], result)
                self.assertEqual(item["review_ref"], "r1")
                self.assertLessEqual(len(item["key_conclusions"]), 8)

    def test_evidence_and_conclusion_clipping_are_separately_visible(self):
        state = history_state([evidence(i) for i in range(10)])
        view = self.history(state)
        item = view["items"][0]
        self.assertFalse(view["truncated"])
        self.assertEqual(item["evidence_total"], 10)
        self.assertTrue(item["evidence_truncated"])
        self.assertEqual(item["conclusions_total"], 10)
        self.assertTrue(item["conclusions_truncated"])
        self.assertTrue(item["historical_not_valid_for_gate"])

    def test_exact_limits_do_not_report_false_truncation(self):
        state = history_state([evidence(i, observed="x" * 16) for i in range(8)])
        item = self.history(state, max_chars=512)["items"][0]
        self.assertFalse(item["evidence_truncated"])
        self.assertFalse(item["conclusions_truncated"])
        self.assertEqual(item["text_truncated_fields"], [])
        self.assertFalse(item["source_refs_truncated"])

    def test_active_attempt_reports_reference_and_text_clipping(self):
        attempt = {"attempt_id": "a1", "hypothesis": "x" * 17,
                   "evidence_refs": [f"e{i}" for i in range(9)]}
        item = projections.compact_attempt(attempt, 8, 16)
        self.assertEqual(len(item["hypothesis"]), 16)
        self.assertEqual(item["evidence_refs_total"], 9)
        self.assertTrue(item["evidence_refs_truncated"])
        self.assertIn("hypothesis", item["text_truncated_fields"])

    def test_diagnosis_and_new_information_report_their_own_limits(self):
        diagnosis = {"cause_summary": "x" * 17, "source_refs": ["e1", "e2"],
                     "new_information": {"summary": "y" * 17, "source_refs": ["e3", "e4"]}}
        item = projections.compact_diagnosis(diagnosis, 1, 16)
        self.assertEqual(item["source_refs_total"], 2)
        self.assertTrue(item["source_refs_truncated"])
        self.assertIn("cause_summary", item["text_truncated_fields"])
        info = item["new_information"]
        self.assertTrue(info["source_refs_truncated"])
        self.assertIn("summary", info["text_truncated_fields"])

    def test_long_log_location_and_observation_are_marked_as_partial(self):
        source = evidence(1, "fail", "failure " * 20)
        source["execution"]["output_ref"] = "logs/" + "a" * 80
        item = self.history(history_state([source]), max_chars=32)["items"][0]
        self.assertEqual(len(item["record_locations"][0]), 32)
        self.assertIn("record_locations[0]", item["text_truncated_fields"])
        self.assertIn("evidence[e0001].observed", item["text_truncated_fields"])
        self.assertEqual(item["evidence_refs"], ["e0001"])

    def test_historical_source_refs_are_bounded_with_a_visible_count(self):
        state = history_state([evidence(1)])
        state["loop"]["attempt_history"][0]["diagnosis"]["source_refs"] = ["e1", "e2"]
        item = self.history(state, max_refs=1)["items"][0]
        self.assertEqual(item["source_refs_total"], 2)
        self.assertTrue(item["source_refs_truncated"])

    def test_diagnosis_and_failure_priority_and_nested_output_refs_are_preserved(self):
        state = history_state([evidence(i) for i in range(8)] + [evidence(8, "fail")])
        state["loop"]["attempt_history"][0]["diagnosis"]["source_refs"] = ["e0007"]
        item = self.history(state, max_refs=2)["items"][0]
        self.assertEqual(item["evidence_refs"], ["e0007", "e0008"])
        self.assertEqual(item["record_locations"], ["logs/check-7.txt", "logs/check-8.txt"])

    def test_full_snapshots_remain_excluded_and_sources_are_not_mutated(self):
        state = history_state([evidence(i) for i in range(32)])
        attempt = state["loop"]["attempt_history"][0]
        attempt["diagnosis"]["source_snapshot"] = {"large_log": "z" * 100000}
        before = copy.deepcopy(state)
        history = self.history(state)
        active = projections.compact_attempt(attempt, 8, 512)
        encoded = json.dumps([history, active])
        self.assertNotIn("large_log", encoded)
        self.assertNotIn("evidence_snapshot", encoded)
        self.assertLess(len(encoded.encode("utf-8")), 16000)
        self.assertEqual(state, before)

    def test_empty_or_legacy_projection_inputs_remain_readable(self):
        self.assertIsNone(projections.compact_attempt(None, 8, 512))
        self.assertIsNone(projections.compact_diagnosis(None, 8, 512))
        self.assertEqual(self.history({}), {"items": [], "total": 0, "truncated": False})

    def test_progress_escapes_c1_controls_and_unicode_line_separators(self):
        text = "任务\u009b31m\u0085\u2028\u2029"
        rendered = projections.safe_display_text(text)
        self.assertEqual(rendered, "任务\\u009b31m\\u0085\\u2028\\u2029")
        self.assertEqual(projections.safe_display_text("中文 test"), "中文 test")

    def test_progress_retains_risk_gaps_and_review_result(self):
        state = {"goal": "verify", "risk": {"details": ["host unavailable"]}, "gaps": ["host receipt"]}
        view = {"display_line": "phase=verify", "review_node": "recorded", "review_result": "fail"}
        output = "\n".join(projections.progress_text_lines(state, view))
        self.assertIn("review_result=fail", output)
        self.assertIn("host unavailable", output)
        self.assertIn("host receipt", output)


if __name__ == "__main__":
    unittest.main()
