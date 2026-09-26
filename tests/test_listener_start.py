"""Real locked startup transactions in disposable authority, never production."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest

import test_three_feature as fixtures
from codex_pro_dispatch import core, listener, resident
from codex_pro_dispatch.queue import Queue


class ListenerStartTests(unittest.TestCase):
    setUp = fixtures.ThreeFeatureTests.setUp
    tearDown = fixtures.ThreeFeatureTests.tearDown
    activate = fixtures.ThreeFeatureTests.activate
    _enrolled_owner = fixtures.ThreeFeatureTests._enrolled_owner
    _bind_session = fixtures.ThreeFeatureTests._bind_session

    def snapshot(self):
        return {str(p): p.read_bytes() for root in (self.paths.config_dir, self.paths.state_dir)
                for p in root.rglob("*") if p.is_file() and p.name != "state.lock"}

    def fresh(self):
        p = listener.plan(self.paths, ["worker-a", "worker-b"], "Isolated fixture has no old executors")
        return listener.commit(self.paths, p, "parent", "turn")["owner"]

    def owner(self):
        return resident.control("inspect", {}, self.paths)["owner"]

    def test_fresh_without_external_pool_and_scalar_mutation_fenced(self):
        owner = self.fresh()
        self.assertEqual(owner["version"], 4)
        self.assertNotIn("worker_pool_json", owner)
        self.assertFalse(self.paths.worker_pool_file.exists())
        self.assertEqual([w.conversation_id for w in core.configured_workers(self.paths)], ["worker-a", "worker-b"])
        for action in (lambda: core.save_worker("other", confirm_worker=True, paths=self.paths),
                       lambda: core.reset_worker(force=True, paths=self.paths),
                       lambda: core.purge_local_state(force=True, paths=self.paths)):
            with self.assertRaises(core.StateError): action()

    def test_legacy_incident_is_blocked_unchanged_then_physical_migration(self):
        owner, _ = self._enrolled_owner()
        self._bind_session(owner)
        p = listener.plan(self.paths, ["new-01", "new-02"])
        before = self.snapshot()
        with self.assertRaisesRegex(core.StateError, "legacy_exclusion_unknown"):
            listener.commit(self.paths, p, "replacement", "turn")
        self.assertEqual(before, self.snapshot())
        p = listener.plan(self.paths, ["new-01", "new-02"], "Fixture old execution physically terminated")
        result = listener.commit(self.paths, p, "replacement", "turn")
        after = self.snapshot()
        key = str(self.paths.state_dir / "resident-owner.json")
        self.assertNotEqual(before.pop(key), after.pop(key))
        self.assertEqual(before, after)
        self.assertEqual(result["owner"]["generation"], owner["generation"]+1)
        self.assertEqual([w.conversation_id for w in core.load_worker_pool(self.paths).workers], ["new-01", "new-02"])

    def test_unused_bound_profile_needs_no_task_read_or_join(self):
        old = self.fresh()
        directory, _ = self._bind_session(old)
        (directory / "waiting-4.fixture").mkdir(mode=0o700)
        (directory / "wake.sock").touch(mode=0o600)
        p = listener.plan(self.paths, ["new-01", "new-02"])
        result = listener.commit(self.paths, p, "replacement", "turn")
        self.assertEqual(result["state"], "next_action")
        self.assertTrue((directory / "wake.sock").exists())
        with self.assertRaises(core.StateError): resident.control("check", old, self.paths)

    def test_concurrent_starts_single_cas_winner_and_retry(self):
        self.fresh()
        p = listener.plan(self.paths, ["new-01"])
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda who: listener.commit(self.paths, p, who, "turn"), ["p1", "p2"]))
        self.assertEqual(sorted(r["state"] for r in results), ["next_action", "stale"])
        winner = next(r for r in results if r["state"] == "next_action")
        before = self.snapshot()
        retry = listener.commit(self.paths, p, winner["owner"]["parent"], "turn")
        self.assertEqual(retry["reason"], "already_committed")
        self.assertEqual(before, self.snapshot())

    def test_lost_commit_ack_is_inspected_and_does_not_rotate_again(self):
        self.fresh()
        p = listener.plan(self.paths, ["new-01"])
        original = core.atomic_write_json
        def write_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("lost acknowledgment after durable write")
        with patch.object(core, "atomic_write_json", write_then_fail), self.assertRaises(OSError):
            listener.commit(self.paths, p, "replacement", "turn")
        generation = self.owner()["generation"]
        self.assertEqual(listener.commit(self.paths, p, "replacement", "turn")["reason"], "already_committed")
        self.assertEqual(self.owner()["generation"], generation)

    def test_precommit_failure_preserves_entire_old_authority(self):
        self.fresh()
        p = listener.plan(self.paths, ["new-01"])
        before = self.snapshot()
        with patch.object(core, "atomic_write_json", side_effect=OSError("prewrite")), self.assertRaises(OSError):
            listener.commit(self.paths, p, "replacement", "turn")
        self.assertEqual(before, self.snapshot())

    def test_current_owner_changes_make_even_physical_packet_stale(self):
        old = self.fresh()
        p = listener.plan(self.paths, ["new-01"], "Fixture quiescent")
        resident.control("start", dict(old, owner="intervening"), self.paths)
        before = self.snapshot()
        self.assertEqual(listener.commit(self.paths, p, "replacement", "turn")["state"], "stale")
        self.assertEqual(before, self.snapshot())

    def test_sticky_fence_precedes_receipt_and_blocks_low_level_rotation(self):
        owner = self.fresh()
        q = Queue(self.paths)
        q.submit("request", b"one send", "client")
        credentials = dict(owner, request="request", invocation="call")
        token = resident.invocation.set(credentials)
        try:
            q.claim("parent", True, "request")
            with patch.object(core, "_save_assignment", side_effect=OSError("receipt write interrupted")), self.assertRaises(OSError):
                core.arm_for_send("slot-a", "request", owner["generation"], "call", self.paths)
        finally:
            resident.invocation.reset(token)
        v = self.owner()
        self.assertTrue(v["send_fence"]["armed_since_barrier"])
        self.assertEqual(core.load_assignment("request", self.paths)["status"], "prepared")
        with core.state_lock(self.paths) as locked:
            changed = copy.deepcopy(v)
            changed["send_fence"]["armed_since_barrier"] = False
            with self.assertRaisesRegex(core.StateError, "cannot be cleared"):
                resident.write(self.paths, locked, changed)
            changed = dict(v, generation=v["generation"]+1, owner="other")
            with self.assertRaisesRegex(core.StateError, "join_required"):
                resident.write(self.paths, locked, changed)

    def test_missing_history_does_not_clear_sticky_fence(self):
        self.fresh()
        with core.state_lock(self.paths) as locked:
            v = resident.read(self.paths, locked)
            v["send_fence"]["armed_since_barrier"] = True
            resident.write(self.paths, locked, v)
        p = listener.plan(self.paths, ["worker-a"])
        with self.assertRaisesRegex(core.StateError, "join_required"):
            listener.commit(self.paths, p, "replacement", "turn")

    def test_claimed_work_blocks_even_physical_replacement_without_mutation(self):
        owner = self.fresh()
        q = Queue(self.paths)
        q.submit("request", b"preserve", "client")
        token = resident.invocation.set(dict(owner, request="request", invocation="call"))
        try: q.claim("parent", True, "request")
        finally: resident.invocation.reset(token)
        p = listener.plan(self.paths, ["new-01"], "Fixture physically terminated")
        before = self.snapshot()
        with self.assertRaises(core.BusyError): listener.commit(self.paths, p, "replacement", "turn")
        self.assertEqual(before, self.snapshot())

    def test_queued_bytes_and_fifo_survive_replacement(self):
        self.fresh()
        q = Queue(self.paths)
        q.submit("first", b"one", "client")
        q.submit("second", b"two", "client")
        before = self.snapshot()
        p = listener.plan(self.paths, ["new-01"])
        listener.commit(self.paths, p, "replacement", "turn")
        after = self.snapshot()
        for name in before:
            if not name.endswith("resident-owner.json"): self.assertEqual(before[name], after[name])

    def test_invalid_schema_four_never_falls_back_to_external_pool(self):
        self._enrolled_owner()
        p = listener.plan(self.paths, ["new-01"], "Fixture physically terminated")
        listener.commit(self.paths, p, "replacement", "turn")
        path = self.paths.state_dir / "resident-owner.json"
        original = path.read_bytes()
        for change in ({"send_fence": None}, {"worker_pool_json": "{}"}, {"version": 5},
                       {"worker_pool_sha256": "0"*64}):
            value = json.loads(original); value.update(change)
            with core.state_lock(self.paths) as locked: core.atomic_write_json(path, value, _locked=locked)
            with self.assertRaises(core.DispatchError): core.load_worker_pool(self.paths)
        path.write_bytes(original)

    def test_no_recursive_loaders(self):
        self.fresh()
        with patch.object(resident, "read", side_effect=AssertionError("recursive resident read")):
            self.assertEqual(len(core.load_worker_pool(self.paths).workers), 2)

    def test_exact_join_resets_fence_only_as_part_of_rotation(self):
        old = self.fresh()
        directory, binding = self._bind_session(old)
        with core.state_lock(self.paths) as locked:
            v = resident.read(self.paths, locked)
            v["send_fence"]["armed_since_barrier"] = True
            resident.write(self.paths, locked, v)
        audit = directory / "transport-audit.json"
        audit.write_text(json.dumps({"sessionId": binding["session_id"], "reason": "resident_stopped", "events": []}))
        audit.chmod(0o600)
        joined = dict(generation=old["generation"], owner=old["owner"], parent=old["parent"],
                      sessionId=binding["session_id"], invocation="original-call")
        path = directory / "resident-joined.json"
        path.write_text(json.dumps(dict(joined, generation=old["generation"]-1)))
        path.chmod(0o600)
        p = listener.plan(self.paths, ["worker-a"])
        before = self.snapshot()
        with self.assertRaisesRegex(core.StateError, "join_required"):
            listener.commit(self.paths, p, "replacement", "turn")
        self.assertEqual(before, self.snapshot())
        path.write_text(json.dumps(joined))
        result = listener.commit(self.paths, p, "replacement", "turn")
        self.assertEqual(result["owner"]["generation"], old["generation"]+1)
        self.assertFalse(self.owner()["send_fence"]["armed_since_barrier"])
        self.assertEqual(json.loads(path.read_text()), joined)
        listener.commit(self.paths, listener.plan(self.paths, ["new-01"]), "third", "turn")
        listener.commit(self.paths, listener.plan(self.paths, ["worker-a", "worker-b"]), "fourth", "turn")
        self.assertEqual(self.owner()["send_fence"]["unexcluded_workers"], [])

    def test_old_startup_operation_is_not_reusable_after_another_rotation(self):
        self.fresh()
        p = listener.plan(self.paths, ["new-01"])
        acquired = listener.commit(self.paths, p, "replacement", "turn")["owner"]
        resident.control("start", dict(acquired, owner="next-owner"), self.paths)
        before = self.snapshot()
        self.assertEqual(listener.commit(self.paths, p, "replacement", "turn")["state"], "stale")
        self.assertEqual(before, self.snapshot())

    def test_disjoint_replacement_carries_exposure_and_blocks_later_reuse(self):
        old = self.fresh()
        with core.state_lock(self.paths) as locked:
            v = resident.read(self.paths, locked)
            v["send_fence"]["armed_since_barrier"] = True
            resident.write(self.paths, locked, v)
        listener.commit(self.paths, listener.plan(self.paths, ["new-01"]), "p2", "t2")
        self.assertEqual(self.owner()["send_fence"], {
            "profile": 2, "armed_since_barrier": False,
            "unexcluded_workers": ["worker-a", "worker-b"]})
        listener.commit(self.paths, listener.plan(self.paths, ["new-02"]), "p3", "t3")
        before = self.snapshot()
        with self.assertRaisesRegex(core.StateError, "destination_exposed"):
            listener.commit(self.paths, listener.plan(self.paths, ["worker-a"]), "p4", "t4")
        self.assertEqual(before, self.snapshot())
        with core.state_lock(self.paths) as locked:
            v = resident.read(self.paths, locked)
            v["send_fence"]["unexcluded_workers"] = []
            with self.assertRaisesRegex(core.StateError, "cannot change"):
                resident.write(self.paths, locked, v)
        listener.commit(self.paths, listener.plan(self.paths, ["worker-a"], "Fixture all old executors terminated"), "p4", "t4")
        self.assertEqual(self.owner()["send_fence"]["unexcluded_workers"], [])
        with self.assertRaises(core.StateError): resident.control("check", old, self.paths)

    def test_armed_session_cannot_be_detached_within_generation(self):
        old = self.fresh()
        self._bind_session(old)
        with core.state_lock(self.paths) as locked:
            v = resident.read(self.paths, locked)
            v["send_fence"]["armed_since_barrier"] = True
            resident.write(self.paths, locked, v)
            v["session"] = None
            with self.assertRaisesRegex(core.StateError, "Armed session"):
                resident.write(self.paths, locked, v)

    def test_profile_one_needs_physical_migration_even_if_unused(self):
        self.fresh()
        with core.state_lock(self.paths) as locked:
            v = resident.read(self.paths, locked)
            v["send_fence"] = {"profile": 1, "armed_since_barrier": False}
            core.atomic_write_json(self.paths.state_dir / "resident-owner.json", v, _locked=locked)
        with self.assertRaisesRegex(core.StateError, "legacy_exclusion_unknown"):
            listener.commit(self.paths, listener.plan(self.paths, ["new"]), "p2", "t2")
        listener.commit(self.paths, listener.plan(self.paths, ["new"], "Fixture terminated"), "p2", "t2")
        self.assertEqual(self.owner()["send_fence"]["profile"], 2)

    def test_reports_request_and_exclusion_blockers_without_mutation(self):
        old = self.fresh()
        q = Queue(self.paths)
        q.submit("request", b"preserve", "client")
        token = resident.invocation.set(dict(old, request="request", invocation="call"))
        try:
            q.claim("parent", True, "request")
        finally:
            resident.invocation.reset(token)
        with core.state_lock(self.paths) as locked:
            value = resident.read(self.paths, locked)
            value["send_fence"]["armed_since_barrier"] = True
            resident.write(self.paths, locked, value)
        packet = listener.plan(self.paths, ["worker-a"])
        before = self.snapshot()
        with self.assertRaises(core.BusyError) as caught:
            listener.commit(self.paths, packet, "replacement", "turn")
        blockers = caught.exception.details["blockers"]
        self.assertIn("request_recovery_required", [b["reason"] for b in blockers])
        self.assertIn("destination_exposed", blockers[-1]["reason"])
        self.assertEqual(before, self.snapshot())

    def test_replaced_credentials_cannot_claim_submit_or_release(self):
        old = self.fresh()
        q = Queue(self.paths)
        q.submit("request", b"preserve", "client")
        listener.commit(self.paths, listener.plan(self.paths, ["new"]), "new-parent", "turn")
        before = self.snapshot()
        token = resident.invocation.set(dict(old, request="request", invocation="old-call"))
        try:
            for action in (lambda: q.claim("parent", True, "request"),
                           lambda: core.mark_submitted("request", "late prompt", self.paths),
                           lambda: q.release("request", "parent")):
                with self.assertRaises(core.DispatchError):
                    action()
                self.assertEqual(before, self.snapshot())
        finally:
            resident.invocation.reset(token)
