from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .collection import NativeCollectionEvidence
from .artifact import ArtifactContract
from .core import (
    DispatchError,
    abandon_assignment,
    active_cooldown,
    active_assignment,
    arm_assignment,
    complete_assignment,
    cleanup_result,
    collect_turn,
    default_paths,
    list_assignments,
    load_assignment,
    load_worker,
    mark_ambiguous,
    mark_indeterminate,
    mark_unusual_activity_403,
    mark_pending,
    mark_submitted,
    materialize_result,
    prepare_assignment,
    purge_local_state,
    redact_stored_diagnostics,
    recovery_info,
    record_parent_restoration,
    reset_worker,
    save_worker,
    verify_artifact,
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


def add_reason_source(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--reason")
    source.add_argument(
        "--reason-file",
        help="UTF-8 reason file, or - for stdin; preferred for untrusted text",
    )


def reason_from_args(args: argparse.Namespace) -> str:
    if args.reason_file is not None:
        return read_text_source(args.reason_file)
    return args.reason


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
    worker_set.add_argument(
        "--native-controls-confirmed",
        action="store_true",
        help="Confirm this Codex task passed the native host-capability preflight",
    )

    worker_sub.add_parser("show", help="Show the configured worker")
    worker_reset = worker_sub.add_parser("reset", help="Remove the configured worker")
    worker_reset.add_argument("--force", action="store_true")

    prepare = subparsers.add_parser(
        "prepare", help="Create one at-most-once native-send assignment"
    )
    prepare.add_argument("--parent-task-id", required=True)
    prepare.add_argument("--prompt-file", default="-", help="UTF-8 prompt file, or - for stdin")
    prepare.add_argument("--continuation-of")
    prepare.add_argument("--assignment-id")
    prepare.add_argument(
        "--result-mode",
        choices=("inline", "artifact", "chunked"),
        default="inline",
        help="Explicit complete-result transport; auto is intentionally unsupported",
    )
    prepare.add_argument(
        "--artifact-contract-file",
        help="Strict UTF-8 artifact contract; valid only with --result-mode artifact",
    )
    prepare.add_argument(
        "--authorize-artifact-write",
        action="store_true",
        help="Record this assignment's explicit one-commit artifact authorization",
    )
    prepare.add_argument(
        "--worker-github-write-confirmed",
        action="store_true",
        help="Confirm the configured worker has the contract's write capability",
    )
    prepare.add_argument(
        "--allow-public-artifact",
        action="store_true",
        help="Acknowledge that public Git content has durable public retention",
    )
    prepare.add_argument(
        "--native-controls-confirmed",
        action="store_true",
        help="Confirm this invocation passed the native host-capability preflight",
    )

    arm = subparsers.add_parser(
        "arm", help="Durably prohibit resends immediately before native submission"
    )
    arm.add_argument("assignment_id")
    arm.add_argument("--turn-id")

    submitted = subparsers.add_parser(
        "submitted", help="Verify the native read-back and record one submission"
    )
    submitted.add_argument("assignment_id")
    submitted.add_argument("--turn-id")
    submitted.add_argument(
        "--sent-prompt-file",
        required=True,
        help="Exact UTF-8 native read-back of the submitted user message, or - for stdin",
    )
    submitted.add_argument(
        "--native-user-message-id",
        help=(
            "Stable native ID of the exact user message read back from the worker; "
            "without it a send is recorded collect-only and cannot be verified"
        ),
    )

    pending = subparsers.add_parser("pending", help="Record that the worker is still running")
    pending.add_argument("assignment_id")
    pending.add_argument("--turn-id")

    indeterminate = subparsers.add_parser(
        "indeterminate", help="Record that submission may have occurred; never resend"
    )
    indeterminate.add_argument("assignment_id")
    indeterminate.add_argument("--turn-id")
    add_reason_source(indeterminate)

    unusual_activity = subparsers.add_parser(
        "unusual-activity",
        help="Record native unusual-activity HTTP 403 and start a 30-minute cooldown",
    )
    unusual_activity.add_argument("assignment_id")
    unusual_activity.add_argument("--turn-id")
    unusual_activity.add_argument(
        "--request-id", help="OpenAI request ID from the native HTTP 403 response"
    )
    add_reason_source(unusual_activity)

    ambiguous = subparsers.add_parser(
        "ambiguous", help="Record an unvalidated response; never resend"
    )
    ambiguous.add_argument("assignment_id")
    ambiguous.add_argument("--turn-id")
    add_reason_source(ambiguous)

    complete = subparsers.add_parser(
        "complete", help="Deprecated evidence-gated inline completion alias"
    )
    complete.add_argument("assignment_id")
    complete.add_argument(
        "--native-evidence-file",
        required=True,
        help="Strict UTF-8 JSON evidence from one native collection operation",
    )
    complete.add_argument(
        "--response-file",
        help="Optional exact body cross-check; body-only completion is never allowed",
    )

    collect = subparsers.add_parser(
        "collect", help="Collect one trusted native turn result without logging its body"
    )
    collect.add_argument("assignment_id")
    collect.add_argument("--turn-id", required=True)
    collect.add_argument("--native-evidence-file", required=True)
    collect.add_argument(
        "--result-file",
        help="Exclusive private result file; required to materialize inline or final chunk content",
    )

    artifact = subparsers.add_parser("artifact", help="Verify an explicitly authorized Git artifact")
    artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_verify = artifact_sub.add_parser("verify", help="Verify exact remote commit/tree/blob")
    artifact_verify.add_argument("assignment_id")
    artifact_verify.add_argument("--result-file", required=True)
    artifact_verify.add_argument("--discover", action="store_true")

    result = subparsers.add_parser("result", help="Materialize or clean up a completed result")
    result_sub = result.add_subparsers(dest="result_command", required=True)
    materialize = result_sub.add_parser("materialize", help="Create an exclusive private result copy")
    materialize.add_argument("assignment_id")
    materialize.add_argument("--result-file", required=True)
    cleanup = result_sub.add_parser("cleanup", help="Remove verified chunk spool after parent restoration")
    cleanup.add_argument("assignment_id")
    parent_restored = result_sub.add_parser(
        "parent-restored", help="Record host-observed parent restoration without reopening content"
    )
    parent_restored.add_argument("assignment_id")
    parent_restored.add_argument("--native-controls-confirmed", action="store_true")

    recover = subparsers.add_parser(
        "recover", help="Show the saved worker and parent IDs without resending"
    )
    recover.add_argument("assignment_id")

    abandon = subparsers.add_parser("abandon", help="Close an unresolved assignment")
    abandon.add_argument("assignment_id")
    add_reason_source(abandon)

    status = subparsers.add_parser("status", help="Show one assignment or all local state")
    status.add_argument("assignment_id", nargs="?")

    doctor = subparsers.add_parser(
        "doctor", help="Check local state and the current host-capability assertion"
    )
    doctor.add_argument(
        "--native-controls-confirmed",
        action="store_true",
        help="Assert that the invoking skill verified every required native capability",
    )

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


def evidence_from_path(path: str) -> NativeCollectionEvidence:
    return NativeCollectionEvidence.from_json_bytes(
        Path(path).read_bytes() if path != "-" else sys.stdin.buffer.read()
    )


def turn_payload(prepared: Any) -> dict[str, Any]:
    return {
        "turn_id": prepared.turn_id,
        "sequence": prepared.sequence,
        "status": "prepared",
        "wrapped_prompt": prepared.wrapped_prompt,
        "wrapped_prompt_sha256": hashlib.sha256(
            prepared.wrapped_prompt.encode("utf-8")
        ).hexdigest(),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    paths = default_paths()

    if args.command == "worker":
        if args.worker_command == "set":
            if not args.native_controls_confirmed:
                raise DispatchError(
                    "Worker setup requires the skill's native host-capability preflight; "
                    "invoke $codex-pro-dispatch inside a supported Codex desktop task"
                )
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
        if not args.native_controls_confirmed:
            raise DispatchError(
                "Assignment preparation requires the current invocation's native "
                "host-capability preflight"
            )
        prompt = read_text_source(args.prompt_file)
        contract = (
            ArtifactContract.from_json_bytes(Path(args.artifact_contract_file).read_bytes())
            if args.artifact_contract_file
            else None
        )
        prepared = prepare_assignment(
            prompt,
            parent_task_id=args.parent_task_id,
            result_mode=args.result_mode,
            continuation_of=args.continuation_of,
            assignment_id=args.assignment_id,
            artifact_contract=contract,
            authorize_artifact_write=args.authorize_artifact_write,
            worker_github_write_confirmed=args.worker_github_write_confirmed,
            allow_public_artifact=args.allow_public_artifact,
            paths=paths,
        )
        return {
            "ok": True,
            "status": "prepared",
            "assignment_id": prepared.assignment_id,
            "worker_conversation_id": prepared.worker_conversation_id,
            "parent_task_id": prepared.parent_task_id,
            "continuation_of": prepared.continuation_of,
            "result_mode": prepared.result_mode,
            "receipt_path": str(prepared.receipt_path),
            "turn": turn_payload(prepared),
            # Retained for one release for scripts that consumed the old prepare
            # response. The receipt itself never stores this body.
            "wrapped_prompt": prepared.wrapped_prompt,
        }

    if args.command == "submitted":
        sent_prompt = read_exact_text_source(args.sent_prompt_file)
        value = mark_submitted(
            args.assignment_id,
            sent_prompt,
            paths,
            turn_id=args.turn_id,
            native_user_message_id=args.native_user_message_id,
        )
        return {"ok": True, "assignment": value}

    if args.command == "arm":
        value = arm_assignment(args.assignment_id, paths, turn_id=args.turn_id)
        return {"ok": True, "assignment": value, "no_resend": True}

    if args.command == "pending":
        value = mark_pending(args.assignment_id, paths, turn_id=args.turn_id)
        return {"ok": True, "assignment": value}

    if args.command == "indeterminate":
        value = mark_indeterminate(
            args.assignment_id,
            reason=reason_from_args(args),
            paths=paths,
            turn_id=args.turn_id,
        )
        return {"ok": True, "assignment": value, "collect_only": True}

    if args.command == "unusual-activity":
        value = mark_unusual_activity_403(
            args.assignment_id,
            reason=reason_from_args(args),
            request_id=args.request_id,
            paths=paths,
            turn_id=args.turn_id,
        )
        return {
            "ok": True,
            "assignment": value,
            "native_http_status": 403,
            "cooldown": active_cooldown(paths),
            "collect_only": True,
        }

    if args.command == "ambiguous":
        value = mark_ambiguous(
            args.assignment_id,
            reason=reason_from_args(args),
            paths=paths,
            turn_id=args.turn_id,
        )
        return {"ok": True, "assignment": value, "collect_only": True}

    if args.command == "complete":
        evidence = evidence_from_path(args.native_evidence_file)
        response = read_text_source(args.response_file) if args.response_file else None
        value, _payload = complete_assignment(
            args.assignment_id, response, paths, evidence=evidence
        )
        return {
            "ok": True,
            "assignment": value,
            "completion_basis": "native-inline",
            "byte_length": value.get("result", {}).get("byte_length"),
            "sha256": value.get("result", {}).get("payload_sha256"),
        }

    if args.command == "collect":
        outcome = collect_turn(
            args.assignment_id,
            args.turn_id,
            evidence_from_path(args.native_evidence_file),
            Path(args.result_file) if args.result_file else None,
            paths=paths,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "status": outcome.status,
            "assignment_id": outcome.assignment_id,
            "turn_id": outcome.turn_id,
            "completion_basis": outcome.completion_basis,
            "byte_length": outcome.byte_length,
            "sha256": outcome.sha256,
        }
        if outcome.result_path is not None:
            payload["result_file"] = str(outcome.result_path)
        if outcome.accepted_chunk is not None:
            payload["accepted_chunk"] = dict(outcome.accepted_chunk)
        if outcome.next_turn is not None:
            payload["action"] = "send_next_turn"
            payload["next_turn"] = turn_payload(outcome.next_turn)
        return payload

    if args.command == "artifact":
        verification = verify_artifact(
            args.assignment_id,
            Path(args.result_file),
            discover=args.discover,
            paths=paths,
        )
        return {
            "ok": True,
            "status": "complete",
            "assignment_id": args.assignment_id,
            "completion_basis": "artifact-discovery" if args.discover else "artifact-manifest",
            "result_file": args.result_file,
            **verification.receipt_fields(),
        }

    if args.command == "result":
        if args.result_command == "materialize":
            descriptor = materialize_result(
                args.assignment_id, Path(args.result_file), paths=paths
            )
            return {
                "ok": True,
                "assignment_id": descriptor.assignment_id,
                "mode": descriptor.mode,
                "result_file": str(descriptor.path),
                "byte_length": descriptor.byte_length,
                "sha256": descriptor.sha256,
                "completion_basis": descriptor.completion_basis,
            }
        if args.result_command == "cleanup":
            cleaned = cleanup_result(args.assignment_id, paths=paths)
            return {
                "ok": True,
                "assignment_id": cleaned.assignment_id,
                "removed_spool_files": cleaned.removed_spool_files,
                "result_retained": cleaned.result_retained,
            }
        if not args.native_controls_confirmed:
            raise DispatchError(
                "Parent restoration must be confirmed by the current native host-capability preflight"
            )
        receipt = record_parent_restoration(
            args.assignment_id, restored=True, paths=paths
        )
        return {
            "ok": True,
            "assignment_id": args.assignment_id,
            "delivery": receipt.get("delivery"),
        }

    if args.command == "recover":
        return {"ok": True, "recovery": recovery_info(args.assignment_id, paths)}

    if args.command == "abandon":
        value = abandon_assignment(
            args.assignment_id, reason=reason_from_args(args), paths=paths
        )
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
            "active_cooldown": active_cooldown(paths),
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
            "active_cooldown": None,
            "redacted_diagnostic_receipts": 0,
        }
        try:
            checks["redacted_diagnostic_receipts"] = redact_stored_diagnostics(paths)
        except DispatchError as exc:
            checks["state_error"] = str(exc)
        try:
            checks["worker"] = worker_payload(load_worker(paths))
            checks["worker_configured"] = True
        except DispatchError as exc:
            checks["worker_error"] = str(exc)
        try:
            current = active_assignment(paths)
            checks["active_assignment"] = current
            checks["active_cooldown"] = active_cooldown(paths)
        except DispatchError as exc:
            checks["state_error"] = str(exc)
        checks["local_ok"] = (
            checks["platform"] == "Darwin"
            and checks["worker_configured"]
            and "worker_error" not in checks
            and "state_error" not in checks
        )
        checks["native_controls_confirmed"] = bool(
            args.native_controls_confirmed
        )
        checks["native_controls"] = (
            "confirmed for this invocation by the Codex skill"
            if args.native_controls_confirmed
            else "not confirmed; run through $codex-pro-dispatch in a supported host"
        )
        checks["ok"] = bool(
            checks["local_ok"] and checks["native_controls_confirmed"]
        )
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
        payload = run(args)
        emit(payload)
        if args.command == "doctor" and not payload.get("ok", False):
            return 1
        return 0
    except DispatchError as exc:
        emit(
            {
                "ok": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "error_code": exc.error_code,
                "details": exc.details,
            },
            stream=sys.stderr,
        )
        return exc.exit_code
    except (OSError, UnicodeError) as exc:
        emit(
            {
                "ok": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "error_code": "runtime_error",
            },
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
