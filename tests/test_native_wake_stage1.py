"""Offline packet contract only. No native execution or real authority access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = ROOT / "skills/codex-pro-dispatch/scripts/parked-activation.mjs"


class ActiveSessionContractTests(unittest.TestCase):
    def test_packet_is_not_activation_and_busy_authority_stays_intact(self):
        with tempfile.TemporaryDirectory(prefix="pro-stage1-") as temporary:
            home = Path(temporary).resolve()
            env = dict(
                os.environ,
                CODEX_PRO_DISPATCH_HOME=str(home / "authority"),
                PYTHONPATH=str(ROOT / "src"),
                PYTHONDONTWRITEBYTECODE="1",
            )

            def run(argv, text=""):
                return subprocess.run(
                    argv, input=text, text=True, capture_output=True,
                    env=env, timeout=30, check=False,
                )

            def cli(*args, text=""):
                result = run(
                    [sys.executable, str(ROOT / "bin/pro-dispatch"), *args],
                    text,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            def image():
                authority = home / "authority"
                return {
                    str(p.relative_to(authority)): p.read_bytes()
                    for p in authority.rglob("*") if p.is_file()
                }

            cli("worker", "set", "--conversation-id", "stage1-pro",
                "--confirm-pro", "--native-controls-confirmed")
            cli("status", "--current")
            cli("queue", "status")
            before = image()

            for _ in range(2):
                result = run([
                    "node", str(ACTIVATION), "packet",
                    "stage1-parent", "stage1-parent", "stage1-pro",
                ])
                self.assertEqual(result.returncode, 0, result.stderr)
                packet = json.loads(result.stdout)
                self.assertEqual(packet["kind"], "native_activation_packet")
                self.assertEqual(packet["authorization"], "required_separately")
                self.assertEqual(set(packet["calls"]), {"open", "receive", "dispatch"})
                self.assertEqual(packet["trusted"]["idleMs"], 45000)
                self.assertEqual(packet["trusted"]["worker"], "stage1-pro")
                self.assertEqual(image(), before)

            cli("prepare", "--assignment-id", "stage1-pending",
                "--parent-task-id", "stage1-parent",
                "--native-controls-confirmed", text="Synthetic request only")
            occupied = image()
            result = run([
                "node", str(ACTIVATION), "packet",
                "stage1-parent", "stage1-parent", "stage1-pro",
            ])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("occupied", json.loads(result.stderr)["error"].lower())
            self.assertEqual(image(), occupied)
            self.assertEqual(
                cli("status", "stage1-pending")["assignment"]["status"],
                "prepared",
            )


if __name__ == "__main__":
    unittest.main()
