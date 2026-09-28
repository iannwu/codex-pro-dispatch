import json
import os
import shlex
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import test_three_feature as fixtures
from codex_pro_dispatch import hook_preflight, cli
from codex_pro_dispatch.core import StateError

ROOT = Path(__file__).resolve().parents[1]
FAKE = str(Path(__file__).with_name('fake_hook_codex.py'))

class HookPreflightTests(unittest.TestCase):
    def setUp(self):
        fixtures.ThreeFeatureTests.setUp(self)
        self.env = patch.dict(os.environ, {'CPD_TEST_HOOK': '{}', 'CPD_TEST_MODE': 'normal',
                              'CODEX_HOME': str(Path(self.temp.name)/'codex'),
                              'CODEX_PRO_DISPATCH_HOME': str(self.paths.state_dir.parent)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def check(self, mode='normal', **hook):
        with patch.dict(os.environ, CPD_TEST_MODE=mode, CPD_TEST_HOOK=json.dumps(hook)):
            return hook_preflight.check(FAKE)

    def test_trusted_and_managed_are_configuration_only(self):
        for trust in ('trusted', 'managed'):
            result = self.check(trustStatus=trust)
            self.assertEqual(result['state'], 'ready')
            self.assertFalse(result['admissionObserved'])
            self.assertEqual(result['scope'], 'fresh_process_configuration_only')
            self.assertIn('fixture-codex', result['server_identity']['userAgent'])

    def test_structural_errors_never_request_trust(self):
        for mode, changes, reason in [('empty', {}, 'missing'),
            ('duplicate', {}, 'duplicate'),
            ('normal', {'command': 'node other/resident-supervision.mjs stop', 'trustStatus':'untrusted'}, 'wrong_definition'),
            ('normal', {'eventName':'Stop', 'trustStatus':'untrusted'}, 'wrong_definition'),
            ('normal', {'async':True}, 'wrong_definition')]:
            with self.subTest(reason=reason):
                result = self.check(mode, **changes)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['state'], 'blocked')
                self.assertNotIn('trust', ' '.join(result['actions']).lower())

    def test_plugin_hook_binds_only_to_this_installation(self):
        # The host reports a plugin hook with ${PLUGIN_ROOT} expanded to its
        # versioned cache copy (observed 2026-09-27, codex-cli 0.158.0-alpha.2).
        expected = shlex.join(['node', str(ROOT/'skills/codex-pro-dispatch/scripts/resident-supervision.mjs'), 'stop'])
        plugin = {'source': 'plugin', 'pluginId': 'codex-pro-dispatch@codex-pro-dispatch',
                  'sourcePath': str(ROOT/'hooks/hooks.json'), 'trustStatus': 'untrusted'}
        result = self.check(**plugin)
        self.assertEqual((result['state'], result['reason'], result['expected_command']), ('blocked', 'untrusted', expected))
        self.assertEqual({k: result['hooks'][0][k] for k in ('source', 'pluginId', 'sourcePath')},
                         {k: plugin[k] for k in ('source', 'pluginId', 'sourcePath')})
        other = '/x/.codex/plugins/cache/codex-pro-dispatch/codex-pro-dispatch/1.3.1/skills/codex-pro-dispatch/scripts/resident-supervision.mjs'
        result = self.check(**dict(plugin, trustStatus='trusted', command=shlex.join(['node', other, 'stop'])))
        self.assertEqual((result['reason'], result['expected_command']), ('wrong_definition', expected))
        self.assertIn('keep one installation', result['actions'][0])

    def test_disabled_and_modified_collect_both_actions(self):
        result = self.check(enabled=False, trustStatus='modified')
        self.assertEqual(result['reason'], 'disabled+modified')
        self.assertEqual(len(result['actions']), 2)
        self.assertIn('Enable', result['actions'][0])
        self.assertIn('trust', result['actions'][1])

    def test_unknown_fields_are_unverified(self):
        for changes in ({'trustStatus':'future'}, {'enabled':'yes'}, {'async':None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.check(**changes)['state'], 'unverified')

    def test_notifications_are_not_replies(self):
        self.assertEqual(self.check('notify')['state'], 'ready')

    def test_protocol_errors_are_unverified_with_details(self):
        for mode in ('garbage', 'oversized', 'rpc_error', 'errors', 'not_object'):
            with self.subTest(mode=mode):
                result = self.check(mode)
                self.assertEqual(result['state'], 'unverified')
                self.assertIn('error', result)
                if mode == 'errors':
                    self.assertIn('fixture discovery error', result['error'])

    def test_unknown_binary_does_not_guess_path(self):
        with patch.object(hook_preflight, 'BUNDLED', Path('/does-not-exist/codex')):
            self.assertEqual(hook_preflight.check()['reason'], 'no_known_host')
        self.assertEqual(hook_preflight.check('/does-not-exist/codex')['state'], 'unverified')
        self.assertEqual(hook_preflight.check('codex')['reason'], 'invalid_host_path')

    def test_timeout_reaps_child(self):
        pidfile = Path(self.temp.name)/'pid'
        with patch.object(hook_preflight, 'TIMEOUT', 0.5), patch.dict(os.environ, CPD_TEST_PID=str(pidfile)):
            result = self.check('timeout')
        self.assertEqual(result['state'], 'unverified')
        pid = int(pidfile.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_early_exit_preserves_diagnostic_and_reaps_child(self):
        pidfile = Path(self.temp.name)/'exit-pid'
        with patch.dict(os.environ, CPD_TEST_PID=str(pidfile)):
            result = self.check('exit')
        self.assertEqual(result['state'], 'unverified')
        self.assertIn('ended', result['error'])
        self.assertNotIn('Operation not permitted', result['error'])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_cleanup_signal_errors_cannot_hide_modified_hook(self):
        original = os.killpg
        def reap_signal_then_error(pid, sig):
            try:
                original(pid, sig)
            except OSError:
                pass
            raise PermissionError('simulated zombie process group')
        with patch.object(hook_preflight.os, 'killpg', side_effect=reap_signal_then_error):
            result = self.check(trustStatus='modified')
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['reason'], 'modified')

    def test_blocked_hook_and_idle_blockers_are_reported_together(self):
        args = cli.build_parser().parse_args(['listener','start','--worker-1','one','--codex',FAKE])
        with patch.dict(os.environ, CPD_TEST_HOOK='{"trustStatus":"modified"}'), patch('codex_pro_dispatch.listener.plan') as plan, patch('codex_pro_dispatch.listener.startup_blockers', return_value=[{'reason':'cooldown'}]):
            with self.assertRaises(StateError) as caught:
                cli.run(args)
            self.assertEqual([b['reason'] for b in caught.exception.details['blockers']], ['listener_hook_modified','cooldown'])
            plan.assert_not_called()

    def client_root(self):
        return str(Path(self.temp.name).resolve()/'clients')

    def test_unverified_does_not_skip_generated_native_qualification(self):
        args = cli.build_parser().parse_args(['listener','start','--worker-1','one','--codex',FAKE,
                                              '--client-root',self.client_root()])
        with patch.dict(os.environ, CPD_TEST_MODE='rpc_error'):
            result = cli.run(args)
        self.assertEqual(result['kind'], 'resident_packet_call')
        self.assertEqual(result['tool'], 'functions.exec')
        self.assertEqual(result['hook_preflight']['state'], 'unverified')
        self.assertFalse(result['send_authorized'])
        self.assertFalse((self.paths.state_dir/'resident-owner.json').exists())
        packet = json.loads(Path(result['packet_file']).read_text())
        self.assertEqual(packet['kind'], 'native_listener_lifecycle_packet')
        self.assertEqual(list(packet['calls']), ['lifecycle'])
        self.assertIn('commitListenerStart', packet['calls']['lifecycle'])

    def test_empty_start_without_enrolled_pool_blocks_before_acquisition(self):
        args = cli.build_parser().parse_args(['listener','start','--codex',FAKE,'--client-root',self.client_root()])
        with self.assertRaises(StateError) as caught:
            cli.run(args)
        self.assertEqual([b['reason'] for b in caught.exception.details['blockers']], ['pool_not_configured'])
        self.assertFalse((self.paths.state_dir/'resident-owner.json').exists())
        self.assertFalse(Path(self.client_root()).exists())

    def test_empty_start_uses_enrolled_exact_pool_without_acquiring(self):
        fixtures.ThreeFeatureTests.activate(self)
        args = cli.build_parser().parse_args(['listener','start','--codex',FAKE,'--client-root',self.client_root()])
        result = cli.run(args)
        self.assertEqual(result['desired_ids'], ['worker-a', 'worker-b'])
        self.assertEqual(result['hook_preflight']['state'], 'ready')
        self.assertFalse((self.paths.state_dir/'resident-owner.json').exists())
        packet = json.loads(Path(result['packet_file']).read_text())
        self.assertEqual(packet['desired_ids'], ['worker-a', 'worker-b'])
        self.assertIsNone(packet['current_owner'])

    def test_second_worker_requires_first(self):
        args = cli.build_parser().parse_args(['listener','start','--worker-2','two','--codex',FAKE])
        with self.assertRaises(cli.ConfigurationError):
            cli.run(args)

    def test_check_command_exit_and_no_owner(self):
        for trust, code in [('trusted',0), ('modified',1)]:
            with patch.dict(os.environ, CPD_TEST_HOOK=json.dumps({'trustStatus':trust})):
                result = subprocess.run(['python3',str(ROOT/'bin/pro-dispatch'),'listener','check','--codex',FAKE], capture_output=True,text=True)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(json.loads(result.stdout)['ok'], code==0)
        self.assertFalse((self.paths.state_dir/'resident-owner.json').exists())
