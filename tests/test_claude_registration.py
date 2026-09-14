import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/codex-pro-dispatch/scripts/register-claude.py'


class ClaudeRegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='pro-registration-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.source = self.home / '.agents/skills/codex-pro-dispatch'
        self.target = self.home / '.claude/skills/codex-pro-dispatch'

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              env=dict(os.environ, HOME=str(self.home)),
                              capture_output=True, text=True, timeout=10)

    def install_source(self):
        for name in ('SKILL.md', 'scripts/pro-dispatch', 'scripts/parked-activation.mjs'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture, must never execute\n')

    def test_missing_source_does_not_create_registration(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Paste into Codex', result.stderr)
        self.assertFalse(self.target.parent.exists())

    def test_partial_source_refused(self):
        self.source.mkdir(parents=True)
        (self.source / 'SKILL.md').write_text('partial')
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse(self.target.parent.exists())

    def test_install_repeat_and_remove_preserve_source(self):
        self.install_source()
        before = {str(p): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(os.readlink(self.target), str(self.source))
        self.assertEqual(self.target.resolve(), self.source.resolve())
        self.assertEqual(self.run_script('--remove').returncode, 0)
        self.assertEqual(self.run_script('--remove').returncode, 0)
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertFalse((self.home / '.local/state').exists())

    def test_unrelated_file_and_broken_link_are_preserved(self):
        self.install_source()
        self.target.parent.mkdir(parents=True)
        self.target.write_text('unrelated')
        for arg in ((), ('--remove',)):
            self.assertNotEqual(self.run_script(*arg).returncode, 0)
            self.assertEqual(self.target.read_text(), 'unrelated')
        self.target.unlink()
        self.target.symlink_to(self.home / 'unrelated-missing')
        for arg in ((), ('--remove',)):
            self.assertNotEqual(self.run_script(*arg).returncode, 0)
            self.assertEqual(os.readlink(self.target), str(self.home / 'unrelated-missing'))

    def test_remove_owned_dangling_registration(self):
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(self.source)
        self.assertEqual(self.run_script('--remove').returncode, 0)
        self.assertFalse(os.path.lexists(self.target))


if __name__ == '__main__':
    unittest.main()
