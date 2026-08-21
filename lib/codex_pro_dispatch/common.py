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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

APP_NAME = "codex-pro-dispatch"

SCHEMA_VERSION = 1

DEFAULT_TIMEOUT_SECONDS = 3600

MAX_DAEMON_REQUEST_BYTES = 8 * 1024 * 1024

ASSIGNMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

ROOT = Path(__file__).resolve().parents[2]
HELPER_DIR = ROOT / "bin"

class DispatchError(RuntimeError):
    """Base class for expected, user-facing bridge failures."""

    exit_code = 2

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})

class ConfigurationError(DispatchError):
    pass

class BusyError(DispatchError):
    exit_code = 3

class DraftPresentError(DispatchError):
    exit_code = 4

class BridgeCommandError(DispatchError):
    exit_code = 5

class BridgeTimeout(BridgeCommandError):
    exit_code = 6

class ResponseUnavailableError(DispatchError):
    exit_code = 7

class DaemonError(DispatchError):
    exit_code = 8

@dataclass(frozen=True)
class TargetConfig:
    bundle_id: str
    app_name: str = ""
    window_title: str = ""
    transport: str = "direct"
    socket_path: str = ""

    def validate(self) -> "TargetConfig":
        if not self.bundle_id.strip():
            raise ConfigurationError("bundle_id is required")
        if self.transport not in {"direct", "daemon"}:
            raise ConfigurationError("transport must be direct or daemon")
        return self

    def helper_args(self) -> list[str]:
        args = ["--bundle-id", self.bundle_id]
        if self.app_name:
            args.extend(["--app-name", self.app_name])
        if self.window_title:
            args.extend(["--window-title", self.window_title])
        return args

@dataclass(frozen=True)
class Snapshot:
    group_count: int
    messages: tuple[Mapping[str, Any], ...]
    input_value: str
    bundle_id: str = ""
    pid: int = 0
    window_title: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Snapshot":
        try:
            group_count = int(payload["group_count"])
            messages_raw = payload.get("messages", [])
            if not isinstance(messages_raw, list):
                raise TypeError("messages must be a list")
            messages: list[Mapping[str, Any]] = []
            for item in messages_raw:
                if not isinstance(item, Mapping):
                    raise TypeError("message must be an object")
                messages.append(dict(item))
            return cls(
                group_count=group_count,
                messages=tuple(messages),
                input_value=str(payload.get("input_value", "")),
                bundle_id=str(payload.get("bundle_id", "")),
                pid=int(payload.get("pid", 0) or 0),
                window_title=str(payload.get("window_title", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BridgeCommandError(
                "The ChatGPT reader returned malformed JSON",
                details={"payload": dict(payload)},
            ) from exc

@dataclass(frozen=True)
class RuntimePaths:
    config_dir: Path
    state_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def receipts_dir(self) -> Path:
        return self.state_dir / "receipts"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "dispatch.lock"

    @property
    def default_socket(self) -> Path:
        return self.state_dir / "dispatch.sock"

def default_paths() -> RuntimePaths:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    ).expanduser()
    state_home = Path(
        os.environ.get("CODEX_PRO_DISPATCH_STATE_DIR")
        or os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    ).expanduser()
    return RuntimePaths(
        config_dir=config_home / APP_NAME,
        state_dir=state_home / APP_NAME,
    )

def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigurationError(
            f"Directory is not private: {path}",
            details={"mode": oct(mode)},
        )

def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _secure_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
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

def save_config(config: TargetConfig, paths: RuntimePaths | None = None) -> Path:
    runtime = paths or default_paths()
    validated = config.validate()
    atomic_write_json(
        runtime.config_file,
        {
            "schema_version": SCHEMA_VERSION,
            **asdict(validated),
        },
    )
    return runtime.config_file

def load_config(paths: RuntimePaths | None = None) -> TargetConfig:
    runtime = paths or default_paths()
    value = read_json(runtime.config_file)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(
            f"Unsupported config schema in {runtime.config_file}"
        )
    return TargetConfig(
        bundle_id=str(value.get("bundle_id", "")),
        app_name=str(value.get("app_name", "")),
        window_title=str(value.get("window_title", "")),
        transport=str(value.get("transport", "direct")),
        socket_path=str(value.get("socket_path", "")),
    ).validate()

def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )

def new_assignment_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"dispatch-{stamp}-{secrets.token_hex(4)}"

def validate_assignment_id(value: str) -> str:
    if not ASSIGNMENT_ID_PATTERN.fullmatch(value):
        raise ConfigurationError(
            "assignment_id may contain only letters, digits, dot, underscore, and hyphen"
        )
    return value

def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def snapshot_digest(snapshot: Snapshot) -> str:
    material = json.dumps(
        {
            "group_count": snapshot.group_count,
            "messages": list(snapshot.messages),
            "input_value": snapshot.input_value,
            "bundle_id": snapshot.bundle_id,
            "pid": snapshot.pid,
            "window_title": snapshot.window_title,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256_text(material)

def wrap_prompt(prompt: str, assignment_id: str) -> str:
    cleaned = prompt.strip()
    if not cleaned:
        raise ConfigurationError("Prompt is empty")
    return (
        f"[CODEX_PRO_DISPATCH assignment_id={assignment_id}]\n\n"
        f"{cleaned}\n\n"
        "Keep the assignment ID in context. Return the requested deliverable in this "
        "thread. Do not claim that local commands or tests ran unless their actual "
        "results were supplied in this conversation."
    )

def extract_response(snapshot: Snapshot, baseline_group_count: int) -> str:
    parts: list[str] = []
    for item in snapshot.messages:
        try:
            index = int(item.get("index", -1))
        except (TypeError, ValueError):
            continue
        if index < baseline_group_count or item.get("kind") != "text":
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        if parts and parts[-1] == text:
            continue
        parts.append(text)
    return "\n".join(parts).strip()

@contextlib.contextmanager
def dispatch_lock(paths: RuntimePaths | None = None) -> Iterable[None]:
    runtime = paths or default_paths()
    _secure_directory(runtime.state_dir)
    descriptor = os.open(runtime.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BusyError("Another dispatch is already running") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

def receipt_path(assignment_id: str, paths: RuntimePaths | None = None) -> Path:
    runtime = paths or default_paths()
    validate_assignment_id(assignment_id)
    return runtime.receipts_dir / f"{assignment_id}.json"

def save_receipt(
    assignment_id: str,
    payload: Mapping[str, Any],
    paths: RuntimePaths | None = None,
) -> Path:
    path = receipt_path(assignment_id, paths)
    value = dict(payload)
    value["schema_version"] = SCHEMA_VERSION
    value["assignment_id"] = assignment_id
    value["updated_at"] = utc_now()
    atomic_write_json(path, value)
    return path

def load_receipt(
    assignment_id: str,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    path = receipt_path(assignment_id, paths)
    value = read_json(path)
    if value.get("assignment_id") != assignment_id:
        raise ConfigurationError(f"Receipt identity mismatch: {path}")
    return value
