from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "pro-dispatch"


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.env = os.environ.copy()
        self.env["CODEX_PRO_DISPATCH_HOME"] = self.temporary.name

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

    def test_help(self) -> None:
        completed = self.run_cli("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("official-app Codex Pro Dispatch", completed.stdout)

    def test_missing_worker_is_structured_error(self) -> None:
        completed = self.run_cli(
            "prepare",
            "--parent-task-id",
            "parent-7319",
            "--assignment-id",
            "dispatch-cli-7319",
            input_text="Task",
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_type"], "ConfigurationError")

    def test_end_to_end_cli_state_flow(self) -> None:
        worker = self.run_cli(
            "worker",
            "set",
            "--conversation-id",
            "6a87c2b8-0a34-83e8-8409-27bc1f4fef5e",
            "--confirm-pro",
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)

        prepared = self.run_cli(
            "prepare",
            "--parent-task-id",
            "parent-7319",
            "--assignment-id",
            "dispatch-cli-7319",
            input_text="Task",
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        prepared_payload = json.loads(prepared.stdout)
        self.assertIn("CODEX_PRO_DISPATCH_RESULT", prepared_payload["wrapped_prompt"])

        submitted = self.run_cli("submitted", "dispatch-cli-7319")
        self.assertEqual(submitted.returncode, 0, submitted.stderr)

        completed = self.run_cli(
            "complete",
            "dispatch-cli-7319",
            input_text=(
                "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-cli-7319]\n"
                "READY"
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["payload"], "READY")


if __name__ == "__main__":
    unittest.main()
