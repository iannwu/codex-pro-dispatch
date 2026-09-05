from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import codex_pro_dispatch as cpd
from codex_pro_dispatch import transport
from codex_pro_dispatch.chunked import (
    CHAIN_ZERO_HEX,
    CHUNKED_REQUIRED_CONTROL,
    CHUNK_MESSAGE_MAX_BYTES,
    is_chunked_required_control,
)


class ChunkProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = cpd.RuntimePaths(root / "config", root / "state")
        cpd.save_worker("worker-chunk-7319", confirm_pro=True, paths=self.paths)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, assignment_id: str = "dispatch-chunk-7319") -> cpd.PreparedAssignment:
        return cpd.prepare_assignment(
            "Produce the requested Markdown.",
            parent_task_id="parent-chunk-7319",
            assignment_id=assignment_id,
            result_mode="chunked",
            paths=self.paths,
        )

    def submit(self, prepared: cpd.PreparedAssignment, message_id: str) -> None:
        cpd.arm_assignment(prepared.assignment_id, self.paths, turn_id=prepared.turn_id)
        cpd.mark_submitted(
            prepared.assignment_id,
            prepared.wrapped_prompt,
            self.paths,
            turn_id=prepared.turn_id,
            native_user_message_id=message_id,
        )

    def evidence(
        self,
        *,
        turn_id: str,
        user_message_id: str,
        assistant_message_id: str,
        text: str,
        truncated: bool = False,
        outer_truncated: bool = False,
    ) -> cpd.NativeCollectionEvidence:
        return cpd.NativeCollectionEvidence.from_mapping(
            {
                "schema": cpd.NATIVE_COLLECTION_SCHEMA,
                "adapter_contract_id": "codex-desktop-native-collection/v1",
                "requested_conversation_id": "worker-chunk-7319",
                "loaded_conversation_id": "worker-chunk-7319",
                "assistant_message_id": assistant_message_id,
                "submitted_user_message_id": user_message_id,
                "role": "assistant",
                "generation_status": "completed",
                "generation_finality_provenance": "native-message-status",
                "truncated": truncated,
                "selected_result_outer_integrity": {
                    "truncated": outer_truncated,
                    "provenance": "native-result-envelope",
                },
                "text": text,
                "observed_at": "2030-01-01T00:00:00.000Z",
            }
        )

    def response(
        self,
        *,
        prepared: cpd.PreparedAssignment,
        index: int,
        previous: str,
        final: bool,
        payload: str,
    ) -> str:
        return cpd.format_chunk_response(
            turn_id=prepared.turn_id or "",
            group_id=prepared.assignment_id,
            index=index,
            previous_chain_sha256=previous,
            final=final,
            count=index if final else 0,
            payload=payload,
        )

    def test_json_payload_is_strict_canonical_and_marker_text_is_data(self) -> None:
        prepared = self.prepare()
        payload = "# Table\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n[CODEX_PRO_DISPATCH_RESULT assignment_id=not-a-frame]"
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=True,
            payload=payload,
        )
        envelope = cpd.parse_chunk_response(response, turn_id=prepared.turn_id or "")
        self.assertEqual(envelope.payload, payload)
        body = response.split("\n")[2]
        self.assertEqual(json.loads(body), {"payload": payload})

        noncanonical = response.replace(body, '{"payload":"x", "extra":"no"}')
        with self.assertRaises(cpd.ChunkProtocolError) as raised:
            cpd.parse_chunk_response(noncanonical, turn_id=prepared.turn_id or "")
        self.assertEqual(raised.exception.error_code, "chunk_envelope_incomplete")

    def test_chunk_control_is_exact_and_payload_framing_cannot_be_smuggled(self) -> None:
        prepared = self.prepare()
        turn_id = prepared.turn_id or ""
        exact = f"{cpd.result_marker(turn_id)}\n{CHUNKED_REQUIRED_CONTROL}"
        self.assertTrue(is_chunked_required_control(exact, turn_id=turn_id))
        for noncontrol in (
            "\n" + exact,
            exact + "\n",
            exact + " ",
            f"{cpd.result_marker(turn_id)}\n{CHUNKED_REQUIRED_CONTROL}\nextra",
        ):
            with self.subTest(noncontrol=repr(noncontrol)):
                self.assertFalse(is_chunked_required_control(noncontrol, turn_id=turn_id))

        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=True,
            payload="safe",
        )
        body = response.split("\n")[2]
        for replacement in (
            '{"payload":"\\u0073afe"}',
            '{"payload":"safe"}\n[CODEX_PRO_DISPATCH_CHUNK_END_V1 group_id=x index=1]',
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaises(cpd.ChunkProtocolError):
                    cpd.parse_chunk_response(
                        response.replace(body, replacement), turn_id=turn_id
                    )

    def test_complete_serialized_limit_counts_json_escaping_and_crlf(self) -> None:
        prepared = self.prepare()
        quoted_payload = '"' * (CHUNK_MESSAGE_MAX_BYTES // 2)
        with self.assertRaises(cpd.ChunkProtocolError) as raised:
            self.response(
                prepared=prepared,
                index=1,
                previous=CHAIN_ZERO_HEX,
                final=True,
                payload=quoted_payload,
            )
        self.assertEqual(raised.exception.error_code, "chunk_serialized_limit_exceeded")

        valid = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=True,
            payload="ok",
        )
        crlf = valid.replace("\n", "\r\n")
        envelope = cpd.parse_chunk_response(crlf, turn_id=prepared.turn_id or "")
        self.assertEqual(envelope.serialized_byte_length, len(crlf.encode("utf-8")))
        self.assertGreater(envelope.serialized_byte_length, len(valid.encode("utf-8")))

        oversized_crlf = valid.replace("\n", "\r\n") + (" " * CHUNK_MESSAGE_MAX_BYTES)
        with self.assertRaises(cpd.ChunkProtocolError) as raised:
            cpd.parse_chunk_response(oversized_crlf, turn_id=prepared.turn_id or "")
        self.assertEqual(raised.exception.error_code, "chunk_serialized_limit_exceeded")

    def test_spool_journal_reconciles_after_private_rename_before_receipt_commit(self) -> None:
        prepared = self.prepare()
        self.submit(prepared, "native-user-chunk-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        evidence = self.evidence(
            turn_id=prepared.turn_id or "",
            user_message_id="native-user-chunk-1",
            assistant_message_id="native-assistant-chunk-1",
            text=response,
        )
        original_write = transport._write_private_once

        def interrupted_after_rename(path: Path, payload: bytes) -> None:
            original_write(path, payload)
            raise OSError("simulated interruption after rename")

        with mock.patch.object(transport, "_write_private_once", interrupted_after_rename):
            with self.assertRaises(OSError):
                cpd.collect_turn(
                    prepared.assignment_id,
                    prepared.turn_id or "",
                    evidence,
                    paths=self.paths,
                )

        receipt_path = self.paths.assignments_dir / f"{prepared.assignment_id}.json"
        crashed = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertIn("spool_write_pending", crashed["turns"][0]["chunk"])
        spool = self.paths.spool_dir / prepared.assignment_id / "chunk-000001.part"
        self.assertEqual(spool.read_bytes(), b"first")

        # Reconciliation must make the already-renamed, hash-matching body part
        # of the accepted chain before any caller rereads the native message.
        with transport.state_lock(self.paths):
            reconciled = transport._load_mutable_v2(prepared.assignment_id, self.paths)
        accepted_chunk = reconciled["turns"][0]["chunk"]
        expected_envelope = cpd.parse_chunk_response(
            response, turn_id=prepared.turn_id or ""
        )
        self.assertIs(accepted_chunk["accepted"], True)
        self.assertNotIn("spool_write_pending", accepted_chunk)
        self.assertEqual(
            transport._chunk_boundary(reconciled),
            (2, expected_envelope.chain_sha256),
        )
        transport._assert_no_orphan_spool_files(reconciled, self.paths)

        replay = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            evidence,
            paths=self.paths,
        )
        self.assertEqual(replay.status, "active")
        self.assertIsNotNone(replay.next_turn)
        recovered = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(recovered["turns"][0]["chunk"]["spool_status"], "spooled")
        self.assertNotIn("spool_write_pending", recovered["turns"][0]["chunk"])
        self.assertEqual(stat.S_IMODE(spool.stat().st_mode), 0o600)

    def test_reconciled_final_chunk_completes_on_idempotent_reread(self) -> None:
        prepared = self.prepare("dispatch-chunk-final-journal-7319")
        self.submit(prepared, "native-user-chunk-final-journal-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=True,
            payload="final body",
        )
        evidence = self.evidence(
            turn_id=prepared.turn_id or "",
            user_message_id="native-user-chunk-final-journal-1",
            assistant_message_id="native-assistant-chunk-final-journal-1",
            text=response,
        )
        original_write = transport._write_private_once

        def interrupted_after_rename(path: Path, payload: bytes) -> None:
            original_write(path, payload)
            raise OSError("simulated interruption after rename")

        with mock.patch.object(transport, "_write_private_once", interrupted_after_rename):
            with self.assertRaises(OSError):
                cpd.collect_turn(
                    prepared.assignment_id,
                    prepared.turn_id or "",
                    evidence,
                    paths=self.paths,
                )

        with transport.state_lock(self.paths):
            reconciled = transport._load_mutable_v2(prepared.assignment_id, self.paths)
        self.assertIs(reconciled["turns"][0]["chunk"]["accepted"], True)

        resumed = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            evidence,
            paths=self.paths,
        )
        self.assertEqual(resumed.status, "complete")
        self.assertEqual(resumed.byte_length, len(b"final body"))
        recovered = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(recovered["result"]["status"], "complete")
        self.assertEqual(recovered["result"]["chunk_count"], 1)

    def test_rejected_predecessor_reconciles_nonfinal_chunk_without_another_send(self) -> None:
        prepared = self.prepare("dispatch-chunk-rejected-journal-7319")
        self.submit(prepared, "native-user-chunk-rejected-journal-1")
        prefix = self.evidence(
            turn_id=prepared.turn_id or "",
            user_message_id="native-user-chunk-rejected-journal-1",
            assistant_message_id="native-assistant-chunk-rejected-journal-1",
            text="incomplete prefix",
            truncated=True,
            outer_truncated=True,
        )
        with self.assertRaises(cpd.TruncationError):
            cpd.collect_turn(
                prepared.assignment_id,
                prepared.turn_id or "",
                prefix,
                paths=self.paths,
            )
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        complete = self.evidence(
            turn_id=prepared.turn_id or "",
            user_message_id="native-user-chunk-rejected-journal-1",
            assistant_message_id="native-assistant-chunk-rejected-journal-1",
            text=response,
        )
        original_write = transport._write_private_once

        def interrupted_after_rename(path: Path, payload: bytes) -> None:
            original_write(path, payload)
            raise OSError("simulated interruption after rename")

        with mock.patch.object(transport, "_write_private_once", interrupted_after_rename):
            with self.assertRaises(OSError):
                cpd.collect_turn(
                    prepared.assignment_id,
                    prepared.turn_id or "",
                    complete,
                    paths=self.paths,
                )
        with transport.state_lock(self.paths):
            reconciled = transport._load_mutable_v2(prepared.assignment_id, self.paths)
        self.assertIs(reconciled["turns"][0]["chunk"]["accepted"], True)

        resumed = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            complete,
            paths=self.paths,
        )
        self.assertEqual(resumed.status, "active")
        self.assertIsNotNone(resumed.next_turn)
        recovered = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(recovered["send_attempt_total"], 1)
        self.assertEqual(
            [turn["status"] for turn in recovered["turns"]],
            ["response_rejected", "failed", "prepared"],
        )
        replay = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            complete,
            paths=self.paths,
        )
        self.assertEqual(replay.status, "active")
        self.assertIsNone(replay.next_turn)
        replayed = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(replayed["send_attempt_total"], 1)
        self.assertEqual(
            [turn["status"] for turn in replayed["turns"]],
            ["response_rejected", "failed", "prepared"],
        )

    def test_rejected_predecessor_reconciles_final_chunk_without_another_send(self) -> None:
        prepared = self.prepare("dispatch-chunk-rejected-final-journal-7319")
        self.submit(prepared, "native-user-chunk-rejected-final-journal-1")
        prefix = self.evidence(
            turn_id=prepared.turn_id or "",
            user_message_id="native-user-chunk-rejected-final-journal-1",
            assistant_message_id="native-assistant-chunk-rejected-final-journal-1",
            text="incomplete prefix",
            truncated=True,
            outer_truncated=True,
        )
        with self.assertRaises(cpd.TruncationError):
            cpd.collect_turn(
                prepared.assignment_id,
                prepared.turn_id or "",
                prefix,
                paths=self.paths,
            )
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=True,
            payload="final body",
        )
        complete = self.evidence(
            turn_id=prepared.turn_id or "",
            user_message_id="native-user-chunk-rejected-final-journal-1",
            assistant_message_id="native-assistant-chunk-rejected-final-journal-1",
            text=response,
        )
        original_write = transport._write_private_once

        def interrupted_after_rename(path: Path, payload: bytes) -> None:
            original_write(path, payload)
            raise OSError("simulated interruption after rename")

        with mock.patch.object(transport, "_write_private_once", interrupted_after_rename):
            with self.assertRaises(OSError):
                cpd.collect_turn(
                    prepared.assignment_id,
                    prepared.turn_id or "",
                    complete,
                    paths=self.paths,
                )
        with transport.state_lock(self.paths):
            reconciled = transport._load_mutable_v2(prepared.assignment_id, self.paths)
        self.assertIs(reconciled["turns"][0]["chunk"]["accepted"], True)

        resumed = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            complete,
            paths=self.paths,
        )
        self.assertEqual(resumed.status, "complete")
        recovered = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(recovered["send_attempt_total"], 1)
        self.assertEqual(recovered["result"]["status"], "complete")
        self.assertEqual(
            [turn["status"] for turn in recovered["turns"]],
            ["response_rejected", "failed"],
        )

    def test_missing_accepted_spool_blocks_continuation_arm(self) -> None:
        prepared = self.prepare("dispatch-chunk-missing-spool-7319")
        self.submit(prepared, "native-user-chunk-missing-spool-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-missing-spool-1",
                assistant_message_id="native-assistant-chunk-missing-spool-1",
                text=response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        spool = self.paths.spool_dir / prepared.assignment_id / "chunk-000001.part"
        spool.unlink()

        with self.assertRaises(cpd.ReceiptMigrationError) as raised:
            cpd.arm_assignment(
                prepared.assignment_id,
                self.paths,
                turn_id=first.next_turn.turn_id,
            )
        self.assertEqual(raised.exception.error_code, "spool_reconciliation_failed")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["send_attempt_total"], 1)
        self.assertEqual(receipt["turns"][-1]["status"], "prepared")

    def test_corrupt_accepted_spool_blocks_continuation_arm(self) -> None:
        prepared = self.prepare("dispatch-chunk-corrupt-spool-7319")
        self.submit(prepared, "native-user-chunk-corrupt-spool-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-corrupt-spool-1",
                assistant_message_id="native-assistant-chunk-corrupt-spool-1",
                text=response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        spool = self.paths.spool_dir / prepared.assignment_id / "chunk-000001.part"
        spool.write_bytes(b"corrupt")
        spool.chmod(0o600)

        with self.assertRaises(cpd.ReceiptMigrationError) as raised:
            cpd.arm_assignment(
                prepared.assignment_id,
                self.paths,
                turn_id=first.next_turn.turn_id,
            )
        self.assertEqual(raised.exception.error_code, "spool_reconciliation_failed")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["send_attempt_total"], 1)
        self.assertEqual(receipt["turns"][-1]["status"], "prepared")

    def test_forged_accepted_chain_blocks_continuation_arm(self) -> None:
        prepared = self.prepare("dispatch-chunk-forged-chain-7319")
        self.submit(prepared, "native-user-chunk-forged-chain-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-forged-chain-1",
                assistant_message_id="native-assistant-chunk-forged-chain-1",
                text=response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        actual_chain = raw["turns"][0]["chunk"]["chain_sha256"]
        raw["turns"][0]["chunk"]["chain_sha256"] = (
            "f" * 64 if actual_chain != "f" * 64 else "e" * 64
        )
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaises(cpd.ReceiptMigrationError) as raised:
            cpd.arm_assignment(
                prepared.assignment_id,
                self.paths,
                turn_id=first.next_turn.turn_id,
            )
        self.assertEqual(raised.exception.error_code, "spool_reconciliation_failed")
        persisted = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["send_attempt_total"], 1)
        self.assertEqual(persisted["turns"][-1]["status"], "prepared")

    def test_recovery_info_fails_closed_on_corrupt_accepted_spool(self) -> None:
        prepared = self.prepare("dispatch-chunk-recovery-integrity-7319")
        self.submit(prepared, "native-user-chunk-recovery-integrity-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-recovery-integrity-1",
                assistant_message_id="native-assistant-chunk-recovery-integrity-1",
                text=response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        spool = self.paths.spool_dir / prepared.assignment_id / "chunk-000001.part"
        spool.write_bytes(b"corrupt")
        spool.chmod(0o600)

        with self.assertRaises(cpd.ReceiptMigrationError) as raised:
            cpd.recovery_info(prepared.assignment_id, self.paths)
        self.assertEqual(raised.exception.error_code, "spool_reconciliation_failed")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["send_attempt_total"], 1)
        self.assertEqual(receipt["turns"][-1]["status"], "prepared")

    def test_null_spool_journal_is_not_an_accepted_chunk_form(self) -> None:
        prepared = self.prepare("dispatch-chunk-null-journal-7319")
        self.submit(prepared, "native-user-chunk-null-journal-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-null-journal-1",
                assistant_message_id="native-assistant-chunk-null-journal-1",
                text=response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        raw["turns"][0]["chunk"]["spool_write_pending"] = None
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaises(cpd.ReceiptMigrationError) as raised:
            cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(raised.exception.error_code, "receipt_schema_unsupported")

    def test_non_0600_accepted_spool_blocks_continuation_arm(self) -> None:
        prepared = self.prepare("dispatch-chunk-permissions-7319")
        self.submit(prepared, "native-user-chunk-permissions-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-permissions-1",
                assistant_message_id="native-assistant-chunk-permissions-1",
                text=response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        spool = self.paths.spool_dir / prepared.assignment_id / "chunk-000001.part"
        spool.chmod(0o400)

        with self.assertRaises(cpd.StateError) as raised:
            cpd.arm_assignment(
                prepared.assignment_id,
                self.paths,
                turn_id=first.next_turn.turn_id,
            )
        self.assertEqual(raised.exception.error_code, "spool_path_invalid")
        persisted = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["send_attempt_total"], 1)
        self.assertEqual(persisted["turns"][-1]["status"], "prepared")

    def test_pending_journal_with_orphan_fails_before_reconciliation_advances(self) -> None:
        prepared = self.prepare("dispatch-chunk-pending-orphan-7319")
        self.submit(prepared, "native-user-chunk-pending-orphan-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        evidence = self.evidence(
            turn_id=prepared.turn_id or "",
            user_message_id="native-user-chunk-pending-orphan-1",
            assistant_message_id="native-assistant-chunk-pending-orphan-1",
            text=response,
        )
        original_write = transport._write_private_once

        def interrupted_after_rename(path: Path, payload: bytes) -> None:
            original_write(path, payload)
            raise OSError("simulated interruption after rename")

        with mock.patch.object(transport, "_write_private_once", interrupted_after_rename):
            with self.assertRaises(OSError):
                cpd.collect_turn(
                    prepared.assignment_id,
                    prepared.turn_id or "",
                    evidence,
                    paths=self.paths,
                )
        spool_root = self.paths.spool_dir / prepared.assignment_id
        orphan = spool_root / "chunk-999999.part"
        orphan.write_bytes(b"orphan")
        orphan.chmod(0o600)

        with self.assertRaises(cpd.StateError) as raised:
            cpd.collect_turn(
                prepared.assignment_id,
                prepared.turn_id or "",
                evidence,
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "spool_orphaned")
        crashed = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        self.assertIn("spool_write_pending", crashed["turns"][0]["chunk"])
        self.assertEqual(len(crashed["turns"]), 1)

    def test_truncated_later_chunk_retransmits_from_last_verified_boundary(self) -> None:
        prepared = self.prepare()
        self.submit(prepared, "native-user-chunk-1")
        first_response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-1",
                assistant_message_id="native-assistant-chunk-1",
                text=first_response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        second_turn = first.next_turn
        self.submit(second_turn, "native-user-chunk-2")
        truncated_prefix = self.response(
            prepared=second_turn,
            index=2,
            previous=str(first.accepted_chunk["chain_sha256"]),
            final=False,
            payload="partial",
        )
        with self.assertRaises(cpd.TruncationError) as raised:
            cpd.collect_turn(
                prepared.assignment_id,
                second_turn.turn_id or "",
                self.evidence(
                    turn_id=second_turn.turn_id or "",
                    user_message_id="native-user-chunk-2",
                    assistant_message_id="native-assistant-chunk-2",
                    text=truncated_prefix,
                    truncated=True,
                    outer_truncated=True,
                ),
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "collection_truncated")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        replacement = receipt["turns"][-1]
        self.assertEqual(receipt["turns"][1]["status"], "response_rejected")
        self.assertEqual(replacement["chunk"]["expected_index"], 2)
        self.assertEqual(
            replacement["chunk"]["expected_previous_chain_sha256"],
            first.accepted_chunk["chain_sha256"],
        )

    def test_orphan_spool_file_fails_closed_before_a_chunk_is_accepted(self) -> None:
        prepared = self.prepare("dispatch-orphan-7319")
        self.submit(prepared, "native-user-orphan-1")
        root = self.paths.spool_dir / prepared.assignment_id
        root.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        orphan = root / "chunk-999999.part"
        orphan.write_bytes(b"untrusted")
        orphan.chmod(0o600)
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=True,
            payload="accepted only if no orphan exists",
        )
        with self.assertRaises(cpd.StateError) as raised:
            cpd.collect_turn(
                prepared.assignment_id,
                prepared.turn_id or "",
                self.evidence(
                    turn_id=prepared.turn_id or "",
                    user_message_id="native-user-orphan-1",
                    assistant_message_id="native-assistant-orphan-1",
                    text=response,
                ),
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "spool_orphaned")

    def test_reassembly_is_exact_and_cleanup_requires_parent_restoration(self) -> None:
        prepared = self.prepare()
        self.submit(prepared, "native-user-chunk-1")
        payload_one = "first\n"
        first_response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload=payload_one,
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-1",
                assistant_message_id="native-assistant-chunk-1",
                text=first_response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        second_turn = first.next_turn
        self.submit(second_turn, "native-user-chunk-2")
        payload_two = "second"
        final_response = self.response(
            prepared=second_turn,
            index=2,
            previous=str(first.accepted_chunk["chain_sha256"]),
            final=True,
            payload=payload_two,
        )
        result = Path(self.temporary.name) / "result.md"
        outcome = cpd.collect_turn(
            prepared.assignment_id,
            second_turn.turn_id or "",
            self.evidence(
                turn_id=second_turn.turn_id or "",
                user_message_id="native-user-chunk-2",
                assistant_message_id="native-assistant-chunk-2",
                text=final_response,
            ),
            result,
            paths=self.paths,
        )
        expected = payload_one + payload_two
        self.assertEqual(outcome.sha256, hashlib.sha256(expected.encode("utf-8")).hexdigest())
        self.assertEqual(result.read_text(encoding="utf-8"), expected)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        with self.assertRaises(cpd.StateError) as raised:
            cpd.cleanup_result(prepared.assignment_id, paths=self.paths)
        self.assertEqual(raised.exception.error_code, "parent_not_restored")
        cpd.record_parent_restoration(prepared.assignment_id, restored=True, paths=self.paths)
        cleaned = cpd.cleanup_result(prepared.assignment_id, paths=self.paths)
        self.assertEqual(cleaned.removed_spool_files, 2)
        self.assertFalse((self.paths.spool_dir / prepared.assignment_id).exists())
        repeated = cpd.cleanup_result(prepared.assignment_id, paths=self.paths)
        self.assertEqual(repeated.removed_spool_files, 0)
        self.assertFalse((self.paths.spool_dir / prepared.assignment_id).exists())
        with self.assertRaises(cpd.StateError) as raised:
            cpd.materialize_result(
                prepared.assignment_id,
                Path(self.temporary.name) / "cleaned-result.md",
                paths=self.paths,
            )
        self.assertEqual(raised.exception.error_code, "spool_cleaned")
        self.assertFalse((self.paths.spool_dir / prepared.assignment_id).exists())

    def test_forged_cleanup_record_cannot_bypass_live_spool_integrity(self) -> None:
        prepared = self.prepare("dispatch-chunk-forged-cleanup-7319")
        self.submit(prepared, "native-user-chunk-forged-cleanup-1")
        response = self.response(
            prepared=prepared,
            index=1,
            previous=CHAIN_ZERO_HEX,
            final=False,
            payload="first",
        )
        first = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(
                turn_id=prepared.turn_id or "",
                user_message_id="native-user-chunk-forged-cleanup-1",
                assistant_message_id="native-assistant-chunk-forged-cleanup-1",
                text=response,
            ),
            paths=self.paths,
        )
        assert first.next_turn is not None
        spool = self.paths.spool_dir / prepared.assignment_id / "chunk-000001.part"
        spool.write_bytes(b"corrupt")
        spool.chmod(0o600)
        raw = json.loads(prepared.receipt_path.read_text(encoding="utf-8"))
        raw["delivery"].update(
            {
                "spool_cleanup_at": "2030-01-01T00:00:00.000Z",
                "spool_cleanup_count": 1,
            }
        )
        prepared.receipt_path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaises(cpd.ReceiptMigrationError) as raised:
            cpd.arm_assignment(
                prepared.assignment_id,
                self.paths,
                turn_id=first.next_turn.turn_id,
            )
        self.assertEqual(raised.exception.error_code, "receipt_schema_unsupported")


if __name__ == "__main__":
    unittest.main()
