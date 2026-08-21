from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .backend import BridgeBackend, SubprocessBackend
from .common import (
    DEFAULT_TIMEOUT_SECONDS,
    HELPER_DIR,
    BridgeCommandError,
    ConfigurationError,
    DispatchError,
    TargetConfig,
    _secure_directory,
    default_paths,
    load_config,
    save_config,
)
from .daemon import _socket_request, configured_socket_path, error_payload, serve
from .dispatcher import Dispatcher

def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file is not None:
        return Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    if sys.stdin.isatty():
        raise ConfigurationError(
            "Provide --prompt, --prompt-file, or pipe the prompt on stdin"
        )
    return sys.stdin.read()

def _resolve_target_from_app_name(
    backend: BridgeBackend,
    app_name: str,
    *,
    window_title: str,
    transport: str,
    socket_path: str,
) -> TargetConfig:
    payload = backend.list_apps()
    apps = payload.get("apps", [])
    if not isinstance(apps, list):
        raise BridgeCommandError("Application discovery returned malformed JSON")
    matches = [
        item
        for item in apps
        if isinstance(item, Mapping)
        and str(item.get("name", "")).casefold() == app_name.casefold()
    ]
    if len(matches) != 1:
        available = [
            str(item.get("name", ""))
            for item in apps
            if isinstance(item, Mapping)
        ]
        raise ConfigurationError(
            f"Expected one running app named {app_name!r}; found {len(matches)}",
            details={"available_apps": available},
        )
    match = matches[0]
    return TargetConfig(
        bundle_id=str(match.get("bundle_id", "")),
        app_name=str(match.get("name", "")),
        window_title=window_title,
        transport=transport,
        socket_path=socket_path,
    ).validate()

def _json_print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))

def _result_output(result: Mapping[str, Any], *, raw: bool) -> None:
    if raw and result.get("ok") and isinstance(result.get("response"), str):
        print(result["response"])
    else:
        _json_print(result)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pro-dispatch",
        description=(
            "Dispatch assignments from Codex to a native ChatGPT Pro thread on macOS."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("apps", help="List running ChatGPT/OpenAI applications")

    configure = subparsers.add_parser("configure", help="Save target application settings")
    target = configure.add_mutually_exclusive_group(required=True)
    target.add_argument("--bundle-id")
    target.add_argument("--app-name")
    configure.add_argument("--window-title", default="")
    configure.add_argument("--transport", choices=["direct", "daemon"], default="direct")
    configure.add_argument("--socket-path", default="")

    subparsers.add_parser("doctor", help="Validate configuration and Accessibility access")

    send_parser = subparsers.add_parser("send", help="Send one assignment exactly once")
    prompt_source = send_parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt")
    prompt_source.add_argument("--prompt-file")
    send_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    send_parser.add_argument("--assignment-id")
    send_parser.add_argument("--direct", action="store_true")
    send_parser.add_argument("--daemon", action="store_true")
    send_parser.add_argument("--raw", action="store_true")

    collect_parser = subparsers.add_parser(
        "collect", help="Collect a response after an earlier timeout"
    )
    collect_parser.add_argument("assignment_id")
    collect_parser.add_argument("--direct", action="store_true")
    collect_parser.add_argument("--daemon", action="store_true")
    collect_parser.add_argument("--raw", action="store_true")

    smoke_parser = subparsers.add_parser(
        "smoke", help="Run a nonce roundtrip against the configured thread"
    )
    smoke_parser.add_argument("--timeout", type=float, default=300)
    smoke_parser.add_argument("--direct", action="store_true")
    smoke_parser.add_argument("--daemon", action="store_true")

    serve_parser = subparsers.add_parser(
        "serve", help="Run the Accessibility bridge over a local Unix socket"
    )
    serve_parser.add_argument("--socket-path")

    ping_parser = subparsers.add_parser("ping", help="Ping the local bridge daemon")
    ping_parser.add_argument("--socket-path")

    return parser

def _selected_transport(args: argparse.Namespace, config: TargetConfig) -> str:
    if getattr(args, "direct", False) and getattr(args, "daemon", False):
        raise ConfigurationError("Choose only one of --direct or --daemon")
    if getattr(args, "direct", False):
        return "direct"
    if getattr(args, "daemon", False):
        return "daemon"
    return config.transport

def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = default_paths()
    backend = SubprocessBackend()

    try:
        if args.command == "apps":
            _json_print(backend.list_apps())
            return 0

        if args.command == "configure":
            if args.app_name:
                config = _resolve_target_from_app_name(
                    backend,
                    args.app_name,
                    window_title=args.window_title,
                    transport=args.transport,
                    socket_path=args.socket_path,
                )
            else:
                config = TargetConfig(
                    bundle_id=args.bundle_id,
                    window_title=args.window_title,
                    transport=args.transport,
                    socket_path=args.socket_path,
                ).validate()
            path = save_config(config, paths)
            _json_print({"ok": True, "config": str(path), "target": asdict(config)})
            return 0

        config = load_config(paths)
        socket_path = configured_socket_path(
            config,
            paths,
            override=getattr(args, "socket_path", None),
        )

        if args.command == "doctor":
            checks: list[dict[str, Any]] = []
            for helper in [
                "cgpt-list-apps",
                "cgpt-read-app",
                "cgpt-send",
                "cgpt-wait-idle",
            ]:
                helper_path = HELPER_DIR / helper
                checks.append(
                    {
                        "name": f"helper:{helper}",
                        "ok": helper_path.exists() and os.access(helper_path, os.X_OK),
                        "path": str(helper_path),
                    }
                )
            try:
                snapshot = backend.read(config)
                checks.append(
                    {
                        "name": "target_window",
                        "ok": True,
                        "bundle_id": snapshot.bundle_id,
                        "pid": snapshot.pid,
                        "window_title": snapshot.window_title,
                        "group_count": snapshot.group_count,
                        "draft_present": bool(snapshot.input_value.strip()),
                    }
                )
            except DispatchError as exc:
                checks.append(
                    {
                        "name": "target_window",
                        "ok": False,
                        "error": str(exc),
                        "details": exc.details,
                    }
                )
            _secure_directory(paths.state_dir)
            checks.append(
                {
                    "name": "state_directory",
                    "ok": stat.S_IMODE(paths.state_dir.stat().st_mode) & 0o077 == 0,
                    "path": str(paths.state_dir),
                    "mode": oct(stat.S_IMODE(paths.state_dir.stat().st_mode)),
                }
            )
            result = {"ok": all(item["ok"] for item in checks), "checks": checks}
            _json_print(result)
            return 0 if result["ok"] else 1

        if args.command == "serve":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status": "starting",
                        "socket": str(socket_path),
                        "target": asdict(config),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            serve(target=config, socket_path=socket_path, paths=paths)
            return 0

        if args.command == "ping":
            result = _socket_request(
                socket_path,
                {"action": "ping"},
                timeout_seconds=2,
            )
            _json_print(result)
            return 0 if result.get("ok") else 1

        dispatcher = Dispatcher(backend=backend, paths=paths)
        transport = _selected_transport(args, config)

        if args.command == "send":
            prompt = _read_prompt(args)
            if transport == "daemon":
                result = _socket_request(
                    socket_path,
                    {
                        "action": "send",
                        "prompt": prompt,
                        "timeout_seconds": args.timeout,
                        "assignment_id": args.assignment_id,
                    },
                    timeout_seconds=args.timeout + 30,
                )
            else:
                result = dispatcher.send(
                    prompt,
                    target=config,
                    timeout_seconds=args.timeout,
                    assignment_id=args.assignment_id,
                )
            _result_output(result, raw=args.raw)
            return 0 if result.get("ok") else 1

        if args.command == "collect":
            if transport == "daemon":
                result = _socket_request(
                    socket_path,
                    {
                        "action": "collect",
                        "assignment_id": args.assignment_id,
                    },
                    timeout_seconds=30,
                )
            else:
                result = dispatcher.collect(args.assignment_id, target=config)
            _result_output(result, raw=args.raw)
            return 0 if result.get("ok") else 1

        if args.command == "smoke":
            marker = f"CODEX_PRO_DISPATCH_OK_{secrets.token_hex(6).upper()}"
            prompt = f"Desktop bridge smoke test. Reply exactly with this marker and nothing else:\n{marker}"
            if transport == "daemon":
                result = _socket_request(
                    socket_path,
                    {
                        "action": "send",
                        "prompt": prompt,
                        "timeout_seconds": args.timeout,
                    },
                    timeout_seconds=args.timeout + 30,
                )
            else:
                result = dispatcher.send(
                    prompt,
                    target=config,
                    timeout_seconds=args.timeout,
                )
            response = str(result.get("response", "")).strip()
            verified = bool(result.get("ok")) and marker in response
            output = {
                **result,
                "smoke_marker": marker,
                "smoke_verified": verified,
            }
            _json_print(output)
            return 0 if verified else 1

        parser.error(f"Unknown command: {args.command}")
        return 2
    except DispatchError as exc:
        _json_print(error_payload(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        _json_print({"ok": False, "error": "interrupted"})
        return 130
    except Exception as exc:
        _json_print(error_payload(exc))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
