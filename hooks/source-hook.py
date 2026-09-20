#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import tempfile


def fail(message: str) -> "NoReturn":
    raise SystemExit(message)


def commands_for(supervisor: str) -> set[str]:
    commands = {f"node {shlex.quote(supervisor)} stop"}
    if not any(character in supervisor for character in '"$`\\'):
        commands.add(f'node "{supervisor}" stop')
    return commands


def load(target: Path) -> tuple[dict[str, object], list[object]]:
    if target.is_symlink():
        fail(f"Refusing symlink hook configuration: {target}")
    if target.exists():
        if not target.is_file():
            fail(f"Refusing non-file hook configuration: {target}")
        try:
            config = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            fail(f"Malformed hook configuration {target}: {exc}")
    else:
        config = {}

    if not isinstance(config, dict):
        fail(f"Malformed hook configuration {target}: root must be an object")
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        fail(f"Malformed hook configuration {target}: hooks must be an object")
    stop = hooks.setdefault("Stop", [])
    if not isinstance(stop, list):
        fail(f"Malformed hook configuration {target}: hooks.Stop must be an array")
    return config, stop


def inspect(
    target: Path, stop: list[object], expected_commands: set[str], expected_fields: dict[str, object]
) -> tuple[int, int]:
    owned_group = -1
    owned_handler = -1
    matches = 0
    for group_index, group in enumerate(stop):
        if not isinstance(group, dict):
            fail(f"Malformed hook configuration {target}: hooks.Stop[{group_index}] must be an object")
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            fail(
                f"Malformed hook configuration {target}: "
                f"hooks.Stop[{group_index}].hooks must be an array"
            )
        for handler_index, handler in enumerate(handlers):
            if not isinstance(handler, dict):
                fail(
                    f"Malformed hook configuration {target}: "
                    f"hooks.Stop[{group_index}].hooks[{handler_index}] must be an object"
                )
            command = handler.get("command")
            if isinstance(command, str) and "resident-supervision.mjs" in command:
                fields = dict(handler)
                fields.pop("command", None)
                if command not in expected_commands or fields != expected_fields:
                    fail(f"Conflicting resident supervision hook in {target}; refusing to replace it")
                matches += 1
                owned_group, owned_handler = group_index, handler_index
    if matches > 1:
        fail(f"Conflicting duplicate resident supervision hooks in {target}")
    return owned_group, owned_handler


def write(target: Path, config: dict[str, object]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = (target.stat().st_mode & 0o777) if target.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".hooks.json.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if len(sys.argv) != 4 or sys.argv[1] not in {"install", "check-remove", "remove"}:
    fail(f"Usage: {sys.argv[0]} install|check-remove|remove HOOK_FILE SUPERVISOR")

action = sys.argv[1]
target = Path(sys.argv[2])
supervisor = str(Path(sys.argv[3]).resolve(strict=True))
expected_commands = commands_for(supervisor)
expected_fields: dict[str, object] = {"type": "command", "timeout": 15, "async": False}
canonical = {**expected_fields, "command": f"node {shlex.quote(supervisor)} stop"}
config, stop = load(target)
group_index, handler_index = inspect(target, stop, expected_commands, expected_fields)

if action == "install" and group_index < 0:
    stop.append({"hooks": [canonical]})
    write(target, config)
elif action == "remove" and group_index >= 0:
    group = stop[group_index]
    handlers = group["hooks"]
    del handlers[handler_index]
    if not handlers:
        del stop[group_index]
    write(target, config)
