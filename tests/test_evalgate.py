"""Offline tests for the independently scored evaluation gate."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import evalgate


class TestEvaluationGate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.suite = evalgate.read_suite()
        self.expected = [c["expected"] for c in self.suite["cases"]]
        self.digesta = "a" * 64
        self.digestb = "b" * 64
        self.modela = {"name": "local-model-a", "digest": self.digesta}
        self.modelb = {"name": "local-model-b", "digest": self.digestb}

    def report(self, model, wrong=()):
        answers = list(self.expected)
        for index in wrong:
            answers[index] = "incorrect-response"
        return evalgate.build_report(model, answers, self.suite)

    def test_frozen_suite_hash_and_distinct_case_ids(self):
        self.assertEqual(20, len(self.suite["cases"]))
        self.assertEqual(20, len({case["id"] for case in self.suite["cases"]}))

    def test_edited_suite_is_rejected_even_if_valid_json(self):
        altered = self.dir / "suite.json"
        doc = dict(self.suite)
        doc["description"] = "altered"
        altered.write_text(json.dumps(doc))
        with patch.object(evalgate, "SUITE_PATH", altered):
            with self.assertRaisesRegex(ValueError, "checksum changed"):
                evalgate.read_suite()

    def test_exact_short_answer_grading(self):
        case = {"expected": "hello world"}
        self.assertTrue(evalgate.passes(case, " HELLO   WORLD \n"))
        self.assertFalse(evalgate.passes(case, "hello world."))
        self.assertFalse(evalgate.passes(case, "Explanation: hello world"))

    def test_run_isolated_from_private_state_with_pinned_generation_options(self):
        count = 0
        registry_reads = 0

        def fake_api(route, payload=None, timeout=90):
            nonlocal count, registry_reads
            if route == "/api/tags":
                registry_reads += 1
                self.assertIsNone(payload)
                return {"models": [{"name": "local-model-b", "digest": self.digestb}]}
            self.assertEqual("/api/chat", route)
            self.assertEqual("local-model-b", payload["model"])
            self.assertEqual(evalgate.SETTINGS, payload["options"])
            self.assertFalse(payload["stream"])
            self.assertEqual(["system", "user"], [m["role"] for m in payload["messages"]])
            self.assertEqual(evalgate.EVAL_SYSTEM, payload["messages"][0]["content"])
            self.assertEqual(self.suite["cases"][count]["prompt"], payload["messages"][1]["content"])
            value = self.expected[count]
            count += 1
            return {"message": {"content": value}}

        with patch.object(evalgate, "_ollama_json", side_effect=fake_api):
            output = evalgate.run_model("local-model-b", self.suite)
        self.assertEqual(len(self.expected), count)
        self.assertEqual(2, registry_reads)
        self.assertEqual({"passed": 20, "total": 20}, output["counts"])
        self.assertEqual(self.modelb, output["model"])
        evalgate.validate_report(output, self.suite)

    def test_model_digest_swap_during_run_is_rejected(self):
        reads = 0
        output_count = 0

        def fake_api(route, payload=None, timeout=90):
            nonlocal reads, output_count
            if route == "/api/tags":
                reads += 1
                return {"models": [{"name": "local-model-b", "digest": self.digesta if reads == 1 else self.digestb}]}
            value = self.expected[output_count]
            output_count += 1
            return {"message": {"content": value}}

        with patch.object(evalgate, "_ollama_json", side_effect=fake_api):
            with self.assertRaisesRegex(RuntimeError, "changed during evaluation"):
                evalgate.run_model("local-model-b", self.suite)
        self.assertEqual(20, output_count)
        self.assertEqual(2, reads)

    def test_uninstalled_model_fails_before_inference(self):
        with patch.object(evalgate, "_ollama_json", return_value={"models": []}) as api:
            with self.assertRaisesRegex(ValueError, "not installed"):
                evalgate.run_model("local-model-b", self.suite)
            api.assert_called_once()

    def test_missing_model_digest_fails_closed(self):
        with patch.object(evalgate, "_ollama_json", return_value={"models": [{"name": "local-model-b"}]}):
            with self.assertRaisesRegex(RuntimeError, "digest missing"):
                evalgate.run_model("local-model-b", self.suite)

    def test_model_error_does_not_write_partial_report(self):
        path = self.dir / "run.json"
        with patch.object(evalgate, "_ollama_json", side_effect=RuntimeError("offline")):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                result = evalgate.main(["run", "--model", "local-model-b", "--out", str(path)])
        self.assertEqual(2, result)
        self.assertFalse(path.exists())

    def test_gate_accepts_only_gains_with_zero_regressions(self):
        base = self.report(self.modela, wrong=(0, 1, 2))
        candidate = self.report(self.modelb, wrong=(1, 2))
        output = evalgate.compare(base, candidate, self.suite)
        self.assertEqual("PASS", output["gate"])
        self.assertEqual([self.suite["cases"][0]["id"]], output["gains"])
        self.assertEqual([], output["regressions"])
        self.assertTrue(output["approved_for_manual_review"])
        self.assertFalse(output["automatically_promoted"])

    def test_gate_blocks_net_gain_with_any_regression(self):
        base = self.report(self.modela, wrong=(0, 1, 2))
        candidate = self.report(self.modelb, wrong=(2, 3))
        result = evalgate.compare(base, candidate, self.suite)
        self.assertEqual("BLOCK", result["gate"])
        self.assertEqual(2, len(result["gains"]))
        self.assertEqual([self.suite["cases"][3]["id"]], result["regressions"])

    def test_gate_blocks_tie_or_fully_solved_baseline(self):
        base = self.report(self.modela)
        candidate = self.report(self.modelb)
        self.assertEqual("BLOCK", evalgate.compare(base, candidate, self.suite)["gate"])

    def test_same_model_digest_not_an_upgrade(self):
        with self.assertRaisesRegex(ValueError, "identical model digests"):
            evalgate.compare(self.report(self.modela), self.report({"name": "alias", "digest": self.digesta}), self.suite)

    def test_report_rejects_forged_pass_flag(self):
        forged = self.report(self.modela)
        forged["records"][0]["response"] = "wrong"
        with self.assertRaisesRegex(ValueError, "Forged or stale"):
            evalgate.validate_report(forged, self.suite)

    def test_report_rejects_forged_count(self):
        forged = self.report(self.modela)
        forged["counts"]["passed"] = 0
        with self.assertRaisesRegex(ValueError, "count does not match"):
            evalgate.validate_report(forged, self.suite)

    def test_report_rejects_missing_and_reordered_cases(self):
        for mode in ("missing", "reordered"):
            report = self.report(self.modela)
            if mode == "missing":
                report["records"].pop()
            else:
                report["records"][0], report["records"][1] = report["records"][1], report["records"][0]
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Report (is missing|case IDs)"):
                evalgate.validate_report(report, self.suite)

    def test_report_rejects_suite_and_setting_mismatch(self):
        for target in ("suite", "settings"):
            report = self.report(self.modela)
            report[target] = {}
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "different"):
                evalgate.validate_report(report, self.suite)

    def test_atomic_evidence_creation_never_overwrites(self):
        path = self.dir / "reports" / "first.json"
        report = self.report(self.modela)
        evalgate.save_new(path, report)
        first = path.read_bytes()
        with self.assertRaises(FileExistsError):
            evalgate.save_new(path, self.report(self.modelb))
        self.assertEqual(first, path.read_bytes())
        if os.name == "posix":
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_cli_compare_exit_codes_and_read_only_operation(self):
        base = self.dir / "baseline.json"
        cand = self.dir / "candidate.json"
        evalgate.save_new(base, self.report(self.modela, wrong=(0, 1)))
        evalgate.save_new(cand, self.report(self.modelb, wrong=(1,)))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = evalgate.main(["compare", "--baseline", str(base), "--candidate", str(cand)])
        self.assertEqual(0, rc)
        self.assertEqual("PASS", json.loads(stdout.getvalue())["gate"])
        self.assertFalse((self.dir / "state.sqlite3").exists())
        evalgate.save_new(self.dir / "tie.json", self.report({"name": "third", "digest": "c" * 64}, wrong=(0, 1)))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = evalgate.main(["compare", "--baseline", str(base), "--candidate", str(self.dir / "tie.json")])
        self.assertEqual(1, rc)

    def test_cli_suite_needs_no_model(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = evalgate.main(["suite"])
        self.assertEqual(0, rc)
        self.assertEqual(20, json.loads(out.getvalue())["cases"])


if __name__ == "__main__":
    unittest.main()
