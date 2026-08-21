from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "pro-dispatch"


class CliTests(unittest.TestCase):
    def test_help(self) -> None:
        completed = subprocess.run(
            [str(CLI), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Dispatch assignments from Codex", completed.stdout)

    def test_missing_config_is_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                **os.environ,
                "XDG_CONFIG_HOME": str(Path(directory) / "config"),
                "XDG_STATE_HOME": str(Path(directory) / "state"),
            }
            completed = subprocess.run(
                [str(CLI), "doctor"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error_type"], "ConfigurationError")


if __name__ == "__main__":
    unittest.main()
