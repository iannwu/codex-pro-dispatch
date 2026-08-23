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

    def configure_and_prepare(self, assignment_id: str) -> dict[str, object]:
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
            assignment_id,
            input_text="Task",
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        return json.loads(prepared.stdout)

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
        prepared_payload = self.configure_and_prepare("dispatch-cli-7319")
        self.assertIn("CODEX_PRO_DISPATCH_RESULT", prepared_payload["wrapped_prompt"])

        read_back_path = Path(self.temporary.name) / "native-read-back.txt"
        read_back_path.write_bytes(str(prepared_payload["wrapped_prompt"]).encode("utf-8"))

        submitted = self.run_cli(
            "submitted",
            "dispatch-cli-7319",
            "--sent-prompt-file",
            str(read_back_path),
        )
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

    def test_submitted_rejects_leading_character_drift_and_blocks_resend(self) -> None:
        prepared = self.configure_and_prepare("dispatch-leading-drift-7319")
        submitted = self.run_cli(
            "submitted",
            "dispatch-leading-drift-7319",
            "--sent-prompt-file",
            "-",
            input_text="+" + str(prepared["wrapped_prompt"]),
        )

        self.assertEqual(submitted.returncode, 4)
        error = json.loads(submitted.stderr)
        self.assertEqual(error["error_type"], "StateError")
        self.assertEqual(error["details"]["status"], "indeterminate")
        self.assertTrue(error["details"]["no_resend"])

        status = self.run_cli("status", "dispatch-leading-drift-7319")
        self.assertEqual(status.returncode, 0, status.stderr)
        receipt = json.loads(status.stdout)["assignment"]
        self.assertEqual(receipt["status"], "indeterminate")
        self.assertEqual(receipt["submission_count"], 1)
        self.assertTrue(receipt["no_resend"])
        self.assertFalse(receipt["outbound_prompt_verified"])

        second = self.run_cli(
            "submitted",
            "dispatch-leading-drift-7319",
            "--sent-prompt-file",
            "-",
            input_text=str(prepared["wrapped_prompt"]),
        )
        self.assertEqual(second.returncode, 4)

    def test_submitted_rejects_newline_drift(self) -> None:
        prepared = self.configure_and_prepare("dispatch-newline-drift-7319")
        submitted = self.run_cli(
            "submitted",
            "dispatch-newline-drift-7319",
            "--sent-prompt-file",
            "-",
            input_text=str(prepared["wrapped_prompt"]) + "\n",
        )

        self.assertEqual(submitted.returncode, 4)
        receipt = json.loads(
            self.run_cli("status", "dispatch-newline-drift-7319").stdout
        )["assignment"]
        self.assertEqual(receipt["status"], "indeterminate")
        self.assertTrue(receipt["no_resend"])

    def test_late_readback_verification_completes_existing_response(self) -> None:
        assignment_id = "dispatch-late-readback-7319"
        prepared = self.configure_and_prepare(assignment_id)
        indeterminate = self.run_cli(
            "indeterminate",
            assignment_id,
            "--reason",
            "native read-back was temporarily stale",
        )
        self.assertEqual(indeterminate.returncode, 0, indeterminate.stderr)
        ambiguous = self.run_cli(
            "ambiguous",
            assignment_id,
            "--reason",
            "response was not visible during the first collection attempt",
        )
        self.assertEqual(ambiguous.returncode, 0, ambiguous.stderr)

        submitted = self.run_cli(
            "submitted",
            assignment_id,
            "--sent-prompt-file",
            "-",
            input_text=str(prepared["wrapped_prompt"]),
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        receipt = json.loads(submitted.stdout)["assignment"]
        self.assertEqual(receipt["status"], "submitted")
        self.assertEqual(receipt["submission_count"], 1)
        self.assertTrue(receipt["outbound_prompt_verified"])
        self.assertEqual(receipt["submission_recovered_from"], "ambiguous")

        completed = self.run_cli(
            "complete",
            assignment_id,
            input_text=(
                f"[CODEX_PRO_DISPATCH_RESULT assignment_id={assignment_id}]\n"
                "RECOVERED"
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["payload"], "RECOVERED")


if __name__ == "__main__":
    unittest.main()
