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
                with self.assertRaises(core.BusyError):
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
