"""Schema-v2 logical dispatch state and long-result transport.

This module owns the v1.2 receipt format.  It intentionally keeps response,
prompt, payload, and artifact bodies outside JSON receipts; every durable record
contains identifiers, state, hashes, and private-file names only.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .artifact import (
    ArtifactContract,
    ArtifactManifest,
    ArtifactVerificationResult,
    GitArtifactVerifier,
    parse_artifact_manifest,
    validate_artifact_manifest,
)
from .chunked import (
    CHAIN_ZERO_HEX,
    CHUNKED_REQUIRED_CONTROL,
    ChunkEnvelope,
    chunk_chain,
    continuation_prompt,
    is_chunked_required_control,
    parse_chunk_response,
)
from .collection import (
    NATIVE_COLLECTION_SCHEMA,
    NativeCollectionEvidence,
    adapter_contract,
    canonical_json_bytes,
    normalize_newlines,
)
from .core import (
    ASSIGNMENT_SCHEMA_VERSION,
    PreparedAssignment,
    RuntimePaths,
    _format_utc,
    _parse_utc,
    _redact_diagnostic_fields,
    atomic_write_json,
    assignment_path,
    default_paths,
    load_worker,
    new_assignment_id,
    read_json,
    result_marker,
    parse_result,
    sha256_text,
    state_lock,
    utc_now,
    validate_identifier,
    validate_native_message_id,
    wrap_prompt,
)
from .errors import (
    ArtifactProtocolError,
    ArtifactVerificationError,
    BusyError,
    ChunkProtocolError,
    CollectionEvidenceError,
    ConfigurationError,
    CooldownError,
    DispatchError,
    MarkerError,
    ReceiptMigrationError,
    StateError,
    TruncationError,
)


RESULT_MODES = frozenset({"inline", "artifact", "chunked"})
ARTIFACT_VERIFICATION_STALE_SECONDS = 5 * 60
DISPATCH_ACTIVE_STATUSES = frozenset({"prepared", "active", "recoverable", "verifying"})
DISPATCH_TERMINAL_STATUSES = frozenset({"complete", "abandoned", "failed"})
DISPATCH_STATUSES = DISPATCH_ACTIVE_STATUSES | DISPATCH_TERMINAL_STATUSES
TURN_ACTIVE_STATUSES = frozenset(
    {"prepared", "armed", "submitted", "pending", "indeterminate", "ambiguous"}
)
TURN_TERMINAL_STATUSES = frozenset({"complete", "response_rejected", "failed"})
TURN_STATUSES = TURN_ACTIVE_STATUSES | TURN_TERMINAL_STATUSES
COLLECTION_STATUSES = frozenset(
    {
        "not_started",
        "accepted",
        "truncated",
        "truncation_unknown",
        "chunked-required-control",
        "inline-limit-exceeded",
        "rejected",
    }
)
# Only these body-free collection outcomes are allowed to reject a proven
# completed response and create a recovery child.  A generic "rejected"
# outcome is deliberately included for a malformed chunk frame: the native
# message itself was final and bound, but its transport framing was unsafe.
RECOVERY_REJECTION_COLLECTION_STATUSES = frozenset(
    {
        "truncated",
        "chunked-required-control",
        "inline-limit-exceeded",
        "rejected",
    }
)
RECOVERY_AUTHORITIES = frozenset(
    {
        "not-established",
        "exact-readback",
        "exact-readback-recovered",
        "collect-only",
        "legacy-unverified",
    }
)
RECOVERY_EXACT_AUTHORITIES = frozenset(
    {"exact-readback", "exact-readback-recovered"}
)
_SPOOL_PART_NAME = re.compile(r"^chunk-[0-9]{6}\.part$")

# Bodies must never enter receipts. Hashes, byte lengths, and private spool names
# are deliberately allowed; these exact names prevent accidental durable bodies.
_BODY_KEYS = frozenset(
    {
        "prompt",
        "wrapped_prompt",
        "sent_prompt",
        "response",
        "payload",
        "text",
        "content",
        "last_error",
        "reason",
    }
)


@dataclass(frozen=True)
class CollectionOutcome:
    assignment_id: str
    turn_id: str
    status: str
    completion_basis: str | None
    result_path: Path | None
    byte_length: int | None
    sha256: str | None
    accepted_chunk: Mapping[str, Any] | None = None
    next_turn: PreparedAssignment | None = None


@dataclass(frozen=True)
class ResultDescriptor:
    assignment_id: str
    mode: str
    path: Path
    byte_length: int
    sha256: str
    completion_basis: str


@dataclass(frozen=True)
class CleanupResult:
    assignment_id: str
    removed_spool_files: int
    result_retained: bool


def _state_error(code: str, message: str, **details: Any) -> None:
    raise StateError(message, details=details, error_code=code)


def _migration_error(code: str, message: str, **details: Any) -> None:
    raise ReceiptMigrationError(message, details=details, error_code=code)


def _require_mode(value: str) -> str:
    if value not in RESULT_MODES:
        raise ConfigurationError(
            "result_mode must be one of inline, artifact, or chunked",
            details={"result_mode": value},
        )
    return value


def _validate_dispatch_status(value: object) -> str:
    if value not in DISPATCH_STATUSES:
        _migration_error("receipt_schema_unsupported", "Dispatch receipt has invalid status")
    return str(value)


def _validate_turn_status(value: object) -> str:
    if value not in TURN_STATUSES:
        _migration_error("receipt_schema_unsupported", "Dispatch turn has invalid status")
    return str(value)


def _is_lower_hex(value: object, length: int = 64) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


_CHUNK_EXPECTATION_FIELDS = frozenset(
    {"expected_index", "expected_previous_chain_sha256", "retransmission"}
)
_CHUNK_ACTUAL_FIELDS = frozenset(
    {
        "index",
        "previous_chain_sha256",
        "chain_sha256",
        "payload_sha256",
        "byte_length",
        "final",
        "count",
    }
)
_CHUNK_SPOOL_FIELDS = frozenset(
    {"spool_write_pending", "spool_filename", "spool_status", "accepted"}
)


def _validate_chunk_receipt_shape(chunk: Mapping[str, Any]) -> None:
    """Validate body-free chunk fields before they influence control flow.

    Receipt JSON names only hashes and private filenames, but those fields still
    decide which continuation can be armed.  Keep every partial journal and
    durable accepted form explicit so malformed metadata fails before any
    ``int(...)`` coercion or chain-boundary calculation can occur.
    """

    keys = set(chunk)
    has_expectation = bool(keys & _CHUNK_EXPECTATION_FIELDS)
    has_actual = bool(keys & _CHUNK_ACTUAL_FIELDS)
    has_spool = bool(keys & _CHUNK_SPOOL_FIELDS)

    if has_expectation:
        if not _CHUNK_EXPECTATION_FIELDS <= keys:
            _migration_error(
                "receipt_schema_unsupported",
                "Chunk expectation is incomplete",
            )
        if (
            type(chunk.get("expected_index")) is not int
            or int(chunk["expected_index"]) < 1
            or not _is_lower_hex(chunk.get("expected_previous_chain_sha256"))
            or type(chunk.get("retransmission")) is not bool
        ):
            _migration_error(
                "receipt_schema_unsupported",
                "Chunk expectation is invalid",
            )

    if not has_actual:
        if not has_expectation or has_spool:
            _migration_error(
                "receipt_schema_unsupported",
                "Chunk receipt has no valid expectation or accepted payload",
            )
        return

    if not _CHUNK_ACTUAL_FIELDS <= keys:
        _migration_error(
            "receipt_schema_unsupported",
            "Chunk payload metadata is incomplete",
        )
    index = chunk.get("index")
    previous = chunk.get("previous_chain_sha256")
    chain = chunk.get("chain_sha256")
    payload_digest = chunk.get("payload_sha256")
    byte_length = chunk.get("byte_length")
    final = chunk.get("final")
    count = chunk.get("count")
    if (
        type(index) is not int
        or int(index) < 1
        or not _is_lower_hex(previous)
        or not _is_lower_hex(chain)
        or not _is_lower_hex(payload_digest)
        or type(byte_length) is not int
        or int(byte_length) < 0
        or type(final) is not bool
        or type(count) is not int
        or int(count) < 0
    ):
        _migration_error(
            "receipt_schema_unsupported",
            "Chunk payload metadata is invalid",
        )
    if (final and count != index) or (not final and count != 0):
        _migration_error(
            "receipt_schema_unsupported",
            "Chunk final/count metadata is invalid",
        )
    if not final and byte_length == 0:
        _migration_error(
            "receipt_schema_unsupported",
            "Nonfinal chunk cannot be empty",
        )
    if final and index == 1 and byte_length == 0:
        _migration_error(
            "receipt_schema_unsupported",
            "First and final chunk cannot be empty",
        )
    if has_expectation and (
        chunk.get("expected_index") != index
        or chunk.get("expected_previous_chain_sha256") != previous
    ):
        _migration_error(
            "receipt_schema_unsupported",
            "Chunk payload contradicts its prepared expectation",
        )

    # Key presence matters here.  A literal ``null`` is not an omitted journal:
    # accepting it would make an otherwise accepted chunk look structurally
    # valid until a later mutable path happens to reject it.  Receipts are an
    # untrusted control boundary, so reject that ambiguity on every read.
    if "spool_write_pending" in chunk:
        pending = chunk["spool_write_pending"]
        if (
            not isinstance(pending, Mapping)
            or set(pending) != {"index", "payload_sha256", "byte_length"}
            or "accepted" in chunk
            or "spool_filename" in chunk
            or "spool_status" in chunk
            or pending.get("index") != index
            or pending.get("payload_sha256") != payload_digest
            or pending.get("byte_length") != byte_length
        ):
            _migration_error(
                "receipt_schema_unsupported",
                "Chunk spool journal is inconsistent",
            )
        return

    expected_filename = f"chunk-{int(index):06d}.part"
    if (
        chunk.get("accepted") is not True
        or chunk.get("spool_filename") != expected_filename
        or chunk.get("spool_status") != "spooled"
    ):
        _migration_error(
            "receipt_schema_unsupported",
            "Chunk accepted spool metadata is invalid",
        )


def _body_free(value: Any, *, path: str = "") -> None:
    """Defend the receipt boundary even if a future caller constructs a dict."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _state_error("receipt_invalid", "Receipt keys must be strings")
            if key in _BODY_KEYS:
                _state_error(
                    "receipt_body_forbidden",
                    "Receipts may not contain prompt or result bodies",
                    field=path + key,
                )
            _body_free(child, path=path + key + ".")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _body_free(child, path=f"{path}{index}.")


def _redact_v2_diagnostic_fields(value: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Scrub historical diagnostic/body fields from every v2 receipt boundary.

    New v2 writes are body-free by construction.  This narrow repair path is
    for receipts written by an interrupted or older helper: it preserves the
    established top-level diagnostic hash semantics and handles the same legacy
    raw diagnostic fields inside turns without letting them leak through a
    status/recovery command or doctor output.
    """

    redacted, changed = _redact_diagnostic_fields(value)
    # Old versions never needed a raw request/result field in a durable
    # receipt. Remove such fields rather than attempting to preserve or log
    # their body; structural validation below will still reject a receipt whose
    # required hashes/state can no longer be proven.
    for key in _BODY_KEYS - {"last_error", "reason"}:
        if key in redacted:
            redacted.pop(key, None)
            changed = True
    turns = redacted.get("turns")
    if not isinstance(turns, list):
        return redacted, changed
    cleaned_turns: list[Any] = []
    for turn in turns:
        if not isinstance(turn, Mapping):
            cleaned_turns.append(turn)
            continue
        copied = dict(turn)
        raw_error = copied.pop("last_error", None)
        if raw_error is not None:
            material = str(raw_error).strip()
            if material:
                copied.setdefault("last_error_kind", "legacy-diagnostic-redacted")
                copied.setdefault("last_error_sha256", sha256_text(material))
            changed = True
        # `reason` is never a valid v2 turn field. Remove it rather than
        # translating it to an abandon field that would widen the schema.
        if "reason" in copied:
            copied.pop("reason", None)
            changed = True
        for key in _BODY_KEYS - {"last_error", "reason"}:
            if key in copied:
                copied.pop(key, None)
                changed = True
        cleaned_turns.append(copied)
    if cleaned_turns != turns:
        redacted["turns"] = cleaned_turns
    return redacted, changed


def _known_keys(value: Mapping[str, Any], allowed: set[str], *, location: str) -> None:
    """Reject unmodelled receipt fields instead of letting a body hide in one."""

    unknown = sorted(set(value) - allowed)
    if unknown:
        _migration_error(
            "receipt_schema_unsupported",
            "Receipt has unrecognized fields",
            location=location,
            unknown_fields=unknown,
        )


def _proves_recovery_rejection(
    receipt: Mapping[str, Any],
    turn: Mapping[str, Any],
    collection: object,
) -> bool:
    """Return whether body-free evidence proves a safe recovery rejection.

    A rejected predecessor is a durable send-authority boundary.  Do not infer
    its finality from the turn state, a marker, or an enclosing conversation:
    retain and revalidate the exact completed native evidence that justified the
    rejection.  This intentionally mirrors the receipt validation boundary so
    a hand-edited receipt cannot manufacture a recovery child.
    """

    if not isinstance(collection, Mapping):
        return False
    if (
        collection.get("status") not in RECOVERY_REJECTION_COLLECTION_STATUSES
        or collection.get("accepted") is not False
        or collection.get("collection_schema") != NATIVE_COLLECTION_SCHEMA
        or collection.get("collection_status") != "completed"
        or collection.get("requested_conversation_id")
        != receipt.get("worker_conversation_id")
        or collection.get("loaded_conversation_id") != receipt.get("worker_conversation_id")
        or collection.get("submitted_user_message_id")
        != turn.get("native_user_message_id")
    ):
        return False
    if not all(
        _is_lower_hex(collection.get(field))
        for field in (
            "collection_evidence_sha256",
            "collection_content_identity_sha256",
            "response_sha256",
        )
    ):
        return False
    if (
        type(collection.get("response_byte_length")) is not int
        or int(collection["response_byte_length"]) < 0
        or collection.get("raw_truncated") not in {"true", "false", "omitted"}
        or collection.get("raw_outer_truncated") not in {"true", "false", "omitted"}
        or type(collection.get("normalized_truncated")) not in {bool, type(None)}
        or type(collection.get("normalized_outer_truncated")) not in {bool, type(None)}
    ):
        return False
    if not all(
        isinstance(collection.get(field), str)
        for field in (
            "adapter_contract_id",
            "assistant_message_id",
            "submitted_user_message_id",
            "requested_conversation_id",
            "loaded_conversation_id",
            "collection_observed_at",
            "generation_finality_provenance",
            "outer_integrity_provenance",
        )
    ):
        return False
    try:
        validate_native_message_id(
            collection.get("assistant_message_id"), field="assistant_message_id"
        )
        validate_native_message_id(
            collection.get("submitted_user_message_id"), field="submitted_user_message_id"
        )
        validate_identifier(
            collection.get("requested_conversation_id"), field="requested_conversation_id"
        )
        validate_identifier(
            collection.get("loaded_conversation_id"), field="loaded_conversation_id"
        )
        _parse_utc(str(collection.get("collection_observed_at")), field="collection_observed_at")
        contract = adapter_contract(collection.get("adapter_contract_id"))
    except (ConfigurationError, CollectionEvidenceError):
        return False
    if (
        collection.get("generation_finality_provenance")
        not in contract.generation_finality_provenance
        or collection.get("outer_integrity_provenance")
        not in contract.outer_integrity_provenance
    ):
        return False
    for raw_key, normalized_key, omission_is_false in (
        ("raw_truncated", "normalized_truncated", contract.omitted_message_truncated_is_false),
        (
            "raw_outer_truncated",
            "normalized_outer_truncated",
            contract.omitted_outer_truncated_is_false,
        ),
    ):
        expected = {
            "true": True,
            "false": False,
            "omitted": False if omission_is_false else None,
        }[str(collection.get(raw_key))]
        if collection.get(normalized_key) is not expected:
            return False
    return True


def _receipt_path(assignment_id: str, paths: RuntimePaths | None) -> Path:
    validate_identifier(assignment_id, field="assignment_id")
    return assignment_path(assignment_id, paths)


def _read_raw(assignment_id: str, paths: RuntimePaths | None) -> dict[str, Any]:
    path = _receipt_path(assignment_id, paths)
    value = read_json(path)
    if value.get("assignment_id") != assignment_id:
        _migration_error(
            "legacy_receipt_unmigratable",
            "Assignment receipt identity does not match its path",
            assignment_id=assignment_id,
        )
    schema = value.get("schema_version")
    if schema not in {1, ASSIGNMENT_SCHEMA_VERSION}:
        _migration_error(
            "receipt_schema_unsupported",
            "Assignment receipt schema is unsupported",
            assignment_id=assignment_id,
            schema_version=schema,
        )
    return value


def _legacy_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose historical completion honestly without modifying its bytes."""

    projected, _changed = _redact_diagnostic_fields(value)
    if value.get("status") == "complete":
        legacy = dict(projected.get("legacy") or {})
        legacy.update(
            {
                "origin_schema": 1,
                "completion_basis": "marker-only",
                "collection_integrity": "unverifiable",
            }
        )
        projected["legacy"] = legacy
    return projected


def _turn_from_v1(value: Mapping[str, Any], assignment_id: str) -> dict[str, Any]:
    status = str(value.get("status", ""))
    legacy_turn_status = {
        "prepared": "prepared",
        "armed": "armed",
        "submitted": "submitted",
        "pending": "pending",
        "indeterminate": "indeterminate",
        "ambiguous": "ambiguous",
        "complete": "complete",
        "abandoned": "failed",
        "failed": "failed",
    }.get(status)
    if legacy_turn_status is None:
        _migration_error(
            "legacy_receipt_unmigratable",
            "Legacy receipt has an unknown state",
            assignment_id=assignment_id,
            status=status,
        )
    turn: dict[str, Any] = {
        "turn_id": assignment_id,
        "sequence": 1,
        "purpose": "initial",
        "previous_turn_id": None,
        "status": legacy_turn_status,
        "wrapped_prompt_sha256": str(value.get("wrapped_prompt_sha256", "")),
        "response_marker": str(value.get("response_marker", result_marker(assignment_id))),
        "submission_count": int(value.get("submission_count", 0)),
        "no_resend": bool(value.get("no_resend", False)),
        "outbound_prompt_verified": value.get("outbound_prompt_verified") is True,
        # v1 did not retain the v2 exact read-back provenance latch.  Its
        # submitted/pending shape remains collectable, but cannot acquire a
        # recovery child merely because a migrated status says "submitted".
        "recovery_authority": (
            "legacy-unverified"
            if legacy_turn_status in {"submitted", "pending"}
            else "collect-only"
            if legacy_turn_status in {"indeterminate", "ambiguous"}
            else "not-established"
        ),
        "created_at": str(value.get("created_at", "")),
        "updated_at": str(value.get("updated_at", value.get("created_at", ""))),
        "collection": {"status": "not_started"},
        "chunk": None,
    }
    for key in (
        "armed_at",
        "submitted_at",
        "pending_since",
        "sent_prompt_sha256",
        "native_user_message_id",
        "outbound_prompt_verified_at",
        "readback_verification_attempt_count",
        "readback_artifact_sha256",
        "readback_correction_allowed",
        "readback_correction_kind",
        "readback_correction_applied_at",
        "submission_recovered_from",
    ):
        if key in value:
            turn[key] = value[key]
    collection_keys = {
        "collection_schema",
        "adapter_contract_id",
        "assistant_message_id",
        "submitted_user_message_id",
        "collection_evidence_sha256",
        "collection_content_identity_sha256",
        "collection_observed_at",
        "collection_status",
        "generation_finality_provenance",
        "raw_truncated",
        "normalized_truncated",
        "raw_outer_truncated",
        "normalized_outer_truncated",
        "outer_integrity_provenance",
        "response_byte_length",
        "response_sha256",
        "payload_sha256",
    }
    copied = {key: value[key] for key in collection_keys if key in value}
    if copied:
        # v1 collection fields were not bound to the v1.2 evidence contract.
        # Preserve their body-free audit metadata without allowing them to bind
        # a native assistant ID or block a newly required evidence collection.
        turn["legacy_collection"] = copied
    return turn


def _migrate_v1(value: Mapping[str, Any], assignment_id: str) -> dict[str, Any]:
    """Build a v2 receipt for an unresolved v1 record under the caller's lock."""

    legacy_status = str(value.get("status", ""))
    top_status = {
        "prepared": "prepared",
        "armed": "active",
        "submitted": "active",
        "pending": "active",
        "indeterminate": "recoverable",
        "ambiguous": "recoverable",
        "abandoned": "abandoned",
        "failed": "failed",
    }.get(legacy_status)
    if top_status is None:
        _migration_error(
            "legacy_receipt_unmigratable",
            "Completed v1 receipts are immutable and cannot be migrated",
            assignment_id=assignment_id,
            status=legacy_status,
        )
    turn = _turn_from_v1(value, assignment_id)
    receipt: dict[str, Any] = {
        "schema_version": ASSIGNMENT_SCHEMA_VERSION,
        "record_type": "dispatch",
        "assignment_id": assignment_id,
        "status": top_status,
        "requested_result_mode": "inline",
        "effective_result_mode": "inline",
        "worker_conversation_id": value.get("worker_conversation_id"),
        "worker_label": value.get("worker_label"),
        "worker_model_confirmation": value.get("worker_model_confirmation"),
        "parent_task_id": value.get("parent_task_id"),
        "continuation_of": value.get("continuation_of"),
        "created_at": value.get("created_at"),
        "updated_at": utc_now(),
        "prompt_sha256": value.get("prompt_sha256"),
        "no_original_resend": bool(value.get("no_resend")) or legacy_status != "prepared",
        "send_attempt_total": 1 if bool(value.get("no_resend")) else 0,
        "turns": [turn],
        "artifact_contract": None,
        "result": {"status": "not_complete"},
        "delivery": {"status": "not_delivered", "parent_restoration_status": "not_started"},
        "legacy": {
            "origin_schema": 1,
            "legacy_status": legacy_status,
            "collection_evidence_required": True,
        },
    }
    for key in (
        "native_http_status",
        "native_error_kind",
        "openai_request_id",
        "cooldown_seconds",
        "cooldown_started_at",
        "cooldown_until",
        "last_error_kind",
        "last_error_sha256",
        "abandon_reason_kind",
        "abandon_reason_sha256",
    ):
        if key in value:
            receipt[key] = value[key]
    _body_free(receipt)
    return receipt


def _validate_v2(value: Mapping[str, Any], assignment_id: str) -> dict[str, Any]:
    required_top = {
        "schema_version",
        "record_type",
        "assignment_id",
        "status",
        "requested_result_mode",
        "effective_result_mode",
        "worker_conversation_id",
        "worker_label",
        "worker_model_confirmation",
        "parent_task_id",
        "continuation_of",
        "created_at",
        "updated_at",
        "prompt_sha256",
        "no_original_resend",
        "send_attempt_total",
        "turns",
        "artifact_contract",
        "result",
        "delivery",
        "legacy",
    }
    missing = required_top - set(value)
    if missing:
        _migration_error(
            "receipt_schema_unsupported",
            "Receipt lacks required schema-v2 fields",
            missing_fields=sorted(missing),
        )
    _known_keys(
        value,
        required_top
        | {
            "native_http_status",
            "native_error_kind",
            "openai_request_id",
            "cooldown_seconds",
            "cooldown_started_at",
            "cooldown_until",
            "last_error_kind",
            "last_error_sha256",
            "abandoned_at",
            "abandon_reason_kind",
            "abandon_reason_sha256",
            "artifact_manifest",
            "artifact_verification_pending",
            "artifact_verification_failure",
        },
        location="dispatch",
    )
    if value.get("schema_version") != ASSIGNMENT_SCHEMA_VERSION:
        _migration_error("receipt_schema_unsupported", "Receipt schema is not v2")
    if value.get("record_type") != "dispatch":
        _migration_error("receipt_schema_unsupported", "Receipt record_type must be dispatch")
    if value.get("assignment_id") != assignment_id:
        _migration_error("receipt_schema_unsupported", "Receipt assignment identity differs")
    validate_identifier(assignment_id, field="assignment_id")
    dispatch_status = _validate_dispatch_status(value.get("status"))
    mode = value.get("requested_result_mode")
    effective = value.get("effective_result_mode")
    if mode not in RESULT_MODES or effective not in RESULT_MODES:
        _migration_error("receipt_schema_unsupported", "Receipt has an invalid result mode")
    if mode == "artifact" and effective != "artifact":
        _migration_error("receipt_schema_unsupported", "Artifact receipts cannot silently change mode")
    if mode == "chunked" and effective != "chunked":
        _migration_error("receipt_schema_unsupported", "Chunked receipts cannot silently change mode")
    for field in ("worker_conversation_id", "parent_task_id"):
        candidate = value.get(field)
        if not isinstance(candidate, str):
            _migration_error("receipt_schema_unsupported", f"Receipt {field} is invalid")
        validate_identifier(candidate, field=field)
    if not isinstance(value.get("worker_label"), str) or not value.get("worker_label").strip():
        _migration_error("receipt_schema_unsupported", "Receipt worker label is invalid")
    if value.get("worker_model_confirmation") != "user-confirmed-pro":
        _migration_error("receipt_schema_unsupported", "Receipt worker confirmation is invalid")
    continuation = value.get("continuation_of")
    if continuation is not None:
        if not isinstance(continuation, str):
            _migration_error("receipt_schema_unsupported", "Receipt continuation identity is invalid")
        validate_identifier(continuation, field="continuation_of")
    if not _is_lower_hex(value.get("prompt_sha256")):
        _migration_error("receipt_schema_unsupported", "Receipt prompt digest is invalid")
    if type(value.get("no_original_resend")) is not bool:
        _migration_error("receipt_schema_unsupported", "Receipt no-resend state is invalid")
    send_attempt_total = value.get("send_attempt_total")
    if type(send_attempt_total) is not int or send_attempt_total < 0:
        _migration_error("receipt_schema_unsupported", "Receipt send attempt count is invalid")
    turns = value.get("turns")
    if not isinstance(turns, list) or not turns:
        _migration_error("receipt_schema_unsupported", "Receipt must contain ordered turns")
    seen: set[str] = set()
    unresolved = 0
    expected_sequence = 1
    previous_turn_id: str | None = None
    required_turn = {
        "turn_id",
        "sequence",
        "purpose",
        "previous_turn_id",
        "status",
        "wrapped_prompt_sha256",
        "response_marker",
        "submission_count",
        "no_resend",
        "outbound_prompt_verified",
        "recovery_authority",
        "collection",
        "chunk",
        "created_at",
        "updated_at",
    }
    for turn in turns:
        if not isinstance(turn, Mapping):
            _migration_error("receipt_schema_unsupported", "Receipt turn is not an object")
        missing_turn = required_turn - set(turn)
        if missing_turn:
            _migration_error(
                "receipt_schema_unsupported",
                "Receipt turn lacks required fields",
                missing_fields=sorted(missing_turn),
            )
        _known_keys(
            turn,
            required_turn
            | {
                "armed_at",
                "submitted_at",
                "sent_prompt_sha256",
                "native_user_message_id",
                "outbound_prompt_verified_at",
                "readback_verification_attempt_count",
                "readback_artifact_sha256",
                "readback_correction_allowed",
                "readback_correction_kind",
                "readback_correction_applied_at",
                "submission_recovered_from",
                "submission_may_have_occurred",
                "submission_observed",
                "last_error_kind",
                "last_error_sha256",
                "pending_since",
                "failed_at",
                "failure_kind",
                "completed_at",
                "response_rejected_at",
                "rejection_kind",
                "rejection_reason_code",
                "rejection_predecessor_status",
                "rejection_collection",
                "trusted_complete_reread_at",
                "completion_kind",
                "legacy_collection",
            },
            location="turn",
        )
        turn_id = turn.get("turn_id")
        if not isinstance(turn_id, str):
            _migration_error("receipt_schema_unsupported", "Receipt turn has no ID")
        validate_identifier(turn_id, field="turn_id")
        if turn_id in seen:
            _migration_error("receipt_schema_unsupported", "Receipt has duplicate turn IDs")
        seen.add(turn_id)
        if turn.get("sequence") != expected_sequence:
            _migration_error("receipt_schema_unsupported", "Receipt turns are not contiguous")
        if expected_sequence == 1:
            if turn_id != assignment_id or turn.get("previous_turn_id") is not None:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Initial turn identity is invalid",
                )
        elif turn.get("previous_turn_id") != previous_turn_id:
            _migration_error(
                "receipt_schema_unsupported",
                "Receipt turns do not form one ordered lineage",
            )
        if not isinstance(turn.get("purpose"), str) or not turn.get("purpose"):
            _migration_error("receipt_schema_unsupported", "Receipt turn purpose is invalid")
        if turn.get("response_marker") != result_marker(turn_id):
            _migration_error("receipt_schema_unsupported", "Receipt turn marker is invalid")
        if not _is_lower_hex(turn.get("wrapped_prompt_sha256")):
            _migration_error("receipt_schema_unsupported", "Receipt turn prompt digest is invalid")
        submission_count = turn.get("submission_count")
        if type(submission_count) is not int or submission_count not in {0, 1}:
            _migration_error("receipt_schema_unsupported", "Receipt turn submission count is invalid")
        if type(turn.get("no_resend")) is not bool or type(turn.get("outbound_prompt_verified")) is not bool:
            _migration_error("receipt_schema_unsupported", "Receipt turn send state is invalid")
        recovery_authority = turn.get("recovery_authority")
        if recovery_authority not in RECOVERY_AUTHORITIES:
            _migration_error(
                "receipt_schema_unsupported",
                "Receipt turn recovery authority is invalid",
            )
        if submission_count == 1:
            if turn.get("no_resend") is not True or not _is_lower_hex(turn.get("sent_prompt_sha256")):
                _migration_error("receipt_schema_unsupported", "Submitted turn lacks immutable send evidence")
        if turn.get("outbound_prompt_verified") is True and submission_count != 1:
            _migration_error("receipt_schema_unsupported", "Verified turn has no exact submission")
        native_user_id = turn.get("native_user_message_id")
        if turn.get("outbound_prompt_verified") is True and not isinstance(native_user_id, str):
            _migration_error(
                "receipt_schema_unsupported",
                "Verified turn lacks an exact native user message ID",
            )
        if native_user_id is not None:
            try:
                validate_native_message_id(native_user_id, field="native_user_message_id")
            except ConfigurationError as exc:
                _migration_error("receipt_schema_unsupported", "Receipt native user message ID is invalid")
                raise AssertionError("unreachable") from exc
        collection = turn.get("collection")
        if not isinstance(collection, Mapping):
            _migration_error("receipt_schema_unsupported", "Receipt turn collection is invalid")
        _known_keys(
            collection,
            {
                "status",
                "accepted",
                "collection_schema",
                "adapter_contract_id",
                "requested_conversation_id",
                "loaded_conversation_id",
                "assistant_message_id",
                "submitted_user_message_id",
                "collection_evidence_sha256",
                "collection_content_identity_sha256",
                "collection_observed_at",
                "collection_status",
                "generation_finality_provenance",
                "raw_truncated",
                "normalized_truncated",
                "raw_outer_truncated",
                "normalized_outer_truncated",
                "outer_integrity_provenance",
                "response_byte_length",
                "response_sha256",
            },
            location="turn.collection",
        )
        collection_state = collection.get("status")
        if collection_state not in COLLECTION_STATUSES:
            _migration_error(
                "receipt_schema_unsupported",
                "Receipt turn collection has an invalid state",
            )
        if collection_state == "not_started":
            if set(collection) != {"status"}:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Unstarted collection may not carry evidence fields",
                )
        else:
            required_collection_identity = {
                "collection_schema",
                "adapter_contract_id",
                "requested_conversation_id",
                "loaded_conversation_id",
                "assistant_message_id",
                "submitted_user_message_id",
                "collection_evidence_sha256",
                "collection_content_identity_sha256",
                "collection_observed_at",
                "collection_status",
                "generation_finality_provenance",
                "raw_truncated",
                "normalized_truncated",
                "raw_outer_truncated",
                "normalized_outer_truncated",
                "outer_integrity_provenance",
                "response_byte_length",
                "response_sha256",
            }
            if not required_collection_identity <= set(collection):
                _migration_error(
                    "receipt_schema_unsupported",
                    "Collected turn lacks exact native association evidence",
                )
            if (
                collection.get("requested_conversation_id") != value.get("worker_conversation_id")
                or collection.get("loaded_conversation_id") != value.get("worker_conversation_id")
                or collection.get("submitted_user_message_id") != native_user_id
                or collection.get("collection_schema") != NATIVE_COLLECTION_SCHEMA
            ):
                # The schema literal is checked below instead of trusting a
                # receipt-supplied association.  Keep this block's failures
                # generic so receipts do not become an oracle for raw content.
                _migration_error(
                    "receipt_schema_unsupported",
                    "Collected turn native association differs from the dispatch",
                )
            try:
                validate_native_message_id(
                    collection.get("assistant_message_id"),
                    field="assistant_message_id",
                )
                validate_native_message_id(
                    collection.get("submitted_user_message_id"),
                    field="submitted_user_message_id",
                )
                validate_identifier(
                    collection.get("requested_conversation_id"),
                    field="requested_conversation_id",
                )
                validate_identifier(
                    collection.get("loaded_conversation_id"),
                    field="loaded_conversation_id",
                )
                _parse_utc(
                    str(collection.get("collection_observed_at")),
                    field="collection_observed_at",
                )
            except DispatchError as exc:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Collected turn native identifiers are invalid",
                )
                raise AssertionError("unreachable") from exc
            if (
                collection.get("collection_status") != "completed"
                or not isinstance(collection.get("generation_finality_provenance"), str)
                or not isinstance(collection.get("outer_integrity_provenance"), str)
                or not _is_lower_hex(collection.get("collection_evidence_sha256"))
                or not _is_lower_hex(collection.get("collection_content_identity_sha256"))
                or not _is_lower_hex(collection.get("response_sha256"))
                or type(collection.get("response_byte_length")) is not int
                or int(collection.get("response_byte_length")) < 0
                or collection.get("raw_truncated") not in {"true", "false", "omitted"}
                or collection.get("raw_outer_truncated") not in {"true", "false", "omitted"}
                or type(collection.get("normalized_truncated")) not in {bool, type(None)}
                or type(collection.get("normalized_outer_truncated")) not in {bool, type(None)}
                or type(collection.get("accepted")) is not bool
            ):
                _migration_error(
                    "receipt_schema_unsupported",
                    "Collected turn integrity fields are invalid",
                )
            try:
                contract = adapter_contract(str(collection.get("adapter_contract_id")))
            except CollectionEvidenceError as exc:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Collected turn adapter is not allowlisted",
                )
                raise AssertionError("unreachable") from exc
            if (
                collection.get("generation_finality_provenance")
                not in contract.generation_finality_provenance
                or collection.get("outer_integrity_provenance")
                not in contract.outer_integrity_provenance
            ):
                _migration_error(
                    "receipt_schema_unsupported",
                    "Collected turn provenance is not trusted by its adapter contract",
                )
            for raw_key, normalized_key, omission_is_false in (
                ("raw_truncated", "normalized_truncated", contract.omitted_message_truncated_is_false),
                (
                    "raw_outer_truncated",
                    "normalized_outer_truncated",
                    contract.omitted_outer_truncated_is_false,
                ),
            ):
                raw_truncation = collection.get(raw_key)
                normalized_truncation = collection.get(normalized_key)
                expected_normalized = {
                    "true": True,
                    "false": False,
                    "omitted": False if omission_is_false else None,
                }[str(raw_truncation)]
                if normalized_truncation is not expected_normalized:
                    _migration_error(
                        "receipt_schema_unsupported",
                        "Collected turn truncation normalization contradicts its adapter contract",
                    )
        chunk = turn.get("chunk")
        if chunk is not None and not isinstance(chunk, Mapping):
            _migration_error("receipt_schema_unsupported", "Receipt turn chunk is invalid")
        if isinstance(chunk, Mapping):
            _known_keys(
                chunk,
                {
                    "expected_index",
                    "expected_previous_chain_sha256",
                    "retransmission",
                    "index",
                    "previous_chain_sha256",
                    "chain_sha256",
                    "payload_sha256",
                    "byte_length",
                    "final",
                    "count",
                    "spool_write_pending",
                    "spool_filename",
                    "spool_status",
                    "accepted",
                },
                location="turn.chunk",
            )
            _validate_chunk_receipt_shape(chunk)
        expected_sequence += 1
        status = _validate_turn_status(turn.get("status"))
        if status in {"prepared", "armed"} and recovery_authority != "not-established":
            _migration_error(
                "receipt_schema_unsupported",
                "Unsubmitted turn has unexpected recovery authority",
            )
        if status in {"indeterminate", "ambiguous"} and recovery_authority != "collect-only":
            _migration_error(
                "receipt_schema_unsupported",
                "Collect-only turn lacks its recovery prohibition latch",
            )
        if status in {"submitted", "pending"} and recovery_authority not in (
            RECOVERY_EXACT_AUTHORITIES | {"legacy-unverified"}
        ):
            _migration_error(
                "receipt_schema_unsupported",
                "Submitted turn lacks exact or legacy recovery provenance",
            )
        if status == "response_rejected" and (
            turn.get("outbound_prompt_verified") is not True or submission_count != 1
        ):
            _migration_error(
                "receipt_schema_unsupported",
                "Rejected turn lacks proven exact submission",
            )
        if status == "response_rejected" and turn.get(
            "rejection_predecessor_status"
        ) not in {"submitted", "pending"}:
            _migration_error(
                "receipt_schema_unsupported",
                "Rejected turn did not originate from a submitted or pending turn",
            )
        if status == "response_rejected" and recovery_authority not in RECOVERY_EXACT_AUTHORITIES:
            _migration_error(
                "receipt_schema_unsupported",
                "Rejected turn lacks exact read-back recovery provenance",
            )
        if status != "response_rejected" and "rejection_predecessor_status" in turn:
            _migration_error(
                "receipt_schema_unsupported",
                "Only a rejected turn may retain a rejection predecessor status",
            )
        if status == "response_rejected" and not _proves_recovery_rejection(
            value, turn, turn.get("rejection_collection")
        ):
            _migration_error(
                "receipt_schema_unsupported",
                "Rejected turn lacks preserved proven completed native evidence",
            )
        if status in TURN_ACTIVE_STATUSES:
            unresolved += 1
        previous_turn_id = turn_id
    if unresolved > 1:
        _migration_error("receipt_schema_unsupported", "Receipt has more than one unresolved turn")
    for turn in turns:
        if turn.get("status") != "response_rejected":
            continue
        successors = [
            candidate
            for candidate in turns
            if candidate.get("previous_turn_id") == turn.get("turn_id")
        ]
        if len(successors) != 1:
            _migration_error(
                "receipt_schema_unsupported",
                "Rejected response must have exactly one recovery successor",
                turn_id=turn.get("turn_id"),
            )
    if int(send_attempt_total) != sum(
        1 for turn in turns if turn.get("no_resend") is True
    ):
        _migration_error("receipt_schema_unsupported", "Receipt send attempt total disagrees with turns")
    result = value.get("result")
    if not isinstance(result, Mapping) or result.get("status") not in {"not_complete", "complete"}:
        _migration_error("receipt_schema_unsupported", "Receipt result state is invalid")
    _known_keys(
        result,
        {
            "status",
            "completion_basis",
            "source_turn_id",
            "response_sha256",
            "payload_sha256",
            "byte_length",
            "completed_at",
            "chunk_count",
            "final_chain_sha256",
            "verified_at",
            "artifact",
        },
        location="result",
    )
    result_complete = result.get("status") == "complete"
    if result_complete:
        if (
            not _is_lower_hex(result.get("payload_sha256"))
            or not _is_lower_hex(result.get("response_sha256"))
            or type(result.get("byte_length")) is not int
            or int(result.get("byte_length")) < 0
            or result.get("source_turn_id") not in seen
        ):
            _migration_error("receipt_schema_unsupported", "Completed result identity is invalid")
    if (dispatch_status == "complete") != result_complete:
        _migration_error("receipt_schema_unsupported", "Dispatch and result completion disagree")
    if result_complete and unresolved:
        _migration_error("receipt_schema_unsupported", "Completed result has an unresolved turn")
    if dispatch_status in {"abandoned", "failed"} and unresolved:
        _migration_error("receipt_schema_unsupported", "Terminal dispatch has an unresolved turn")
    if dispatch_status in {"prepared", "active"} and unresolved != 1:
        _migration_error("receipt_schema_unsupported", "Active dispatch lacks exactly one current turn")
    delivery = value.get("delivery")
    if not isinstance(delivery, Mapping) or delivery.get("status") not in {
        "not_delivered",
        "materialized",
        "parent_restored",
    }:
        _migration_error("receipt_schema_unsupported", "Receipt delivery state is invalid")
    _known_keys(
        delivery,
        {
            "status",
            "parent_restoration_status",
            "parent_restoration_updated_at",
            "parent_restored_at",
            "materialized_at",
            "spool_cleanup_at",
            "spool_cleanup_count",
        },
        location="delivery",
    )
    if delivery.get("parent_restoration_status") not in {"not_started", "restored", "failed"}:
        _migration_error("receipt_schema_unsupported", "Receipt parent restoration state is invalid")
    if (
        delivery.get("status") in {"materialized", "parent_restored"}
        or delivery.get("parent_restoration_status") in {"restored", "failed"}
    ) and not result_complete:
        _migration_error("receipt_schema_unsupported", "Delivery state exists without immutable result")
    cleanup_at = delivery.get("spool_cleanup_at")
    cleanup_count = delivery.get("spool_cleanup_count")
    if (cleanup_at is None) != (cleanup_count is None):
        _migration_error(
            "receipt_schema_unsupported",
            "Spool cleanup timestamp and count must be recorded together",
        )
    if cleanup_at is not None:
        if (
            effective != "chunked"
            or not result_complete
            or delivery.get("parent_restoration_status") != "restored"
            or not isinstance(cleanup_at, str)
            or type(cleanup_count) is not int
            or int(cleanup_count) < 0
        ):
            _migration_error(
                "receipt_schema_unsupported",
                "Spool cleanup record is not a completed restored chunk result",
            )
        try:
            _parse_utc(cleanup_at, field="delivery.spool_cleanup_at")
        except DispatchError as exc:
            _migration_error(
                "receipt_schema_unsupported",
                "Spool cleanup timestamp is invalid",
            )
            raise AssertionError("unreachable") from exc
        accepted_count = sum(
            1
            for turn in turns
            if isinstance(turn.get("chunk"), Mapping)
            and turn["chunk"].get("accepted") is True
        )
        if cleanup_count != accepted_count:
            _migration_error(
                "receipt_schema_unsupported",
                "Spool cleanup count disagrees with accepted chunk records",
            )
    artifact_contract = value.get("artifact_contract")
    if mode == "artifact":
        if not isinstance(artifact_contract, Mapping):
            _migration_error("receipt_schema_unsupported", "Artifact receipt lacks a contract")
        _known_keys(
            artifact_contract,
            {
                "sha256",
                "contract",
                "write_authorized",
                "worker_github_write_confirmed",
                "public_retention_acknowledged",
            },
            location="artifact_contract",
        )
        if not isinstance(artifact_contract.get("contract"), Mapping):
            _migration_error("receipt_schema_unsupported", "Artifact receipt contract is invalid")
        try:
            contract = ArtifactContract.from_mapping(artifact_contract["contract"])
        except DispatchError as exc:
            _migration_error("receipt_schema_unsupported", "Artifact receipt contract is malformed")
            raise AssertionError("unreachable") from exc
        if artifact_contract.get("sha256") != contract.sha256:
            _migration_error("receipt_schema_unsupported", "Artifact receipt contract digest is invalid")
        if (
            artifact_contract.get("write_authorized") is not True
            or artifact_contract.get("worker_github_write_confirmed") is not True
        ):
            _migration_error("receipt_schema_unsupported", "Artifact receipt authorization is invalid")
        if contract.visibility == "public":
            if artifact_contract.get("public_retention_acknowledged") is not True:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Public artifact receipt lacks retention acknowledgement",
                )
        elif artifact_contract.get("public_retention_acknowledged") is not None:
            _migration_error(
                "receipt_schema_unsupported",
                "Private artifact receipt has an unexpected retention acknowledgement",
            )
        stored_manifest = value.get("artifact_manifest")
        if stored_manifest is not None:
            # Reconstructing the typed manifest reruns every exact contract
            # binding before a stored locator can reach a Git operation.
            _stored_artifact_manifest(value, contract)
        pending = value.get("artifact_verification_pending")
        if pending is not None:
            if not isinstance(pending, Mapping) or set(pending) != {
                "nonce_sha256",
                "started_at",
                "contract_sha256",
                "manifest_sha256",
                "discover",
            }:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Artifact verification pending record is malformed",
                )
            if (
                not _is_lower_hex(pending.get("nonce_sha256"))
                or pending.get("contract_sha256") != contract.sha256
                or type(pending.get("discover")) is not bool
            ):
                _migration_error(
                    "receipt_schema_unsupported",
                    "Artifact verification pending identity is invalid",
                )
            try:
                _parse_utc(str(pending.get("started_at")), field="artifact_verification_pending.started_at")
            except DispatchError as exc:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Artifact verification pending timestamp is invalid",
                )
                raise AssertionError("unreachable") from exc
            manifest_digest = _artifact_manifest_digest(value)
            if pending.get("manifest_sha256") != manifest_digest:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Artifact verification pending manifest binding changed",
                )
        failure = value.get("artifact_verification_failure")
        if failure is not None:
            if not isinstance(failure, Mapping) or set(failure) != {"error_code", "at"}:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Artifact verification failure record is malformed",
                )
            if not isinstance(failure.get("error_code"), str) or not failure.get("error_code"):
                _migration_error(
                    "receipt_schema_unsupported",
                    "Artifact verification failure code is invalid",
                )
            try:
                _parse_utc(str(failure.get("at")), field="artifact_verification_failure.at")
            except DispatchError as exc:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Artifact verification failure timestamp is invalid",
                )
                raise AssertionError("unreachable") from exc
        if dispatch_status == "verifying" and pending is None and stored_manifest is None:
            _migration_error(
                "receipt_schema_unsupported",
                "Artifact verification lacks both a manifest and a verifier nonce",
            )
        if result_complete and effective == "artifact":
            artifact_result = result.get("artifact")
            required_artifact_result = {
                "branch",
                "commit_sha",
                "tree_sha",
                "blob_sha",
                "file_mode",
                "byte_length",
                "content_sha256",
                "base_state",
                "branch_head_before",
                "branch_head_after",
                "verifier_version",
            }
            if not isinstance(artifact_result, Mapping) or set(artifact_result) != required_artifact_result:
                _migration_error(
                    "receipt_schema_unsupported",
                    "Completed artifact result lacks exact object evidence",
                )
            if (
                artifact_result.get("branch") != contract.branch
                or not all(
                    isinstance(artifact_result.get(field), str)
                    and len(str(artifact_result.get(field))) == 40
                    and all(char in "0123456789abcdef" for char in str(artifact_result.get(field)))
                    for field in ("commit_sha", "tree_sha", "blob_sha", "branch_head_before", "branch_head_after")
                )
                or artifact_result.get("file_mode") != "100644"
                or artifact_result.get("byte_length") != result.get("byte_length")
                or artifact_result.get("content_sha256") != result.get("payload_sha256")
                or artifact_result.get("base_state") not in {"base_unchanged", "base_advanced"}
                or artifact_result.get("verifier_version") != "git-artifact-verifier/v1"
            ):
                _migration_error(
                    "receipt_schema_unsupported",
                    "Completed artifact result object identity is invalid",
                )
        elif result_complete and result.get("artifact") is not None:
            _migration_error(
                "receipt_schema_unsupported",
                "Non-artifact result contains artifact object evidence",
            )
    elif artifact_contract is not None:
        _migration_error("receipt_schema_unsupported", "Non-artifact receipt has an artifact contract")
    if mode != "artifact" and (
        value.get("artifact_manifest") is not None
        or value.get("artifact_verification_pending") is not None
        or value.get("artifact_verification_failure") is not None
    ):
        _migration_error("receipt_schema_unsupported", "Non-artifact receipt has artifact verification state")
    legacy = value.get("legacy")
    if legacy is not None:
        if not isinstance(legacy, Mapping):
            _migration_error("receipt_schema_unsupported", "Receipt legacy state is invalid")
        _known_keys(
            legacy,
            {"origin_schema", "legacy_status", "collection_evidence_required"},
            location="legacy",
        )
    _body_free(value)
    return dict(value)


def load_assignment_v2(
    assignment_id: str, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    """Read either receipt schema, never rewriting completed v1 history."""

    raw = _read_raw(assignment_id, paths)
    if raw.get("schema_version") == 1:
        return _legacy_projection(raw)
    # Status/recovery may safely hide a pre-v1.2 diagnostic body long enough for
    # doctor to durably redact it. State-changing commands still require repair.
    projected, _changed = _redact_v2_diagnostic_fields(raw)
    return _validate_v2(projected, assignment_id)


def _save_v2(
    assignment_id: str, value: Mapping[str, Any], paths: RuntimePaths | None = None
) -> Path:
    payload = dict(value)
    payload["schema_version"] = ASSIGNMENT_SCHEMA_VERSION
    payload["record_type"] = "dispatch"
    payload["assignment_id"] = assignment_id
    payload["updated_at"] = utc_now()
    _validate_v2(payload, assignment_id)
    path = _receipt_path(assignment_id, paths)
    atomic_write_json(path, payload)
    return path


def _load_mutable_v2(
    assignment_id: str, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    raw = _read_raw(assignment_id, paths)
    if raw.get("schema_version") == 1:
        if raw.get("status") == "complete":
            _migration_error(
                "legacy_completed_immutable",
                "Completed v1 receipt is marker-only historical evidence and immutable",
                assignment_id=assignment_id,
            )
        migrated = _migrate_v1(raw, assignment_id)
        _save_v2(assignment_id, migrated, paths)
        return migrated
    receipt = _validate_v2(raw, assignment_id)
    # A process can stop after the private rename and before clearing the
    # receipt journal.  Reconcile only exact hash-matching files while already
    # under the caller's state lock; a missing file remains pending so the same
    # bound assistant message can be reread without a send.
    reconciled_spools = _reconcile_pending_spools(receipt, paths or default_paths())
    # A prepared continuation is send-capable. Do not let it arm or collect a
    # later chunk while an earlier accepted private part is missing, corrupt, or
    # accompanied by an orphan. This is intentionally checked on every mutable
    # load, not only when the final result is materialized.
    _assert_private_spool_integrity(receipt, paths or default_paths())
    if reconciled_spools:
        _save_v2(assignment_id, receipt, paths)
    return receipt


def list_assignments_v2(paths: RuntimePaths | None = None) -> list[dict[str, Any]]:
    runtime = paths or default_paths()
    if not runtime.assignments_dir.exists():
        return []
    values: list[dict[str, Any]] = []
    for path in sorted(runtime.assignments_dir.glob("*.json")):
        try:
            value = read_json(path)
            assignment_id = value.get("assignment_id")
            if not isinstance(assignment_id, str):
                _migration_error("receipt_schema_unsupported", "Receipt has no assignment ID")
            validate_identifier(assignment_id, field="assignment_id")
            if path != _receipt_path(assignment_id, runtime):
                _migration_error("receipt_schema_unsupported", "Receipt identity does not match filename")
            values.append(load_assignment_v2(assignment_id, runtime))
        except DispatchError as exc:
            _state_error(
                "receipt_invalid",
                "Invalid assignment receipt; refusing to dispatch",
                path=str(path),
                error_code=exc.error_code,
            )
    return sorted(values, key=lambda item: str(item.get("created_at", "")))


def active_assignment_v2(paths: RuntimePaths | None = None) -> dict[str, Any] | None:
    active: list[dict[str, Any]] = []
    for value in list_assignments_v2(paths):
        if value.get("schema_version") == 1:
            if value.get("status") in {
                "prepared",
                "armed",
                "submitted",
                "pending",
                "indeterminate",
                "ambiguous",
            }:
                active.append(value)
        elif value.get("status") in DISPATCH_ACTIVE_STATUSES:
            active.append(value)
    if len(active) > 1:
        _state_error(
            "multiple_active_dispatches",
            "Multiple unresolved dispatches exist",
            assignment_ids=[item.get("assignment_id") for item in active],
        )
    return active[0] if active else None


def active_cooldown_v2(
    paths: RuntimePaths | None = None, *, now: dt.datetime | None = None
) -> dict[str, Any] | None:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ConfigurationError("Cooldown comparison time must include a timezone")
    current = current.astimezone(dt.timezone.utc)
    active: list[tuple[dt.datetime, Mapping[str, Any]]] = []
    for value in list_assignments_v2(paths):
        until = value.get("cooldown_until")
        if until is None:
            continue
        try:
            parsed = _parse_utc(str(until), field="cooldown_until")
        except DispatchError as exc:
            _state_error(
                "receipt_invalid",
                "Invalid cooldown receipt; refusing to dispatch",
                assignment_id=value.get("assignment_id"),
                error_code=exc.error_code,
            )
        if parsed > current:
            active.append((parsed, value))
    if not active:
        return None
    expires, value = max(active, key=lambda item: item[0])
    return {
        "assignment_id": value.get("assignment_id"),
        "native_http_status": value.get("native_http_status"),
        "native_error_kind": value.get("native_error_kind"),
        "cooldown_seconds": value.get("cooldown_seconds"),
        "cooldown_started_at": value.get("cooldown_started_at"),
        "cooldown_until": value.get("cooldown_until"),
        "retry_after_seconds": max(1, int((expires - current).total_seconds() + 0.999999)),
        **(
            {"openai_request_id": value["openai_request_id"]}
            if value.get("openai_request_id")
            else {}
        ),
    }


def _validate_continuation(
    continuation_of: str | None, worker_id: str, paths: RuntimePaths
) -> None:
    if continuation_of is None:
        return
    validate_identifier(continuation_of, field="continuation_of")
    prior = load_assignment_v2(continuation_of, paths)
    if prior.get("status") != "complete":
        _state_error(
            "continuation_not_complete",
            "Continuation requires a completed prior dispatch",
            continuation_of=continuation_of,
            status=prior.get("status"),
        )
    if prior.get("worker_conversation_id") != worker_id:
        _state_error(
            "continuation_wrong_worker",
            "Continuation worker does not match configured worker",
            continuation_of=continuation_of,
        )


def _initial_prompt(
    prompt: str,
    *,
    assignment_id: str,
    turn_id: str,
    mode: str,
    artifact_contract: ArtifactContract | None,
) -> str:
    base = wrap_prompt(prompt, turn_id)
    if mode == "inline":
        return (
            base
            + "\\n\\nLong-result policy: target at most 12,000 UTF-8 bytes including "
            "the result marker. This is a soft budget, not completion evidence. If a "
            "complete answer cannot fit, return exactly the result marker, LF, then "
            f"{CHUNKED_REQUIRED_CONTROL}; do not use artifact mode."
        )
    if mode == "chunked":
        return (
            base
            + "\\n\\nReturn chunk 1 in the strict JSON-framed chunk protocol. The "
            "original assignment is sent only in this initial turn. Use an assistant "
            "message no larger than the helper limit; all Markdown belongs in the "
            "JSON payload string. Do not write a repository or use artifact mode."
        )
    if artifact_contract is None:
        raise ArtifactProtocolError(
            "Artifact mode requires a prepared contract",
            error_code="artifact_authorization_missing",
        )
    return (
        base
        + "\\n\\nArtifact authorization applies only to this assignment: create only "
        f"branch {artifact_contract.branch} from {artifact_contract.base_sha}; add only "
        f"{artifact_contract.path} as one UTF-8 Markdown file; make exactly one commit "
        f"with message {artifact_contract.commit_message!r}; then return the canonical "
        "artifact manifest. Do not modify base or other refs, force-push, create a PR, "
        "merge, tag, release, deploy, change settings/workflows, or paste the artifact "
        "body into chat. This authority does not transfer to continuations."
    )


def _turn(
    *,
    turn_id: str,
    sequence: int,
    purpose: str,
    previous_turn_id: str | None,
    wrapped_prompt: str,
) -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "sequence": sequence,
        "purpose": purpose,
        "previous_turn_id": previous_turn_id,
        "status": "prepared",
        "wrapped_prompt_sha256": sha256_text(wrapped_prompt),
        "response_marker": result_marker(turn_id),
        "submission_count": 0,
        "no_resend": False,
        "outbound_prompt_verified": False,
        "recovery_authority": "not-established",
        "collection": {"status": "not_started"},
        "chunk": None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def prepare_assignment_v2(
    prompt: str,
    *,
    parent_task_id: str,
    result_mode: str = "inline",
    continuation_of: str | None = None,
    assignment_id: str | None = None,
    artifact_contract: ArtifactContract | Mapping[str, Any] | None = None,
    authorize_artifact_write: bool = False,
    worker_github_write_confirmed: bool = False,
    allow_public_artifact: bool = False,
    artifact_verifier: GitArtifactVerifier | None = None,
    paths: RuntimePaths | None = None,
) -> PreparedAssignment:
    """Prepare one explicit-mode logical dispatch without storing its prompt."""

    runtime = paths or default_paths()
    validate_identifier(parent_task_id, field="parent_task_id")
    mode = _require_mode(result_mode)
    resolved_id = assignment_id if assignment_id is not None else new_assignment_id()
    validate_identifier(resolved_id, field="assignment_id")

    contract: ArtifactContract | None = None
    if mode == "artifact":
        if artifact_contract is None or not authorize_artifact_write or not worker_github_write_confirmed:
            raise ArtifactProtocolError(
                "Artifact mode requires contract, explicit write authorization, and worker-write confirmation",
                error_code="artifact_authorization_missing",
            )
        # Reparse even a Python dataclass supplied by an embedding caller.  The
        # public write-capable path must have exactly the same strict contract
        # boundary as the CLI JSON path.
        contract = ArtifactContract.from_mapping(
            artifact_contract.to_dict()
            if isinstance(artifact_contract, ArtifactContract)
            else artifact_contract
        )
        if contract.visibility == "public" and not allow_public_artifact:
            raise ArtifactProtocolError(
                "Public artifact retention requires explicit acknowledgement",
                error_code="artifact_public_retention_unacknowledged",
            )
        # Network reads happen before the state lock. Verification repeats the
        # checks after the worker returns, so this cannot authorize an implicit write.
        (artifact_verifier or GitArtifactVerifier()).preflight(contract)
    elif artifact_contract is not None:
        raise ArtifactProtocolError(
            "Artifact contract is valid only for explicit artifact mode",
            error_code="artifact_authorization_missing",
        )

    with state_lock(runtime):
        worker = load_worker(runtime)
        if _receipt_path(resolved_id, runtime).exists():
            _state_error(
                "assignment_exists",
                "Assignment ID already exists; refusing a possible duplicate submission",
                assignment_id=resolved_id,
            )
        active = active_assignment_v2(runtime)
        if active:
            raise BusyError(
                "Another dispatch is unresolved",
                details={"assignment_id": active.get("assignment_id"), "status": active.get("status")},
            )
        cooldown = active_cooldown_v2(runtime)
        if cooldown:
            raise CooldownError("Native ChatGPT HTTP 403 cooldown is still active", details=cooldown)
        _validate_continuation(continuation_of, worker.conversation_id, runtime)

        turn_id = resolved_id
        wrapped = _initial_prompt(
            prompt,
            assignment_id=resolved_id,
            turn_id=turn_id,
            mode=mode,
            artifact_contract=contract,
        )
        receipt: dict[str, Any] = {
            "schema_version": ASSIGNMENT_SCHEMA_VERSION,
            "record_type": "dispatch",
            "assignment_id": resolved_id,
            "status": "prepared",
            "requested_result_mode": mode,
            "effective_result_mode": mode,
            "worker_conversation_id": worker.conversation_id,
            "worker_label": worker.label,
            "worker_model_confirmation": worker.model_confirmation,
            "parent_task_id": parent_task_id,
            "continuation_of": continuation_of,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "prompt_sha256": sha256_text(normalize_newlines(prompt).strip()),
            "no_original_resend": False,
            "send_attempt_total": 0,
            "turns": [
                _turn(
                    turn_id=turn_id,
                    sequence=1,
                    purpose="initial",
                    previous_turn_id=None,
                    wrapped_prompt=wrapped,
                )
            ],
            "artifact_contract": (
                {
                    "sha256": contract.sha256,
                    "contract": contract.to_dict(),
                    "write_authorized": True,
                    "worker_github_write_confirmed": True,
                    "public_retention_acknowledged": bool(allow_public_artifact)
                    if contract and contract.visibility == "public"
                    else None,
                }
                if contract
                else None
            ),
            "result": {
                "status": "not_complete",
            },
            "delivery": {"status": "not_delivered", "parent_restoration_status": "not_started"},
            "legacy": None,
        }
        path = _save_v2(resolved_id, receipt, runtime)

    return PreparedAssignment(
        assignment_id=resolved_id,
        worker_conversation_id=worker.conversation_id,
        parent_task_id=parent_task_id,
        receipt_path=path,
        wrapped_prompt=wrapped,
        continuation_of=continuation_of,
        turn_id=turn_id,
        result_mode=mode,
        sequence=1,
    )


def _find_turn(receipt: Mapping[str, Any], turn_id: str) -> dict[str, Any]:
    validate_identifier(turn_id, field="turn_id")
    for turn in receipt["turns"]:
        if turn.get("turn_id") == turn_id:
            return turn
    _state_error(
        "turn_not_found",
        "Dispatch turn does not exist",
        assignment_id=receipt.get("assignment_id"),
        turn_id=turn_id,
    )


def _unresolved_turns(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        turn
        for turn in receipt["turns"]
        if turn.get("status") in TURN_ACTIVE_STATUSES
    ]


def _resolve_turn(
    receipt: Mapping[str, Any], turn_id: str | None, *, require_unresolved: bool = True
) -> dict[str, Any]:
    if turn_id is not None:
        turn = _find_turn(receipt, turn_id)
        if require_unresolved and turn.get("status") not in TURN_ACTIVE_STATUSES:
            _state_error(
                "turn_not_unresolved",
                "Dispatch turn is not unresolved",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn_id,
                status=turn.get("status"),
            )
        return turn
    unresolved = _unresolved_turns(receipt)
    if len(unresolved) != 1:
        _state_error(
            "turn_selection_ambiguous",
            "Exactly one unresolved turn is required",
            assignment_id=receipt.get("assignment_id"),
            unresolved_turn_ids=[item.get("turn_id") for item in unresolved],
        )
    return unresolved[0]


def _prepared_from_turn(
    receipt: Mapping[str, Any], turn: Mapping[str, Any], prompt: str, paths: RuntimePaths
) -> PreparedAssignment:
    return PreparedAssignment(
        assignment_id=str(receipt["assignment_id"]),
        worker_conversation_id=str(receipt["worker_conversation_id"]),
        parent_task_id=str(receipt["parent_task_id"]),
        receipt_path=_receipt_path(str(receipt["assignment_id"]), paths),
        wrapped_prompt=prompt,
        continuation_of=receipt.get("continuation_of"),
        turn_id=str(turn["turn_id"]),
        result_mode=str(receipt["effective_result_mode"]),
        sequence=int(turn["sequence"]),
    )


def _transition_turn(
    receipt: dict[str, Any],
    turn: dict[str, Any],
    *,
    status: str,
    dispatch_status: str | None = None,
    **updates: Any,
) -> None:
    _validate_turn_status(status)
    turn["status"] = status
    turn["updated_at"] = utc_now()
    turn.update(updates)
    if dispatch_status is not None:
        _validate_dispatch_status(dispatch_status)
        receipt["status"] = dispatch_status


def arm_assignment_v2(
    assignment_id: str,
    paths: RuntimePaths | None = None,
    *,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Arm exactly one turn before its one native send attempt."""

    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        turn = _resolve_turn(receipt, turn_id)
        if turn.get("status") != "prepared":
            _state_error(
                "turn_already_armed",
                "A turn may be armed only once",
                assignment_id=assignment_id,
                turn_id=turn.get("turn_id"),
                status=turn.get("status"),
            )
        _transition_turn(
            receipt,
            turn,
            status="armed",
            dispatch_status="active",
            armed_at=utc_now(),
            no_resend=True,
        )
        receipt["send_attempt_total"] = int(receipt.get("send_attempt_total", 0)) + 1
        if turn.get("sequence") == 1:
            receipt["no_original_resend"] = True
        _save_v2(assignment_id, receipt, runtime)
        return receipt


def mark_submitted_v2(
    assignment_id: str,
    sent_prompt: str,
    paths: RuntimePaths | None = None,
    *,
    turn_id: str | None = None,
    native_user_message_id: str | None = None,
) -> dict[str, Any]:
    """Record one exact native read-back, retaining collect-only recovery on drift."""

    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        turn = _resolve_turn(receipt, turn_id)
        current = str(turn.get("status"))
        submission_count = int(turn.get("submission_count", 0))
        expected_hash = str(turn.get("wrapped_prompt_sha256", ""))
        sent_hash = sha256_text(sent_prompt)
        prior_sent_hash = str(turn.get("sent_prompt_sha256", ""))
        supplied_native_user_id: str | None = None
        supplied_native_user_id_invalid = False
        if native_user_message_id is not None:
            try:
                supplied_native_user_id = validate_native_message_id(
                    native_user_message_id, field="native_user_message_id"
                )
            except ConfigurationError:
                # The native send may already have happened.  Preserve the
                # no-resend boundary below instead of throwing before durable
                # state records that the exact user-message association is
                # unavailable.
                supplied_native_user_id_invalid = True
        late_verification = (
            current in {"indeterminate", "ambiguous"}
            and submission_count == 0
            and turn.get("no_resend") is True
        )
        trailing_newline_artifact = (
            sent_hash == expected_hash
            and sha256_text(sent_prompt + "\n") == prior_sent_hash
        )
        readback_correction = (
            current in {"indeterminate", "ambiguous"}
            and submission_count == 1
            and turn.get("no_resend") is True
            and turn.get("outbound_prompt_verified") is False
            and (
                turn.get("readback_correction_allowed") is True
                or trailing_newline_artifact
            )
            and sent_hash == expected_hash
        )
        if current != "armed" and not late_verification and not readback_correction:
            _state_error(
                "submission_not_armed",
                "Submission may be recorded only once after durable arm",
                assignment_id=assignment_id,
                turn_id=turn.get("turn_id"),
                status=current,
                submission_count=submission_count,
            )

        verified_at = utc_now()
        if not readback_correction:
            turn["submitted_at"] = verified_at
        turn["submission_count"] = 1
        turn["sent_prompt_sha256"] = sent_hash
        turn["no_resend"] = True
        turn["readback_verification_attempt_count"] = int(
            turn.get(
                "readback_verification_attempt_count",
                1 if trailing_newline_artifact else 0,
            )
        ) + 1

        if sent_hash != expected_hash:
            one_extra_newline = (
                sent_prompt.endswith("\n")
                and sha256_text(sent_prompt[:-1]) == expected_hash
            )
            _transition_turn(
                receipt,
                turn,
                status="indeterminate",
                dispatch_status="recoverable",
                outbound_prompt_verified=False,
                recovery_authority="collect-only",
                submission_may_have_occurred=True,
                readback_artifact_sha256=sent_hash,
                last_error_kind="native-readback-mismatch",
            )
            if one_extra_newline:
                turn["readback_correction_allowed"] = True
                turn["readback_correction_kind"] = "single-trailing-newline"
            else:
                turn.pop("readback_correction_allowed", None)
                turn.pop("readback_correction_kind", None)
            _save_v2(assignment_id, receipt, runtime)
            _state_error(
                "native_readback_mismatch",
                "Submitted prompt failed exact read-back verification; never resend",
                assignment_id=assignment_id,
                turn_id=turn.get("turn_id"),
                status="indeterminate",
                expected_sha256=expected_hash,
                actual_sha256=sent_hash,
                no_resend=True,
                readback_correction_allowed=one_extra_newline,
            )

        if supplied_native_user_id is not None:
            turn["native_user_message_id"] = supplied_native_user_id
        known_native_user_id = turn.get("native_user_message_id")
        if supplied_native_user_id_invalid or not isinstance(known_native_user_id, str):
            _transition_turn(
                receipt,
                turn,
                status="indeterminate",
                dispatch_status="recoverable",
                outbound_prompt_verified=False,
                recovery_authority="collect-only",
                submission_may_have_occurred=True,
                last_error_kind=(
                    "native-user-message-id-invalid"
                    if supplied_native_user_id_invalid
                    else "native-user-message-id-missing"
                ),
            )
            _save_v2(assignment_id, receipt, runtime)
            _state_error(
                (
                    "native_user_message_id_invalid"
                    if supplied_native_user_id_invalid
                    else "native_user_message_id_required"
                ),
                "Exact native user-message identity is required before a submission is verified",
                assignment_id=assignment_id,
                turn_id=turn.get("turn_id"),
                no_resend=True,
            )

        _transition_turn(
            receipt,
            turn,
            status="submitted",
            dispatch_status="active",
            outbound_prompt_verified=True,
            recovery_authority=(
                "exact-readback-recovered"
                if readback_correction or late_verification
                else "exact-readback"
            ),
            outbound_prompt_verified_at=verified_at,
            submission_observed=True,
        )
        turn.pop("last_error_kind", None)
        turn.pop("last_error_sha256", None)
        turn.pop("submission_may_have_occurred", None)
        if readback_correction:
            if trailing_newline_artifact:
                turn["readback_artifact_sha256"] = prior_sent_hash
                turn["readback_correction_kind"] = "single-trailing-newline"
            turn["readback_correction_applied_at"] = verified_at
            turn.pop("readback_correction_allowed", None)
            turn["submission_recovered_from"] = current
        elif late_verification:
            turn["submission_recovered_from"] = current
        _save_v2(assignment_id, receipt, runtime)
        return receipt


def mark_pending_v2(
    assignment_id: str, paths: RuntimePaths | None = None, *, turn_id: str | None = None
) -> dict[str, Any]:
    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        turn = _resolve_turn(receipt, turn_id)
        if turn.get("status") != "submitted":
            _state_error(
                "pending_not_submitted",
                "Only a submitted turn can become pending",
                assignment_id=assignment_id,
                turn_id=turn.get("turn_id"),
                status=turn.get("status"),
            )
        _transition_turn(
            receipt,
            turn,
            status="pending",
            dispatch_status="active",
            pending_since=utc_now(),
        )
        _save_v2(assignment_id, receipt, runtime)
        return receipt


def _record_collect_only(
    receipt: dict[str, Any],
    turn: dict[str, Any],
    *,
    status: str,
    error_kind: str,
    reason: str,
) -> None:
    if not reason.strip():
        raise ConfigurationError("Recovery reason is empty")
    if turn.get("status") not in {
        "armed",
        "submitted",
        "pending",
        "indeterminate",
        "ambiguous",
    }:
        _state_error(
            "collect_only_not_armed",
            "Only an armed or previously submitted turn can become collect-only",
            assignment_id=receipt.get("assignment_id"),
            turn_id=turn.get("turn_id"),
            status=turn.get("status"),
        )
    _transition_turn(
        receipt,
        turn,
        status=status,
        dispatch_status="recoverable",
        no_resend=True,
        recovery_authority="collect-only",
        last_error_kind=error_kind,
        last_error_sha256=sha256_text(reason.strip()),
    )


def mark_indeterminate_v2(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        turn = _resolve_turn(receipt, turn_id)
        _record_collect_only(
            receipt,
            turn,
            status="indeterminate",
            error_kind="native-send-indeterminate",
            reason=reason,
        )
        _save_v2(assignment_id, receipt, runtime)
        return receipt


def mark_ambiguous_v2(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        turn = _resolve_turn(receipt, turn_id)
        _record_collect_only(
            receipt,
            turn,
            status="ambiguous",
            error_kind="response-ambiguous",
            reason=reason,
        )
        _save_v2(assignment_id, receipt, runtime)
        return receipt


def mark_unusual_activity_403_v2(
    assignment_id: str,
    *,
    reason: str,
    request_id: str | None = None,
    paths: RuntimePaths | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    cleaned = reason.strip()
    if not cleaned:
        raise ConfigurationError("HTTP 403 reason is empty")
    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        turn = _resolve_turn(receipt, turn_id)
        if receipt.get("native_error_kind") == "openai-unusual-activity":
            if request_id and not receipt.get("openai_request_id"):
                receipt["openai_request_id"] = validate_identifier(
                    request_id, field="openai_request_id"
                )
                _save_v2(assignment_id, receipt, runtime)
            return receipt
        _record_collect_only(
            receipt,
            turn,
            status="indeterminate",
            error_kind="openai-unusual-activity",
            reason=cleaned,
        )
        started = dt.datetime.now(dt.timezone.utc)
        receipt.update(
            {
                "native_http_status": 403,
                "native_error_kind": "openai-unusual-activity",
                "cooldown_seconds": 30 * 60,
                "cooldown_started_at": _format_utc(started),
                "cooldown_until": _format_utc(started + dt.timedelta(minutes=30)),
            }
        )
        if request_id:
            receipt["openai_request_id"] = validate_identifier(
                request_id, field="openai_request_id"
            )
        _save_v2(assignment_id, receipt, runtime)
        return receipt


def abandon_assignment_v2(
    assignment_id: str,
    *,
    reason: str,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    cleaned = reason.strip()
    if not cleaned:
        raise ConfigurationError("Abandon reason is empty")
    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        if receipt.get("status") not in DISPATCH_ACTIVE_STATUSES:
            _state_error(
                "dispatch_not_unresolved",
                "Only an unresolved dispatch can be abandoned",
                assignment_id=assignment_id,
                status=receipt.get("status"),
            )
        for turn in _unresolved_turns(receipt):
            _transition_turn(receipt, turn, status="failed")
            turn["failed_at"] = utc_now()
            turn["failure_kind"] = "user-authorized-abandon"
        receipt["status"] = "abandoned"
        receipt["abandoned_at"] = utc_now()
        receipt["abandon_reason_kind"] = "user-authorized"
        receipt["abandon_reason_sha256"] = sha256_text(cleaned)
        _save_v2(assignment_id, receipt, runtime)
        return receipt


def recovery_info_v2(
    assignment_id: str, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    value = load_assignment_v2(assignment_id, paths)
    if value.get("schema_version") == 1:
        return {
            "assignment_id": assignment_id,
            "status": value.get("status"),
            "worker_conversation_id": value.get("worker_conversation_id"),
            "parent_task_id": value.get("parent_task_id"),
            "legacy": value.get("legacy"),
        }
    unresolved = _unresolved_turns(value)
    current = unresolved[0] if len(unresolved) == 1 else value["turns"][-1]
    runtime = paths or default_paths()
    # `recovery_info` may reconstruct a continuation prompt and advertise the
    # next chunk boundary.  It is therefore a control-bearing reader, not
    # merely cosmetic status: never derive either from a missing, corrupt, or
    # orphaned accepted private part.
    if value.get("effective_result_mode") == "chunked":
        _assert_private_spool_integrity(value, runtime)
    current_summary = {
        "turn_id": current.get("turn_id"),
        "status": current.get("status"),
        "submission_count": current.get("submission_count"),
        "no_resend": current.get("no_resend"),
        "outbound_prompt_verified": current.get("outbound_prompt_verified"),
        "wrapped_prompt_sha256": current.get("wrapped_prompt_sha256"),
        "sent_prompt_sha256": current.get("sent_prompt_sha256"),
        "native_user_message_id": current.get("native_user_message_id"),
        "last_error_kind": current.get("last_error_kind"),
        "last_error_sha256": current.get("last_error_sha256"),
    }
    next_action = "inspect_terminal_result"
    prepared_turn: dict[str, Any] | None = None
    if current.get("status") == "prepared":
        if int(current.get("sequence", 0)) == 1:
            # The original assignment is intentionally not persisted.  It may
            # not be reconstructed from a hash, and the caller must not treat a
            # recovery display as permission to resend it.
            next_action = "original_prompt_not_retained_never_resend_automatically"
        else:
            reconstructed = _prepared_recovery_successor(value, current, runtime)
            next_action = "arm_prepared_child_then_send_once"
            prepared_turn = {
                "turn_id": reconstructed.turn_id,
                "wrapped_prompt": reconstructed.wrapped_prompt,
                "wrapped_prompt_sha256": current.get("wrapped_prompt_sha256"),
            }
    elif current.get("status") in {"armed", "indeterminate", "ambiguous"}:
        next_action = "collect_or_verify_existing_native_message_without_resend"
    elif current.get("status") in {"submitted", "pending"}:
        next_action = "collect_exact_completed_native_message"
    elif (
        value.get("effective_result_mode") == "artifact"
        and value.get("status") in {"recoverable", "verifying"}
    ):
        next_action = "resume_read_only_artifact_verification"

    boundary: dict[str, Any] | None = None
    if value.get("effective_result_mode") == "chunked":
        index, previous = _chunk_boundary(value)
        boundary = {
            "next_index": index,
            "previous_chain_sha256": previous,
            "accepted_chunk_count": len(_accepted_chunk_turns(value)),
        }
    cooldown = active_cooldown_v2(paths)
    return {
        "assignment_id": assignment_id,
        "status": value.get("status"),
        "worker_conversation_id": value.get("worker_conversation_id"),
        "parent_task_id": value.get("parent_task_id"),
        "result_mode": value.get("effective_result_mode"),
        "no_original_resend": value.get("no_original_resend"),
        "send_attempt_total": value.get("send_attempt_total", 0),
        "current_turn": current_summary,
        "next_action": next_action,
        **({"prepared_child": prepared_turn} if prepared_turn is not None else {}),
        **({"chunk_boundary": boundary} if boundary is not None else {}),
        **(
            {
                "artifact_contract_sha256": value["artifact_contract"].get("sha256"),
                "artifact_verification_pending": value.get("artifact_verification_pending") is not None,
            }
            if value.get("effective_result_mode") == "artifact"
            and isinstance(value.get("artifact_contract"), Mapping)
            else {}
        ),
        # Compact compatibility projection; callers should migrate to
        # current_turn so the logical dispatch and send state stay distinct.
        **current_summary,
        "result": {
            key: value["result"].get(key)
            for key in ("status", "completion_basis")
        },
        "delivery": dict(value.get("delivery") or {}),
        **(
            {"active_cooldown": cooldown}
            if cooldown and cooldown.get("assignment_id") == assignment_id
            else {}
        ),
    }


def _child_turn_id(assignment_id: str, sequence: int) -> str:
    candidate = f"{assignment_id}.chunk.{sequence:04d}"
    if len(candidate.encode("utf-8")) > 128:
        _state_error(
            "child_turn_id_too_long",
            "Assignment ID leaves no room for a safe child turn ID",
            assignment_id=assignment_id,
        )
    validate_identifier(candidate, field="turn_id")
    return candidate


def _append_child_turn(
    receipt: dict[str, Any],
    predecessor: dict[str, Any],
    *,
    purpose: str,
    next_index: int,
    previous_chain_sha256: str,
    retransmission: bool,
    paths: RuntimePaths,
) -> PreparedAssignment:
    """Atomically reject a proven-final predecessor and prepare one child."""

    predecessor_status = predecessor.get("status")
    if predecessor.get("outbound_prompt_verified") is not True or int(
        predecessor.get("submission_count", 0)
    ) != 1:
        _state_error(
            "recovery_submission_unverified",
            "Uncertain sends may not create a recovery successor",
            assignment_id=receipt.get("assignment_id"),
            turn_id=predecessor.get("turn_id"),
        )
    if predecessor_status not in {"submitted", "pending", "response_rejected"}:
        _state_error(
            "recovery_predecessor_not_submitted",
            "Only a submitted or pending turn may be rejected for recovery",
            assignment_id=receipt.get("assignment_id"),
            turn_id=predecessor.get("turn_id"),
            status=predecessor_status,
        )
    if (
        predecessor_status != "response_rejected"
        and predecessor.get("recovery_authority") not in RECOVERY_EXACT_AUTHORITIES
    ):
        _state_error(
            "recovery_authority_unproven",
            "A recovery successor requires an exact read-back provenance latch",
            assignment_id=receipt.get("assignment_id"),
            turn_id=predecessor.get("turn_id"),
        )
    if not _proves_recovery_rejection(
        receipt, predecessor, predecessor.get("collection")
    ):
        _state_error(
            "recovery_generation_unproven",
            "A recovery successor requires preserved completed native collection evidence",
            assignment_id=receipt.get("assignment_id"),
            turn_id=predecessor.get("turn_id"),
        )
    if predecessor_status == "response_rejected":
        successors = [
            turn
            for turn in receipt["turns"]
            if turn.get("previous_turn_id") == predecessor.get("turn_id")
            and turn.get("purpose") == purpose
        ]
        if len(successors) != 1:
            _state_error(
                "recovery_successor_invalid",
                "Rejected predecessor does not have exactly one recovery successor",
                assignment_id=receipt.get("assignment_id"),
                turn_id=predecessor.get("turn_id"),
            )
        successor = successors[0]
        _state_error(
            "recovery_successor_already_prepared",
            "Recovery successor is already prepared; do not send a second one",
            assignment_id=receipt.get("assignment_id"),
            turn_id=successor.get("turn_id"),
        )
    child_id = _child_turn_id(str(receipt["assignment_id"]), len(receipt["turns"]) + 1)
    prompt = continuation_prompt(
        assignment_id=str(receipt["assignment_id"]),
        turn_id=child_id,
        next_index=next_index,
        previous_chain_sha256=previous_chain_sha256,
        retransmission=retransmission,
    )
    _transition_turn(
        receipt,
        predecessor,
        status="response_rejected",
        dispatch_status="active",
        response_rejected_at=utc_now(),
        rejection_kind="proven-complete-response-rejected",
        rejection_predecessor_status=predecessor_status,
    )
    # A later trusted complete reread may replace `collection` for the accepted
    # result.  Keep the body-free evidence that justified this rejection
    # immutable and separately auditable rather than losing it during that
    # permitted upgrade.
    predecessor["rejection_collection"] = dict(predecessor["collection"])
    child = _turn(
        turn_id=child_id,
        sequence=len(receipt["turns"]) + 1,
        purpose=purpose,
        previous_turn_id=str(predecessor["turn_id"]),
        wrapped_prompt=prompt,
    )
    child["chunk"] = {
        "expected_index": next_index,
        "expected_previous_chain_sha256": previous_chain_sha256,
        "retransmission": retransmission,
    }
    receipt["turns"].append(child)
    return _prepared_from_turn(receipt, child, prompt, paths)


def _private_directory(path: Path) -> None:
    """Create/check an owned private directory without following symlinks."""

    if path.exists():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _state_error("private_path_invalid", "Private transport path is not a directory", path=str(path))
    else:
        path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        _state_error("private_path_invalid", "Private transport directory is not mode 0700", path=str(path))


def _require_private_directory(path: Path) -> None:
    """Require, but never chmod, a caller-selected result directory."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        _state_error(
            "private_path_invalid",
            "Result parent directory must already exist and be private",
            path=str(path),
        )
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        _state_error("private_path_invalid", "Result parent is not a real directory", path=str(path))
    if stat.S_IMODE(info.st_mode) & 0o077:
        _state_error(
            "private_path_invalid",
            "Result parent directory is not private",
            path=str(path),
        )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _spool_root(runtime: RuntimePaths, assignment_id: str) -> Path:
    """Create the helper-owned spool path for a new private write only."""

    validate_identifier(assignment_id, field="assignment_id")
    root = runtime.spool_dir
    _private_directory(root)
    child = root / assignment_id
    _private_directory(child)
    return child


def _existing_spool_root(runtime: RuntimePaths, assignment_id: str) -> Path | None:
    """Return an existing private spool path without creating or repairing it."""

    validate_identifier(assignment_id, field="assignment_id")
    root = runtime.spool_dir
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        _state_error("private_path_invalid", "Private spool root is not a real directory", path=str(root))
    if stat.S_IMODE(root_info.st_mode) & 0o077:
        _state_error("private_path_invalid", "Private spool root is not private", path=str(root))
    child = root / assignment_id
    try:
        child_info = child.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
        _state_error(
            "private_path_invalid",
            "Private assignment spool is not a real directory",
            path=str(child),
        )
    if stat.S_IMODE(child_info.st_mode) & 0o077:
        _state_error("private_path_invalid", "Private assignment spool is not private", path=str(child))
    return child


def _spool_filename(index: int) -> str:
    if type(index) is not int or index < 1:
        _state_error("spool_path_invalid", "Chunk index must be positive")
    return f"chunk-{index:06d}.part"


def _spool_path(runtime: RuntimePaths, assignment_id: str, index: int) -> Path:
    return _spool_root(runtime, assignment_id) / _spool_filename(index)


def _assert_no_orphan_spool_files(
    receipt: Mapping[str, Any], runtime: RuntimePaths, *, include_pending: bool = True
) -> None:
    """Refuse a spool directory that names content outside this receipt."""

    assignment_id = str(receipt["assignment_id"])
    root = _existing_spool_root(runtime, assignment_id)
    if root is None:
        return
    expected: set[str] = set()
    for turn in receipt.get("turns", []):
        if not isinstance(turn, Mapping) or not isinstance(turn.get("chunk"), Mapping):
            continue
        chunk = turn["chunk"]
        index = chunk.get("index")
        if type(index) is not int or index < 1:
            continue
        if chunk.get("accepted") is True or (
            include_pending and isinstance(chunk.get("spool_write_pending"), Mapping)
        ):
            expected.add(f"chunk-{index:06d}.part")
    actual = {path.name for path in root.iterdir()}
    unexpected = sorted(actual - expected)
    if unexpected:
        _state_error(
            "spool_orphaned",
            "Private spool contains content not named by this receipt",
            assignment_id=assignment_id,
            unexpected_filenames=unexpected,
        )


def _safe_existing_file(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _state_error("spool_path_invalid", "Private spool path is not a regular file", path=str(path))
    if stat.S_IMODE(info.st_mode) != 0o600:
        _state_error("spool_path_invalid", "Private spool file is not mode 0600", path=str(path))
    return path.read_bytes()


def _write_private_once(path: Path, payload: bytes) -> None:
    """Atomically create one private body file, rejecting unexpected overwrite."""

    existing = _safe_existing_file(path)
    digest = hashlib.sha256(payload).hexdigest()
    if existing is not None:
        if hashlib.sha256(existing).hexdigest() == digest and len(existing) == len(payload):
            return
        _state_error(
            "spool_conflict",
            "A private spool file already has different content",
            path=str(path),
            expected_sha256=digest,
            actual_sha256=hashlib.sha256(existing).hexdigest(),
        )
    _private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A hostile local writer is outside the private-state trust boundary;
        # still refuse a race rather than replace a newly appeared target.
        if path.exists():
            _safe_existing_file(path)
            _state_error("spool_conflict", "Spool target appeared during write", path=str(path))
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _write_result_file(path: Path, payload: bytes) -> None:
    """Create an exclusive private materialization target and fsync it."""

    if path.exists() or path.is_symlink():
        _state_error(
            "result_path_exists",
            "Result materialization path already exists; refusing overwrite",
            path=str(path),
        )
    _require_private_directory(path.parent)
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        _state_error("result_path_exists", "Result materialization path already exists", path=str(path))
        raise AssertionError("unreachable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_or_match_private_result(path: Path, payload: bytes) -> None:
    """Materialize once, or accept only an identical private replay target.

    Artifact verification can be retried after a process exits after the result
    file has reached disk but before its receipt update is observed.  Treating an
    already-existing path as a generic overwrite opportunity would weaken the
    output boundary; accepting it is safe only when it is a real mode-0600 file
    whose exact bytes already equal the independently verified object.
    """

    _require_private_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        _write_result_file(path, payload)
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _state_error(
            "result_path_exists",
            "Result materialization path is not a regular private file",
            path=str(path),
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        _state_error(
            "result_path_exists",
            "Result materialization path is not private",
            path=str(path),
        )
    existing = path.read_bytes()
    if len(existing) != len(payload) or hashlib.sha256(existing).digest() != hashlib.sha256(payload).digest():
        _state_error(
            "result_path_exists",
            "Result materialization path already contains different bytes",
            path=str(path),
        )


def _collection_from_evidence(
    evidence: Any, *, status: str, accepted: bool = False
) -> dict[str, Any]:
    value = dict(evidence.receipt_fields())
    value["status"] = status
    value["accepted"] = accepted
    return value


def _evidence_association(
    receipt: Mapping[str, Any], turn: Mapping[str, Any], evidence: Any
) -> str:
    """Check identity, association, finality, and the trusted reread exception.

    The return value is new, idempotent, or upgrade. Observation time is not part
    of content identity, so a fresh observation of immutable content remains
    idempotent.
    """

    worker = receipt.get("worker_conversation_id")
    if (
        evidence.requested_conversation_id != worker
        or evidence.loaded_conversation_id != worker
    ):
        raise CollectionEvidenceError(
            "Native collection evidence is for a different worker",
            details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
            error_code="collection_wrong_worker",
        )
    submitted = turn.get("native_user_message_id")
    if not submitted or evidence.submitted_user_message_id != submitted:
        raise CollectionEvidenceError(
            "Native collection evidence is not bound to the exact verified user message",
            details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
            error_code="collection_submission_mismatch",
        )
    if turn.get("outbound_prompt_verified") is not True or int(turn.get("submission_count", 0)) != 1:
        _state_error(
            "collection_submission_unverified",
            "Collection cannot be accepted before one exact outbound submission",
            assignment_id=receipt.get("assignment_id"),
            turn_id=turn.get("turn_id"),
        )
    submitted_at = turn.get("submitted_at")
    if submitted_at:
        try:
            submitted_time = _parse_utc(str(submitted_at), field="submitted_at")
            observed_time = _parse_utc(str(evidence.observed_at), field="observed_at")
        except DispatchError as exc:
            raise CollectionEvidenceError(
                "Collection timestamps are invalid",
                details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
                error_code="collection_evidence_invalid",
            ) from exc
        if observed_time < submitted_time - dt.timedelta(minutes=5):
            raise CollectionEvidenceError(
                "Collection observation predates verified submission beyond clock skew",
                details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
                error_code="collection_timestamp_invalid",
            )
    collection = turn.get("collection")
    if not isinstance(collection, Mapping) or not collection.get("assistant_message_id"):
        return "new"
    previous_id = collection.get("assistant_message_id")
    previous_identity = collection.get("collection_content_identity_sha256")
    if previous_id != evidence.assistant_message_id:
        raise CollectionEvidenceError(
            "A different assistant message cannot replace a bound turn",
            details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
            error_code="collection_duplicate_response",
        )
    if previous_identity == evidence.content_identity_sha256:
        return "idempotent"
    prior_was_incomplete = (
        collection.get("normalized_truncated") is True
        or collection.get("normalized_outer_truncated") is True
        or collection.get("normalized_truncated") is None
        or collection.get("normalized_outer_truncated") is None
    )
    if (
        prior_was_incomplete
        and evidence.complete_and_untruncated
        and collection.get("adapter_contract_id") == evidence.adapter_contract_id
        and adapter_contract(evidence.adapter_contract_id).supports_complete_reread_upgrade
        and collection.get("accepted") is not True
    ):
        return "upgrade"
    raise CollectionEvidenceError(
        "A bound assistant message has conflicting immutable content",
        details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
        error_code="collection_message_conflict",
    )


def _record_evidence(
    turn: dict[str, Any], evidence: Any, *, status: str, accepted: bool = False
) -> None:
    turn["collection"] = _collection_from_evidence(
        evidence, status=status, accepted=accepted
    )
    turn["updated_at"] = utc_now()


def _accepted_chunk_turns(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for turn in receipt["turns"]:
        if not isinstance(turn, Mapping) or not isinstance(turn.get("chunk"), Mapping):
            continue
        chunk = turn["chunk"]
        if chunk.get("accepted") is not True:
            continue
        if type(chunk.get("index")) is not int or int(chunk["index"]) < 1:
            _migration_error(
                "receipt_schema_unsupported",
                "Accepted chunk has no valid index",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn.get("turn_id"),
            )
        # Callers mutate only receipt-shaped dicts; preserve that invariant here
        # rather than allowing a receipt subclass to make ordering ambiguous.
        chunks.append(dict(turn))
    return sorted(chunks, key=lambda turn: turn["chunk"]["index"])


def _chunk_boundary(receipt: Mapping[str, Any]) -> tuple[int, str]:
    accepted = _accepted_chunk_turns(receipt)
    if not accepted:
        return 1, CHAIN_ZERO_HEX
    for expected, turn in enumerate(accepted, start=1):
        chunk = turn["chunk"]
        if chunk.get("index") != expected:
            _state_error(
                "chunk_receipt_gap",
                "Stored chunk indices are not contiguous",
                assignment_id=receipt.get("assignment_id"),
            )
    last = accepted[-1]["chunk"]
    return int(last["index"]) + 1, str(last["chain_sha256"])


def _reconcile_spool_pending(
    receipt: dict[str, Any], turn: dict[str, Any], runtime: RuntimePaths
) -> bool:
    """Recover a completed local write after a receipt-before-spool crash."""

    chunk = turn.get("chunk")
    if not isinstance(chunk, Mapping) or not isinstance(chunk.get("spool_write_pending"), Mapping):
        return False
    pending = dict(chunk["spool_write_pending"])
    index = pending.get("index")
    digest = pending.get("payload_sha256")
    length = pending.get("byte_length")
    if type(index) is not int or not isinstance(digest, str) or type(length) is not int:
        _migration_error(
            "spool_reconciliation_failed",
            "Chunk spool journal is malformed",
            assignment_id=receipt.get("assignment_id"),
            turn_id=turn.get("turn_id"),
        )
    root = _existing_spool_root(runtime, str(receipt["assignment_id"]))
    if root is None:
        return False
    path = root / _spool_filename(index)
    body = _safe_existing_file(path)
    if body is None:
        return False
    if len(body) != length or hashlib.sha256(body).hexdigest() != digest:
        _migration_error(
            "spool_reconciliation_failed",
            "Chunk spool journal does not match the private file",
            assignment_id=receipt.get("assignment_id"),
            turn_id=turn.get("turn_id"),
        )
    chunk = dict(chunk)
    chunk.pop("spool_write_pending", None)
    chunk["spool_filename"] = path.name
    chunk["spool_status"] = "spooled"
    # The journal and an exact mode-0600 private file together are durable
    # integrity evidence for this chunk.  Leaving it unaccepted would make the
    # receipt boundary, orphan check, and recovery replay disagree about content
    # that has already reached the private spool.
    chunk["accepted"] = True
    turn["chunk"] = chunk
    return True


def _reconcile_pending_spools(
    receipt: dict[str, Any], runtime: RuntimePaths
) -> bool:
    """Finalize only journal entries whose private file already matches.

    A missing pending file is deliberately left pending.  The collector may
    then reread the *same* bound native message and retry the private write;
    treating a missing file as an empty chunk would silently lose content.
    """

    changed = False
    for turn in receipt.get("turns", []):
        if isinstance(turn, dict):
            changed = _reconcile_spool_pending(receipt, turn, runtime) or changed
    return changed


def _spool_chunk(
    receipt: dict[str, Any], turn: dict[str, Any], envelope: Any, runtime: RuntimePaths
) -> None:
    """Write a journal then one content-addressed private payload file."""

    payload = envelope.payload_bytes
    digest = hashlib.sha256(payload).hexdigest()
    path = _spool_path(runtime, str(receipt["assignment_id"]), envelope.index)
    existing = turn.get("chunk")
    if isinstance(existing, Mapping) and existing.get("accepted") is True:
        if (
            existing.get("payload_sha256") == digest
            and existing.get("index") == envelope.index
            and existing.get("chain_sha256") == envelope.chain_sha256
        ):
            return
        _state_error(
            "chunk_message_conflict",
            "Accepted chunk cannot be changed",
            assignment_id=receipt.get("assignment_id"),
            turn_id=turn.get("turn_id"),
        )
    chunk = dict(existing or {})
    chunk.update(
        {
            "index": envelope.index,
            "previous_chain_sha256": envelope.previous_chain_sha256,
            "chain_sha256": envelope.chain_sha256,
            "payload_sha256": digest,
            "byte_length": len(payload),
            "final": envelope.final,
            "count": envelope.count,
            "spool_write_pending": {
                "index": envelope.index,
                "payload_sha256": digest,
                "byte_length": len(payload),
            },
        }
    )
    turn["chunk"] = chunk
    _assert_no_orphan_spool_files(receipt, runtime)
    _save_v2(str(receipt["assignment_id"]), receipt, runtime)
    _write_private_once(path, payload)
    chunk.pop("spool_write_pending", None)
    chunk["spool_filename"] = path.name
    chunk["spool_status"] = "spooled"
    chunk["accepted"] = True
    turn["chunk"] = chunk
    _assert_private_spool_integrity(receipt, runtime)


def _spool_payloads(receipt: Mapping[str, Any], runtime: RuntimePaths) -> list[bytes]:
    _assert_no_orphan_spool_files(receipt, runtime)
    accepted = _accepted_chunk_turns(receipt)
    if not accepted:
        return []
    root = _existing_spool_root(runtime, str(receipt["assignment_id"]))
    if root is None:
        _migration_error(
            "spool_reconciliation_failed",
            "Private spool directory is missing for accepted chunks",
            assignment_id=receipt.get("assignment_id"),
        )
    payloads: list[bytes] = []
    previous_chain_sha256 = CHAIN_ZERO_HEX
    final_seen = False
    for expected_index, turn in enumerate(accepted, start=1):
        chunk = turn["chunk"]
        if chunk.get("index") != expected_index:
            _migration_error(
                "spool_reconciliation_failed",
                "Chunk receipt has a gap",
                assignment_id=receipt.get("assignment_id"),
            )
        if final_seen:
            _migration_error(
                "spool_reconciliation_failed",
                "Accepted spool contains content after a final chunk",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn.get("turn_id"),
            )
        if (
            chunk.get("previous_chain_sha256") != previous_chain_sha256
            or chunk.get("spool_filename") != _spool_filename(expected_index)
            or chunk.get("spool_status") != "spooled"
            or chunk.get("accepted") is not True
            or "spool_write_pending" in chunk
        ):
            _migration_error(
                "spool_reconciliation_failed",
                "Accepted chunk metadata is inconsistent with the spool boundary",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn.get("turn_id"),
            )
        path = root / _spool_filename(expected_index)
        body = _safe_existing_file(path)
        digest = hashlib.sha256(body).hexdigest() if body is not None else None
        if (
            body is None
            or len(body) != chunk.get("byte_length")
            or digest != chunk.get("payload_sha256")
        ):
            _migration_error(
                "spool_reconciliation_failed",
                "Private spool file is missing or corrupt",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn.get("turn_id"),
            )
        try:
            expected_chain_sha256 = chunk_chain(
                previous_chain_sha256, expected_index, body
            )
        except ChunkProtocolError as exc:
            _migration_error(
                "spool_reconciliation_failed",
                "Accepted chunk chain cannot be re-derived",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn.get("turn_id"),
            )
            raise AssertionError("unreachable") from exc
        if chunk.get("chain_sha256") != expected_chain_sha256:
            _migration_error(
                "spool_reconciliation_failed",
                "Accepted chunk chain differs from its private payload",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn.get("turn_id"),
            )
        if (chunk.get("final") and chunk.get("count") != expected_index) or (
            not chunk.get("final") and chunk.get("count") != 0
        ):
            _migration_error(
                "spool_reconciliation_failed",
                "Accepted chunk final/count metadata is inconsistent",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn.get("turn_id"),
            )
        payloads.append(body)
        previous_chain_sha256 = expected_chain_sha256
        final_seen = chunk.get("final") is True
    return payloads


def _has_private_spool_state(receipt: Mapping[str, Any]) -> bool:
    """Whether this receipt has a durable chunk body or write journal to check."""

    return any(
        isinstance(turn, Mapping)
        and isinstance(turn.get("chunk"), Mapping)
        and (
            turn["chunk"].get("accepted") is True
            or isinstance(turn["chunk"].get("spool_write_pending"), Mapping)
        )
        for turn in receipt.get("turns", [])
    )


def _spool_cleanup_recorded(receipt: Mapping[str, Any]) -> bool:
    """Whether a validated receipt explicitly and irreversibly removed parts."""

    delivery = receipt.get("delivery")
    result = receipt.get("result")
    return (
        receipt.get("effective_result_mode") == "chunked"
        and isinstance(result, Mapping)
        and result.get("status") == "complete"
        and isinstance(delivery, Mapping)
        and delivery.get("parent_restoration_status") == "restored"
        and isinstance(delivery.get("spool_cleanup_at"), str)
        and type(delivery.get("spool_cleanup_count")) is int
    )


def _assert_private_spool_integrity(
    receipt: Mapping[str, Any], runtime: RuntimePaths
) -> None:
    """Require each live accepted part and spool directory to match the receipt.

    After explicit cleanup, chunk files are intentionally gone and the immutable
    completed result no longer relies on the spool. Before cleanup this guard is
    used before a child can be armed and before a new chunk can advance the
    chain.
    """

    if _spool_cleanup_recorded(receipt):
        return
    if _has_private_spool_state(receipt):
        # `_spool_payloads` checks orphan names plus every accepted part's real
        # file type/mode/length/digest. Its returned bytes are deliberately
        # discarded here: JSON receipts remain body-free.
        _spool_payloads(receipt, runtime)


def _unarmed_recovery_successor(
    receipt: Mapping[str, Any], predecessor: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the one still-unsent recovery child of a rejected predecessor."""

    successors = [
        turn
        for turn in receipt["turns"]
        if turn.get("previous_turn_id") == predecessor.get("turn_id")
        and turn.get("status") in TURN_ACTIVE_STATUSES
    ]
    if len(successors) != 1 or successors[0].get("status") != "prepared":
        _state_error(
            "reread_upgrade_unavailable",
            "A complete reread cannot supersede an armed or missing recovery child",
            assignment_id=receipt.get("assignment_id"),
            turn_id=predecessor.get("turn_id"),
        )
    return successors[0]


def _prepared_recovery_successor(
    receipt: Mapping[str, Any], child: Mapping[str, Any], paths: RuntimePaths
) -> PreparedAssignment:
    """Regenerate a body-free child prompt from its immutable protocol fields."""

    chunk = child.get("chunk")
    if (
        not isinstance(chunk, Mapping)
        or type(chunk.get("expected_index")) is not int
        or not _is_lower_hex(chunk.get("expected_previous_chain_sha256"))
        or type(chunk.get("retransmission")) is not bool
    ):
        _migration_error(
            "receipt_schema_unsupported",
            "Prepared recovery child lacks its deterministic chunk protocol fields",
            assignment_id=receipt.get("assignment_id"),
            turn_id=child.get("turn_id"),
        )
    prompt = continuation_prompt(
        assignment_id=str(receipt["assignment_id"]),
        turn_id=str(child["turn_id"]),
        next_index=int(chunk["expected_index"]),
        previous_chain_sha256=str(chunk["expected_previous_chain_sha256"]),
        retransmission=bool(chunk["retransmission"]),
    )
    if sha256_text(prompt) != child.get("wrapped_prompt_sha256"):
        _migration_error(
            "receipt_schema_unsupported",
            "Prepared recovery child prompt digest is inconsistent",
            assignment_id=receipt.get("assignment_id"),
            turn_id=child.get("turn_id"),
        )
    return _prepared_from_turn(receipt, child, prompt, paths)


def _cancel_unarmed_recovery_successor(
    receipt: dict[str, Any], predecessor: Mapping[str, Any]
) -> dict[str, Any]:
    """Cancel only an unarmed successor when a trusted reread upgrades a prefix."""

    successor = _unarmed_recovery_successor(receipt, predecessor)
    _transition_turn(receipt, successor, status="failed")
    successor["failed_at"] = utc_now()
    successor["failure_kind"] = "superseded-by-complete-reread"
    return successor


def _finish_result(
    receipt: dict[str, Any],
    *,
    turn: dict[str, Any],
    payload: bytes,
    response_sha256: str,
    completion_basis: str,
    result_path: Path | None,
    source_rejected: bool = False,
) -> CollectionOutcome:
    """Make immutable content completion independent from delivery/navigation."""

    digest = hashlib.sha256(payload).hexdigest()
    existing = receipt["result"]
    if existing.get("status") == "complete":
        if (
            existing.get("response_sha256") != response_sha256
            or existing.get("payload_sha256") != digest
            or existing.get("byte_length") != len(payload)
        ):
            _state_error(
                "result_immutable_conflict",
                "Completed logical result cannot be replaced",
                assignment_id=receipt.get("assignment_id"),
            )
        return CollectionOutcome(
            assignment_id=str(receipt["assignment_id"]),
            turn_id=str(turn["turn_id"]),
            status="complete",
            completion_basis=str(existing.get("completion_basis")),
            result_path=None,
            byte_length=int(existing.get("byte_length", 0)),
            sha256=str(existing.get("payload_sha256")),
        )
    if result_path is not None:
        _write_result_file(result_path, payload)
    if not source_rejected:
        _transition_turn(receipt, turn, status="complete")
        turn["completed_at"] = utc_now()
    else:
        # response_rejected is terminal for send authority. A version-scoped host
        # reread may still supply complete content and complete the logical dispatch.
        turn["trusted_complete_reread_at"] = utc_now()
    receipt["status"] = "complete"
    receipt["result"] = {
        "status": "complete",
        "completion_basis": completion_basis,
        "source_turn_id": turn["turn_id"],
        "response_sha256": response_sha256,
        "payload_sha256": digest,
        "byte_length": len(payload),
        "completed_at": utc_now(),
    }
    receipt["delivery"] = {
        "status": "materialized" if result_path is not None else "not_delivered",
        "parent_restoration_status": "not_started",
    }
    return CollectionOutcome(
        assignment_id=str(receipt["assignment_id"]),
        turn_id=str(turn["turn_id"]),
        status="complete",
        completion_basis=completion_basis,
        result_path=result_path,
        byte_length=len(payload),
        sha256=digest,
    )


def _response_recovery(
    receipt: dict[str, Any],
    turn: dict[str, Any],
    *,
    purpose: str,
    next_index: int,
    previous_chain_sha256: str,
    reason_code: str,
    paths: RuntimePaths,
) -> PreparedAssignment:
    child = _append_child_turn(
        receipt,
        turn,
        purpose=purpose,
        next_index=next_index,
        previous_chain_sha256=previous_chain_sha256,
        retransmission=True,
        paths=paths,
    )
    turn["rejection_reason_code"] = reason_code
    return child


def _known_truncation(evidence: Any) -> bool:
    return (
        evidence.normalized_truncated is True
        or evidence.normalized_outer_truncated is True
    )


def _ensure_known_complete_evidence(
    receipt: dict[str, Any],
    turn: dict[str, Any],
    evidence: Any,
    runtime: RuntimePaths,
) -> str:
    observation = _evidence_association(receipt, turn, evidence)
    if not evidence.has_known_truncation:
        _record_evidence(turn, evidence, status="truncation_unknown", accepted=False)
        _transition_turn(
            receipt,
            turn,
            status="ambiguous",
            dispatch_status="recoverable",
            recovery_authority="collect-only",
        )
        # The fail-closed observation itself is durable state.  Without this
        # write, an exception would discard the raw/normalized provenance and
        # make a later retry look like the first collection attempt.
        _save_v2(str(receipt["assignment_id"]), receipt, runtime)
        raise CollectionEvidenceError(
            "Native collection truncation is unknown for this trusted adapter",
            details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
            error_code="collection_truncation_unknown",
        )
    return observation


def _turn_can_accept_collection(turn: Mapping[str, Any], observation: str) -> bool:
    return turn.get("status") in TURN_ACTIVE_STATUSES or (
        observation == "upgrade" and turn.get("status") == "response_rejected"
    ) or (
        # A process can exit after a private chunk rename and exact journal save,
        # but before advancing the turn.  Exact reconciliation marks that chunk
        # accepted; the same immutable native observation may then resume only
        # the unsaved post-spool transition.  This does not reopen general
        # response_rejected collection or authorize another send.
        observation == "idempotent"
        and turn.get("status") == "response_rejected"
        and isinstance(turn.get("chunk"), Mapping)
        and turn["chunk"].get("accepted") is True
    )


def _is_reconciled_rejected_chunk_resume(
    receipt: Mapping[str, Any], turn: Mapping[str, Any], observation: str
) -> bool:
    """Identify the one terminal-looking state that still needs post-spool work.

    ``response_rejected`` normally remains terminal for native-send authority.
    The exception is deliberately narrower than general rereading: a trusted
    complete reread has already written an exact chunk body and a crash happened
    before the logical dispatch advanced.  Replaying that same observation may
    only finish the locally durable transition; it can never arm or send the
    original/rejected turn again.
    """

    direct_successors = [
        candidate
        for candidate in receipt.get("turns", [])
        if isinstance(candidate, Mapping)
        and candidate.get("previous_turn_id") == turn.get("turn_id")
    ]
    return (
        observation == "idempotent"
        and receipt.get("effective_result_mode") == "chunked"
        and receipt.get("result", {}).get("status") != "complete"
        and turn.get("status") == "response_rejected"
        and isinstance(turn.get("chunk"), Mapping)
        and turn["chunk"].get("accepted") is True
        # The first post-crash resume consumes exactly this one unarmed recovery
        # child.  After it has advanced, later identical rereads are ordinary
        # terminal idempotent no-ops rather than attempts to cancel it again.
        and len(direct_successors) == 1
        and direct_successors[0].get("status") == "prepared"
    )


def _next_chunk_turn(
    receipt: dict[str, Any],
    turn: dict[str, Any],
    *,
    next_index: int,
    previous_chain_sha256: str,
    paths: RuntimePaths,
    source_rejected: bool,
    continuation_parent_turn_id: str | None = None,
) -> PreparedAssignment:
    """Accept a nonfinal chunk then prepare the one next child under the lock."""

    if source_rejected:
        if continuation_parent_turn_id is None:
            _state_error(
                "reread_upgrade_unavailable",
                "A complete reread needs its existing unarmed recovery child",
                assignment_id=receipt.get("assignment_id"),
                turn_id=turn.get("turn_id"),
            )
        child_id = _child_turn_id(str(receipt["assignment_id"]), len(receipt["turns"]) + 1)
        prompt = continuation_prompt(
            assignment_id=str(receipt["assignment_id"]),
            turn_id=child_id,
            next_index=next_index,
            previous_chain_sha256=previous_chain_sha256,
            retransmission=False,
        )
        child = _turn(
            turn_id=child_id,
            sequence=len(receipt["turns"]) + 1,
            purpose="chunk-continuation",
            previous_turn_id=continuation_parent_turn_id,
            wrapped_prompt=prompt,
        )
        child["chunk"] = {
            "expected_index": next_index,
            "expected_previous_chain_sha256": previous_chain_sha256,
            "retransmission": False,
        }
        receipt["turns"].append(child)
        receipt["status"] = "active"
        return _prepared_from_turn(receipt, child, prompt, paths)
    _transition_turn(receipt, turn, status="complete", dispatch_status="active")
    turn["completed_at"] = utc_now()
    child_id = _child_turn_id(str(receipt["assignment_id"]), len(receipt["turns"]) + 1)
    prompt = continuation_prompt(
        assignment_id=str(receipt["assignment_id"]),
        turn_id=child_id,
        next_index=next_index,
        previous_chain_sha256=previous_chain_sha256,
        retransmission=False,
    )
    child = _turn(
        turn_id=child_id,
        sequence=len(receipt["turns"]) + 1,
        purpose="chunk-continuation",
        previous_turn_id=str(turn["turn_id"]),
        wrapped_prompt=prompt,
    )
    child["chunk"] = {
        "expected_index": next_index,
        "expected_previous_chain_sha256": previous_chain_sha256,
        "retransmission": False,
    }
    receipt["turns"].append(child)
    return _prepared_from_turn(receipt, child, prompt, paths)


def _collect_inline(
    receipt: dict[str, Any],
    turn: dict[str, Any],
    evidence: Any,
    *,
    observation: str,
    result_path: Path | None,
    runtime: RuntimePaths,
) -> CollectionOutcome:
    source_rejected = turn.get("status") == "response_rejected"
    response = evidence.text
    if is_chunked_required_control(response, turn_id=str(turn["turn_id"])):
        _record_evidence(turn, evidence, status="chunked-required-control", accepted=False)
        child = (
            _prepared_recovery_successor(
                receipt, _unarmed_recovery_successor(receipt, turn), runtime
            )
            if source_rejected
            else _response_recovery(
                receipt,
                turn,
                purpose="inline-chunked-escalation",
                next_index=1,
                previous_chain_sha256=CHAIN_ZERO_HEX,
                reason_code="chunked-required-control",
                paths=runtime,
            )
        )
        receipt["effective_result_mode"] = "chunked"
        _save_v2(str(receipt["assignment_id"]), receipt, runtime)
        return CollectionOutcome(
            assignment_id=str(receipt["assignment_id"]),
            turn_id=str(turn["turn_id"]),
            status="active",
            completion_basis=None,
            result_path=None,
            byte_length=None,
            sha256=None,
            next_turn=child,
        )
    if len(response.encode("utf-8")) > 16_000:
        _record_evidence(turn, evidence, status="inline-limit-exceeded", accepted=False)
        child = (
            _prepared_recovery_successor(
                receipt, _unarmed_recovery_successor(receipt, turn), runtime
            )
            if source_rejected
            else _response_recovery(
                receipt,
                turn,
                purpose="inline-limit-retransmission",
                next_index=1,
                previous_chain_sha256=CHAIN_ZERO_HEX,
                reason_code="inline-limit-exceeded",
                paths=runtime,
            )
        )
        receipt["effective_result_mode"] = "chunked"
        _save_v2(str(receipt["assignment_id"]), receipt, runtime)
        raise CollectionEvidenceError(
            "Untruncated inline response exceeds the complete-message limit",
            details={
                "assignment_id": receipt.get("assignment_id"),
                "turn_id": turn.get("turn_id"),
                "recoverable": True,
                "next_turn_id": child.turn_id,
            },
            error_code="inline_limit_exceeded",
        )
    normalized, payload = parse_result(response, str(turn["turn_id"]))
    response_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    if source_rejected:
        _cancel_unarmed_recovery_successor(receipt, turn)
    _record_evidence(turn, evidence, status="accepted", accepted=True)
    outcome = _finish_result(
        receipt,
        turn=turn,
        payload=payload.encode("utf-8"),
        response_sha256=response_digest,
        completion_basis="native-inline",
        result_path=result_path,
        source_rejected=source_rejected,
    )
    _save_v2(str(receipt["assignment_id"]), receipt, runtime)
    return outcome


def _matches_accepted_spooled_envelope(turn: Mapping[str, Any], envelope: ChunkEnvelope) -> bool:
    """Return whether an idempotent reread is the exact durable chunk.

    This narrow predicate is only for the rename-before-receipt crash window.
    It does not make a generic repeated response acceptable after a turn has
    progressed, and it rejects receipt tampering rather than trusting a payload
    merely because its collection identity is unchanged.
    """

    chunk = turn.get("chunk")
    if not isinstance(chunk, Mapping) or chunk.get("accepted") is not True:
        return False
    exact = (
        type(chunk.get("index")) is int
        and chunk.get("index") == envelope.index
        and isinstance(chunk.get("previous_chain_sha256"), str)
        and chunk.get("previous_chain_sha256") == envelope.previous_chain_sha256
        and isinstance(chunk.get("chain_sha256"), str)
        and chunk.get("chain_sha256") == envelope.chain_sha256
        and isinstance(chunk.get("payload_sha256"), str)
        and chunk.get("payload_sha256") == envelope.payload_sha256
        and type(chunk.get("byte_length")) is int
        and chunk.get("byte_length") == len(envelope.payload_bytes)
        and type(chunk.get("final")) is bool
        and chunk.get("final") is envelope.final
        and type(chunk.get("count")) is int
        and chunk.get("count") == envelope.count
        and chunk.get("spool_filename") == f"chunk-{envelope.index:06d}.part"
        and chunk.get("spool_status") == "spooled"
    )
    if not exact:
        _state_error(
            "chunk_message_conflict",
            "Accepted spool metadata differs from its idempotent native chunk",
            assignment_id=turn.get("assignment_id"),
            turn_id=turn.get("turn_id"),
        )
    return True


def _advance_spooled_chunk(
    receipt: dict[str, Any],
    turn: dict[str, Any],
    envelope: ChunkEnvelope,
    *,
    result_path: Path | None,
    runtime: RuntimePaths,
    source_rejected: bool,
) -> CollectionOutcome:
    """Advance exactly one already-durable accepted chunk.

    The helper is shared by the normal post-spool path and a same-message
    idempotent reread after journal reconciliation.  In either case, no native
    send occurs here.
    """

    if not envelope.final:
        continuation_parent_turn_id = None
        if source_rejected:
            superseded = _cancel_unarmed_recovery_successor(receipt, turn)
            continuation_parent_turn_id = str(superseded["turn_id"])
        child = _next_chunk_turn(
            receipt,
            turn,
            next_index=envelope.index + 1,
            previous_chain_sha256=envelope.chain_sha256,
            paths=runtime,
            source_rejected=source_rejected,
            continuation_parent_turn_id=continuation_parent_turn_id,
        )
        _save_v2(str(receipt["assignment_id"]), receipt, runtime)
        return CollectionOutcome(
            assignment_id=str(receipt["assignment_id"]),
            turn_id=str(turn["turn_id"]),
            status="active",
            completion_basis=None,
            result_path=None,
            byte_length=None,
            sha256=None,
            accepted_chunk={
                "index": envelope.index,
                "byte_length": len(envelope.payload_bytes),
                "sha256": envelope.payload_sha256,
                "chain_sha256": envelope.chain_sha256,
            },
            next_turn=child,
        )
    if source_rejected:
        _cancel_unarmed_recovery_successor(receipt, turn)
    payload = b"".join(_spool_payloads(receipt, runtime))
    response_digest = hashlib.sha256(payload).hexdigest()
    outcome = _finish_result(
        receipt,
        turn=turn,
        payload=payload,
        response_sha256=response_digest,
        completion_basis="native-chunked",
        result_path=result_path,
        source_rejected=source_rejected,
    )
    receipt["result"]["chunk_count"] = envelope.count
    receipt["result"]["final_chain_sha256"] = envelope.chain_sha256
    _save_v2(str(receipt["assignment_id"]), receipt, runtime)
    return CollectionOutcome(
        assignment_id=outcome.assignment_id,
        turn_id=outcome.turn_id,
        status=outcome.status,
        completion_basis=outcome.completion_basis,
        result_path=outcome.result_path,
        byte_length=outcome.byte_length,
        sha256=outcome.sha256,
        accepted_chunk={
            "index": envelope.index,
            "byte_length": len(envelope.payload_bytes),
            "sha256": envelope.payload_sha256,
            "chain_sha256": envelope.chain_sha256,
        },
    )


def _collect_chunked(
    receipt: dict[str, Any],
    turn: dict[str, Any],
    evidence: Any,
    *,
    observation: str,
    result_path: Path | None,
    runtime: RuntimePaths,
) -> CollectionOutcome:
    source_rejected = turn.get("status") == "response_rejected"
    # Do not advance a later continuation from metadata alone. Every prior
    # accepted part must still be the exact private file named by the receipt.
    _assert_private_spool_integrity(receipt, runtime)
    expected_index, previous = _chunk_boundary(receipt)
    try:
        envelope = parse_chunk_response(evidence.text, turn_id=str(turn["turn_id"]))
        if envelope.group_id != receipt.get("assignment_id"):
            raise ChunkProtocolError(
                "Chunk group does not match dispatch",
                details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
                error_code="chunk_envelope_incomplete",
            )
        resuming_spooled_chunk = (
            observation == "idempotent"
            and _matches_accepted_spooled_envelope(turn, envelope)
        )
        if resuming_spooled_chunk:
            if (
                expected_index != envelope.index + 1
                or previous != envelope.chain_sha256
            ):
                _state_error(
                    "chunk_receipt_gap",
                    "Accepted spool cannot resume beyond the current chain boundary",
                    assignment_id=receipt.get("assignment_id"),
                    turn_id=turn.get("turn_id"),
                )
        elif envelope.index > expected_index:
            raise ChunkProtocolError(
                "Chunk index skips an unaccepted predecessor",
                details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
                error_code="chunk_gap",
            )
        elif envelope.index < expected_index:
            raise ChunkProtocolError(
                "Chunk index replays an earlier accepted chunk",
                details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
                error_code="chunk_replay",
            )
        if not resuming_spooled_chunk and envelope.previous_chain_sha256 != previous:
            raise ChunkProtocolError(
                "Chunk previous chain digest does not match the accepted boundary",
                details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
                error_code="chunk_chain_mismatch",
            )
        if envelope.final and not envelope.payload and not _accepted_chunk_turns(receipt):
            raise ChunkProtocolError(
                "An empty final chunk requires an earlier nonempty accepted chunk",
                details={"assignment_id": receipt.get("assignment_id"), "turn_id": turn.get("turn_id")},
                error_code="chunk_envelope_incomplete",
            )
    except ChunkProtocolError:
        _record_evidence(turn, evidence, status="rejected", accepted=False)
        if not source_rejected:
            _response_recovery(
                receipt,
                turn,
                purpose="chunk-repair",
                next_index=expected_index,
                previous_chain_sha256=previous,
                reason_code="chunk-protocol-error",
                paths=runtime,
            )
        _save_v2(str(receipt["assignment_id"]), receipt, runtime)
        raise

    if resuming_spooled_chunk:
        return _advance_spooled_chunk(
            receipt,
            turn,
            envelope,
            result_path=result_path,
            runtime=runtime,
            source_rejected=source_rejected,
        )
    _record_evidence(turn, evidence, status="accepted", accepted=True)
    _spool_chunk(receipt, turn, envelope, runtime)
    return _advance_spooled_chunk(
        receipt,
        turn,
        envelope,
        result_path=result_path,
        runtime=runtime,
        source_rejected=source_rejected,
    )


def _collect_artifact_manifest(
    receipt: dict[str, Any], turn: dict[str, Any], evidence: Any, runtime: RuntimePaths
) -> CollectionOutcome:
    stored = receipt.get("artifact_contract")
    if not isinstance(stored, Mapping) or not isinstance(stored.get("contract"), Mapping):
        raise ArtifactProtocolError(
            "Artifact receipt lacks a prepared contract",
            error_code="artifact_authorization_missing",
        )
    contract = ArtifactContract.from_mapping(stored["contract"])
    manifest = parse_artifact_manifest(
        evidence.text, assignment_id=str(receipt["assignment_id"]), contract=contract
    )
    _record_evidence(turn, evidence, status="accepted", accepted=True)
    _transition_turn(receipt, turn, status="complete", dispatch_status="verifying")
    turn["completed_at"] = utc_now()
    receipt["artifact_manifest"] = {
        "assignment_id": manifest.assignment_id,
        "repository_id": manifest.repository_id,
        "repository": manifest.repository,
        "remote_url": manifest.remote_url,
        "base_branch": manifest.base_branch,
        "base_sha": manifest.base_sha,
        "branch": manifest.branch,
        "commit_sha": manifest.commit_sha,
        "content_sha256": manifest.content_sha256,
        "byte_length": manifest.byte_length,
        "path": manifest.path,
        "encoding": manifest.encoding,
        "media_type": manifest.media_type,
        "changed_path_count": manifest.changed_path_count,
        "commit_message": manifest.commit_message,
    }
    _save_v2(str(receipt["assignment_id"]), receipt, runtime)
    return CollectionOutcome(
        assignment_id=str(receipt["assignment_id"]),
        turn_id=str(turn["turn_id"]),
        status="verifying",
        completion_basis=None,
        result_path=None,
        byte_length=None,
        sha256=None,
    )


def collect_turn_v2(
    assignment_id: str,
    turn_id: str,
    evidence: Any,
    result_path: Path | str | None = None,
    *,
    paths: RuntimePaths | None = None,
) -> CollectionOutcome:
    """Accept trusted collection evidence for precisely one turn.

    A native evidence object is mandatory. No caller supplied boolean can make an
    incomplete or unknown observation look complete.
    """

    if not isinstance(evidence, NativeCollectionEvidence):
        raise CollectionEvidenceError(
            "collect_turn requires a validated native collection evidence envelope",
            details={"assignment_id": assignment_id, "turn_id": turn_id},
            error_code="collection_metadata_missing",
        )
    runtime = paths or default_paths()
    destination = Path(result_path) if result_path is not None else None
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        turn = _find_turn(receipt, turn_id)
        observation = _ensure_known_complete_evidence(receipt, turn, evidence, runtime)
        if (
            observation == "upgrade"
            and turn.get("status") == "response_rejected"
            and receipt.get("requested_result_mode") == "inline"
        ):
            # The recovery child has not been sent yet.  A trusted complete reread
            # of the original inline result supersedes that provisional escalation.
            receipt["effective_result_mode"] = "inline"
        if (
            observation == "idempotent"
            and turn.get("status") in TURN_TERMINAL_STATUSES
            and not _is_reconciled_rejected_chunk_resume(receipt, turn, observation)
        ):
            result = receipt["result"]
            chunk = turn.get("chunk")
            return CollectionOutcome(
                assignment_id=assignment_id,
                turn_id=turn_id,
                status=str(receipt.get("status")),
                completion_basis=result.get("completion_basis"),
                result_path=None,
                byte_length=result.get("byte_length"),
                sha256=result.get("payload_sha256"),
                accepted_chunk=(
                    {
                        "index": chunk.get("index"),
                        "byte_length": chunk.get("byte_length"),
                        "sha256": chunk.get("payload_sha256"),
                        "chain_sha256": chunk.get("chain_sha256"),
                    }
                    if isinstance(chunk, Mapping) and chunk.get("accepted") is True
                    else None
                ),
            )
        if not _turn_can_accept_collection(turn, observation):
            if observation == "idempotent" and receipt["result"].get("status") == "complete":
                result = receipt["result"]
                return CollectionOutcome(
                    assignment_id=assignment_id,
                    turn_id=turn_id,
                    status="complete",
                    completion_basis=result.get("completion_basis"),
                    result_path=None,
                    byte_length=result.get("byte_length"),
                    sha256=result.get("payload_sha256"),
                )
            _state_error(
                "collection_turn_terminal",
                "Collection cannot alter a terminal turn",
                assignment_id=assignment_id,
                turn_id=turn_id,
                status=turn.get("status"),
            )

        if _known_truncation(evidence):
            _record_evidence(turn, evidence, status="truncated", accepted=False)
            if receipt.get("effective_result_mode") == "artifact":
                _transition_turn(
                    receipt,
                    turn,
                    status="ambiguous",
                    dispatch_status="recoverable",
                    recovery_authority="collect-only",
                )
                _save_v2(assignment_id, receipt, runtime)
                raise TruncationError(
                    "Artifact manifest collection is truncated; remote discovery remains available",
                    details={"assignment_id": assignment_id, "turn_id": turn_id, "recoverable": True},
                    error_code="collection_truncated",
                )
            next_index, previous = _chunk_boundary(receipt)
            child = _response_recovery(
                receipt,
                turn,
                purpose="truncated-retransmission",
                next_index=next_index,
                previous_chain_sha256=previous,
                reason_code="collection-truncated",
                paths=runtime,
            )
            receipt["effective_result_mode"] = "chunked"
            _save_v2(assignment_id, receipt, runtime)
            raise TruncationError(
                "Native collection was truncated; a fresh child retransmission is prepared",
                details={
                    "assignment_id": assignment_id,
                    "turn_id": turn_id,
                    "recoverable": True,
                    "no_resend": True,
                    "next_turn_id": child.turn_id,
                },
                error_code="collection_truncated",
            )

        mode = str(receipt["effective_result_mode"])
        if mode == "inline":
            return _collect_inline(
                receipt,
                turn,
                evidence,
                observation=observation,
                result_path=destination,
                runtime=runtime,
            )
        if mode == "chunked":
            return _collect_chunked(
                receipt,
                turn,
                evidence,
                observation=observation,
                result_path=destination,
                runtime=runtime,
            )
        if mode == "artifact":
            return _collect_artifact_manifest(receipt, turn, evidence, runtime)
        _migration_error("receipt_schema_unsupported", "Receipt effective result mode is invalid")


def _stored_artifact_contract(receipt: Mapping[str, Any]) -> ArtifactContract:
    stored = receipt.get("artifact_contract")
    if (
        not isinstance(stored, Mapping)
        or stored.get("write_authorized") is not True
        or not isinstance(stored.get("contract"), Mapping)
    ):
        raise ArtifactProtocolError(
            "Artifact receipt lacks an explicit prepared authorization",
            error_code="artifact_authorization_missing",
        )
    contract = ArtifactContract.from_mapping(stored["contract"])
    if stored.get("sha256") != contract.sha256:
        raise ArtifactVerificationError(
            "Stored artifact contract hash differs from its canonical contract",
            details={"assignment_id": receipt.get("assignment_id")},
            error_code="artifact_contract_conflict",
        )
    if contract.visibility == "public" and stored.get("public_retention_acknowledged") is not True:
        raise ArtifactProtocolError(
            "Public artifact retention acknowledgement is absent",
            error_code="artifact_public_retention_unacknowledged",
        )
    return contract


def _stored_artifact_manifest(
    receipt: Mapping[str, Any], contract: ArtifactContract
) -> ArtifactManifest | None:
    value = receipt.get("artifact_manifest")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _migration_error("receipt_schema_unsupported", "Stored artifact manifest is invalid")
    required = {
        "assignment_id",
        "repository_id",
        "repository",
        "remote_url",
        "base_branch",
        "base_sha",
        "branch",
        "commit_sha",
        "path",
        "byte_length",
        "content_sha256",
        "encoding",
        "media_type",
        "changed_path_count",
        "commit_message",
    }
    if set(value) != required:
        _migration_error("receipt_schema_unsupported", "Stored artifact manifest is incomplete")
    try:
        manifest = ArtifactManifest(
            assignment_id=str(value["assignment_id"]),
            repository_id=int(value["repository_id"]),
            repository=str(value["repository"]),
            remote_url=str(value["remote_url"]),
            base_branch=str(value["base_branch"]),
            base_sha=str(value["base_sha"]),
            branch=str(value["branch"]),
            commit_sha=str(value["commit_sha"]),
            path=str(value["path"]),
            byte_length=int(value["byte_length"]),
            content_sha256=str(value["content_sha256"]),
            encoding=str(value["encoding"]),
            media_type=str(value["media_type"]),
            changed_path_count=int(value["changed_path_count"]),
            commit_message=str(value["commit_message"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _migration_error("receipt_schema_unsupported", "Stored artifact manifest is invalid")
        raise AssertionError("unreachable") from exc
    try:
        # Do not trust prior wire validation once the values have crossed the
        # receipt boundary: this reruns exact contract/hash/size checks before
        # any field can reach the bare Git verifier.
        return validate_artifact_manifest(
            manifest,
            assignment_id=str(receipt["assignment_id"]),
            contract=contract,
        )
    except ArtifactProtocolError as exc:
        _migration_error(
            "receipt_schema_unsupported",
            "Stored artifact manifest conflicts with its contract",
        )
        raise AssertionError("unreachable") from exc


def _complete_artifact_dispatch(
    receipt: dict[str, Any],
    verification: ArtifactVerificationResult,
    *,
    result_path: Path,
    completion_basis: str,
    runtime: RuntimePaths,
) -> None:
    _write_or_match_private_result(result_path, verification.content)
    for turn in _unresolved_turns(receipt):
        # Artifact discovery is a separately authorized remote proof, not a
        # rejected native response that needs a send-capable successor.  Reserve
        # response_rejected for recovery predecessors, each of which has exactly
        # one child under the state lock.
        _transition_turn(receipt, turn, status="complete")
        turn["completed_at"] = utc_now()
        turn["completion_kind"] = "artifact-remote-verification"
    receipt["status"] = "complete"
    receipt["result"] = {
        "status": "complete",
        "completion_basis": completion_basis,
        "source_turn_id": (
            receipt["turns"][-1]["turn_id"] if receipt.get("turns") else None
        ),
        "payload_sha256": verification.content_sha256,
        "response_sha256": verification.content_sha256,
        "byte_length": verification.byte_length,
        "verified_at": utc_now(),
        "artifact": verification.receipt_fields(),
    }
    receipt["delivery"] = {
        "status": "materialized",
        "parent_restoration_status": "not_started",
    }
    receipt.pop("artifact_verification_pending", None)
    _save_v2(str(receipt["assignment_id"]), receipt, runtime)


def _artifact_manifest_digest(receipt: Mapping[str, Any]) -> str | None:
    """Hash the body-free manifest binding used by a verification nonce."""

    stored = receipt.get("artifact_manifest")
    if stored is None:
        return None
    if not isinstance(stored, Mapping):
        _migration_error("receipt_schema_unsupported", "Stored artifact manifest is invalid")
    return hashlib.sha256(canonical_json_bytes(dict(stored))).hexdigest()


def _pending_artifact_verification_is_stale(pending: Mapping[str, Any]) -> bool:
    started_at = pending.get("started_at")
    if not isinstance(started_at, str):
        _migration_error(
            "receipt_schema_unsupported",
            "Artifact verification pending record lacks its start time",
        )
    started = _parse_utc(started_at, field="artifact_verification_pending.started_at")
    now = dt.datetime.now(dt.timezone.utc)
    if started > now + dt.timedelta(minutes=5):
        _migration_error(
            "receipt_schema_unsupported",
            "Artifact verification pending record has an implausible future start time",
        )
    return now - started >= dt.timedelta(seconds=ARTIFACT_VERIFICATION_STALE_SECONDS)


def _completed_artifact_replay(
    assignment_id: str,
    destination: Path,
    *,
    verifier: GitArtifactVerifier,
    runtime: RuntimePaths,
) -> ArtifactVerificationResult:
    """Materialize the exact completed blob without consulting a mutable branch.

    A branch is intentionally permitted to drift after completion.  Replaying a
    completed verification therefore fetches the content-addressed commit/blob
    from the immutable receipt instead of re-checking a branch head and turning
    harmless later cleanup or movement into permission to rewrite completion.
    """

    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        if receipt.get("status") != "complete" or receipt.get("effective_result_mode") != "artifact":
            _state_error(
                "artifact_not_verifiable",
                "Artifact receipt is no longer a completed artifact dispatch",
                assignment_id=assignment_id,
            )
        contract = _stored_artifact_contract(receipt)
        result = receipt.get("result")
        if not isinstance(result, Mapping) or result.get("status") != "complete":
            _migration_error("receipt_schema_unsupported", "Completed artifact result is invalid")
        artifact = result.get("artifact")
        if not isinstance(artifact, Mapping):
            _migration_error("receipt_schema_unsupported", "Completed artifact result lacks object identity")
        required = {
            "branch",
            "commit_sha",
            "tree_sha",
            "blob_sha",
            "file_mode",
            "byte_length",
            "content_sha256",
            "base_state",
            "branch_head_before",
            "branch_head_after",
        }
        if not required <= set(artifact):
            _migration_error("receipt_schema_unsupported", "Completed artifact result is incomplete")
        request = {
            "contract_sha256": contract.sha256,
            "commit_sha": artifact["commit_sha"],
            "path": contract.path,
            "blob_sha": artifact["blob_sha"],
            "byte_length": artifact["byte_length"],
            "content_sha256": artifact["content_sha256"],
            "result_sha256": result.get("payload_sha256"),
            "artifact": dict(artifact),
        }

    try:
        content = verifier.fetch_verified_blob(
            contract,
            commit_sha=str(request["commit_sha"]),
            path=str(request["path"]),
            blob_sha=str(request["blob_sha"]),
            byte_length=int(request["byte_length"]),
            content_sha256=str(request["content_sha256"]),
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactVerificationError(
            "Completed artifact receipt has invalid object identity",
            details={"assignment_id": assignment_id},
            error_code="artifact_hash_mismatch",
        ) from exc
    if (
        hashlib.sha256(content).hexdigest() != request["result_sha256"]
        or len(content) != request["byte_length"]
    ):
        raise ArtifactVerificationError(
            "Fetched artifact differs from immutable result receipt",
            details={"assignment_id": assignment_id},
            error_code="artifact_hash_mismatch",
        )

    with state_lock(runtime):
        current = _load_mutable_v2(assignment_id, runtime)
        current_result = current.get("result")
        if (
            current.get("status") != "complete"
            or not isinstance(current_result, Mapping)
            or current_result.get("payload_sha256") != request["result_sha256"]
            or _stored_artifact_contract(current).sha256 != request["contract_sha256"]
        ):
            _state_error(
                "result_materialization_raced",
                "Artifact result changed while its exact blob was fetched",
                assignment_id=assignment_id,
            )
        _write_or_match_private_result(destination, content)
        delivery = dict(current.get("delivery") or {})
        delivery["status"] = "materialized"
        delivery["materialized_at"] = utc_now()
        current["delivery"] = delivery
        _save_v2(assignment_id, current, runtime)

    artifact = request["artifact"]
    return ArtifactVerificationResult(
        branch=str(artifact["branch"]),
        commit_sha=str(artifact["commit_sha"]),
        tree_sha=str(artifact["tree_sha"]),
        blob_sha=str(artifact["blob_sha"]),
        file_mode=str(artifact["file_mode"]),
        byte_length=int(artifact["byte_length"]),
        content_sha256=str(artifact["content_sha256"]),
        base_state=str(artifact["base_state"]),
        branch_head_before=str(artifact["branch_head_before"]),
        branch_head_after=str(artifact["branch_head_after"]),
        content=content,
    )


def verify_artifact_v2(
    assignment_id: str,
    result_path: Path | str,
    *,
    discover: bool = False,
    verifier: GitArtifactVerifier | None = None,
    paths: RuntimePaths | None = None,
) -> ArtifactVerificationResult:
    """Verify one exact remote object graph without holding the state lock for Git."""

    runtime = paths or default_paths()
    destination = Path(result_path)
    active_verifier = verifier or GitArtifactVerifier()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        if receipt.get("requested_result_mode") != "artifact" or receipt.get("effective_result_mode") != "artifact":
            raise ArtifactProtocolError(
                "Artifact verification is available only for explicit artifact mode",
                error_code="artifact_authorization_missing",
            )
        if receipt.get("status") == "complete":
            completed = True
        else:
            completed = False
        if completed:
            # Leave the lock before exact-object reads.  This replay cannot send,
            # mutate a result identity, or authorize a branch write.
            pass
        else:
            pending = receipt.get("artifact_verification_pending")
            if receipt.get("status") == "verifying":
                if pending is not None:
                    if not isinstance(pending, Mapping):
                        _migration_error(
                            "receipt_schema_unsupported",
                            "Artifact verification pending record is invalid",
                        )
                    if not _pending_artifact_verification_is_stale(pending):
                        _state_error(
                            "verification_in_progress",
                            "Artifact verification is already in progress",
                            assignment_id=assignment_id,
                        )
                    # A crashed verifier has no send authority.  Clear only its
                    # nonce under the lock so a later verifier starts a fresh
                    # read-only verification session.
                    receipt["status"] = "recoverable"
                    receipt.pop("artifact_verification_pending", None)
                    receipt["artifact_verification_failure"] = {
                        "error_code": "artifact_verification_stale",
                        "at": utc_now(),
                    }
                    _save_v2(assignment_id, receipt, runtime)
                # A collected manifest deliberately enters `verifying` before
                # a verifier owns a nonce.  It is ready for exactly one
                # read-only verifier to claim below; a second caller will see
                # the nonce written by that first caller under this same lock.
            if receipt.get("status") not in {"recoverable", "active", "verifying"}:
                _state_error(
                    "artifact_not_verifiable",
                    "Artifact dispatch is not in a verifiable state",
                    assignment_id=assignment_id,
                    status=receipt.get("status"),
                )
            contract = _stored_artifact_contract(receipt)
            manifest = _stored_artifact_manifest(receipt, contract)
            if manifest is None and not discover:
                raise ArtifactProtocolError(
                    "A canonical artifact manifest is required unless explicit discovery is requested",
                    error_code="artifact_manifest_invalid",
                )
            # Discovery is the sole exception to readable chat evidence, and only an
            # already-authorized artifact receipt reaches this point.
            nonce = os.urandom(16).hex()
            nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
            manifest_sha256 = _artifact_manifest_digest(receipt)
            receipt["artifact_verification_pending"] = {
                "nonce_sha256": nonce_hash,
                "started_at": utc_now(),
                "contract_sha256": contract.sha256,
                "manifest_sha256": manifest_sha256,
                "discover": bool(discover),
            }
            receipt["status"] = "verifying"
            _save_v2(assignment_id, receipt, runtime)

    if completed:
        return _completed_artifact_replay(
            assignment_id, destination, verifier=active_verifier, runtime=runtime
        )

    try:
        verification = active_verifier.verify(contract, manifest=manifest)
    except DispatchError as exc:
        with state_lock(runtime):
            current = _load_mutable_v2(assignment_id, runtime)
            pending = current.get("artifact_verification_pending")
            if isinstance(pending, Mapping) and pending.get("nonce_sha256") == nonce_hash:
                current["status"] = "recoverable"
                current["artifact_verification_failure"] = {
                    "error_code": exc.error_code,
                    "at": utc_now(),
                }
                current.pop("artifact_verification_pending", None)
                _save_v2(assignment_id, current, runtime)
        raise

    with state_lock(runtime):
        current = _load_mutable_v2(assignment_id, runtime)
        pending = current.get("artifact_verification_pending")
        if (
            current.get("status") != "verifying"
            or not isinstance(pending, Mapping)
            or pending.get("nonce_sha256") != nonce_hash
            or pending.get("contract_sha256") != contract.sha256
            or pending.get("manifest_sha256") != _artifact_manifest_digest(current)
            or _stored_artifact_contract(current).sha256 != contract.sha256
        ):
            _state_error(
                "artifact_verification_raced",
                "Artifact receipt changed while remote verification ran",
                assignment_id=assignment_id,
            )
        _complete_artifact_dispatch(
            current,
            verification,
            result_path=destination,
            completion_basis="artifact-manifest" if manifest is not None else "artifact-discovery",
            runtime=runtime,
        )
    return verification


def record_parent_restoration_v2(
    assignment_id: str,
    *,
    restored: bool,
    paths: RuntimePaths | None = None,
) -> dict[str, Any]:
    """Record navigation restoration without reopening immutable result content."""

    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        if receipt.get("result", {}).get("status") != "complete":
            _state_error(
                "result_not_complete",
                "Parent restoration is recorded only after immutable completion",
                assignment_id=assignment_id,
            )
        delivery = dict(receipt.get("delivery") or {})
        delivery["parent_restoration_status"] = "restored" if restored else "failed"
        delivery["parent_restoration_updated_at"] = utc_now()
        if restored:
            delivery["parent_restored_at"] = utc_now()
        receipt["delivery"] = delivery
        _save_v2(assignment_id, receipt, runtime)
        return receipt


def materialize_result_v2(
    assignment_id: str,
    result_path: Path | str,
    *,
    verifier: GitArtifactVerifier | None = None,
    paths: RuntimePaths | None = None,
) -> ResultDescriptor:
    """Copy verified retained content without changing immutable result completion."""

    runtime = paths or default_paths()
    destination = Path(result_path)
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        result = receipt.get("result")
        if not isinstance(result, Mapping) or result.get("status") != "complete":
            _state_error("result_not_complete", "Result is not complete", assignment_id=assignment_id)
        mode = receipt.get("effective_result_mode")
        if mode == "inline":
            _state_error(
                "inline_result_not_retained",
                "Inline result bodies are transient and cannot be rematerialized",
                assignment_id=assignment_id,
            )
        if mode == "chunked":
            if _spool_cleanup_recorded(receipt):
                _state_error(
                    "spool_cleaned",
                    "Chunk spool was explicitly cleaned after parent restoration",
                    assignment_id=assignment_id,
                )
            content = b"".join(_spool_payloads(receipt, runtime))
            if (
                len(content) != result.get("byte_length")
                or hashlib.sha256(content).hexdigest() != result.get("payload_sha256")
            ):
                _migration_error(
                    "spool_reconciliation_failed",
                    "Chunk spool does not match immutable result receipt",
                    assignment_id=assignment_id,
                )
            _write_result_file(destination, content)
            delivery = dict(receipt.get("delivery") or {})
            delivery["status"] = "materialized"
            delivery["materialized_at"] = utc_now()
            receipt["delivery"] = delivery
            _save_v2(assignment_id, receipt, runtime)
            return ResultDescriptor(
                assignment_id=assignment_id,
                mode="chunked",
                path=destination,
                byte_length=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                completion_basis=str(result.get("completion_basis")),
            )
        if mode != "artifact":
            _migration_error("receipt_schema_unsupported", "Completed result has unsupported mode")
        contract = _stored_artifact_contract(receipt)
        artifact = result.get("artifact")
        if not isinstance(artifact, Mapping):
            _migration_error("receipt_schema_unsupported", "Artifact result has no verified object identity")
        request = {
            "commit_sha": artifact.get("commit_sha"),
            "path": contract.path,
            "blob_sha": artifact.get("blob_sha"),
            "byte_length": artifact.get("byte_length"),
            "content_sha256": artifact.get("content_sha256"),
            "result_sha256": result.get("payload_sha256"),
        }

    content = (verifier or GitArtifactVerifier()).fetch_verified_blob(
        contract,
        commit_sha=str(request["commit_sha"]),
        path=str(request["path"]),
        blob_sha=str(request["blob_sha"]),
        byte_length=int(request["byte_length"]),
        content_sha256=str(request["content_sha256"]),
    )
    if hashlib.sha256(content).hexdigest() != request["result_sha256"]:
        raise ArtifactVerificationError(
            "Fetched artifact differs from immutable result receipt",
            details={"assignment_id": assignment_id},
            error_code="artifact_hash_mismatch",
        )
    with state_lock(runtime):
        current = _load_mutable_v2(assignment_id, runtime)
        current_result = current.get("result")
        if (
            current_result.get("status") != "complete"
            or current_result.get("payload_sha256") != request["result_sha256"]
        ):
            _state_error(
                "result_materialization_raced",
                "Result receipt changed while artifact body was fetched",
                assignment_id=assignment_id,
            )
        _write_result_file(destination, content)
        delivery = dict(current.get("delivery") or {})
        delivery["status"] = "materialized"
        delivery["materialized_at"] = utc_now()
        current["delivery"] = delivery
        _save_v2(assignment_id, current, runtime)
    return ResultDescriptor(
        assignment_id=assignment_id,
        mode="artifact",
        path=destination,
        byte_length=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        completion_basis=str(current_result.get("completion_basis")),
    )


def cleanup_result_v2(
    assignment_id: str, *, paths: RuntimePaths | None = None
) -> CleanupResult:
    """Explicitly remove only verified chunk spool files after parent restoration."""

    runtime = paths or default_paths()
    with state_lock(runtime):
        receipt = _load_mutable_v2(assignment_id, runtime)
        result = receipt.get("result")
        delivery = receipt.get("delivery")
        if not isinstance(result, Mapping) or result.get("status") != "complete":
            _state_error("result_not_complete", "Result cleanup requires completion", assignment_id=assignment_id)
        if not isinstance(delivery, Mapping) or delivery.get("parent_restoration_status") != "restored":
            _state_error(
                "parent_not_restored",
                "Result cleanup requires exact parent restoration",
                assignment_id=assignment_id,
            )
        if receipt.get("effective_result_mode") != "chunked":
            return CleanupResult(
                assignment_id=assignment_id, removed_spool_files=0, result_retained=True
            )
        if _spool_cleanup_recorded(receipt):
            return CleanupResult(
                assignment_id=assignment_id, removed_spool_files=0, result_retained=True
            )
        _assert_no_orphan_spool_files(receipt, runtime)
        spool_root = _existing_spool_root(runtime, assignment_id)
        if spool_root is None:
            _migration_error(
                "spool_reconciliation_failed",
                "Private spool directory is missing before cleanup",
                assignment_id=assignment_id,
            )
        removed = 0
        for turn in _accepted_chunk_turns(receipt):
            chunk = turn["chunk"]
            path = spool_root / _spool_filename(int(chunk["index"]))
            body = _safe_existing_file(path)
            if body is None or hashlib.sha256(body).hexdigest() != chunk.get("payload_sha256"):
                _migration_error(
                    "spool_reconciliation_failed",
                    "Verified spool file changed before cleanup",
                    assignment_id=assignment_id,
                    turn_id=turn.get("turn_id"),
                )
            path.unlink()
            removed += 1
        with contextlib.suppress(OSError):
            spool_root.rmdir()
        delivery = dict(delivery)
        delivery["spool_cleanup_at"] = utc_now()
        delivery["spool_cleanup_count"] = removed
        receipt["delivery"] = delivery
        _save_v2(assignment_id, receipt, runtime)
        return CleanupResult(
            assignment_id=assignment_id, removed_spool_files=removed, result_retained=True
        )


def purge_local_state_v2(
    *, force: bool = False, paths: RuntimePaths | None = None
) -> dict[str, Any]:
    """Remove only enumerated private helper state, including verified spools.

    This is deliberately not a recursive delete.  It traverses at most the
    helper-owned assignment and spool directories, rejects links/unrecognized
    entries, and unlinks individual regular files.  Without ``force`` an
    unresolved dispatch remains an absolute blocker; with it the caller has
    explicitly chosen to discard the local recovery record.
    """

    runtime = paths or default_paths()
    with state_lock(runtime):
        if not force:
            current = active_assignment_v2(runtime)
            if current is not None:
                raise BusyError(
                    "Cannot purge local state while a dispatch is unresolved",
                    details={
                        "assignment_id": current.get("assignment_id"),
                        "status": current.get("status"),
                    },
                )

        worker_removed = False
        assignments_removed = 0
        spool_files_removed = 0

        with contextlib.suppress(FileNotFoundError):
            worker_info = runtime.worker_file.lstat()
            if stat.S_ISLNK(worker_info.st_mode) or not stat.S_ISREG(worker_info.st_mode):
                _state_error("private_path_invalid", "Worker config is not a regular private file")
            runtime.worker_file.unlink()
            worker_removed = True

        if runtime.assignments_dir.exists():
            directory_info = runtime.assignments_dir.lstat()
            if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
                _state_error("private_path_invalid", "Assignments path is not a real directory")
            assignment_files = sorted(runtime.assignments_dir.iterdir(), key=lambda path: path.name)
            for path in assignment_files:
                if not path.name.endswith(".json"):
                    _state_error(
                        "purge_path_unrecognized",
                        "Assignments directory contains an unrecognized entry",
                        path=str(path),
                    )
                validate_identifier(path.name[:-5], field="assignment_id")
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    _state_error("private_path_invalid", "Assignment receipt is not a regular file", path=str(path))
            for path in assignment_files:
                path.unlink()
                assignments_removed += 1
            with contextlib.suppress(OSError):
                runtime.assignments_dir.rmdir()

        if runtime.spool_dir.exists():
            spool_info = runtime.spool_dir.lstat()
            if stat.S_ISLNK(spool_info.st_mode) or not stat.S_ISDIR(spool_info.st_mode):
                _state_error("private_path_invalid", "Spool path is not a real directory")
            spool_dirs = sorted(runtime.spool_dir.iterdir(), key=lambda path: path.name)
            for root in spool_dirs:
                validate_identifier(root.name, field="assignment_id")
                info = root.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    _state_error("private_path_invalid", "Spool assignment path is not a real directory", path=str(root))
                for part in root.iterdir():
                    part_info = part.lstat()
                    if (
                        not _SPOOL_PART_NAME.fullmatch(part.name)
                        or stat.S_ISLNK(part_info.st_mode)
                        or not stat.S_ISREG(part_info.st_mode)
                    ):
                        _state_error(
                            "purge_path_unrecognized",
                            "Spool directory contains an unrecognized entry",
                            path=str(part),
                        )
            for root in spool_dirs:
                for part in sorted(root.iterdir(), key=lambda path: path.name):
                    part.unlink()
                    spool_files_removed += 1
                with contextlib.suppress(OSError):
                    root.rmdir()
            with contextlib.suppress(OSError):
                runtime.spool_dir.rmdir()

        return {
            "worker_removed": worker_removed,
            "assignments_removed": assignments_removed,
            "spool_files_removed": spool_files_removed,
        }


def complete_assignment_v2(
    assignment_id: str,
    response: str | None = None,
    paths: RuntimePaths | None = None,
    *,
    evidence: NativeCollectionEvidence | None = None,
) -> tuple[dict[str, Any], str]:
    """Deprecated inline alias; response-only completion remains fail closed."""

    if evidence is None:
        raise CollectionEvidenceError(
            "Response-only completion is disabled; native collection evidence is required",
            details={"assignment_id": assignment_id},
            error_code="collection_evidence_required",
        )
    if response is not None and normalize_newlines(response) != evidence.text:
        raise CollectionEvidenceError(
            "Response file does not exactly match native collection evidence",
            details={"assignment_id": assignment_id},
            error_code="collection_evidence_conflict",
        )
    runtime = paths or default_paths()
    receipt = load_assignment_v2(assignment_id, runtime)
    if receipt.get("schema_version") == 1 and receipt.get("status") == "complete":
        # Historical marker-only receipts are readable but not writable.  Their
        # body was never safely retained by this helper.
        return receipt, ""
    if receipt.get("effective_result_mode") != "inline":
        _state_error(
            "complete_alias_mode_invalid",
            "Deprecated complete alias is available only for inline collection",
            assignment_id=assignment_id,
        )
    turn = _resolve_turn(receipt, None)
    collect_turn_v2(
        assignment_id,
        str(turn["turn_id"]),
        evidence,
        paths=runtime,
    )
    updated = load_assignment_v2(assignment_id, runtime)
    _normalized, payload = parse_result(evidence.text, str(turn["turn_id"]))
    return updated, payload


def redact_stored_diagnostics_v2(paths: RuntimePaths | None = None) -> int:
    """Remove legacy raw diagnostics without rewriting immutable v1 completion."""

    runtime = paths or default_paths()
    changed_count = 0
    with state_lock(runtime):
        if not runtime.assignments_dir.exists():
            return 0
        for path in sorted(runtime.assignments_dir.glob("*.json")):
            raw = read_json(path)
            assignment_id = raw.get("assignment_id")
            if not isinstance(assignment_id, str):
                _migration_error("receipt_schema_unsupported", "Receipt has no assignment ID")
            if raw.get("schema_version") == 1 and raw.get("status") == "complete":
                continue
            if raw.get("schema_version") == 1:
                redacted, changed = _redact_diagnostic_fields(raw)
            else:
                redacted, changed = _redact_v2_diagnostic_fields(raw)
            if changed:
                if raw.get("schema_version") != 1:
                    _validate_v2(redacted, assignment_id)
                atomic_write_json(path, redacted)
                changed_count += 1
    return changed_count
