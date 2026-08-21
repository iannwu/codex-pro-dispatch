from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Mapping

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import codex_pro_dispatch as cpd


class FakeBackend:
    def __init__(
        self,
        snapshots: list[cpd.Snapshot],
        *,
        wait_error: Exception | None = None,
        apps: list[Mapping[str, Any]] | None = None,
    ) -> None:
        self.snapshots = list(snapshots)
        self.wait_error = wait_error
        self.apps = list(apps or [])
        self.read_count = 0
        self.send_count = 0
        self.sent_messages: list[str] = []
        self.wait_count = 0

    def list_apps(self) -> Mapping[str, Any]:
        return {"ok": True, "accessibility_trusted": True, "apps": self.apps}

    def read(self, target: cpd.TargetConfig) -> cpd.Snapshot:
        self.read_count += 1
        if not self.snapshots:
            raise AssertionError("No fake snapshot remains")
        return self.snapshots.pop(0)

    def send(self, target: cpd.TargetConfig, message: str) -> Mapping[str, Any]:
        self.send_count += 1
        self.sent_messages.append(message)
        return {"ok": True}

    def wait(
        self,
        target: cpd.TargetConfig,
        *,
        baseline_group_count: int,
        timeout_seconds: float,
        quiet: bool = True,
    ) -> Mapping[str, Any]:
        self.wait_count += 1
        if self.wait_error:
            raise self.wait_error
        return {
            "ok": True,
            "elapsed_ms": 10,
            "group_count": baseline_group_count + 2,
        }


class FakeDispatcher:
    def send(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "status": "complete", "response": prompt}

    def collect(self, assignment_id: str, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "status": "complete", "response": assignment_id}


class CodexProDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = cpd.RuntimePaths(
            config_dir=root / "config",
            state_dir=root / "state",
        )
        self.target = cpd.TargetConfig(
            bundle_id="com.openai.chat",
            app_name="ChatGPT Classic",
            window_title="Worker",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def snapshot(
        group_count: int,
        messages: list[Mapping[str, Any]] | None = None,
        *,
        draft: str = "",
    ) -> cpd.Snapshot:
        return cpd.Snapshot(
            group_count=group_count,
            messages=tuple(messages or []),
            input_value=draft,
            bundle_id="com.openai.chat",
            pid=42,
            window_title="Worker",
        )

    def test_config_roundtrip_and_private_mode(self) -> None:
        path = cpd.save_config(self.target, self.paths)
        self.assertEqual(cpd.load_config(self.paths), self.target)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_atomic_receipt_write_is_private_and_valid_json(self) -> None:
        path = cpd.save_receipt("dispatch-test", {"status": "prepared"}, self.paths)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["assignment_id"], "dispatch-test")
        self.assertEqual(payload["status"], "prepared")

    def test_assignment_id_rejects_path_traversal(self) -> None:
        for value in ["../secret", "/absolute", "bad space", ""]:
            with self.subTest(value=value):
                with self.assertRaises(cpd.ConfigurationError):
                    cpd.validate_assignment_id(value)

    def test_extract_response_preserves_code_and_diff_text(self) -> None:
        code = "```ts\nconst answer = 42;\nconsole.log(answer);\n```"
        diff = "@@ -1 +1 @@\n-old\n+new"
        snapshot = self.snapshot(
            6,
            [
                {"index": 2, "kind": "text", "text": "old"},
                {"index": 4, "kind": "thinking", "text": "Thought for 2 seconds"},
                {"index": 5, "kind": "text", "text": code},
                {"index": 5, "kind": "text", "text": diff},
            ],
        )
        self.assertEqual(cpd.extract_response(snapshot, 4), f"{code}\n{diff}")

    def test_wrap_prompt_identifies_assignment_without_claiming_tests(self) -> None:
        wrapped = cpd.wrap_prompt("Implement the repair.", "dispatch-123")
        self.assertIn("assignment_id=dispatch-123", wrapped)
        self.assertIn("Implement the repair.", wrapped)
        self.assertIn("Do not claim that local commands or tests ran", wrapped)

    def test_successful_dispatch_sends_once_and_writes_complete_receipt(self) -> None:
        response = "Implemented.\n```diff\n+safe\n```"
        backend = FakeBackend(
            [
                self.snapshot(3),
                self.snapshot(5, [{"index": 4, "kind": "text", "text": response}]),
            ]
        )
        dispatcher = cpd.Dispatcher(backend=backend, paths=self.paths)
        result = dispatcher.send(
            "Do the work",
            target=self.target,
            timeout_seconds=10,
            assignment_id="dispatch-success",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], response)
        self.assertEqual(backend.send_count, 1)
        self.assertEqual(backend.wait_count, 1)
        receipt = cpd.load_receipt("dispatch-success", self.paths)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["response_sha256"], cpd.sha256_text(response))

    def test_existing_assignment_receipt_blocks_resubmission_before_read(self) -> None:
        cpd.save_receipt("dispatch-existing", {"status": "sent"}, self.paths)
        backend = FakeBackend([self.snapshot(1)])
        dispatcher = cpd.Dispatcher(backend=backend, paths=self.paths)

        with self.assertRaises(cpd.ConfigurationError):
            dispatcher.send(
                "Do not resend",
                target=self.target,
                assignment_id="dispatch-existing",
            )

        self.assertEqual(backend.read_count, 0)
        self.assertEqual(backend.send_count, 0)

    def test_draft_refusal_occurs_before_send(self) -> None:
        backend = FakeBackend([self.snapshot(3, draft="unfinished local note")])
        dispatcher = cpd.Dispatcher(backend=backend, paths=self.paths)
        with self.assertRaises(cpd.DraftPresentError):
            dispatcher.send(
                "Do the work",
                target=self.target,
                assignment_id="dispatch-draft",
            )
        self.assertEqual(backend.send_count, 0)
        self.assertFalse(cpd.receipt_path("dispatch-draft", self.paths).exists())

    def test_timeout_never_resends_and_can_be_collected_later(self) -> None:
        backend = FakeBackend(
            [
                self.snapshot(8),
                self.snapshot(
                    10,
                    [{"index": 9, "kind": "text", "text": "finished later"}],
                ),
            ],
            wait_error=cpd.BridgeTimeout("timeout"),
        )
        dispatcher = cpd.Dispatcher(backend=backend, paths=self.paths)
        timed_out = dispatcher.send(
            "Long task",
            target=self.target,
            timeout_seconds=1,
            assignment_id="dispatch-timeout",
        )
        self.assertFalse(timed_out["ok"])
        self.assertEqual(timed_out["status"], "timed_out")
        self.assertEqual(backend.send_count, 1)

        collected = dispatcher.collect("dispatch-timeout", target=self.target)
        self.assertTrue(collected["ok"])
        self.assertEqual(collected["response"], "finished later")
        self.assertEqual(backend.send_count, 1)

    def test_collect_refuses_a_different_app_or_window_target(self) -> None:
        backend = FakeBackend(
            [self.snapshot(8)],
            wait_error=cpd.BridgeTimeout("timeout"),
        )
        dispatcher = cpd.Dispatcher(backend=backend, paths=self.paths)
        dispatcher.send(
            "Long task",
            target=self.target,
            timeout_seconds=1,
            assignment_id="dispatch-target",
        )

        wrong_target = cpd.TargetConfig(
            bundle_id=self.target.bundle_id,
            app_name=self.target.app_name,
            window_title="Different Worker",
        )
        with self.assertRaises(cpd.ConfigurationError):
            dispatcher.collect("dispatch-target", target=wrong_target)
        self.assertEqual(backend.read_count, 1)

    def test_response_unavailable_does_not_resend(self) -> None:
        backend = FakeBackend([self.snapshot(1), self.snapshot(3, [])])
        dispatcher = cpd.Dispatcher(backend=backend, paths=self.paths)
        result = dispatcher.send(
            "Task",
            target=self.target,
            assignment_id="dispatch-no-response",
        )
        self.assertEqual(result["status"], "response_unavailable")
        self.assertEqual(backend.send_count, 1)

    def test_nonblocking_lock_rejects_concurrent_dispatch(self) -> None:
        with cpd.dispatch_lock(self.paths):
            with self.assertRaises(cpd.BusyError):
                with cpd.dispatch_lock(self.paths):
                    self.fail("nested lock should not be acquired")

    def test_app_name_resolution_requires_one_exact_running_match(self) -> None:
        backend = FakeBackend(
            [],
            apps=[
                {
                    "name": "ChatGPT Classic",
                    "bundle_id": "com.openai.chat.classic",
                    "pid": 123,
                }
            ],
        )
        target = cpd._resolve_target_from_app_name(
            backend,
            "chatgpt classic",
            window_title="Worker",
            transport="daemon",
            socket_path="/tmp/test.sock",
        )
        self.assertEqual(target.bundle_id, "com.openai.chat.classic")
        self.assertEqual(target.app_name, "ChatGPT Classic")
        self.assertEqual(target.transport, "daemon")

    def test_daemon_roundtrip_and_socket_permissions(self) -> None:
        socket_path = self.paths.state_dir / "test.sock"
        cpd._secure_directory(socket_path.parent)
        server = cpd._ThreadingUnixServer(
            socket_path,
            dispatcher=FakeDispatcher(),  # type: ignore[arg-type]
            target=self.target,
        )
        socket_path.chmod(0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(50):
                if socket_path.exists():
                    break
                time.sleep(0.01)
            ping = cpd._socket_request(
                socket_path,
                {"action": "ping"},
                timeout_seconds=2,
            )
            self.assertTrue(ping["ok"])
            result = cpd._socket_request(
                socket_path,
                {"action": "send", "prompt": "hello", "timeout_seconds": 1},
                timeout_seconds=2,
            )
            self.assertEqual(result["response"], "hello")
            self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if socket_path.exists():
                socket_path.unlink()

    def test_daemon_rejects_unknown_action(self) -> None:
        server = object.__new__(cpd._ThreadingUnixServer)
        server.dispatcher = FakeDispatcher()  # type: ignore[attr-defined]
        server.target = self.target  # type: ignore[attr-defined]
        with self.assertRaises(cpd.DaemonError):
            server.handle_payload({"action": "destroy-everything"})

    def test_socket_path_is_confined_to_private_state_directory(self) -> None:
        nested = cpd.TargetConfig(
            bundle_id=self.target.bundle_id,
            socket_path="sockets/worker.sock",
        )
        self.assertEqual(
            cpd.configured_socket_path(nested, self.paths),
            (self.paths.state_dir / "sockets" / "worker.sock").resolve(),
        )

        escaped = cpd.TargetConfig(
            bundle_id=self.target.bundle_id,
            socket_path="/tmp/codex-pro-dispatch.sock",
        )
        with self.assertRaises(cpd.ConfigurationError):
            cpd.configured_socket_path(escaped, self.paths)


if __name__ == "__main__":
    unittest.main()
