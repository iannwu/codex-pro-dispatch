from __future__ import annotations

import json
import datetime as dt
import stat
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import codex_pro_dispatch as cpd


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = cpd.RuntimePaths(
            config_dir=root / "config",
            state_dir=root / "state",
        )
        self.worker_id = "6a87c2b8-0a34-83e8-8409-27bc1f4fef5e"
        self.parent_id = "parent-task-7319"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure_worker(self) -> cpd.WorkerConfig:
        return cpd.save_worker(
            self.worker_id,
            label="Official App Pro Worker",
            confirm_pro=True,
            paths=self.paths,
        )

    def prepare(self, *, assignment_id: str = "dispatch-test-7319") -> cpd.PreparedAssignment:
        self.configure_worker()
        return cpd.prepare_assignment(
            "Implement the approved repair.",
            parent_task_id=self.parent_id,
            assignment_id=assignment_id,
            paths=self.paths,
        )

    def arm(self, prepared: cpd.PreparedAssignment) -> dict[str, object]:
        return cpd.arm_assignment(prepared.assignment_id, self.paths)

    def bounded_response(self, assignment_id: str, body: str) -> str:
        return (
            f"[CODEX_PRO_DISPATCH_RESULT assignment_id={assignment_id}]\n"
            f"{body}\n"
            f"[CODEX_PRO_DISPATCH_END assignment_id={assignment_id}]"
        )

    def control_response(self, assignment_id: str, root_assignment_id: str | None = None) -> str:
        root = root_assignment_id or assignment_id
        return (
            f"[CODEX_PRO_DISPATCH_RESULT assignment_id={assignment_id}]\n"
            "[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED "
            f"root_assignment_id={root}]\n"
            f"[CODEX_PRO_DISPATCH_END assignment_id={assignment_id}]"
        )

    def chunk_response(
        self,
        assignment_id: str,
        root_assignment_id: str,
        index: str,
        final: str,
        body: str,
    ) -> str:
        return (
            f"[CODEX_PRO_DISPATCH_RESULT assignment_id={assignment_id}]\n"
            "[CODEX_PRO_DISPATCH_CHUNK "
            f"root_assignment_id={root_assignment_id} index={index} final={final}]\n"
            f"{body}\n"
            f"[CODEX_PRO_DISPATCH_END assignment_id={assignment_id}]"
        )

    def submit(self, prepared: cpd.PreparedAssignment) -> None:
        self.arm(prepared)
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)

    def test_worker_requires_explicit_pro_confirmation(self) -> None:
        with self.assertRaises(cpd.ConfigurationError):
            cpd.save_worker(
                self.worker_id,
                confirm_pro=False,
                paths=self.paths,
            )

    def test_worker_config_is_private(self) -> None:
        worker = self.configure_worker()
        self.assertEqual(worker.conversation_id, self.worker_id)
        self.assertEqual(stat.S_IMODE(self.paths.worker_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.config_dir.stat().st_mode), 0o700)
        self.assertEqual(cpd.load_worker(self.paths), worker)

    def test_identifier_rejects_path_traversal(self) -> None:
        self.configure_worker()
        for assignment_id in ["../escape", "/absolute", "bad space", ""]:
            with self.subTest(assignment_id=assignment_id):
                with self.assertRaises(cpd.ConfigurationError):
                    cpd.prepare_assignment(
                        "Task",
                        parent_task_id=self.parent_id,
                        assignment_id=assignment_id,
                        paths=self.paths,
                    )

    def test_prepare_records_hashes_not_prompt_text(self) -> None:
        prepared = self.prepare()
        value = cpd.load_assignment(prepared.assignment_id, self.paths)
        serialized = json.dumps(value)
        self.assertNotIn("Implement the approved repair", serialized)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(value["submission_count"], 0)
        self.assertIn("[CODEX_PRO_DISPATCH assignment_id=dispatch-test-7319]", prepared.wrapped_prompt)
        self.assertIn(
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]",
            prepared.wrapped_prompt,
        )

    def test_second_active_assignment_is_blocked(self) -> None:
        self.prepare()
        with self.assertRaises(cpd.BusyError):
            cpd.prepare_assignment(
                "Second task",
                parent_task_id=self.parent_id,
                assignment_id="dispatch-second-7319",
                paths=self.paths,
            )

    def test_one_verified_submission_cannot_be_recorded_twice(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        value = cpd.mark_submitted(
            prepared.assignment_id, prepared.wrapped_prompt, self.paths
        )
        self.assertEqual(value["status"], "submitted")
        self.assertEqual(value["submission_count"], 1)
        self.assertTrue(value["outbound_prompt_verified"])
        self.assertTrue(value["no_resend"])
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id, prepared.wrapped_prompt, self.paths
            )

    def test_stale_readback_preserves_receipt_then_current_message_verifies(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        before = cpd.load_assignment(prepared.assignment_id, self.paths)
        stale = prepared.wrapped_prompt.replace(prepared.assignment_id, "dispatch-older")
        for _ in range(2):
            with self.assertRaises(cpd.StateError) as caught:
                cpd.mark_submitted(prepared.assignment_id, stale, self.paths)
            self.assertIn("another assignment", str(caught.exception))
            self.assertEqual(cpd.load_assignment(prepared.assignment_id, self.paths), before)
        with self.assertRaises(cpd.StateError):
            self.arm(prepared)
        verified = cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        self.assertTrue(verified["outbound_prompt_verified"])
        self.assertTrue(verified["no_resend"])
        self.assertEqual(verified["submission_count"], 1)

    def test_submission_mismatch_is_collect_only_and_cannot_be_retried(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id, "+" + prepared.wrapped_prompt, self.paths
            )

        value = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(value["status"], "indeterminate")
        self.assertEqual(value["submission_count"], 1)
        self.assertFalse(value["outbound_prompt_verified"])
        self.assertTrue(value["no_resend"])
        self.assertNotEqual(
            value["sent_prompt_sha256"], value["wrapped_prompt_sha256"]
        )
        self.assertNotIn("Implement the approved repair", json.dumps(value))
        recovery = cpd.recovery_info(prepared.assignment_id, self.paths)
        self.assertEqual(recovery["status"], "indeterminate")
        self.assertTrue(recovery["no_resend"])
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id, prepared.wrapped_prompt, self.paths
            )

    def test_trailing_newline_readback_artifact_can_be_corrected_without_resend(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id, prepared.wrapped_prompt + "\n", self.paths
            )

        mismatched = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(mismatched["status"], "indeterminate")
        self.assertEqual(mismatched["submission_count"], 1)
        self.assertTrue(mismatched["no_resend"])
        self.assertTrue(mismatched["readback_correction_allowed"])

        corrected = cpd.mark_submitted(
            prepared.assignment_id, prepared.wrapped_prompt, self.paths
        )

        self.assertEqual(corrected["status"], "submitted")
        self.assertEqual(corrected["submission_count"], 1)
        self.assertTrue(corrected["outbound_prompt_verified"])
        self.assertTrue(corrected["no_resend"])
        self.assertEqual(corrected["readback_verification_attempt_count"], 2)
        self.assertEqual(corrected["submission_recovered_from"], "indeterminate")
        self.assertEqual(corrected["readback_correction_kind"], "single-trailing-newline")
        self.assertIn("readback_artifact_sha256", corrected)
        self.assertNotIn("readback_correction_allowed", corrected)
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id, prepared.wrapped_prompt, self.paths
            )

    def test_legacy_newline_mismatch_receipt_can_be_corrected_from_stored_hash(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id, prepared.wrapped_prompt + "\n", self.paths
            )

        legacy_receipt = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        legacy_receipt.pop("readback_correction_allowed", None)
        legacy_receipt.pop("readback_correction_kind", None)
        legacy_receipt.pop("readback_artifact_sha256", None)
        legacy_receipt.pop("readback_verification_attempt_count", None)
        prepared.receipt_path.write_text(
            json.dumps(legacy_receipt), encoding="utf-8"
        )

        corrected = cpd.mark_submitted(
            prepared.assignment_id, prepared.wrapped_prompt, self.paths
        )

        self.assertEqual(corrected["status"], "submitted")
        self.assertEqual(corrected["submission_count"], 1)
        self.assertTrue(corrected["outbound_prompt_verified"])
        self.assertEqual(corrected["readback_verification_attempt_count"], 2)
        self.assertEqual(corrected["readback_correction_kind"], "single-trailing-newline")
        self.assertEqual(corrected["submission_recovered_from"], "indeterminate")

    def test_indeterminate_state_blocks_resend_and_supports_recovery(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        value = cpd.mark_indeterminate(
            prepared.assignment_id,
            reason="native send returned after possible submission",
            paths=self.paths,
        )
        self.assertEqual(value["status"], "indeterminate")
        self.assertTrue(value["no_resend"])
        recovery = cpd.recovery_info(prepared.assignment_id, self.paths)
        self.assertTrue(recovery["no_resend"])
        self.assertEqual(recovery["worker_conversation_id"], self.worker_id)
        self.assertEqual(recovery["parent_task_id"], self.parent_id)

    def test_unusual_activity_403_reports_details_and_enforces_cooldown(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        reason = (
            "HTTP 403: Unusual activity has been detected from your device. "
            "Try again later."
        )

        value = cpd.mark_unusual_activity_403(
            prepared.assignment_id,
            reason=reason,
            request_id="d2740d8b-5006-4e4d-a78a-820b4abab4f8",
            paths=self.paths,
        )

        self.assertEqual(value["status"], "indeterminate")
        self.assertEqual(value["native_http_status"], 403)
        self.assertEqual(value["native_error_kind"], "openai-unusual-activity")
        self.assertEqual(value["cooldown_seconds"], 1800)
        self.assertTrue(value["no_resend"])
        cooldown = cpd.active_cooldown(self.paths)
        self.assertIsNotNone(cooldown)
        assert cooldown is not None
        self.assertEqual(cooldown["assignment_id"], prepared.assignment_id)
        self.assertGreater(cooldown["retry_after_seconds"], 0)
        recovery = cpd.recovery_info(prepared.assignment_id, self.paths)
        self.assertEqual(recovery["native_http_status"], 403)
        self.assertEqual(
            recovery["openai_request_id"],
            "d2740d8b-5006-4e4d-a78a-820b4abab4f8",
        )
        self.assertGreater(
            recovery["active_cooldown"]["retry_after_seconds"], 0
        )

        repeated = cpd.mark_unusual_activity_403(
            prepared.assignment_id,
            reason="a repeated observation must not restart the clock",
            request_id="different-request-id",
            paths=self.paths,
        )
        self.assertEqual(repeated["cooldown_until"], value["cooldown_until"])
        self.assertEqual(repeated["last_error_kind"], "openai-unusual-activity")
        self.assertEqual(repeated["last_error_sha256"], cpd.sha256_text(reason))
        self.assertNotIn("last_error", repeated)
        self.assertEqual(
            repeated["openai_request_id"],
            "d2740d8b-5006-4e4d-a78a-820b4abab4f8",
        )

        cpd.abandon_assignment(
            prepared.assignment_id,
            reason="user authorized a fresh assignment",
            paths=self.paths,
        )
        with self.assertRaises(cpd.CooldownError) as raised:
            cpd.prepare_assignment(
                "Fresh task",
                parent_task_id=self.parent_id,
                assignment_id="dispatch-cooldown-blocked-7319",
                paths=self.paths,
            )
        self.assertEqual(raised.exception.details["native_http_status"], 403)
        self.assertEqual(raised.exception.details["cooldown_seconds"], 1800)

        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        receipt["cooldown_until"] = "2000-01-01T00:00:00.000Z"
        prepared.receipt_path.write_text(
            json.dumps(receipt) + "\n", encoding="utf-8"
        )
        fresh = cpd.prepare_assignment(
            "Fresh task",
            parent_task_id=self.parent_id,
            assignment_id="dispatch-after-cooldown-7319",
            paths=self.paths,
        )
        self.assertEqual(fresh.assignment_id, "dispatch-after-cooldown-7319")

    def test_legacy_raw_diagnostics_are_redacted_durably(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        receipt = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        sentinel = "UNIQUE_PRIVATE_ERROR_BODY_7f4c489e"
        receipt["last_error"] = sentinel
        receipt["reason"] = "private abandon explanation"
        prepared.receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

        visible = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertNotIn("last_error", visible)
        self.assertNotIn("reason", visible)
        self.assertIn(sentinel, prepared.receipt_path.read_text(encoding="utf-8"))

        self.assertEqual(cpd.redact_stored_diagnostics(self.paths), 1)
        stored = prepared.receipt_path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, stored)
        self.assertNotIn("private abandon explanation", stored)
        migrated = json.loads(stored)
        self.assertEqual(
            migrated["last_error_sha256"], cpd.sha256_text(sentinel)
        )
        self.assertEqual(
            migrated["abandon_reason_kind"], "legacy-abandon-reason-redacted"
        )

    def test_empty_legacy_diagnostics_are_removed_without_fabricated_metadata(self) -> None:
        prepared = self.prepare()
        receipt = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        receipt["last_error"] = " \n "
        receipt["reason"] = ""
        prepared.receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

        self.assertEqual(cpd.redact_stored_diagnostics(self.paths), 1)
        migrated = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        for field in (
            "last_error",
            "last_error_kind",
            "last_error_sha256",
            "reason",
            "abandon_reason_kind",
            "abandon_reason_sha256",
        ):
            self.assertNotIn(field, migrated)

    def test_legacy_redaction_preserves_existing_structured_metadata(self) -> None:
        prepared = self.prepare()
        receipt = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        receipt.update(
            {
                "last_error": "legacy raw error",
                "last_error_kind": "specific-native-error",
                "last_error_sha256": "existing-error-hash",
                "reason": "legacy raw abandon reason",
                "abandon_reason_kind": "specific-user-reason",
                "abandon_reason_sha256": "existing-reason-hash",
            }
        )
        prepared.receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

        self.assertEqual(cpd.redact_stored_diagnostics(self.paths), 1)
        migrated = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        self.assertNotIn("last_error", migrated)
        self.assertNotIn("reason", migrated)
        self.assertEqual(migrated["last_error_kind"], "specific-native-error")
        self.assertEqual(migrated["last_error_sha256"], "existing-error-hash")
        self.assertEqual(migrated["abandon_reason_kind"], "specific-user-reason")
        self.assertEqual(migrated["abandon_reason_sha256"], "existing-reason-hash")

    def test_unusual_activity_cooldown_calculation_accepts_explicit_time(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        value = cpd.mark_unusual_activity_403(
            prepared.assignment_id,
            reason="HTTP 403 unusual activity",
            paths=self.paths,
        )
        started = dt.datetime.fromisoformat(
            value["cooldown_started_at"].replace("Z", "+00:00")
        )
        cooldown = cpd.active_cooldown(
            self.paths, now=started + dt.timedelta(minutes=29)
        )
        self.assertIsNotNone(cooldown)
        assert cooldown is not None
        self.assertEqual(cooldown["retry_after_seconds"], 60)
        self.assertIsNone(
            cpd.active_cooldown(
                self.paths, now=started + dt.timedelta(minutes=30)
            )
        )

    def test_late_readback_verifies_indeterminate_submission_without_resend(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        cpd.mark_indeterminate(
            prepared.assignment_id,
            reason="native read-back was temporarily stale",
            paths=self.paths,
        )

        value = cpd.mark_submitted(
            prepared.assignment_id, prepared.wrapped_prompt, self.paths
        )

        self.assertEqual(value["status"], "submitted")
        self.assertEqual(value["submission_count"], 1)
        self.assertTrue(value["outbound_prompt_verified"])
        self.assertTrue(value["no_resend"])
        self.assertEqual(value["submission_recovered_from"], "indeterminate")
        self.assertNotIn("last_error", value)
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id, prepared.wrapped_prompt, self.paths
            )

    def test_late_readback_verifies_ambiguous_submission_without_resend(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        cpd.mark_indeterminate(
            prepared.assignment_id,
            reason="native read-back was temporarily stale",
            paths=self.paths,
        )
        cpd.mark_ambiguous(
            prepared.assignment_id,
            reason="collection completed before native read-back converged",
            paths=self.paths,
        )

        value = cpd.mark_submitted(
            prepared.assignment_id, prepared.wrapped_prompt, self.paths
        )

        self.assertEqual(value["status"], "submitted")
        self.assertEqual(value["submission_count"], 1)
        self.assertTrue(value["outbound_prompt_verified"])
        self.assertEqual(value["submission_recovered_from"], "ambiguous")
        self.assertNotIn("last_error", value)

    def test_result_marker_must_be_first_nonempty_line(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        with self.assertRaises(cpd.MarkerError):
            cpd.complete_assignment(
                prepared.assignment_id,
                "Preamble\n[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\nDone",
                self.paths,
            )

    def test_result_marker_rejects_surrounding_whitespace(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        with self.assertRaises(cpd.MarkerError):
            cpd.complete_assignment(
                prepared.assignment_id,
                " [CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319] \nDone",
                self.paths,
            )

    def test_mismatched_result_marker_is_rejected(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        with self.assertRaises(cpd.MarkerError):
            cpd.complete_assignment(
                prepared.assignment_id,
                "[CODEX_PRO_DISPATCH_RESULT assignment_id=wrong]\nDone",
                self.paths,
            )

    def test_complete_validates_marker_and_is_idempotent(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        response = self.bounded_response(
            "dispatch-test-7319", "commit_sha=abc123\nbranch=feature/test"
        )
        value, payload = cpd.complete_assignment(
            prepared.assignment_id, response, self.paths
        )
        self.assertEqual(value["status"], "complete")
        self.assertEqual(payload, "commit_sha=abc123\nbranch=feature/test")
        second, second_payload = cpd.complete_assignment(
            prepared.assignment_id, response, self.paths
        )
        self.assertEqual(second["response_sha256"], value["response_sha256"])
        self.assertEqual(second_payload, payload)

    def test_prepare_marks_new_receipts_with_the_bounded_footer_protocol(self) -> None:
        prepared = self.prepare()
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["result_protocol"], "bounded-footer-v1")
        self.assertIn(
            "[CODEX_PRO_DISPATCH_END assignment_id=dispatch-test-7319]",
            prepared.wrapped_prompt,
        )

    def test_wrap_prompt_uses_mutually_exclusive_initial_and_continuation_forms(self) -> None:
        initial = cpd.wrap_prompt("Inspect the code.", "dispatch-initial-7319")
        self.assertIn("CONTINUATION_REQUIRED", initial)
        self.assertNotIn("CODEX_PRO_DISPATCH_CHUNK", initial)
        self.assertIn("Aim to keep the entire assistant response below 10000 UTF-8 bytes", initial)

        continuation_body = (
            "[CODEX_PRO_DISPATCH_CONTINUE root_assignment_id=dispatch-root-7319 "
            "next_index=1]\n\nReturn only the next body."
        )
        continuation = cpd.wrap_prompt(continuation_body, "dispatch-chunk-7319")
        self.assertIn("CODEX_PRO_DISPATCH_CHUNK", continuation)
        self.assertNotIn("CONTINUATION_REQUIRED", continuation)
        self.assertIn("root_assignment_id=dispatch-root-7319 index=1", continuation)

        leading_byte = cpd.wrap_prompt("\n" + continuation_body, "dispatch-leading-7319")
        self.assertIn("CONTINUATION_REQUIRED", leading_byte)
        self.assertNotIn("CODEX_PRO_DISPATCH_CHUNK", leading_byte)

    def test_raw_envelope_requires_byte_zero_footer_utf8_and_lf_only(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        valid = self.bounded_response(prepared.assignment_id, "OK")
        invalid_responses: list[str | bytes] = [
            "x" + valid,
            valid + "x",
            valid.replace("\nOK\n", "\r\nOK\n"),
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\nOK",
            b"[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\n\xff\n"
            b"[CODEX_PRO_DISPATCH_END assignment_id=dispatch-test-7319]",
        ]
        for response in invalid_responses:
            with self.subTest(response_type=type(response).__name__):
                with self.assertRaises(cpd.MarkerError):
                    cpd.complete_assignment(prepared.assignment_id, response, self.paths)
        self.assertEqual(
            cpd.load_assignment(prepared.assignment_id, self.paths)["status"], "submitted"
        )

    def test_raw_body_is_sliced_without_normalization_or_marker_scanning(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        body = (
            "\nfirst\n"
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=another-assignment]\n"
            "[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED root_assignment_id=example]\n"
            "[CODEX_PRO_DISPATCH_CHUNK root_assignment_id=example index=1 final=0]\n"
            "[CODEX_PRO_DISPATCH_END assignment_id=another-assignment]\n"
        )
        value, payload = cpd.complete_assignment(
            prepared.assignment_id,
            self.bounded_response(prepared.assignment_id, body),
            self.paths,
        )
        self.assertNotIn("result_kind", value)
        self.assertEqual(payload, body)

    def test_response_above_size_guideline_completes_when_envelope_is_intact(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        marker = f"[CODEX_PRO_DISPATCH_RESULT assignment_id={prepared.assignment_id}]\n"
        footer = f"\n[CODEX_PRO_DISPATCH_END assignment_id={prepared.assignment_id}]"
        body = "x" * (10_291 - len(marker.encode("utf-8")) - len(footer.encode("utf-8")))
        response = marker + body + footer
        self.assertEqual(len(response.encode("utf-8")), 10_291)
        value, payload = cpd.complete_assignment(prepared.assignment_id, response, self.paths)
        self.assertEqual(value["status"], "complete")
        self.assertEqual(payload, body)

    def test_explicit_native_truncation_is_rejected_without_completion(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        with self.assertRaisesRegex(cpd.MarkerError, "truncated-response"):
            cpd.complete_assignment(
                prepared.assignment_id,
                self.bounded_response(prepared.assignment_id, "OK"),
                self.paths,
                truncated=True,
            )
        self.assertEqual(
            cpd.load_assignment(prepared.assignment_id, self.paths)["status"], "submitted"
        )

    def test_control_response_is_exact_and_only_reserved_first_line_is_interpreted(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        value, payload = cpd.complete_assignment(
            prepared.assignment_id,
            self.control_response(prepared.assignment_id),
            self.paths,
        )
        self.assertNotIn("result_kind", value)
        self.assertEqual(payload, "")

        malformed = cpd.prepare_assignment(
            "Try control parsing.",
            parent_task_id=self.parent_id,
            assignment_id="dispatch-control-invalid-7319",
            paths=self.paths,
        )
        self.submit(malformed)
        with self.assertRaisesRegex(cpd.MarkerError, "control-root-mismatch"):
            cpd.complete_assignment(
                malformed.assignment_id,
                self.control_response(malformed.assignment_id, "wrong-root-7319"),
                self.paths,
            )
        with self.assertRaisesRegex(cpd.MarkerError, "continuation-required-control-invalid"):
            cpd.complete_assignment(
                malformed.assignment_id,
                self.bounded_response(
                    malformed.assignment_id,
                    "[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED "
                    f"root_assignment_id={malformed.assignment_id}]\nextra",
                ),
                self.paths,
            )
        opaque = self.bounded_response(
            malformed.assignment_id,
            "\n[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED "
            f"root_assignment_id={malformed.assignment_id}]",
        )
        value, payload = cpd.complete_assignment(malformed.assignment_id, opaque, self.paths)
        self.assertNotIn("result_kind", value)
        self.assertTrue(payload.startswith("\n[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED"))

    def test_chunk_mode_requires_paired_expectations_and_exact_matching_header(self) -> None:
        prepared = self.prepare(assignment_id="dispatch-chunk-current-7319")
        self.submit(prepared)
        root = "dispatch-root-7319"
        response = self.chunk_response(prepared.assignment_id, root, "1", "0", "first")
        with self.assertRaises(cpd.ConfigurationError):
            cpd.complete_assignment(
                prepared.assignment_id,
                response,
                self.paths,
                expected_root_assignment_id=root,
            )
        with self.assertRaisesRegex(cpd.MarkerError, "chunk-arguments-required"):
            cpd.complete_assignment(prepared.assignment_id, response, self.paths)
        with self.assertRaisesRegex(cpd.MarkerError, "chunk-envelope-required"):
            cpd.complete_assignment(
                prepared.assignment_id,
                self.bounded_response(prepared.assignment_id, "short"),
                self.paths,
                expected_root_assignment_id=root,
                expected_chunk_index="1",
            )
        with self.assertRaisesRegex(cpd.MarkerError, "chunk-root-mismatch"):
            cpd.complete_assignment(
                prepared.assignment_id,
                self.chunk_response(prepared.assignment_id, "wrong-root-7319", "1", "0", "first"),
                self.paths,
                expected_root_assignment_id=root,
                expected_chunk_index="1",
            )
        with self.assertRaisesRegex(cpd.MarkerError, "chunk-index-invalid"):
            cpd.complete_assignment(
                prepared.assignment_id,
                self.chunk_response(prepared.assignment_id, root, "01", "0", "first"),
                self.paths,
                expected_root_assignment_id=root,
                expected_chunk_index="1",
            )
        with self.assertRaisesRegex(cpd.MarkerError, "chunk-index-mismatch"):
            cpd.complete_assignment(
                prepared.assignment_id,
                self.chunk_response(prepared.assignment_id, root, "2", "0", "first"),
                self.paths,
                expected_root_assignment_id=root,
                expected_chunk_index="1",
            )
        with self.assertRaisesRegex(cpd.MarkerError, "chunk-final-invalid"):
            cpd.complete_assignment(
                prepared.assignment_id,
                self.chunk_response(prepared.assignment_id, root, "1", "2", "first"),
                self.paths,
                expected_root_assignment_id=root,
                expected_chunk_index="1",
            )
        value, payload = cpd.complete_assignment(
            prepared.assignment_id,
            response,
            self.paths,
            expected_root_assignment_id=root,
            expected_chunk_index="1",
        )
        self.assertNotIn("result_kind", value)
        self.assertNotIn("result_root_assignment_id", value)
        self.assertNotIn("chunk_index", value)
        self.assertNotIn("final", value)
        self.assertEqual(payload, "first")

    def test_chunk_empty_rules_and_idempotency_are_fail_closed(self) -> None:
        prepared = self.prepare(assignment_id="dispatch-chunk-empty-7319")
        self.submit(prepared)
        root = "dispatch-root-7319"
        for final in ("0", "1"):
            with self.subTest(final=final):
                with self.assertRaisesRegex(cpd.MarkerError, "chunk-body-empty"):
                    cpd.complete_assignment(
                        prepared.assignment_id,
                        self.chunk_response(prepared.assignment_id, root, "1", final, ""),
                        self.paths,
                        expected_root_assignment_id=root,
                        expected_chunk_index="1",
                    )

        response = self.chunk_response(prepared.assignment_id, root, "1", "0", "first")
        first, payload = cpd.complete_assignment(
            prepared.assignment_id,
            response,
            self.paths,
            expected_root_assignment_id=root,
            expected_chunk_index="1",
        )
        repeated, repeated_payload = cpd.complete_assignment(
            prepared.assignment_id,
            response,
            self.paths,
            expected_root_assignment_id=root,
            expected_chunk_index="1",
        )
        self.assertEqual(repeated["response_sha256"], first["response_sha256"])
        self.assertEqual(repeated_payload, payload)
        with self.assertRaises(cpd.StateError):
            cpd.complete_assignment(
                prepared.assignment_id,
                self.chunk_response(prepared.assignment_id, root, "1", "1", "changed"),
                self.paths,
                expected_root_assignment_id=root,
                expected_chunk_index="1",
            )

        later = cpd.prepare_assignment(
            "Second chunk.",
            parent_task_id=self.parent_id,
            continuation_of=prepared.assignment_id,
            assignment_id="dispatch-empty-final-7319",
            paths=self.paths,
        )
        self.submit(later)
        value, empty_payload = cpd.complete_assignment(
            later.assignment_id,
            self.chunk_response(later.assignment_id, root, "2", "1", ""),
            self.paths,
            expected_root_assignment_id=root,
            expected_chunk_index="2",
        )
        self.assertNotIn("final", value)
        self.assertEqual(empty_payload, "")

    def test_active_legacy_receipt_is_inspect_recover_or_abandon_only(self) -> None:
        prepared = self.prepare()
        receipt = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        receipt.pop("result_protocol")
        prepared.receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        self.assertEqual(cpd.load_assignment(prepared.assignment_id, self.paths)["status"], "prepared")
        self.assertEqual(cpd.recovery_info(prepared.assignment_id, self.paths)["status"], "prepared")
        with self.assertRaisesRegex(cpd.StateError, "legacy-active-assignment"):
            self.arm(prepared)
        with self.assertRaisesRegex(cpd.StateError, "legacy-active-assignment"):
            cpd.prepare_assignment(
                "Blocked by legacy receipt.",
                parent_task_id=self.parent_id,
                assignment_id="dispatch-legacy-successor-7319",
                paths=self.paths,
            )
        abandoned = cpd.abandon_assignment(
            prepared.assignment_id,
            reason="operator is finishing the old receipt with v1.1",
            paths=self.paths,
        )
        self.assertEqual(abandoned["status"], "abandoned")

    def test_completed_receipt_cannot_be_rewritten(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        cpd.complete_assignment(
            prepared.assignment_id,
            self.bounded_response("dispatch-test-7319", "Original"),
            self.paths,
        )
        with self.assertRaises(cpd.StateError):
            cpd.complete_assignment(
                prepared.assignment_id,
                self.bounded_response("dispatch-test-7319", "Later"),
                self.paths,
            )

    def test_continuation_uses_same_worker_after_completion(self) -> None:
        first = self.prepare()
        self.arm(first)
        cpd.mark_submitted(first.assignment_id, first.wrapped_prompt, self.paths)
        cpd.complete_assignment(
            first.assignment_id,
            self.bounded_response("dispatch-test-7319", "Done"),
            self.paths,
        )
        second = cpd.prepare_assignment(
            "Repair the failing test.",
            parent_task_id=self.parent_id,
            continuation_of=first.assignment_id,
            assignment_id="dispatch-followup-7319",
            paths=self.paths,
        )
        self.assertEqual(second.worker_conversation_id, self.worker_id)
        value = cpd.load_assignment(second.assignment_id, self.paths)
        self.assertEqual(value["continuation_of"], first.assignment_id)

    def test_abandon_clears_active_slot(self) -> None:
        first = self.prepare()
        cpd.abandon_assignment(
            first.assignment_id,
            reason="user cancelled",
            paths=self.paths,
        )
        second = cpd.prepare_assignment(
            "New task",
            parent_task_id=self.parent_id,
            assignment_id="dispatch-new-7319",
            paths=self.paths,
        )
        self.assertEqual(second.assignment_id, "dispatch-new-7319")

    def test_worker_cannot_be_replaced_while_assignment_is_active(self) -> None:
        self.prepare()
        with self.assertRaises(cpd.BusyError):
            cpd.save_worker(
                "different-worker-7319",
                confirm_pro=True,
                paths=self.paths,
            )

    def test_corrupt_receipt_fails_closed(self) -> None:
        self.configure_worker()
        self.paths.assignments_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.assignments_dir / "broken.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(cpd.StateError):
            cpd.prepare_assignment(
                "Task",
                parent_task_id=self.parent_id,
                assignment_id="dispatch-safe-7319",
                paths=self.paths,
            )

    def test_reset_worker_refuses_unresolved_assignment(self) -> None:
        self.prepare()
        with self.assertRaises(cpd.BusyError):
            cpd.reset_worker(paths=self.paths)
        self.assertTrue(cpd.reset_worker(force=True, paths=self.paths))

    def test_purge_refuses_unresolved_assignment_without_force(self) -> None:
        self.prepare()
        with self.assertRaises(cpd.BusyError):
            cpd.purge_local_state(paths=self.paths)
        result = cpd.purge_local_state(force=True, paths=self.paths)
        self.assertTrue(result["worker_removed"])
        self.assertTrue(result["assignments_removed"])

    def test_armed_state_closes_crash_window_and_prohibits_resend(self) -> None:
        prepared = self.prepare()
        before = cpd.recovery_info(prepared.assignment_id, self.paths)
        self.assertFalse(before["no_resend"])

        armed = self.arm(prepared)
        self.assertEqual(armed["status"], "armed")
        self.assertTrue(armed["no_resend"])
        after = cpd.recovery_info(prepared.assignment_id, self.paths)
        self.assertTrue(after["no_resend"])
        self.assertFalse(after["outbound_prompt_verified"])
        with self.assertRaises(cpd.StateError):
            self.arm(prepared)

    def test_submission_requires_durable_arm(self) -> None:
        prepared = self.prepare()
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id, prepared.wrapped_prompt, self.paths
            )
        value = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(value["status"], "prepared")
        self.assertEqual(value["submission_count"], 0)

        with self.assertRaises(cpd.StateError):
            cpd.mark_indeterminate(
                prepared.assignment_id,
                reason="attempted arming bypass",
                paths=self.paths,
            )
        with self.assertRaises(cpd.StateError):
            cpd.mark_ambiguous(
                prepared.assignment_id,
                reason="attempted arming bypass",
                paths=self.paths,
            )
        with self.assertRaises(cpd.StateError):
            cpd.mark_unusual_activity_403(
                prepared.assignment_id,
                reason="attempted arming bypass",
                paths=self.paths,
            )
        value = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(value["status"], "prepared")

    def test_complete_requires_one_verified_submission(self) -> None:
        prepared = self.prepare()
        response = (
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\nDone\n"
            "[CODEX_PRO_DISPATCH_END assignment_id=dispatch-test-7319]"
        )
        with self.assertRaises(cpd.StateError):
            cpd.complete_assignment(prepared.assignment_id, response, self.paths)
        self.arm(prepared)
        with self.assertRaises(cpd.StateError):
            cpd.complete_assignment(prepared.assignment_id, response, self.paths)
        cpd.mark_indeterminate(
            prepared.assignment_id,
            reason="native send outcome is unknown",
            paths=self.paths,
        )
        with self.assertRaises(cpd.StateError):
            cpd.complete_assignment(prepared.assignment_id, response, self.paths)
        value = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(value["status"], "indeterminate")
        self.assertEqual(value["submission_count"], 0)

    def test_recovery_exposes_outbound_verification_fields(self) -> None:
        prepared = self.prepare()
        self.arm(prepared)
        submitted = cpd.mark_submitted(
            prepared.assignment_id, prepared.wrapped_prompt, self.paths
        )
        recovery = cpd.recovery_info(prepared.assignment_id, self.paths)
        self.assertTrue(recovery["outbound_prompt_verified"])
        self.assertEqual(
            recovery["wrapped_prompt_sha256"], submitted["wrapped_prompt_sha256"]
        )
        self.assertEqual(
            recovery["sent_prompt_sha256"], submitted["sent_prompt_sha256"]
        )
        self.assertFalse(recovery["readback_correction_allowed"])

    def test_force_repairs_corrupt_state(self) -> None:
        self.configure_worker()
        self.paths.assignments_dir.mkdir(parents=True, exist_ok=True)
        broken = self.paths.assignments_dir / "broken.json"
        broken.write_text("not json", encoding="utf-8")

        with self.assertRaises(cpd.StateError):
            cpd.reset_worker(paths=self.paths)
        self.assertTrue(cpd.reset_worker(force=True, paths=self.paths))

        with self.assertRaises(cpd.StateError):
            cpd.purge_local_state(paths=self.paths)
        result = cpd.purge_local_state(force=True, paths=self.paths)
        self.assertFalse(result["worker_removed"])
        self.assertTrue(result["assignments_removed"])
        self.assertFalse(broken.exists())


if __name__ == "__main__":
    unittest.main()
