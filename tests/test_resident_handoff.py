"""Graceful ownership rotation against isolated real helper state."""
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path

import test_three_feature as fixtures
from codex_pro_dispatch import core, resident
from codex_pro_dispatch.queue import Queue


class HandoffTests(unittest.TestCase):
    setUp = fixtures.ThreeFeatureTests.setUp
    tearDown = fixtures.ThreeFeatureTests.tearDown
    activate = fixtures.ThreeFeatureTests.activate
    _enrolled_owner = fixtures.ThreeFeatureTests._enrolled_owner
    _bind_session = fixtures.ThreeFeatureTests._bind_session
    _arm_crash_request = fixtures.ThreeFeatureTests._arm_crash_request
    target = "01a0ac4a-313b-7f33-9ca0-f76d994ef745"

    def test_cli_exposes_one_handoff_operation(self):
        from codex_pro_dispatch.cli import build_parser
        parsed = build_parser().parse_args(["resident", "handoff", '{"new_parent":"example"}'])
        self.assertEqual(parsed.operation, "handoff")
        self.assertEqual(parsed.credentials, {"new_parent": "example"})

    def test_copied_credentials_and_fake_attestation_cannot_use_cli(self):
        owner, credentials, directory = self.closed()
        before = self.snapshot()
        for extra in ({}, {"native_thread_id": "parent", "attested": True},
                      {"join_verified": True, "target_exists": True}):
            with self.assertRaisesRegex(core.StateError, "native handoff packet"):
                resident.control("handoff", dict(credentials, **extra), self.paths)
        self.assertEqual(before, self.snapshot())

    def test_closure_without_join_barrier_refuses(self):
        owner, credentials, directory = self.closed()
        (directory / "resident-joined.json").unlink()
        with self.assertRaisesRegex(core.StateError, "completion barrier"):
            self.commit(credentials)

    def test_recover_start_keeps_parent_and_needs_no_join_barrier(self):
        owner, credentials, directory = self.closed()
        (directory / "resident-joined.json").unlink()
        result = resident.control("recover-start", dict(credentials,
            evidence_file=str(self.evidence), evidence_sha256=self.evidence_hash,
            new_owner="recovered-owner"), self.paths)
        self.assertEqual(result["owner"]["parent"], owner["parent"])
        self.assertEqual(result["owner"]["generation"], owner["generation"] + 1)
        self.assertIsNone(result["owner"]["session"])

    def commit(self, credentials):
        # Unit-level commit tests; native entry and CLI rejection are tested separately.
        return resident._handoff_from_native(self.paths, credentials)

    def closed(self):
        owner, credentials = self._enrolled_owner()
        directory, binding = self._bind_session(owner)
        audit = directory / "transport-audit.json"
        audit.write_text(json.dumps({"sessionId": binding["session_id"],
                                     "reason": "resident_stopped", "events": []}))
        audit.chmod(0o600)
        joined = directory / "resident-joined.json"
        joined.write_text(json.dumps(dict(generation=owner["generation"], owner=owner["owner"],
                                         parent=owner["parent"], sessionId=binding["session_id"],
                                         invocation="joined-fixture")))
        joined.chmod(0o600)
        return owner, dict(credentials, new_parent=self.target), directory

    def snapshot(self):
        return {str(p): p.read_bytes() for root in (self.paths.config_dir, self.paths.state_dir)
                for p in root.rglob("*") if p.is_file() and p.name != "state.lock"}

    def test_handoff_preserves_evidence_and_fences_old_parent(self):
        owner, credentials, directory = self.closed()
        Queue(self.paths).submit("queued", b"preserve me", "client")
        before = self.snapshot()
        result = self.commit(credentials)
        after = self.snapshot()
        owner_path = str(self.paths.state_dir / "resident-owner.json")
        self.assertNotEqual(before.pop(owner_path), after.pop(owner_path))
        self.assertEqual(before, after)
        new = result["owner"]
        self.assertEqual(new["parent"], self.target)
        self.assertEqual(new["generation"], owner["generation"] + 1)
        self.assertIsNone(new["session"])
        self.assertEqual(new["slots"], owner["slots"])
        self.assertNotEqual(new["owner"], owner["owner"])
        for action in ("handoff", "start", "check", "begin"):
            with self.subTest(action=action), self.assertRaises(core.StateError):
                resident.control(action, credentials, self.paths)
        started = resident.control("start", new, self.paths)["owner"]
        self.assertEqual(resident.control("check", started, self.paths)["owner"], started)
        fresh = directory.parent / "new-session"
        fresh.mkdir(mode=0o700)
        descriptor = dict(resident=True, sessionId="b" * 32, parent=self.target,
                          configDir=str(self.paths.config_dir), stateDir=str(self.paths.state_dir),
                          workerPoolSha256=started["worker_pool_sha256"])
        raw = json.dumps(descriptor).encode()
        (fresh / "session.json").write_bytes(raw)
        (fresh / "session.json").chmod(0o600)
        binding = dict(directory=str(fresh), session_id="b" * 32,
                       descriptor_sha256=hashlib.sha256(raw).hexdigest())
        bound = resident.control("bind-session", dict(started, session=binding), self.paths)
        self.assertEqual(bound["owner"]["session"], binding)
        begun = resident.control("begin", dict(started, request="queued", invocation="new-inv"), self.paths)
        self.assertEqual(begun["worker_slot"], "slot-a")
        audit = resident.control("inspect", {}, self.paths)["owner"]["qualification"]["handoffs"]
        self.assertEqual(audit[-1]["previous_parent"], "parent")

    def test_bad_credentials_refuse_without_writes(self):
        owner, credentials, directory = self.closed()
        for change in ({"generation": 0}, {"generation": True}, {"parent": self.target},
                       {"owner": "foreign"}, {"worker_pool_sha256": "0" * 64},
                       {"new_parent": "title"}, {"new_parent": "parent"}):
            with self.subTest(change=change):
                before = self.snapshot()
                with self.assertRaises(core.DispatchError):
                    self.commit(dict(credentials, **change))
                self.assertEqual(before, self.snapshot())

    def test_requires_graceful_closure_and_no_waiter_or_socket(self):
        owner, credentials, directory = self.closed()
        audit = directory / "transport-audit.json"
        original = audit.read_bytes()
        for reason in ("resident_failed", "idle_expired", "closed"):
            audit.write_text(json.dumps({"sessionId": "a" * 32, "reason": reason, "events": []}))
            with self.assertRaises(core.StateError):
                self.commit(credentials)
        audit.write_bytes(original)
        for name in ("wake.sock", "waiting-1." + "a" * 32):
            marker = directory / name
            marker.touch()
            with self.assertRaises(core.DispatchError):
                self.commit(credentials)
            marker.unlink()
        audit.unlink()
        with self.assertRaises(core.StateError):
            self.commit(credentials)

    def test_recovery_owner_refuses(self):
        owner, credentials, directory = self.closed()
        resident.control("collector-open", credentials, self.paths)
        with self.assertRaises(core.BusyError):
            self.commit(credentials)

    def test_concurrent_handoffs_have_one_winner(self):
        owner, credentials, directory = self.closed()
        def attempt(_):
            try:
                return self.commit(credentials)["reason"]
            except core.DispatchError:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(list(executor.map(attempt, range(2))),
                                  ["owner_handed_off", "refused"])
        current = resident.control("inspect", {}, self.paths)["owner"]
        self.assertEqual(current["generation"], owner["generation"] + 1)

    def test_active_slot_and_indeterminate_assignment_refuse(self):
        owner, credentials, directory = self.closed()
        active = self._arm_crash_request(owner, credentials)
        token = resident.invocation.set(active)
        try:
            core.mark_unusual_activity_403("request-crash", reason="403", paths=self.paths)
        finally:
            resident.invocation.reset(token)
        self.assertEqual(core.load_assignment("request-crash", self.paths)["status"], "indeterminate")
        before = self.snapshot()
        with self.assertRaises(core.BusyError):
            self.commit(credentials)
        self.assertEqual(before, self.snapshot())
        # Even inconsistent idle slot metadata must not hide an active receipt.
        with core.state_lock(self.paths) as locked:
            value = resident.read(self.paths, locked)
            value["slots"] = owner["slots"]
            resident.write(self.paths, locked, value)
        with self.assertRaises(core.BusyError):
            self.commit(credentials)

    def test_unbound_owner_refuses(self):
        owner, credentials = self._enrolled_owner()
        with self.assertRaises(core.StateError):
            self.commit(dict(credentials, new_parent=self.target))

    def test_cooldown_and_completed_receipt_survive_and_block_claim(self):
        owner, credentials, directory = self.closed()
        active = self._arm_crash_request(owner, credentials)
        token = resident.invocation.set(active)
        try:
            core.mark_unusual_activity_403("request-crash", reason="403", paths=self.paths)
        finally:
            resident.invocation.reset(token)
        # Seed a resolved historic receipt with a still-active account cooldown.
        # This fixture is entirely private; no recovery or state clearing is performed live.
        with core.state_lock(self.paths) as locked:
            receipt = core.load_assignment("request-crash", self.paths, _locked=locked)
            receipt["status"] = "complete"
            core.atomic_write_json(core.assignment_path("request-crash", self.paths),
                                   receipt, _locked=locked)
            queue = Queue(self.paths)
            row = queue.load("request-crash", _locked=locked)
            row["state"] = "acknowledged"
            queue.save(row, _locked=locked)
            value = resident.read(self.paths, locked)
            value["slots"] = owner["slots"]
            resident.write(self.paths, locked, value)
        cooldown = core.active_cooldown(self.paths)
        self.assertIsNotNone(cooldown)
        before = self.snapshot()
        new = self.commit(credentials)["owner"]
        after = self.snapshot()
        before.pop(str(self.paths.state_dir / "resident-owner.json"))
        after.pop(str(self.paths.state_dir / "resident-owner.json"))
        self.assertEqual(before, after)
        self.assertEqual(cooldown, core.active_cooldown(self.paths))
        started = resident.control("start", new, self.paths)["owner"]
        Queue(self.paths).submit("next", b"later", "client")
        token = resident.invocation.set(dict(started, invocation="next-inv", request="next"))
        try:
            with self.assertRaises(core.CooldownError):
                Queue(self.paths).claim(self.target, True, "next")
        finally:
            resident.invocation.reset(token)


if __name__ == "__main__":
    unittest.main()
