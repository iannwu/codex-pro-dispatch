from __future__ import annotations

import contextlib
import json
import os
import socket
import socketserver
import stat
from pathlib import Path
from typing import Any, Mapping

from .common import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_DAEMON_REQUEST_BYTES,
    ConfigurationError,
    DaemonError,
    DispatchError,
    RuntimePaths,
    TargetConfig,
    _secure_directory,
    default_paths,
)
from .dispatcher import Dispatcher

def configured_socket_path(
    config: TargetConfig | None,
    paths: RuntimePaths | None = None,
    *,
    override: str | None = None,
) -> Path:
    runtime = paths or default_paths()
    raw = override or (config.socket_path if config else "")
    candidate = Path(raw).expanduser() if raw else runtime.default_socket
    if not candidate.is_absolute():
        candidate = runtime.state_dir / candidate

    state_root = runtime.state_dir.expanduser().resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(state_root):
        raise ConfigurationError(
            "The daemon socket must be inside the private state directory",
            details={
                "socket_path": str(resolved),
                "state_directory": str(state_root),
            },
        )
    return resolved

def _socket_request(
    socket_path: Path,
    payload: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    request = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(request.encode("utf-8")) > MAX_DAEMON_REQUEST_BYTES:
        raise DaemonError("Daemon request exceeds the size limit")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_seconds)
    try:
        client.connect(str(socket_path))
        client.sendall(request.encode("utf-8"))
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = client.recv(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DAEMON_REQUEST_BYTES:
                raise DaemonError("Daemon response exceeds the size limit")
            if b"\n" in chunk:
                break
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise DaemonError(f"Cannot reach daemon at {socket_path}: {exc}") from exc
    finally:
        client.close()
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DaemonError("Daemon returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DaemonError("Daemon returned a non-object JSON value")
    return value

class _DispatchRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_DAEMON_REQUEST_BYTES + 1)
        if len(raw) > MAX_DAEMON_REQUEST_BYTES:
            response: dict[str, Any] = {
                "ok": False,
                "error": "request_too_large",
            }
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                response = self.server.handle_payload(payload)  # type: ignore[attr-defined]
            except Exception as exc:
                response = error_payload(exc)
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(encoded.encode("utf-8") + b"\n")

class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        socket_path: Path,
        *,
        dispatcher: Dispatcher,
        target: TargetConfig,
    ) -> None:
        self.dispatcher = dispatcher
        self.target = target
        super().__init__(str(socket_path), _DispatchRequestHandler)

    def handle_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", ""))
        if action == "ping":
            return {"ok": True, "status": "ready", "pid": os.getpid()}
        if action == "send":
            return self.dispatcher.send(
                str(payload.get("prompt", "")),
                target=self.target,
                timeout_seconds=float(
                    payload.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
                ),
                assignment_id=(
                    str(payload["assignment_id"])
                    if payload.get("assignment_id")
                    else None
                ),
            )
        if action == "collect":
            return self.dispatcher.collect(
                str(payload.get("assignment_id", "")),
                target=self.target,
            )
        raise DaemonError(f"Unknown daemon action: {action}")

def _remove_stale_socket(path: Path) -> None:
    if not path.exists():
        return
    mode = path.lstat().st_mode
    if not stat.S_ISSOCK(mode):
        raise DaemonError(f"Refusing to remove non-socket path: {path}")
    try:
        response = _socket_request(path, {"action": "ping"}, timeout_seconds=1)
    except DaemonError:
        path.unlink()
        return
    if response.get("ok"):
        raise DaemonError(f"A daemon is already listening at {path}")
    raise DaemonError(f"Socket is in use: {path}")

def serve(
    *,
    target: TargetConfig,
    socket_path: Path,
    dispatcher: Dispatcher | None = None,
    paths: RuntimePaths | None = None,
) -> None:
    runtime = paths or default_paths()
    socket_path = configured_socket_path(
        target,
        runtime,
        override=str(socket_path),
    )
    _secure_directory(runtime.state_dir)
    _secure_directory(socket_path.parent)
    _remove_stale_socket(socket_path)
    old_umask = os.umask(0o077)
    try:
        server = _ThreadingUnixServer(
            socket_path,
            dispatcher=dispatcher or Dispatcher(),
            target=target,
        )
    finally:
        os.umask(old_umask)
    socket_path.chmod(0o600)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()

def error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DispatchError):
        return {
            "ok": False,
            "error": str(exc),
            "error_type": exc.__class__.__name__,
            "details": exc.details,
        }
    return {
        "ok": False,
        "error": str(exc),
        "error_type": exc.__class__.__name__,
    }
