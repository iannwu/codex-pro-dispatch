from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "pro-dispatch"
BUNDLED_CLI = ROOT / "skills" / "codex-pro-dispatch" / "scripts" / "pro-dispatch"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
sys.path.insert(0, str(ROOT / "src"))

from codex_pro_dispatch import cli as cli_module
from codex_pro_dispatch import sha256_text


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
            "--native-controls-confirmed",
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)

        prepared = self.run_cli(
            "prepare",
            "--parent-task-id",
            "parent-7319",
            "--assignment-id",
            assignment_id,
            "--native-controls-confirmed",
            input_text="Task",
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        payload = json.loads(prepared.stdout)
        armed = self.run_cli("arm", assignment_id)
        self.assertEqual(armed.returncode, 0, armed.stderr)
        return payload

    def write_evidence(self, assignment_id: str, response: str) -> Path:
        evidence = {
            "schema": "codex-pro-dispatch.native-collection/v1",
            "adapter_contract_id": "codex-desktop-native-collection/v1",
            "requested_conversation_id": "6a87c2b8-0a34-83e8-8409-27bc1f4fef5e",
            "loaded_conversation_id": "6a87c2b8-0a34-83e8-8409-27bc1f4fef5e",
            "assistant_message_id": f"native-assistant-{assignment_id}",
            "submitted_user_message_id": f"native-user-{assignment_id}",
            "role": "assistant",
            "generation_status": "completed",
            "generation_finality_provenance": "native-message-status",
            "truncated": False,
            "selected_result_outer_integrity": {
                "truncated": False,
                "provenance": "native-result-envelope",
            },
            "text": response,
            "observed_at": "2026-09-02T00:00:00.000Z",
        }
        path = Path(self.temporary.name) / f"{assignment_id}-evidence.json"
        path.write_text(json.dumps(evidence), encoding="utf-8")
        return path

    def test_help(self) -> None:
        completed = self.run_cli("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("official-app Codex Pro Dispatch", completed.stdout)

    def test_version(self) -> None:
        completed = self.run_cli("--version")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), f"pro-dispatch {VERSION}")

    def test_bundled_plugin_helper_runs_from_the_source_layout(self) -> None:
        completed = subprocess.run(
            [str(BUNDLED_CLI), "--version"],
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), f"pro-dispatch {VERSION}")

    def test_missing_worker_is_structured_error(self) -> None:
        completed = self.run_cli(
            "prepare",
            "--parent-task-id",
            "parent-7319",
            "--assignment-id",
            "dispatch-cli-7319",
            "--native-controls-confirmed",
            input_text="Task",
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_type"], "ConfigurationError")

    def test_worker_setup_requires_native_host_preflight(self) -> None:
        completed = self.run_cli(
            "worker",
            "set",
            "--conversation-id",
            "6a87c2b8-0a34-83e8-8409-27bc1f4fef5e",
            "--confirm-pro",
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn("native host-capability preflight", payload["error"])

    def test_prepare_requires_current_preflight_before_reading_prompt(self) -> None:
        completed = self.run_cli(
            "prepare",
            "--parent-task-id",
            "parent-7319",
            "--assignment-id",
            "dispatch-preflight-7319",
            "--prompt-file",
            "/path/that/must/not/be/read",
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stderr)
        self.assertIn("current invocation's native host-capability preflight", payload["error"])
        self.assertFalse((Path(self.temporary.name) / "state" / "assignments").exists())

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
            "--native-user-message-id",
            "native-user-dispatch-cli-7319",
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)

        response = (
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-cli-7319]\n"
            "READY"
        )
        completed = self.run_cli(
            "complete",
            "dispatch-cli-7319",
            "--native-evidence-file",
            str(self.write_evidence("dispatch-cli-7319", response)),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["completion_basis"], "native-inline")

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

    def test_submitted_allows_correcting_newline_readback_artifact(self) -> None:
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
        self.assertEqual(receipt["submission_count"], 1)
        self.assertTrue(receipt["readback_correction_allowed"])

        corrected = self.run_cli(
            "submitted",
            "dispatch-newline-drift-7319",
            "--sent-prompt-file",
            "-",
            input_text=str(prepared["wrapped_prompt"]),
        )
        self.assertEqual(corrected.returncode, 0, corrected.stderr)
        corrected_receipt = json.loads(corrected.stdout)["assignment"]
        self.assertEqual(corrected_receipt["status"], "submitted")
        self.assertEqual(corrected_receipt["submission_count"], 1)
        self.assertTrue(corrected_receipt["outbound_prompt_verified"])
        self.assertTrue(corrected_receipt["no_resend"])

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
            "--native-user-message-id",
            f"native-user-{assignment_id}",
            input_text=str(prepared["wrapped_prompt"]),
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        receipt = json.loads(submitted.stdout)["assignment"]
        self.assertEqual(receipt["status"], "submitted")
        self.assertEqual(receipt["submission_count"], 1)
        self.assertTrue(receipt["outbound_prompt_verified"])
        self.assertEqual(receipt["submission_recovered_from"], "ambiguous")

        response = (
            f"[CODEX_PRO_DISPATCH_RESULT assignment_id={assignment_id}]\n"
            "RECOVERED"
        )
        completed = self.run_cli(
            "complete",
            assignment_id,
            "--native-evidence-file",
            str(self.write_evidence(assignment_id, response)),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["completion_basis"], "native-inline")

    def test_reason_file_hashes_untrusted_text_without_persisting_or_interpolating(self) -> None:
        assignment_id = "dispatch-reason-file-7319"
        self.configure_and_prepare(assignment_id)
        reason = "native thread isn't loaded; $(touch should-not-run)"
        reason_path = Path(self.temporary.name) / "reason.txt"
        reason_path.write_text(reason + "\n", encoding="utf-8")

        indeterminate = self.run_cli(
            "indeterminate",
            assignment_id,
            "--reason-file",
            str(reason_path),
        )
        self.assertEqual(indeterminate.returncode, 0, indeterminate.stderr)
        indeterminate_receipt = json.loads(indeterminate.stdout)["assignment"]
        self.assertEqual(indeterminate_receipt["last_error_kind"], "native-send-indeterminate")
        self.assertNotIn("last_error", indeterminate_receipt)

        ambiguous = self.run_cli(
            "ambiguous",
            assignment_id,
            "--reason-file",
            str(reason_path),
        )
        self.assertEqual(ambiguous.returncode, 0, ambiguous.stderr)
        ambiguous_receipt = json.loads(ambiguous.stdout)["assignment"]
        self.assertEqual(ambiguous_receipt["last_error_kind"], "response-ambiguous")
        self.assertNotIn("last_error", ambiguous_receipt)

        abandoned = self.run_cli(
            "abandon",
            assignment_id,
            "--reason-file",
            str(reason_path),
        )
        self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
        abandoned_receipt = json.loads(abandoned.stdout)["assignment"]
        self.assertEqual(abandoned_receipt["abandon_reason_kind"], "user-authorized")
        self.assertNotIn("reason", abandoned_receipt)
        receipt_text = (
            Path(self.temporary.name)
            / "state"
            / "assignments"
            / f"{assignment_id}.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(reason, receipt_text)

    def test_unusual_activity_403_command_reports_and_blocks_fresh_prepare(self) -> None:
        assignment_id = "dispatch-403-7319"
        self.configure_and_prepare(assignment_id)
        reason_path = Path(self.temporary.name) / "403-reason.txt"
        reason_path.write_text(
            "Unusual activity has been detected from your device. Try again later.\n",
            encoding="utf-8",
        )

        blocked = self.run_cli(
            "unusual-activity",
            assignment_id,
            "--request-id",
            "d2740d8b-5006-4e4d-a78a-820b4abab4f8",
            "--reason-file",
            str(reason_path),
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        payload = json.loads(blocked.stdout)
        self.assertEqual(payload["native_http_status"], 403)
        self.assertTrue(payload["collect_only"])
        self.assertEqual(payload["cooldown"]["cooldown_seconds"], 1800)
        self.assertGreater(payload["cooldown"]["retry_after_seconds"], 0)

        abandoned = self.run_cli(
            "abandon",
            assignment_id,
            "--reason",
            "user authorized a fresh assignment",
        )
        self.assertEqual(abandoned.returncode, 0, abandoned.stderr)
        fresh = self.run_cli(
            "prepare",
            "--parent-task-id",
            "parent-7319",
            "--assignment-id",
            "dispatch-fresh-during-cooldown-7319",
            "--native-controls-confirmed",
            input_text="Fresh task",
        )
        self.assertEqual(fresh.returncode, 6)
        error = json.loads(fresh.stderr)
        self.assertEqual(error["error_type"], "CooldownError")
        self.assertEqual(error["details"]["native_http_status"], 403)
        self.assertEqual(error["details"]["cooldown_seconds"], 1800)

        status = self.run_cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = json.loads(status.stdout)
        self.assertIsNone(status_payload["active_assignment"])
        self.assertEqual(
            status_payload["active_cooldown"]["assignment_id"], assignment_id
        )

    def test_recover_reports_verified_outbound_state(self) -> None:
        assignment_id = "dispatch-recover-fields-7319"
        prepared = self.configure_and_prepare(assignment_id)
        submitted = self.run_cli(
            "submitted",
            assignment_id,
            "--sent-prompt-file",
            "-",
            input_text=str(prepared["wrapped_prompt"]),
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)

        recovered = self.run_cli("recover", assignment_id)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        recovery = json.loads(recovered.stdout)["recovery"]
        self.assertTrue(recovery["outbound_prompt_verified"])
        self.assertTrue(recovery["no_resend"])
        self.assertEqual(
            recovery["wrapped_prompt_sha256"], recovery["sent_prompt_sha256"]
        )

    def test_doctor_is_unhealthy_when_assignment_state_is_corrupt(self) -> None:
        self.configure_and_prepare("dispatch-doctor-corrupt-7319")
        assignments = Path(self.temporary.name) / "state" / "assignments"
        (assignments / "broken.json").write_text("not json", encoding="utf-8")

        doctor = self.run_cli("doctor")
        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        payload = json.loads(doctor.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("state_error", payload)

    def test_doctor_reports_and_persists_legacy_diagnostic_redaction(self) -> None:
        assignment_id = "dispatch-doctor-redaction-7319"
        self.configure_and_prepare(assignment_id)
        receipt_path = (
            Path(self.temporary.name)
            / "state"
            / "assignments"
            / f"{assignment_id}.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        sentinel = "LEGACY_PRIVATE_DIAGNOSTIC_7319"
        receipt["last_error"] = sentinel
        receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

        status = self.run_cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = json.loads(status.stdout)
        visible = status_payload["assignments"][0]
        self.assertNotIn("last_error", visible)
        self.assertEqual(visible["last_error_sha256"], sha256_text(sentinel))
        self.assertIn(sentinel, receipt_path.read_text(encoding="utf-8"))

        doctor = self.run_cli("doctor")
        self.assertEqual(doctor.returncode, 1, doctor.stderr)
        doctor_payload = json.loads(doctor.stdout)
        self.assertEqual(doctor_payload["redacted_diagnostic_receipts"], 1)
        stored = receipt_path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, stored)
        self.assertEqual(
            json.loads(stored)["last_error_sha256"],
            sha256_text(sentinel),
        )

    def test_doctor_fails_until_native_controls_are_confirmed(self) -> None:
        worker = self.run_cli(
            "worker",
            "set",
            "--conversation-id",
            "6a87c2b8-0a34-83e8-8409-27bc1f4fef5e",
            "--confirm-pro",
            "--native-controls-confirmed",
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)

        unconfirmed = self.run_cli("doctor")
        self.assertEqual(unconfirmed.returncode, 1)
        unconfirmed_payload = json.loads(unconfirmed.stdout)
        expected_local_ok = platform.system() == "Darwin"
        self.assertEqual(unconfirmed_payload["local_ok"], expected_local_ok)
        self.assertFalse(unconfirmed_payload["native_controls_confirmed"])
        self.assertFalse(unconfirmed_payload["ok"])

        confirmed = self.run_cli("doctor", "--native-controls-confirmed")
        self.assertEqual(confirmed.returncode, 0 if expected_local_ok else 1, confirmed.stderr)
        confirmed_payload = json.loads(confirmed.stdout)
        self.assertEqual(confirmed_payload["local_ok"], expected_local_ok)
        self.assertTrue(confirmed_payload["native_controls_confirmed"])
        self.assertEqual(confirmed_payload["ok"], expected_local_ok)

    def test_doctor_fails_closed_on_a_non_darwin_host(self) -> None:
        worker = self.run_cli(
            "worker",
            "set",
            "--conversation-id",
            "6a87c2b8-0a34-83e8-8409-27bc1f4fef5e",
            "--confirm-pro",
            "--native-controls-confirmed",
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)

        emitted: list[dict[str, object]] = []
        with (
            mock.patch.dict(
                os.environ,
                {"CODEX_PRO_DISPATCH_HOME": self.temporary.name},
            ),
            mock.patch.object(cli_module.platform, "system", return_value="Linux"),
            mock.patch.object(
                cli_module,
                "emit",
                side_effect=lambda payload, **_: emitted.append(payload),
            ),
        ):
            exit_code = cli_module.main(["doctor", "--native-controls-confirmed"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(emitted), 1)
        payload = emitted[0]
        self.assertFalse(payload["local_ok"])
        self.assertTrue(payload["native_controls_confirmed"])
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
