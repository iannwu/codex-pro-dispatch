import concurrent.futures
import contextlib
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_pro_dispatch import core, resident
from codex_pro_dispatch.queue import Queue


class ResidentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.paths = core.RuntimePaths(root / "config", root / "state")
        core.save_worker("worker", confirm_pro=True, paths=self.paths)
        self.q = Queue(self.paths)
        self.initial = dict(generation=0, owner="original", parent="parent", worker="worker")
        self.assertEqual(self.call("start", self.initial)["reason"], "enrollment_required")
        proof = root / "qualification.json"
        raw = json.dumps(dict(kind="fresh_deployment", parent="parent", worker="worker",
                             config_dir=str(self.paths.config_dir), state_dir=str(self.paths.state_dir),
                             implementation="unit-fixture", observations="New isolated test authority, no native host. " + "? ! " * 3500,
                             authorization="Test harness only")).encode()
        proof.write_bytes(raw)
        proof.chmod(0o600)
        self.initial.update(evidence_file=str(proof), evidence_sha256=hashlib.sha256(raw).hexdigest())
        self.c = self.call("enroll", self.initial)["owner"]
        self.assertLess(len(json.dumps(self.c)), 512)

    def call(self, action, c=None):
        return resident.control(action, c or self.c, self.paths)

    def next(self):
        return self.call("start", {**self.c, "owner": "replacement"})

    def begin(self, rid="request"):
        self.q.submit(rid, b"answer this", "client")
        self.c = {**self.c, "invocation": "original-execution", "request": rid}
        return self.call("begin")

    @contextlib.contextmanager
    def authorized(self):
        token = resident.invocation.set(self.c)
        try:
            yield
        finally:
            resident.invocation.reset(token)

    def claim(self):
        return self.q.claim("parent", True, self.c["request"], expected_worker_conversation_id="worker")

    def test_replacement_wins_no_stale_begin_or_claim(self):
        self.assertEqual(self.next()["state"], "ready")
        with self.assertRaises(core.StateError):
            self.begin()
        with self.authorized(), self.assertRaises(core.BusyError):
            self.claim()

    def test_reservation_covers_prearm_and_native_tail(self):
        self.begin()
        self.assertEqual(self.next()["state"], "busy")
        with self.authorized():
            self.claim()
            core.arm_assignment("request", self.paths)
        self.assertEqual(self.next()["state"], "busy")
        with self.assertRaises(core.StateError):
            self.call("end", {**self.c, "invocation": "other-execution"})
        self.assertEqual(self.next()["state"], "busy")

    def test_collect_only_after_original_final_continuation(self):
        self.begin()
        with self.authorized():
            self.claim()
            core.arm_assignment("request", self.paths)
        self.call("end")
        new = self.next()
        self.assertEqual(new["state"], "collect_only")
        self.assertEqual(new["request_id"], "request")
        self.c = {**new["owner"], "invocation": "collector", "request": "request"}
        self.call("begin")
        with self.authorized():
            self.assertEqual(self.claim()["action"], "collect_only")
            with self.assertRaises(core.StateError):
                core.arm_assignment("request", self.paths)

    def test_prepared_same_id_resume(self):
        self.begin()
        with self.authorized():
            first = self.claim()
        self.call("end")
        new = self.next()
        self.assertEqual(new["state"], "ready")
        self.assertEqual(new["request_id"], "request")
        self.c = {**new["owner"], "invocation": "resumed", "request": "request"}
        self.call("begin")
        with self.authorized():
            self.assertEqual(self.claim()["wrapped_prompt"], first["wrapped_prompt"])

    def test_unowned_direct_arm_and_configuration_rejected(self):
        self.begin()
        with self.authorized():
            self.claim()
        for action in [lambda: core.arm_assignment("request", self.paths),
                       lambda: core.reset_worker(force=True, paths=self.paths),
                       lambda: core.purge_local_state(force=True, paths=self.paths),
                       lambda: core.save_worker("other", confirm_pro=True,
                                                expected_conversation_id="worker", paths=self.paths),
                       lambda: self.q.release("request", "parent")]:
            with self.assertRaises(core.BusyError):
                action()
        self.assertEqual(core.load_assignment("request", self.paths)["status"], "prepared")

    def test_client_commands_still_work_during_reservation(self):
        self.begin()
        self.q.submit("second", b"other prompt", "client")
        self.assertEqual(self.q.collect("second")["state"], "queued")
        self.assertEqual(self.q.collect("request")["state"], "queued")

    def test_bad_record_blocks_without_mutation(self):
        path = self.paths.state_dir / "resident-owner.json"
        with core.state_lock(self.paths) as lock:
            core.atomic_write_json(path, {"version": True}, _locked=lock)
        before = path.read_bytes()
        with self.assertRaises(core.StateError):
            self.next()
        self.assertEqual(before, path.read_bytes())

    def test_begin_and_replace_race_one_winner(self):
        self.q.submit("request", b"answer this", "client")
        self.c = {**self.c, "invocation": "old", "request": "request"}
        barrier = threading.Barrier(2)
        def begin():
            barrier.wait()
            try:
                self.call("begin")
                return "began"
            except core.StateError:
                return "stale"
        def replace():
            barrier.wait()
            return self.next()["state"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(begin), pool.submit(replace)
            self.assertIn((a.result(), b.result()), [("began", "busy"), ("stale", "ready")])

    def test_concurrent_starts_one_winner(self):
        barrier = threading.Barrier(2)
        def start():
            barrier.wait()
            return self.next()["state"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: start(), range(2))), ["busy", "ready"])

    def test_context_cannot_claim_different_parent(self):
        self.begin()
        with self.authorized(), self.assertRaises(core.StateError):
            self.q.claim("another-parent", True, "request")

    def test_queued_request_survives_replacement(self):
        self.q.submit("request", b"answer this", "client")
        new = self.next()
        self.assertEqual((new["state"], new["request_id"]), ("ready", "request"))

    def test_partial_claim_survives_replacement(self):
        from unittest.mock import patch
        self.begin()
        with self.authorized(), patch.object(core, "prepare_assignment", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.claim()
        self.call("end")
        new = self.next()
        self.assertEqual((new["state"], new["request_id"]), ("ready", "request"))
        self.c = {**new["owner"], "invocation": "continued", "request": "request"}
        self.call("begin")
        with self.authorized():
            self.assertEqual(self.claim()["action"], "arm_then_send_once")

    def test_enrollment_cannot_replace_existing_authority(self):
        before = (self.paths.state_dir / "resident-owner.json").read_bytes()
        self.assertEqual(self.call("enroll", self.initial)["reason"], "already_enrolled")
        self.assertEqual((self.paths.state_dir / "resident-owner.json").read_bytes(), before)

    def test_large_qualification_stays_out_of_operational_replies(self):
        self.assertGreater(len(json.dumps(self.call("inspect"))), 14000)
        self.assertLess(len(json.dumps(self.begin())), 1024)
        self.assertLess(len(json.dumps(self.call("check"))), 1024)
        self.assertLess(len(json.dumps(self.call("end"))), 1024)
        self.assertLess(len(json.dumps(self.next())), 1024)

    def test_completed_unpublished_answer_survives_replacement(self):
        self.begin()
        with self.authorized():
            wrapped = self.claim()["wrapped_prompt"]
            core.arm_assignment("request", self.paths)
            core.mark_submitted("request", wrapped, self.paths)
            raw = json.dumps({"schemaVersion": 1,
                "thread": {"id": "worker", "kind": "chatgpt", "status": {"type": "idle"}},
                "turns": [{"id": "turn", "items": [
                    {"id": "turn", "type": "userMessage", "content": [{"type": "text", "text": wrapped}]},
                    {"id": "answer", "type": "agentMessage", "text":
                     core.result_marker("request") + "\nanswer\n" + core.end_marker("request")}
                ]}]}).encode()
            core.complete_assignment("request", b"", self.paths, native_read=raw)
        self.assertEqual(self.next()["state"], "busy")
        self.call("end")
        new = self.next()
        self.assertEqual((new["state"], new["request_id"]), ("collect_only", "request"))
        self.c = {**new["owner"], "invocation": "collector", "request": "request"}
        self.call("begin")
        with self.authorized():
            self.q.observe("request", "parent", confirmed=True, native_read=raw)
        first, second = self.q.collect("request"), self.q.collect("request")
        self.assertEqual(first, second)
        self.assertEqual(first["answer"]["payload"], "answer")
        self.assertEqual(core.load_assignment("request", self.paths)["submission_count"], 1)

    def test_doctor_cannot_write_without_reserved_invocation(self):
        self.begin()
        with self.authorized():
            self.claim()
        path = core.assignment_path("request", self.paths)
        with core.state_lock(self.paths) as lock:
            value = core.read_json(path)
            value["last_error"] = "legacy diagnostic body"
            core.atomic_write_json(path, value, _locked=lock)
        before = path.read_bytes()
        with self.assertRaises(core.BusyError):
            core.redact_stored_diagnostics(self.paths)
        self.assertEqual(before, path.read_bytes())
        with self.authorized():
            self.assertEqual(core.redact_stored_diagnostics(self.paths), 1)
        self.assertNotIn(b"legacy diagnostic body", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
