"""Actual CLI and isolated authority tests for the reduced queue release."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_pro_dispatch import core
from codex_pro_dispatch.cli import build_parser
from codex_pro_dispatch.queue import Queue

ROOT = Path(__file__).resolve().parents[1]


class ReleaseQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pro-release-queue-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.root.chmod(0o700)
        self.home = self.root / "authority"
        self.paths = core.RuntimePaths(self.home / "config", self.home / "state")
        self.env = dict(
            os.environ,
            CODEX_PRO_DISPATCH_HOME=str(self.home),
            PYTHONPATH=str(ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.index = 0
        self.call("worker", "set", "--conversation-id", "unit-pro",
                  "--confirm-pro", "--native-controls-confirmed")

    def call(self, *args, code=0, body=None):
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin/pro-dispatch"), *args],
            input=body, text=True, capture_output=True,
            env=self.env, timeout=15,
        )
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        value = json.loads(result.stdout if code == 0 else result.stderr)
        self.assertEqual(value["ok"], code == 0)
        return value

    def submit(self, rid="unit-A", body="unit request\n", code=0):
        return self.call(
            "queue", "submit", "--request-id", rid,
            "--prompt-file", "-", "--client-session-id", "unit-client",
            body=body, code=code,
        )

    def test_check_submit_is_read_only_and_shares_exact_size_boundary(self):
        q = Queue(self.paths)
        overhead = len(core.wrap_prompt("x", "unit-A").encode("utf-16-le")) // 2 - 1
        before = self.authority_bytes()
        for units in (19999, 20000, 20001):
            body = "x" * (units - overhead)
            checked = self.call("queue", "check-submit", "--request-id", "unit-A",
                                body=body, code=0 if units < 20000 else 2)
            if units < 20000:
                self.assertTrue(checked["input_valid"])
                self.assertFalse(checked["send_authorized"])
            else:
                with self.assertRaisesRegex(core.ConfigurationError, "native read limit"):
                    q.submit("unit-A", body.encode())
            self.assertEqual(self.authority_bytes(), before)
        self.assertFalse(q.root.exists())
        for raw in (b"\xff", b" \n\t", ("😀" * 10000).encode()):
            with self.assertRaises(core.ConfigurationError):
                q.validate_submission("unit-A", raw)
        with self.assertRaises(core.ConfigurationError):
            q.validate_submission("../bad", b"valid")
        with self.assertRaises(core.ConfigurationError):
            q.validate_submission("unit-A", b"valid", "../bad")
        self.assertEqual(self.authority_bytes(), before)

    def test_check_submit_with_no_authority_creates_nothing(self):
        untouched = self.root / "untouched"
        self.env["CODEX_PRO_DISPATCH_HOME"] = str(untouched)
        checked = self.call("queue", "check-submit", "--request-id", "unit-A",
                            body="valid")
        self.assertTrue(checked["input_valid"])
        self.assertFalse(checked["send_authorized"])
        self.assertFalse(untouched.exists())

    def begin(self, arm=True):
        self.assertEqual(self.submit()["state"], "queued")
        result = self.call(
            "queue", "claim", "--request-id", "unit-A",
            "--parent-task-id", "unit-parent", "--native-controls-confirmed",
        )
        self.assertEqual(result["action"], "arm_then_send_once")
        self.assertFalse(result["send_authorized"])
        self.wrapped = result["wrapped_prompt"]
        if arm:
            self.call("arm", "unit-A")
        return result

    def document(self):
        return {
            "schemaVersion": 1,
            "thread": {
                "id": "unit-pro", "kind": "chatgpt",
                "status": {"type": "idle"},
            },
            "turns": [{
                "id": "unit-turn",
                "items": [
                    {
                        "id": "unit-turn", "type": "userMessage",
                        "content": [{"type": "text", "text": self.wrapped}],
                    },
                    {
                        "id": "unit-answer", "type": "agentMessage",
                        "text": core.result_marker("unit-A") + "\nunit answer\n"
                                + core.end_marker("unit-A"),
                    },
                ],
            }],
        }

    def evidence(self, document):
        self.index += 1
        path = self.root / ("history-" + str(self.index) + ".json")
        path.write_bytes(json.dumps(document).encode("utf-8"))
        path.chmod(0o600)
        return path

    def observe(self, document=None, code=0):
        args = [
            "queue", "observe", "unit-A",
            "--parent-task-id", "unit-parent", "--native-controls-confirmed",
        ]
        if document is not None:
            args += ["--native-read-file", str(self.evidence(document))]
        return self.call(*args, code=code)

    def records(self):
        return {
            str(p.relative_to(self.home)): p.read_bytes()
            for p in self.home.rglob("*.json")
        }

    def authority_bytes(self):
        return {
            str(p.relative_to(self.home)): p.read_bytes()
            for p in self.home.rglob("*") if p.is_file()
        }

    def set_worker(self, conversation="unit-new", expected="unit-pro", code=0):
        args = ["worker", "set", "--conversation-id", conversation,
                "--label", "Same chat name", "--confirm-pro", "--native-controls-confirmed"]
        if expected is not None:
            args += ["--expected-conversation-id", expected]
        return self.call(*args, code=code)

    def test_guarded_worker_replacement_and_idempotence(self):
        self.assertEqual(core.load_worker(self.paths).conversation_id, "unit-pro")
        self.assertFalse((self.paths.state_dir / "queue").exists())
        before = self.authority_bytes()
        for target in ("unit-pro", "unit-new"):
            for expected, code in ((None, 2), ("stale-pro", 4)):
                with self.subTest(target=target, expected=expected):
                    value = self.set_worker(target, expected, code)
                    self.assertEqual(value["error_type"],
                                     "ConfigurationError" if code == 2 else "StateError")
                    self.assertEqual(self.authority_bytes(), before)
        self.assertEqual(self.set_worker()["worker"]["conversation_id"], "unit-new")
        after = self.authority_bytes()
        self.assertEqual(
            {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)},
            {"config/worker.json"},
        )
        current = core.load_worker(self.paths)
        with core.state_lock(self.paths) as locked:
            with patch.object(core, "utc_now", side_effect=AssertionError("Timestamp changed")):
                retry = core.save_worker(
                    "unit-new", expected_conversation_id="unit-new",
                    label="Ignored label change", confirm_pro=True,
                    paths=self.paths, _locked=locked,
                )
        self.assertEqual(retry, current)
        self.assertEqual(self.authority_bytes(), after)
        self.set_worker(code=4)  # Same requested ID still rejects stale expected old ID.
        self.assertEqual(self.authority_bytes(), after)
        self.set_worker("unit-new", "unit-new")
        self.assertEqual(self.authority_bytes(), after)

    def test_replacement_refuses_active_and_same_id_reaffirmation(self):
        self.begin(arm=False)
        for status in ("prepared", "armed"):
            if status == "armed":
                self.call("arm", "unit-A")
            before = self.authority_bytes()
            for target in ("unit-pro", "unit-new"):
                value = self.set_worker(target, code=3)
                self.assertEqual(value["error_type"], "BusyError")
                self.assertEqual(value["details"]["assignment_id"], "unit-A")
                self.assertEqual(value["details"]["status"], status)
                self.assertEqual(self.authority_bytes(), before)

    def test_orphan_and_abandoned_but_unreleased_claim_block_replacement(self):
        self.submit()
        queue = Queue(self.paths)
        with patch.object(core, "prepare_assignment", side_effect=OSError("Fixture fault")):
            with self.assertRaises(OSError):
                queue.claim("unit-parent", True, "unit-A",
                            expected_worker_conversation_id="unit-pro")
        self.assertFalse(core.assignment_path("unit-A", self.paths).exists())
        orphan = queue.load("unit-A")
        self.assertEqual(orphan["state"], "claimed")
        self.assertEqual(orphan["worker_conversation_id"], "unit-pro")
        before = self.authority_bytes()
        for target in ("unit-pro", "unit-new"):
            value = self.set_worker(target, code=3)
            self.assertEqual(value["details"]["request_id"], "unit-A")
            self.assertEqual(self.authority_bytes(), before)
        queue.claim("unit-parent", True, "unit-A",
                    expected_worker_conversation_id="unit-pro")
        self.call("abandon", "unit-A", "--reason", "Synthetic abandonment")
        before = self.authority_bytes()
        value = self.set_worker(code=3)
        self.assertEqual(value["details"]["request_id"], "unit-A")
        self.assertEqual(self.authority_bytes(), before)
        self.call("queue", "release", "unit-A", "--parent-task-id", "unit-parent")
        receipt = core.assignment_path("unit-A", self.paths).read_bytes()
        self.set_worker()
        self.assertEqual(core.assignment_path("unit-A", self.paths).read_bytes(), receipt)

    def test_malformed_queue_is_not_treated_as_an_empty_queue(self):
        self.submit()
        Queue(self.paths).path("unit-A").write_text("not json", encoding="utf-8")
        before = self.authority_bytes()
        value = self.set_worker(code=4)
        self.assertEqual(value["error_type"], "StateError")
        self.assertEqual(self.authority_bytes(), before)

    def test_duplicate_queue_keys_fail_closed(self):
        self.submit()
        path = Queue(self.paths).path("unit-A")
        original = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_text(original[:-1] + ', "state": "claimed"}\n', encoding="utf-8")
        before = self.authority_bytes()
        value = self.set_worker(code=4)
        self.assertEqual(value["error_type"], "StateError")
        self.assertEqual(self.authority_bytes(), before)

    def expected_claim(self, expected, code=0):
        args = ["queue", "claim", "--request-id", "unit-A",
                "--parent-task-id", "unit-parent", "--native-controls-confirmed"]
        if expected is not None:
            args += ["--expected-worker-conversation-id", expected]
        return self.call(*args, code=code)

    def test_initial_expectation_and_malformed_worker_fail_closed(self):
        self.paths.worker_file.unlink()  # Isolated fixture setup, not runtime recovery.
        before = self.authority_bytes()
        self.set_worker(code=4)
        self.assertEqual(self.authority_bytes(), before)
        self.set_worker("unit-first", None)
        self.assertEqual(core.load_worker(self.paths).conversation_id, "unit-first")
        self.paths.worker_file.write_text("not json", encoding="utf-8")
        before = self.authority_bytes()
        for expected in (None, "unit-first"):
            self.set_worker("unit-new", expected, code=2)
            self.assertEqual(self.authority_bytes(), before)

    def test_replacement_wins_then_stale_session_cannot_claim(self):
        self.submit()
        queued = Queue(self.paths).path("unit-A").read_bytes()
        self.set_worker()
        self.assertEqual(Queue(self.paths).path("unit-A").read_bytes(), queued)
        before = self.authority_bytes()
        rejected = self.expected_claim("unit-pro", code=4)
        self.assertEqual(rejected["error_type"], "StateError")
        self.assertEqual(rejected["details"]["worker_conversation_id"], "unit-new")
        self.assertEqual(self.authority_bytes(), before)
        self.assertFalse(core.assignment_path("unit-A", self.paths).exists())
        result = self.expected_claim("unit-new")
        self.assertEqual(result["worker_conversation_id"], "unit-new")
        self.assertEqual(result["action"], "arm_then_send_once")
        receipt = core.load_assignment("unit-A", self.paths)
        self.assertEqual(receipt["worker_conversation_id"], "unit-new")
        self.assertEqual(receipt["parent_task_id"], "unit-parent")
        self.assertEqual(receipt["submission_count"], 0)
        saved = self.authority_bytes()
        self.assertEqual(self.expected_claim("unit-new"), result)
        self.assertEqual(self.authority_bytes(), saved)

    def test_claim_snapshot_blocks_replacement_before_receipt_creation(self):
        self.submit()
        queue = Queue(self.paths)
        original = core.prepare_assignment

        def at_receipt_boundary(*args, **kwargs):
            locked = kwargs["_locked"]
            locked.validate(self.paths)
            claim = queue.load("unit-A", _locked=locked)
            self.assertEqual(claim["worker_conversation_id"], "unit-pro")
            before = self.authority_bytes()
            with self.assertRaises(core.BusyError) as caught:
                core.save_worker(
                    "unit-new", expected_conversation_id="unit-pro",
                    confirm_pro=True, paths=self.paths, _locked=locked,
                )
            self.assertEqual(caught.exception.details["request_id"], "unit-A")
            self.assertEqual(self.authority_bytes(), before)
            return original(*args, **kwargs)

        with patch.object(core, "prepare_assignment", side_effect=at_receipt_boundary) as prepare:
            result = queue.claim("unit-parent", True, "unit-A",
                                 expected_worker_conversation_id="unit-pro")
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(result["worker_conversation_id"], "unit-pro")
        self.assertEqual(core.load_assignment("unit-A", self.paths)["worker_conversation_id"],
                         "unit-pro")

    def test_existing_and_receiptless_claims_validate_expected_and_saved_workers(self):
        self.submit()
        queue = Queue(self.paths)
        with patch.object(core, "prepare_assignment", side_effect=OSError("Fixture fault")):
            with self.assertRaises(OSError):
                queue.claim("unit-parent", True, "unit-A",
                            expected_worker_conversation_id="unit-pro")
        bound = queue.load("unit-A")
        original = self.paths.worker_file.read_bytes()
        changed = json.loads(original)
        changed["conversation_id"] = "unit-new"
        for established in (False, True):
            with self.subTest(receipt_established=established):
                if established:
                    self.expected_claim("unit-pro")
                    self.call("arm", "unit-A")
                # Deliberate isolated configuration drift: never a recovery procedure.
                self.paths.worker_file.write_text(json.dumps(changed), encoding="utf-8")
                before = self.authority_bytes()
                for expected in ("unit-pro", "unit-new"):
                    self.expected_claim(expected, code=4)
                    self.assertEqual(self.authority_bytes(), before)
                if established:
                    legacy = self.expected_claim(None)
                    self.assertEqual(legacy["action"], "collect_only")
                    self.assertEqual(legacy["worker_conversation_id"], "unit-pro")
                else:
                    self.expected_claim(None, code=4)
                    self.assertFalse(core.assignment_path("unit-A", self.paths).exists())
                self.assertEqual(self.authority_bytes(), before)
                self.paths.worker_file.write_bytes(original)
        saved = self.authority_bytes()
        self.assertEqual(self.expected_claim("unit-pro")["action"], "collect_only")
        self.assertEqual(self.authority_bytes(), saved)
        current = queue.load("unit-A")
        for key in ("parent_task_id", "worker_conversation_id", "queue_claim_token",
                    "prompt_sha256", "wrapped_prompt_sha256", "result_protocol"):
            self.assertEqual(current[key], bound[key])

    def test_reduced_parser_and_dependency_closure(self):
        parser = build_parser()
        choices = next(
            a.choices for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        self.assertIn("queue", choices)
        for name in ("request", "request-status", "request-collect", "ack", "native-broker"):
            self.assertNotIn(name, choices)
        package = ROOT / "src" / "codex_pro_dispatch"
        self.assertEqual(
            {p.name for p in package.glob("native_*.py")},
            {"native_storage.py"},
        )
        self.assertEqual(self.call("queue", "status")["requests"], [])
        self.assertEqual(self.call("status")["worker"]["conversation_id"], "unit-pro")

    def test_publication_collection_and_tombstones(self):
        self.begin()
        result = self.observe(self.document())
        answer = result["answer"]
        self.assertEqual(result["observation"], "published")
        self.assertEqual(answer["payload"], "unit answer")
        self.assertEqual(answer["verification_level"], "bounded_native_summary")
        self.assertTrue(answer["outbound_prompt_verified"])
        self.assertFalse(answer["source_bytes_verified"])
        self.assertFalse(answer["generation_finality_verified"])
        receipt = self.call("status", "unit-A")["assignment"]
        self.assertEqual(receipt["submission_count"], 1)
        self.assertEqual(receipt["native_collection"]["assistant_message_id"], "unit-answer")
        saved = self.records()
        self.assertEqual(self.observe()["answer"], answer)
        self.assertEqual(self.call("queue", "collect", "unit-A")["answer"], answer)
        self.assertEqual(self.submit()["state"], "published")
        self.assertEqual(saved, self.records())
        self.call("queue", "acknowledge", "unit-A")
        self.call("queue", "acknowledge", "unit-A")
        self.assertFalse(self.call("queue", "collect", "unit-A")["body_available"])
        self.assertEqual(self.submit()["state"], "acknowledged")
        self.submit(body="different content", code=4)
        self.observe(self.document(), code=4)

    def test_pending_rejection_and_private_evidence(self):
        self.begin(arm=False)
        before = self.records()
        self.observe(self.document(), code=4)
        self.assertEqual(before, self.records())
        self.call("arm", "unit-A")
        before = self.records()
        self.assertEqual(self.observe(code=4)["error"], "No native snapshot or staged history")
        for kind in ("running", "missing", "worker", "prompt", "truncated"):
            document = self.document()
            if kind == "running":
                document["thread"]["status"]["type"] = "running"
                document["turns"] = []  # No current outbound evidence yet.
            elif kind == "missing":
                document["turns"] = []
            elif kind == "worker":
                document["thread"]["id"] = "wrong-worker"
            elif kind == "prompt":
                document["turns"][0]["items"][0]["content"][0]["text"] += "x"
            elif kind == "truncated":
                document["turns"][0]["items"][0]["truncated"] = True
            with self.subTest(kind=kind):
                if kind in {"running", "missing"}:
                    self.assertEqual(self.observe(document)["observation"], "pending")
                else:
                    self.observe(document, code=4 if kind == "prompt" else 5)
                self.assertEqual(before, self.records())
        path = self.evidence(self.document())
        path.chmod(0o640)
        for operation in ("observe", "publish"):
            self.call(
                "queue", operation, "unit-A", "--parent-task-id", "unit-parent",
                "--native-controls-confirmed", "--native-read-file", str(path),
                code=2,
            )
        self.assertEqual(before, self.records())

    def test_interrupted_publication_recovers_staged_bytes(self):
        self.begin()
        raw = json.dumps(self.document()).encode("utf-8")
        with patch.object(core, "complete_assignment", side_effect=OSError("unit fault")):
            with self.assertRaises(OSError):
                Queue(self.paths).observe(
                    "unit-A", "unit-parent", confirmed=True, native_read=raw
                )
        self.assertEqual(self.call("queue", "status", "unit-A")["state"], "claimed")
        self.assertEqual(
            self.call("status", "unit-A")["assignment"]["submission_count"], 1
        )
        result = self.observe()
        self.assertEqual(result["answer"]["payload"], "unit answer")
        self.assertEqual(result["observation"], "published")
        self.assertEqual(
            self.call("status", "unit-A")["assignment"]["submission_count"], 1
        )

    def complete_core_only(self):
        """Core completes from native history while the queue stage is lost."""
        self.begin()
        raw = json.dumps(self.document()).encode("utf-8")
        core.mark_submitted("unit-A", self.wrapped, self.paths)
        core.complete_assignment("unit-A", b"", self.paths, native_read=raw)
        receipt = self.call("status", "unit-A")["assignment"]
        self.assertEqual(receipt["status"], "complete")
        record = self.call("queue", "status", "unit-A")
        self.assertEqual(record["state"], "claimed")
        self.assertEqual(record["dispatch_status"], "complete")
        return receipt

    def test_complete_receipt_without_stage_publishes_matching_history(self):
        receipt = self.complete_core_only()
        before = self.records()
        # Without any history the runner is told to fetch one read-only.
        self.assertEqual(self.observe(code=4)["error"], "No native snapshot or staged history")
        self.assertEqual(before, self.records())
        result = self.observe(self.document())
        self.assertEqual(result["observation"], "published")
        self.assertEqual(result["answer"]["payload"], "unit answer")
        self.assertEqual(self.call("status", "unit-A")["assignment"], receipt)
        self.assertEqual(self.call("queue", "collect", "unit-A")["answer"], result["answer"])
        saved = self.records()
        self.assertEqual(self.observe()["answer"], result["answer"])
        self.assertEqual(self.observe(self.document())["answer"], result["answer"])
        self.assertEqual(saved, self.records())

    def test_complete_receipt_prevalidation_completes_once(self):
        self.complete_core_only()
        raw = json.dumps(self.document()).encode("utf-8")
        real, calls = core.complete_assignment, []

        def counted(*args, **kwargs):
            calls.append(kwargs.get("native_read"))
            return real(*args, **kwargs)

        # Prevalidation of the complete receipt is reused; the same validation
        # never runs twice against the same bytes on this branch.
        with patch.object(core, "complete_assignment", counted):
            published = Queue(self.paths).publish(
                "unit-A", "unit-parent", confirmed=True, native_read=raw)
        self.assertEqual(calls, [raw])
        self.assertEqual(published["answer"]["payload"], "unit answer")
        self.assertEqual(self.call("status", "unit-A")["assignment"]["status"], "complete")

    def test_conflicting_history_never_stages_against_a_complete_receipt(self):
        receipt = self.complete_core_only()
        before = self.records()
        for kind in ("response", "assistant_id", "turn_id"):
            document = self.document()
            if kind == "response":
                document["turns"][0]["items"][1]["text"] = (
                    core.result_marker("unit-A") + "\nother answer\n" + core.end_marker("unit-A")
                )
            elif kind == "assistant_id":
                document["turns"][0]["items"][1]["id"] = "other-answer"
            else:
                document["turns"][0]["id"] = document["turns"][0]["items"][0]["id"] = "other-turn"
            with self.subTest(kind=kind):
                for operation in ("observe", "publish"):
                    self.call(
                        "queue", operation, "unit-A", "--parent-task-id", "unit-parent",
                        "--native-controls-confirmed",
                        "--native-read-file", str(self.evidence(document)), code=4,
                    )
                self.assertEqual(before, self.records())
        self.assertEqual(self.call("status", "unit-A")["assignment"], receipt)
        self.assertEqual(self.observe(self.document())["observation"], "published")
        self.call("queue", "acknowledge", "unit-A")
        self.assertFalse(self.call("queue", "collect", "unit-A")["body_available"])
        self.observe(self.document(), code=4)
        self.assertEqual(self.call("status", "unit-A")["assignment"], receipt)

    def test_interruption_after_each_boundary_recovers_without_a_second_arm(self):
        claim = ["queue", "claim", "--request-id", "unit-A",
                 "--parent-task-id", "unit-parent", "--native-controls-confirmed"]
        self.begin()  # Interrupted after arm: the counter is still zero.
        self.assertEqual(self.call(*claim)["action"], "collect_only")
        self.assertEqual(self.call("status", "unit-A")["assignment"]["submission_count"], 0)
        self.call("arm", "unit-A", code=4)
        self.assertEqual(self.observe(code=4)["error"], "No native snapshot or staged history")
        core.mark_submitted("unit-A", self.wrapped, self.paths)  # Interrupted after send.
        self.assertEqual(self.call(*claim)["action"], "collect_only")
        raw = json.dumps(self.document()).encode("utf-8")
        with patch.object(core, "complete_assignment", side_effect=OSError("unit fault")):
            with self.assertRaises(OSError):  # Interrupted after staging.
                Queue(self.paths).publish("unit-A", "unit-parent", confirmed=True, native_read=raw)
        self.assertEqual(self.call(*claim)["action"], "collect_only")
        core.complete_assignment("unit-A", b"", self.paths, native_read=raw)  # After core completion.
        self.assertEqual(self.call(*claim)["action"], "collect_only")
        result = self.observe()
        self.assertEqual(result["observation"], "published")
        receipt = self.call("status", "unit-A")["assignment"]
        self.assertEqual(receipt["submission_count"], 1)
        self.assertTrue(receipt["no_resend"])

    def test_busy_no_resend_cooldown_and_purge(self):
        self.begin()
        self.call("arm", "unit-A", code=4)
        self.submit("unit-B")
        self.call(
            "queue", "claim", "--request-id", "unit-B",
            "--parent-task-id", "unit-parent", "--native-controls-confirmed",
            code=3,
        )
        self.call(
            "prepare", "--parent-task-id", "unit-parent",
            "--assignment-id", "unit-C", "--native-controls-confirmed",
            body="another request", code=3,
        )
        self.call("queue", "cancel", "unit-A", code=4)
        self.call("purge", "--yes", "--force", code=4)
        self.call("unusual-activity", "unit-A", "--reason", "unit unusual activity")
        self.call("abandon", "unit-A", "--reason", "unit authorized abandonment")
        self.call("queue", "release", "unit-A", "--parent-task-id", "unit-parent")
        self.call(
            "prepare", "--parent-task-id", "unit-parent",
            "--assignment-id", "unit-C", "--native-controls-confirmed",
            body="another request", code=6,
        )
        self.call(
            "queue", "claim", "--request-id", "unit-B",
            "--parent-task-id", "unit-parent", "--native-controls-confirmed",
            code=6,
        )
        self.assertFalse(core.assignment_path("unit-B", self.paths).exists())
        self.assertFalse(core.assignment_path("unit-C", self.paths).exists())
        self.assertIsNotNone(self.call("status")["active_cooldown"])

    def test_foreign_state_refuses_mutation_without_loading_handlers(self):
        self.begin()
        foreign = self.paths.state_dir / "native-client"
        foreign.mkdir(mode=0o700)
        reservation = foreign / "reservation.json"
        reservation.write_text('{"uninterpreted":"foreign"}\n', encoding="utf-8")
        reservation.chmod(0o600)
        before = self.records()
        self.assertEqual(self.call("status")["active_assignment"]["status"], "armed")
        self.submit("unit-B", code=4)
        self.observe(self.document(), code=4)
        self.call("worker", "reset", "--force", code=4)
        self.call("purge", "--yes", "--force", code=4)
        self.assertEqual(before, self.records())
        self.assertFalse(core.assignment_path("unit-B", self.paths).exists())


if __name__ == "__main__":
    unittest.main()
