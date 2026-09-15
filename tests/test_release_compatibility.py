"""Fixture compatibility must not weaken runtime authority protection."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_pro_dispatch import core


class ReleaseCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pro-compatibility-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.home = self.root / "authority"
        self.paths = core.RuntimePaths(
            self.home / "config", self.home / "state"
        )
        core.save_worker(
            "compatibility-pro", confirm_pro=True, paths=self.paths
        )

    def records(self):
        return {
            str(path.relative_to(self.home)): path.read_bytes()
            for path in self.home.rglob("*.json")
        }

    def test_runtime_authority_alias_still_rejected_without_mutation(self):
        alias = self.root / "authority-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        aliased_paths = core.RuntimePaths(
            alias / "config", alias / "state"
        )
        before = self.records()
        with self.assertRaises(core.ConfigurationError):
            core.prepare_assignment(
                "must not be prepared through an alias",
                assignment_id="compatibility-blocked",
                parent_task_id="compatibility-parent",
                paths=aliased_paths,
            )
        with self.assertRaises(core.ConfigurationError):
            core.save_worker(
                "replacement-pro", confirm_pro=True, paths=aliased_paths
            )
        self.assertEqual(self.records(), before)
        self.assertFalse(
            core.assignment_path(
                "compatibility-blocked", self.paths
            ).exists()
        )
        self.assertEqual(
            core.load_worker(self.paths).conversation_id, "compatibility-pro"
        )

    def test_internal_fixture_write_requires_live_lock_token(self):
        prepared = core.prepare_assignment(
            "fixture only",
            assignment_id="compatibility-lock",
            parent_task_id="compatibility-parent",
            paths=self.paths,
        )
        receipt = core.load_assignment(prepared.assignment_id, self.paths)
        before = self.records()
        with self.assertRaises(TypeError):
            core._save_assignment(
                prepared.assignment_id, receipt, self.paths
            )
        self.assertEqual(self.records(), before)

        with core.state_lock(self.paths) as locked:
            receipt["last_error_kind"] = "unit-fixture"
            core._save_assignment(
                prepared.assignment_id, receipt, self.paths, _locked=locked
            )
            stored = core.load_assignment(
                prepared.assignment_id, self.paths, _locked=locked
            )
            self.assertEqual(stored["last_error_kind"], "unit-fixture")
            with self.assertRaises(core.StateError):
                core.load_assignment(prepared.assignment_id, self.paths)

        saved = self.records()
        with self.assertRaises(core.StateError):
            core._save_assignment(
                prepared.assignment_id, receipt, self.paths, _locked=locked
            )
        self.assertEqual(self.records(), saved)

    def refused_cli(self, *args, input_text=""):
        launcher = Path(__file__).resolve().parents[1] / "bin/pro-dispatch"
        environment = dict(
            os.environ,
            CODEX_PRO_DISPATCH_HOME=str(self.home),
            PYTHONDONTWRITEBYTECODE="1",
        )
        result = subprocess.run(
            [sys.executable, str(launcher), *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=environment,
            timeout=15,
        )
        self.assertEqual(result.returncode, 4, result.stderr or result.stdout)
        self.assertEqual(result.stdout, "")
        error = json.loads(result.stderr)
        self.assertFalse(error["ok"])
        self.assertEqual(error["error_type"], "StateError")

    def test_corruption_blocks_cli_force_and_preserves_armed_identity(self):
        prepared = core.prepare_assignment(
            "synthetic armed fixture",
            assignment_id="compatibility-armed",
            parent_task_id="compatibility-parent",
            paths=self.paths,
        )
        core.arm_assignment(prepared.assignment_id, self.paths)
        broken = self.paths.assignments_dir / "unreadable.json"
        broken.write_text("not json", encoding="utf-8")
        broken.chmod(0o600)
        before = self.records()

        for arguments in (
            ("worker", "reset"),
            ("worker", "reset", "--force"),
            ("purge", "--yes"),
            ("purge", "--yes", "--force"),
        ):
            with self.subTest(arguments=arguments):
                self.refused_cli(*arguments)
                self.assertEqual(self.records(), before)

        self.refused_cli(
            "prepare",
            "--assignment-id", "compatibility-new",
            "--parent-task-id", "compatibility-parent",
            "--native-controls-confirmed",
            input_text="must remain blocked",
        )
        self.refused_cli("arm", prepared.assignment_id)
        self.assertEqual(self.records(), before)
        self.assertFalse(
            core.assignment_path("compatibility-new", self.paths).exists()
        )
        # Exact-ID inspection remains possible for this intact receipt.
        receipt = core.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["status"], "armed")
        self.assertEqual(receipt["submission_count"], 0)
        self.assertTrue(receipt["no_resend"])
        self.assertEqual(receipt["parent_task_id"], "compatibility-parent")

    def test_mixed_foreign_and_corrupt_receipts_survive_forced_cleanup(self):
        prepared = core.prepare_assignment(
            "synthetic foreign fixture",
            assignment_id="compatibility-foreign",
            parent_task_id="compatibility-parent",
            paths=self.paths,
        )
        core.arm_assignment(prepared.assignment_id, self.paths)
        receipt = core.load_assignment(prepared.assignment_id, self.paths)
        receipt["native_client"] = True
        # Intentional fixture injection, not an application recovery operation.
        prepared.receipt_path.write_text(
            json.dumps(receipt) + "\n", encoding="utf-8"
        )
        broken = self.paths.assignments_dir / "unreadable.json"
        broken.write_text('{"native_client":true,', encoding="utf-8")
        broken.chmod(0o600)
        self.assertFalse((self.paths.state_dir / "native-client").exists())
        before = self.records()

        for arguments in (
            ("worker", "reset", "--force"),
            ("purge", "--yes", "--force"),
        ):
            with self.subTest(arguments=arguments):
                self.refused_cli(*arguments)
                self.assertEqual(self.records(), before)

        preserved = core.load_assignment(prepared.assignment_id, self.paths)
        self.assertTrue(preserved["native_client"])
        self.assertTrue(preserved["no_resend"])
        self.assertEqual(preserved["status"], "armed")
        self.assertEqual(preserved["submission_count"], 0)


if __name__ == "__main__":
    unittest.main()
