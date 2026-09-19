import concurrent.futures
import contextlib
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_inspect_projects_terminal_detachment_without_changing_owner(self):
        before = (self.paths.state_dir / "resident-owner.json").read_bytes()
        self.assertEqual(self.call("inspect")["owner_state"], "active")
        directory = self.paths.state_dir / "resident-supervision"
        directory.mkdir(mode=0o700)
        marker = directory / f"terminal-{self.c['generation']}-{self.c['owner']}.json"
        marker.write_text(json.dumps({
            "version": 1, "generation": self.c["generation"],
            "owner": self.c["owner"], "parent": self.c["parent"],
            "session": self.c.get("session"),
            "state": "terminally_detached", "send_authorized": False,
        }))
        marker.chmod(0o600)
        inspected = self.call("inspect")
        self.assertEqual(inspected["owner_state"], "terminally_detached")
        for key in ("generation", "owner", "parent", "session"):
            self.assertEqual(inspected["owner"][key], self.c[key])
        self.assertEqual(before, (self.paths.state_dir / "resident-owner.json").read_bytes())
        with self.assertRaisesRegex(core.StateError, "terminally detached"):
            self.call("check")

    def session_binding(self):
        directory = self.paths.state_dir.parent / "session"
        directory.mkdir(mode=0o700)
        raw = json.dumps(dict(resident=True, sessionId="a" * 32, parent="parent", worker="worker", idleMs=45000,
                              helper=str(Path(resident.__file__).resolve().parents[2] / "skills/codex-pro-dispatch/scripts/pro-dispatch"),
                              configDir=str(self.paths.config_dir), stateDir=str(self.paths.state_dir))).encode()
        path = directory / "session.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        return dict(directory=str(directory), session_id="a" * 32,
                    descriptor_sha256=hashlib.sha256(raw).hexdigest())

    def admission(self):
        binding = self.session_binding()
        self.c = self.call("bind-session", {**self.c, "session": binding})["owner"]
        directory = Path(binding["directory"])
        marker = directory / ("waiting-1." + binding["session_id"])
        marker.mkdir(mode=0o700)
        prompt = directory.parent / "prompt.txt"
        prompt.write_bytes(b"Answer this")
        prompt.chmod(0o600)
        record = dict(sessionId=binding["session_id"], ordinal=1, requestId="request",
                      clientSessionId="client", nonce="b" * 32, deadlineAt=int(time.time() * 1000) + 45000,
                      promptSha256=hashlib.sha256(prompt.read_bytes()).hexdigest(), pid=1, ppid=1)
        credentials = {**self.c, "command": json.dumps(record, separators=(",", ":")),
                       "prompt_file": str(prompt), "retry": False}
        return directory, marker, credentials

    def test_replacement_after_client_precheck_before_admission_creates_nothing(self):
        directory, marker, credentials = self.admission()
        self.call("check", credentials)  # Advisory client precheck succeeded.
        before = sorted(directory.iterdir())
        self.assertEqual(self.next()["state"], "ready")
        with self.assertRaisesRegex(core.StateError, "owner replaced"):
            self.call("admit", credentials)
        self.assertEqual(sorted(directory.iterdir()), before)
        self.assertTrue(marker.is_dir())
        self.assertEqual(self.q.status()["requests"], [])

    def test_admission_publication_and_replacement_share_one_transaction(self):
        directory, marker, credentials = self.admission()
        reached, release, replacing = threading.Event(), threading.Event(), threading.Event()
        rename = resident.os.rename

        def pause_claim(source, target, **kwargs):
            if source == marker.name:
                reached.set()
                if not release.wait(5):
                    raise RuntimeError("Admission barrier timed out")
            return rename(source, target, **kwargs)

        def replace():
            replacing.set()
            result = self.next()
            self.assertEqual((directory / "command-1.json").read_text(), credentials["command"])
            self.assertEqual((directory / "command-1/prompt.txt").read_bytes(), b"Answer this")
            return result

        with patch.object(resident.os, "rename", side_effect=pause_claim):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                admitted = pool.submit(self.call, "admit", credentials)
                try:
                    self.assertTrue(reached.wait(5))
                    replacement = pool.submit(replace)
                    self.assertTrue(replacing.wait(5))
                    with self.assertRaises(concurrent.futures.TimeoutError):
                        replacement.result(timeout=0.1)
                finally:
                    release.set()
                self.assertTrue(admitted.result()["published"])
                self.assertEqual(replacement.result()["state"], "ready")
        before = (directory / "command-1.json").read_bytes()
        with self.assertRaisesRegex(core.StateError, "owner replaced"):
            self.call("begin", {**self.c, "invocation": "old-native", "request": "request"})
        with self.assertRaises(core.StateError):
            self.call("admit", credentials)
        self.assertEqual((directory / "command-1.json").read_bytes(), before)
        self.assertEqual(self.q.status()["requests"], [])
        self.assertFalse(marker.exists())

    def test_admission_rechecks_mutable_inputs_before_ticket(self):
        directory, marker, credentials = self.admission()
        before = sorted(directory.iterdir())
        self.begin()
        with self.assertRaises(core.BusyError):
            self.call("admit", credentials)
        self.call("end")
        Path(credentials["prompt_file"]).write_bytes(b"Changed after client precheck")
        with self.assertRaisesRegex(core.StateError, "Prompt changed"):
            self.call("admit", credentials)
        self.assertTrue(marker.is_dir())
        self.assertEqual(sorted(directory.iterdir()), before)

    def test_partial_admission_preserves_ticket_and_never_reuses_it(self):
        from codex_pro_dispatch.native_storage import Directory
        directory, marker, credentials = self.admission()
        with patch.object(Directory, "write", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(OSError):
                self.call("admit", credentials)
        self.assertFalse(marker.exists())
        self.assertTrue((directory / "command-1").is_dir())
        self.assertFalse((directory / "command-1.json").exists())
        with self.assertRaisesRegex(core.StateError, "Existing rendezvous artifact"):
            self.call("admit", credentials)
        self.assertEqual(self.q.status()["requests"], [])

    def test_session_bound_once_by_current_unreserved_owner(self):
        binding = self.session_binding()
        bound = self.call("bind-session", {**self.c, "session": binding})["owner"]
        self.assertEqual(bound["session"], binding)
        before = (self.paths.state_dir / "resident-owner.json").read_bytes()
        with self.assertRaises(core.BusyError):
            self.call("bind-session", {**self.c, "session": binding})
        self.assertEqual((self.paths.state_dir / "resident-owner.json").read_bytes(), before)
        self.c = self.next()["owner"]
        self.assertIsNone(self.c["session"])
        self.begin()
        with self.assertRaises(core.BusyError):
            self.call("bind-session", {**self.c, "session": binding})

    def test_session_binding_rejects_wrong_bytes_identity_and_stale_owner(self):
        binding = self.session_binding()
        before = (self.paths.state_dir / "resident-owner.json").read_bytes()
        for field, value in [("descriptor_sha256", "0" * 64), ("session_id", "b" * 32)]:
            with self.assertRaises(core.StateError):
                self.call("bind-session", {**self.c, "session": {**binding, field: value}})
            self.assertEqual((self.paths.state_dir / "resident-owner.json").read_bytes(), before)
        self.next()
        with self.assertRaises(core.StateError):
            self.call("bind-session", {**self.c, "session": binding})

    def test_v1_is_read_without_migration_and_only_unreserved_start_upgrades(self):
        path = self.paths.state_dir / "resident-owner.json"
        with core.state_lock(self.paths) as lock:
            v = core.read_json(path)
            v["version"] = 1
            del v["session"]
            core.atomic_write_json(path, v, _locked=lock)
        before = path.read_bytes()
        self.assertEqual(self.call("inspect")["owner"]["version"], 1)
        self.assertEqual(path.read_bytes(), before)
        self.begin()
        self.assertEqual(self.next()["state"], "busy")
        self.assertEqual(self.call("inspect")["owner"]["version"], 1)
        self.call("end")
        self.assertEqual(self.next()["owner"]["version"], 2)

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
