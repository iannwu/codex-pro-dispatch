from __future__ import annotations

import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"


@unittest.skipUnless(platform.system() == "Darwin", "macOS installer only")
class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.codex_home = Path(self.temporary.name) / "codex-home"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["CODEX_HOME"] = str(self.codex_home)
        self.dispatch_home = Path(self.temporary.name) / "dispatch-home"
        self.env["CODEX_PRO_DISPATCH_HOME"] = str(self.dispatch_home)
        self.env.pop("CODEX_PRO_DISPATCH_CONFIG_DIR", None)
        self.env.pop("CODEX_PRO_DISPATCH_STATE_DIR", None)
        self.env.pop("XDG_CONFIG_HOME", None)
        self.env.pop("XDG_STATE_HOME", None)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_script(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), *args],
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

    @property
    def bin_target(self) -> Path:
        return self.home / ".local" / "bin" / "pro-dispatch"

    @property
    def skill_target(self) -> Path:
        return self.codex_home / "skills" / "codex-pro-dispatch"

    def test_install_is_idempotent_and_uninstall_removes_owned_links(self) -> None:
        first = self.run_script(INSTALL)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script(INSTALL)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.bin_target.readlink(), ROOT / "bin" / "pro-dispatch")
        self.assertEqual(
            self.skill_target.readlink(), ROOT / "skills" / "codex-pro-dispatch"
        )

        removed = self.run_script(UNINSTALL)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.bin_target.exists())
        self.assertFalse(self.skill_target.exists())

    def test_unowned_skill_target_prevents_any_link_removal(self) -> None:
        installed = self.run_script(INSTALL)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.skill_target.unlink()
        self.skill_target.write_text("owned by another installation\n", encoding="utf-8")

        removed = self.run_script(UNINSTALL)
        self.assertNotEqual(removed.returncode, 0)
        self.assertIn("Refusing to remove unowned path", removed.stderr)
        self.assertTrue(self.bin_target.is_symlink())
        self.assertTrue(self.skill_target.is_file())

    def test_unowned_target_prevents_purge_before_state_mutation(self) -> None:
        installed = self.run_script(INSTALL)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.skill_target.unlink()
        self.skill_target.write_text("owned by another installation\n", encoding="utf-8")
        worker_file = self.dispatch_home / "config" / "worker.json"
        worker_file.parent.mkdir(parents=True)
        worker_file.write_text("state must survive\n", encoding="utf-8")

        removed = self.run_script(UNINSTALL, "--purge-state")
        self.assertNotEqual(removed.returncode, 0)
        self.assertTrue(worker_file.exists())
        self.assertTrue(self.bin_target.is_symlink())


if __name__ == "__main__":
    unittest.main()
