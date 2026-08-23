from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

APP_NAME = "codex-pro-dispatch"
SCHEMA_VERSION = 1

ACTIVE_STATUSES = frozenset({"prepared", "submitted", "pending", "indeterminate", "ambiguous"})
TERMINAL_STATUSES = frozenset({"complete", "abandoned", "failed"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RESULT_MARKER_PREFIX = "[CODEX_PRO_DISPATCH_RESULT assignment_id="


class DispatchError(RuntimeError):
    """Expected, user-facing error."""

    exit_code = 2

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class ConfigurationError(DispatchError):
    pass


class BusyError(DispatchError):
    exit_code = 3


class StateError(DispatchError):
    exit_code = 4


class MarkerError(DispatchError):
    exit_code = 5


@dataclass(frozen=True)
class RuntimePaths:
    config_dir: Path
    state_dir: Path

    @property
    def worker_file(self) -> Path:
        return self.config_dir / "worker.json"

    @property
    def assignments_dir(self) -> Path:
        return self.state_dir / "assignments"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "state.lock"


@dataclass(frozen=True)
class WorkerConfig:
    conversation_id: str
    label: str
    model_confirmation: str
    configured_at: str


@dataclass(frozen=True)
class PreparedAssignment:
    assignment_id: str
    worker_conversation_id: str
    parent_task_id: str
    receipt_path: Path
    wrapped_prompt: str
    continuation_of: str | None = None


def default_paths() -> RuntimePaths:
    combined_home = os.environ.get("CODEX_PRO_DISPATCH_HOME")
    if combined_home:
        root = Path(combined_home).expanduser()
        return RuntimePaths(config_dir=root / "config", state_dir=root / "state")

    config_home = Path(
        os.environ.get("CODEX_PRO_DISPATCH_CONFIG_DIR")
        or os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    ).expanduser()
    state_home = Path(
        os.environ.get("CODEX_PRO_DISPATCH_STATE_DIR")
        or os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    ).expanduser()
    return RuntimePaths(
        config_dir=config_home / APP_NAME,
        state_dir=state_home / APP_NAME,
    )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigurationError(
            f"Directory is not private: {path}", details={"mode": oct(mode)}
        )


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _secure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"Expected a JSON object in {path}")
    return value


def validate_identifier(value: str, *, field: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ConfigurationError(
            f"{field} must use only letters, digits, dot, underscore, colon, and hyphen",
            details={field: value},
        )
    return value


def validate_status(value: str) -> str:
    if value not in ALL_STATUSES:
        raise ConfigurationError("Invalid assignment status", details={"status": value})
    return value


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def new_assignment_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"dispatch-{stamp}-{secrets.token_hex(4)}"


def prompt_marker(assignment_id: str) -> str:
    validate_identifier(assignment_id, field="assignment_id")
    return f"[CODEX_PRO_DISPATCH assignment_id={assignment_id}]"


def result_marker(assignment_id: str) -> str:
    validate_identifier(assignment_id, field="assignment_id")
    return f"{RESULT_MARKER_PREFIX}{assignment_id}]"


def wrap_prompt(prompt: str, assignment_id: str) -> str:
    cleaned = normalize_newlines(prompt).strip()
    if not cleaned:
        raise ConfigurationError("Prompt is empty")
    marker = result_marker(assignment_id)
    return (
        f"{prompt_marker(assignment_id)}\n\n"
        f"{cleaned}\n\n"
        "Completion protocol:\n"
        f"1. Begin your final response with this exact line: {marker}\n"
        "2. Then provide the requested deliverable.\n"
        "3. Use only tools actually available inside this Chat conversation.\n"
        "4. Do not claim a repository write, command, test, or deployment unless it actually occurred.\n"
        "5. Keep this assignment ID in context for any follow-up in this worker thread."
    )


def parse_result(response: str, assignment_id: str) -> tuple[str, str]:
    normalized = normalize_newlines(response)
    expected = result_marker(assignment_id)
    lines = normalized.split("\n")
    first_nonempty = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_nonempty is None:
        raise MarkerError("Worker response is empty")
    first = lines[first_nonempty]
    if first != expected:
        raise MarkerError(
            "Worker response does not begin with the expected result marker",
            details={"expected": expected, "actual": first},
        )

    for line in lines[first_nonempty + 1 :]:
        stripped = line.strip()
        if stripped.startswith(RESULT_MARKER_PREFIX) and stripped != expected:
            raise MarkerError(
                "Worker response contains a mismatched assignment marker",
                details={"expected": expected, "actual": stripped},
            )

    payload_lines = lines[first_nonempty + 1 :]
    if payload_lines and payload_lines[0] == "":
        payload_lines = payload_lines[1:]
    payload = "\n".join(payload_lines).rstrip("\n")
    return normalized, payload


@contextlib.contextmanager
def state_lock(paths: RuntimePaths | None = None) -> Iterable[None]:
    runtime = paths or default_paths()
    _secure_directory(runtime.state_dir)
    descriptor = os.open(runtime.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def save_worker(
    conversation_id: str,
    *,
    label: str = "Codex Pro Dispatch Worker",
    confirm_pro: bool,
    paths: RuntimePaths | None = None,
) -> WorkerConfig:
    runtime = paths or default_paths()
    validate_identifier(conversation_id, field="conversation_id")
    cleaned_label = label.strip()
    if not cleaned_label:
        raise ConfigurationError("Worker label is empty")
    if len(cleaned_label) > 120:
        raise ConfigurationError("Worker label is too long")
    if not confirm_pro:
        raise ConfigurationError(
            "The user must visibly select Pro in the worker conversation and confirm it"
        )
    worker = WorkerConfig(
        conversation_id=conversation_id,
        label=cleaned_label,
        model_confirmation="user-confirmed-pro",
        configured_at=utc_now(),
    )
    with state_lock(runtime):
        current = active_assignment(runtime)
        if current:
            raise BusyError(
                "Cannot replace the worker while an assignment is unresolved",
                details={
                    "assignment_id": current.get("assignment_id"),
                    "status": current.get("status"),
                },
            )
        atomic_write_json(
            runtime.worker_file,
            {
                "schema_version": SCHEMA_VERSION,
                "conversation_id": worker.conversation_id,
                "label": worker.label,
                "model_confirmation": worker.model_confirmation,
                "configured_at": worker.configured_at,
            },
        )
    return worker


def load_worker(paths: RuntimePaths | None = None) -> WorkerConfig:
    runtime = paths or default_paths()
    value = read_json(runtime.worker_file)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(f"Unsupported worker config schema: {runtime.worker_file}")
    conversation_id = validate_identifier(
        str(value.get("conversation_id", "")), field="conversation_id"
    )
    label = str(value.get("label", "")).strip()
    if not label:
        raise ConfigurationError(f"Worker label is missing: {runtime.worker_file}")
    confirmation = str(value.get("model_confirmation", ""))
    if confirmation != "user-confirmed-pro":
        raise ConfigurationError(
            "Worker model has not been confirmed as Pro by the user"
        )
    configured_at = str(value.get("configured_at", ""))
    if not configured_at:
        raise ConfigurationError(f"Worker configured_at is missing: {runtime.worker_file}")
    return WorkerConfig(
        conversation_id=conversation_id,
        label=label,
        model_confirmation=confirmation,
        configured_at=configured_at,
    )


def assignment_path(assignment_id: str, paths: RuntimePaths | None = None) -> Path:
    runtime = paths or default_paths()
    validate_identifier(assignment_id, field="assignment_id")
    return runtime.assignments_dir / f"{assignment_id}.json"


def load_assignment(
    assignment_id: str, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    path = assignment_path(assignment_id, paths)
    value = read_json(path)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(f"Unsupported assignment schema: {path}")
    if value.get("assignment_id") != assignment_id:
        raise ConfigurationError(f"Assignment identity mismatch: {path}")
    validate_status(str(value.get("status", "")))
    return value


def _save_assignment(
    assignment_id: str,
    value: Mapping[str, Any],
    paths: RuntimePaths | None = None,
) -> Path:
    path = assignment_path(assignment_id, paths)
    payload = dict(value)
    payload["schema_version"] = SCHEMA_VERSION
    payload["assignment_id"] = assignment_id
    payload["updated_at"] = utc_now()
    atomic_write_json(path, payload)
    return path


def list_assignments(paths: RuntimePaths | None = None) -> list[dict[str, Any]]:
    runtime = paths or default_paths()
    if not runtime.assignments_dir.exists():
        return []
    values: list[dict[str, Any]] = []
    for path in sorted(runtime.assignments_dir.glob("*.json")):
        try:
            value = read_json(path)
            assignment_id = str(value.get("assignment_id", ""))
            validate_identifier(assignment_id, field="assignment_id")
            validate_status(str(value.get("status", "")))
            values.append(value)
        except DispatchError as exc:
            raise StateError(
                "Invalid assignment receipt; refusing to dispatch",
                details={"path": str(path), "error": str(exc)},
            ) from exc
    return sorted(values, key=lambda value: str(value.get("created_at", "")))


def active_assignment(paths: RuntimePaths | None = None) -> dict[str, Any] | None:
    active = [
        value for value in list_assignments(paths) if value.get("status") in ACTIVE_STATUSES
    ]
    if len(active) > 1:
        raise StateError(
            "Multiple active assignments exist",
            details={"assignment_ids": [value.get("assignment_id") for value in active]},
        )
    return active[0] if active else None


def prepare_assignment(
    prompt: str,
    *,
    parent_task_id: str,
    continuation_of: str | None = None,
    assignment_id: str | None = None,
    paths: RuntimePaths | None = None,
) -> PreparedAssignment:
    runtime = paths or default_paths()
    validate_identifier(parent_task_id, field="parent_task_id")
    resolved_id = assignment_id if assignment_id is not None else new_assignment_id()
    validate_identifier(resolved_id, field="assignment_id")

    with state_lock(runtime):
        worker = load_worker(runtime)
        if assignment_path(resolved_id, runtime).exists():
            raise StateError(
                "Assignment ID already exists; refusing a possible duplicate submission",
                details={"assignment_id": resolved_id},
            )
        existing_active = active_assignment(runtime)
        if existing_active:
            raise BusyError(
                "Another dispatch is unresolved",
                details={
                    "assignment_id": existing_active.get("assignment_id"),
                    "status": existing_active.get("status"),
                },
            )

        previous: dict[str, Any] | None = None
        if continuation_of:
            validate_identifier(continuation_of, field="continuation_of")
            previous = load_assignment(continuation_of, runtime)
            if previous.get("status") != "complete":
                raise StateError(
                    "Continuation requires a completed prior assignment",
                    details={
                        "continuation_of": continuation_of,
                        "status": previous.get("status"),
                    },
                )
            if previous.get("worker_conversation_id") != worker.conversation_id:
                raise StateError(
                    "Continuation worker does not match the configured worker",
                    details={"continuation_of": continuation_of},
                )

        wrapped = wrap_prompt(prompt, resolved_id)
        receipt: dict[str, Any] = {
            "status": "prepared",
            "created_at": utc_now(),
            "worker_conversation_id": worker.conversation_id,
            "worker_label": worker.label,
            "worker_model_confirmation": worker.model_confirmation,
            "parent_task_id": parent_task_id,
            "prompt_sha256": sha256_text(normalize_newlines(prompt).strip()),
            "wrapped_prompt_sha256": sha256_text(wrapped),
            "submission_count": 0,
            "response_marker": result_marker(resolved_id),
        }
        if continuation_of:
            receipt["continuation_of"] = continuation_of
        path = _save_assignment(resolved_id, receipt, runtime)

    return PreparedAssignment(
        assignment_id=resolved_id,
        worker_conversation_id=worker.conversation_id,
        parent_task_id=parent_task_id,
        receipt_path=path,
        wrapped_prompt=wrapped,
        continuation_of=continuation_of,
    )


def _transition(
    assignment_id: str,
    *,
    allowed: set[str] | frozenset[str],
    target: str,
    updates: Mapping[str, Any] | None = None,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    runtime = paths or default_paths()
    validate_status(target)
    with state_lock(runtime):
        value = load_assignment(assignment_id, runtime)
        current = str(value["status"])
        if current not in allowed:
            raise StateError(
                f"Cannot move assignment from {current} to {target}",
                details={"assignment_id": assignment_id, "status": current},
            )
        value["status"] = target
        if updates:
            value.update(dict(updates))
        _save_assignment(assignment_id, value, runtime)
        return value


def mark_submitted(
    assignment_id: str,
    sent_prompt: str,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    runtime = paths or default_paths()
    with state_lock(runtime):
        value = load_assignment(assignment_id, runtime)
        current = str(value.get("status"))
        submission_count = int(value.get("submission_count", 0))
        expected_hash = str(value.get("wrapped_prompt_sha256", ""))
        sent_hash = sha256_text(sent_prompt)
        prior_sent_hash = str(value.get("sent_prompt_sha256", ""))
        is_late_verification = (
            current in {"indeterminate", "ambiguous"}
            and submission_count == 0
            and value.get("no_resend") is True
        )
        is_legacy_single_trailing_newline_correction = (
            sent_hash == expected_hash
            and sha256_text(sent_prompt + "\n") == prior_sent_hash
        )
        is_readback_correction = (
            current in {"indeterminate", "ambiguous"}
            and submission_count == 1
            and value.get("no_resend") is True
            and value.get("outbound_prompt_verified") is False
            and (
                value.get("readback_correction_allowed") is True
                or is_legacy_single_trailing_newline_correction
            )
            and sent_hash == expected_hash
        )
        if (
            current != "prepared"
            and not is_late_verification
            and not is_readback_correction
        ):
            raise StateError(
                "Submission may be recorded only once",
                details={
                    "assignment_id": assignment_id,
                    "status": current,
                    "submission_count": submission_count,
                },
            )

        verified_at = utc_now()
        if not is_readback_correction:
            value["submitted_at"] = verified_at
        value["submission_count"] = 1
        value["sent_prompt_sha256"] = sent_hash
        value["no_resend"] = True
        value["readback_verification_attempt_count"] = int(
            value.get(
                "readback_verification_attempt_count",
                1 if is_legacy_single_trailing_newline_correction else 0,
            )
        ) + 1

        if sent_hash != expected_hash:
            is_single_trailing_newline_artifact = (
                sent_prompt.endswith("\n")
                and sha256_text(sent_prompt[:-1]) == expected_hash
            )
            value["status"] = "indeterminate"
            value["outbound_prompt_verified"] = False
            value["submission_may_have_occurred"] = True
            value["readback_artifact_sha256"] = sent_hash
            if is_single_trailing_newline_artifact:
                value["readback_correction_allowed"] = True
                value["readback_correction_kind"] = "single-trailing-newline"
            else:
                value.pop("readback_correction_allowed", None)
                value.pop("readback_correction_kind", None)
            value["last_error"] = (
                "Native read-back prompt does not exactly match the prepared wrapped_prompt"
            )
            _save_assignment(assignment_id, value, runtime)
            raise StateError(
                "Submitted prompt failed exact read-back verification; never resend",
                details={
                    "assignment_id": assignment_id,
                    "status": "indeterminate",
                    "expected_sha256": expected_hash,
                    "actual_sha256": sent_hash,
                    "no_resend": True,
                    "readback_correction_allowed": is_single_trailing_newline_artifact,
                },
            )

        value["status"] = "submitted"
        value["outbound_prompt_verified"] = True
        value["outbound_prompt_verified_at"] = verified_at
        value["submission_observed"] = True
        value.pop("last_error", None)
        value.pop("submission_may_have_occurred", None)
        if is_readback_correction:
            if is_legacy_single_trailing_newline_correction:
                value["readback_artifact_sha256"] = prior_sent_hash
                value["readback_correction_kind"] = "single-trailing-newline"
            value["readback_correction_applied_at"] = verified_at
            value.pop("readback_correction_allowed", None)
        if is_late_verification or is_readback_correction:
            value["submission_recovered_from"] = current
        _save_assignment(assignment_id, value, runtime)
        return value


def mark_pending(
    assignment_id: str, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    return _transition(
        assignment_id,
        allowed={"submitted"},
        target="pending",
        updates={"pending_since": utc_now()},
        paths=paths,
    )


def mark_indeterminate(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    cleaned = reason.strip()
    if not cleaned:
        raise ConfigurationError("Indeterminate reason is empty")
    return _transition(
        assignment_id,
        allowed={"prepared", "submitted", "pending", "ambiguous", "indeterminate"},
        target="indeterminate",
        updates={
            "last_error": cleaned,
            "submission_may_have_occurred": True,
            "no_resend": True,
        },
        paths=paths,
    )


def mark_ambiguous(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    cleaned = reason.strip()
    if not cleaned:
        raise ConfigurationError("Ambiguous reason is empty")
    return _transition(
        assignment_id,
        allowed={"prepared", "submitted", "pending", "indeterminate", "ambiguous"},
        target="ambiguous",
        updates={"last_error": cleaned, "no_resend": True},
        paths=paths,
    )


def abandon_assignment(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    cleaned = reason.strip()
    if not cleaned:
        raise ConfigurationError("Abandon reason is empty")
    return _transition(
        assignment_id,
        allowed=ACTIVE_STATUSES,
        target="abandoned",
        updates={"abandoned_at": utc_now(), "reason": cleaned},
        paths=paths,
    )


def complete_assignment(
    assignment_id: str,
    response: str,
    paths: RuntimePaths | None = None,
) -> tuple[dict[str, Any], str]:
    runtime = paths or default_paths()
    normalized, payload = parse_result(response, assignment_id)
    response_hash = sha256_text(normalized)
    payload_hash = sha256_text(payload)

    with state_lock(runtime):
        value = load_assignment(assignment_id, runtime)
        current = str(value["status"])
        if current == "complete":
            if value.get("response_sha256") != response_hash:
                raise StateError(
                    "Completed assignment is immutable and the new response differs",
                    details={"assignment_id": assignment_id},
                )
            return value, payload
        if current in {"abandoned", "failed"}:
            raise StateError(
                f"Cannot complete an assignment in terminal state {current}",
                details={"assignment_id": assignment_id},
            )
        value["status"] = "complete"
        value["completed_at"] = utc_now()
        value["response_sha256"] = response_hash
        value["payload_sha256"] = payload_hash
        value["result_marker_validated"] = True
        value["no_resend"] = True
        _save_assignment(assignment_id, value, runtime)
        return value, payload


def recovery_info(
    assignment_id: str, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    value = load_assignment(assignment_id, paths)
    return {
        "assignment_id": assignment_id,
        "status": value["status"],
        "worker_conversation_id": value["worker_conversation_id"],
        "parent_task_id": value["parent_task_id"],
        "response_marker": value["response_marker"],
        "submission_count": value.get("submission_count", 0),
        "no_resend": value.get("status") != "prepared" or value.get("submission_count", 0) > 0,
        "continuation_of": value.get("continuation_of"),
        "last_error": value.get("last_error"),
    }


def reset_worker(
    *, force: bool = False, paths: RuntimePaths | None = None
) -> bool:
    runtime = paths or default_paths()
    with state_lock(runtime):
        current = active_assignment(runtime)
        if current and not force:
            raise BusyError(
                "Cannot reset the worker while an assignment is unresolved",
                details={
                    "assignment_id": current.get("assignment_id"),
                    "status": current.get("status"),
                },
            )
        try:
            runtime.worker_file.unlink()
            return True
        except FileNotFoundError:
            return False


def purge_local_state(
    *, force: bool = False, paths: RuntimePaths | None = None
) -> dict[str, bool]:
    runtime = paths or default_paths()
    with state_lock(runtime):
        current = active_assignment(runtime)
        if current and not force:
            raise BusyError(
                "Cannot purge local state while an assignment is unresolved",
                details={
                    "assignment_id": current.get("assignment_id"),
                    "status": current.get("status"),
                },
            )
        worker_removed = False
        assignments_removed = False
        with contextlib.suppress(FileNotFoundError):
            runtime.worker_file.unlink()
            worker_removed = True
        if runtime.assignments_dir.exists():
            for path in runtime.assignments_dir.glob("*.json"):
                path.unlink()
            with contextlib.suppress(OSError):
                runtime.assignments_dir.rmdir()
            assignments_removed = True
        return {
            "worker_removed": worker_removed,
            "assignments_removed": assignments_removed,
        }
