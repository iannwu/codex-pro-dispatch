"""Actual CLI and canonical locks against private fixtures, never native tools."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_pro_dispatch import core
from codex_pro_dispatch.queue import Queue

ROOT = Path(__file__).resolve().parents[1]


class QueuedResumeCheckTests(unittest.TestCase):
    def setUp(self):
        self.fresh()

    def fresh(self, confirm="--confirm-pro"):
        temporary = tempfile.TemporaryDirectory(prefix="pro-queued-resume-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve(strict=True)
        self.paths = core.RuntimePaths(self.home / "config", self.home / "state")
        self.env = dict(os.environ, CODEX_PRO_DISPATCH_HOME=str(self.home),
                        PYTHONDONTWRITEBYTECODE="1")
        self.rid = "queued-A2"
        self.worker = "unit-pro"
        self.client = "unit-client"
        self.prompt = "  Review résumé.\r\nKeep exact bytes.\n"
        self.raw_hash = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        self.fingerprint = hashlib.sha256(json.dumps(
            [self.prompt, self.client], ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        self.call("worker", "set", "--conversation-id", self.worker,
                  confirm, "--native-controls-confirmed")
        submitted = self.call(
            "queue", "submit", "--request-id", self.rid,
            "--client-session-id", self.client, "--prompt-file", "-",
            body=self.prompt.encode("utf-8"),
        )
        self.assertEqual(submitted["fingerprint"], self.fingerprint)
        self.record = self.paths.state_dir / "queue" / (self.rid + ".json")

    def call(self, *args, body=b"", code=0):
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin/pro-dispatch"), *args],
            input=body, capture_output=True, env=self.env, timeout=15,
        )
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        raw = result.stdout if code == 0 else result.stderr
        self.assertLessEqual(len(raw), 8192)
        value = json.loads(raw)
        self.assertEqual(value["ok"], code == 0)
        if code:
            self.assertEqual(result.stdout, b"")
            self.assertNotIn(self.prompt.encode("utf-8"), result.stderr)
        return value

    def check(self, code=0, **changes):
        values = {
            "rid": self.rid, "fingerprint": self.fingerprint,
            "worker": self.worker, "client": self.client, "raw_hash": self.raw_hash,
        }
        values.update(changes)
        return self.call(
            "queue", "resume-check", values["rid"],
            "--fingerprint", values["fingerprint"],
            "--worker-conversation-id", values["worker"],
            "--client-session-id", values["client"],
            "--raw-prompt-sha256", values["raw_hash"], code=code,
        )

    def stored(self):
        return {
            str(p.relative_to(self.home)): (p.stat().st_mode & 0o777, p.read_bytes())
            for p in self.home.rglob("*") if p.is_file() and not p.is_symlink()
        }

    def rejected(self, code=4, **changes):
        before = self.stored()
        result = self.check(code=code, **changes)
        self.assertEqual(self.stored(), before)
        return result

    def write_record(self, value, path=None):
        target = path or self.record
        target.write_text(json.dumps(value) + "\n", encoding="utf-8")
        target.chmod(0o600)

    def test_valid_check_is_read_only_and_repeatable(self):
        before = self.stored()
        first = self.check()
        self.assertEqual(first, self.check())
        self.assertTrue(first["resume_eligible"])
        self.assertTrue(first["assignment_absent"])
        self.assertFalse(first["send_authorized"])
        self.assertEqual(first["state"], "queued")
        self.assertEqual(first["fingerprint"], self.fingerprint)
        self.assertEqual(first["raw_prompt_sha256"], self.raw_hash)
        self.assertEqual(first["worker_model_confirmation"], "user-confirmed-pro")
        self.assertNotIn("prompt", first)
        self.assertNotIn("parent_task_id", first)
        self.assertFalse(core.assignment_path(self.rid, self.paths).exists())
        self.assertEqual(before, self.stored())
        queued = json.loads(self.record.read_text("utf-8"))
        self.assertEqual(queued["prompt"].encode("utf-8"), self.prompt.encode("utf-8"))

    def test_neutral_marker_worker_passes_and_other_markers_fail(self):
        self.fresh(confirm="--confirm-worker")
        first = self.check()
        self.assertTrue(first["resume_eligible"])
        self.assertEqual(first["worker_model_confirmation"], "user-confirmed-worker")
        self.rejected(worker="different-worker")
        worker_file = self.paths.worker_file
        record = json.loads(worker_file.read_text("utf-8"))
        for marker in ("user-confirmed-other", "", None, ["user-confirmed-worker"]):
            with self.subTest(marker=marker):
                value = dict(record, model_confirmation=marker)
                if marker is None:
                    del value["model_confirmation"]
                self.write_record(value, worker_file)
                result = self.rejected(code=2)
                self.assertEqual(result["error_type"], "ConfigurationError")
        self.write_record(record, worker_file)
        self.assertEqual(self.check(), first)

    def test_expectations_must_match(self):
        for changes in (
            {"rid": "missing-request"},
            {"worker": "different-pro"},
            {"client": "different-client"},
            {"fingerprint": "0" * 64},
            {"raw_hash": "1" * 64},
        ):
            with self.subTest(changes=changes):
                self.rejected(**changes)
        for changes in (
            {"rid": "../bad"},
            {"fingerprint": "not-a-hash"},
            {"raw_hash": "A" * 64},
        ):
            with self.subTest(changes=changes):
                self.rejected(code=2, **changes)

    def test_fingerprint_is_recomputed_and_claim_fields_are_rejected(self):
        original = json.loads(self.record.read_text("utf-8"))
        modified = dict(original, prompt=original["prompt"] + "altered")
        self.write_record(modified)
        self.rejected(raw_hash=hashlib.sha256(
            modified["prompt"].encode("utf-8")).hexdigest())
        modified = dict(original, client_session_id="altered-client")
        self.write_record(modified)
        self.rejected(client="altered-client")
        self.write_record(dict(original, queue_claim_token="leftover-claim"))
        self.rejected()

    def test_every_existing_receipt_state_blocks_resume(self):
        receipt = core.assignment_path(self.rid, self.paths)
        for status in sorted(core.ALL_STATUSES):
            with self.subTest(status=status):
                self.write_record({
                    "schema_version": 1, "assignment_id": self.rid,
                    "status": status, "created_at": core.utc_now(),
                }, receipt)
                self.rejected()

    def test_conflicts_cooldown_and_foreign_state_block(self):
        for kind, code in (
            ("active", 3), ("claimed", 3), ("cooldown", 6),
            ("foreign_directory", 4), ("foreign_receipt", 4),
            ("corrupt_history", 4),
        ):
            with self.subTest(kind=kind):
                self.fresh()
                if kind == "active":
                    core.prepare_assignment(
                        "other work", assignment_id="other-active",
                        parent_task_id="other-parent", paths=self.paths,
                    )
                elif kind == "claimed":
                    value = json.loads(self.record.read_text("utf-8"))
                    value.update(
                        request_id="other-claimed", state="claimed",
                        parent_task_id="other-parent",
                        worker_conversation_id=self.worker,
                        queue_claim_token="fixture-claim",
                        prompt_sha256=core.sha256_text(self.prompt.strip()),
                        wrapped_prompt_sha256=core.sha256_text(
                            core.wrap_prompt(self.prompt, "other-claimed")),
                        result_protocol=core.BOUNDED_RESULT_PROTOCOL,
                    )
                    self.write_record(
                        value, self.record.parent / "other-claimed.json")
                elif kind == "foreign_directory":
                    (self.paths.state_dir / "native-client").mkdir(mode=0o700)
                elif kind == "corrupt_history":
                    path = self.paths.assignments_dir / "corrupt.json"
                    path.write_text("not JSON", encoding="utf-8")
                    path.chmod(0o600)
                else:
                    value = {
                        "schema_version": 1, "assignment_id": "history",
                        "status": "complete", "created_at": core.utc_now(),
                    }
                    if kind == "cooldown":
                        value["cooldown_until"] = (
                            dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
                        ).isoformat()
                    else:
                        value["native_client"] = True
                    self.write_record(
                        value, self.paths.assignments_dir / "history.json")
                self.rejected(code=code)

    def test_temporary_or_ambiguous_storage_is_not_proof_of_absence(self):
        for kind in ("receipt_temporary", "duplicate_queue_key"):
            with self.subTest(kind=kind):
                self.fresh()
                if kind == "receipt_temporary":
                    path = self.paths.assignments_dir / (
                        "." + self.rid + ".json.fixture.tmp")
                    path.write_text("interrupted receipt write", encoding="utf-8")
                    path.chmod(0o600)
                else:
                    original = self.record.read_text("utf-8").rstrip()
                    self.record.write_text(
                        original[:-1] + ', "state": "queued"}\n',
                        encoding="utf-8",
                    )
                self.rejected(code=2)
                self.assertFalse(core.assignment_path(self.rid, self.paths).exists())

    def test_check_holds_one_canonical_lock(self):
        before = self.stored()
        with mock.patch.object(core, "state_lock", wraps=core.state_lock) as locked:
            result = Queue(self.paths).resume_check(
                self.rid, self.fingerprint, self.worker, self.client, self.raw_hash)
        self.assertTrue(result["resume_eligible"])
        acquisitions = [
            c for c in locked.call_args_list if c.kwargs.get("token") is None
        ]
        self.assertEqual(len(acquisitions), 1)
        self.assertIs(acquisitions[0].kwargs["create"], False)
        tokens = [
            c.kwargs["token"] for c in locked.call_args_list
            if c.kwargs.get("token") is not None
        ]
        self.assertTrue(tokens)
        self.assertTrue(all(token is tokens[0] for token in tokens))
        self.assertFalse(tokens[0].active)
        self.assertEqual(before, self.stored())


if __name__ == "__main__":
    unittest.main()
