import os
from pathlib import Path
import unittest
from unittest.mock import patch
from codex_pro_dispatch import hook_preflight, cli
from codex_pro_dispatch.core import StateError

FAKE = str(Path(__file__).with_name('fake_hook_codex.py'))

class HookPreflightTests(unittest.TestCase):
    def test_trusted_discovery_is_not_admission(self):
        result = hook_preflight.check(FAKE)
        self.assertTrue(result['ok'])
        self.assertFalse(result['admissionObserved'])

    def test_modified_disabled_wrong_and_async_hooks_refuse(self):
        for value in ('{"trustStatus":"modified"}', '{"trustStatus":"untrusted"}',
                      '{"enabled":false}', '{"async":true}', '{"command":"node wrong stop"}'):
            with self.subTest(value=value), patch.dict(os.environ, CPD_TEST_HOOK=value):
                with self.assertRaisesRegex(StateError, 'listener_hook_not_ready'):
                    hook_preflight.check(FAKE)

    def test_unavailable_binary_fails_closed(self):
        with self.assertRaisesRegex(StateError, 'hook_preflight_unavailable'):
            hook_preflight.check('/does-not-exist/codex')

    def test_failed_check_never_generates_plan(self):
        args = cli.build_parser().parse_args(['listener', 'start', '--worker-1', 'one', '--codex', FAKE])
        with patch.dict(os.environ, CPD_TEST_HOOK='{"trustStatus":"modified"}'), patch('codex_pro_dispatch.listener.plan') as plan:
            with self.assertRaisesRegex(StateError, 'listener_hook_not_ready'):
                cli.run(args)
            plan.assert_not_called()
