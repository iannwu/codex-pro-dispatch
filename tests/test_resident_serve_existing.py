"""Pristine serving claim changes session evidence only, never authority."""
import concurrent.futures
import copy
import socket
import unittest
from unittest.mock import patch

import test_three_feature as fixtures
from codex_pro_dispatch import core, resident
from codex_pro_dispatch.queue import Queue


class ServeExistingTests(unittest.TestCase):
    setUp = fixtures.ThreeFeatureTests.setUp
    tearDown = fixtures.ThreeFeatureTests.tearDown
    activate = fixtures.ThreeFeatureTests.activate
    _enrolled_owner = fixtures.ThreeFeatureTests._enrolled_owner
    _bind_session = fixtures.ThreeFeatureTests._bind_session
    _arm_crash_request = fixtures.ThreeFeatureTests._arm_crash_request

    def pristine(self):
        owner, base = self._enrolled_owner()
        directory, binding = self._bind_session(owner)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(directory / 'wake.sock'))
        listener.listen()
        self.addCleanup(listener.close)
        return owner, {**base, 'session': binding}, directory

    def claim(self, credentials):
        return resident.control('claim-serve-existing', credentials, self.paths)

    def snapshot(self):
        return {str(p): p.read_bytes() for root in [self.paths.state_dir, self.paths.config_dir]
                for p in root.rglob('*') if p.is_file()}

    def replace(self, credentials):
        return resident.control('replace-unused-serving', credentials, self.paths)

    def test_unused_replacement_preserves_consume_and_owner_and_is_exclusive(self):
        _, c, directory = self.pristine()
        self.claim(c)
        marker = (directory / 'resident-serve-existing.json').read_bytes()
        before = resident.control('inspect', {}, self.paths)['owner']
        def attempt(_):
            try:
                return self.replace(c)['replaced']
            except core.StateError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(attempt, range(8))).count(True), 1)
        self.assertEqual((directory / 'resident-serve-existing.json').read_bytes(), marker)
        self.assertTrue((directory / 'resident-unused-replacement.json').exists())
        after = resident.control('inspect', {}, self.paths)['owner']
        self.assertEqual(after, {**before, 'session': None})

    def test_unused_replacement_rejects_extra_evidence_and_wrong_identity(self):
        _, c, directory = self.pristine()
        self.claim(c)
        before = self.snapshot()
        for name in ['waiting-1', 'ready-1.json', 'command-1.json',
                     'transport-audit.json', 'resident-failure.json',
                     'resident-unused-replacement.json', 'unknown']:
            path = directory / name
            path.write_bytes(b'preserve')
            with self.subTest(name=name), self.assertRaises(core.StateError):
                self.replace(c)
            self.assertEqual(path.read_bytes(), b'preserve')
            self.assertEqual(self.snapshot(), before)
            path.unlink()  # Isolated test fixture only.
        for key, value in [('generation', c['generation']+1), ('owner', 'other'),
                           ('parent', 'other'), ('session', {})]:
            with self.subTest(key=key), self.assertRaises(core.StateError):
                self.replace({**c, key: value})
        for function, value in [('active_assignments', ['active']), ('active_cooldown', {'active': True})]:
            with patch.object(core, function, return_value=value), self.assertRaises(core.BusyError):
                self.replace(c)
        self.assertEqual(self.snapshot(), before)

    def test_unused_replacement_requires_exact_durable_consume_marker(self):
        _, c, directory = self.pristine()
        with self.assertRaises(FileNotFoundError):
            self.replace(c)
        self.claim(c)
        (directory / 'resident-serve-existing.json').write_bytes(b'{}')
        with self.assertRaises(core.StateError):
            self.replace(c)
        self.assertFalse((directory / 'resident-unused-replacement.json').exists())

    def test_unused_replacement_uncertain_marker_write_never_detaches_or_retries(self):
        _, c, directory = self.pristine()
        self.claim(c)
        before = self.snapshot()
        with patch.object(resident.os, 'fsync', side_effect=OSError('uncertain durability')):
            with self.assertRaises(OSError):
                self.replace(c)
        self.assertTrue((directory / 'resident-unused-replacement.json').exists())
        with self.assertRaises(core.StateError):
            self.replace(c)
        self.assertEqual(self.snapshot(), before)

    def test_success_preserves_canonical_and_request_bytes_and_consumes_forever(self):
        _, c, directory = self.pristine()
        Queue(self.paths).submit('unrelated-queued', b'preserve me', 'client')
        before = self.snapshot()
        self.assertEqual(self.claim(c), dict(ok=True, claimed=True, send_authorized=False))
        self.assertEqual(self.snapshot(), before)
        marker = (directory / 'resident-serve-existing.json').read_bytes()
        with self.assertRaises(core.StateError):
            self.claim(c)
        self.assertEqual((directory / 'resident-serve-existing.json').read_bytes(), marker)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_claim_has_one_winner(self):
        _, c, _ = self.pristine()
        def attempt(_):
            try:
                return self.claim(c)['claimed']
            except core.StateError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(attempt, range(8))).count(True), 1)

    def test_any_extra_evidence_including_partial_claim_fails_closed(self):
        _, c, directory = self.pristine()
        before = self.snapshot()
        for name in ['resident-serve-existing.json', 'waiting-1.' + 'a'*32,
                     'ready-1.json', 'command-1', 'command-1.json',
                     'command-observed-1.json', 'ticket-1', 'transport-audit.json',
                     'resident-failure.json', 'resident-failure-final.json',
                     'resident-joined.json', 'resident-stop.json', 'unknown']:
            with self.subTest(name=name):
                evidence = directory / name
                evidence.write_bytes(b'{malformed')
                with self.assertRaises(core.StateError):
                    self.claim(c)
                self.assertEqual(evidence.read_bytes(), b'{malformed')
                self.assertEqual(self.snapshot(), before)
                evidence.unlink()  # Isolated test fixture only.

    def test_wrong_generation_owner_session_hash_and_malformed_evidence(self):
        _, c, directory = self.pristine()
        for key, value in [('generation', True), ('generation', c['generation']+1),
                           ('owner', 'other'), ('parent', 'other'),
                           ('worker_pool_sha256', 'f'*64)]:
            with self.subTest(key=key), self.assertRaises(core.StateError):
                self.claim({**c, key: value})
        for key, value in [('session_id', 'b'*32), ('directory', str(directory.parent)),
                           ('descriptor_sha256', 'f'*64)]:
            with self.subTest(key=key), self.assertRaises(core.StateError):
                self.claim({**c, 'session': {**c['session'], key: value}})
        with self.assertRaises(core.StateError):
            self.claim({**c, 'extra': True})
        raw = (directory / 'session.json').read_bytes()
        (directory / 'session.json').write_bytes(b'{}')
        with self.assertRaises(core.StateError):
            self.claim(c)
        (directory / 'session.json').write_bytes(raw)
        (directory / 'wake.sock').unlink()
        with self.assertRaises(core.StateError):
            self.claim(c)
        (directory / 'wake.sock').write_bytes(b'not socket')
        with self.assertRaises(core.StateError):
            self.claim(c)

    def test_every_pool_slot_and_active_invocation_blocks_without_mutation(self):
        _, c, _ = self.pristine()
        path = self.paths.state_dir / 'resident-owner.json'
        original = core.read_json(path)
        for index in range(2):
            for phase in ['reserved', 'running', 'collect_only', 'cancel_pending']:
                row = copy.deepcopy(original)
                row['slots'][index].update(request='job', phase=phase)
                if phase == 'running':
                    row['slots'][index]['invocation'] = dict(request='job', invocation='invocation')
                with core.state_lock(self.paths, create=False) as locked:
                    core.atomic_write_json(path, row, _locked=locked)
                before = self.snapshot()
                with self.assertRaises(core.DispatchError):
                    self.claim(c)
                self.assertEqual(self.snapshot(), before)
        with core.state_lock(self.paths, create=False) as locked:
            core.atomic_write_json(path, original, _locked=locked)

    def test_active_assignment_claim_and_cooldown_block(self):
        owner, c, _ = self.pristine()
        for function, value in [('active_assignments', [{'assignment_id': 'active'}]),
                                ('active_cooldown', {'active': True})]:
            before = self.snapshot()
            with patch.object(core, function, return_value=value), self.assertRaises(core.BusyError):
                self.claim(c)
            self.assertEqual(self.snapshot(), before)
        self._arm_crash_request(owner, c)
        before = self.snapshot()
        with self.assertRaises(core.BusyError):
            self.claim(c)
        self.assertEqual(self.snapshot(), before)

    def test_missing_owner_and_session_fail_closed(self):
        self.activate()
        with self.assertRaises(core.StateError):
            self.claim(dict(generation=1, owner='none', parent='parent',
                            worker_pool_sha256='a'*64, session=None))

    def completed_recovery(self):
        owner, c, directory = self.pristine()
        self._arm_crash_request(owner, c)
        path = self.paths.state_dir / 'resident-owner.json'
        row = core.read_json(path)
        slot = row['slots'][0]
        receipt_path = core.assignment_path(slot['request'], self.paths)
        receipt = core.read_json(receipt_path)
        receipt['status'] = 'complete'
        row['generation'] += 1
        slot.update(phase='collect_only', invocation=None)
        with core.state_lock(self.paths, create=False) as locked:
            core.atomic_write_json(path, row, _locked=locked)
            core.atomic_write_json(receipt_path, receipt, _locked=locked)
        return {**c, 'generation': row['generation']}, directory, path, receipt_path

    def test_completed_fenced_collection_claim_preserves_authority_and_receipt(self):
        c, directory, _, _ = self.completed_recovery()
        before = self.snapshot()
        result = self.claim(c)
        self.assertTrue(result['claimed'])
        self.assertFalse(result['send_authorized'])
        self.assertEqual(result['recovery']['request_ids'], ['request-crash'])
        self.assertEqual(result['recovery']['prepared_ids'], [])
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(core.DispatchError):
            self.replace(c)  # Never broaden unused replacement to occupied slots.
        with self.assertRaises(core.DispatchError):
            self.claim(c)

    def test_completed_collection_requires_exact_fenced_receipt_and_no_invocation(self):
        c, directory, owner_path, receipt_path = self.completed_recovery()
        receipt = core.read_json(receipt_path)
        owner = core.read_json(owner_path)
        cases = []
        for key, value in [('status', 'armed'), ('status', 'prepared'),
                           ('status', 'indeterminate'), ('no_resend', False),
                           ('owner_generation', c['generation']),
                           ('worker_conversation_id', 'foreign'), ('worker_slot', 'slot-b')]:
            cases.append((receipt_path, {**receipt, key: value}))
        for key, value in [('phase', 'reserved'), ('phase', 'running'),
                           ('phase', 'cancel_pending'),
                           ('invocation', {'invocation': 'old', 'request': 'request-crash'})]:
            changed = copy.deepcopy(owner)
            changed['slots'][0][key] = value
            cases.append((owner_path, changed))
        for path, changed in cases:
            with self.subTest(changed=changed):
                with core.state_lock(self.paths, create=False) as locked:
                    core.atomic_write_json(path, changed, _locked=locked)
                before = self.snapshot()
                with self.assertRaises(core.DispatchError):
                    self.claim(c)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((directory / 'resident-serve-existing.json').exists())
                with core.state_lock(self.paths, create=False) as locked:
                    core.atomic_write_json(path, receipt if path == receipt_path else owner, _locked=locked)

    def test_claim_write_failure_keeps_consumed_evidence(self):
        _, c, directory = self.pristine()
        before = self.snapshot()
        with patch.object(resident.os, 'fsync', side_effect=OSError('uncertain durability')):
            with self.assertRaises(OSError):
                self.claim(c)
        self.assertTrue((directory / 'resident-serve-existing.json').exists())
        with self.assertRaises(core.StateError):
            self.claim(c)
        self.assertEqual(self.snapshot(), before)

    def test_unbound_owner_cannot_be_recovered(self):
        _, base = self._enrolled_owner()
        with self.assertRaises(core.StateError):
            self.claim({**base, 'session': None})
