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

APP_NAME = "codex-pro-dispatch"
SCHEMA_VERSION = 1

ACTIVE_STATUSES = frozenset(
    {"prepared", "armed", "submitted", "pending", "indeterminate", "ambiguous"}
)
TERMINAL_STATUSES = frozenset({"complete", "abandoned", "failed"})
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

IDENTIFIER_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
IDENTIFIER_PATTERN = re.compile(rf"^{IDENTIFIER_TOKEN}$")
RESULT_MARKER_PREFIX = "[CODEX_PRO_DISPATCH_RESULT assignment_id="
END_MARKER_PREFIX = "[CODEX_PRO_DISPATCH_END assignment_id="
CONTINUATION_REQUIRED_PREFIX = "[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED "
CHUNK_PREFIX = "[CODEX_PRO_DISPATCH_CHUNK "
BOUNDED_RESULT_PROTOCOL = "bounded-footer-v1"
RESPONSE_GUIDELINE_BYTES = 10_000
MAX_CHUNKS = 16
CHUNK_INDEX_PATTERN = re.compile(r"^(?:[1-9]|1[0-6])$")
CONTINUE_PROMPT_PATTERN = re.compile(
    rf"^\[CODEX_PRO_DISPATCH_CONTINUE root_assignment_id=(?P<root>{IDENTIFIER_TOKEN}) "
    r"next_index=(?P<index>[0-9]+)\]$"
)
CONTROL_LINE_PATTERN = re.compile(
    rf"^\[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED root_assignment_id=(?P<root>{IDENTIFIER_TOKEN})\]$"
)
CHUNK_LINE_PATTERN = re.compile(
    rf"^\[CODEX_PRO_DISPATCH_CHUNK root_assignment_id=(?P<root>{IDENTIFIER_TOKEN}) "
    r"index=(?P<index>[0-9]+) final=(?P<final>[^\]]*)\]$"
)
UNUSUAL_ACTIVITY_COOLDOWN_SECONDS = 30 * 60


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


class CooldownError(DispatchError):
    exit_code = 6


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


@dataclass(frozen=True)
class ParsedResult:
    """One validated bounded response envelope."""

    response: str
    payload: str
    result_kind: str
    root_assignment_id: str | None = None
    chunk_index: int | None = None
    final: int | None = None


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
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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


def end_marker(assignment_id: str) -> str:
    validate_identifier(assignment_id, field="assignment_id")
    return f"{END_MARKER_PREFIX}{assignment_id}]"


def _continuation_required_marker(root_assignment_id: str) -> str:
    validate_identifier(root_assignment_id, field="root_assignment_id")
    return (
        "[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED "
        f"root_assignment_id={root_assignment_id}]"
    )


def _chunk_header(root_assignment_id: str, index: int, final: int) -> str:
    validate_identifier(root_assignment_id, field="root_assignment_id")
    if index < 1 or index > MAX_CHUNKS:
        raise ConfigurationError(
            f"chunk index must be between 1 and {MAX_CHUNKS}",
            details={"chunk_index": index},
        )
    if final not in {0, 1}:
        raise ConfigurationError("chunk final must be 0 or 1", details={"final": final})
    return (
        "[CODEX_PRO_DISPATCH_CHUNK "
        f"root_assignment_id={root_assignment_id} index={index} final={final}]"
    )


def _canonical_chunk_index(value: int | str, *, field: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{field} must be a canonical decimal from 1 to {MAX_CHUNKS}")
    text = str(value)
    if not CHUNK_INDEX_PATTERN.fullmatch(text):
        raise ConfigurationError(
            f"{field} must be a canonical decimal from 1 to {MAX_CHUNKS}",
            details={field: value},
        )
    return int(text)


def _continuation_prompt_at_byte_zero(prompt: str) -> tuple[str, int] | None:
    first_line = prompt.split("\n", 1)[0]
    match = CONTINUE_PROMPT_PATTERN.fullmatch(first_line)
    if not match:
        return None
    try:
        index = _canonical_chunk_index(match.group("index"), field="next_index")
    except ConfigurationError:
        return None
    return match.group("root"), index


def wrap_prompt(prompt: str, assignment_id: str) -> str:
    normalized = normalize_newlines(prompt)
    cleaned = normalized.strip()
    if not cleaned:
        raise ConfigurationError("Prompt is empty")
    marker = result_marker(assignment_id)
    footer = end_marker(assignment_id)
    continuation = _continuation_prompt_at_byte_zero(normalized)
    shared = (
        "Response limits and framing:\n"
        f"1. Aim to keep the entire assistant response below {RESPONSE_GUIDELINE_BYTES} UTF-8 bytes.\n"
        "2. Target no more than 6,000 characters of body text.\n"
        f"3. Begin at byte zero with this exact line: {marker}\n"
        f"4. End with this exact final line and no byte after it: {footer}\n"
        "5. Use only tools actually available inside this Chat conversation.\n"
        "6. Do not claim a repository write, command, test, or deployment unless it actually occurred.\n"
        "7. Keep this assignment ID in context for any follow-up in this worker thread.\n"
    )
    if continuation is not None:
        root_assignment_id, index = continuation
        response_form = (
            "Return only this chunk response form:\n"
            f"{marker}\n"
            f"{_chunk_header(root_assignment_id, index, 0)}\n"
            "<nonempty chunk body unless final=1 after an earlier nonempty chunk>\n"
            f"{footer}\n"
            "Use the same root and index shown in the user message. Set final=1 only "
            "when this chunk completes the deliverable; otherwise set final=0."
        )
    else:
        response_form = (
            "Return only one of these response forms:\n"
            "- A nonempty complete result between the supplied result marker and end marker.\n"
            "- This exact no-body continuation-required control response when the "
            "complete deliverable cannot fit safely:\n"
            f"{marker}\n"
            f"{_continuation_required_marker(assignment_id)}\n"
            f"{footer}\n"
            "Do not return any other response form."
        )
    return (
        f"{prompt_marker(assignment_id)}\n\n"
        f"{cleaned}\n\n"
        f"{shared}\n"
        f"{response_form}"
    )


def _response_bytes(response: str | bytes) -> bytes:
    if isinstance(response, bytes):
        return response
    if isinstance(response, str):
        try:
            return response.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise MarkerError("Worker response is not valid UTF-8") from exc
    raise ConfigurationError("Worker response must be text or bytes")


def _parse_result(
    response: str | bytes,
    assignment_id: str,
    *,
    expected_root_assignment_id: str | None = None,
    expected_chunk_index: int | str | None = None,
    truncated: bool | None = None,
) -> ParsedResult:
    paired_expectations = (
        expected_root_assignment_id is not None,
        expected_chunk_index is not None,
    )
    if paired_expectations[0] != paired_expectations[1]:
        raise ConfigurationError(
            "expected-root-assignment-id and expected-chunk-index must be supplied together"
        )
    if truncated is not None and not isinstance(truncated, bool):
        raise ConfigurationError("truncated must be true, false, or omitted")
    if truncated is True:
        raise MarkerError(
            "truncated-response: native reader reported truncated: true",
            details={"assignment_id": assignment_id},
        )

    expected_root: str | None = None
    expected_index: int | None = None
    if paired_expectations[0]:
        assert expected_root_assignment_id is not None
        assert expected_chunk_index is not None
        expected_root = validate_identifier(
            expected_root_assignment_id, field="expected_root_assignment_id"
        )
        expected_index = _canonical_chunk_index(
            expected_chunk_index, field="expected_chunk_index"
        )

    raw = _response_bytes(response)
    if b"\r" in raw:
        raise MarkerError("response-cr-byte: response contains a CR byte")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarkerError("response-invalid-utf8: worker response is not valid UTF-8") from exc

    marker = result_marker(assignment_id).encode("ascii")
    footer = end_marker(assignment_id).encode("ascii")
    prefix = marker + b"\n"
    suffix = b"\n" + footer
    if not raw.startswith(prefix):
        raise MarkerError(
            "response-marker-not-at-byte-zero: worker response does not begin with the expected result marker",
            details={"assignment_id": assignment_id},
        )
    if not raw.endswith(suffix):
        raise MarkerError(
            "response-footer-missing-or-not-final: worker response must end with the exact end marker",
            details={"assignment_id": assignment_id},
        )

    interior = raw[len(prefix) : len(raw) - len(suffix)]
    first_body_line, has_body_separator, remaining_body = interior.partition(b"\n")

    if expected_root is None:
        if first_body_line.startswith(CONTINUATION_REQUIRED_PREFIX.encode("ascii")):
            try:
                control_line = first_body_line.decode("ascii")
            except UnicodeDecodeError as exc:
                raise MarkerError("continuation-required-control-invalid") from exc
            control_match = CONTROL_LINE_PATTERN.fullmatch(control_line)
            if control_match and control_match.group("root") != assignment_id:
                raise MarkerError(
                    "control-root-mismatch: continuation control root does not match the assignment",
                    details={"assignment_id": assignment_id},
                )
            if interior != _continuation_required_marker(assignment_id).encode("ascii"):
                raise MarkerError("continuation-required-control-invalid")
            return ParsedResult(
                response=decoded,
                payload="",
                result_kind="continuation_required",
                root_assignment_id=assignment_id,
            )
        if first_body_line.startswith(CHUNK_PREFIX.encode("ascii")):
            raise MarkerError("chunk-arguments-required")
        if not interior:
            raise MarkerError("short-result-body-empty")
        return ParsedResult(response=decoded, payload=interior.decode("utf-8"), result_kind="short")

    if not first_body_line.startswith(CHUNK_PREFIX.encode("ascii")):
        raise MarkerError("chunk-envelope-required")
    try:
        header_line = first_body_line.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MarkerError("chunk-header-invalid") from exc
    header_match = CHUNK_LINE_PATTERN.fullmatch(header_line)
    if not header_match:
        raise MarkerError("chunk-header-invalid")
    root_assignment_id = header_match.group("root")
    if root_assignment_id != expected_root:
        raise MarkerError(
            "chunk-root-mismatch",
            details={"expected_root_assignment_id": expected_root},
        )
    try:
        chunk_index = _canonical_chunk_index(
            header_match.group("index"), field="chunk_index"
        )
    except ConfigurationError as exc:
        raise MarkerError("chunk-index-invalid") from exc
    if chunk_index != expected_index:
        raise MarkerError(
            "chunk-index-mismatch",
            details={"expected_chunk_index": expected_index},
        )
    final_text = header_match.group("final")
    if final_text not in {"0", "1"}:
        raise MarkerError("chunk-final-invalid")
    if not has_body_separator:
        raise MarkerError("chunk-body-missing")
    final = int(final_text)
    if not remaining_body and (final == 0 or expected_index == 1):
        raise MarkerError("chunk-body-empty")
    return ParsedResult(
        response=decoded,
        payload=remaining_body.decode("utf-8"),
        result_kind="chunk",
        root_assignment_id=root_assignment_id,
        chunk_index=chunk_index,
        final=final,
    )


def parse_result(
    response: str | bytes,
    assignment_id: str,
    *,
    expected_root_assignment_id: str | None = None,
    expected_chunk_index: int | str | None = None,
    truncated: bool | None = None,
) -> tuple[str, str]:
    parsed = _parse_result(
        response,
        assignment_id,
        expected_root_assignment_id=expected_root_assignment_id,
        expected_chunk_index=expected_chunk_index,
        truncated=truncated,
    )
    return parsed.response, parsed.payload


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
            if value.get("schema_version") != SCHEMA_VERSION or path != assignment_path(assignment_id, runtime):
                raise StateError("Unsupported or misplaced assignment receipt")
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


def _reject_legacy_active_assignment(
    value: Mapping[str, Any], *, operation: str
) -> None:
    if (
        value.get("status") in ACTIVE_STATUSES
        and value.get("result_protocol") != BOUNDED_RESULT_PROTOCOL
    ):
        raise StateError(
            "legacy-active-assignment: v1.2 may only inspect, recover, or abandon "
            "an active v1.1 receipt",
            details={
                "assignment_id": value.get("assignment_id"),
                "status": value.get("status"),
                "operation": operation,
            },
        )


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
            _reject_legacy_active_assignment(existing_active, operation="prepare")
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
        if len(wrapped.encode("utf-16-le")) // 2 >= 20000:
            raise ConfigurationError("Wrapped prompt reaches the native read limit; use a smaller prompt or a pinned repository reference")
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
            "result_protocol": BOUNDED_RESULT_PROTOCOL,
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
        if target != "abandoned":
            _reject_legacy_active_assignment(value, operation=target)
        if current not in allowed:
            raise StateError(
                f"Cannot move assignment from {current} to {target}",
                details={"assignment_id": assignment_id, "status": current},
            )
        if target == "armed":
            active_assignment(runtime)  # Reject multiple unresolved assignments.
            if active_cooldown(runtime):
                raise CooldownError("Native unusual-activity cooldown blocks arming")
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
) -> dict[str, Any]:
    runtime = paths or default_paths()
    with state_lock(runtime):
        value = load_assignment(assignment_id, runtime)
        _reject_legacy_active_assignment(value, operation="submitted")
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

        readback_marker = re.match(
            rf"\[CODEX_PRO_DISPATCH assignment_id=({IDENTIFIER_TOKEN})\]\n",
            sent_prompt,
        )
        if readback_marker and readback_marker.group(1) != assignment_id:
            raise StateError(
                "Read-back belongs to another assignment; wait for the current message, never resend",
                details={
                    "assignment_id": assignment_id,
                    "status": current,
                    "no_resend": True,
                    "reason": "stale-readback",
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
        _reject_legacy_active_assignment(value, operation="unusual-activity")
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


def _native_response(raw: bytes, receipt: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Select a framed exchange from the desktop's lossy history summary.

    This establishes summary association, not source-byte integrity or native
    generation finality. Do not manufacture production evidence from it.
    """
    def invalid() -> None:
        raise MarkerError("native-read-invalid: unsupported or ambiguous native summary")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                invalid()
            result[key] = value
        return result

    def native_id(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
            invalid()
        return value

    try:
        if not raw or len(raw) > 4 * 1024 * 1024 or raw.startswith(b"\xef\xbb\xbf"):
            invalid()
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: invalid())
        # Catch escaped lone surrogates and numeric overflow without printing input.
        json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
            invalid()
        thread = data.get("thread")
        if not isinstance(thread, dict) or thread.get("kind") != "chatgpt" or thread.get("id") != receipt["worker_conversation_id"]:
            invalid()
        status = thread.get("status")
        if not isinstance(status, dict) or not isinstance(status.get("type"), str):
            invalid()
        if status["type"] != "idle":
            raise StateError("native-worker-not-idle: wait and read again; never resend")
        turns = data.get("turns")
        if not isinstance(turns, list):
            invalid()
        turn_ids: set[str] = set()
        item_ids: set[str] = set()
        candidates = []
        marker = f'[CODEX_PRO_DISPATCH assignment_id={receipt["assignment_id"]}]\n'
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
                invalid()
            tid = native_id(turn.get("id"))
            if tid in turn_ids:
                invalid()
            turn_ids.add(tid)
            for item in turn["items"]:
                if not isinstance(item, dict):
                    invalid()
                iid = native_id(item.get("id"))
                if iid in item_ids:
                    invalid()
                item_ids.add(iid)
                if item.get("type") == "userMessage":
                    content = item.get("content")
                    if isinstance(content, list) and any(isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].startswith(marker) for c in content):
                        candidates.append((turn, item))
        if not candidates:
            raise StateError("native-reply-not-observed: read existing history; never resend")
        if len(candidates) != 1:
            invalid()
        turn, user = candidates[0]
        content = user.get("content")
        if len(content) != 1 or content[0].get("type") != "text" or user["id"] != turn["id"] or turn["items"][0] is not user:
            invalid()
        if sha256_text(content[0]["text"]) != receipt["wrapped_prompt_sha256"]:
            raise StateError("native-readback-mismatch: returned prompt differs; never resend")
        if len(turn["items"]) == 1:
            raise StateError("native-reply-not-observed: assistant absent; never resend")
        if len(turn["items"]) != 2:
            invalid()
        assistant = turn["items"][1]
        if assistant.get("type") != "agentMessage" or not isinstance(assistant.get("text"), str):
            invalid()
        flags = {}
        for name, scope in (("envelope", data), ("thread", thread), ("turn", turn), ("user", user), ("user_text", content[0]), ("assistant", assistant)):
            for key in ("truncated", "textTruncated"):
                value = scope.get(key)
                if key in scope and type(value) is not bool:
                    invalid()
                if value is True:
                    raise MarkerError("truncated-response: native summary reports shortening")
                flags[f"{name}.{key}"] = value  # null means omitted, never false.
        text = assistant["text"]
        if len(text.encode("utf-16-le")) // 2 >= 20000:
            raise MarkerError("native-read-limit: reject a response at the reader's boundary")
        return text.encode("utf-8"), {
            "worker_id": thread["id"], "turn_id": turn["id"],
            "user_message_id": user["id"], "assistant_message_id": assistant["id"],
            "read_sha256": hashlib.sha256(raw).hexdigest(), "raw_truncation": flags,
        }
    except (ValueError, UnicodeError, RecursionError, TypeError, KeyError, IndexError):
        invalid()


def complete_assignment(
    assignment_id: str,
    response: str | bytes,
    paths: RuntimePaths | None = None,
    *,
    expected_root_assignment_id: str | None = None,
    expected_chunk_index: int | str | None = None,
    truncated: bool | None = None,
    native_read: bytes | None = None,
) -> tuple[dict[str, Any], str]:
    runtime = paths or default_paths()
    raw = _response_bytes(response)

    with state_lock(runtime):
        value = load_assignment(assignment_id, runtime)
        current = str(value["status"])
        _reject_legacy_active_assignment(value, operation="complete")
        native_collection = None
        if native_read is not None:
            if raw:
                raise ConfigurationError("Supply a native read or response, not both")
            raw, native_collection = _native_response(native_read, value)
        parsed = _parse_result(
            raw,
            assignment_id,
            expected_root_assignment_id=expected_root_assignment_id,
            expected_chunk_index=expected_chunk_index,
            truncated=truncated,
        )
        response_hash = hashlib.sha256(raw).hexdigest()
        payload_hash = sha256_text(parsed.payload)
        if current == "complete":
            if value.get("response_sha256") != response_hash:
                raise StateError(
                    "Completed assignment is immutable and the new response differs",
                    details={"assignment_id": assignment_id},
                )
            previous = value.get("native_collection")
            if previous and native_collection and any(previous[k] != native_collection[k] for k in ("worker_id", "turn_id", "user_message_id", "assistant_message_id")):
                raise StateError("Completed native message identity changed")
            return value, parsed.payload
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
        value["result_marker_validated"] = True
        value["verification_level"] = "bounded_native_summary" if native_collection else "framed_response"
        value["generation_finality_verified"] = False
        value["source_bytes_verified"] = False
        if native_collection:
            value["native_collection"] = native_collection
        value["no_resend"] = True
        _save_assignment(assignment_id, value, runtime)
        return load_assignment(assignment_id, runtime), parsed.payload


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
            if active_cooldown(runtime):
                raise CooldownError("Cannot purge receipts while a native cooldown is active")
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
