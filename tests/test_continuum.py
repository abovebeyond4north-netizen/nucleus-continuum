"""Offline deterministic tests: no model download or network required."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import continuum


class ContinuumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = continuum.State(self.base / "state.db")
        self.addCleanup(self.state.close)

    def test_identity_stable_across_reopen(self):
        identity = self.state.identity()
        with continuum.State(self.base / "state.db") as another:
            self.assertEqual(identity, another.identity())

    def test_memory_recall_and_literal_sql_wildcard(self):
        self.state.remember("99% complete")
        self.state.remember("other things")
        self.assertEqual(1, len(self.state.notes("%")))
        self.assertEqual("99% complete", self.state.notes("99")[0]["content"])

    def test_exchange_is_ordered(self):
        self.state.add_exchange("hello", "world", "mock")
        history = self.state.history()
        self.assertEqual(["user", "assistant"], [x["role"] for x in history])
        self.assertEqual(["hello", "world"], [x["content"] for x in history])

    def test_export_import_exact_identity_and_records(self):
        self.state.remember("portable state")
        self.state.add_exchange("ping", "pong", "mock")
        dest = self.base / "backup.json"
        checksum = self.state.export(dest)
        self.assertEqual(checksum, json.loads(dest.read_text())["sha256"])
        with continuum.State(self.base / "restored.db") as restored:
            restored.restore(dest)
            self.assertEqual(self.state.snapshot(), restored.snapshot())

    def test_tamper_rejected_without_modification(self):
        self.state.remember("do not destroy")
        dest = self.base / "backup.json"
        self.state.export(dest)
        obj = json.loads(dest.read_text())
        obj["payload"]["notes"][0]["content"] = "changed"
        dest.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.state.restore(dest, replace=True)
        self.assertEqual("do not destroy", self.state.notes()[0]["content"])

    def test_import_conflict_rejected(self):
        dest = self.base / "backup.json"
        self.state.export(dest)
        self.state.remember("existing record")
        with self.assertRaisesRegex(ValueError, "Destination contains data"):
            self.state.restore(dest)

    def test_bad_snapshot_rejected(self):
        dest = self.base / "bad.json"
        payload = self.state.snapshot()
        payload["notes"].append({"id": "oops", "created_at": "x", "content": "x"})
        dest.write_text(json.dumps({"payload": payload, "sha256": continuum.digest(payload)}))
        with self.assertRaisesRegex(ValueError, "Malformed note"):
            self.state.restore(dest)

    def test_model_preference(self):
        self.assertEqual("qwen3.5:4b", continuum.select_model(["gemma3:4b", "qwen3.5:4b"]))
        self.assertEqual("gemma3:4b", continuum.select_model(["gemma3:4b"]))
        with self.assertRaises(ValueError):
            continuum.select_model([])

    def test_mock_chat_persists_after_success(self):
        def fake_api(route, payload, timeout):
            self.assertEqual("/api/chat", route)
            self.assertEqual("hi", payload["messages"][-1]["content"])
            self.assertEqual("system", payload["messages"][0]["role"])
            return {"message": {"content": "hello from mock"}}
        with patch.object(continuum, "_ollama_json", fake_api):
            output = continuum.ask(self.state, "hi", "qwen3.5:4b")
        self.assertEqual("hello from mock", output)
        self.assertEqual(2, len(self.state.history()))

    def test_failed_model_call_does_not_pollute_state(self):
        with patch.object(continuum, "_ollama_json", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                continuum.ask(self.state, "hi", "qwen3.5:4b")
        self.assertEqual([], self.state.history())

    def test_input_limits(self):
        with self.assertRaises(ValueError):
            self.state.remember(" ")
        with self.assertRaises(ValueError):
            self.state.add_exchange("", "answer", "test")


if __name__ == "__main__":
    unittest.main()
