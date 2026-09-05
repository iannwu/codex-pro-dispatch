from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import codex_pro_dispatch as cpd
from codex_pro_dispatch import transport


class LegacyMigrationTests(unittest.TestCase):
    """Keep the v1 safety cases explicit while schema-v2 changes shape."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = cpd.RuntimePaths(root / "config", root / "state")
        self.assignment_id = "legacy-migration-7319"
        self.worker_id = "worker-migration-7319"
        self.parent_id = "parent-migration-7319"
        cpd.save_worker(self.worker_id, confirm_pro=True, paths=self.paths)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def legacy(self, status: str, **overrides: object) -> dict[str, object]:
        sent = status in {"submitted", "pending", "indeterminate", "ambiguous"}
        value: dict[str, object] = {
            "schema_version": 1,
            "assignment_id": self.assignment_id,
            "status": status,
            "created_at": "2030-01-01T00:00:00.000Z",
            "updated_at": "2030-01-01T00:00:00.000Z",
            "worker_conversation_id": self.worker_id,
            "worker_label": "migration worker",
            "worker_model_confirmation": "user-confirmed-pro",
            "parent_task_id": self.parent_id,
            "prompt_sha256": "a" * 64,
            "wrapped_prompt_sha256": "b" * 64,
            "response_marker": cpd.result_marker(self.assignment_id),
            "submission_count": 1 if sent else 0,
            "no_resend": status == "armed" or sent,
            "outbound_prompt_verified": status in {"submitted", "pending"},
        }
        if sent:
            value.update(
                {
                    "sent_prompt_sha256": "c" * 64,
                    "native_user_message_id": "native-user-migration-7319",
                    "submitted_at": "2030-01-01T00:00:00.000Z",
                }
            )
        value.update(overrides)
        return value

    def write_legacy(self, value: dict[str, object]) -> Path:
        path = self.paths.assignments_dir / f"{self.assignment_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_every_unresolved_and_terminal_v1_root_state_has_an_explicit_mapping(self) -> None:
        expected = {
            "prepared": ("prepared", "prepared"),
            "armed": ("active", "armed"),
            "submitted": ("active", "submitted"),
            "pending": ("active", "pending"),
            "indeterminate": ("recoverable", "indeterminate"),
            "ambiguous": ("recoverable", "ambiguous"),
            "abandoned": ("abandoned", "failed"),
            "failed": ("failed", "failed"),
        }
        for legacy_status, (dispatch_status, turn_status) in expected.items():
            with self.subTest(legacy_status=legacy_status):
                migrated = transport._migrate_v1(
                    self.legacy(legacy_status), self.assignment_id
                )
                self.assertEqual(migrated["status"], dispatch_status)
                self.assertEqual(migrated["turns"][0]["status"], turn_status)
                self.assertEqual(migrated["legacy"]["legacy_status"], legacy_status)
                self.assertTrue(migrated["legacy"]["collection_evidence_required"])
                self.assertEqual(transport._validate_v2(migrated, self.assignment_id)["schema_version"], 2)

    def test_completed_v1_history_is_only_a_marker_only_projection(self) -> None:
        path = self.write_legacy(self.legacy("complete"))
        before = path.read_bytes()
        projected = cpd.load_assignment(self.assignment_id, self.paths)
        self.assertEqual(projected["legacy"]["completion_basis"], "marker-only")
        self.assertEqual(projected["legacy"]["collection_integrity"], "unverifiable")
        with self.assertRaises(cpd.ReceiptMigrationError):
            cpd.record_parent_restoration(self.assignment_id, restored=True, paths=self.paths)
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_response_only_collection_is_kept_as_audit_data_not_v2_completion(self) -> None:
        value = self.legacy(
            "submitted",
            collection_schema=cpd.NATIVE_COLLECTION_SCHEMA,
            assistant_message_id="assistant-legacy-7319",
            submitted_user_message_id="native-user-migration-7319",
            response_sha256="d" * 64,
            payload_sha256="e" * 64,
            raw_truncated="false",
            normalized_truncated=False,
        )
        migrated = transport._migrate_v1(value, self.assignment_id)
        turn = migrated["turns"][0]
        self.assertEqual(turn["collection"], {"status": "not_started"})
        self.assertIn("legacy_collection", turn)
        self.assertEqual(migrated["result"], {"status": "not_complete"})

    def test_legacy_unknown_state_and_unprovable_send_shape_fail_closed(self) -> None:
        with self.assertRaises(cpd.ReceiptMigrationError):
            transport._migrate_v1(self.legacy("invented"), self.assignment_id)
        unprovable = self.legacy("submitted")
        unprovable.pop("sent_prompt_sha256")
        with self.assertRaises(cpd.ReceiptMigrationError):
            transport._validate_v2(
                transport._migrate_v1(unprovable, self.assignment_id),
                self.assignment_id,
            )

    def test_legacy_cooldown_remains_effective_without_migrating_or_resending(self) -> None:
        started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        until = started + dt.timedelta(minutes=30)
        self.write_legacy(
            self.legacy(
                "abandoned",
                native_http_status=403,
                native_error_kind="openai-unusual-activity",
                cooldown_seconds=1800,
                cooldown_started_at=started.isoformat().replace("+00:00", "Z"),
                cooldown_until=until.isoformat().replace("+00:00", "Z"),
            )
        )
        cooldown = cpd.active_cooldown(self.paths, now=started + dt.timedelta(minutes=1))
        self.assertIsNotNone(cooldown)
        assert cooldown is not None
        self.assertEqual(cooldown["assignment_id"], self.assignment_id)
        with self.assertRaises(cpd.CooldownError):
            cpd.prepare_assignment(
                "new work",
                parent_task_id=self.parent_id,
                assignment_id="blocked-by-legacy-cooldown-7319",
                paths=self.paths,
            )

    def test_v1_prepared_state_migrates_under_lock_before_arming(self) -> None:
        self.write_legacy(self.legacy("prepared"))
        armed = cpd.arm_assignment(self.assignment_id, self.paths)
        self.assertEqual(armed["schema_version"], 2)
        self.assertEqual(armed["status"], "active")
        self.assertEqual(armed["turns"][0]["status"], "armed")
        self.assertTrue(armed["no_original_resend"])


if __name__ == "__main__":
    unittest.main()
