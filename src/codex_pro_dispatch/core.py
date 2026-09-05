from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .collection import NativeCollectionEvidence
from .errors import (
    BusyError,
    CollectionEvidenceError,
    ConfigurationError,
    CooldownError,
    DispatchError,
    MarkerError,
    StateError,
    TruncationError,
)

APP_NAME = "codex-pro-dispatch"
# Worker configuration deliberately remains v1.  Dispatch receipts have their
# own lifecycle and advance independently in the long-result transport.
WORKER_SCHEMA_VERSION = 1
ASSIGNMENT_SCHEMA_VERSION = 2
# Deprecated source-compatible name for the logical dispatch receipt.  Worker
# configuration deliberately has its own named v1 constant; new code must not
# use this alias for worker data.
SCHEMA_VERSION = ASSIGNMENT_SCHEMA_VERSION

ACTIVE_STATUSES = frozenset(
    {"prepared", "armed", "submitted", "pending", "indeterminate", "ambiguous"}
)
TERMINAL_STATUSES = frozenset({"complete", "abandoned", "failed"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
RESULT_MARKER_PREFIX = "[CODEX_PRO_DISPATCH_RESULT assignment_id="
UNUSUAL_ACTIVITY_COOLDOWN_SECONDS = 30 * 60


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

    @property
    def spool_dir(self) -> Path:
        return self.state_dir / "spool"

    @property
    def results_dir(self) -> Path:
        return self.state_dir / "results"


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
    turn_id: str | None = None
    result_mode: str = "inline"
    # Turn order is explicit so a host cannot infer recovery ownership from an
    # opaque identifier or accidentally re-arm the original assignment.
    sequence: int = 1


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


def _format_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def utc_now() -> str:
    return _format_utc(dt.datetime.now(dt.timezone.utc))


def _parse_utc(value: str, *, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"Invalid UTC timestamp in {field}", details={field: value}
        ) from exc
    if parsed.tzinfo is None:
        raise ConfigurationError(
            f"UTC timestamp in {field} must include a timezone", details={field: value}
        )
    return parsed.astimezone(dt.timezone.utc)


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


def validate_native_message_id(value: str, *, field: str) -> str:
    """Validate an opaque host ID without pretending it has our assignment grammar."""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
        raise ConfigurationError(f"{field} must be a nonempty bounded native ID")
    if any(ord(character) < 32 for character in value):
        raise ConfigurationError(f"{field} must not contain control characters")
    return value


def validate_status(value: str) -> str:
    if value not in ALL_STATUSES:
        raise ConfigurationError("Invalid assignment status", details={"status": value})
    return value


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_diagnostic_fields(value: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Replace legacy raw diagnostic bodies with categories and hashes."""
    redacted = dict(value)
    changed = False
    for raw_field, kind_field, hash_field, fallback_kind in (
        (
            "last_error",
            "last_error_kind",
            "last_error_sha256",
            "legacy-diagnostic-redacted",
        ),
        (
            "reason",
            "abandon_reason_kind",
            "abandon_reason_sha256",
            "legacy-abandon-reason-redacted",
        ),
    ):
        if raw_field not in redacted:
            continue
        cleaned = str(redacted.pop(raw_field)).strip()
        changed = True
        if cleaned:
            redacted.setdefault(kind_field, fallback_kind)
            redacted.setdefault(hash_field, sha256_text(cleaned))
    return redacted, changed


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
                "schema_version": WORKER_SCHEMA_VERSION,
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
    if value.get("schema_version") != WORKER_SCHEMA_VERSION:
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
    redacted, _ = _redact_diagnostic_fields(value)
    return redacted


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
            redacted, _ = _redact_diagnostic_fields(value)
            values.append(redacted)
        except DispatchError as exc:
            raise StateError(
                "Invalid assignment receipt; refusing to dispatch",
                details={"path": str(path), "error": str(exc)},
            ) from exc
    return sorted(values, key=lambda value: str(value.get("created_at", "")))


def redact_stored_diagnostics(paths: RuntimePaths | None = None) -> int:
    """Durably remove raw diagnostic bodies written by releases before v1.1."""
    runtime = paths or default_paths()
    redacted_count = 0
    with state_lock(runtime):
        if not runtime.assignments_dir.exists():
            return 0
        for path in sorted(runtime.assignments_dir.glob("*.json")):
            value = read_json(path)
            assignment_id = str(value.get("assignment_id", ""))
            validate_identifier(assignment_id, field="assignment_id")
            validate_status(str(value.get("status", "")))
            if path != assignment_path(assignment_id, runtime):
                raise StateError(
                    "Assignment identity mismatch during diagnostic redaction",
                    details={"path": str(path), "assignment_id": assignment_id},
                )
            redacted, changed = _redact_diagnostic_fields(value)
            if changed:
                atomic_write_json(path, redacted)
                redacted_count += 1
    return redacted_count


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


def active_cooldown(
    paths: RuntimePaths | None = None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Return the latest unexpired native unusual-activity cooldown."""
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ConfigurationError("Cooldown comparison time must include a timezone")
    current = current.astimezone(dt.timezone.utc)
    active: list[tuple[dt.datetime, dict[str, Any]]] = []
    for value in list_assignments(paths):
        cooldown_until = value.get("cooldown_until")
        if not cooldown_until:
            continue
        try:
            parsed_until = _parse_utc(
                str(cooldown_until), field="cooldown_until"
            )
        except DispatchError as exc:
            raise StateError(
                "Invalid cooldown receipt; refusing to dispatch",
                details={
                    "assignment_id": value.get("assignment_id"),
                    "error": str(exc),
                },
            ) from exc
        if parsed_until > current:
            active.append((parsed_until, value))

    if not active:
        return None

    parsed_until, value = max(active, key=lambda item: item[0])
    result: dict[str, Any] = {
        "assignment_id": value.get("assignment_id"),
        "native_http_status": value.get("native_http_status"),
        "native_error_kind": value.get("native_error_kind"),
        "cooldown_seconds": value.get("cooldown_seconds"),
        "cooldown_started_at": value.get("cooldown_started_at"),
        "cooldown_until": value.get("cooldown_until"),
        "retry_after_seconds": max(
            1, math.ceil((parsed_until - current).total_seconds())
        ),
    }
    if value.get("openai_request_id"):
        result["openai_request_id"] = value["openai_request_id"]
    return result


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
        cooldown = active_cooldown(runtime)
        if cooldown:
            raise CooldownError(
                "Native ChatGPT HTTP 403 cooldown is still active",
                details=cooldown,
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


def arm_assignment(
    assignment_id: str, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    """Durably prohibit resends immediately before the one native send attempt."""
    return _transition(
        assignment_id,
        allowed={"prepared"},
        target="armed",
        updates={"armed_at": utc_now(), "no_resend": True},
        paths=paths,
    )


def mark_submitted(
    assignment_id: str,
    sent_prompt: str,
    paths: RuntimePaths | None = None,
    *,
    native_user_message_id: str | None = None,
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
            current != "armed"
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
        if native_user_message_id is not None:
            value["native_user_message_id"] = validate_native_message_id(
                native_user_message_id, field="native_user_message_id"
            )
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
            value["last_error_kind"] = "native-readback-mismatch"
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
        value.pop("last_error_kind", None)
        value.pop("last_error_sha256", None)
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
        allowed={"armed", "submitted", "pending", "ambiguous", "indeterminate"},
        target="indeterminate",
        updates={
            "last_error_kind": "native-send-indeterminate",
            "last_error_sha256": sha256_text(cleaned),
            "submission_may_have_occurred": True,
            "no_resend": True,
        },
        paths=paths,
    )


def mark_unusual_activity_403(
    assignment_id: str,
    *,
    reason: str,
    request_id: str | None = None,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    """Record a native unusual-activity HTTP 403 and start a fixed cooldown."""
    cleaned = reason.strip()
    if not cleaned:
        raise ConfigurationError("HTTP 403 reason is empty")
    cleaned_request_id: str | None = None
    if request_id is not None:
        cleaned_request_id = validate_identifier(
            request_id.strip(), field="openai_request_id"
        )
    runtime = paths or default_paths()
    allowed = {"armed", "submitted", "pending", "ambiguous", "indeterminate"}
    with state_lock(runtime):
        value = load_assignment(assignment_id, runtime)
        current = str(value["status"])
        if current not in allowed:
            raise StateError(
                f"Cannot record HTTP 403 from assignment state {current}",
                details={"assignment_id": assignment_id, "status": current},
            )

        if value.get("native_error_kind") == "openai-unusual-activity":
            if cleaned_request_id and not value.get("openai_request_id"):
                value["openai_request_id"] = cleaned_request_id
                _save_assignment(assignment_id, value, runtime)
            return value

        started = dt.datetime.now(dt.timezone.utc)
        value.update(
            {
                "status": "indeterminate",
                "last_error_kind": "openai-unusual-activity",
                "last_error_sha256": sha256_text(cleaned),
                "submission_may_have_occurred": True,
                "no_resend": True,
                "native_http_status": 403,
                "native_error_kind": "openai-unusual-activity",
                "cooldown_seconds": UNUSUAL_ACTIVITY_COOLDOWN_SECONDS,
                "cooldown_started_at": _format_utc(started),
                "cooldown_until": _format_utc(
                    started
                    + dt.timedelta(seconds=UNUSUAL_ACTIVITY_COOLDOWN_SECONDS)
                ),
            }
        )
        if cleaned_request_id:
            value["openai_request_id"] = cleaned_request_id
        _save_assignment(assignment_id, value, runtime)
        return value


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
        allowed={"armed", "submitted", "pending", "indeterminate", "ambiguous"},
        target="ambiguous",
        updates={
            "last_error_kind": "response-ambiguous",
            "last_error_sha256": sha256_text(cleaned),
            "no_resend": True,
        },
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
        updates={
            "abandoned_at": utc_now(),
            "abandon_reason_kind": "user-authorized",
            "abandon_reason_sha256": sha256_text(cleaned),
        },
        paths=paths,
    )


def complete_assignment(
    assignment_id: str,
    response: str | None = None,
    paths: RuntimePaths | None = None,
    *,
    evidence: NativeCollectionEvidence | None = None,
) -> tuple[dict[str, Any], str]:
    """Complete a v1 receipt only from one trusted native evidence envelope.

    ``response`` remains a compatibility argument so callers receive a clear
    fail-closed error instead of accidentally treating a body file as proof of a
    complete native read.  It is never sufficient by itself.
    """
    runtime = paths or default_paths()
    if evidence is None:
        raise CollectionEvidenceError(
            "Response-only completion is disabled; native collection evidence is required",
            details={"assignment_id": assignment_id},
            error_code="collection_evidence_required",
        )
    if response is not None and normalize_newlines(response) != evidence.text:
        raise CollectionEvidenceError(
            "Response file does not exactly match the native collection evidence",
            details={"assignment_id": assignment_id},
            error_code="collection_evidence_conflict",
        )
    normalized, payload = parse_result(evidence.text, assignment_id)
    response_hash = sha256_text(normalized)
    payload_hash = sha256_text(payload)

    with state_lock(runtime):
        value = load_assignment(assignment_id, runtime)
        current = str(value["status"])
        worker_id = str(value.get("worker_conversation_id", ""))
        if (
            evidence.requested_conversation_id != worker_id
            or evidence.loaded_conversation_id != worker_id
        ):
            raise CollectionEvidenceError(
                "Native collection evidence is for a different worker",
                details={"assignment_id": assignment_id},
                error_code="collection_wrong_worker",
            )
        submitted_message_id = value.get("native_user_message_id")
        if not submitted_message_id or evidence.submitted_user_message_id != submitted_message_id:
            raise CollectionEvidenceError(
                "Native collection evidence is not associated with the verified submitted message",
                details={"assignment_id": assignment_id},
                error_code="collection_submission_mismatch",
            )
        if not evidence.has_known_truncation:
            raise CollectionEvidenceError(
                "Native collection truncation is unknown for this adapter",
                details={"assignment_id": assignment_id},
                error_code="collection_truncation_unknown",
            )
        evidence_fields = evidence.receipt_fields()
        if not evidence.complete_and_untruncated:
            value.update(evidence_fields)
            value["status"] = "ambiguous"
            value["no_resend"] = True
            _save_assignment(assignment_id, value, runtime)
            raise TruncationError(
                "Native collection was truncated and cannot complete",
                details={"assignment_id": assignment_id, "no_resend": True},
                error_code="collection_truncated",
            )
        if current == "complete":
            if (
                value.get("response_sha256") != response_hash
                or value.get("assistant_message_id") != evidence.assistant_message_id
                or value.get("submitted_user_message_id") != evidence.submitted_user_message_id
            ):
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
        if (
            int(value.get("submission_count", 0)) != 1
            or value.get("outbound_prompt_verified") is not True
        ):
            raise StateError(
                "Cannot complete before one exact outbound submission is verified",
                details={
                    "assignment_id": assignment_id,
                    "status": current,
                    "submission_count": value.get("submission_count", 0),
                    "outbound_prompt_verified": value.get(
                        "outbound_prompt_verified", False
                    ),
                },
            )
        value["status"] = "complete"
        value["completed_at"] = utc_now()
        value["response_sha256"] = response_hash
        value["payload_sha256"] = payload_hash
        value.update(evidence_fields)
        value["result_marker_validated"] = True
        value["no_resend"] = True
        _save_assignment(assignment_id, value, runtime)
        return value, payload


def recovery_info(
    assignment_id: str, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    value = load_assignment(assignment_id, paths)
    recovery = {
        "assignment_id": assignment_id,
        "status": value["status"],
        "worker_conversation_id": value["worker_conversation_id"],
        "parent_task_id": value["parent_task_id"],
        "response_marker": value["response_marker"],
        "submission_count": value.get("submission_count", 0),
        "no_resend": bool(value.get("no_resend"))
        or value.get("status") != "prepared"
        or value.get("submission_count", 0) > 0,
        "outbound_prompt_verified": value.get("outbound_prompt_verified", False),
        "readback_correction_allowed": value.get(
            "readback_correction_allowed", False
        ),
        "readback_correction_kind": value.get("readback_correction_kind"),
        "wrapped_prompt_sha256": value.get("wrapped_prompt_sha256"),
        "sent_prompt_sha256": value.get("sent_prompt_sha256"),
        "readback_artifact_sha256": value.get("readback_artifact_sha256"),
        "continuation_of": value.get("continuation_of"),
        "last_error_kind": value.get("last_error_kind"),
        "last_error_sha256": value.get("last_error_sha256"),
    }
    for field in (
        "native_http_status",
        "native_error_kind",
        "openai_request_id",
        "cooldown_seconds",
        "cooldown_started_at",
        "cooldown_until",
    ):
        if value.get(field) is not None:
            recovery[field] = value[field]
    cooldown = active_cooldown(paths)
    if cooldown and cooldown.get("assignment_id") == assignment_id:
        recovery["active_cooldown"] = cooldown
    return recovery


def reset_worker(
    *, force: bool = False, paths: RuntimePaths | None = None
) -> bool:
    runtime = paths or default_paths()
    with state_lock(runtime):
        if not force:
            current = active_assignment(runtime)
            if current:
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
        if not force:
            current = active_assignment(runtime)
            if current:
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


# Schema-v2 logical dispatch API.  The imports are intentionally at end of this
# module: transport reuses the established private-file and worker helpers above,
# while public callers receive only the v2 state machine.
from .transport import (  # noqa: E402
    DISPATCH_ACTIVE_STATUSES,
    DISPATCH_STATUSES,
    CleanupResult,
    CollectionOutcome,
    ResultDescriptor,
    abandon_assignment_v2,
    active_assignment_v2,
    active_cooldown_v2,
    arm_assignment_v2,
    cleanup_result_v2,
    collect_turn_v2,
    complete_assignment_v2,
    list_assignments_v2,
    load_assignment_v2,
    mark_ambiguous_v2,
    mark_indeterminate_v2,
    mark_pending_v2,
    mark_submitted_v2,
    mark_unusual_activity_403_v2,
    materialize_result_v2,
    prepare_assignment_v2,
    purge_local_state_v2,
    record_parent_restoration_v2,
    recovery_info_v2,
    redact_stored_diagnostics_v2,
    verify_artifact_v2,
)

ACTIVE_STATUSES = DISPATCH_ACTIVE_STATUSES
ALL_STATUSES = DISPATCH_STATUSES
prepare_assignment = prepare_assignment_v2
load_assignment = load_assignment_v2
list_assignments = list_assignments_v2
active_assignment = active_assignment_v2
active_cooldown = active_cooldown_v2
arm_assignment = arm_assignment_v2
mark_submitted = mark_submitted_v2
mark_pending = mark_pending_v2
mark_indeterminate = mark_indeterminate_v2
mark_ambiguous = mark_ambiguous_v2
mark_unusual_activity_403 = mark_unusual_activity_403_v2
abandon_assignment = abandon_assignment_v2
complete_assignment = complete_assignment_v2
recovery_info = recovery_info_v2
redact_stored_diagnostics = redact_stored_diagnostics_v2
collect_turn = collect_turn_v2
verify_artifact = verify_artifact_v2
materialize_result = materialize_result_v2
cleanup_result = cleanup_result_v2
record_parent_restoration = record_parent_restoration_v2
purge_local_state = purge_local_state_v2
