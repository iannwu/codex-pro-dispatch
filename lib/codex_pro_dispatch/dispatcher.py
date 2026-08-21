from __future__ import annotations

import shlex
import time
from dataclasses import asdict
from typing import Any, Callable, Mapping

from .backend import BridgeBackend, SubprocessBackend
from .common import (
    DEFAULT_TIMEOUT_SECONDS,
    BridgeCommandError,
    BridgeTimeout,
    ConfigurationError,
    DispatchError,
    DraftPresentError,
    RuntimePaths,
    TargetConfig,
    default_paths,
    dispatch_lock,
    extract_response,
    load_receipt,
    new_assignment_id,
    receipt_path,
    save_receipt,
    sha256_text,
    snapshot_digest,
    utc_now,
    validate_assignment_id,
    wrap_prompt,
)


def _target_identity(target: TargetConfig) -> tuple[str, str, str]:
    return (target.bundle_id, target.app_name, target.window_title)


def _receipt_target(receipt: Mapping[str, Any]) -> TargetConfig:
    raw = receipt.get("target")
    if not isinstance(raw, Mapping):
        raise ConfigurationError("Receipt is missing its target")
    return TargetConfig(
        bundle_id=str(raw.get("bundle_id", "")),
        app_name=str(raw.get("app_name", "")),
        window_title=str(raw.get("window_title", "")),
        transport=str(raw.get("transport", "direct")),
        socket_path=str(raw.get("socket_path", "")),
    ).validate()

class Dispatcher:
    def __init__(
        self,
        *,
        backend: BridgeBackend | None = None,
        paths: RuntimePaths | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend or SubprocessBackend()
        self.paths = paths or default_paths()
        self.clock = clock

    def send(
        self,
        prompt: str,
        *,
        target: TargetConfig,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        assignment_id: str | None = None,
    ) -> dict[str, Any]:
        if timeout_seconds <= 0:
            raise ConfigurationError("timeout must be greater than zero")
        identity = validate_assignment_id(assignment_id or new_assignment_id())
        wrapped_prompt = wrap_prompt(prompt, identity)

        with dispatch_lock(self.paths):
            receipt_file = receipt_path(identity, self.paths)
            if receipt_file.exists():
                raise ConfigurationError(
                    f"Assignment already exists: {identity}. Use collect instead of resending."
                )
            baseline = self.backend.read(target)
            if baseline.input_value.strip():
                raise DraftPresentError(
                    "The target ChatGPT input contains an unsent draft. Clear it first."
                )
            started = self.clock()
            receipt: dict[str, Any] = {
                "created_at": utc_now(),
                "status": "prepared",
                "target": asdict(target),
                "timeout_seconds": timeout_seconds,
                "prompt": prompt,
                "prompt_sha256": sha256_text(prompt),
                "wrapped_prompt_sha256": sha256_text(wrapped_prompt),
                "baseline": {
                    "group_count": baseline.group_count,
                    "snapshot_sha256": snapshot_digest(baseline),
                    "window_title": baseline.window_title,
                    "pid": baseline.pid,
                },
            }
            receipt_file = save_receipt(identity, receipt, self.paths)

            try:
                self.backend.send(target, wrapped_prompt)
            except Exception as exc:
                receipt.update(
                    {
                        "status": "send_indeterminate",
                        "error": str(exc),
                    }
                )
                save_receipt(identity, receipt, self.paths)
                if isinstance(exc, DispatchError):
                    raise
                raise BridgeCommandError(str(exc)) from exc

            receipt.update({"status": "sent", "sent_at": utc_now()})
            save_receipt(identity, receipt, self.paths)

            try:
                wait_result = self.backend.wait(
                    target,
                    baseline_group_count=baseline.group_count,
                    timeout_seconds=timeout_seconds,
                    quiet=True,
                )
            except BridgeTimeout as exc:
                receipt.update(
                    {
                        "status": "timed_out",
                        "error": str(exc),
                        "elapsed_ms": int((self.clock() - started) * 1000),
                    }
                )
                save_receipt(identity, receipt, self.paths)
                return {
                    "ok": False,
                    "assignment_id": identity,
                    "status": "timed_out",
                    "receipt": str(receipt_file),
                    "error": "timeout",
                    "hint": f"Run: pro-dispatch collect {shlex.quote(identity)}",
                }

            after = self.backend.read(target)
            response = extract_response(after, baseline.group_count)
            if not response:
                receipt.update(
                    {
                        "status": "response_unavailable",
                        "error": "No new assistant text was exposed by Accessibility",
                        "elapsed_ms": int((self.clock() - started) * 1000),
                        "wait": dict(wait_result),
                    }
                )
                save_receipt(identity, receipt, self.paths)
                return {
                    "ok": False,
                    "assignment_id": identity,
                    "status": "response_unavailable",
                    "receipt": str(receipt_file),
                    "error": receipt["error"],
                    "hint": f"Run: pro-dispatch collect {shlex.quote(identity)}",
                }

            elapsed_ms = int((self.clock() - started) * 1000)
            receipt.update(
                {
                    "status": "complete",
                    "completed_at": utc_now(),
                    "elapsed_ms": elapsed_ms,
                    "wait": dict(wait_result),
                    "after": {
                        "group_count": after.group_count,
                        "snapshot_sha256": snapshot_digest(after),
                    },
                    "response": response,
                    "response_sha256": sha256_text(response),
                }
            )
            save_receipt(identity, receipt, self.paths)
            return {
                "ok": True,
                "assignment_id": identity,
                "status": "complete",
                "elapsed_ms": elapsed_ms,
                "receipt": str(receipt_file),
                "response": response,
            }

    def collect(
        self,
        assignment_id: str,
        *,
        target: TargetConfig,
    ) -> dict[str, Any]:
        identity = validate_assignment_id(assignment_id)
        with dispatch_lock(self.paths):
            receipt = load_receipt(identity, self.paths)
            expected_target = _receipt_target(receipt)
            if _target_identity(target) != _target_identity(expected_target):
                raise ConfigurationError(
                    "Configured ChatGPT target differs from the assignment receipt. "
                    "Restore the original app and window target before collecting.",
                    details={
                        "expected": {
                            "bundle_id": expected_target.bundle_id,
                            "app_name": expected_target.app_name,
                            "window_title": expected_target.window_title,
                        },
                        "configured": {
                            "bundle_id": target.bundle_id,
                            "app_name": target.app_name,
                            "window_title": target.window_title,
                        },
                    },
                )
            baseline = receipt.get("baseline")
            if not isinstance(baseline, Mapping):
                raise ConfigurationError("Receipt is missing its baseline")
            try:
                baseline_group_count = int(baseline["group_count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigurationError("Receipt has an invalid baseline") from exc

            snapshot = self.backend.read(target)
            response = extract_response(snapshot, baseline_group_count)
            if not response:
                return {
                    "ok": False,
                    "assignment_id": identity,
                    "status": "pending",
                    "receipt": str(receipt_path(identity, self.paths)),
                    "error": "No new assistant response is available yet",
                }
            receipt.update(
                {
                    "status": "complete",
                    "completed_at": utc_now(),
                    "after": {
                        "group_count": snapshot.group_count,
                        "snapshot_sha256": snapshot_digest(snapshot),
                    },
                    "response": response,
                    "response_sha256": sha256_text(response),
                }
            )
            save_receipt(identity, receipt, self.paths)
            return {
                "ok": True,
                "assignment_id": identity,
                "status": "complete",
                "receipt": str(receipt_path(identity, self.paths)),
                "response": response,
            }
