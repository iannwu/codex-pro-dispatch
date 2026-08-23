from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .core import (
    DispatchError,
    abandon_assignment,
    active_assignment,
    complete_assignment,
    default_paths,
    list_assignments,
    load_assignment,
    load_worker,
    mark_ambiguous,
    mark_indeterminate,
    mark_pending,
    mark_submitted,
    prepare_assignment,
    purge_local_state,
    recovery_info,
    reset_worker,
    save_worker,
)


def emit(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    stream.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def read_text_source(path: str | None) -> str:
    if path in {None, "-"}:
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def read_exact_text_source(path: str) -> str:
    if path == "-":
        return sys.stdin.buffer.read().decode("utf-8")
    return Path(path).read_bytes().decode("utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pro-dispatch",
        description="State and safety helper for the official-app Codex Pro Dispatch skill.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    worker = subparsers.add_parser("worker", help="Configure the dedicated Chat Pro worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)

    worker_set = worker_sub.add_parser("set", help="Save a user-confirmed Pro worker")
    worker_set.add_argument("--conversation-id", required=True)
    worker_set.add_argument("--label", default="Codex Pro Dispatch Worker")
    worker_set.add_argument(
        "--confirm-pro",
        action="store_true",
        help="Confirm the user visibly selected Pro in this Chat conversation",
    )

    worker_sub.add_parser("show", help="Show the configured worker")
    worker_reset = worker_sub.add_parser("reset", help="Remove the configured worker")
    worker_reset.add_argument("--force", action="store_true")

    prepare = subparsers.add_parser("prepare", help="Create one exactly-once assignment")
    prepare.add_argument("--parent-task-id", required=True)
    prepare.add_argument("--prompt-file", default="-", help="UTF-8 prompt file, or - for stdin")
    prepare.add_argument("--continuation-of")
    prepare.add_argument("--assignment-id")

    submitted = subparsers.add_parser(
        "submitted", help="Verify the native read-back and record one submission"
    )
    submitted.add_argument("assignment_id")
    submitted.add_argument(
        "--sent-prompt-file",
        required=True,
        help="Exact UTF-8 native read-back of the submitted user message, or - for stdin",
    )

    pending = subparsers.add_parser("pending", help="Record that the worker is still running")
    pending.add_argument("assignment_id")

    indeterminate = subparsers.add_parser(
        "indeterminate", help="Record that submission may have occurred; never resend"
    )
    indeterminate.add_argument("assignment_id")
    indeterminate.add_argument("--reason", required=True)

    ambiguous = subparsers.add_parser(
        "ambiguous", help="Record an unvalidated response; never resend"
    )
    ambiguous.add_argument("assignment_id")
    ambiguous.add_argument("--reason", required=True)

    complete = subparsers.add_parser(
        "complete", help="Validate the result marker and complete an assignment"
    )
    complete.add_argument("assignment_id")
    complete.add_argument("--response-file", default="-", help="UTF-8 response file, or - for stdin")

    recover = subparsers.add_parser(
        "recover", help="Show the saved worker and parent IDs without resending"
    )
    recover.add_argument("assignment_id")

    abandon = subparsers.add_parser("abandon", help="Close an unresolved assignment")
    abandon.add_argument("assignment_id")
    abandon.add_argument("--reason", required=True)

    status = subparsers.add_parser("status", help="Show one assignment or all local state")
    status.add_argument("assignment_id", nargs="?")

    subparsers.add_parser("doctor", help="Check local helper configuration")

    purge = subparsers.add_parser("purge", help="Remove private worker and assignment state")
    purge.add_argument("--yes", action="store_true")
    purge.add_argument("--force", action="store_true")

    return parser


def worker_payload(worker: Any) -> dict[str, Any]:
    return {
        "conversation_id": worker.conversation_id,
        "label": worker.label,
        "model_confirmation": worker.model_confirmation,
        "configured_at": worker.configured_at,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    paths = default_paths()

    if args.command == "worker":
        if args.worker_command == "set":
            worker = save_worker(
                args.conversation_id,
                label=args.label,
                confirm_pro=args.confirm_pro,
                paths=paths,
            )
            return {"ok": True, "worker": worker_payload(worker), "path": str(paths.worker_file)}
        if args.worker_command == "show":
            worker = load_worker(paths)
            return {"ok": True, "worker": worker_payload(worker), "path": str(paths.worker_file)}
        removed = reset_worker(force=args.force, paths=paths)
        return {"ok": True, "removed": removed, "path": str(paths.worker_file)}

    if args.command == "prepare":
        prompt = read_text_source(args.prompt_file)
        prepared = prepare_assignment(
            prompt,
            parent_task_id=args.parent_task_id,
            continuation_of=args.continuation_of,
            assignment_id=args.assignment_id,
            paths=paths,
        )
        return {
            "ok": True,
            "status": "prepared",
            "assignment_id": prepared.assignment_id,
            "worker_conversation_id": prepared.worker_conversation_id,
            "parent_task_id": prepared.parent_task_id,
            "continuation_of": prepared.continuation_of,
            "receipt_path": str(prepared.receipt_path),
            "wrapped_prompt": prepared.wrapped_prompt,
        }

    if args.command == "submitted":
        sent_prompt = read_exact_text_source(args.sent_prompt_file)
        value = mark_submitted(args.assignment_id, sent_prompt, paths)
        return {"ok": True, "assignment": value}

    if args.command == "pending":
        value = mark_pending(args.assignment_id, paths)
        return {"ok": True, "assignment": value}

    if args.command == "indeterminate":
        value = mark_indeterminate(args.assignment_id, reason=args.reason, paths=paths)
        return {"ok": True, "assignment": value, "collect_only": True}

    if args.command == "ambiguous":
        value = mark_ambiguous(args.assignment_id, reason=args.reason, paths=paths)
        return {"ok": True, "assignment": value, "collect_only": True}

    if args.command == "complete":
        response = read_text_source(args.response_file)
        value, payload = complete_assignment(args.assignment_id, response, paths)
        return {"ok": True, "assignment": value, "payload": payload}

    if args.command == "recover":
        return {"ok": True, "recovery": recovery_info(args.assignment_id, paths)}

    if args.command == "abandon":
        value = abandon_assignment(args.assignment_id, reason=args.reason, paths=paths)
        return {"ok": True, "assignment": value}

    if args.command == "status":
        if args.assignment_id:
            return {"ok": True, "assignment": load_assignment(args.assignment_id, paths)}
        worker: dict[str, Any] | None
        try:
            worker = worker_payload(load_worker(paths))
        except DispatchError:
            worker = None
        return {
            "ok": True,
            "worker": worker,
            "active_assignment": active_assignment(paths),
            "assignments": list_assignments(paths),
            "paths": {
                "config_dir": str(paths.config_dir),
                "state_dir": str(paths.state_dir),
            },
        }

    if args.command == "doctor":
        checks: dict[str, Any] = {
            "platform": platform.system(),
            "python": platform.python_version(),
            "worker_configured": False,
            "active_assignment": None,
        }
        try:
            checks["worker"] = worker_payload(load_worker(paths))
            checks["worker_configured"] = True
        except DispatchError as exc:
            checks["worker_error"] = str(exc)
        try:
            current = active_assignment(paths)
            checks["active_assignment"] = current
        except DispatchError as exc:
            checks["state_error"] = str(exc)
        checks["native_controls"] = "manual acceptance required inside Codex Desktop"
        checks["ok"] = checks["platform"] == "Darwin" and checks["worker_configured"]
        return checks

    if args.command == "purge":
        if not args.yes:
            raise DispatchError("purge requires --yes")
        result = purge_local_state(force=args.force, paths=paths)
        return {"ok": True, **result}

    raise DispatchError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        emit(run(args))
        return 0
    except DispatchError as exc:
        emit(
            {
                "ok": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "details": exc.details,
            },
            stream=sys.stderr,
        )
        return exc.exit_code
    except (OSError, UnicodeError) as exc:
        emit(
            {"ok": False, "error": str(exc), "error_type": exc.__class__.__name__},
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
