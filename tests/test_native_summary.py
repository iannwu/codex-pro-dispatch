from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codex_pro_dispatch import core


class NativeSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = core.RuntimePaths(self.root / "config", self.root / "state")
        core.save_worker("native-worker", confirm_pro=True, paths=self.paths)
        self.prepared = core.prepare_assignment("Review this arithmetic: 17 * 23.", parent_task_id="parent", paths=self.paths)
        self.aid = self.prepared.assignment_id
        core.arm_assignment(self.aid, self.paths)
        core.mark_submitted(self.aid, self.prepared.wrapped_prompt, self.paths)
        self.response = f"{core.result_marker(self.aid)}\n 391\n中文 🙂\n\n{core.end_marker(self.aid)}"
        self.data = {
            "schemaVersion": 1,
            "thread": {"id": "native-worker", "kind": "chatgpt", "status": {"type": "idle"}},
            "turns": [{"id": "user", "status": "completed", "items": [
                {"id": "user", "type": "userMessage", "content": [{"type": "text", "text": self.prepared.wrapped_prompt}]},
                {"id": "assistant", "type": "agentMessage", "text": self.response},
            ]}],
        }

    def complete(self, data=None):
        return core.complete_assignment(self.aid, b"", self.paths, native_read=json.dumps(self.data if data is None else data).encode())

    def test_complete_retains_payload_and_honest_provenance(self):
        receipt, payload = self.complete()
        self.assertEqual(payload, " 391\n中文 🙂\n")
        self.assertEqual(receipt["verification_level"], "bounded_native_summary")
        self.assertFalse(receipt["generation_finality_verified"])
        self.assertFalse(receipt["source_bytes_verified"])
        self.assertIsNone(receipt["native_collection"]["raw_truncation"]["assistant.textTruncated"])
        # Short digits can occur in timestamps, random IDs, and SHA-256 hashes.
        # Check distinctive response text so the privacy assertion is stable.
        self.assertNotIn("中文 🙂", json.dumps(receipt, ensure_ascii=False))
        self.assertNotIn(json.dumps(payload), json.dumps(receipt))
        self.assertIsNone(core.active_assignment(self.paths))

    def test_wrong_worker_and_synthetic_completed_active_turn_cannot_pass(self):
        for change in (lambda d: d["thread"].update(id="wrong"), lambda d: d["thread"].update(status={"type": "active"}), lambda d: d["thread"].update(status="idle")):
            data = copy.deepcopy(self.data)
            change(data)
            with self.assertRaises(core.DispatchError):
                self.complete(data)
            self.assertEqual(core.load_assignment(self.aid, self.paths)["status"], "submitted")

    def test_stale_missing_and_mismatched_prompt_never_complete(self):
        for text in ("old prompt", self.prepared.wrapped_prompt + "x"):
            self.data["turns"][0]["items"][0]["content"][0]["text"] = text
            with self.assertRaises(core.StateError):
                self.complete()
        self.assertEqual(core.load_assignment(self.aid, self.paths)["submission_count"], 1)

    def test_duplicates_cross_turn_and_extra_items_reject(self):
        cases = []
        d = copy.deepcopy(self.data)
        d["turns"].append(copy.deepcopy(d["turns"][0]))
        cases.append(d)
        d = copy.deepcopy(self.data)
        d["turns"][0]["items"][1]["id"] = "user"
        cases.append(d)
        d = copy.deepcopy(self.data)
        d["turns"].append({"id": "other", "items": [d["turns"][0]["items"].pop()]})
        cases.append(d)
        d = copy.deepcopy(self.data)
        d["turns"][0]["items"].append({"id": "extra", "type": "agentMessage", "text": "extra"})
        cases.append(d)
        for d in cases:
            with self.assertRaises(core.DispatchError):
                self.complete(d)

    def test_every_selected_truncation_scope_rejects(self):
        for scope in (self.data, self.data["thread"], self.data["turns"][0], self.data["turns"][0]["items"][0], self.data["turns"][0]["items"][0]["content"][0], self.data["turns"][0]["items"][1]):
            for name in ("truncated", "textTruncated"):
                for value in (True, "false", None):
                    scope[name] = value
                    with self.assertRaises(core.MarkerError):
                        self.complete()
                del scope[name]

    def test_unrelated_truncation_does_not_reject_selected_exchange(self):
        self.data["turns"].append({"id": "old", "items": [{"id": "old", "type": "userMessage", "content": [], "textTruncated": True}]})
        self.complete()

    def test_prefix_and_20k_boundary_reject_even_with_marker_looking_body(self):
        agent = self.data["turns"][0]["items"][1]
        prefix = core.result_marker(self.aid) + "\n"
        footer = "\n" + core.end_marker(self.aid)
        for text in (prefix + "incomplete", prefix + "x" * (20000 - len(prefix + footer)) + footer):
            agent["text"] = text
            with self.assertRaises(core.MarkerError):
                self.complete()

    def test_malformed_json_is_body_safe(self):
        for raw in (b'{"schemaVersion":1,"schemaVersion":1}', b'{"sentinel":NaN}', b'{"sentinel":1e9999}', b'{"sentinel":"\\ud800"}', b'\xef\xbb\xbf{}', b'{} trailing', b'x' * (4 * 1024 * 1024 + 1)):
            with self.assertRaises(core.MarkerError) as raised:
                core.complete_assignment(self.aid, b"", self.paths, native_read=raw)
            self.assertNotIn("sentinel", str(raised.exception))

    def test_reread_metadata_is_idempotent_but_message_identity_is_immutable(self):
        first, _ = self.complete()
        self.data["page"] = {"nextCursor": "other"}
        second, _ = self.complete()
        self.assertEqual(first, second)
        self.data["turns"][0]["items"][1]["id"] = "replacement"
        with self.assertRaises(core.StateError):
            self.complete()

    def test_abandonment_cannot_be_reopened(self):
        core.abandon_assignment(self.aid, reason="test stopped", paths=self.paths)
        with self.assertRaises(core.StateError):
            self.complete()

    def test_cli_native_read_is_private_bounded_and_does_not_need_response_extraction(self):
        source = self.root / "native.json"
        source.write_text(json.dumps(self.data))
        source.chmod(0o600)
        env = {**os.environ, "CODEX_PRO_DISPATCH_HOME": str(self.root)}
        command = [sys.executable, str(ROOT / "bin/pro-dispatch"), "complete", self.aid, "--native-read-file", str(source)]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["payload"], " 391\n中文 🙂\n")
        source.chmod(0o644)
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_cooldown_appearing_after_prepare_blocks_arm(self):
        self.complete()
        next_one = core.prepare_assignment("next", parent_task_id="parent", paths=self.paths)
        receipt = core.load_assignment(self.aid, self.paths)
        receipt["cooldown_until"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        core._save_assignment(self.aid, receipt, self.paths)
        with self.assertRaises(core.CooldownError):
            core.arm_assignment(next_one.assignment_id, self.paths)
        self.assertEqual(core.load_assignment(next_one.assignment_id, self.paths)["status"], "prepared")

    def test_concurrent_arm_grants_only_once(self):
        self.complete()
        next_one = core.prepare_assignment("next", parent_task_id="parent", paths=self.paths)
        env = {**os.environ, "CODEX_PRO_DISPATCH_HOME": str(self.root)}
        command = [sys.executable, str(ROOT / "bin/pro-dispatch"), "arm", next_one.assignment_id]
        processes = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for process in processes:
            process.communicate(timeout=10)
        self.assertEqual(sorted(p.returncode for p in processes), [0, 4])

    def test_oversized_prompt_fails_before_reserving_worker(self):
        self.complete()
        before = len(core.list_assignments(self.paths))
        with self.assertRaises(core.ConfigurationError):
            core.prepare_assignment("🙂" * 10000, parent_task_id="parent", paths=self.paths)
        self.assertIsNone(core.active_assignment(self.paths))
        self.assertEqual(len(core.list_assignments(self.paths)), before)

    def test_foreign_schema_cannot_hide_unresolved_work(self):
        receipt = core.load_assignment(self.aid, self.paths)
        receipt["schema_version"] = 2
        core.atomic_write_json(core.assignment_path(self.aid, self.paths), receipt)
        with self.assertRaises(core.StateError):
            core.active_assignment(self.paths)

    def test_terminal_cooldown_cannot_be_purged_normally(self):
        self.complete()
        receipt = core.load_assignment(self.aid, self.paths)
        receipt["cooldown_until"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        core._save_assignment(self.aid, receipt, self.paths)
        with self.assertRaises(core.CooldownError):
            core.purge_local_state(paths=self.paths)
        self.assertTrue(self.paths.worker_file.exists())
        self.assertTrue(core.assignment_path(self.aid, self.paths).exists())


if __name__ == "__main__":
    unittest.main()
