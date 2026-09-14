"""Real CLI, private synthetic history; no native or installed state access."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_pro_dispatch import core

ROOT = Path(__file__).resolve().parents[1]


class CurrentStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pro-current-status-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve(strict=True)
        self.paths = core.RuntimePaths(self.home / "config", self.home / "state")
        self.env = dict(os.environ, CODEX_PRO_DISPATCH_HOME=str(self.home),
                        PYTHONDONTWRITEBYTECODE="1")
        core.save_worker("unit-pro", confirm_pro=True, paths=self.paths)
        with core.state_lock(self.paths) as locked:
            for i in range(64):
                core._save_assignment("history-" + str(i), {
                    "status": "complete", "created_at": "2026-01-01T00:00:00Z",
                    "fixture_padding": "x" * 4096,
                }, self.paths, _locked=locked)

    def call(self, *args, code=0):
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin/pro-dispatch"), *args],
            input="", text=True, capture_output=True, env=self.env, timeout=15,
        )
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        value = json.loads(result.stdout if code == 0 else result.stderr)
        self.assertEqual(value["ok"], code == 0)
        if code:
            self.assertEqual(result.stdout, "")
        return result, value

    def records(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob("*.json")}

    def prepare(self):
        return core.prepare_assignment("fixture only", assignment_id="current-A",
            parent_task_id="unit-parent", paths=self.paths)

    def test_history_is_validated_but_not_emitted(self):
        before = self.records()
        full, history = self.call("status")
        short, current = self.call("status", "--current")
        self.assertGreater(len(full.stdout.encode()), 117000)
        self.assertLessEqual(len(short.stdout.encode()), 8192)
        self.assertNotIn("assignments", current)
        self.assertNotIn("fixture_padding", short.stdout)
        self.assertEqual(current, {k: v for k, v in history.items() if k != "assignments"})
        self.assertEqual(before, self.records())

    def test_active_receipt_and_historical_cooldown_are_not_hidden(self):
        prepared = self.prepare()
        core.arm_assignment(prepared.assignment_id, self.paths)
        with core.state_lock(self.paths) as locked:
            value = core.load_assignment("history-0", self.paths, _locked=locked)
            value["cooldown_until"] = (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(minutes=30)).isoformat()
            core._save_assignment("history-0", value, self.paths, _locked=locked)
        before = self.records()
        output, current = self.call("status", "--current")
        self.assertLessEqual(len(output.stdout.encode()), 8192)
        self.assertEqual(current["active_assignment"]["assignment_id"], "current-A")
        self.assertTrue(current["active_assignment"]["no_resend"])
        self.assertEqual(current["active_cooldown"]["assignment_id"], "history-0")
        self.assertGreater(current["active_cooldown"]["retry_after_seconds"], 0)
        self.assertEqual(before, self.records())

    def test_corrupt_historical_receipt_still_blocks_current_summary(self):
        path = self.paths.assignments_dir / "history-0.json"
        path.write_text("not JSON", encoding="utf-8")
        before = self.records()
        output, value = self.call("status", "--current", code=4)
        self.assertEqual(value["error_type"], "StateError")
        self.assertLess(len(output.stderr.encode()), 8192)
        self.assertEqual(before, self.records())

    def test_oversized_current_state_fails_closed_instead_of_truncating(self):
        prepared = self.prepare()
        with core.state_lock(self.paths) as locked:
            value = core.load_assignment(prepared.assignment_id, self.paths, _locked=locked)
            value["fixture_padding"] = "x" * 9000
            core._save_assignment(prepared.assignment_id, value, self.paths, _locked=locked)
        before = self.records()
        output, value = self.call("status", "--current", code=4)
        self.assertIn("exceeds 8192 bytes", value["error"])
        self.assertLess(len(output.stderr.encode()), 8192)
        self.assertEqual(before, self.records())

    def test_current_summary_cannot_be_mixed_with_assignment_lookup(self):
        before = self.records()
        _, value = self.call("status", "history-0", "--current", code=2)
        self.assertEqual(value["error_type"], "ConfigurationError")
        self.assertEqual(before, self.records())

    def test_conflicting_active_receipts_fail_with_bounded_diagnostics(self):
        with core.state_lock(self.paths) as locked:
            for i in range(64):
                rid = "history-" + str(i)
                value = core.load_assignment(rid, self.paths, _locked=locked)
                value["status"] = "armed"
                value["no_resend"] = True
                core._save_assignment(rid, value, self.paths, _locked=locked)
        before = self.records()
        output, value = self.call("status", "--current", code=4)
        self.assertEqual(value["details"], {"cause": "StateError"})
        self.assertLess(len(output.stderr.encode()), 8192)
        self.assertNotIn("history-", output.stderr)
        self.assertEqual(before, self.records())


if __name__ == "__main__":
    unittest.main()
