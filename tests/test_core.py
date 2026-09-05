from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
import datetime as dt
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import codex_pro_dispatch as cpd
from codex_pro_dispatch.chunked import CHAIN_ZERO_HEX
from codex_pro_dispatch import transport


class DispatchV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = cpd.RuntimePaths(root / "config", root / "state")
        self.worker_id = "worker-7319"
        self.parent_id = "parent-7319"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure(self) -> None:
        if self.paths.worker_file.exists():
            cpd.load_worker(self.paths)
            return
        cpd.save_worker(self.worker_id, confirm_pro=True, paths=self.paths)

    def prepare(
        self, *, assignment_id: str = "dispatch-7319", mode: str = "inline"
    ) -> cpd.PreparedAssignment:
        self.configure()
        return cpd.prepare_assignment(
            "Implement the approved repair.",
            parent_task_id=self.parent_id,
            assignment_id=assignment_id,
            result_mode=mode,
            paths=self.paths,
        )

    def submit(
        self, prepared: cpd.PreparedAssignment, *, user_message_id: str = "user-7319"
    ) -> None:
        cpd.arm_assignment(
            prepared.assignment_id, self.paths, turn_id=prepared.turn_id
        )
        cpd.mark_submitted(
            prepared.assignment_id,
            prepared.wrapped_prompt,
            self.paths,
            turn_id=prepared.turn_id,
            native_user_message_id=user_message_id,
        )

    def evidence(
        self,
        *,
        turn_id: str,
        user_message_id: str,
        text: str,
        assistant_message_id: str | None = None,
        truncated: bool | None = False,
        outer_truncated: bool | None = False,
        observed_at: str = "2030-01-01T00:00:00.000Z",
    ) -> cpd.NativeCollectionEvidence:
        outer: dict[str, object] = {"provenance": "native-result-envelope"}
        if outer_truncated is not None:
            outer["truncated"] = outer_truncated
        value: dict[str, object] = {
            "schema": cpd.NATIVE_COLLECTION_SCHEMA,
            "adapter_contract_id": "codex-desktop-native-collection/v1",
            "requested_conversation_id": self.worker_id,
            "loaded_conversation_id": self.worker_id,
            "assistant_message_id": assistant_message_id or f"assistant-{turn_id}",
            "submitted_user_message_id": user_message_id,
            "role": "assistant",
            "generation_status": "completed",
            "generation_finality_provenance": "native-message-status",
            "selected_result_outer_integrity": outer,
            "text": text,
            "observed_at": observed_at,
        }
        if truncated is not None:
            value["truncated"] = truncated
        return cpd.NativeCollectionEvidence.from_mapping(value)

    def test_v2_receipt_is_body_free_and_explicit_mode_only(self) -> None:
        prepared = self.prepare()
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["schema_version"], cpd.ASSIGNMENT_SCHEMA_VERSION)
        self.assertEqual(receipt["record_type"], "dispatch")
        self.assertEqual(receipt["requested_result_mode"], "inline")
        self.assertEqual(receipt["effective_result_mode"], "inline")
        self.assertEqual(receipt["turns"][0]["status"], "prepared")
        serialized = json.dumps(receipt)
        self.assertNotIn("Implement the approved repair.", serialized)
        self.assertNotIn(prepared.wrapped_prompt, serialized)
        self.assertEqual(stat.S_IMODE(prepared.receipt_path.stat().st_mode), 0o600)
        with self.assertRaises(cpd.ConfigurationError):
            cpd.prepare_assignment(
                "x",
                parent_task_id=self.parent_id,
                assignment_id="dispatch-auto-7319",
                result_mode="auto",
                paths=self.paths,
            )

    def test_each_turn_is_armed_and_submitted_once(self) -> None:
        prepared = self.prepare()
        armed = cpd.arm_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(armed["status"], "active")
        self.assertEqual(armed["turns"][0]["status"], "armed")
        self.assertTrue(armed["no_original_resend"])
        submitted = cpd.mark_submitted(
            prepared.assignment_id,
            prepared.wrapped_prompt,
            self.paths,
            native_user_message_id="user-7319",
        )
        turn = submitted["turns"][0]
        self.assertEqual(turn["status"], "submitted")
        self.assertEqual(turn["submission_count"], 1)
        self.assertTrue(turn["outbound_prompt_verified"])
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id,
                prepared.wrapped_prompt,
                self.paths,
                native_user_message_id="user-7319",
            )

    def test_unverified_send_cannot_create_a_recovery_turn(self) -> None:
        prepared = self.prepare()
        cpd.arm_assignment(prepared.assignment_id, self.paths)
        cpd.mark_indeterminate(
            prepared.assignment_id,
            reason="native send outcome was not visible",
            paths=self.paths,
        )
        with self.assertRaises(cpd.CollectionEvidenceError) as raised:
            cpd.collect_turn(
                prepared.assignment_id,
                prepared.turn_id or prepared.assignment_id,
                self.evidence(
                    turn_id=prepared.turn_id or prepared.assignment_id,
                    user_message_id="user-7319",
                    text=f"{cpd.result_marker(prepared.turn_id or prepared.assignment_id)}\nbody",
                ),
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "collection_submission_mismatch")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(len(receipt["turns"]), 1)
        self.assertEqual(receipt["turns"][0]["status"], "indeterminate")

    def test_uncertain_verified_turn_cannot_prepare_a_recovery_successor(self) -> None:
        """A later ambiguous observation must not restore recovery-send authority."""

        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        cpd.mark_ambiguous(
            prepared.assignment_id,
            reason="native response association became ambiguous",
            paths=self.paths,
        )
        with self.assertRaises(cpd.StateError) as raised:
            cpd.collect_turn(
                prepared.assignment_id,
                turn_id,
                self.evidence(
                    turn_id=turn_id,
                    user_message_id="user-7319",
                    text=f"{cpd.result_marker(turn_id)}\\nprefix",
                    truncated=True,
                    outer_truncated=True,
                ),
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "recovery_predecessor_not_submitted")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["turns"][0]["status"], "ambiguous")
        self.assertEqual(receipt["turns"][0]["recovery_authority"], "collect-only")
        self.assertEqual(len(receipt["turns"]), 1)

    def test_persisted_ambiguous_state_cannot_be_tampered_back_to_submitted(self) -> None:
        """A status-only receipt edit cannot restore child-send authority."""

        prepared = self.prepare()
        self.submit(prepared)
        cpd.mark_ambiguous(
            prepared.assignment_id,
            reason="native response association became ambiguous",
            paths=self.paths,
        )
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        raw["turns"][0]["status"] = "submitted"
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(cpd.ReceiptMigrationError):
            cpd.load_assignment(prepared.assignment_id, self.paths)

    def test_verified_submission_requires_exact_native_user_message_id(self) -> None:
        prepared = self.prepare()
        cpd.arm_assignment(prepared.assignment_id, self.paths)
        with self.assertRaises(cpd.StateError) as raised:
            cpd.mark_submitted(
                prepared.assignment_id,
                prepared.wrapped_prompt,
                self.paths,
            )
        self.assertEqual(raised.exception.error_code, "native_user_message_id_required")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        turn = receipt["turns"][0]
        self.assertEqual(receipt["status"], "recoverable")
        self.assertEqual(turn["status"], "indeterminate")
        self.assertEqual(turn["submission_count"], 1)
        self.assertTrue(turn["no_resend"])
        self.assertFalse(turn["outbound_prompt_verified"])
        self.assertNotIn("native_user_message_id", turn)

    def test_tampered_verified_submission_without_native_id_fails_closed(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        raw["turns"][0].pop("native_user_message_id")
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(cpd.ReceiptMigrationError):
            cpd.load_assignment(prepared.assignment_id, self.paths)

    def test_inline_collection_is_idempotent_across_observation_times(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        response = f"{cpd.result_marker(turn_id)}\nComplete result"
        first = self.evidence(
            turn_id=turn_id,
            user_message_id="user-7319",
            text=response,
            observed_at="2030-01-01T00:00:00.000Z",
        )
        output = Path(self.temporary.name) / "inline.md"
        outcome = cpd.collect_turn(
            prepared.assignment_id, turn_id, first, output, paths=self.paths
        )
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(output.read_text(encoding="utf-8"), "Complete result")
        second = self.evidence(
            turn_id=turn_id,
            user_message_id="user-7319",
            text=response,
            observed_at="2031-01-01T00:00:00.000Z",
        )
        replay = cpd.collect_turn(prepared.assignment_id, turn_id, second, paths=self.paths)
        self.assertEqual(replay.status, "complete")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["result"]["completion_basis"], "native-inline")
        self.assertEqual(receipt["delivery"]["status"], "materialized")
        self.assertNotIn("Complete result", json.dumps(receipt))

    def test_changed_accepted_message_is_an_immutable_conflict(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        response = f"{cpd.result_marker(turn_id)}\nfirst"
        cpd.collect_turn(
            prepared.assignment_id,
            turn_id,
            self.evidence(turn_id=turn_id, user_message_id="user-7319", text=response),
            paths=self.paths,
        )
        with self.assertRaises(cpd.CollectionEvidenceError) as raised:
            cpd.collect_turn(
                prepared.assignment_id,
                turn_id,
                self.evidence(
                    turn_id=turn_id,
                    user_message_id="user-7319",
                    text=f"{cpd.result_marker(turn_id)}\nchanged",
                ),
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "collection_message_conflict")

    def test_omitted_truncation_is_fail_closed(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        with self.assertRaises(cpd.CollectionEvidenceError) as raised:
            cpd.collect_turn(
                prepared.assignment_id,
                turn_id,
                self.evidence(
                    turn_id=turn_id,
                    user_message_id="user-7319",
                    text=f"{cpd.result_marker(turn_id)}\nbody",
                    truncated=None,
                    outer_truncated=None,
                ),
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "collection_truncation_unknown")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["status"], "recoverable")
        self.assertEqual(receipt["turns"][0]["collection"]["raw_truncated"], "omitted")
        self.assertEqual(receipt["turns"][0]["collection"]["normalized_truncated"], None)
        self.assertEqual(len(receipt["turns"]), 1)

    def test_unknown_observation_can_upgrade_only_on_a_trusted_complete_reread(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        with self.assertRaises(cpd.CollectionEvidenceError) as raised:
            cpd.collect_turn(
                prepared.assignment_id,
                turn_id,
                self.evidence(
                    turn_id=turn_id,
                    user_message_id="user-7319",
                    text=f"{cpd.result_marker(turn_id)}\nprefix",
                    truncated=None,
                    outer_truncated=None,
                ),
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "collection_truncation_unknown")
        outcome = cpd.collect_turn(
            prepared.assignment_id,
            turn_id,
            self.evidence(
                turn_id=turn_id,
                user_message_id="user-7319",
                text=f"{cpd.result_marker(turn_id)}\ncomplete reread",
                observed_at="2031-01-01T00:00:00.000Z",
            ),
            paths=self.paths,
        )
        self.assertEqual(outcome.status, "complete")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["turns"][0]["collection"]["accepted"], True)
        self.assertEqual(receipt["result"]["completion_basis"], "native-inline")

    def test_truncation_rejects_predecessor_and_prepares_one_child(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        truncated = self.evidence(
            turn_id=turn_id,
            user_message_id="user-7319",
            text=f"{cpd.result_marker(turn_id)}\nprefix",
            truncated=True,
            outer_truncated=True,
        )
        with self.assertRaises(cpd.TruncationError) as raised:
            cpd.collect_turn(prepared.assignment_id, turn_id, truncated, paths=self.paths)
        self.assertEqual(raised.exception.error_code, "collection_truncated")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["turns"][0]["status"], "response_rejected")
        self.assertEqual(receipt["turns"][0]["rejection_predecessor_status"], "submitted")
        self.assertEqual(receipt["turns"][1]["status"], "prepared")
        self.assertEqual(receipt["turns"][1]["previous_turn_id"], turn_id)
        self.assertEqual(receipt["effective_result_mode"], "chunked")
        self.assertEqual(
            cpd.recovery_info(prepared.assignment_id, self.paths)["current_turn"]["status"],
            "prepared",
        )
        # A replay cannot manufacture another child.
        replay = cpd.collect_turn(prepared.assignment_id, turn_id, truncated, paths=self.paths)
        self.assertEqual(replay.status, "active")
        self.assertEqual(len(cpd.load_assignment(prepared.assignment_id, self.paths)["turns"]), 2)

    def test_recovery_requires_and_preserves_completed_collection_evidence(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        predecessor = receipt["turns"][0]
        with self.assertRaises(cpd.StateError) as raised:
            transport._append_child_turn(
                receipt,
                predecessor,
                purpose="truncated-retransmission",
                next_index=1,
                previous_chain_sha256=CHAIN_ZERO_HEX,
                retransmission=True,
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "recovery_generation_unproven")

        turn_id = prepared.turn_id or prepared.assignment_id
        with self.assertRaises(cpd.TruncationError):
            cpd.collect_turn(
                prepared.assignment_id,
                turn_id,
                self.evidence(
                    turn_id=turn_id,
                    user_message_id="user-7319",
                    text=f"{cpd.result_marker(turn_id)}\nprefix",
                    truncated=True,
                    outer_truncated=True,
                ),
                paths=self.paths,
            )
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        rejected = raw["turns"][0]
        self.assertEqual(rejected["rejection_collection"]["status"], "truncated")
        self.assertFalse(rejected["rejection_collection"]["accepted"])
        rejected.pop("rejection_collection")
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(cpd.ReceiptMigrationError):
            cpd.load_assignment(prepared.assignment_id, self.paths)

    def test_rejected_turn_receipt_requires_submitted_or_pending_origin(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        with self.assertRaises(cpd.TruncationError):
            cpd.collect_turn(
                prepared.assignment_id,
                turn_id,
                self.evidence(
                    turn_id=turn_id,
                    user_message_id="user-7319",
                    text=f"{cpd.result_marker(turn_id)}\\nprefix",
                    truncated=True,
                    outer_truncated=True,
                ),
                paths=self.paths,
            )
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        raw["turns"][0]["rejection_predecessor_status"] = "ambiguous"
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(cpd.ReceiptMigrationError):
            cpd.load_assignment(prepared.assignment_id, self.paths)

    def test_truncated_prefix_can_upgrade_before_its_child_is_armed(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        with self.assertRaises(cpd.TruncationError):
            cpd.collect_turn(
                prepared.assignment_id,
                turn_id,
                self.evidence(
                    turn_id=turn_id,
                    user_message_id="user-7319",
                    text=f"{cpd.result_marker(turn_id)}\nprefix",
                    truncated=True,
                    outer_truncated=True,
                ),
                paths=self.paths,
            )
        complete = self.evidence(
            turn_id=turn_id,
            user_message_id="user-7319",
            text=f"{cpd.result_marker(turn_id)}\ncomplete reread",
            truncated=False,
            outer_truncated=False,
            observed_at="2031-01-01T00:00:00.000Z",
        )
        outcome = cpd.collect_turn(prepared.assignment_id, turn_id, complete, paths=self.paths)
        self.assertEqual(outcome.status, "complete")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["turns"][0]["status"], "response_rejected")
        self.assertEqual(receipt["turns"][1]["status"], "failed")
        self.assertEqual(receipt["result"]["completion_basis"], "native-inline")

    def test_chunked_json_payload_is_lossless_and_has_no_inserted_separator(self) -> None:
        prepared = self.prepare(mode="chunked")
        self.submit(prepared, user_message_id="user-1")
        first_turn = prepared.turn_id or prepared.assignment_id
        payload_one = "first\n[CODEX_PRO_DISPATCH_RESULT assignment_id=not-framing]\n"
        first_response = cpd.format_chunk_response(
            turn_id=first_turn,
            group_id=prepared.assignment_id,
            index=1,
            previous_chain_sha256=CHAIN_ZERO_HEX,
            final=False,
            count=0,
            payload=payload_one,
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            first_turn,
            self.evidence(
                turn_id=first_turn,
                user_message_id="user-1",
                assistant_message_id="assistant-1",
                text=first_response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        child = first.next_turn
        self.submit(child, user_message_id="user-2")
        payload_two = "second"
        second_response = cpd.format_chunk_response(
            turn_id=child.turn_id or "",
            group_id=prepared.assignment_id,
            index=2,
            previous_chain_sha256=str(first.accepted_chunk["chain_sha256"]),
            final=True,
            count=2,
            payload=payload_two,
        )
        output = Path(self.temporary.name) / "chunked.md"
        final = cpd.collect_turn(
            prepared.assignment_id,
            child.turn_id or "",
            self.evidence(
                turn_id=child.turn_id or "",
                user_message_id="user-2",
                assistant_message_id="assistant-2",
                text=second_response,
            ),
            output,
            paths=self.paths,
        )
        self.assertEqual(final.status, "complete")
        self.assertEqual(output.read_text(encoding="utf-8"), payload_one + payload_two)
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["result"]["chunk_count"], 2)
        self.assertNotIn(payload_one, json.dumps(receipt))
        self.assertEqual(
            receipt["result"]["payload_sha256"],
            hashlib.sha256((payload_one + payload_two).encode("utf-8")).hexdigest(),
        )

    def test_legacy_completed_receipt_is_immutable_but_honestly_labeled(self) -> None:
        self.configure()
        assignment_id = "legacy-complete-7319"
        path = self.paths.assignments_dir / f"{assignment_id}.json"
        path.parent.mkdir(parents=True)
        legacy = {
            "schema_version": 1,
            "assignment_id": assignment_id,
            "status": "complete",
            "created_at": "2025-01-01T00:00:00.000Z",
            "worker_conversation_id": self.worker_id,
            "parent_task_id": self.parent_id,
            "response_marker": cpd.result_marker(assignment_id),
        }
        raw = json.dumps(legacy, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
        viewed = cpd.load_assignment(assignment_id, self.paths)
        self.assertEqual(viewed["legacy"]["completion_basis"], "marker-only")
        self.assertEqual(viewed["legacy"]["collection_integrity"], "unverifiable")
        with self.assertRaises(cpd.ReceiptMigrationError):
            cpd.arm_assignment(assignment_id, self.paths)
        self.assertEqual(path.read_bytes(), raw)

    def test_unresolved_v1_migrates_under_the_lock_on_state_change(self) -> None:
        self.configure()
        assignment_id = "legacy-open-7319"
        path = self.paths.assignments_dir / f"{assignment_id}.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "assignment_id": assignment_id,
                    "status": "prepared",
                    "created_at": "2025-01-01T00:00:00.000Z",
                    "worker_conversation_id": self.worker_id,
                    "worker_label": "worker",
                    "worker_model_confirmation": "user-confirmed-pro",
                    "parent_task_id": self.parent_id,
                    "prompt_sha256": "a" * 64,
                    "wrapped_prompt_sha256": "b" * 64,
                    "response_marker": cpd.result_marker(assignment_id),
                    "submission_count": 0,
                }
            ),
            encoding="utf-8",
        )
        armed = cpd.arm_assignment(assignment_id, self.paths)
        self.assertEqual(armed["schema_version"], 2)
        self.assertEqual(armed["legacy"]["origin_schema"], 1)
        self.assertTrue(armed["legacy"]["collection_evidence_required"])
        self.assertEqual(armed["turns"][0]["status"], "armed")

    def test_delivery_and_parent_restoration_do_not_reopen_result(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        turn_id = prepared.turn_id or prepared.assignment_id
        cpd.collect_turn(
            prepared.assignment_id,
            turn_id,
            self.evidence(
                turn_id=turn_id,
                user_message_id="user-7319",
                text=f"{cpd.result_marker(turn_id)}\nready",
            ),
            paths=self.paths,
        )
        restored = cpd.record_parent_restoration(
            prepared.assignment_id, restored=True, paths=self.paths
        )
        self.assertEqual(restored["status"], "complete")
        self.assertEqual(restored["result"]["status"], "complete")
        self.assertEqual(restored["delivery"]["parent_restoration_status"], "restored")
        with self.assertRaises(cpd.StateError):
            cpd.arm_assignment(prepared.assignment_id, self.paths)

    # Baseline v1 safety behavior, asserted against v2's logical-dispatch and
    # per-turn split rather than the former overloaded root receipt fields.

    def test_worker_confirmation_and_private_configuration_are_preserved(self) -> None:
        with self.assertRaises(cpd.ConfigurationError):
            cpd.save_worker(self.worker_id, confirm_pro=False, paths=self.paths)
        self.configure()
        worker = cpd.load_worker(self.paths)
        self.assertEqual(worker.conversation_id, self.worker_id)
        self.assertEqual(stat.S_IMODE(self.paths.worker_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.config_dir.stat().st_mode), 0o700)

    def test_identifiers_and_second_active_dispatch_fail_closed(self) -> None:
        self.configure()
        for assignment_id in ("../escape", "/absolute", "bad space", ""):
            with self.subTest(assignment_id=assignment_id):
                with self.assertRaises(cpd.ConfigurationError):
                    cpd.prepare_assignment(
                        "Task",
                        parent_task_id=self.parent_id,
                        assignment_id=assignment_id,
                        paths=self.paths,
                    )
        self.prepare()
        with self.assertRaises(cpd.BusyError):
            cpd.prepare_assignment(
                "Second task",
                parent_task_id=self.parent_id,
                assignment_id="dispatch-second-7319",
                paths=self.paths,
            )

    def test_readback_drift_is_collect_only_and_never_resends(self) -> None:
        prepared = self.prepare()
        cpd.arm_assignment(prepared.assignment_id, self.paths)
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id,
                "+" + prepared.wrapped_prompt,
                self.paths,
            )
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        turn = receipt["turns"][0]
        self.assertEqual(receipt["status"], "recoverable")
        self.assertEqual(turn["status"], "indeterminate")
        self.assertEqual(turn["submission_count"], 1)
        self.assertTrue(turn["no_resend"])
        self.assertFalse(turn["outbound_prompt_verified"])
        self.assertNotEqual(turn["sent_prompt_sha256"], turn["wrapped_prompt_sha256"])
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)

    def test_one_trailing_newline_readback_correction_keeps_one_send(self) -> None:
        prepared = self.prepare()
        cpd.arm_assignment(prepared.assignment_id, self.paths)
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id,
                prepared.wrapped_prompt + "\n",
                self.paths,
            )
        mismatched = cpd.load_assignment(prepared.assignment_id, self.paths)["turns"][0]
        self.assertTrue(mismatched["readback_correction_allowed"])
        corrected = cpd.mark_submitted(
            prepared.assignment_id,
            prepared.wrapped_prompt,
            self.paths,
            native_user_message_id="user-7319",
        )
        turn = corrected["turns"][0]
        self.assertEqual(corrected["status"], "active")
        self.assertEqual(turn["status"], "submitted")
        self.assertEqual(turn["submission_count"], 1)
        self.assertEqual(turn["readback_verification_attempt_count"], 2)
        self.assertEqual(turn["submission_recovered_from"], "indeterminate")
        self.assertEqual(turn["readback_correction_kind"], "single-trailing-newline")
        self.assertNotIn("readback_correction_allowed", turn)
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(prepared.assignment_id, prepared.wrapped_prompt, self.paths)

    def test_late_readback_after_indeterminate_or_ambiguous_is_not_a_resend(self) -> None:
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                prepared = self.prepare(
                    assignment_id=f"dispatch-late-{'ambiguous' if ambiguous else 'indeterminate'}-7319"
                )
                cpd.arm_assignment(prepared.assignment_id, self.paths)
                cpd.mark_indeterminate(
                    prepared.assignment_id,
                    reason="native read-back was temporarily stale",
                    paths=self.paths,
                )
                if ambiguous:
                    cpd.mark_ambiguous(
                        prepared.assignment_id,
                        reason="response visibility was ambiguous",
                        paths=self.paths,
                    )
                updated = cpd.mark_submitted(
                    prepared.assignment_id,
                    prepared.wrapped_prompt,
                    self.paths,
                    native_user_message_id="user-7319",
                )
                turn = updated["turns"][0]
                self.assertEqual(turn["submission_count"], 1)
                self.assertTrue(turn["outbound_prompt_verified"])
                self.assertEqual(
                    turn["submission_recovered_from"],
                    "ambiguous" if ambiguous else "indeterminate",
                )
                self.assertEqual(turn["recovery_authority"], "exact-readback-recovered")
                self.assertEqual(updated["send_attempt_total"], 1)
                cpd.abandon_assignment(
                    prepared.assignment_id, reason="finish subcase", paths=self.paths
                )

    def test_collect_only_transitions_require_durable_arm(self) -> None:
        prepared = self.prepare()
        for operation in (
            lambda: cpd.mark_indeterminate(
                prepared.assignment_id, reason="bypass", paths=self.paths
            ),
            lambda: cpd.mark_ambiguous(
                prepared.assignment_id, reason="bypass", paths=self.paths
            ),
            lambda: cpd.mark_unusual_activity_403(
                prepared.assignment_id, reason="bypass", paths=self.paths
            ),
        ):
            with self.assertRaises(cpd.StateError):
                operation()
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["status"], "prepared")
        self.assertEqual(receipt["turns"][0]["status"], "prepared")

    def test_cooldown_persists_through_abandon_and_blocks_a_new_dispatch(self) -> None:
        prepared = self.prepare()
        cpd.arm_assignment(prepared.assignment_id, self.paths)
        value = cpd.mark_unusual_activity_403(
            prepared.assignment_id,
            reason="HTTP 403 unusual activity",
            request_id="request-7319",
            paths=self.paths,
        )
        self.assertEqual(value["native_http_status"], 403)
        self.assertEqual(value["turns"][0]["status"], "indeterminate")
        started = dt.datetime.fromisoformat(value["cooldown_started_at"].replace("Z", "+00:00"))
        cooldown = cpd.active_cooldown(self.paths, now=started + dt.timedelta(minutes=29))
        self.assertIsNotNone(cooldown)
        assert cooldown is not None
        self.assertEqual(cooldown["retry_after_seconds"], 60)
        cpd.abandon_assignment(prepared.assignment_id, reason="user authorized", paths=self.paths)
        with self.assertRaises(cpd.CooldownError):
            self.prepare(assignment_id="dispatch-cooldown-7319")
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        raw["cooldown_until"] = "2000-01-01T00:00:00.000Z"
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")
        fresh = self.prepare(assignment_id="dispatch-after-cooldown-7319")
        self.assertEqual(fresh.assignment_id, "dispatch-after-cooldown-7319")

    def test_complete_alias_remains_evidence_gated_and_marker_strict(self) -> None:
        prepared = self.prepare()
        marker = cpd.result_marker(prepared.turn_id or prepared.assignment_id)
        response = f"{marker}\nDone"
        with self.assertRaises(cpd.CollectionEvidenceError) as raised:
            cpd.complete_assignment(prepared.assignment_id, response, self.paths)
        self.assertEqual(raised.exception.error_code, "collection_evidence_required")
        cpd.arm_assignment(prepared.assignment_id, self.paths)
        with self.assertRaises(cpd.CollectionEvidenceError):
            cpd.complete_assignment(
                prepared.assignment_id,
                response,
                self.paths,
                evidence=self.evidence(
                    turn_id=prepared.turn_id or prepared.assignment_id,
                    user_message_id="user-7319",
                    text=response,
                ),
            )
        cpd.mark_submitted(
            prepared.assignment_id,
            prepared.wrapped_prompt,
            self.paths,
            turn_id=prepared.turn_id,
            native_user_message_id="user-7319",
        )
        for invalid in (
            f"Preamble\n{marker}\nDone",
            f" {marker} \nDone",
            "[CODEX_PRO_DISPATCH_RESULT assignment_id=wrong]\nDone",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(cpd.MarkerError):
                    cpd.complete_assignment(
                        prepared.assignment_id,
                        invalid,
                        self.paths,
                        evidence=self.evidence(
                            turn_id=prepared.turn_id or prepared.assignment_id,
                            user_message_id="user-7319",
                            text=invalid,
                        ),
                    )

    def test_continuation_requires_completed_dispatch_and_same_worker(self) -> None:
        first = self.prepare()
        self.submit(first)
        turn_id = first.turn_id or first.assignment_id
        cpd.collect_turn(
            first.assignment_id,
            turn_id,
            self.evidence(
                turn_id=turn_id,
                user_message_id="user-7319",
                text=f"{cpd.result_marker(turn_id)}\nDone",
            ),
            paths=self.paths,
        )
        second = self.prepare(assignment_id="dispatch-followup-7319")
        cpd.abandon_assignment(second.assignment_id, reason="clear slot", paths=self.paths)
        followup = cpd.prepare_assignment(
            "Repair a test.",
            parent_task_id=self.parent_id,
            continuation_of=first.assignment_id,
            assignment_id="dispatch-continuation-7319",
            paths=self.paths,
        )
        self.assertEqual(
            cpd.load_assignment(followup.assignment_id, self.paths)["continuation_of"],
            first.assignment_id,
        )

    def test_worker_reset_purge_and_corrupt_receipt_stay_fail_closed(self) -> None:
        prepared = self.prepare()
        with self.assertRaises(cpd.BusyError):
            cpd.reset_worker(paths=self.paths)
        with self.assertRaises(cpd.BusyError):
            cpd.purge_local_state(paths=self.paths)

        # Force purge may remove only a helper-shaped private spool part.  This
        # guards the narrow filename allowlist while ensuring it does not reject
        # the files chunk collection itself creates.
        spool_root = self.paths.spool_dir / prepared.assignment_id
        spool_root.mkdir(parents=True, mode=0o700)
        spool_root.chmod(0o700)
        spool_part = spool_root / "chunk-000001.part"
        spool_part.write_bytes(b"private chunk payload")
        spool_part.chmod(0o600)
        result = cpd.purge_local_state(force=True, paths=self.paths)
        self.assertTrue(result["worker_removed"])
        self.assertTrue(result["assignments_removed"])
        self.assertEqual(result["spool_files_removed"], 1)
        self.assertFalse(spool_part.exists())

        self.configure()
        self.paths.assignments_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.assignments_dir / "broken.json").write_text("not json", encoding="utf-8")
        with self.assertRaises(cpd.StateError):
            cpd.prepare_assignment(
                "Task",
                parent_task_id=self.parent_id,
                assignment_id="dispatch-safe-7319",
                paths=self.paths,
            )

    def test_armed_crash_window_is_collect_only_and_submission_requires_arm(self) -> None:
        prepared = self.prepare()
        with self.assertRaises(cpd.StateError):
            cpd.mark_submitted(
                prepared.assignment_id,
                prepared.wrapped_prompt,
                self.paths,
                native_user_message_id="user-7319",
            )
        armed = cpd.arm_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(armed["status"], "active")
        self.assertTrue(armed["no_original_resend"])
        recovery = cpd.recovery_info(prepared.assignment_id, self.paths)
        self.assertEqual(
            recovery["next_action"],
            "collect_or_verify_existing_native_message_without_resend",
        )
        self.assertNotIn("Implement the approved repair.", json.dumps(recovery))

    def test_abandon_clears_the_active_slot_but_does_not_bypass_a_cooldown(self) -> None:
        prepared = self.prepare()
        cpd.abandon_assignment(
            prepared.assignment_id, reason="user chose to stop", paths=self.paths
        )
        self.assertIsNone(cpd.active_assignment(self.paths))
        self.assertTrue(cpd.reset_worker(paths=self.paths))
        cpd.save_worker(self.worker_id, confirm_pro=True, paths=self.paths)
        fresh = cpd.prepare_assignment(
            "different bounded task",
            parent_task_id=self.parent_id,
            assignment_id="dispatch-after-abandon-7319",
            paths=self.paths,
        )
        self.assertEqual(fresh.assignment_id, "dispatch-after-abandon-7319")

    def test_v2_nested_raw_diagnostics_are_hidden_then_durably_redacted(self) -> None:
        prepared = self.prepare()
        cpd.arm_assignment(prepared.assignment_id, self.paths)
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        sentinel = "PRIVATE_NESTED_ERROR_BODY_7319"
        raw["turns"][0]["last_error"] = sentinel
        raw["turns"][0]["reason"] = "private nested reason"
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")

        visible = cpd.load_assignment(prepared.assignment_id, self.paths)
        turn = visible["turns"][0]
        self.assertNotIn("last_error", turn)
        self.assertNotIn("reason", turn)
        self.assertEqual(turn["last_error_sha256"], cpd.sha256_text(sentinel))
        self.assertIn(sentinel, prepared.receipt_path.read_text(encoding="utf-8"))

        self.assertEqual(cpd.redact_stored_diagnostics(self.paths), 1)
        persisted = prepared.receipt_path.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, persisted)
        self.assertNotIn("private nested reason", persisted)

    def test_unusual_activity_repeated_observation_keeps_the_original_cooldown(self) -> None:
        prepared = self.prepare()
        cpd.arm_assignment(prepared.assignment_id, self.paths)
        first = cpd.mark_unusual_activity_403(
            prepared.assignment_id,
            reason="HTTP 403 unusual activity first response",
            request_id="request-first-7319",
            paths=self.paths,
        )
        repeated = cpd.mark_unusual_activity_403(
            prepared.assignment_id,
            reason="HTTP 403 different body must not restart cooldown",
            request_id="request-second-7319",
            paths=self.paths,
        )
        self.assertEqual(repeated["cooldown_until"], first["cooldown_until"])
        self.assertEqual(repeated["openai_request_id"], "request-first-7319")
        self.assertNotIn("different body", json.dumps(repeated))

    def test_tampered_collection_state_cannot_be_used_to_complete_a_result(self) -> None:
        prepared = self.prepare()
        self.submit(prepared)
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        raw["turns"][0]["collection"] = {"status": "accepted"}
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(cpd.ReceiptMigrationError):
            cpd.load_assignment(prepared.assignment_id, self.paths)


if __name__ == "__main__":
    unittest.main()
