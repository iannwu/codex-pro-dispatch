from __future__ import annotations

import json
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

    def test_submission_is_recorded_exactly_once(self) -> None:
        prepared = self.prepare()
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

    def test_submission_mismatch_is_collect_only_and_cannot_be_retried(self) -> None:
        prepared = self.prepare()
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

    def test_indeterminate_state_blocks_resend_and_supports_recovery(self) -> None:
        prepared = self.prepare()
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

    def test_result_marker_must_be_first_nonempty_line(self) -> None:
        prepared = self.prepare()
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        with self.assertRaises(cpd.MarkerError):
            cpd.complete_assignment(
                prepared.assignment_id,
                "Preamble\n[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\nDone",
                self.paths,
            )

    def test_result_marker_rejects_surrounding_whitespace(self) -> None:
        prepared = self.prepare()
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        with self.assertRaises(cpd.MarkerError):
            cpd.complete_assignment(
                prepared.assignment_id,
                " [CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319] \nDone",
                self.paths,
            )

    def test_mismatched_result_marker_is_rejected(self) -> None:
        prepared = self.prepare()
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        with self.assertRaises(cpd.MarkerError):
            cpd.complete_assignment(
                prepared.assignment_id,
                "[CODEX_PRO_DISPATCH_RESULT assignment_id=wrong]\nDone",
                self.paths,
            )

    def test_complete_validates_marker_and_is_idempotent(self) -> None:
        prepared = self.prepare()
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        response = (
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\n\n"
            "commit_sha=abc123\nbranch=feature/test"
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

    def test_completed_receipt_cannot_be_rewritten(self) -> None:
        prepared = self.prepare()
        cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)
        cpd.complete_assignment(
            prepared.assignment_id,
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\nOriginal",
            self.paths,
        )
        with self.assertRaises(cpd.StateError):
            cpd.complete_assignment(
                prepared.assignment_id,
                "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\nLater",
                self.paths,
            )

    def test_continuation_uses_same_worker_after_completion(self) -> None:
        first = self.prepare()
        cpd.mark_submitted(first.assignment_id, first.wrapped_prompt, self.paths)
        cpd.complete_assignment(
            first.assignment_id,
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-test-7319]\nDone",
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


if __name__ == "__main__":
    unittest.main()
