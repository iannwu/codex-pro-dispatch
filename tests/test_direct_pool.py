"""Default 2x2 admission, migration and recovery through disposable public CLIs."""
import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_direct_routes as direct_fixtures
from codex_pro_dispatch import core, routes, resident

ROOT = direct_fixtures.ROOT


class DirectPoolTests(unittest.TestCase):
    setUp = direct_fixtures.DirectRoutesTests.setUp
    tearDown = direct_fixtures.DirectRoutesTests.tearDown
    cli = direct_fixtures.DirectRoutesTests.cli
    prepare = direct_fixtures.DirectRoutesTests.prepare
    bridge = direct_fixtures.DirectRoutesTests.bridge
    occupied_bridge = direct_fixtures.DirectRoutesTests.occupied_bridge
    activate = direct_fixtures.DirectRoutesTests.activate
    _enrolled_owner = direct_fixtures.DirectRoutesTests._enrolled_owner

    def pair(self, ids=("direct-01", "direct-02"), expected=(), labels=None, success=True):
        args = ["worker", "direct-pool-set", "--confirm-worker", "--native-controls-confirmed"]
        for value in ids:
            args.extend(["--conversation-id", value])
        for value in expected:
            args.extend(["--expected-conversation-id", value])
        if labels is not None:
            for value in labels:
                args.extend(["--label", value])
        return self.cli(*args, success=success)

    def status(self):
        return self.cli("status", "--route", "direct")

    def registry(self):
        return routes.path(self.paths).read_bytes()

    def finish(self, prepared):
        rid, worker = prepared["assignment_id"], prepared["worker_conversation_id"]
        self.cli("submitted", rid, "--sent-prompt-file", "-", text=prepared["wrapped_prompt"])
        self.cli("pending", rid)
        result = core.result_marker(rid) + "\nAnswer for " + rid + "\n" + core.end_marker(rid)
        data = {"schemaVersion": 1, "thread": {"id": worker, "kind": "chatgpt", "status": {"type": "idle"}},
                "turns": [{"id": rid + "-user", "status": "completed", "items": [
                    {"id": rid + "-user", "type": "userMessage", "content": [{"type": "text", "text": prepared["wrapped_prompt"]}]},
                    {"id": rid + "-assistant", "type": "agentMessage", "text": result}]}]}
        native_file = self.paths.config_dir.parent / (rid + "-native.json")
        native_file.write_text(json.dumps(data))
        native_file.chmod(0o600)
        value = self.cli("complete", rid, "--native-read-file", str(native_file))
        self.assertEqual(value["payload"], "Answer for " + rid)
        self.assertEqual(value["verification_level"], "bounded_native_summary")
        return core.assignment_path(rid, self.paths).read_bytes()

    def continuation(self, rid, prior, success=True, parent="direct-parent"):
        return self.cli("prepare", "--parent-task-id", parent, "--assignment-id", rid,
                        "--continuation-of", prior, "--native-controls-confirmed", text="Continue", success=success)

    def write_registry(self, value):
        with core.state_lock(self.paths) as locked:
            core.atomic_write_json(routes.path(self.paths), value, _locked=locked)

    def legacy_registry(self):
        value = json.loads(self.registry())
        workers = value.pop("direct_workers")
        value.update(version=1, direct_worker=workers[0] if workers else None)
        self.write_registry(value)
        return self.registry()

    def test_atomic_pair_defaults_and_public_invalid_requests(self):
        self.bridge()
        value = self.pair()
        self.assertIsNone(value["worker"])
        self.assertEqual(value["worker_count"], 2)
        self.assertEqual([w["label"] for w in value["workers"]], list(routes.DIRECT_LABELS))
        before = self.registry()
        for ids, expected, labels in [(("same", "same"), ("direct-01", "direct-02"), None),
                                      (("one",), ("direct-01", "direct-02"), None),
                                      (("one", "two", "three"), ("direct-01", "direct-02"), None),
                                      (("one", "two"), ("direct-02", "direct-01"), None),
                                      (("one", "two"), (), None),
                                      (("one", "two"), ("direct-01", "direct-02"), ("one",)),
                                      (("one", "worker-a"), ("direct-01", "direct-02"), None)]:
            with self.subTest(ids=ids, expected=expected, labels=labels):
                self.pair(ids, expected, labels, success=False)
                self.assertEqual(self.registry(), before)
        error = self.cli("worker", "direct-set", "--conversation-id", "one", "--confirm-worker",
                         "--native-controls-confirmed", success=False)
        self.assertIn("direct_pool_configured", error["error"])
        self.assertEqual(self.registry(), before)
        self.cli("worker", "direct-reset")
        for name in ("direct-01", "direct-02"):
            with self.assertRaises(core.DispatchError):
                from codex_pro_dispatch import listener
                listener.commit(self.paths, listener.plan(self.paths, [name], "Disposable fixture"), "replacement", "turn")
        # Retired destinations remain available within their original route.
        self.pair()
        from codex_pro_dispatch import listener
        listener.commit(self.paths, listener.plan(self.paths, ["fresh-bridge"], "Disposable fixture"), "replacement", "turn")
        before = self.registry()
        self.pair(("worker-b", "free"), ("direct-01", "direct-02"), success=False)
        self.assertEqual(self.registry(), before)

    def test_preflight_and_confirmation_fail_before_registry(self):
        for flags in (("--confirm-worker",), ("--native-controls-confirmed",)):
            self.cli("worker", "direct-pool-set", "--conversation-id", "direct-01", "--conversation-id", "direct-02",
                     *flags, success=False)
            self.assertFalse(routes.path(self.paths).exists())

    def test_idempotence_with_busy_work_and_configuration_changes_refuse(self):
        initial = self.pair()
        self.prepare("busy")
        before = self.registry()
        receipt = core.assignment_path("busy", self.paths).read_bytes()
        self.assertEqual(self.pair(expected=("direct-01", "direct-02"))["workers"], initial["workers"])
        self.assertEqual(self.registry(), before)
        self.pair(("replacement-01", "replacement-02"), ("direct-01", "direct-02"), success=False)
        self.pair(expected=("direct-01", "direct-02"), labels=("Changed", "Changed"), success=False)
        self.cli("worker", "direct-reset", success=False)
        self.assertEqual(self.registry(), before)
        self.assertEqual(core.assignment_path("busy", self.paths).read_bytes(), receipt)

    def test_version_one_empty_reads_do_not_migrate_and_mutation_does(self):
        self.pair()
        self.cli("worker", "direct-reset")
        before = self.legacy_registry()
        for args in (("worker", "direct-show"), ("status", "--route", "direct")):
            value = self.cli(*args)
            self.assertFalse(value["ok"])
            self.assertEqual(value["workers"], [])
            self.assertEqual(self.registry(), before)
        env = dict(os.environ, CODEX_PRO_DISPATCH_HOME=str(self.paths.config_dir.parent))
        doctor = subprocess.run([sys.executable, str(ROOT / "bin/pro-dispatch"), "doctor", "--route", "direct",
                                 "--native-controls-confirmed"], text=True, capture_output=True, env=env)
        self.assertNotEqual(doctor.returncode, 0)
        self.assertFalse(json.loads(doctor.stdout)["ok"])
        self.assertEqual(self.registry(), before)
        self.pair()
        self.assertEqual(json.loads(self.registry())["version"], 2)

    def test_version_one_singleton_preserves_active_and_terminal_receipts(self):
        self.cli("worker", "direct-set", "--conversation-id", "old", "--confirm-worker", "--native-controls-confirmed")
        complete = self.prepare("complete-old")
        self.cli("arm", "complete-old")
        terminal = self.finish(complete)
        self.prepare("active-old")
        before = self.legacy_registry()
        active = core.assignment_path("active-old", self.paths).read_bytes()
        for args in (("worker", "direct-show"), ("status", "--route", "direct"),
                     ("doctor", "--route", "direct", "--native-controls-confirmed")):
            self.assertEqual(self.cli(*args)["worker_count"], 1)
            self.assertEqual(self.registry(), before)
        self.pair(expected=("old",), success=False)
        self.assertEqual(self.registry(), before)
        self.assertEqual(core.assignment_path("active-old", self.paths).read_bytes(), active)
        self.cli("abandon", "active-old", "--reason", "Disposable prepared fixture")
        self.pair(expected=("old",))
        self.assertEqual(json.loads(self.registry())["version"], 2)
        self.assertEqual(core.assignment_path("complete-old", self.paths).read_bytes(), terminal)
        self.assertEqual(self.continuation("retained", "complete-old")["worker_conversation_id"], "old")

    def test_failed_atomic_write_preserves_complete_previous_registry(self):
        self.pair()
        before = self.registry()
        with patch("codex_pro_dispatch.native_storage.os.replace", side_effect=OSError("injected before atomic replacement")):
            with self.assertRaises(OSError):
                routes.set_direct_pool(self.paths, ["new-01", "new-02"], confirm_worker=True,
                                       expected_conversation_ids=["direct-01", "direct-02"])
        self.assertEqual(self.registry(), before)

    def test_three_concurrent_processes_reserve_two_distinct_workers(self):
        import threading
        self.pair()
        barrier = threading.Barrier(3)
        env = dict(os.environ, CODEX_PRO_DISPATCH_HOME=str(self.paths.config_dir.parent))
        def attempt(rid):
            barrier.wait()
            return subprocess.run([sys.executable, str(ROOT / "bin/pro-dispatch"), "prepare", "--parent-task-id", "direct-parent",
                                   "--assignment-id", rid, "--native-controls-confirmed"],
                                  input="Question " + rid, text=True, capture_output=True, env=env)
        with ThreadPoolExecutor(3) as executor:
            results = list(executor.map(attempt, ["race-a", "race-b", "race-c"]))
        winners = [json.loads(p.stdout) for p in results if p.returncode == 0]
        self.assertEqual(len(winners), 2)
        self.assertEqual({w["worker_conversation_id"] for w in winners}, {"direct-01", "direct-02"})
        error = json.loads(next(p.stderr for p in results if p.returncode != 0))
        self.assertIn("direct_pool_busy", error["error"])
        self.assertEqual({a["assignment_id"] for a in error["details"]["active_assignments"]}, {w["assignment_id"] for w in winners})
        self.assertEqual(len(list(self.paths.assignments_dir.glob("*.json"))), 2)
        for winner in winners:
            self.cli("arm", winner["assignment_id"])
        other = core.assignment_path(winners[1]["assignment_id"], self.paths).read_bytes()
        self.finish(winners[0])
        self.assertEqual(core.assignment_path(winners[1]["assignment_id"], self.paths).read_bytes(), other)
        self.assertEqual(self.status()["remaining_capacity"], 1)
        self.finish(winners[1])
        self.assertEqual(self.status()["remaining_capacity"], 2)

    def test_restart_prepared_armed_and_uncertain_occupancy_is_exact(self):
        self.pair()
        first = self.prepare("first")
        self.assertEqual(first["worker_conversation_id"], "direct-01")
        self.assertEqual([w["conversation_id"] for w in self.status()["available_workers"]], ["direct-02"])
        self.cli("arm", "first")
        self.cli("indeterminate", "first", "--reason", "Lost native output")
        second = self.prepare("second")
        self.assertEqual(second["worker_conversation_id"], "direct-02")
        self.cli("arm", "first", success=False)
        recovered = self.cli("recover", "first")["recovery"]
        self.assertEqual(recovered["worker_conversation_id"], "direct-01")
        self.assertTrue(recovered["no_resend"])
        error = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "third",
                         "--native-controls-confirmed", text="Question", success=False)
        self.assertIn("direct_pool_busy", error["error"])
        self.assertFalse(core.assignment_path("third", self.paths).exists())
        self.finish(first)  # Late readback settles collect-only work.
        self.assertEqual(self.prepare("replacement")["worker_conversation_id"], "direct-01")

    def test_continuation_keeps_original_worker_and_checks_parent(self):
        self.pair()
        first = self.prepare("root")
        self.cli("arm", "root")
        self.finish(first)
        self.prepare("occupant")
        error = self.continuation("blocked", "root", success=False)
        self.assertIn("direct_worker_busy", error["error"])
        self.assertEqual(self.status()["remaining_capacity"], 1)
        self.continuation("foreign", "root", parent="other-parent", success=False)
        self.assertFalse(core.assignment_path("foreign", self.paths).exists())

    def test_retired_binding_consumes_global_capacity_and_duplicate_chat_is_integrity(self):
        self.pair()
        first = self.prepare("root")
        self.cli("arm", "root")
        self.finish(first)
        self.pair(("new-01", "new-02"), ("direct-01", "direct-02"))
        self.continuation("retired", "root")
        self.prepare("current")
        status = self.status()
        self.assertEqual(status["availability"], "busy")
        self.assertEqual(len(status["available_workers"]), 1)
        self.assertEqual(status["remaining_capacity"], 0)
        error = self.continuation("limit", "root", success=False)
        self.assertIn("direct_worker_busy", error["error"])
        # Two binding records for one conversation cannot create capacity.
        value = json.loads(self.registry())
        duplicate = dict(value["direct_bindings"][0], binding_id="new-binding-same-chat")
        value["direct_bindings"].append(duplicate)
        self.write_registry(value)
        with core.state_lock(self.paths) as locked:
            receipt = core.load_assignment("current", self.paths, _locked=locked)
            receipt.update(direct_binding_id=duplicate["binding_id"], worker_conversation_id="direct-01")
            core.atomic_write_json(core.assignment_path("current", self.paths), receipt, _locked=locked)
        error = self.cli("status", "--route", "direct", success=False)
        self.assertIn("share one conversation", error["error"])

    def test_global_limit_blocks_retired_continuation_even_with_original_chat_free(self):
        self.pair()
        first = self.prepare("root")
        self.cli("arm", "root")
        self.finish(first)
        self.pair(("new-01", "new-02"), ("direct-01", "direct-02"))
        self.prepare("current-01")
        self.prepare("current-02")
        error = self.continuation("limit", "root", success=False)
        self.assertIn("direct_pool_busy", error["error"])

    def test_two_direct_and_two_bridge_jobs_coexist_and_broken_bridge_does_not_block(self):
        calls = self.occupied_bridge(2)
        self.pair()
        first, second = self.prepare("direct-a"), self.prepare("direct-b")
        self.assertEqual(len(self.status()["active_assignments"]), 2)
        self.assertEqual(len(core.active_assignments(self.paths, execution_route="bridge")), 2)
        caller, claim = calls[0]
        token = resident.invocation.set(caller)
        try:
            core.mark_submitted("bridge-0", claim["wrapped_prompt"], self.paths)
            core.mark_pending("bridge-0", self.paths)
            core.complete_assignment("bridge-0", core.result_marker("bridge-0") + "\nBridge answer\n" + core.end_marker("bridge-0"), self.paths)
        finally:
            resident.invocation.reset(token)
        self.assertEqual(len(self.status()["active_assignments"]), 2)
        for path in [self.paths.state_dir / "resident-owner.json", self.paths.worker_pool_file]:
            path.write_text("{")
            path.chmod(0o600)
        for prepared in (first, second):
            self.cli("arm", prepared["assignment_id"])
            self.finish(prepared)
        env = dict(os.environ, CODEX_PRO_DISPATCH_HOME=str(self.paths.config_dir.parent))
        stopped = subprocess.run(["node", str(ROOT / "skills/codex-pro-dispatch/scripts/resident-supervision.mjs"), "stop"],
                                 input=json.dumps({"hook_event_name": "Stop", "session_id": "direct-parent", "turn_id": "turn"}),
                                 text=True, capture_output=True, env=env)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout), {})

    def cooldown(self):
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        with core.state_lock(self.paths) as locked:
            core.atomic_write_json(core.assignment_path("cooldown", self.paths),
                                   dict(schema_version=1, assignment_id="cooldown", status="complete", created_at=core.utc_now(),
                                        cooldown_until=until, native_http_status=403), _locked=locked)

    def test_cooldown_precedes_busy_and_status_reports_both(self):
        self.pair()
        self.prepare("one")
        self.prepare("two")
        self.cooldown()
        status = self.status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["local_ok"])
        self.assertEqual(status["availability"], "busy")
        self.assertIsNotNone(status["active_cooldown"])
        error = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "blocked",
                         "--native-controls-confirmed", text="Question", success=False)
        self.assertEqual(error["error_type"], "CooldownError")
        self.assertEqual(error["details"]["remaining_capacity"], 0)
        self.assertEqual(len(error["details"]["active_assignments"]), 2)
        self.assertFalse(core.assignment_path("blocked", self.paths).exists())
        # Canonical corruption takes precedence over a busy pool and cooldown.
        (self.paths.assignments_dir / "broken.json").write_text("{")
        (self.paths.assignments_dir / "broken.json").chmod(0o600)
        error = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "broken-blocked",
                         "--native-controls-confirmed", text="Question", success=False)
        self.assertEqual(error["error_type"], "StateError")
        self.assertNotIn("direct_pool_busy", error["error"])

    def test_missing_configuration_precedes_valid_cooldown(self):
        self.pair()
        self.cli("worker", "direct-reset")
        self.cooldown()
        error = self.cli("prepare", "--parent-task-id", "direct-parent", "--assignment-id", "blocked",
                         "--native-controls-confirmed", text="Question", success=False)
        self.assertIn("direct_worker_not_configured", error["error"])

    def test_public_status_zero_one_two_and_scalar_compatibility(self):
        for count in range(3):
            if count == 1:
                self.cli("worker", "direct-set", "--conversation-id", "single", "--confirm-worker", "--native-controls-confirmed")
            elif count == 2:
                self.pair(expected=("single",))
            for args in (("worker", "direct-show"), ("status", "--route", "direct")):
                status = self.cli(*args)
                self.assertEqual(status["worker_count"], count)
                self.assertEqual(len(status["workers"]), count)
                self.assertEqual(status["ok"], count > 0)
                self.assertEqual(status["local_ok"], count > 0)
                self.assertEqual(status["remaining_capacity"], count)
                self.assertEqual(len(status["available_workers"]), count)
                self.assertEqual(status["availability"], "ready" if count else "not_configured")
                self.assertEqual(status["active_assignments"], [])
                self.assertIsNone(status["active_assignment"])
                self.assertIsNone(status["active_cooldown"])
                self.assertEqual(status["worker"] is None, count != 1)
        self.prepare("one")
        self.assertIsNotNone(self.status()["active_assignment"])
        self.prepare("two")
        self.assertIsNone(self.status()["active_assignment"])
        self.assertTrue(self.cli("doctor", "--route", "direct", "--native-controls-confirmed")["ok"])

    def test_registry_invalid_current_count_retention_and_duplicates(self):
        self.pair()
        original = json.loads(self.registry())
        for mutate in (lambda v: v["direct_workers"].append(v["direct_workers"][0]),
                       lambda v: v["direct_workers"].__setitem__(1, v["direct_workers"][0]),
                       lambda v: v["direct_bindings"].pop(),
                       lambda v: v.update(direct_worker=None)):
            value = copy.deepcopy(original)
            mutate(value)
            self.write_registry(value)
            error = self.cli("status", "--route", "direct", success=False)
            self.assertIn("dispatch_routes_integrity", error["error"])


if __name__ == "__main__":
    unittest.main()
