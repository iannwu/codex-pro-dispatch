from __future__ import annotations

import os
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.codex_home = Path(self.temporary.name) / "codex-home"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.fake_bin = Path(self.temporary.name) / "fake-bin"
        self.fake_bin.mkdir()
        uname = self.fake_bin / "uname"
        uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        uname.chmod(0o755)
        self.env["PATH"] = f"{self.fake_bin}{os.pathsep}{self.env['PATH']}"
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
        return self.home / ".agents" / "skills" / "codex-pro-dispatch"

    @property
    def legacy_skill_target(self) -> Path:
        return self.codex_home / "skills" / "codex-pro-dispatch"

    @property
    def hook_target(self) -> Path:
        return self.codex_home / "hooks.json"

    @property
    def expected_hook(self) -> dict[str, object]:
        supervisor = (
            ROOT / "skills" / "codex-pro-dispatch" / "scripts" / "resident-supervision.mjs"
        ).resolve()
        return {
            "type": "command",
            "command": f"node {shlex.quote(str(supervisor))} stop",
            "timeout": 15,
            "async": False,
        }

    def test_fresh_install_adds_pinned_global_stop_hook(self) -> None:
        installed = self.run_script(INSTALL)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        config = json.loads(self.hook_target.read_text(encoding="utf-8"))
        self.assertEqual(config["hooks"]["Stop"], [{"hooks": [self.expected_hook]}])

    def test_reinstall_does_not_duplicate_stop_hook(self) -> None:
        first = self.run_script(INSTALL)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.hook_target.read_bytes()
        second = self.run_script(INSTALL)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.hook_target.read_bytes(), before)

    def test_install_preserves_unrelated_hook_configuration(self) -> None:
        unrelated = {
            "description": "owned elsewhere",
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}],
                "Stop": [{"matcher": "other", "hooks": [{"type": "command", "command": "true"}]}],
            },
        }
        self.hook_target.parent.mkdir(parents=True)
        self.hook_target.write_text(json.dumps(unrelated), encoding="utf-8")

        installed = self.run_script(INSTALL)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        config = json.loads(self.hook_target.read_text(encoding="utf-8"))
        self.assertEqual(config["description"], unrelated["description"])
        self.assertEqual(config["hooks"]["SessionStart"], unrelated["hooks"]["SessionStart"])
        self.assertEqual(config["hooks"]["Stop"][0], unrelated["hooks"]["Stop"][0])
        self.assertEqual(config["hooks"]["Stop"][1], {"hooks": [self.expected_hook]})

    def test_conflicting_resident_hook_fails_before_installing_links(self) -> None:
        conflict = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "node /another/resident-supervision.mjs stop",
                                "timeout": 15,
                                "async": False,
                            }
                        ]
                    }
                ]
            }
        }
        self.hook_target.parent.mkdir(parents=True)
        original = json.dumps(conflict)
        self.hook_target.write_text(original, encoding="utf-8")

        installed = self.run_script(INSTALL)
        self.assertNotEqual(installed.returncode, 0)
        self.assertIn("Conflicting resident supervision hook", installed.stderr)
        self.assertEqual(self.hook_target.read_text(encoding="utf-8"), original)
        self.assertFalse(self.bin_target.exists())
        self.assertFalse(self.skill_target.exists())

    def test_malformed_hook_configuration_fails_before_installing_links(self) -> None:
        self.hook_target.parent.mkdir(parents=True)
        self.hook_target.write_text("{not json\n", encoding="utf-8")

        installed = self.run_script(INSTALL)
        self.assertNotEqual(installed.returncode, 0)
        self.assertIn("Malformed hook configuration", installed.stderr)
        self.assertFalse(self.bin_target.exists())
        self.assertFalse(self.skill_target.exists())

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

    def test_install_is_idempotent_when_codex_home_matches_agents_home(self) -> None:
        self.env["CODEX_HOME"] = f"{self.home / '.agents'}/"

        first = self.run_script(INSTALL)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script(INSTALL)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(self.skill_target.is_symlink())
        self.assertEqual(self.skill_target.readlink(), ROOT / "skills" / "codex-pro-dispatch")

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

    def test_owned_legacy_skill_install_is_migrated(self) -> None:
        self.legacy_skill_target.parent.mkdir(parents=True)
        self.legacy_skill_target.symlink_to(ROOT / "skills" / "codex-pro-dispatch")

        installed = self.run_script(INSTALL)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn("Migrated legacy skill link", installed.stdout)
        self.assertFalse(self.legacy_skill_target.exists())
        self.assertTrue(self.bin_target.is_symlink())
        self.assertTrue(self.skill_target.is_symlink())

    def test_unowned_legacy_skill_install_blocks_migration(self) -> None:
        self.legacy_skill_target.parent.mkdir(parents=True)
        self.legacy_skill_target.write_text("owned elsewhere\n", encoding="utf-8")

        installed = self.run_script(INSTALL)
        self.assertNotEqual(installed.returncode, 0)
        self.assertIn("Refusing to migrate unowned legacy skill path", installed.stderr)
        self.assertFalse(self.bin_target.exists())
        self.assertFalse(self.skill_target.exists())


if __name__ == "__main__":
    unittest.main()
