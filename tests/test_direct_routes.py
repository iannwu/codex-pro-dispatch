"""Independent route fixtures. All native evidence and authority are disposable."""
import json
import os
import subprocess
import sys
from pathlib import Path
import unittest

import test_three_feature as fixtures
from codex_pro_dispatch import core, listener

ROOT = Path(__file__).resolve().parents[1]


class DirectRoutesTests(unittest.TestCase):
    setUp = fixtures.ThreeFeatureTests.setUp
    tearDown = fixtures.ThreeFeatureTests.tearDown

    def cli(self, *args, text="", success=True):
        env = dict(os.environ, CODEX_PRO_DISPATCH_HOME=str(self.paths.config_dir.parent))
        p = subprocess.run([sys.executable, str(ROOT / "bin/pro-dispatch"), *args],
                           input=text, text=True, capture_output=True, env=env)
        if success:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        return json.loads(p.stderr)

    def bridge(self):
        packet = listener.plan(self.paths, ["worker-a", "worker-b"], "Disposable fixture has no executor")
        return listener.commit(self.paths, packet, "listener-task", "listener-turn")

    def direct(self):
        return self.cli("worker", "direct-set", "--conversation-id", "direct-worker",
                        "--confirm-worker", "--native-controls-confirmed")

    def lifecycle(self, rid="direct-request"):
        prepared = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", rid,
                            "--native-controls-confirmed", text="Please answer")
        self.assertEqual(prepared["worker_conversation_id"], "direct-worker")
        self.cli("arm", rid)
        self.cli("submitted", rid, "--sent-prompt-file", "-", text=prepared["wrapped_prompt"])
        self.cli("pending", rid)
        result = core.result_marker(rid) + "\nAnswer\n" + core.end_marker(rid)
        self.cli("complete", rid, text=result)
        self.assertEqual(core.load_assignment(rid, self.paths)["status"], "complete")

    def test_public_cli_lifecycle_with_closed_bridge(self):
        self.bridge()
        self.direct()
        self.lifecycle()

    def test_established_route_ignores_malformed_bridge_lifecycle(self):
        self.bridge()
        self.direct()
        for path in [self.paths.state_dir / "resident-owner.json", self.paths.worker_pool_file]:
            path.write_text("{")
            path.chmod(0o600)
        self.lifecycle()

    def prepare(self, rid):
        return self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", rid,
                        "--native-controls-confirmed", text="Question")

    def test_two_direct_preparations_have_one_winner_and_no_resend(self):
        from concurrent.futures import ThreadPoolExecutor
        self.bridge()
        self.direct()
        env = dict(os.environ, CODEX_PRO_DISPATCH_HOME=str(self.paths.config_dir.parent))
        def attempt(rid):
            return subprocess.run([sys.executable, str(ROOT / "bin/pro-dispatch"), "prepare",
                                   "--parent-task-id", "direct-parent", "--assignment-id", rid,
                                   "--native-controls-confirmed"],
                                  input="Question", text=True, capture_output=True, env=env)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["race-a", "race-b"]))
        self.assertEqual(sorted(r.returncode == 0 for r in results), [False, True])
        winner = json.loads(next(r.stdout for r in results if r.returncode == 0))["assignment_id"]
        self.cli("arm", winner)
        self.cli("arm", winner, success=False)
        recovery = self.cli("recover", winner)
        self.assertIn("no_resend", json.dumps(recovery))
        self.cli("worker", "direct-reset", success=False)

    def test_malformed_canonical_receipt_blocks_direct_admission(self):
        self.bridge()
        self.direct()
        self.paths.assignments_dir.mkdir(mode=0o700, exist_ok=True)
        broken = self.paths.assignments_dir / "broken.json"
        broken.write_text("{")
        broken.chmod(0o600)
        error = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "new",
                         "--native-controls-confirmed", text="Question", success=False)
        self.assertIn("receipt", error["error"])

    def test_overlap_rejected_in_both_configuration_orders(self):
        self.bridge()
        self.cli("worker", "direct-set", "--conversation-id", "worker-a", "--confirm-worker",
                 "--native-controls-confirmed", success=False)
        self.direct()
        packet = listener.plan(self.paths, ["direct-worker"], "Isolated fixture")
        with self.assertRaises(core.DispatchError):
            listener.commit(self.paths, packet, "replacement-task", "replacement-turn")

    def test_direct_configuration_is_explicit_when_pool_exists(self):
        self.bridge()
        error = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "new",
                         "--native-controls-confirmed", text="Question", success=False)
        self.assertIn("direct_worker_not_configured", error["error"])

    activate = fixtures.ThreeFeatureTests.activate
    _enrolled_owner = fixtures.ThreeFeatureTests._enrolled_owner

    def occupied_bridge(self, count=1):
        from codex_pro_dispatch import resident
        from codex_pro_dispatch.queue import Queue
        owner, base = self._enrolled_owner()
        queue = Queue(self.paths)
        calls = []
        for index in range(count):
            rid = "bridge-" + str(index)
            queue.submit(rid, b"Bridge question", "client")
            caller = dict(base, invocation="inv-" + str(index), request=rid)
            token = resident.invocation.set(caller)
            try:
                claim = queue.claim("parent", True, rid)
                core.arm_for_send(claim["worker_slot"], rid, owner["generation"], caller["invocation"], self.paths)
            finally:
                resident.invocation.reset(token)
            calls.append((caller, claim))
        return calls

    def test_both_bridge_slots_occupied_do_not_block_direct_lifecycle(self):
        self.occupied_bridge(2)
        self.direct()
        self.lifecycle()

    def test_bridge_completes_while_direct_is_indeterminate(self):
        from codex_pro_dispatch import resident
        calls = self.occupied_bridge()
        self.direct()
        self.prepare("direct-lost")
        self.cli("arm", "direct-lost")
        core.mark_indeterminate("direct-lost", reason="Lost native output", paths=self.paths)
        caller, claim = calls[0]
        token = resident.invocation.set(caller)
        try:
            core.mark_submitted("bridge-0", claim["wrapped_prompt"], self.paths)
            core.mark_pending("bridge-0", self.paths)
            core.complete_assignment("bridge-0", core.result_marker("bridge-0") + "\nAnswer\n" + core.end_marker("bridge-0"), self.paths)
        finally:
            resident.invocation.reset(token)
        current = self.cli("status", "--current")
        self.assertEqual(current["active_assignments"], [])
        self.assertEqual(self.cli("status", "--route", "direct")["active_assignment"]["assignment_id"], "direct-lost")

    def test_original_403_survives_owner_corruption_without_renewal(self):
        from codex_pro_dispatch import resident
        calls = self.occupied_bridge()
        self.direct()
        (self.paths.state_dir / "resident-owner.json").write_text("{")
        caller, _ = calls[0]
        token = resident.invocation.set(caller)
        try:
            first = core.mark_unusual_activity_403("bridge-0", reason="HTTP 403 unusual activity", paths=self.paths)
            repeated = core.mark_unusual_activity_403("bridge-0", reason="same", request_id="openai-request", paths=self.paths)
            self.assertEqual(first["cooldown_until"], repeated["cooldown_until"])
            with self.assertRaises(core.DispatchError):
                core.mark_pending("bridge-0", self.paths)
        finally:
            resident.invocation.reset(token)
        for key, value in [("generation", 77), ("owner", "other"), ("parent", "other"),
                           ("invocation", "other"), ("request", "other"), ("worker", "other"), ("slot", "other")]:
            token = resident.invocation.set(dict(caller, **{key: value}))
            try:
                with self.subTest(key=key), self.assertRaises(core.DispatchError):
                    core.mark_unusual_activity_403("bridge-0", reason="forged identity", paths=self.paths)
            finally:
                resident.invocation.reset(token)
        error = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "new",
                         "--native-controls-confirmed", text="Question", success=False)
        self.assertEqual(error["error_type"], "CooldownError")

    def test_hook_unrelated_task_does_not_read_broken_bridge(self):
        self.bridge()
        self.direct()
        (self.paths.state_dir / "resident-owner.json").write_text("{")
        env = dict(os.environ, CODEX_PRO_DISPATCH_HOME=str(self.paths.config_dir.parent))
        def stop(parent):
            p = subprocess.run(["node", str(ROOT / "skills/codex-pro-dispatch/scripts/resident-supervision.mjs"), "stop"],
                               input=json.dumps({"hook_event_name": "Stop", "session_id": parent, "turn_id": "turn"}),
                               text=True, capture_output=True, env=env)
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertEqual(stop("direct-parent"), {})
        self.assertEqual(stop("listener-task")["decision"], "block")
        (self.paths.config_dir / "dispatch-routes.json").write_text("{")
        self.assertIn("registry integrity", stop("direct-parent")["reason"])

    def test_continuation_uses_retained_original_binding_after_reconfiguration(self):
        self.direct()
        self.lifecycle()
        self.cli("worker", "direct-set", "--conversation-id", "new-direct", "--expected-conversation-id", "direct-worker",
                 "--confirm-worker", "--native-controls-confirmed")
        self.cli("worker", "direct-reset")
        result = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "continuation",
                          "--continuation-of", "direct-request", "--native-controls-confirmed", text="Continue")
        self.assertEqual(result["worker_conversation_id"], "direct-worker")
        self.cli("arm", "continuation")

    def test_partial_new_bridge_provenance_blocks_direct_admission(self):
        self.bridge()
        self.direct()
        self.paths.assignments_dir.mkdir(mode=0o700, exist_ok=True)
        path = self.paths.assignments_dir / "forged.json"
        path.write_text(json.dumps({"schema_version": 1, "assignment_id": "forged", "status": "armed",
                                   "worker_conversation_id": "worker-a", "parent_task_id": "parent",
                                   "execution_route": "bridge", "safety_observer": "garbage"}))
        path.chmod(0o600)
        self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "new",
                 "--native-controls-confirmed", text="Question", success=False)

    def test_registration_fault_boundaries_preserve_disjointness(self):
        from unittest.mock import patch
        from codex_pro_dispatch import routes
        for destination in ("resident-owner.json", "worker-pool.json"):
            for boundary in ("dispatch-routes.json", destination):
                for after in (False, True):
                    with self.subTest(destination=destination, boundary=boundary, after=after):
                        fixture = fixtures.ThreeFeatureTests(methodName="runTest")
                        fixture.setUp()
                        self.addCleanup(fixture.tearDown)
                        paths = fixture.paths
                        direct = routes.set_direct(paths, "direct-worker", confirm_worker=True)
                        packet = listener.plan(paths, ["worker-a", "worker-b"], "Isolated fault fixture")
                        original = core.atomic_write_json
                        def fault(path, payload, **kwargs):
                            if path.name == boundary and not after:
                                raise OSError("injected pre-write failure")
                            original(path, payload, **kwargs)
                            if path.name == boundary and after:
                                raise OSError("injected lost acknowledgment")
                        with patch.object(core, "atomic_write_json", side_effect=fault), self.assertRaises(OSError):
                            if destination == "worker-pool.json":
                                fixture.activate()
                            else:
                                listener.commit(paths, packet, "listener", "turn")
                        with core.state_lock(paths) as locked:
                            registry = routes.read(paths, locked)
                        self.assertEqual(registry["direct_worker"], direct)
                        reserved = boundary != "dispatch-routes.json" or after
                        self.assertEqual("worker-b" in registry["bridge_workers"], reserved)
                        authority = paths.worker_pool_file if destination == "worker-pool.json" else paths.state_dir / destination
                        self.assertEqual(authority.exists(), boundary == destination and after)
                        if reserved:
                            with self.assertRaises(core.DispatchError):
                                routes.set_direct(paths, "worker-b", confirm_worker=True, expected_conversation_id="direct-worker")

    def test_concurrent_direct_and_bridge_registration_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        from codex_pro_dispatch import routes
        packet = listener.plan(self.paths, ["contended"], "Disposable fixture")
        def direct():
            return routes.set_direct(self.paths, "contended", confirm_worker=True)
        def bridge():
            return listener.commit(self.paths, packet, "listener", "turn")
        def outcome(call):
            try:
                call()
                return True
            except core.DispatchError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(outcome, [direct, bridge])), [False, True])

    def test_bootstrap_retains_terminal_historical_bridge_destination(self):
        from codex_pro_dispatch import routes
        # Old standalone broker, before any registry, retains terminal association.
        from codex_pro_dispatch.queue import Queue
        queue = Queue(self.paths)
        queue.submit("old-bridge", b"Question", "client")
        claim = queue.claim("parent", True, "old-bridge")
        core.arm_assignment("old-bridge", self.paths)
        core.mark_submitted("old-bridge", claim["wrapped_prompt"], self.paths)
        core.complete_assignment("old-bridge", core.result_marker("old-bridge") + "\nAnswer\n" + core.end_marker("old-bridge"), self.paths)
        # Preserve an old no-route receipt and an acknowledged queue witness.
        with core.state_lock(self.paths) as locked:
            receipt = core.load_assignment("old-bridge", self.paths, _locked=locked)
            receipt.pop("execution_route", None)
            core.atomic_write_json(core.assignment_path("old-bridge", self.paths), receipt, _locked=locked)
            row = queue.load("old-bridge", _locked=locked)
            row["state"] = "acknowledged"
            core.atomic_write_json(queue.path("old-bridge"), row, _locked=locked)
        core.save_worker("current-scalar", confirm_worker=True, expected_conversation_id="worker-a", paths=self.paths)
        with self.assertRaises(core.DispatchError):
            routes.set_direct(self.paths, "worker-a", confirm_worker=True)
        self.assertFalse(routes.path(self.paths).exists())

    def test_legacy_standalone_is_collect_only_after_registration(self):
        prepared = core.prepare_assignment("Question", parent_task_id="parent", assignment_id="legacy", paths=self.paths)
        core.arm_assignment("legacy", self.paths)
        self.direct()
        core.mark_submitted("legacy", prepared.wrapped_prompt, self.paths)
        core.mark_pending("legacy", self.paths)
        core.complete_assignment("legacy", core.result_marker("legacy") + "\nAnswer\n" + core.end_marker("legacy"), self.paths)
        with self.assertRaisesRegex(core.StateError, "legacy_read_only"):
            core.prepare_assignment("Continue", parent_task_id="parent", continuation_of="legacy", paths=self.paths)

    def test_route_and_binding_cannot_change_during_save(self):
        from codex_pro_dispatch import resident
        self.direct()
        self.prepare("direct")
        with core.state_lock(self.paths) as locked:
            receipt = core.load_assignment("direct", self.paths, _locked=locked)
            for changes in ({"execution_route": "bridge"}, {"direct_binding_id": "forged"},
                            {"parent_task_id": "other"}, {"worker_conversation_id": "other"}):
                with self.subTest(changes=changes), self.assertRaises(core.StateError):
                    core._save_assignment("direct", dict(receipt, **changes), self.paths, _locked=locked)
        token = resident.invocation.set({"parent": "direct-parent"})
        try:
            with self.assertRaises(core.StateError):
                core.arm_assignment("direct", self.paths)
        finally:
            resident.invocation.reset(token)

    def test_malformed_cooldown_blocks_both_admissions(self):
        from codex_pro_dispatch import resident
        from codex_pro_dispatch.queue import Queue
        calls = self.occupied_bridge()
        self.direct()
        with core.state_lock(self.paths) as locked:
            receipt = core.load_assignment("bridge-0", self.paths, _locked=locked)
            receipt["cooldown_until"] = ""
            core.atomic_write_json(core.assignment_path("bridge-0", self.paths), receipt, _locked=locked)
        self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "new-direct",
                 "--native-controls-confirmed", text="Question", success=False)
        queue = Queue(self.paths)
        queue.submit("new-bridge", b"Question", "client")
        caller = dict(calls[0][0], request="new-bridge", invocation="new-invocation")
        token = resident.invocation.set(caller)
        try:
            with self.assertRaises(core.StateError):
                queue.claim("parent", True, "new-bridge")
        finally:
            resident.invocation.reset(token)
