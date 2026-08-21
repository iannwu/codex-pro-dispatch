from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .common import (
    HELPER_DIR,
    BridgeCommandError,
    BridgeTimeout,
    Snapshot,
    TargetConfig,
)

class BridgeBackend(Protocol):
    def list_apps(self) -> Mapping[str, Any]: ...

    def read(self, target: TargetConfig) -> Snapshot: ...

    def send(self, target: TargetConfig, message: str) -> Mapping[str, Any]: ...

    def wait(
        self,
        target: TargetConfig,
        *,
        baseline_group_count: int,
        timeout_seconds: float,
        quiet: bool = True,
    ) -> Mapping[str, Any]: ...

class SubprocessBackend:
    def __init__(self, helper_dir: Path = HELPER_DIR) -> None:
        self.helper_dir = helper_dir

    def _run(
        self,
        helper: str,
        args: Sequence[str] = (),
        *,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> Mapping[str, Any]:
        path = self.helper_dir / helper
        if not path.exists():
            raise BridgeCommandError(f"Missing helper: {path}")
        command = [str(path), *args]
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeTimeout(
                f"Helper exceeded its process timeout: {helper}",
                details={"command": command},
            ) from exc
        stdout = completed.stdout.strip()
        try:
            payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as exc:
            raise BridgeCommandError(
                f"Helper returned invalid JSON: {helper}",
                details={
                    "command": command,
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            ) from exc
        if not isinstance(payload, dict):
            raise BridgeCommandError(
                f"Helper returned a non-object JSON value: {helper}",
                details={"payload": payload},
            )
        if completed.returncode != 0 or payload.get("ok") is not True:
            error = str(payload.get("error") or f"{helper} failed")
            details = {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "payload": payload,
            }
            if error == "timeout":
                raise BridgeTimeout(error, details=details)
            raise BridgeCommandError(error, details=details)
        return payload

    def list_apps(self) -> Mapping[str, Any]:
        return self._run("cgpt-list-apps")

    def read(self, target: TargetConfig) -> Snapshot:
        return Snapshot.from_payload(
            self._run("cgpt-read-app", target.helper_args())
        )

    def send(self, target: TargetConfig, message: str) -> Mapping[str, Any]:
        return self._run(
            "cgpt-send",
            [*target.helper_args(), "-"],
            input_text=message,
        )

    def wait(
        self,
        target: TargetConfig,
        *,
        baseline_group_count: int,
        timeout_seconds: float,
        quiet: bool = True,
    ) -> Mapping[str, Any]:
        args = [
            *target.helper_args(),
            "--baseline-groups",
            str(baseline_group_count),
            "--timeout",
            str(timeout_seconds),
        ]
        if quiet:
            args.append("--quiet")
        return self._run(
            "cgpt-wait-idle",
            args,
            timeout=timeout_seconds + 15,
        )
