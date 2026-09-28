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
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from .native_storage import Directory, IntegrityError, decode

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
    def worker_pool_file(self) -> Path:
        return self.config_dir / "worker-pool.json"

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
class WorkerPoolEntry:
    """One explicitly configured, stable worker slot."""

    slot: str
    conversation_id: str
    label: str
    model_confirmation: str
    configured_at: str

    @property
    def worker(self) -> str:
        return self.conversation_id


@dataclass(frozen=True)
class WorkerPool:
    workers: tuple[WorkerPoolEntry, ...]
    legacy_worker_sha256: str | None
    file_sha256: str
    authority_path: str | None = None


# Both markers record that the user confirmed the intended worker conversation.
# Neither verifies the model or reasoning effort the user chose there.
WORKER_CONFIRMATIONS = ("user-confirmed-worker", "user-confirmed-pro")


@dataclass(frozen=True)
class PreparedAssignment:
    assignment_id: str
    worker_conversation_id: str
    parent_task_id: str
    receipt_path: Path
    wrapped_prompt: str
    continuation_of: str | None = None
    worker_slot: str | None = None
    owner_generation: int | None = None


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
    try:
        with Directory(path.absolute(), create=True):
            pass
    except (IntegrityError, OSError) as exc:
        raise ConfigurationError("Unsafe authority directory") from exc


def _remove_file(path):
    with Directory(path.parent.absolute()) as directory:
        if not directory.exists(path.name):
            raise FileNotFoundError(str(path))
        directory.remove(path.name)


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, _locked) -> None:
    _locked.validate(_locked.runtime)
    if not any(path.absolute().is_relative_to(root.absolute()) for root in
               (_locked.runtime.state_dir, _locked.runtime.config_dir)):
        raise StateError("Writer target is outside locked authority")
    if "native_client" in payload:
        raise StateError("Unsupported native-client receipt; preserve it")
    try:
        with Directory(path.parent.absolute(), create=True) as directory:
            _locked.validate(_locked.runtime)
            raw = (json.dumps(payload, indent=2, sort_keys=True,
                              ensure_ascii=False) + "\n").encode("utf-8")
            directory.write(path.name, raw)
    except IntegrityError as exc:
        raise ConfigurationError("Authority storage recovery required") from exc


def read_json(path: Path) -> dict[str, Any]:
    try:
        with Directory(path.parent.absolute()) as directory:
            value = decode(directory.read(path.name))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Missing file: {path}") from exc
    except (ValueError, UnicodeError) as exc:
        raise ConfigurationError("Invalid authority JSON; recovery required") from exc
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


class LockedToken:
    def __init__(self, runtime, directory, descriptor):
        self.runtime, self.directory, self.descriptor = runtime, directory, descriptor
        self.owner = (os.getpid(), threading.get_ident())
        self.active = True
        self.config_directory = None

    def validate(self, runtime):
        if (not self.active or self.runtime != runtime or
                self.owner != (os.getpid(), threading.get_ident())):
            raise StateError("Invalid authority lock token")
        self.directory.revalidate()
        if self.config_directory is None and os.path.lexists(runtime.config_dir):
            self.config_directory = Directory(runtime.config_dir.absolute())
        if self.config_directory is not None:
            self.config_directory.revalidate()
        info = os.stat("state.lock", dir_fd=self.directory.fd, follow_symlinks=False)
        held = os.fstat(self.descriptor)
        if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
            raise StateError("Authority lock replaced")


_lock_owners = set()
_lock_owners_guard = threading.Lock()


@contextlib.contextmanager
def state_lock(paths: RuntimePaths | None = None, *, token=None,
               deadline=None, create=True):
    runtime = paths or default_paths()
    if token is not None:
        token.validate(runtime)
        yield token
        return
    owner = (os.getpid(), threading.get_ident(), str(runtime.state_dir.absolute()))
    with _lock_owners_guard:
        if owner in _lock_owners:
            raise StateError("Recursive authority lock acquisition")
        _lock_owners.add(owner)
    try:
        with Directory(runtime.state_dir.absolute(), create=create) as directory:
            try:
                descriptor = directory.open("state.lock", os.O_RDWR, create=create)
                if create:
                    os.fsync(directory.fd)
            except FileExistsError:
                descriptor = directory.open("state.lock", os.O_RDWR)
            locked = LockedToken(runtime, directory, descriptor)
            try:
                limit = deadline if deadline is not None else time.monotonic() + 30
                while True:
                    if time.monotonic() >= limit:
                        raise TimeoutError("authority_lock_deadline")
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(min(0.01, max(0, limit - time.monotonic())))
                locked.validate(runtime)
                yield locked
            finally:
                locked.active = False
                if locked.config_directory is not None:
                    locked.config_directory.close()
                os.close(descriptor)
    except (IntegrityError, NotADirectoryError) as exc:
        raise ConfigurationError("Authority lock integrity failure") from exc
    finally:
        with _lock_owners_guard:
            _lock_owners.discard(owner)


def _snapshot(function):
    import functools
    import inspect
    signature = inspect.signature(function)

    @functools.wraps(function)
    def read(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        runtime = bound.arguments.get("paths") or default_paths()
        with state_lock(runtime, token=bound.arguments.get("_locked"),
                        create=False) as locked:
            bound.arguments["_locked"] = locked
            return function(*bound.args, **bound.kwargs)
    return read


def reservation_guard(runtime, token, assignment_id=None, parent=None, claim=None, operation=None):
    token.validate(runtime)
    if assignment_id is not None:
        from .resident import guard
        guard(runtime, token, assignment_id, operation=operation)
    if os.path.lexists(runtime.state_dir / "native-client"):
        raise StateError("Unsupported native-client storage; preserve it")
    if any("native_client" in value
           for value in list_assignments(runtime, _locked=token)):
        raise StateError("Unsupported native-client receipt; preserve it")


def save_worker(
    conversation_id: str,
    *,
    label: str = "Codex Pro Dispatch Worker",
    confirm_pro: bool = False,
    confirm_worker: bool = False,
    expected_conversation_id: str | None = None,
    paths: RuntimePaths | None = None,
    _locked=None,
) -> WorkerConfig:
    runtime = paths or default_paths()
    validate_identifier(conversation_id, field="conversation_id")
    if expected_conversation_id is not None:
        validate_identifier(expected_conversation_id, field="expected_conversation_id")
    cleaned_label = label.strip()
    if not cleaned_label:
        raise ConfigurationError("Worker label is empty")
    if len(cleaned_label) > 120:
        raise ConfigurationError("Worker label is too long")
    if not (confirm_worker or confirm_pro):
        raise ConfigurationError(
            "The user must confirm this is the intended worker conversation "
            "(confirm_worker, or the legacy confirm_pro)"
        )
    # The neutral marker wins for a new record. The legacy flag alone keeps
    # writing the legacy marker so older binaries can still read the file.
    confirmation = "user-confirmed-worker" if confirm_worker else "user-confirmed-pro"
    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked)
        legacy_worker_mutation_guard(runtime, _locked=locked)
        from .resident import guard
        guard(runtime, locked, configuration=True)
        current = active_assignment(runtime, _locked=locked)
        if current:
            raise BusyError(
                "Cannot replace the worker while an assignment is unresolved",
                details={
                    "assignment_id": current.get("assignment_id"),
                    "status": current.get("status"),
                },
            )
        # Import after core initialization; reuse the existing authority token.
        from .queue import Queue
        queue = Queue(runtime)
        with queue.locked(create=False, _locked=locked):
            claims = [r for r in queue.records(_locked=locked) if r["state"] == "claimed"]
        if claims:
            raise BusyError(
                "Cannot replace the worker while a queue claim is outstanding",
                details={"request_id": claims[0]["request_id"], "state": "claimed"},
            )
        existing = load_worker(runtime, _locked=locked) if os.path.lexists(runtime.worker_file) else None
        if expected_conversation_id is not None and (
            existing is None or existing.conversation_id != expected_conversation_id
        ):
            raise StateError(
                "Configured worker does not match expected conversation",
                details={"expected_conversation_id": expected_conversation_id,
                         "conversation_id": existing.conversation_id if existing else None},
            )
        if existing is not None:
            if expected_conversation_id is None:
                raise ConfigurationError("Replacing a configured worker requires expected_conversation_id")
            if existing.conversation_id == conversation_id:
                return existing
        worker = WorkerConfig(
            conversation_id=conversation_id,
            label=cleaned_label,
            model_confirmation=confirmation,
            configured_at=utc_now(),
        )
        atomic_write_json(
            runtime.worker_file,
            {
                "schema_version": SCHEMA_VERSION,
                "conversation_id": worker.conversation_id,
                "label": worker.label,
                "model_confirmation": worker.model_confirmation,
                "configured_at": worker.configured_at,
            }, _locked=locked,
        )
    return worker


@_snapshot
def load_worker(paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> WorkerConfig:
    runtime = paths or default_paths()
    value = read_json(runtime.worker_file)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(f"Unsupported worker config schema: {runtime.worker_file}")
    conversation_value = value.get("conversation_id")
    if not isinstance(conversation_value, str):
        raise ConfigurationError(f"Worker conversation_id is missing: {runtime.worker_file}")
    conversation_id = validate_identifier(conversation_value, field="conversation_id")
    label_value = value.get("label")
    label = label_value.strip() if isinstance(label_value, str) else ""
    if not label:
        raise ConfigurationError(f"Worker label is missing: {runtime.worker_file}")
    confirmation = value.get("model_confirmation")
    if not isinstance(confirmation, str) or confirmation not in WORKER_CONFIRMATIONS:
        raise ConfigurationError(
            "Worker conversation has not been confirmed by the user"
        )
    configured_at = value.get("configured_at")
    if not isinstance(configured_at, str) or not configured_at:
        raise ConfigurationError(f"Worker configured_at is missing: {runtime.worker_file}")
    return WorkerConfig(
        conversation_id=conversation_id,
        label=label,
        model_confirmation=confirmation,
        configured_at=configured_at,
    )


def _authority_file_bytes(path: Path) -> bytes:
    try:
        with Directory(path.parent.absolute()) as directory:
            return directory.read(path.name, 4 * 1024 * 1024)
    except FileNotFoundError:
        raise
    except (IntegrityError, OSError) as exc:
        raise ConfigurationError("Private authority file integrity failure") from exc


def _sha256_authority_file(path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    return hashlib.sha256(_authority_file_bytes(path)).hexdigest()


def _worker_entry_from_mapping(value: Mapping[str, Any]) -> WorkerPoolEntry:
    if not isinstance(value, Mapping):
        raise ConfigurationError("Invalid worker-pool entry")
    expected = {"slot", "conversation_id", "label", "model_confirmation", "configured_at"}
    if set(value) != expected:
        raise ConfigurationError("Invalid worker-pool entry schema")
    slot_value = value.get("slot")
    conversation_value = value.get("conversation_id")
    if not isinstance(slot_value, str) or not isinstance(conversation_value, str):
        raise ConfigurationError("Worker-pool slot and conversation_id must be strings")
    slot = validate_identifier(slot_value, field="worker_slot")
    conversation_id = validate_identifier(conversation_value, field="conversation_id")
    label_value = value.get("label")
    label = label_value.strip() if isinstance(label_value, str) else ""
    if not label or len(label) > 120:
        raise ConfigurationError("Worker label is missing or too long")
    confirmation = value.get("model_confirmation")
    if not isinstance(confirmation, str) or confirmation not in WORKER_CONFIRMATIONS:
        raise ConfigurationError("Worker conversation has not been confirmed by the user")
    configured_at = value.get("configured_at")
    if not isinstance(configured_at, str) or not configured_at:
        raise ConfigurationError("Worker configured_at is missing")
    return WorkerPoolEntry(
        slot=slot,
        conversation_id=conversation_id,
        label=label,
        model_confirmation=confirmation,
        configured_at=configured_at,
    )


def _pool_from_value(value: Mapping[str, Any], *, file_sha256: str) -> WorkerPool:
    if set(value) != {"schema_version", "workers", "legacy_worker_sha256"}:
        raise ConfigurationError("Invalid worker-pool schema")
    if value.get("schema_version") != 1 or not isinstance(value.get("workers"), list):
        raise ConfigurationError("Unsupported worker-pool schema")
    raw_legacy = value.get("legacy_worker_sha256")
    if raw_legacy is not None and (
        not isinstance(raw_legacy, str)
        or not re.fullmatch(r"[0-9a-f]{64}", raw_legacy)
    ):
        raise ConfigurationError("Invalid legacy worker configuration hash")
    if not 1 <= len(value["workers"]) <= 2:
        raise ConfigurationError("Worker pool must contain one or two workers")
    workers = tuple(_worker_entry_from_mapping(item) for item in value["workers"])
    if len({item.slot for item in workers}) != len(workers):
        raise ConfigurationError("Worker pool contains duplicate slots")
    if len({item.conversation_id for item in workers}) != len(workers):
        raise ConfigurationError("Worker pool contains duplicate conversations")
    return WorkerPool(
        workers=workers,
        legacy_worker_sha256=raw_legacy,
        file_sha256=file_sha256,
    )


def _load_legacy_pool(runtime: RuntimePaths, *, _locked) -> WorkerPool:
    _locked.validate(runtime)
    raw = _authority_file_bytes(runtime.worker_pool_file)
    try:
        value = decode(raw)
    except (ValueError, UnicodeError) as exc:
        raise ConfigurationError("Invalid worker-pool JSON") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("Worker pool must be a JSON object")
    pool = _pool_from_value(
        value,
        file_sha256=hashlib.sha256(raw).hexdigest(),
    )
    actual_legacy = _sha256_authority_file(runtime.worker_file)
    if pool.legacy_worker_sha256 != actual_legacy:
        raise StateError("Legacy worker configuration hash differs from worker pool")
    return pool


def authority_snapshot(paths: RuntimePaths, *, _locked):
    """Decode one authority snapshot without recursive loaders or nested locks."""
    _locked.validate(paths)
    owner_path = paths.state_dir / "resident-owner.json"
    owner = read_json(owner_path) if os.path.lexists(owner_path) else None
    if owner is not None and (type(owner.get("version")) is not int
                              or owner["version"] not in (1, 2, 3, 4)):
        raise StateError("Unsupported resident owner; preserve evidence")
    if owner is not None and owner["version"] == 4:
        raw = owner.get("worker_pool_json")
        if not isinstance(raw, str):
            raise StateError("Missing embedded worker pool")
        try:
            payload = decode(raw.encode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("pool object required")
            pool = _pool_from_value(payload, file_sha256=sha256_text(raw))
        except (ValueError, UnicodeError, TypeError) as exc:
            raise StateError("Invalid embedded worker pool") from exc
        if pool.legacy_worker_sha256 != _sha256_authority_file(paths.worker_file):
            raise StateError("Legacy worker migration witness changed")
        qualification = owner.get("qualification")
        if not isinstance(qualification, dict) or "pool_witness_sha256" not in qualification:
            raise StateError("Missing listener migration witness")
        witness = qualification["pool_witness_sha256"]
        if witness != _sha256_authority_file(paths.worker_pool_file):
            raise StateError("Legacy pool migration witness changed")
    else:
        pool = (_load_legacy_pool(paths, _locked=_locked)
                if os.path.lexists(paths.worker_pool_file) else None)
    if owner is not None and owner["version"] in (3, 4):
        if pool is None:
            raise StateError("Resident owner has no worker pool")
        from .resident import validate_pool_owner
        validate_pool_owner(owner, pool)
    if pool is not None:
        pool = WorkerPool(pool.workers, pool.legacy_worker_sha256, pool.file_sha256,
                          str(owner_path if owner and owner["version"] == 4 else paths.worker_pool_file))
    return owner, pool


def _load_worker_pool_unlocked(runtime: RuntimePaths, *, _locked):
    pool = authority_snapshot(runtime, _locked=_locked)[1]
    if pool is None:
        raise ConfigurationError(f"Missing worker pool: {runtime.worker_pool_file}")
    return pool


@_snapshot
def load_worker_pool(paths: RuntimePaths | None = None, *, _locked=None) -> WorkerPool:
    return _load_worker_pool_unlocked(paths or default_paths(), _locked=_locked)


@_snapshot
def worker_pool_active(paths: RuntimePaths | None = None, *, _locked=None) -> bool:
    return authority_snapshot(paths or default_paths(), _locked=_locked)[1] is not None


@_snapshot
def configured_workers(paths: RuntimePaths | None = None, *, _locked=None) -> tuple[WorkerPoolEntry, ...]:
    runtime = paths or default_paths()
    if worker_pool_active(runtime, _locked=_locked):
        return _load_worker_pool_unlocked(runtime, _locked=_locked).workers
    worker = load_worker(runtime, _locked=_locked)
    return (WorkerPoolEntry(
        slot="worker-1",
        conversation_id=worker.conversation_id,
        label=worker.label,
        model_confirmation=worker.model_confirmation,
        configured_at=worker.configured_at,
    ),)


def worker_pool_payload(pool: WorkerPool) -> dict[str, Any]:
    return {
        "authority_path": pool.authority_path,
        "schema_version": 1,
        "legacy_worker_sha256": pool.legacy_worker_sha256,
        "file_sha256": pool.file_sha256,
        "workers": [
            {
                "slot": worker.slot,
                "conversation_id": worker.conversation_id,
                "label": worker.label,
                "model_confirmation": worker.model_confirmation,
                "configured_at": worker.configured_at,
            }
            for worker in pool.workers
        ],
    }


def worker_pool_runtime_status(
    pool: WorkerPool,
    paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> dict[str, Any]:
    """Return bounded pool readiness without exposing credentials or answers."""
    runtime = paths or default_paths()
    if _locked is None:
        with state_lock(runtime, create=False) as locked:
            return worker_pool_runtime_status(pool, runtime, _locked=locked)
    _locked.validate(runtime)
    from .queue import Queue
    from . import resident

    configured = {worker.conversation_id: worker.slot for worker in pool.workers}
    occupied: set[str] = set()
    for record in Queue(runtime).records(_locked=_locked):
        if record.get("state") != "claimed":
            continue
        worker = record.get("worker_conversation_id")
        slot = record.get("worker_slot") or configured.get(worker)
        if worker not in configured or slot != configured[worker]:
            raise StateError("Pool status found an unbound queue claim")
        occupied.add(slot)
    for record in active_assignments(runtime, _locked=_locked):
        worker = record.get("worker_conversation_id")
        slot = record.get("worker_slot") or configured.get(worker)
        if worker not in configured or slot != configured[worker]:
            raise StateError("Pool status found an unbound active receipt")
        occupied.add(slot)

    owner = resident.read(runtime, _locked)
    if owner is None:
        readiness = "enrollment_required"
    elif owner.get("version") not in (3, 4):
        readiness = "legacy_owner_requires_explicit_migration"
    elif any(item["phase"] == "cancel_pending" for item in owner["slots"]):
        readiness = "takeover_settlement_required"
    elif (any(item["phase"] == "collect_only" for item in owner["slots"])
          and not any(item["phase"] == "idle" for item in owner["slots"])):
        readiness = "collector_only_recovery_required"
    elif any(item["invocation"] is not None for item in owner["slots"]):
        readiness = "busy_original_invocation_not_finished"
    elif owner.get("session") is not None:
        readiness = "resident_bound_requires_explicit_recovery"
    else:
        readiness = "ready_for_explicit_start"
    return {
        "collect_only_slots": [item["slot"] for item in (owner or {}).get("slots", [])
                               if item["phase"] == "collect_only"],
        "capacity": len(pool.workers),
        "occupied_slots": [worker.slot for worker in pool.workers
                            if worker.slot in occupied],
        "readiness": readiness,
        "automatic_startup": "unsupported",
    }


def legacy_worker_mutation_guard(runtime: RuntimePaths, *, _locked) -> None:
    if worker_pool_active(runtime, _locked=_locked):
        raise StateError(
            "Worker pool is active; use listener start for destination changes"
        )


def worker_entry_payload(worker: WorkerPoolEntry) -> dict[str, Any]:
    return {
        "slot": worker.slot,
        "conversation_id": worker.conversation_id,
        "label": worker.label,
        "model_confirmation": worker.model_confirmation,
        "configured_at": worker.configured_at,
    }


def activate_worker_pool(
    workers: Iterable[Mapping[str, Any]],
    *,
    expected_legacy_sha256: str | None,
    evidence_file: str | Path,
    evidence_sha256: str,
    paths: RuntimePaths | None = None,
    _locked=None,
) -> WorkerPool:
    """Explicit maintenance-only pool activation with a write-before-proof fence."""
    runtime = paths or default_paths()
    if expected_legacy_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", expected_legacy_sha256
    ):
        raise ConfigurationError("Expected lowercase legacy worker SHA-256 or null")
    if not isinstance(evidence_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", evidence_sha256
    ):
        raise ConfigurationError("Expected lowercase evidence SHA-256")
    entries = tuple(_worker_entry_from_mapping(dict(item)) for item in workers)
    if not 1 <= len(entries) <= 2:
        raise ConfigurationError("Worker pool must contain one or two workers")
    if len({item.slot for item in entries}) != len(entries):
        raise ConfigurationError("Worker pool contains duplicate slots")
    if len({item.conversation_id for item in entries}) != len(entries):
        raise ConfigurationError("Worker pool contains duplicate conversations")

    from .native_storage import read_evidence
    with state_lock(runtime, token=_locked, create=False) as locked:
        reservation_guard(runtime, locked)
        if worker_pool_active(runtime, _locked=locked):
            raise BusyError("Worker pool is already active; use listener start for destination changes")
        actual_legacy = _sha256_authority_file(runtime.worker_file)
        if actual_legacy != expected_legacy_sha256:
            raise StateError("Expected legacy worker hash differs; activation writes nothing")
        try:
            raw_evidence = read_evidence(str(evidence_file))
        except (IntegrityError, OSError) as exc:
            raise ConfigurationError("Pool activation evidence integrity rejected") from exc
        if hashlib.sha256(raw_evidence).hexdigest() != evidence_sha256:
            raise StateError("Pool activation evidence hash differs")
        try:
            proof = json.loads(raw_evidence)
        except (ValueError, UnicodeError) as exc:
            raise StateError("Pool activation evidence is not valid JSON") from exc
        bindings = {
            "config_dir": str(runtime.config_dir),
            "state_dir": str(runtime.state_dir),
        }
        if (
            not isinstance(proof, dict)
            or proof.get("kind") not in {"fresh_deployment", "legacy_quiescence"}
            or any(proof.get(key) != value for key, value in bindings.items())
            or proof.get("physical_quiescence") is not True
            or any(not isinstance(proof.get(key), str) or not proof[key].strip()
                   for key in ("implementation", "observations", "authorization"))
        ):
            raise StateError("Pool activation evidence binding or authorization missing")

        # Only claims still require their worker, including interrupted publication
        # after receipt completion. Published answers are durable and collectible
        # without that worker; acknowledged/released records are terminal history.
        # Active receipts are guarded separately below, regardless of queue state.
        from .queue import Queue
        records = Queue(runtime).records(_locked=locked)
        retained = {entry.conversation_id for entry in entries}
        for record in records:
            if record["state"] != "claimed":
                continue
            worker = record.get("worker_conversation_id")
            if worker and worker not in retained:
                raise StateError("Unresolved queue worker is not retained by pool")
        for assignment in list_assignments(runtime, _locked=locked):
            if assignment.get("status") in ACTIVE_STATUSES and (
                assignment.get("worker_conversation_id") not in retained
            ):
                raise StateError("Unresolved receipt worker is not retained by pool")
        owner_path = runtime.state_dir / "resident-owner.json"
        if os.path.lexists(owner_path):
            owner = read_json(owner_path)
            if owner.get("version") in (3, 4):
                raise StateError("Schema-3 resident owner requires the worker pool")
            if owner.get("version") in {1, 2} and (
                owner.get("inflight") is not None
                or owner.get("worker") not in retained
            ):
                raise BusyError("Legacy resident owner is not physically quiescent")
            if owner.get("version") not in {1, 2}:
                raise StateError("Unsupported resident owner; activation writes nothing")
            if owner.get("session") is not None:
                from .resident import require_closed_session
                require_closed_session(runtime, owner, _locked=locked)
        if os.path.lexists(runtime.state_dir / "native-client"):
            raise StateError("Unsupported native-client storage; activation writes nothing")
        payload = {
            "schema_version": 1,
            "workers": [worker_entry_payload(entry) for entry in entries],
            "legacy_worker_sha256": actual_legacy,
        }
        atomic_write_json(runtime.worker_pool_file, payload, _locked=locked)
        raw = _authority_file_bytes(runtime.worker_pool_file)
        return _pool_from_value(payload, file_sha256=hashlib.sha256(raw).hexdigest())


def assignment_path(assignment_id: str, paths: RuntimePaths | None = None) -> Path:
    runtime = paths or default_paths()
    validate_identifier(assignment_id, field="assignment_id")
    return runtime.assignments_dir / f"{assignment_id}.json"


@_snapshot
def load_assignment(
    assignment_id: str, paths: RuntimePaths | None = None,
    *,
    _locked=None,
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
    *,
    _locked,
    operation=None,
) -> Path:
    _locked.validate(paths or default_paths())
    from .resident import guard
    guard(paths or default_paths(), _locked, assignment_id, operation=operation)
    from .resident import invocation
    caller = invocation.get()
    expected_parent = (caller.get("request_parent", caller.get("parent")) if isinstance(caller, dict)
                       and (caller.get("takeover_settlement") is True or caller.get("collector_only") is True)
                       else caller.get("parent")) if caller is not None else None
    if caller is not None and (value.get("parent_task_id") != expected_parent
                              or (caller.get("worker") is not None
                                  and value.get("worker_conversation_id") != caller.get("worker"))):
        raise StateError("Resident receipt identity mismatch")
    path = assignment_path(assignment_id, paths)
    payload = dict(value)
    payload["schema_version"] = SCHEMA_VERSION
    payload["assignment_id"] = assignment_id
    payload["updated_at"] = utc_now()
    atomic_write_json(path, payload, _locked=_locked)
    return path


@_snapshot
def list_assignments(paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> list[dict[str, Any]]:
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


def redact_stored_diagnostics(paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> int:
    """Durably remove raw diagnostic bodies written by releases before v1.1."""
    runtime = paths or default_paths()
    redacted_count = 0
    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked)
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
                from .resident import guard
                guard(runtime, locked, assignment_id)
                atomic_write_json(path, redacted, _locked=locked)
                redacted_count += 1
    return redacted_count


@_snapshot
def active_assignments(paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> list[dict[str, Any]]:
    runtime = paths or default_paths()
    active = [
        value for value in list_assignments(runtime, _locked=_locked)
        if value.get("status") in ACTIVE_STATUSES
    ]
    if worker_pool_active(runtime, _locked=_locked):
        pool = _load_worker_pool_unlocked(runtime, _locked=_locked)
        configured = {worker.conversation_id: worker.slot for worker in pool.workers}
        seen_workers: set[str] = set()
        seen_slots: set[str] = set()
        for index, original in enumerate(active):
            value = dict(original)
            worker = value.get("worker_conversation_id")
            slot = value.get("worker_slot") or configured.get(worker)
            if worker not in configured or not isinstance(slot, str) or configured[worker] != slot:
                raise StateError(
                    "Active pool receipt is not bound to a configured worker slot",
                    details={"assignment_id": value.get("assignment_id")},
                )
            if worker in seen_workers or slot in seen_slots:
                raise StateError("Multiple active assignments share one worker slot")
            seen_workers.add(worker)
            seen_slots.add(slot)
            active[index] = value
        if len(active) > len(pool.workers):
            raise StateError("Active assignment count exceeds worker pool capacity")
    elif len(active) > 1:
        raise StateError(
            "Multiple active assignments exist",
            details={"assignment_ids": [value.get("assignment_id") for value in active]},
        )
    return active


@_snapshot
def active_assignment(paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> dict[str, Any] | None:
    active = active_assignments(paths, _locked=_locked)
    if len(active) > 1:
        raise StateError(
            "Multiple active assignments exist",
            details={"assignment_ids": [value.get("assignment_id") for value in active]},
        )
    return active[0] if active else None


@_snapshot
def active_cooldown(
    paths: RuntimePaths | None = None,
    *,
    now: dt.datetime | None = None,
    _locked=None,
) -> dict[str, Any] | None:
    """Return the latest unexpired native unusual-activity cooldown."""
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ConfigurationError("Cooldown comparison time must include a timezone")
    current = current.astimezone(dt.timezone.utc)
    active: list[tuple[dt.datetime, dict[str, Any]]] = []
    for value in list_assignments(paths, _locked=_locked):
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
    queue_claim_token: str | None = None,
    worker_config: WorkerConfig | WorkerPoolEntry | None = None,
    worker_slot: str | None = None,
    owner_generation: int | None = None,
    _locked=None,
) -> PreparedAssignment:
    runtime = paths or default_paths()
    validate_identifier(parent_task_id, field="parent_task_id")
    resolved_id = assignment_id if assignment_id is not None else new_assignment_id()
    validate_identifier(resolved_id, field="assignment_id")
    if worker_slot is not None:
        validate_identifier(worker_slot, field="worker_slot")
    if owner_generation is not None and (
        type(owner_generation) is not int or owner_generation < 0
    ):
        raise ConfigurationError("owner_generation must be a nonnegative integer")

    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked)
        from .resident import guard
        guard(runtime, locked, resolved_id)
        from . import resident
        caller = resident.invocation.get()
        if worker_pool_active(runtime, _locked=locked):
            pool = _load_worker_pool_unlocked(runtime, _locked=locked)
            owner = resident.read(runtime, locked)
            selected = worker_config
            if selected is None and worker_slot is not None:
                selected = next((item for item in pool.workers if item.slot == worker_slot), None)
            if not isinstance(selected, WorkerPoolEntry):
                raise StateError("Pool assignment requires an explicitly selected worker slot")
            if selected not in pool.workers:
                raise StateError("Selected worker is not in the configured pool")
            if worker_slot is not None and selected.slot != worker_slot:
                raise StateError("Selected worker slot differs from the pool claim")
            worker = selected
            if owner is not None and owner.get("version") in (3, 4):
                if (not isinstance(caller, dict) or caller.get("collector_only") is True
                        or (caller.get("takeover_settlement") is True
                            and resident.settlement_operation.get() != "prepare_cancel")):
                    raise StateError("Pool assignment requires the serving resident invocation")
                if (owner_generation != owner["generation"]
                        and caller.get("takeover_settlement") is not True):
                    raise StateError("Pool assignment generation differs from the resident owner")
                if caller.get("slot") not in {None, worker.slot}:
                    raise StateError("Pool assignment slot differs from the resident invocation")
            elif owner is not None:
                raise StateError("Legacy resident owner cannot prepare a pool assignment")
        else:
            if worker_config is not None or worker_slot is not None:
                raise StateError("Worker slots require an active worker pool")
            worker = load_worker(runtime, _locked=locked)
        if isinstance(caller, dict) and caller.get("takeover_settlement") is True:
            from .queue import Queue
            stored = Queue(runtime).load(resolved_id, _locked=locked)
            if (resident.settlement_operation.get() != "prepare_cancel"
                    or parent_task_id != stored.get("parent_task_id")
                    or prompt != stored.get("prompt")
                    or queue_claim_token != stored.get("queue_claim_token")
                    or worker.conversation_id != stored.get("worker_conversation_id")
                    or worker_slot != stored.get("worker_slot")
                    or owner_generation != stored.get("owner_generation")):
                raise BusyError("settlement_out_of_scope")
        if os.path.lexists(assignment_path(resolved_id, runtime)):
            raise StateError(
                "Assignment ID already exists; refusing a possible duplicate submission",
                details={"assignment_id": resolved_id},
            )
        existing_active = active_assignments(runtime, _locked=locked)
        if existing_active:
            if not worker_pool_active(runtime, _locked=locked) or any(
                value.get("worker_conversation_id") == worker.conversation_id
                for value in existing_active
            ) or len(existing_active) >= 2:
                value = existing_active[0]
                _reject_legacy_active_assignment(value, operation="prepare")
                raise BusyError(
                    "Another dispatch is unresolved",
                    details={
                        "assignment_id": value.get("assignment_id"),
                        "status": value.get("status"),
                    },
                )
        cooldown = active_cooldown(runtime, _locked=locked)
        if cooldown:
            raise CooldownError(
                "Native ChatGPT HTTP 403 cooldown is still active",
                details=cooldown,
            )

        previous: dict[str, Any] | None = None
        if continuation_of:
            validate_identifier(continuation_of, field="continuation_of")
            previous = load_assignment(continuation_of, runtime, _locked=locked)
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
        if queue_claim_token is not None:
            receipt["queue_claim_token"] = queue_claim_token
        if isinstance(worker, WorkerPoolEntry):
            receipt["worker_slot"] = worker.slot
        if owner_generation is not None:
            receipt["owner_generation"] = owner_generation
        if isinstance(caller, dict) and caller.get("takeover_settlement") is True:
            if any(receipt.get(key) != stored.get(key) for key in (
                    "prompt_sha256", "wrapped_prompt_sha256", "result_protocol")):
                raise BusyError("settlement_out_of_scope")
        path = _save_assignment(resolved_id, receipt, runtime, _locked=locked)

    return PreparedAssignment(
        assignment_id=resolved_id,
        worker_conversation_id=worker.conversation_id,
        parent_task_id=parent_task_id,
        receipt_path=path,
        wrapped_prompt=wrapped,
        continuation_of=continuation_of,
        worker_slot=worker.slot if isinstance(worker, WorkerPoolEntry) else None,
        owner_generation=owner_generation,
    )


def _transition(
    assignment_id: str,
    *,
    allowed: set[str] | frozenset[str],
    target: str,
    updates: Mapping[str, Any] | None = None,
    paths: RuntimePaths | None = None,
    _locked=None,
) -> dict[str, Any]:
    runtime = paths or default_paths()
    validate_status(target)
    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked, assignment_id, operation=target)
        value = load_assignment(assignment_id, runtime, _locked=locked)
        current = str(value["status"])
        if target != "abandoned":
            _reject_legacy_active_assignment(value, operation=target)
        if current not in allowed:
            raise StateError(
                f"Cannot move assignment from {current} to {target}",
                details={"assignment_id": assignment_id, "status": current},
            )
        if target == "armed":
            if worker_pool_active(runtime, _locked=locked):
                raise StateError(
                    "Pool assignments require the slot-specific arm-for-send operation"
                )
            active_assignment(runtime, _locked=locked)  # Reject multiple unresolved assignments.
            if active_cooldown(runtime, _locked=locked):
                raise CooldownError("Native unusual-activity cooldown blocks arming")
        value["status"] = target
        if updates:
            value.update(dict(updates))
        _save_assignment(assignment_id, value, runtime, _locked=locked, operation=target)
        return value


def arm_assignment(
    assignment_id: str, paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> dict[str, Any]:
    """Durably prohibit resends immediately before the one native send attempt."""
    return _transition(
        assignment_id,
        allowed={"prepared"},
        target="armed",
        updates={"armed_at": utc_now(), "no_resend": True},
        paths=paths, _locked=_locked,
    )


def arm_for_send(
    worker_slot: str,
    request_id: str,
    generation: int,
    invocation_id: str,
    paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> dict[str, Any]:
    """Atomically fence one pool slot and return the exact prompt to send.

    The caller must immediately perform its one native send with the returned
    bytes. No later admission, worker selection, or release operation is part
    of this helper's contract.
    """
    runtime = paths or default_paths()
    validate_identifier(worker_slot, field="worker_slot")
    validate_identifier(request_id, field="request_id")
    validate_identifier(invocation_id, field="invocation")
    if type(generation) is not int or generation < 1:
        raise ConfigurationError("generation must be a positive integer")
    from .resident import invocation, guard
    caller = invocation.get()
    if not isinstance(caller, dict):
        raise StateError("arm-for-send requires a resident invocation")
    if caller.get("collector_only") is True or caller.get("takeover_settlement") is True:
        raise StateError("Collector-only recovery cannot arm or send")
    if caller.get("invocation") != invocation_id or caller.get("request") != request_id:
        raise StateError("arm-for-send invocation identity differs")
    if caller.get("generation") != generation or caller.get("slot") not in {None, worker_slot}:
        raise StateError("arm-for-send generation or slot differs")

    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked, request_id, operation="arm")
        from . import resident
        owner = resident.read(runtime, locked)
        marker = resident._recovery_marker(runtime, owner) if owner is not None else None
        if marker is not None and marker[0] == "current":
            raise StateError("Collector-only recovery is open; cannot arm or send")
        pool = _load_worker_pool_unlocked(runtime, _locked=locked)
        selected = next((item for item in pool.workers if item.slot == worker_slot), None)
        if selected is None:
            raise StateError("arm-for-send worker slot is not configured")
        if caller.get("worker") not in {None, selected.conversation_id}:
            raise StateError("arm-for-send worker identity differs")
        guard(runtime, locked, request_id)
        from .queue import Queue
        broker = Queue(runtime)
        record = broker.load(request_id, _locked=locked)
        if record.get("state") != "claimed":
            raise StateError("arm-for-send requires the bound queue claim")
        if (
            record.get("parent_task_id") != caller.get("parent")
            or record.get("worker_conversation_id") != selected.conversation_id
            or record.get("worker_slot") != worker_slot
        ):
            raise StateError("arm-for-send queue association differs")
        claimed_generation = record.get("owner_generation")
        if type(claimed_generation) is int and claimed_generation > generation:
            raise StateError("arm-for-send queue generation is newer than the caller")
        receipt = broker.receipt(record, _locked=locked)
        if receipt is None or receipt.get("status") != "prepared":
            raise StateError("arm-for-send requires a prepared receipt")
        if receipt.get("worker_slot") != worker_slot:
            raise StateError("arm-for-send receipt association differs")
        receipt_generation = receipt.get("owner_generation")
        if type(receipt_generation) is int and receipt_generation > generation:
            raise StateError("arm-for-send receipt generation is newer than the caller")
        wrapped = wrap_prompt(record["prompt"], request_id)
        if sha256_text(wrapped) != record.get("wrapped_prompt_sha256") or \
                sha256_text(wrapped) != receipt.get("wrapped_prompt_sha256"):
            raise StateError("arm-for-send wrapped prompt changed")
        active = active_assignments(runtime, _locked=locked)
        if not any(value.get("assignment_id") == request_id for value in active):
            raise StateError("arm-for-send receipt is not active")
        if active_cooldown(runtime, _locked=locked):
            raise CooldownError("Native unusual-activity cooldown blocks arming")
        receipt = dict(receipt)
        receipt.update({"status": "armed", "armed_at": utc_now(), "no_resend": True})
        from .resident import mark_running
        mark_running(
            runtime, locked, worker_slot, request_id, generation, invocation_id
        )
        _save_assignment(request_id, receipt, runtime, _locked=locked)
        return {
            "assignment": receipt,
            "wrapped_prompt": wrapped,
            "worker_conversation_id": selected.conversation_id,
            "worker_slot": worker_slot,
            "owner_generation": generation,
            "send_authorized": False,
            "no_resend": True,
        }


def mark_submitted(
    assignment_id: str,
    sent_prompt: str,
    paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> dict[str, Any]:
    runtime = paths or default_paths()
    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked, assignment_id, operation="submitted")
        value = load_assignment(assignment_id, runtime, _locked=locked)
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
            _save_assignment(assignment_id, value, runtime, _locked=locked)
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
        _save_assignment(assignment_id, value, runtime, _locked=locked)
        return value


def mark_pending(
    assignment_id: str, paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> dict[str, Any]:
    return _transition(
        assignment_id,
        allowed={"submitted"},
        target="pending",
        updates={"pending_since": utc_now()},
        paths=paths, _locked=_locked,
    )


def mark_indeterminate(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
    _locked=None,
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
        paths=paths, _locked=_locked,
    )


def mark_unusual_activity_403(
    assignment_id: str,
    *,
    reason: str,
    request_id: str | None = None,
    paths: RuntimePaths | None = None,
    _locked=None,
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
    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked, assignment_id)
        value = load_assignment(assignment_id, runtime, _locked=locked)
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
                _save_assignment(assignment_id, value, runtime, _locked=locked)
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
        _save_assignment(assignment_id, value, runtime, _locked=locked)
        return value


def mark_ambiguous(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
    _locked=None,
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
        paths=paths, _locked=_locked,
    )


def abandon_assignment(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
    _locked=None,
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
        paths=paths, _locked=_locked,
    )


def _native_outbound(
    raw: bytes, receipt: Mapping[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Select exact outbound read-back independently of worker/reply progress."""
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
        flags = {}
        for name, scope in (("envelope", data), ("thread", thread), ("turn", turn), ("user", user), ("user_text", content[0])):
            for key in ("truncated", "textTruncated"):
                value = scope.get(key)
                if key in scope and type(value) is not bool:
                    invalid()
                if value is True:
                    raise MarkerError("truncated-response: native summary reports shortening")
                flags[f"{name}.{key}"] = value  # null means omitted, never false.
        return content[0]["text"], data, turn, flags
    except (ValueError, UnicodeError, RecursionError, TypeError, KeyError, IndexError):
        invalid()


def _native_response(raw: bytes, receipt: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Select a framed exchange from the desktop's lossy history summary.

    This establishes summary association, not source-byte integrity or native
    generation finality. Do not manufacture production evidence from it.
    """
    def invalid() -> None:
        raise MarkerError("native-read-invalid: unsupported or ambiguous native summary")

    try:
        _, data, turn, flags = _native_outbound(raw, receipt)
        thread = data["thread"]
        user = turn["items"][0]
        if thread["status"]["type"] != "idle":
            raise StateError("native-worker-not-idle: wait and read again; never resend")
        if len(turn["items"]) == 1:
            raise StateError("native-reply-not-observed: assistant absent; never resend")
        if len(turn["items"]) != 2:
            invalid()
        assistant = turn["items"][1]
        if assistant.get("type") != "agentMessage" or not isinstance(assistant.get("text"), str):
            invalid()
        for key in ("truncated", "textTruncated"):
            value = assistant.get(key)
            if key in assistant and type(value) is not bool:
                invalid()
            if value is True:
                raise MarkerError("truncated-response: native summary reports shortening")
            flags[f"assistant.{key}"] = value
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
    _locked=None,
) -> tuple[dict[str, Any], str]:
    runtime = paths or default_paths()
    raw = _response_bytes(response)

    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked, assignment_id, operation="complete")
        value = load_assignment(assignment_id, runtime, _locked=locked)
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
        _save_assignment(assignment_id, value, runtime, _locked=locked)
        return load_assignment(assignment_id, runtime, _locked=locked), parsed.payload


@_snapshot
def recovery_info(
    assignment_id: str, paths: RuntimePaths | None = None,
    *,
    _locked=None,
) -> dict[str, Any]:
    value = load_assignment(assignment_id, paths, _locked=_locked)
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
    cooldown = active_cooldown(paths, _locked=_locked)
    if cooldown and cooldown.get("assignment_id") == assignment_id:
        recovery["active_cooldown"] = cooldown
    return recovery


def reset_worker(
    *, force: bool = False, paths: RuntimePaths | None = None,
    _locked=None,
) -> bool:
    runtime = paths or default_paths()
    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked)
        legacy_worker_mutation_guard(runtime, _locked=locked)
        from .resident import guard
        guard(runtime, locked, configuration=True)
        if not force:
            current = active_assignment(runtime, _locked=locked)
            if current:
                raise BusyError(
                    "Cannot reset the worker while an assignment is unresolved",
                    details={
                        "assignment_id": current.get("assignment_id"),
                        "status": current.get("status"),
                    },
                )
        try:
            _remove_file(runtime.worker_file)
            return True
        except FileNotFoundError:
            return False


def purge_local_state(
    *, force: bool = False, paths: RuntimePaths | None = None,
    _locked=None,
) -> dict[str, bool]:
    runtime = paths or default_paths()
    with state_lock(runtime, token=_locked) as locked:
        reservation_guard(runtime, locked)
        legacy_worker_mutation_guard(runtime, _locked=locked)
        from .resident import guard
        guard(runtime, locked, configuration=True)
        if os.path.lexists(runtime.state_dir / "queue"):
            raise StateError("Cannot purge while queue records exist; preserve queue receipts")
        if not force:
            current = active_assignment(runtime, _locked=locked)
            if current:
                raise BusyError(
                    "Cannot purge local state while an assignment is unresolved",
                    details={
                        "assignment_id": current.get("assignment_id"),
                        "status": current.get("status"),
                    },
                )
            if active_cooldown(runtime, _locked=locked):
                raise CooldownError("Cannot purge receipts while a native cooldown is active")
        worker_removed = False
        assignments_removed = False
        with contextlib.suppress(FileNotFoundError):
            _remove_file(runtime.worker_file)
            worker_removed = True
        if runtime.assignments_dir.exists():
            for path in runtime.assignments_dir.glob("*.json"):
                _remove_file(path)
            with contextlib.suppress(OSError):
                runtime.assignments_dir.rmdir()
            assignments_removed = True
        return {
            "worker_removed": worker_removed,
            "assignments_removed": assignments_removed,
        }
