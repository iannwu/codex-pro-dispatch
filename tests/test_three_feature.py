from __future__ import annotations

import hashlib
import json
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_pro_dispatch import core, resident
from codex_pro_dispatch.queue import Queue


class ThreeFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve(strict=True)
        self.paths = core.RuntimePaths(root / "config", root / "state")
        self.paths.config_dir.mkdir(mode=0o700)
        self.paths.state_dir.mkdir(mode=0o700)
        core.save_worker("worker-a", confirm_worker=True, paths=self.paths)
        self.legacy_bytes = self.paths.worker_file.read_bytes()
        self.legacy_hash = hashlib.sha256(self.legacy_bytes).hexdigest()
        self.evidence = root / "pool-evidence.json"
        self.evidence.write_text(json.dumps({
            "kind": "legacy_quiescence",
            "config_dir": str(self.paths.config_dir),
            "state_dir": str(self.paths.state_dir),
            "implementation": "isolated fixture",
            "observations": "no live owner or listener in fixture",
            "authorization": "test-only explicit maintenance evidence",
            "physical_quiescence": True,
        }))
        self.evidence.chmod(0o600)
        self.evidence_hash = hashlib.sha256(self.evidence.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def activate(self):
        return core.activate_worker_pool([
            {"slot": "slot-a", "conversation_id": "worker-a", "label": "A",
             "model_confirmation": "user-confirmed-worker", "configured_at": "fixture"},
            {"slot": "slot-b", "conversation_id": "worker-b", "label": "B",
             "model_confirmation": "user-confirmed-pro", "configured_at": "fixture"},
        ], expected_legacy_sha256=self.legacy_hash,
            evidence_file=self.evidence, evidence_sha256=self.evidence_hash,
            paths=self.paths)

    def test_pool_activation_preserves_legacy_bytes_and_model_neutral_fields(self):
        pool = self.activate()
        self.assertEqual(self.paths.worker_file.read_bytes(), self.legacy_bytes)
        self.assertEqual([worker.slot for worker in pool.workers], ["slot-a", "slot-b"])
        self.assertEqual(pool.legacy_worker_sha256, self.legacy_hash)
        self.assertNotIn("model", json.loads(self.paths.worker_pool_file.read_text()))
        self.assertNotIn("effort", json.loads(self.paths.worker_pool_file.read_text()))

    def test_pool_activation_refusal_is_write_free(self):
        before = self.paths.worker_file.read_bytes()
        with self.assertRaises(core.StateError):
            core.activate_worker_pool([
                {"slot": "slot-a", "conversation_id": "worker-a", "label": "A",
                 "model_confirmation": "user-confirmed-worker", "configured_at": "fixture"},
                {"slot": "slot-b", "conversation_id": "worker-b", "label": "B",
                 "model_confirmation": "user-confirmed-pro", "configured_at": "fixture"},
            ], expected_legacy_sha256="0" * 64,
                evidence_file=self.evidence, evidence_sha256=self.evidence_hash,
                paths=self.paths)
        self.assertFalse(self.paths.worker_pool_file.exists())
        self.assertEqual(self.paths.worker_file.read_bytes(), before)

    def test_two_claims_use_stable_slots_and_third_waits(self):
        self.activate()
        queue = Queue(self.paths)
        for request in ("request-a", "request-b", "request-c"):
            queue.submit(request, request.encode(), "client")
        # The canonical queue lock makes the claim decision atomic. Submit the
        # oldest request first so this fixture tests capacity rather than
        # depending on thread scheduling to establish FIFO order.
        claims = [
            queue.claim("parent", True, "request-a"),
            queue.claim("parent", True, "request-b"),
        ]
        self.assertEqual({claim["worker_slot"] for claim in claims}, {"slot-a", "slot-b"})
        third = queue.claim("parent", True, "request-c")
        self.assertEqual(third["action"], "queued")
        self.assertEqual(third["reason"], "no_worker_slot")
        self.assertEqual(len(core.active_assignments(self.paths)), 2)

    def _enrolled_owner(self):
        self.activate()
        enrolled = resident.control("enroll", {
            "generation": 0, "owner": "owner", "parent": "parent",
            "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
        }, self.paths)
        started = resident.control("start", {
            "generation": enrolled["owner"]["generation"], "owner": "owner",
            "parent": "parent", "worker_pool_sha256": enrolled["owner"]["worker_pool_sha256"],
        }, self.paths)
        owner = started["owner"]
        base = {
            "generation": owner["generation"], "owner": "owner", "parent": "parent",
            "worker_pool_sha256": owner["worker_pool_sha256"],
        }
        return owner, base

    def _bind_session(self, owner):
        directory = (self.paths.state_dir.parent / "session").resolve()
        directory.mkdir(mode=0o700)
        helper = Path(resident.__file__).resolve().parents[2] / "skills/codex-pro-dispatch/scripts/pro-dispatch"
        descriptor = {
            "resident": True,
            "sessionId": "a" * 32,
            "parent": "parent",
            "configDir": str(self.paths.config_dir),
            "stateDir": str(self.paths.state_dir),
            "workerPoolSha256": owner["worker_pool_sha256"],
            "idleMs": 45000,
            "helper": str(helper),
        }
        raw = json.dumps(descriptor, separators=(",", ":")).encode()
        path = directory / "session.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        binding = {
            "directory": str(directory),
            "session_id": "a" * 32,
            "descriptor_sha256": hashlib.sha256(raw).hexdigest(),
        }
        resident.control("bind-session", {
            "generation": owner["generation"], "owner": owner["owner"],
            "parent": owner["parent"], "worker_pool_sha256": owner["worker_pool_sha256"],
            "session": binding,
        }, self.paths)
        return directory, binding

    def _arm_crash_request(self, owner, base, request="request-crash"):
        queue = Queue(self.paths)
        queue.submit(request, b"possibly sent after crash", "client")
        credentials = {**base, "invocation": "invocation-crash", "request": request}
        token = resident.invocation.set(credentials)
        try:
            queue.claim("parent", True, request)
            core.arm_for_send("slot-a", request, owner["generation"],
                              "invocation-crash", self.paths)
        finally:
            resident.invocation.reset(token)
        return credentials

    def test_pool_start_installs_the_opening_attempt(self):
        owner, base = self._enrolled_owner()
        started = resident.control("start", {
            "generation": owner["generation"], "owner": "open-attempt",
            "parent": "parent", "worker_pool_sha256": owner["worker_pool_sha256"],
        }, self.paths)
        self.assertEqual(started["state"], "ready")
        self.assertEqual(started["reason"], "owner_acquired")
        self.assertEqual(started["owner"]["owner"], "open-attempt")
        self.assertEqual(started["owner"]["generation"], owner["generation"] + 1)
        stale = resident.control("start", {
            "generation": owner["generation"], "owner": "other-attempt",
            "parent": "parent", "worker_pool_sha256": owner["worker_pool_sha256"],
        }, self.paths)
        self.assertEqual(stale["state"], "busy")
        self.assertEqual(stale["reason"], "owner_changed")
        self.assertEqual(
            resident.control("inspect", {}, self.paths)["owner"]["owner"],
            "open-attempt",
        )

    def test_arm_for_send_is_exact_and_reservation_blocks_replacement(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-arm", b"send exactly once", "client")
        credentials = {**base, "invocation": "invocation-arm", "request": "request-arm"}
        token = resident.invocation.set(credentials)
        try:
            claim = queue.claim("parent", True, "request-arm")
            self.assertEqual(claim["worker_slot"], "slot-a")
            with self.assertRaisesRegex(core.StateError, "generation or slot differs"):
                core.arm_for_send(
                    "slot-a", "request-arm", owner["generation"] + 1,
                    "invocation-arm", self.paths,
                )
            mismatched = {**credentials, "slot": "slot-b"}
            mismatched_token = resident.invocation.set(mismatched)
            try:
                with self.assertRaisesRegex(core.StateError, "generation or slot differs"):
                    core.arm_for_send(
                        "slot-a", "request-arm", owner["generation"],
                        "invocation-arm", self.paths,
                    )
            finally:
                resident.invocation.reset(mismatched_token)
            armed = core.arm_for_send("slot-a", "request-arm", owner["generation"],
                                      "invocation-arm", self.paths)
            self.assertEqual(armed["assignment"]["status"], "armed")
            self.assertTrue(armed["assignment"]["no_resend"])
            with self.assertRaises(core.StateError):
                core.arm_for_send("slot-a", "request-arm", owner["generation"],
                                  "invocation-arm", self.paths)
            blocked = resident.control("start", base, self.paths)
            self.assertEqual(blocked["state"], "busy")
            self.assertEqual(blocked["reason"], "original_invocation_not_finished")
        finally:
            resident.invocation.reset(token)

    def test_recovery_fences_stale_invocation_and_opens_one_collector(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-recover", b"possibly sent", "client")
        old_credentials = {**base, "invocation": "invocation-old", "request": "request-recover"}
        token = resident.invocation.set(old_credentials)
        try:
            queue.claim("parent", True, "request-recover")
            core.arm_for_send("slot-a", "request-recover", owner["generation"],
                              "invocation-old", self.paths)
        finally:
            resident.invocation.reset(token)

        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-recovered",
        }, self.paths)
        self.assertEqual(recovery["state"], "collect_only")
        new_owner = recovery["owner"]
        self.assertEqual(new_owner["generation"], owner["generation"] + 1)
        self.assertEqual(new_owner["slots"][0]["phase"], "collect_only")
        stale = {**old_credentials}
        stale_token = resident.invocation.set(stale)
        try:
            with self.assertRaises(core.DispatchError):
                core.arm_for_send("slot-a", "request-recover", owner["generation"],
                                  "invocation-old", self.paths)
        finally:
            resident.invocation.reset(stale_token)

        collector = {
            "generation": new_owner["generation"], "owner": "owner-recovered",
            "parent": "parent", "worker_pool_sha256": new_owner["worker_pool_sha256"],
        }
        opened = resident.control("collector-open", collector, self.paths)
        self.assertEqual(opened["mode"], "collector-only")
        collector_token = resident.invocation.set({**collector, "collector_only": True})
        try:
            with self.assertRaises(core.StateError):
                queue.claim("parent", True, "request-recover")
        finally:
            resident.invocation.reset(collector_token)
        with self.assertRaises(core.BusyError):
            resident.control("collector-open", collector, self.paths)
        started = resident.control("start", collector, self.paths)
        self.assertEqual(started["state"], "ready")
        self.assertEqual(started["automatic_startup"], "unsupported")

    def test_crash_without_transport_audit_fences_and_stays_collect_only(self):
        owner, base = self._enrolled_owner()
        directory, binding = self._bind_session(owner)
        blocked = resident.control("start", base, self.paths)
        self.assertEqual(blocked["reason"], "automatic_startup_unsupported")
        self.assertEqual(blocked["send_authorized"], False)
        credentials = self._arm_crash_request(owner, base)
        still = resident.control("start", base, self.paths)
        self.assertEqual(still["reason"], "original_invocation_not_finished")
        self.assertEqual(still["send_authorized"], False)
        marker = directory / ("waiting-1." + binding["session_id"])
        marker.mkdir(mode=0o700)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(directory / "wake.sock"))
        stale.listen(1)
        stale.close()
        with core.state_lock(self.paths) as locked:
            with self.assertRaisesRegex(core.StateError, "durable closure evidence"):
                resident.require_closed_session(
                    self.paths, resident.read(self.paths, locked), _locked=locked
                )
        session_bytes = (directory / "session.json").read_bytes()
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-crash",
        }, self.paths)
        self.assertEqual(recovery["state"], "collect_only")
        self.assertEqual(recovery["session_closure"], "no_graceful_audit")
        self.assertEqual(recovery["send_authorized"], False)
        self.assertEqual(recovery["request_ids"], ["request-crash"])
        self.assertEqual(recovery["prepared_ids"], [])
        self.assertIsNone(recovery["owner"]["session"])
        self.assertEqual(recovery["owner"]["slots"][0]["phase"], "collect_only")
        self.assertTrue(marker.is_dir())
        self.assertTrue((directory / "wake.sock").exists())
        self.assertFalse((directory / "transport-audit.json").exists())
        self.assertEqual((directory / "session.json").read_bytes(), session_bytes)
        stale_token = resident.invocation.set(credentials)
        try:
            with self.assertRaises(core.DispatchError):
                core.arm_for_send("slot-a", "request-crash", owner["generation"],
                                  "invocation-crash", self.paths)
        finally:
            resident.invocation.reset(stale_token)

    def test_reboot_without_session_directory_fences_collect_only(self):
        owner, base = self._enrolled_owner()
        directory, _binding = self._bind_session(owner)
        credentials = self._arm_crash_request(owner, base, "request-reboot")
        shutil.rmtree(directory)
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-reboot",
        }, self.paths)
        self.assertEqual(recovery["state"], "collect_only")
        self.assertEqual(recovery["session_closure"], "session_absent")
        self.assertEqual(recovery["request_ids"], ["request-reboot"])
        self.assertEqual(recovery["owner"]["slots"][0]["phase"], "collect_only")
        self.assertFalse(directory.exists())
        stale_token = resident.invocation.set(credentials)
        try:
            with self.assertRaises(core.DispatchError):
                core.arm_for_send("slot-a", "request-reboot", owner["generation"],
                                  "invocation-crash", self.paths)
        finally:
            resident.invocation.reset(stale_token)

    def test_live_listener_blocks_crash_recovery(self):
        owner, base = self._enrolled_owner()
        directory, _binding = self._bind_session(owner)
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(str(directory / "wake.sock"))
        live.listen(1)
        before = (self.paths.state_dir / "resident-owner.json").read_bytes()
        try:
            with self.assertRaisesRegex(core.StateError, "still has a listener"):
                resident.control("recover-start", {
                    **base, "evidence_file": str(self.evidence),
                    "evidence_sha256": self.evidence_hash, "new_owner": "owner-live",
                }, self.paths)
        finally:
            live.close()
        self.assertEqual((self.paths.state_dir / "resident-owner.json").read_bytes(), before)
        self.assertEqual(
            resident.control("inspect", {}, self.paths)["owner"]["generation"],
            owner["generation"],
        )

    def test_graceful_audit_still_fences_when_present(self):
        owner, base = self._enrolled_owner()
        directory, binding = self._bind_session(owner)
        self._arm_crash_request(owner, base, "request-closed")
        audit = directory / "transport-audit.json"
        audit.write_bytes(json.dumps({
            "sessionId": binding["session_id"], "reason": "resident_failed", "events": [],
        }).encode())
        audit.chmod(0o600)
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-closed",
        }, self.paths)
        self.assertEqual(recovery["session_closure"], "graceful_audit")
        self.assertEqual(recovery["state"], "collect_only")
        self.assertTrue(audit.exists())

    def test_pool_fences_scalar_reset_and_rollback(self):
        owner, base = self._enrolled_owner()
        with self.assertRaises(core.StateError):
            core.reset_worker(force=True, paths=self.paths)
        with self.assertRaises(core.StateError) as raised:
            resident.control("rollback-check", base, self.paths)
        self.assertIn("fenced", str(raised.exception).lower())

    def test_legacy_owner_bytes_are_readable_and_activation_refuses_inflight(self):
        owner_path = self.paths.state_dir / "resident-owner.json"
        core.atomic_write_json(owner_path, {
            "version": 2, "generation": 1, "owner": "legacy", "parent": "parent",
            "worker": "worker-a", "inflight": {"invocation": "live", "request": "stuck"},
            "session": None, "qualification": {"sha256": self.evidence_hash, "evidence": {}},
        }, _locked=_Lock(self.paths))
        before = owner_path.read_bytes()
        worker_before = self.paths.worker_file.read_bytes()
        with self.assertRaises(core.BusyError):
            self.activate()
        self.assertFalse(self.paths.worker_pool_file.exists())
        self.assertEqual(owner_path.read_bytes(), before)
        self.assertEqual(self.paths.worker_file.read_bytes(), worker_before)
        loaded = core.read_json(owner_path)
        self.assertEqual(loaded["version"], 2)
        self.assertEqual(loaded["inflight"]["request"], "stuck")

    def test_legacy_scalar_owner_allows_only_its_bound_collector_observation(self):
        owner_path = self.paths.state_dir / "resident-owner.json"
        owner = {
            "version": 2, "generation": 1, "owner": "legacy", "parent": "parent",
            "worker": "worker-a", "inflight": {"invocation": "live", "request": "stuck"},
            "session": None, "qualification": {"sha256": self.evidence_hash, "evidence": {}},
        }
        core.atomic_write_json(owner_path, owner, _locked=_Lock(self.paths))
        credentials = {
            "generation": 1, "owner": "legacy", "parent": "parent", "worker": "worker-a",
        }
        resident.control("collector-open", credentials, self.paths)
        token = resident.invocation.set({**credentials, "collector_only": True})
        try:
            with core.state_lock(self.paths, create=False) as locked:
                resident.validate_collector_observation(self.paths, locked, "stuck")
                with resident.collector_observation():
                    resident.guard(self.paths, locked, "stuck")
                with self.assertRaises(core.BusyError):
                    resident.guard(self.paths, locked, "stuck")
            with core.state_lock(self.paths, create=False) as locked:
                with self.assertRaises(core.StateError):
                    resident.validate_collector_observation(self.paths, locked, "other")
        finally:
            resident.invocation.reset(token)

    def test_prepared_crash_can_still_arm_on_fenced_generation(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-prepared", b"unsent", "client")
        credentials = {**base, "invocation": "inv-prepared", "request": "request-prepared"}
        token = resident.invocation.set(credentials)
        try:
            claim = queue.claim("parent", True, "request-prepared")
            self.assertEqual(claim["action"], "arm_then_send_once")
        finally:
            resident.invocation.reset(token)
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-prepared",
        }, self.paths)
        self.assertEqual(recovery["owner"]["slots"][0]["phase"], "reserved")
        self.assertEqual(recovery["state"], "ready")
        self.assertEqual(recovery["prepared_ids"], ["request-prepared"])
        self.assertEqual(recovery["request_ids"], [])
        new = recovery["owner"]
        adopted = {
            "generation": new["generation"], "owner": "owner-prepared", "parent": "parent",
            "worker_pool_sha256": new["worker_pool_sha256"],
            "invocation": "inv-new", "request": "request-prepared",
        }
        token = resident.invocation.set(adopted)
        try:
            resident.control("begin", {**adopted, "slot": "slot-a"}, self.paths)
            armed = core.arm_for_send(
                "slot-a", "request-prepared", new["generation"], "inv-new", self.paths
            )
            self.assertEqual(armed["assignment"]["status"], "armed")
            self.assertTrue(armed["assignment"]["no_resend"])
        finally:
            resident.invocation.reset(token)

    def test_claim_to_receipt_crash_recovers_as_sendable_without_reusing_a_slot(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-pre-receipt", b"not armed yet", "client")
        old = {**base, "invocation": "inv-old", "request": "request-pre-receipt"}
        token = resident.invocation.set(old)
        try:
            with patch("codex_pro_dispatch.core.prepare_assignment",
                       side_effect=RuntimeError("crash before receipt")):
                with self.assertRaisesRegex(RuntimeError, "crash before receipt"):
                    queue.claim("parent", True, "request-pre-receipt")
        finally:
            resident.invocation.reset(token)
        claimed = queue.load("request-pre-receipt")
        self.assertEqual(claimed["state"], "claimed")
        self.assertNotIn("receipt_established", claimed)
        self.assertFalse(core.assignment_path("request-pre-receipt", self.paths).exists())

        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence),
            "evidence_sha256": self.evidence_hash, "new_owner": "owner-receiptless",
        }, self.paths)
        self.assertEqual(recovery["state"], "ready")
        self.assertEqual(recovery["prepared_ids"], ["request-pre-receipt"])
        self.assertEqual(recovery["request_ids"], [])
        new = recovery["owner"]
        self.assertEqual(queue.load("request-pre-receipt")["owner_generation"],
                         new["generation"])
        adopted = {
            "generation": new["generation"], "owner": "owner-receiptless",
            "parent": "parent", "worker_pool_sha256": new["worker_pool_sha256"],
            "invocation": "inv-new", "request": "request-pre-receipt",
            "slot": "slot-a",
        }
        resident.control("begin", adopted, self.paths)
        new_token = resident.invocation.set(adopted)
        try:
            resumed = queue.claim("parent", True, "request-pre-receipt")
            self.assertEqual(resumed["action"], "arm_then_send_once")
            self.assertEqual(resumed["dispatch_status"], "prepared")
            armed = core.arm_for_send(
                "slot-a", "request-pre-receipt", new["generation"],
                "inv-new", self.paths,
            )
            self.assertEqual(armed["assignment"]["status"], "armed")
            self.assertTrue(armed["assignment"]["no_resend"])
        finally:
            resident.invocation.reset(new_token)

    def test_account_cooldown_is_shared_across_slots(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-cool", b"cool", "client")
        queue.submit("request-other", b"other", "client")
        credentials = {**base, "invocation": "inv-cool", "request": "request-cool"}
        token = resident.invocation.set(credentials)
        try:
            queue.claim("parent", True, "request-cool")
            core.arm_for_send("slot-a", "request-cool", owner["generation"],
                              "inv-cool", self.paths)
            core.mark_unusual_activity_403(
                "request-cool", reason="unusual activity 403", paths=self.paths
            )
            other = {**base, "invocation": "inv-other", "request": "request-other"}
            other_token = resident.invocation.set(other)
            try:
                with self.assertRaises(core.CooldownError):
                    queue.claim("parent", True, "request-other")
            finally:
                resident.invocation.reset(other_token)
        finally:
            resident.invocation.reset(token)

    def test_arm_for_send_respects_account_cooldown(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-cool", b"cool", "client")
        queue.submit("request-other", b"other", "client")
        first = {**base, "invocation": "inv-cool", "request": "request-cool"}
        token = resident.invocation.set(first)
        try:
            self.assertEqual(queue.claim("parent", True, "request-cool")["worker_slot"], "slot-a")
            other = {**base, "invocation": "inv-other", "request": "request-other"}
            other_token = resident.invocation.set(other)
            try:
                self.assertEqual(queue.claim("parent", True, "request-other")["worker_slot"], "slot-b")
            finally:
                resident.invocation.reset(other_token)
            core.arm_for_send(
                "slot-a", "request-cool", owner["generation"], "inv-cool", self.paths
            )
            core.mark_unusual_activity_403(
                "request-cool", reason="unusual activity 403", paths=self.paths
            )
            other_token = resident.invocation.set(other)
            try:
                with self.assertRaises(core.CooldownError):
                    core.arm_for_send(
                        "slot-b", "request-other", owner["generation"],
                        "inv-other", self.paths
                    )
                self.assertEqual(
                    core.load_assignment("request-other", self.paths)["status"], "prepared"
                )
            finally:
                resident.invocation.reset(other_token)
        finally:
            resident.invocation.reset(token)

    def test_two_clients_cannot_double_claim_one_slot(self):
        core.activate_worker_pool([
            {"slot": "slot-a", "conversation_id": "worker-a", "label": "A",
             "model_confirmation": "user-confirmed-worker", "configured_at": "fixture"},
        ], expected_legacy_sha256=self.legacy_hash,
            evidence_file=self.evidence, evidence_sha256=self.evidence_hash,
            paths=self.paths)
        queue = Queue(self.paths)
        queue.submit("request-a", b"first", "client")
        queue.submit("request-b", b"second", "client")
        results = []

        def claim(request):
            results.append(queue.claim("parent", True, request))

        first = threading.Thread(target=claim, args=("request-a",))
        second = threading.Thread(target=claim, args=("request-b",))
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(len(results), 2)
        actions = sorted(item.get("action") for item in results)
        self.assertEqual(actions, ["arm_then_send_once", "queued"])
        queued = next(item for item in results if item.get("action") == "queued")
        self.assertIn(queued["reason"], {"no_worker_slot", "fifo_wait"})
        self.assertEqual(len(core.active_assignments(self.paths)), 1)

    def test_two_clients_claim_distinct_slots_under_the_lock(self):
        self.activate()
        queue = Queue(self.paths)
        queue.submit("request-a", b"first", "client")
        queue.submit("request-b", b"second", "client")
        barrier = threading.Barrier(2)
        results = []

        def claim():
            barrier.wait()
            results.append(queue.claim("parent", True))

        first = threading.Thread(target=claim)
        second = threading.Thread(target=claim)
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(
            sorted(item.get("action") for item in results),
            ["arm_then_send_once", "arm_then_send_once"],
        )
        self.assertEqual(
            {item["worker_slot"] for item in results},
            {"slot-a", "slot-b"},
        )
        self.assertEqual(
            {item["assignment_id"] for item in results},
            {"request-a", "request-b"},
        )
        pool = core.load_worker_pool(self.paths)
        status = core.worker_pool_runtime_status(pool, self.paths)
        self.assertEqual(status["capacity"], 2)
        self.assertEqual(status["occupied_slots"], ["slot-a", "slot-b"])
        self.assertEqual(status["automatic_startup"], "unsupported")
        encoded = json.dumps(status)
        self.assertNotIn("token", encoded)
        self.assertNotIn("wrapped_prompt", encoded)
        self.assertNotIn("answer", encoded)

    def test_prepared_recovery_is_scheduled_not_collector_only(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-prepared", b"unsent", "client")
        credentials = {**base, "invocation": "inv-prepared", "request": "request-prepared"}
        token = resident.invocation.set(credentials)
        try:
            self.assertEqual(queue.claim("parent", True, "request-prepared")["action"],
                             "arm_then_send_once")
        finally:
            resident.invocation.reset(token)
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-prepared",
        }, self.paths)
        self.assertEqual(recovery["state"], "ready")
        self.assertEqual(recovery["prepared_ids"], ["request-prepared"])
        self.assertEqual(recovery["request_ids"], [])
        adopted = {
            "generation": recovery["owner"]["generation"], "owner": "owner-prepared",
            "parent": "parent", "worker_pool_sha256": recovery["owner"]["worker_pool_sha256"],
        }
        started = resident.control("start", adopted, self.paths)
        self.assertEqual(started["state"], "ready")
        self.assertEqual(started["prepared_ids"], ["request-prepared"])
        self.assertEqual(started["request_ids"], [])
        serving = {
            **adopted, "generation": started["owner"]["generation"],
            "invocation": "inv-new", "request": "request-prepared",
        }
        token = resident.invocation.set(serving)
        try:
            resident.control("begin", {**serving, "slot": "slot-a"}, self.paths)
            with self.assertRaisesRegex(core.StateError, "generation or slot differs"):
                core.arm_for_send(
                    "slot-a", "request-prepared", owner["generation"], "inv-new", self.paths
                )
            armed = core.arm_for_send(
                "slot-a", "request-prepared", serving["generation"], "inv-new", self.paths
            )
            self.assertEqual(armed["assignment"]["status"], "armed")
            self.assertTrue(armed["assignment"]["no_resend"])
            self.assertEqual(armed["owner_generation"], serving["generation"])
        finally:
            resident.invocation.reset(token)

    def test_begin_without_slot_rebinds_the_reserved_request(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-prepared", b"unsent", "client")
        credentials = {**base, "invocation": "inv-old", "request": "request-prepared"}
        token = resident.invocation.set(credentials)
        try:
            resident.control("begin", {**credentials, "slot": "slot-a"}, self.paths)
            self.assertEqual(queue.claim("parent", True, "request-prepared")["worker_slot"],
                             "slot-a")
        finally:
            resident.invocation.reset(token)
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-rebind",
        }, self.paths)
        adopted = {
            "generation": recovery["owner"]["generation"], "owner": "owner-rebind",
            "parent": "parent", "worker_pool_sha256": recovery["owner"]["worker_pool_sha256"],
        }
        started = resident.control("start", adopted, self.paths)
        serving = {
            **adopted, "generation": started["owner"]["generation"],
            "invocation": "inv-new", "request": "request-prepared",
        }
        token = resident.invocation.set(serving)
        try:
            begun = resident.control("begin", serving, self.paths)
            self.assertEqual(begun["worker_slot"], "slot-a")
            slots = {
                item["slot"]: item
                for item in resident.control("inspect", {}, self.paths)["owner"]["slots"]
            }
            self.assertEqual(slots["slot-a"]["request"], "request-prepared")
            self.assertEqual(slots["slot-a"]["invocation"]["invocation"], "inv-new")
            self.assertIsNone(slots["slot-b"]["request"])
            self.assertEqual(slots["slot-b"]["phase"], "idle")
            armed = core.arm_for_send(
                "slot-a", "request-prepared", serving["generation"], "inv-new", self.paths
            )
            self.assertEqual(armed["worker_slot"], "slot-a")
        finally:
            resident.invocation.reset(token)

    def test_enroll_binds_retained_claim_and_begin_skips_that_slot(self):
        queue = Queue(self.paths)
        queue.submit("request-legacy", b"leftover prepared", "client")
        leftover = queue.claim("parent", True, "request-legacy")
        self.assertEqual(leftover["action"], "arm_then_send_once")
        self.activate()
        enrolled = resident.control("enroll", {
            "generation": 0, "owner": "owner", "parent": "parent",
            "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
        }, self.paths)
        slots = {item["slot"]: item for item in enrolled["owner"]["slots"]}
        self.assertEqual(slots["slot-a"]["request"], "request-legacy")
        self.assertEqual(slots["slot-a"]["phase"], "reserved")
        self.assertIsNone(slots["slot-b"]["request"])
        self.assertEqual(slots["slot-b"]["phase"], "idle")
        started = resident.control("start", {
            "generation": enrolled["owner"]["generation"], "owner": "owner",
            "parent": "parent", "worker_pool_sha256": enrolled["owner"]["worker_pool_sha256"],
        }, self.paths)
        self.assertEqual(started["prepared_ids"], ["request-legacy"])
        self.assertEqual(started["request_ids"], [])
        queue.submit("request-other", b"new work", "client")
        serving = {
            "generation": started["owner"]["generation"], "owner": "owner",
            "parent": "parent", "worker_pool_sha256": started["owner"]["worker_pool_sha256"],
            "invocation": "inv-new", "request": "request-other",
        }
        token = resident.invocation.set(serving)
        try:
            begun = resident.control("begin", serving, self.paths)
            self.assertEqual(begun["worker_slot"], "slot-b")
            with self.assertRaisesRegex(core.BusyError, "already reserved|occupied by another claim"):
                resident.control("begin", {**serving, "slot": "slot-a"}, self.paths)
            claimed = queue.claim("parent", True, "request-other")
            self.assertEqual(claimed["worker_slot"], "slot-b")
            leftover_status = queue.status("request-legacy")
            self.assertEqual(leftover_status["state"], "claimed")
            self.assertEqual(leftover_status["worker_conversation_id"], "worker-a")
        finally:
            resident.invocation.reset(token)

    def test_reserved_claim_refuses_a_slot_occupied_by_another_claim(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-held", b"held", "client")
        queue.submit("request-new", b"new", "client")
        held = {**base, "invocation": "inv-held", "request": "request-held"}
        token = resident.invocation.set(held)
        try:
            self.assertEqual(queue.claim("parent", True, "request-held")["worker_slot"], "slot-a")
        finally:
            resident.invocation.reset(token)
        # Leave the owner slot idle while the leftover claim still occupies
        # worker-a, which is the activation/enroll hole in miniature.
        owner_path = self.paths.state_dir / "resident-owner.json"
        current = core.read_json(owner_path)
        for item in current["slots"]:
            if item["slot"] == "slot-a":
                item["request"] = None
                item["invocation"] = None
                item["phase"] = "idle"
        core.atomic_write_json(owner_path, current, _locked=_Lock(self.paths))
        serving = {**base, "invocation": "inv-new", "request": "request-new"}
        token = resident.invocation.set(serving)
        try:
            with self.assertRaisesRegex(core.BusyError, "occupied by another claim"):
                resident.control("begin", {**serving, "slot": "slot-a"}, self.paths)
            begun = resident.control("begin", serving, self.paths)
            self.assertEqual(begun["worker_slot"], "slot-b")
        finally:
            resident.invocation.reset(token)

    def test_mixed_recovery_collects_armed_and_keeps_prepared_sendable(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-armed", b"possibly sent", "client")
        queue.submit("request-prepared", b"unsent", "client")
        armed_credentials = {**base, "invocation": "inv-armed", "request": "request-armed"}
        token = resident.invocation.set(armed_credentials)
        try:
            queue.claim("parent", True, "request-armed")
            core.arm_for_send("slot-a", "request-armed", owner["generation"],
                              "inv-armed", self.paths)
        finally:
            resident.invocation.reset(token)
        prepared_credentials = {**base, "invocation": "inv-prepared", "request": "request-prepared"}
        token = resident.invocation.set(prepared_credentials)
        try:
            self.assertEqual(queue.claim("parent", True, "request-prepared")["worker_slot"],
                             "slot-b")
        finally:
            resident.invocation.reset(token)
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-mixed",
        }, self.paths)
        self.assertEqual(recovery["state"], "collect_only")
        self.assertEqual(recovery["request_ids"], ["request-armed"])
        self.assertEqual(recovery["prepared_ids"], ["request-prepared"])
        phases = {item["request"]: item["phase"] for item in recovery["owner"]["slots"]}
        self.assertEqual(phases["request-armed"], "collect_only")
        self.assertEqual(phases["request-prepared"], "reserved")
        collector = {
            "generation": recovery["owner"]["generation"], "owner": "owner-mixed",
            "parent": "parent", "worker_pool_sha256": recovery["owner"]["worker_pool_sha256"],
        }
        resident.control("collector-open", collector, self.paths)
        send = {**collector, "invocation": "inv-new", "request": "request-prepared"}
        token = resident.invocation.set(send)
        try:
            resident.control("begin", {**send, "slot": "slot-b"}, self.paths)
            with self.assertRaises(core.StateError):
                core.arm_for_send(
                    "slot-b", "request-prepared", collector["generation"], "inv-new", self.paths
                )
        finally:
            resident.invocation.reset(token)
        resident.control("collector-close", {**collector, "collector_only": True}, self.paths)
        token = resident.invocation.set(send)
        try:
            armed = core.arm_for_send(
                "slot-b", "request-prepared", collector["generation"], "inv-new", self.paths
            )
            self.assertEqual(armed["assignment"]["status"], "armed")
            self.assertTrue(armed["assignment"]["no_resend"])
        finally:
            resident.invocation.reset(token)

    def test_one_worker_pool_keeps_capacity_one(self):
        pool = core.activate_worker_pool([
            {"slot": "slot-a", "conversation_id": "worker-a", "label": "A",
             "model_confirmation": "user-confirmed-worker", "configured_at": "fixture"},
        ], expected_legacy_sha256=self.legacy_hash,
            evidence_file=self.evidence, evidence_sha256=self.evidence_hash,
            paths=self.paths)
        self.assertEqual(len(pool.workers), 1)
        queue = Queue(self.paths)
        queue.submit("request-a", b"first", "client")
        queue.submit("request-b", b"second", "client")
        first = queue.claim("parent", True, "request-a")
        self.assertEqual(first["worker_slot"], "slot-a")
        second = queue.claim("parent", True, "request-b")
        self.assertEqual(second["action"], "queued")
        status = core.worker_pool_runtime_status(pool, self.paths)
        self.assertEqual(status["capacity"], 1)
        self.assertEqual(status["occupied_slots"], ["slot-a"])

    def test_reserved_specific_claim_skips_fifo_wait(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-old", b"old", "client")
        queue.submit("request-new", b"new", "client")
        older = {**base, "invocation": "inv-old", "request": "request-old"}
        newer = {**base, "invocation": "inv-new", "request": "request-new"}
        token = resident.invocation.set(older)
        try:
            resident.control("begin", {**older, "slot": "slot-a"}, self.paths)
        finally:
            resident.invocation.reset(token)
        token = resident.invocation.set({**base, "invocation": "inv-skip",
                                         "request": "request-new"})
        try:
            waiting = queue.claim("parent", True, "request-new")
            self.assertEqual(waiting["action"], "queued")
            self.assertEqual(waiting["reason"], "fifo_wait")
        finally:
            resident.invocation.reset(token)
        token = resident.invocation.set(newer)
        try:
            resident.control("begin", {**newer, "slot": "slot-b"}, self.paths)
            claimed = queue.claim("parent", True, "request-new")
            self.assertEqual(claimed["action"], "arm_then_send_once")
            self.assertEqual(claimed["assignment_id"], "request-new")
            self.assertEqual(claimed["worker_slot"], "slot-b")
        finally:
            resident.invocation.reset(token)

    def test_begin_before_submit_then_claims_the_reserved_slot(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        credentials = {**base, "invocation": "inv-admit", "request": "request-admit"}
        token = resident.invocation.set(credentials)
        try:
            begun = resident.control("begin", credentials, self.paths)
            self.assertEqual(begun["worker_slot"], "slot-a")
            slots = {
                item["slot"]: item
                for item in resident.control("inspect", {}, self.paths)["owner"]["slots"]
            }
            self.assertEqual(slots["slot-a"]["request"], "request-admit")
            self.assertEqual(slots["slot-a"]["phase"], "reserved")
            self.assertEqual(slots["slot-a"]["invocation"]["invocation"], "inv-admit")
            self.assertIsNone(slots["slot-b"]["request"])
            queue.submit("request-admit", b"native admit then submit", "client")
            claimed = queue.claim("parent", True, "request-admit")
            self.assertEqual(claimed["action"], "arm_then_send_once")
            self.assertEqual(claimed["assignment_id"], "request-admit")
            self.assertEqual(claimed["worker_slot"], "slot-a")
            self.assertEqual(claimed["worker_conversation_id"], "worker-a")
            armed = core.arm_for_send(
                "slot-a", "request-admit", owner["generation"], "inv-admit", self.paths
            )
            self.assertEqual(armed["assignment"]["status"], "armed")
            self.assertTrue(armed["assignment"]["no_resend"])
            self.assertEqual(armed["assignment"]["worker_conversation_id"], "worker-a")
        finally:
            resident.invocation.reset(token)

    def test_begin_without_submit_recovers_by_releasing_the_slot(self):
        owner, base = self._enrolled_owner()
        credentials = {**base, "invocation": "inv-missing", "request": "request-missing"}
        token = resident.invocation.set(credentials)
        try:
            resident.control("begin", {**credentials, "slot": "slot-a"}, self.paths)
        finally:
            resident.invocation.reset(token)
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-missing",
        }, self.paths)
        self.assertEqual(recovery["state"], "ready")
        self.assertEqual(recovery["prepared_ids"], [])
        self.assertEqual(recovery["request_ids"], [])
        slots = {item["slot"]: item for item in recovery["owner"]["slots"]}
        self.assertIsNone(slots["slot-a"]["request"])
        self.assertEqual(slots["slot-a"]["phase"], "idle")
        self.assertIsNone(slots["slot-a"]["invocation"])

    def test_begin_without_claim_recovers_as_sendable(self):
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        queue.submit("request-queued", b"unsent", "client")
        credentials = {**base, "invocation": "inv-queued", "request": "request-queued"}
        token = resident.invocation.set(credentials)
        try:
            resident.control("begin", {**credentials, "slot": "slot-a"}, self.paths)
        finally:
            resident.invocation.reset(token)
        recovery = resident.control("recover-start", {
            **base, "evidence_file": str(self.evidence), "evidence_sha256": self.evidence_hash,
            "new_owner": "owner-queued",
        }, self.paths)
        self.assertEqual(recovery["state"], "ready")
        self.assertEqual(recovery["prepared_ids"], ["request-queued"])
        self.assertEqual(recovery["request_ids"], [])
        phases = {
            item["request"]: item["phase"]
            for item in recovery["owner"]["slots"] if item["request"]
        }
        self.assertEqual(phases["request-queued"], "reserved")
        adopted = {
            "generation": recovery["owner"]["generation"], "owner": "owner-queued",
            "parent": "parent", "worker_pool_sha256": recovery["owner"]["worker_pool_sha256"],
        }
        started = resident.control("start", adopted, self.paths)
        self.assertEqual(started["state"], "ready")
        serving = {
            **adopted, "generation": started["owner"]["generation"],
            "invocation": "inv-new", "request": "request-queued",
        }
        token = resident.invocation.set(serving)
        try:
            resident.control("begin", {**serving, "slot": "slot-a"}, self.paths)
            claimed = queue.claim("parent", True, "request-queued")
            self.assertEqual(claimed["action"], "arm_then_send_once")
            armed = core.arm_for_send(
                "slot-a", "request-queued", serving["generation"], "inv-new", self.paths
            )
            self.assertEqual(armed["assignment"]["status"], "armed")
            self.assertTrue(armed["assignment"]["no_resend"])
        finally:
            resident.invocation.reset(token)

    def test_missing_pid_or_socket_does_not_authorize_serving_takeover(self):
        owner, base = self._enrolled_owner()
        owner_path = self.paths.state_dir / "resident-owner.json"
        current = core.read_json(owner_path)
        current["session"] = {
            "directory": "/tmp/does-not-exist-resident-session",
            "session_id": "a" * 32,
            "descriptor_sha256": "b" * 64,
        }
        core.atomic_write_json(owner_path, current, _locked=_Lock(self.paths))
        blocked = resident.control("start", base, self.paths)
        self.assertEqual(blocked["reason"], "automatic_startup_unsupported")
        self.assertEqual(blocked["automatic_startup"], "unsupported")
        self.assertEqual(blocked["send_authorized"], False)


class _Lock:
    def __init__(self, runtime):
        self.runtime = runtime

    def validate(self, runtime):
        if runtime != self.runtime:
            raise core.StateError("Lock runtime mismatch")


if __name__ == "__main__":
    unittest.main()
