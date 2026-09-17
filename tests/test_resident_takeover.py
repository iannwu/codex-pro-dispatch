"""Host-idle fenced replacement uses one canonical owner write."""
import json
import os
import stat
import time
import hashlib
import io
from contextlib import redirect_stderr
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import test_three_feature as fixtures
from codex_pro_dispatch import core, resident
from codex_pro_dispatch.queue import Queue


PARENT = "01a09f71-aa77-7502-8c1c-0d2ea8264ae9"
REPLACEMENT = "01a0ac4a-313b-7f33-9ca0-f76d994ef745"


class TakeoverTests(unittest.TestCase):
    setUp = fixtures.ThreeFeatureTests.setUp
    tearDown = fixtures.ThreeFeatureTests.tearDown
    activate = fixtures.ThreeFeatureTests.activate
    _enrolled_owner = fixtures.ThreeFeatureTests._enrolled_owner
    _arm_crash_request = fixtures.ThreeFeatureTests._arm_crash_request

    def packet(self, owner, *, replacement=REPLACEMENT, status="idle"):
        directory = Path(tempfile.mkdtemp(prefix="takeover-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        os.chmod(directory, 0o700)
        raw = json.dumps({"schemaVersion": 1, "thread": {
            "id": owner["parent"], "kind": "codex", "hostId": "local",
            "status": {"type": status},
        }}, separators=(",", ":"))
        evidence = directory / "owner-read.json"
        evidence.write_text(raw)
        evidence.chmod(0o600)
        return {"schema_version": 1, "replacement_task_id": replacement,
                "expected": {key: owner[key] for key in ("generation", "owner", "parent", "worker_pool_sha256")},
                "owner_read_text": raw, "evidence_path": str(evidence)}

    def test_T5_idle_commit_fences_old_owner_and_accepts_only_one_racer(self):
        owner, old = self._enrolled_owner()
        packet = self.packet(owner)
        packets = [packet, self.packet(owner, replacement="01a0ac4b-313b-7f33-9ca0-f76d994ef745")]
        barrier = threading.Barrier(2)
        acquire = core.state_lock
        @contextmanager
        def synchronized_lock(*args, **kwargs):
            if kwargs.get("token") is None:
                barrier.wait(timeout=5)
            with acquire(*args, **kwargs) as locked:
                yield locked
        with patch.object(core, "state_lock", synchronized_lock), ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(lambda item: resident._takeover_from_native(self.paths, item), packets))
        self.assertCountEqual([value["outcome"] for value in outcomes], ["committed", "expected_state_stale"])
        current = resident.control("inspect", {}, self.paths)["owner"]
        self.assertIn(current["parent"], {REPLACEMENT, "01a0ac4b-313b-7f33-9ca0-f76d994ef745"})
        self.assertEqual(current["generation"], owner["generation"] + 1)
        self.assertEqual([slot["phase"] for slot in current["slots"]], ["idle", "idle"])
        with self.assertRaises(core.DispatchError):
            resident.control("start", old, self.paths)

    def test_T3_active_and_working_refuse_without_mutation_then_idle_commits(self):
        owner, _ = self._enrolled_owner()
        before = (self.paths.state_dir / "resident-owner.json").read_bytes()
        for status in ("active", "working"):
            self.assertEqual(resident._takeover_from_native(self.paths, self.packet(owner, status=status))["outcome"],
                             "old_owner_active")
            self.assertEqual((self.paths.state_dir / "resident-owner.json").read_bytes(), before)
        self.assertEqual(resident._takeover_from_native(self.paths, self.packet(owner))["outcome"], "committed")

    def test_T10_armed_is_collect_only_and_prepared_is_cancel_pending(self):
        owner, credentials = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("prepared", b"not sent", "client")
        token = resident.invocation.set({**credentials, "invocation": "prepared-inv", "request": "prepared"})
        try:
            queue.claim("parent", True, "prepared")
        finally:
            resident.invocation.reset(token)
        # A second slot has crossed arm. It is forever collection-only.
        armed_credentials = {**credentials, "invocation": "armed-inv", "request": "armed"}
        token = resident.invocation.set(armed_credentials)
        try:
            claim = queue.submit("armed", b"possibly sent", "client")
            queue.claim("parent", True, "armed")
            core.arm_for_send("slot-b", "armed", owner["generation"], "armed-inv", self.paths)
        finally:
            resident.invocation.reset(token)
        committed = resident._takeover_from_native(self.paths, self.packet(owner))
        self.assertEqual(committed["outcome"], "committed")
        current = resident.control("inspect", {}, self.paths)["owner"]
        self.assertEqual({slot["request"]: slot["phase"] for slot in current["slots"]},
                         {"prepared": "cancel_pending", "armed": "collect_only"})
        blocked = resident.control("start", {key: current[key] for key in
                                              ("generation", "owner", "parent", "worker_pool_sha256")}, self.paths)
        self.assertEqual(blocked["reason"], "takeover_settlement_required")
        settled = resident.control("settle", {key: current[key] for key in
                                               ("generation", "owner", "parent", "worker_pool_sha256")}, self.paths)
        self.assertEqual(settled["settled"], ["prepared"])
        self.assertEqual(core.load_assignment("prepared", self.paths)["status"], "abandoned")
        self.assertEqual(Queue(self.paths).load("prepared")["state"], "released")

    def test_invalid_status_and_evidence_modes_do_not_commit(self):
        owner, _ = self._enrolled_owner()
        self.assertEqual(resident._takeover_from_native(self.paths, self.packet(owner, status="notLoaded"))["outcome"],
                         "old_owner_not_loaded")
        packet = self.packet(owner)
        os.chmod(packet["evidence_path"], 0o644)
        self.assertEqual(resident._takeover_from_native(self.paths, packet)["outcome"], "evidence_stale")

    def current(self):
        return resident.control("inspect", {}, self.paths)["owner"]

    def creds(self, owner=None, **extra):
        owner = owner or self.current()
        return {**{k: owner[k] for k in ("generation", "owner", "parent", "worker_pool_sha256")}, **extra}

    @contextmanager
    def caller(self, c):
        token = resident.invocation.set(c)
        try:
            yield
        finally:
            resident.invocation.reset(token)

    def snapshot(self):
        return {str(p.relative_to(self.paths.state_dir)): p.read_bytes()
                for p in self.paths.state_dir.rglob("*") if p.is_file()}

    def write_fixture(self, path, value):
        with core.state_lock(self.paths) as locked:
            core.atomic_write_json(path, value, _locked=locked)

    def claimed(self, c, rid="request", status="armed"):
        q = Queue(self.paths)
        q.submit(rid, b"fixture prompt", "client")
        c = dict(c, invocation="inv-" + rid, request=rid)
        with self.caller(c):
            claim = q.claim(c["parent"], True, rid)
            if status != "prepared":
                core.arm_for_send(claim["worker_slot"], rid, c["generation"], c["invocation"], self.paths)
                if status == "submitted":
                    core.mark_submitted(rid, claim["wrapped_prompt"], self.paths)
                elif status in {"pending", "indeterminate", "ambiguous"}:
                    receipt = core.load_assignment(rid, self.paths)
                    receipt["status"] = status
                    self.write_fixture(core.assignment_path(rid, self.paths), receipt)
        return claim, c

    def late_history(self, rid, claim, pending=False):
        items = [{"id": "turn-" + rid, "type": "userMessage",
                  "content": [{"type": "text", "text": claim["wrapped_prompt"]}]}]
        if not pending:
            items.append({"id": "answer-" + rid, "type": "agentMessage",
                          "text": core.result_marker(rid) + "\nlate answer\n" + core.end_marker(rid)})
        return json.dumps({"schemaVersion": 1, "thread": {"id": claim["worker_conversation_id"],
            "kind": "chatgpt", "status": {"type": "idle"}},
            "turns": [{"id": "turn-" + rid, "items": items}]}).encode()

    def test_T2_uncertain_statuses_preserve_bytes_and_deny_every_arm(self):
        for status in ("armed", "submitted", "pending", "indeterminate", "ambiguous"):
            with self.subTest(status=status):
                self.tearDown()
                self.setUp()
                owner, c = self._enrolled_owner()
                claim, old = self.claimed(c, status=status)
                before = self.snapshot()
                result = resident._takeover_from_native(self.paths, self.packet(owner))
                self.assertEqual(result["collect_only"], ["request"])
                after = self.snapshot()
                self.assertEqual([k for k in before if before[k] != after[k]], ["resident-owner.json"])
                for cred in (old, self.creds(invocation="new", request="request"),
                             self.creds(takeover_settlement=True, request="request", request_parent="parent")):
                    with self.caller(cred), self.assertRaises(core.DispatchError):
                        core.arm_for_send(claim["worker_slot"], "request", cred["generation"], cred.get("invocation", "new"), self.paths)
                self.assertEqual(self.snapshot(), after)
                self.tearDown()

    def test_T7_T14_successive_takeovers_collect_original_parent_late_answer(self):
        owner, c = self._enrolled_owner()
        claim, _ = self.claimed(c)
        self.assertEqual(resident._takeover_from_native(self.paths, self.packet(owner))["outcome"], "committed")
        b = self.creds()
        started = resident.control("start", b, self.paths)
        self.assertEqual(started["state"], "ready")
        current = self.current()
        self.assertEqual(resident._takeover_from_native(self.paths, self.packet(current,
            replacement="01a0ac4b-313b-7f33-9ca0-f76d994ef745"))["outcome"], "committed")
        owner = self.current()
        self.assertEqual(owner["qualification"]["takeover"]["request_bindings"]["request"]["prior_parent"], "parent")
        resident.control("collector-open", self.creds(), self.paths)
        collector = self.creds(collector_only=True, request="request", request_parent="parent")
        q = Queue(self.paths)
        with self.caller(collector):
            pending = q.observe("request", "parent", True, self.late_history("request", claim, True))
            self.assertEqual(pending["observation"], "pending")
            result = q.observe("request", "parent", True, self.late_history("request", claim))
            self.assertEqual(result["observation"], "published")
        self.assertEqual(core.load_assignment("request", self.paths)["submission_count"], 1)
        scoped = self.creds(takeover_settlement=True, request="request", request_parent="parent")
        before = self.snapshot()
        with self.caller(scoped):
            with self.assertRaises(core.DispatchError):
                core.complete_assignment("request", b"", self.paths, native_read=self.late_history("request", claim))
            with self.assertRaises(core.DispatchError):
                q.publish("request", "parent", True, self.late_history("request", claim))
        self.assertEqual(self.snapshot(), before)
        resident.control("end", dict(collector, slot=claim["worker_slot"]), self.paths)
        self.assertEqual(self.current()["slots"][0]["phase"], "idle")
        with self.caller({**b, "takeover_settlement": True, "request": "request", "request_parent": "parent"}), self.assertRaises(core.DispatchError):
            q.release("request", "parent")

    def test_T4_T13_cancel_variants_and_crash_converge(self):
        for variant in ("prepared", "missing_receipt", "queued"):
            with self.subTest(variant=variant):
                self.tearDown()
                self.setUp()
                owner, c = self._enrolled_owner()
                q = Queue(self.paths)
                if variant == "queued":
                    q.submit("request", b"fixture prompt", "client")
                    resident.control("begin", {**c, "request": "request", "invocation": "inv"}, self.paths)
                else:
                    self.claimed(c, status="prepared")
                    if variant == "missing_receipt":
                        core.assignment_path("request", self.paths).unlink()
                        r = q.load("request"); r["receipt_established"] = False
                        self.write_fixture(q.path("request"), r)
                self.assertEqual(resident._takeover_from_native(self.paths, self.packet(owner))["cancel_prepared"], ["request"])
                current = self.current()
                self.assertEqual(resident._takeover_from_native(self.paths, self.packet(current))["outcome"], "already_owner")
                self.assertEqual(resident.control("start", self.creds(), self.paths)["reason"], "takeover_settlement_required")
                resident.control("settle", self.creds(), self.paths)
                after = self.snapshot()
                resident.control("settle", self.creds(), self.paths)
                self.assertEqual(self.snapshot(), after)
                self.assertEqual(q.load("request")["state"], "queued" if variant == "queued" else "released")
                if variant != "queued":
                    r = core.load_assignment("request", self.paths)
                    self.assertEqual(r["abandon_reason_sha256"], core.sha256_text("takeover"))
                self.assertTrue(all(slot["phase"] == "idle" for slot in self.current()["slots"]))
                self.assertEqual(resident.control("start", self.creds(), self.paths)["state"], "ready")
                self.tearDown()

    def test_T8_receipt_association_errors_fail_closed(self):
        owner, c = self._enrolled_owner()
        self.claimed(c)
        r = core.load_assignment("request", self.paths)
        r["parent_task_id"] = "wrong-parent"
        self.write_fixture(core.assignment_path("request", self.paths), r)
        before = self.snapshot()
        self.assertEqual(resident._takeover_from_native(self.paths, self.packet(owner))["outcome"], "request_evidence_invalid")
        self.assertEqual(self.snapshot(), before)

    def test_T8_running_prepared_is_never_cancellable(self):
        owner, c = self._enrolled_owner()
        self.claimed(c, status="prepared")
        v = self.current(); v["slots"][0]["phase"] = "running"
        self.write_fixture(self.paths.state_dir / "resident-owner.json", v)
        result = resident._takeover_from_native(self.paths, self.packet(owner))
        self.assertEqual(result["collect_only"], ["request"])
        self.assertEqual(result["cancel_prepared"], [])

    def test_T9_evidence_rejections_preserve_all_bytes(self):
        owner, _ = self._enrolled_owner()
        for key, value in (("id", "wrong"), ("kind", "chatgpt"), ("hostId", "remote"), ("truncated", True)):
            packet = self.packet(owner)
            raw = json.loads(packet["owner_read_text"]); raw["thread"][key] = value
            packet["owner_read_text"] = json.dumps(raw)
            Path(packet["evidence_path"]).write_text(packet["owner_read_text"])
            before = self.snapshot()
            self.assertEqual(resident._takeover_from_native(self.paths, packet)["outcome"], "old_owner_unreadable")
            self.assertEqual(self.snapshot(), before)
        for update in ({"replacement_task_id": None}, {"expected": {}}, {"schema_version": 2}):
            packet = self.packet(owner); packet.update(update)
            self.assertEqual(resident._takeover_from_native(self.paths, packet)["outcome"], "evidence_stale")
        from codex_pro_dispatch.cli import build_parser
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["resident", "takeover"])
        with self.assertRaises(core.DispatchError):
            resident.control("takeover", {}, self.paths)

    def test_T12_idle_sibling_reports_capacity_and_can_arm_once(self):
        owner, c = self._enrolled_owner()
        self.claimed(c)
        resident._takeover_from_native(self.paths, self.packet(owner))
        status = core.worker_pool_runtime_status(core.load_worker_pool(self.paths), self.paths)
        self.assertEqual(status["readiness"], "ready_for_explicit_start")
        self.assertEqual(status["collect_only_slots"], ["slot-a"])
        started = resident.control("start", self.creds(), self.paths)
        self.assertEqual(started["request_ids"], ["request"])
        claim, new = self.claimed(self.creds(), rid="new")
        self.assertEqual(claim["worker_slot"], "slot-b")
        with self.caller(new), self.assertRaises(core.DispatchError):
            core.arm_for_send("slot-b", "new", new["generation"], new["invocation"], self.paths)

    def test_T14_settlement_operations_fail_closed_and_public_release_works(self):
        owner, c = self._enrolled_owner()
        claim, _ = self.claimed(c, status="prepared")
        resident._takeover_from_native(self.paths, self.packet(owner))
        scoped = self.creds(takeover_settlement=True, request="request", request_parent="parent")
        q = Queue(self.paths)
        calls = [lambda: q.claim("parent", True, "request"),
                 lambda: q.observe("request", "parent", True),
                 lambda: q.publish("request", "parent", True),
                 lambda: core.complete_assignment("request", b"", self.paths),
                 lambda: core.mark_submitted("request", claim["wrapped_prompt"], self.paths),
                 lambda: core.mark_pending("request", self.paths),
                 lambda: core.prepare_assignment("fixture prompt", parent_task_id="parent", assignment_id="request", paths=self.paths),
                 lambda: resident.control("begin", dict(scoped, invocation="bad", slot="slot-a"), self.paths),
                 lambda: core.mark_indeterminate("request", reason="test", paths=self.paths),
                 lambda: core.mark_ambiguous("request", reason="test", paths=self.paths),
                 lambda: resident.control("end", scoped, self.paths),
                 lambda: q.release("request", "parent")]
        for call in calls:
            before = self.snapshot()
            with self.caller(scoped), self.assertRaises(core.DispatchError): call()
            self.assertEqual(self.snapshot(), before)
        with core.state_lock(self.paths) as locked:
            with self.caller(scoped), self.assertRaises(core.DispatchError):
                resident.reserve_slot(self.paths, locked, "slot-a", "request", "worker-a", scoped["generation"])
            with self.caller(scoped), self.assertRaises(core.DispatchError):
                resident.mark_running(self.paths, locked, "slot-a", "request", scoped["generation"], "bad")
            for operation in (None, "unknown", "pending", "complete", "observe"):
                with self.caller(scoped), self.assertRaises(core.DispatchError):
                    resident.guard(self.paths, locked, "request", operation=operation)
        with self.caller(scoped):
            core.abandon_assignment("request", reason="operator cancellation", paths=self.paths)
            q.release("request", "parent")
        resident.control("end", scoped, self.paths)
        self.assertEqual(self.current()["slots"][0]["phase"], "idle")

    def test_T14_forged_ancestry_and_missing_collector_binding_rejected(self):
        owner, c = self._enrolled_owner()
        claim, _ = self.claimed(c)
        resident._takeover_from_native(self.paths, self.packet(owner))
        resident.control("collector-open", self.creds(), self.paths)
        with self.caller(self.creds(collector_only=True, request="request")), self.assertRaises(core.DispatchError):
            Queue(self.paths).observe("request", "parent", True, self.late_history("request", claim))
        v = self.current(); v["qualification"]["takeover"]["replacement_parent"] = "forged"
        self.write_fixture(self.paths.state_dir / "resident-owner.json", v)
        before = self.snapshot()
        result = resident._takeover_from_native(self.paths, self.packet(v, replacement="01a0ac4b-313b-7f33-9ca0-f76d994ef745"))
        self.assertEqual(result["outcome"], "request_evidence_invalid")
        self.assertEqual(self.snapshot(), before)

    def test_T15_stale_marker_archival_and_current_marker_exclusion(self):
        owner, c = self._enrolled_owner()
        self.claimed(c)
        resident.control("collector-open", c, self.paths)
        marker = self.paths.state_dir / "resident-recovery.json"
        old_bytes = marker.read_bytes()
        resident._takeover_from_native(self.paths, self.packet(owner))
        collector = self.creds(collector_only=True, request="request", request_parent="parent")
        with core.state_lock(self.paths) as locked, self.assertRaisesRegex(core.StateError, "marker_stale"):
            resident._collector_mutex(self.paths, locked, collector, "request")
        resident.control("collector-open", self.creds(), self.paths)
        archived = list((self.paths.state_dir / "markers/retired").glob("*"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), old_bytes)
        with self.assertRaises(core.BusyError):
            resident.control("collector-open", self.creds(), self.paths)
        resident.control("collector-close", self.creds(collector_only=True), self.paths)
        resident.control("settle", self.creds(), self.paths)
        self.assertEqual(len(list((self.paths.state_dir / "markers/retired").glob("*"))), 1)

    def test_T6_every_old_generation_mutator_preserves_state(self):
        owner, c = self._enrolled_owner()
        claim, old = self.claimed(c)
        resident._takeover_from_native(self.paths, self.packet(owner))
        q = Queue(self.paths)
        operations = [
            lambda: q.claim("parent", True, "request"),
            lambda: core.arm_for_send("slot-a", "request", old["generation"], old["invocation"], self.paths),
            lambda: q.publish("request", "parent", True, self.late_history("request", claim)),
            lambda: core.mark_submitted("request", claim["wrapped_prompt"], self.paths),
            lambda: core.mark_indeterminate("request", reason="old", paths=self.paths),
            lambda: core.mark_ambiguous("request", reason="old", paths=self.paths),
            lambda: core.abandon_assignment("request", reason="old", paths=self.paths),
            lambda: q.release("request", "parent"),
        ] + [lambda action=action: resident.control(action, {**old, "slot": "slot-a"}, self.paths)
             for action in ("begin", "end", "bind-session", "collector-open", "handoff")]
        for operation in operations:
            before = self.snapshot()
            with self.caller(old), self.assertRaises(core.DispatchError): operation()
            self.assertEqual(self.snapshot(), before)

    def test_T1_T11_clean_commit_changes_only_owner_then_real_admission(self):
        owner, _ = self._enrolled_owner()
        before_owner = self.current(); before = self.snapshot()
        resident._takeover_from_native(self.paths, self.packet(owner))
        after = self.snapshot(); after_owner = self.current()
        self.assertEqual([k for k in before if before[k] != after[k]], ["resident-owner.json"])
        self.assertEqual(set(before_owner), set(after_owner))
        self.assertEqual({k for k in before_owner if before_owner[k] != after_owner[k]},
                         {"generation", "owner", "parent", "qualification"})
        self.assertEqual(after_owner["qualification"]["takeover"]["collect_only"], [])
        started = resident.control("start", self.creds(), self.paths)["owner"]
        directory = self.paths.state_dir.parent / "admission"; directory.mkdir(mode=0o700)
        helper = Path(resident.__file__).resolve().parents[2] / "skills/codex-pro-dispatch/scripts/pro-dispatch"
        descriptor = dict(resident=True, sessionId="b" * 32, parent=started["parent"],
            configDir=str(self.paths.config_dir), stateDir=str(self.paths.state_dir),
            workerPoolSha256=started["worker_pool_sha256"], helper=str(helper), idleMs=45000)
        raw = json.dumps(descriptor).encode()
        (directory / "session.json").write_bytes(raw); (directory / "session.json").chmod(0o600)
        binding = dict(directory=str(directory), session_id="b" * 32, descriptor_sha256=hashlib.sha256(raw).hexdigest())
        resident.control("bind-session", self.creds(session=binding), self.paths)
        (directory / ("waiting-1." + binding["session_id"])).mkdir(mode=0o700)
        prompt = directory / "prompt.txt"; prompt.write_bytes(b"admitted once"); prompt.chmod(0o600)
        command = dict(sessionId=binding["session_id"], ordinal=1, requestId="admitted",
                       clientSessionId="client", nonce="c" * 32, deadlineAt=int(time.time()*1000)+30000,
                       promptSha256=hashlib.sha256(prompt.read_bytes()).hexdigest(), pid=1, ppid=1)
        admitted = resident.control("admit", self.creds(session=binding, command=json.dumps(command),
                                                       prompt_file=str(prompt), retry=False), self.paths)
        self.assertTrue(admitted["published"])
        self.assertFalse(admitted["send_authorized"])
        self.assertTrue((directory / "command-1.json").is_file())

    def test_T8_terminal_missing_orphan_and_legacy_matrix(self):
        for shape in ("missing_queue", "terminal", "orphan", "legacy", "unknown", "released_cancel"):
            with self.subTest(shape=shape):
                self.tearDown(); self.setUp()
                owner, c = self._enrolled_owner(); q = Queue(self.paths)
                if shape == "missing_queue":
                    resident.control("begin", {**c, "invocation": "inv", "request": "request"}, self.paths)
                else:
                    claim, old = self.claimed(c, status="prepared" if shape == "released_cancel" else "armed")
                    receipt = core.load_assignment("request", self.paths)
                    if shape == "terminal": receipt["status"] = "failed"
                    if shape == "legacy": receipt.pop("result_protocol")
                    if shape == "unknown": receipt["submission_count"] = 7
                    self.write_fixture(core.assignment_path("request", self.paths), receipt)
                    if shape == "orphan": q.path("request").unlink()
                    if shape == "released_cancel":
                        resident._takeover_from_native(self.paths, self.packet(owner))
                        scoped = self.creds(takeover_settlement=True, request="request", request_parent="parent")
                        with self.caller(scoped):
                            core.abandon_assignment("request", reason="takeover", paths=self.paths)
                            q.release("request", "parent")
                        owner = self.current()
                before = self.snapshot()
                result = resident._takeover_from_native(self.paths, self.packet(owner,
                    replacement="01a0ac4b-313b-7f33-9ca0-f76d994ef745"))
                if shape in {"orphan", "legacy"}:
                    self.assertEqual(result["outcome"], "request_evidence_invalid")
                    self.assertEqual(self.snapshot(), before)
                else:
                    self.assertEqual(result["outcome"], "committed")
                    self.assertEqual(self.current()["slots"][0]["phase"],
                        "idle" if shape == "missing_queue" else "cancel_pending" if shape == "released_cancel" else "collect_only")

    def test_T15_stale_marker_readers_and_recovery_never_reenable_send(self):
        owner, c = self._enrolled_owner()
        self.claimed(c, status="prepared")
        resident.control("collector-open", c, self.paths)
        resident._takeover_from_native(self.paths, self.packet(owner))
        recovered = resident.control("recover-start", self.creds(evidence_file=str(self.evidence),
            evidence_sha256=self.evidence_hash), self.paths)
        self.assertEqual(recovered["owner"]["slots"][0]["phase"], "cancel_pending")
        close = resident.control("collector-close", self.creds(collector_only=True), self.paths)
        self.assertFalse(close["closed"])
        resident.control("settle", self.creds(), self.paths)
        self.assertEqual(self.current()["slots"][0]["phase"], "idle")

    def test_T14_operator_abandon_collect_only_and_denied_mixed_flags(self):
        owner, c = self._enrolled_owner(); claim, _ = self.claimed(c)
        resident._takeover_from_native(self.paths, self.packet(owner))
        scoped = self.creds(takeover_settlement=True, request="request", request_parent="parent")
        before = self.snapshot()
        with self.caller(dict(scoped, collector_only=True)), self.assertRaises(core.DispatchError):
            core.abandon_assignment("request", reason="operator", paths=self.paths)
        self.assertEqual(self.snapshot(), before)
        with self.caller(scoped):
            core.abandon_assignment("request", reason="operator", paths=self.paths)
            Queue(self.paths).release("request", "parent")
        resident.control("end", scoped, self.paths)
        self.assertEqual(self.current()["slots"][0]["phase"], "idle")

    def one_worker(self):
        activate = core.activate_worker_pool
        with patch.object(core, "activate_worker_pool", side_effect=lambda workers, **kw: activate(workers[:1], **kw)):
            return self._enrolled_owner()

    def test_T12_single_worker_collect_only_cannot_start(self):
        owner, c = self.one_worker(); self.claimed(c)
        resident._takeover_from_native(self.paths, self.packet(owner))
        self.assertEqual(resident.control("start", self.creds(), self.paths)["state"], "collect_only")
        status = core.worker_pool_runtime_status(core.load_worker_pool(self.paths), self.paths)
        self.assertEqual(status["readiness"], "collector_only_recovery_required")

    def test_T13_single_worker_cancellation_frees_capacity(self):
        owner, c = self.one_worker(); self.claimed(c, status="prepared")
        resident._takeover_from_native(self.paths, self.packet(owner))
        self.assertEqual(resident.control("start", self.creds(), self.paths)["reason"], "takeover_settlement_required")
        resident.control("settle", self.creds(), self.paths)
        resident.control("start", self.creds(), self.paths)
        claim, fresh = self.claimed(self.creds(), rid="fresh")
        with self.caller(fresh):
            core.mark_submitted("fresh", claim["wrapped_prompt"], self.paths)
        self.assertEqual(claim["worker_slot"], "slot-a")
        self.assertEqual(core.load_assignment("request", self.paths)["submission_count"], 0)
        self.assertEqual(core.load_assignment("fresh", self.paths)["submission_count"], 1)

    def test_T8_T15_marker_generation_matrix(self):
        for delta in (-1, 0, 1):
            with self.subTest(delta=delta):
                self.tearDown(); self.setUp()
                owner, c = self._enrolled_owner()
                resident.control("collector-open", c, self.paths)
                path = self.paths.state_dir / "resident-recovery.json"
                marker = core.read_json(path); marker["generation"] += delta
                self.write_fixture(path, marker); before = self.snapshot()
                result = resident._takeover_from_native(self.paths, self.packet(owner))
                if delta == 1:
                    self.assertEqual(result["outcome"], "request_evidence_invalid")
                    self.assertEqual(self.snapshot(), before)
                else:
                    self.assertEqual(result["outcome"], "committed")
                    self.assertEqual(path.read_bytes(), before["resident-recovery.json"])
                    self.assertEqual(len(self.current()["qualification"]["takeover"]["markers_retired"]), 1)
                    # A stale marker must not prevent a sibling from crossing arm.
                    self.claimed(self.creds(), rid="new")

    def test_T14_binding_mismatch_history_only_and_unlisted_deny(self):
        owner, c = self._enrolled_owner(); self.claimed(c)
        resident._takeover_from_native(self.paths, self.packet(owner))
        current = self.current()
        for change in ("parent", "worker", "slot", "history", "unlisted"):
            v = json.loads(json.dumps(current))
            audit = v["qualification"]["takeover"]
            if change == "history":
                v["qualification"]["takeover_history"] = [audit]
                v["qualification"]["takeover"] = {"request_bindings": {}, "collect_only": [], "cancel_prepared": []}
            elif change == "unlisted": audit["collect_only"] = []
            else: audit["request_bindings"]["request"]["prior_parent" if change == "parent" else change] = "wrong"
            self.write_fixture(self.paths.state_dir / "resident-owner.json", v)
            before = self.snapshot()
            with self.caller(self.creds(takeover_settlement=True, request="request", request_parent="parent")), self.assertRaises(core.DispatchError):
                core.abandon_assignment("request", reason="operator", paths=self.paths)
            self.assertEqual(self.snapshot(), before)
        self.write_fixture(self.paths.state_dir / "resident-owner.json", current)

    def test_T15_handoff_ignores_stale_marker_but_current_blocks(self):
        owner, c = self._enrolled_owner(); self.claimed(c)
        resident.control("collector-open", c, self.paths)
        resident._takeover_from_native(self.paths, self.packet(owner))
        current = self.current(); credentials = self.creds(new_parent="01a0ac4b-313b-7f33-9ca0-f76d994ef745")
        with core.state_lock(self.paths) as locked, self.assertRaisesRegex(core.BusyError, "slots must be idle"):
            resident._commit_handoff(self.paths, locked, current, credentials)
        resident.control("collector-open", self.creds(), self.paths)
        with core.state_lock(self.paths) as locked, self.assertRaisesRegex(core.BusyError, "Recovery owner must finish"):
            resident._commit_handoff(self.paths, locked, current, credentials)

    def test_T4_settlement_crashes_before_and_after_each_write_converge(self):
        for target in ("abandon", "release", "end"):
            for after in (False, True):
                with self.subTest(target=target, after=after):
                    self.tearDown(); self.setUp()
                    owner, c = self._enrolled_owner(); self.claimed(c, status="prepared")
                    resident._takeover_from_native(self.paths, self.packet(owner))
                    credentials = self.creds(); generation = credentials["generation"]
                    obj, name = {"abandon": (core, "abandon_assignment"), "release": (Queue, "release"),
                                 "end": (resident, "write")}[target]
                    original = getattr(obj, name)
                    def crash(*args, **kwargs):
                        if after: original(*args, **kwargs)
                        raise RuntimeError("injected crash")
                    with patch.object(obj, name, crash), self.assertRaisesRegex(RuntimeError, "injected crash"):
                        resident.control("settle", credentials, self.paths)
                    resident.control("settle", credentials, self.paths)
                    self.assertEqual(self.current()["generation"], generation)
                    self.assertEqual(self.current()["slots"][0]["phase"], "idle")
                    self.assertEqual(Queue(self.paths).load("request")["state"], "released")
                    self.assertEqual(core.load_assignment("request", self.paths)["submission_count"], 0)

    def test_normal_recover_start_collector_uses_recorded_parent_without_takeover(self):
        owner, c = self._enrolled_owner(); claim, _ = self.claimed(c)
        resident.control("recover-start", dict(c, evidence_file=str(self.evidence),
            evidence_sha256=self.evidence_hash), self.paths)
        resident.control("collector-open", self.creds(), self.paths)
        with self.caller(self.creds(collector_only=True, request="request", request_parent="parent")):
            result = Queue(self.paths).observe("request", "parent", True, self.late_history("request", claim))
        self.assertEqual(result["observation"], "published")

    def test_T9_malformed_marker_and_host_shape_are_named_refusals(self):
        owner, _ = self._enrolled_owner()
        packet = self.packet(owner)
        for raw, outcome in (("not json", "old_owner_unreadable"),
                             (json.dumps({"schemaVersion": 1, "thread": {"id": owner["parent"],
                              "kind": "codex", "hostId": "local", "status": {"type": []}}}), "old_owner_status_unsupported")):
            packet["owner_read_text"] = raw; Path(packet["evidence_path"]).write_text(raw)
            before = self.snapshot()
            self.assertEqual(resident._takeover_from_native(self.paths, packet)["outcome"], outcome)
            self.assertEqual(self.snapshot(), before)
        self.write_fixture(self.paths.state_dir / "resident-recovery.json", {})
        before = self.snapshot()
        self.assertEqual(resident._takeover_from_native(self.paths, self.packet(owner))["outcome"], "request_evidence_invalid")
        self.assertEqual(self.snapshot(), before)

    def test_T12_cooldown_survives_takeover_and_blocks_idle_worker(self):
        owner, c = self._enrolled_owner(); _, old = self.claimed(c)
        with self.caller(old):
            core.mark_unusual_activity_403("request", reason="fixture cooldown", paths=self.paths)
        receipt = core.load_assignment("request", self.paths)
        resident._takeover_from_native(self.paths, self.packet(owner))
        resident.control("start", self.creds(), self.paths)
        q = Queue(self.paths); q.submit("fresh", b"must wait", "client")
        with self.caller(self.creds(invocation="fresh", request="fresh")), self.assertRaises(core.CooldownError):
            q.claim(REPLACEMENT, True, "fresh")
        self.assertEqual(q.load("fresh")["state"], "queued")
        self.assertEqual(core.load_assignment("request", self.paths), receipt)
        self.assertIsNotNone(core.active_cooldown(self.paths))

    def test_T14_cancellation_preparation_rejects_changed_stored_hash_before_write(self):
        owner, c = self._enrolled_owner(); self.claimed(c, status="prepared")
        q = Queue(self.paths); core.assignment_path("request", self.paths).unlink()
        record = q.load("request"); record["receipt_established"] = False
        record["wrapped_prompt_sha256"] = "0" * 64
        self.write_fixture(q.path("request"), record)
        self.assertEqual(resident._takeover_from_native(self.paths, self.packet(owner))["outcome"], "committed")
        before = self.snapshot()
        with self.assertRaises(core.DispatchError):
            resident.control("settle", self.creds(), self.paths)
        self.assertEqual(self.snapshot(), before)

    def test_T4_crash_after_cancellation_receipt_creation_converges(self):
        owner, c = self._enrolled_owner(); self.claimed(c, status="prepared")
        q = Queue(self.paths); core.assignment_path("request", self.paths).unlink()
        record = q.load("request"); record["receipt_established"] = False
        self.write_fixture(q.path("request"), record)
        resident._takeover_from_native(self.paths, self.packet(owner))
        prepare = core.prepare_assignment
        def crash(*args, **kwargs):
            prepare(*args, **kwargs)
            raise RuntimeError("receipt committed before crash")
        with patch.object(core, "prepare_assignment", crash), self.assertRaises(RuntimeError):
            resident.control("settle", self.creds(), self.paths)
        self.assertEqual(core.load_assignment("request", self.paths)["status"], "prepared")
        self.assertFalse(q.load("request")["receipt_established"])
        resident.control("settle", self.creds(), self.paths)
        self.assertEqual(q.load("request")["state"], "released")
        self.assertEqual(self.current()["slots"][0]["phase"], "idle")

    def test_T15_crash_before_and_after_marker_rename_converges(self):
        for after in (False, True):
            with self.subTest(after=after):
                self.tearDown(); self.setUp()
                owner, c = self._enrolled_owner(); self.claimed(c)
                resident.control("collector-open", c, self.paths)
                marker = self.paths.state_dir / "resident-recovery.json"
                raw = marker.read_bytes()
                resident._takeover_from_native(self.paths, self.packet(owner))
                credentials = self.creds(); rename = os.rename
                def crash(*args, **kwargs):
                    if after: rename(*args, **kwargs)
                    raise OSError("injected rename crash")
                with patch.object(os, "rename", crash), self.assertRaises(OSError):
                    resident.control("settle", credentials, self.paths)
                resident.control("settle", credentials, self.paths)
                resident.control("settle", credentials, self.paths)
                archived = list((self.paths.state_dir / "markers/retired").glob("*"))
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].read_bytes(), raw)
                self.assertFalse(marker.exists())
                self.assertEqual(self.current()["generation"], owner["generation"] + 1)
                self.assertEqual(self.current()["slots"][0]["phase"], "collect_only")


if __name__ == "__main__":
    unittest.main()
