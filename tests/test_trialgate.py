"""No Ollama needed: deterministic cross-version generator and fail-closed gate."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import trialgate as t


class TrialGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.plan = t.create_plan([101, 202, 303])
        self.a = {"name": "baseline:latest", "digest": "a" * 64}
        self.b = {"name": "candidate:latest", "digest": "b" * 64}
        self.correct = [[case["expected"] for case in t.generate_cases(seed)]
                        for seed in self.plan["seeds"]]

    def report(self, model, wrong=()):
        answers = copy.deepcopy(self.correct)
        for seed_index, case_index in wrong:
            answers[seed_index][case_index] = "NOT_THE_ANSWER"
        return t.build_report(model, self.plan, answers)

    def test_seeded_cases_have_locked_cross_version_bytes(self):
        digest = hashlib.sha256(t.canonical(t.generate_cases(42))).hexdigest()
        self.assertEqual("9410c60f77e9f6b9bbdc87aaa7d5e625d8830bde0211a41d61eca7c2ad82d759", digest)
        self.assertEqual(24, len(t.generate_cases(42)))
        self.assertEqual(6, len({c["category"] for c in t.generate_cases(42)}))

    def test_different_seeds_produce_different_questions(self):
        cases1, cases2 = t.generate_cases(1001), t.generate_cases(1002)
        self.assertNotEqual([c["prompt"] for c in cases1], [c["prompt"] for c in cases2])
        self.assertEqual({c["id"] for c in cases1}, {c["id"] for c in cases2})

    def test_plan_generated_from_random_seeds_and_reproducible(self):
        plan = t.create_plan()
        self.assertEqual(3, len(set(plan["seeds"])))
        self.assertEqual(plan, t.validate_plan(copy.deepcopy(plan)))
        self.assertEqual(t.plan_id(plan["seeds"]), plan["plan_id"])

    def test_plan_rejects_tamper_duplicate_wrong_length_and_bool(self):
        for seeds in ([101, 202, 202], [101, 202], [True, 202, 303], [0, 202, 303], [101, 202, 2**64]):
            bad = copy.deepcopy(self.plan)
            bad["seeds"] = seeds
            bad["plan_id"] = t.plan_id(seeds)
            with self.subTest(seeds=seeds), self.assertRaises(ValueError):
                t.validate_plan(bad)
        bad = copy.deepcopy(self.plan)
        bad["seeds"][0] = 444
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            t.validate_plan(bad)

    def test_generated_answers_are_scored_independently(self):
        report = self.report(self.a)
        self.assertEqual({"passed": 72, "total": 72}, report["counts"])
        self.assertEqual(report, t.validate_report(report))
        self.assertFalse(self.report(self.a, [(0,0)])["rounds"][0]["records"][0]["passed"])

    def test_run_calls_local_ollama_with_isolated_prompts(self):
        calls = []
        ids = [c["expected"] for seed in self.plan["seeds"] for c in t.generate_cases(seed)]
        def fake_api(route, payload=None, timeout=90):
            self.assertEqual("/api/chat", route)
            self.assertEqual(self.b["name"], payload["model"])
            self.assertEqual(["system", "user"], [m["role"] for m in payload["messages"]])
            self.assertEqual(t.SETTINGS, payload["options"])
            calls.append(payload)
            return {"message": {"content": ids[len(calls)-1]}}
        with patch.object(t, "model_fingerprint", return_value=self.b) as fp, patch.object(t, "_ollama_json", side_effect=fake_api):
            report = t.run_model(self.b["name"], self.plan)
        self.assertEqual(72, len(calls))
        self.assertEqual(2, fp.call_count)
        self.assertEqual({"passed": 72, "total": 72}, report["counts"])
        self.assertNotIn("state.sqlite3", json.dumps(calls))

    def test_change_of_model_digest_during_run_rejected(self):
        answers = iter([c["expected"] for seed in self.plan["seeds"] for c in t.generate_cases(seed)])
        with patch.object(t, "model_fingerprint", side_effect=[self.a, self.b]), patch.object(
            t, "_ollama_json", side_effect=lambda *a, **k: {"message":{"content":next(answers)}}
        ):
            with self.assertRaisesRegex(RuntimeError, "changed during trial"):
                t.run_model(self.a["name"], self.plan)

    def test_bad_inference_does_not_create_partial_evidence(self):
        planned = self.root / "plan.json"
        out = self.root / "failed.json"
        t.save_new(planned, self.plan)
        with patch.object(t, "model_fingerprint", return_value=self.a), patch.object(t, "_ollama_json", side_effect=RuntimeError("offline")):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = t.main(["run", "--model", self.a["name"], "--plan", str(planned), "--out", str(out)])
        self.assertEqual(2, rc)
        self.assertFalse(out.exists())

    def test_gate_accepts_improvements_in_two_rounds_with_zero_regressions(self):
        base = self.report(self.a, [(0,0), (1,0)])
        candidate = self.report(self.b)
        result = t.compare(base, candidate)
        self.assertEqual("PASS", result["gate"])
        self.assertEqual(2, result["gains"])
        self.assertEqual(2, result["improved_rounds"])
        self.assertFalse(result["automatically_promoted"])

    def test_gate_rejects_single_round_improvement(self):
        self.assertEqual("BLOCK", t.compare(self.report(self.a, [(0,0)]), self.report(self.b))["gate"])

    def test_gate_rejects_gain_with_any_regression(self):
        base = self.report(self.a, [(0,0), (1,0)])
        candidate = self.report(self.b, [(2,0)])
        result = t.compare(base, candidate)
        self.assertEqual("BLOCK", result["gate"])
        self.assertEqual(1, result["regressions"])

    def test_gate_rejects_different_plan_and_same_digest(self):
        other_plan = t.create_plan([111,222,333])
        cand = t.build_report(self.b,other_plan,[[c["expected"] for c in t.generate_cases(s)] for s in other_plan["seeds"]])
        with self.assertRaisesRegex(ValueError,"different challenge plans"):
            t.compare(self.report(self.a), cand)
        with self.assertRaisesRegex(ValueError,"identical model digests"):
            t.compare(self.report(self.a), self.report(self.a))

    def test_score_and_count_tampering_rejected(self):
        for field in ("passed", "response", "counts"):
            report = self.report(self.a)
            if field == "passed": report["rounds"][0]["records"][0]["passed"] = False
            if field == "response": report["rounds"][0]["records"][0]["response"] = "wrong"
            if field == "counts": report["counts"]["passed"] = 0
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,"(contradicts|Incorrect)"):
                t.validate_report(report)

    def test_reordered_or_missing_case_rejected(self):
        for alteration in ("swap", "pop"):
            report = self.report(self.a)
            records=report["rounds"][0]["records"]
            if alteration=="swap": records[0], records[1] = records[1], records[0]
            else: records.pop()
            with self.subTest(alteration=alteration),self.assertRaises(ValueError):
                t.validate_report(report)

    def test_tamper_settings_and_model_digest_rejected(self):
        report=self.report(self.a);report["settings"]["seed"] = 1
        with self.assertRaisesRegex(ValueError,"settings"):t.validate_report(report)
        report=self.report(self.a);report["model"]["digest"] = "bad"
        with self.assertRaisesRegex(ValueError,"digest"):t.validate_report(report)

    def test_file_cap_prevents_unbounded_read(self):
        path=self.root / "large.json"
        path.write_bytes(b" " * (t.MAX_INPUT_BYTES + 1))
        with self.assertRaisesRegex(ValueError,"size cap"):t.load_json(path)

    def test_plan_and_comparison_cli_have_correct_exit_codes(self):
        plan_path=self.root/"plan.json"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(0,t.main(["plan","--out",str(plan_path)]))
        plan=t.validate_plan(t.load_json(plan_path))
        self.assertEqual(plan,json.loads(out.getvalue()))
        self.assertFalse(plan_path.stat().st_mode & 0o077 if os.name=="posix" else False)
        base=self.root/"base.json";cand=self.root/"cand.json"
        self.plan=plan
        self.correct=[[c["expected"] for c in t.generate_cases(s)] for s in plan["seeds"]]
        t.save_new(base,self.report(self.a,[(0,0),(1,0)]))
        t.save_new(cand,self.report(self.b))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0,t.main(["compare","--baseline",str(base),"--candidate",str(cand)]))
        self.assertEqual("PASS",json.loads(output.getvalue())["gate"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2,t.main(["compare","--baseline",str(base),"--candidate",str(self.root/"missing.json")]))


if __name__ == "__main__":
    unittest.main()
