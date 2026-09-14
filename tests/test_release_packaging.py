"""macOS source-link packaging fixtures, not installed native qualification."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(platform.system() == "Darwin", "macOS source installer")
class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pro-package-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.authority = self.home / "authority"
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            CODEX_HOME=str(self.home / ".codex"),
            CODEX_PRO_DISPATCH_HOME=str(self.authority),
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.checkout = self.copy_checkout("candidate-a")
        self.bin_link = self.home / ".local/bin/pro-dispatch"
        self.skill_link = self.home / ".agents/skills/codex-pro-dispatch"

    def copy_checkout(self, name):
        target = self.root / name
        target.mkdir(mode=0o700)
        for name in ("install.sh", "uninstall.sh", "VERSION"):
            shutil.copy2(ROOT / name, target / name)
        shutil.copytree(
            ROOT / "src", target / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (target / "bin").mkdir()
        shutil.copy2(ROOT / "bin/pro-dispatch", target / "bin/pro-dispatch")
        shutil.copytree(
            ROOT / "skills/codex-pro-dispatch",
            target / "skills/codex-pro-dispatch",
        )
        return target

    def process(self, arguments, body=""):
        return subprocess.run(
            [str(value) for value in arguments],
            input=body, text=True, capture_output=True,
            env=self.env, timeout=30,
        )

    def script(self, checkout, name, *args, success=True):
        result = self.process([checkout / name, *args])
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def cli(self, *args, body=""):
        result = self.process([sys.executable, self.bin_link, *args], body)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertTrue(value["ok"])
        return value

    def links_point_to(self, checkout):
        self.assertTrue(self.bin_link.is_symlink())
        self.assertTrue(self.skill_link.is_symlink())
        self.assertEqual(self.bin_link.readlink(), checkout / "bin/pro-dispatch")
        self.assertEqual(
            self.skill_link.readlink(), checkout / "skills/codex-pro-dispatch"
        )

    def records(self):
        return {
            str(path.relative_to(self.authority)): path.read_bytes()
            for path in self.authority.rglob("*.json")
        }

    def seed_queue(self):
        self.cli(
            "worker", "set", "--conversation-id", "package-fixture-pro",
            "--confirm-pro", "--native-controls-confirmed",
        )
        self.cli(
            "queue", "submit", "--request-id", "package-A",
            "--prompt-file", "-", "--client-session-id", "package-client",
            body="synthetic packaging request",
        )

    def test_idempotent_install_and_installed_fixture_client_collection(self):
        result = self.script(self.checkout, "install.sh")
        self.assertIn("No native session was started", result.stdout)
        self.script(self.checkout, "install.sh")
        self.links_point_to(self.checkout)
        self.seed_queue()
        before = self.records()
        session = self.root / "session"
        session.mkdir(mode=0o700)
        descriptor = session / "session.json"
        descriptor.write_text(json.dumps({
            "helper": str(self.bin_link),
            "configDir": str(self.authority / "config"),
            "stateDir": str(self.authority / "state"),
            "worker": "package-fixture-pro",
            "parent": "package-fixture-parent",
            "sessionId": "a" * 32,
            "token": "b" * 48,
            "expiresAt": 1,
            "leaseMs": 1000,
            "idleMs": 1000,
            "replyMs": 1000,
        }), encoding="utf-8")
        descriptor.chmod(0o600)
        result = self.process([
            "node", self.skill_link / "scripts/parked-client.mjs",
            session, "collect", "package-A",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["state"], "queued")
        self.assertFalse(value["body_available"])
        self.assertFalse((session / "wake.sock").exists())
        self.assertEqual(self.records(), before)
        self.script(self.checkout, "uninstall.sh")
        self.assertFalse(os.path.lexists(self.bin_link))
        self.assertFalse(os.path.lexists(self.skill_link))
        self.assertEqual(self.records(), before)

    def test_refused_purge_retains_both_links_and_queue(self):
        self.script(self.checkout, "install.sh")
        self.seed_queue()
        before = self.records()
        result = self.script(
            self.checkout, "uninstall.sh", "--purge-state", success=False
        )
        self.assertIn("Purge refused", result.stderr)
        self.assertIn("without --purge-state", result.stderr)
        self.links_point_to(self.checkout)
        self.assertEqual(self.records(), before)
        self.script(self.checkout, "uninstall.sh")
        self.assertEqual(self.records(), before)

    def test_unowned_install_target_is_not_replaced(self):
        self.bin_link.parent.mkdir(parents=True)
        self.bin_link.write_text("unowned fixture\n", encoding="utf-8")
        result = self.script(self.checkout, "install.sh", success=False)
        self.assertIn("Refusing to replace existing path", result.stderr)
        self.assertEqual(self.bin_link.read_text(), "unowned fixture\n")
        self.assertFalse(os.path.lexists(self.skill_link))
        self.assertFalse(self.authority.exists())

    def test_unowned_skill_prevents_uninstall_and_purge(self):
        self.script(self.checkout, "install.sh")
        self.seed_queue()
        before = self.records()
        self.skill_link.unlink()
        self.skill_link.write_text("different owner\n", encoding="utf-8")
        result = self.script(
            self.checkout, "uninstall.sh", "--purge-state", success=False
        )
        self.assertIn("Refusing to remove unowned path", result.stderr)
        self.assertTrue(self.bin_link.is_symlink())
        self.assertEqual(self.skill_link.read_text(), "different owner\n")
        self.assertEqual(self.records(), before)

    def test_broken_node_preflight_creates_no_links(self):
        shims = self.root / "shims"
        shims.mkdir(mode=0o700)
        node = shims / "node"
        node.write_text("#!/bin/sh\nexit 79\n", encoding="utf-8")
        node.chmod(0o700)
        self.env["PATH"] = str(shims) + os.pathsep + self.env["PATH"]
        result = self.script(self.checkout, "install.sh", success=False)
        self.assertIn("Node.js standard-library preflight failed", result.stderr)
        self.assertFalse(os.path.lexists(self.bin_link))
        self.assertFalse(os.path.lexists(self.skill_link))
        self.assertFalse(self.authority.exists())

    def test_owned_switch_and_rollback_between_compatible_fixture_copies(self):
        second = self.copy_checkout("candidate-b")
        self.script(self.checkout, "install.sh")
        self.seed_queue()
        self.cli("queue", "cancel", "package-A")
        before = self.records()

        self.script(second, "install.sh", success=False)
        self.links_point_to(self.checkout)
        self.script(self.checkout, "uninstall.sh")
        self.script(second, "install.sh")
        self.links_point_to(second)
        self.assertEqual(self.records(), before)
        self.assertEqual(self.cli("queue", "collect", "package-A")["state"], "cancelled")

        self.script(second, "uninstall.sh")
        self.script(self.checkout, "install.sh")
        self.links_point_to(self.checkout)
        self.assertEqual(self.records(), before)
        self.assertEqual(self.cli("queue", "collect", "package-A")["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
