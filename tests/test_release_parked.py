"""Reduced-tree packaging checks; all CLI state is disposable fixture state."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin/pro-dispatch"
SKILL = ROOT / "skills/codex-pro-dispatch/scripts/pro-dispatch"
BASELINE = "860876820ef52e7d3ffc62e626de213b527fe710"
RUNTIME_NAMES = {
    "skills/codex-pro-dispatch/scripts/parked-runner.js",
    "skills/codex-pro-dispatch/scripts/parked-socket.mjs",
    "skills/codex-pro-dispatch/scripts/parked-client.mjs",
}
TEST_NAMES = {
    "tests/test_parked_runner.cjs",
    "tests/test_parked_socket.mjs",
    "tests/test_parked_delivery_integration.mjs",
    "tests/test_parked_queued_resume.mjs",
}
EXCLUDED = (
    "request", "request-status", "request-collect", "ack", "native-broker"
)


class ReleaseParkedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pro-release-parked-")
        self.addCleanup(temporary.cleanup)
        self.fixture = Path(temporary.name).resolve(strict=True)
        self.home = self.fixture / "authority"
        self.env = dict(
            os.environ,
            HOME=str(self.fixture),
            CODEX_PRO_DISPATCH_HOME=str(self.home),
            PYTHONPATH=str(ROOT / "src"),
            PYTHONDONTWRITEBYTECODE="1",
        )

    def call(self, launcher, *args, body=None, code=0, as_json=True):
        result = subprocess.run(
            [sys.executable, str(launcher), *args],
            input=body, text=True, capture_output=True,
            env=self.env, timeout=15,
        )
        self.assertEqual(result.returncode, code, result.stderr or result.stdout)
        if not as_json:
            return result
        value = json.loads(result.stdout if code == 0 else result.stderr)
        self.assertEqual(value["ok"], code == 0)
        return value

    def configure(self):
        self.call(
            BIN, "worker", "set", "--conversation-id", "unit4-pro",
            "--confirm-pro", "--native-controls-confirmed",
        )

    def submit(self, launcher, body="unit4 opaque input\n", code=0):
        return self.call(
            launcher, "queue", "submit", "--request-id", "unit4-A",
            "--prompt-file", "-", "--client-session-id", "unit4-client",
            body=body, code=code,
        )

    def test_carried_bytes_match_manifest(self):
        manifest = json.loads(
            (ROOT / "tests/release-unit4-manifest.json").read_text("utf-8")
        )
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["baseline"], BASELINE)
        self.assertEqual(set(manifest["runtime"]), RUNTIME_NAMES)
        self.assertEqual(set(manifest["tests"]), TEST_NAMES)
        self.assertEqual(
            sum(len(v["cases"]) for v in manifest["tests"].values()), 59
        )
        pins = dict(manifest["runtime"])
        pins.update({k: v["sha256"] for k, v in manifest["tests"].items()})
        for name, expected in pins.items():
            with self.subTest(name=name):
                path = ROOT / name
                self.assertFalse(path.is_symlink())
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(), expected
                )

    def test_reduced_python_dependency_and_parser_closure(self):
        package = ROOT / "src/codex_pro_dispatch"
        self.assertEqual(
            {p.relative_to(package).as_posix() for p in package.rglob("*.py")},
            {"__init__.py", "cli.py", "core.py", "queue.py", "native_storage.py", "resident.py", "listener.py"},
        )
        forbidden = {
            "native_client", "native_endpoint", "native_broker",
            "native_transport", "native_setup", "native_q1",
            "native_schema", "native_qualification",
        }
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""] + [a.name for a in node.names]
                self.assertFalse(
                    any(forbidden.intersection(m.split(".")) for m in modules),
                    str(path),
                )
            self.assertFalse(any(
                isinstance(n, ast.FunctionDef) and n.name == "native_main"
                for n in tree.body
            ))

    def test_both_launchers_work_before_configuration(self):
        versions = []
        for launcher in (BIN, SKILL):
            help_result = self.call(launcher, "--help", as_json=False)
            self.assertIn("queue", help_result.stdout)
            versions.append(
                self.call(launcher, "--version", as_json=False).stdout
            )
        self.assertEqual(versions[0], versions[1])
        self.assertTrue(versions[0].startswith("pro-dispatch "))
        self.assertFalse(self.home.exists())

    def test_both_launchers_share_the_same_queue(self):
        self.configure()
        first = self.submit(SKILL)
        self.assertEqual(first["state"], "queued")
        self.assertFalse(first["send_authorized"])
        observed = self.call(BIN, "queue", "status", "unit4-A")
        self.assertEqual(observed["fingerprint"], first["fingerprint"])
        for launcher in (BIN, SKILL):
            state = self.call(launcher, "status")
            self.assertEqual(state["paths"], {
                "config_dir": str(self.home / "config"),
                "state_dir": str(self.home / "state"),
            })
            self.assertIsNone(state["active_assignment"])
        self.call(BIN, "queue", "cancel", "unit4-A")
        self.assertEqual(self.submit(SKILL)["state"], "cancelled")
        self.submit(BIN, body="different input", code=4)
        self.assertEqual(
            self.call(SKILL, "queue", "collect", "unit4-A")["state"], "cancelled"
        )

    def test_excluded_commands_are_rejected_by_both_launchers(self):
        for launcher in (BIN, SKILL):
            for command in EXCLUDED:
                with self.subTest(launcher=launcher, command=command):
                    result = self.call(
                        launcher, command, code=2, as_json=False
                    )
                    self.assertIn("invalid choice", result.stderr)
        self.assertFalse(self.home.exists())

    def test_foreign_receipt_is_readable_but_blocks_mutation(self):
        self.configure()
        rid = "unit4-foreign"
        self.call(
            BIN, "prepare", "--assignment-id", rid,
            "--parent-task-id", "unit4-parent",
            "--native-controls-confirmed", body="fixture only",
        )
        self.call(BIN, "arm", rid)
        receipt = self.call(BIN, "status", rid)["assignment"]
        receipt["native_client"] = True
        path = self.home / "state/assignments" / (rid + ".json")
        path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        before = path.read_bytes()
        worker = self.home / "config/worker.json"
        worker_before = worker.read_bytes()
        self.assertFalse((self.home / "state/native-client").exists())
        for launcher in (BIN, SKILL):
            self.assertTrue(
                self.call(launcher, "status", rid)["assignment"]["native_client"]
            )
            self.submit(launcher, code=4)
            self.call(launcher, "worker", "reset", "--force", code=4)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(worker.read_bytes(), worker_before)
        self.assertFalse((self.home / "state/queue/unit4-A.json").exists())


if __name__ == "__main__":
    unittest.main()
