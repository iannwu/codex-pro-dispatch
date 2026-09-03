"""Strict native collection evidence parsing and validation.

The desktop host is a trust boundary.  A caller cannot turn a missing truncation
field into ``False`` with a command-line flag: that normalization is owned by a
version-scoped, helper-allowlisted adapter contract.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import CollectionEvidenceError


NATIVE_COLLECTION_SCHEMA = "codex-pro-dispatch.native-collection/v1"
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_ID_BYTES = 256


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_object(raw: bytes, *, maximum_bytes: int = MAX_EVIDENCE_BYTES) -> dict[str, Any]:
    """Decode one UTF-8 JSON object with duplicate keys and trailing data rejected."""

    if not raw:
        raise CollectionEvidenceError(
            "Native collection evidence is empty", error_code="collection_metadata_missing"
        )
    if len(raw) > maximum_bytes:
        raise CollectionEvidenceError(
            "Native collection evidence exceeds the configured size limit",
            details={"maximum_bytes": maximum_bytes},
            error_code="collection_evidence_invalid",
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise CollectionEvidenceError(
            "Native collection evidence must not contain a UTF-8 BOM",
            error_code="collection_evidence_invalid",
        )
    try:
        text = raw.decode("utf-8", "strict")
        decoder = json.JSONDecoder(
            object_pairs_hook=_reject_duplicate_object, parse_constant=_reject_constant
        )
        value, end = decoder.raw_decode(text)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise CollectionEvidenceError(
            "Native collection evidence is not strict JSON",
            error_code="collection_evidence_invalid",
        ) from exc
    if text[end:].strip():
        raise CollectionEvidenceError(
            "Native collection evidence contains trailing data",
            error_code="collection_evidence_invalid",
        )
    if not isinstance(value, dict):
        raise CollectionEvidenceError(
            "Native collection evidence must be a JSON object",
            error_code="collection_evidence_invalid",
        )
    return value


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the receipt's canonical JSON representation without a newline."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _safe_native_id(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise CollectionEvidenceError(
            f"{field} must be a string", error_code="collection_evidence_invalid"
        )
    encoded = value.encode("utf-8")
    if not value or len(encoded) > MAX_MESSAGE_ID_BYTES or any(ord(char) < 32 for char in value):
        raise CollectionEvidenceError(
            f"{field} is not a safe stable native ID",
            error_code="collection_evidence_invalid",
        )
    return value


def _utc(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise CollectionEvidenceError(
            f"{field} must be a UTC string", error_code="collection_evidence_invalid"
        )
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectionEvidenceError(
            f"{field} is not a valid UTC timestamp", error_code="collection_evidence_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise CollectionEvidenceError(
            f"{field} must include a timezone", error_code="collection_evidence_invalid"
        )
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class NativeAdapterContract:
    """A helper-owned, version-specific statement about a native host adapter.

    Contracts are intentionally not loaded from an evidence file or CLI option.  A
    future host may be added only by a reviewed helper release after an inspected
    deployment or authoritative contract establishes its omission behavior.
    """

    adapter_contract_id: str
    host: str
    host_contract_version: str
    omitted_message_truncated_is_false: bool
    omitted_outer_truncated_is_false: bool
    supports_complete_reread_upgrade: bool
    generation_finality_provenance: frozenset[str]


# The currently shipped adapter is deliberately conservative: it requires both
# flags.  The code supports future allowlisted normalization but does not infer it
# from examples, model prose, a host title, or an optional caller boolean.
NATIVE_ADAPTER_CONTRACTS: dict[str, NativeAdapterContract] = {
    "codex-desktop-native-collection/v1": NativeAdapterContract(
        adapter_contract_id="codex-desktop-native-collection/v1",
        host="codex-desktop",
        host_contract_version="native-collection-v1",
        omitted_message_truncated_is_false=False,
        omitted_outer_truncated_is_false=False,
        supports_complete_reread_upgrade=True,
        generation_finality_provenance=frozenset({"native-message-status"}),
    ),
}


def adapter_contract(adapter_contract_id: str) -> NativeAdapterContract:
    contract = NATIVE_ADAPTER_CONTRACTS.get(adapter_contract_id)
    if contract is None:
        raise CollectionEvidenceError(
            "Native collection adapter is not allowlisted by this helper",
            details={"adapter_contract_id": adapter_contract_id},
            error_code="collection_adapter_unsupported",
        )
    return contract


def _raw_truncation(
    value: Mapping[str, Any],
    *,
    key: str,
    can_normalize_omission: bool,
) -> tuple[str, bool | None]:
    if key not in value:
        return "omitted", False if can_normalize_omission else None
    raw = value[key]
    if type(raw) is not bool:
        raise CollectionEvidenceError(
            f"{key} must be a JSON boolean when present",
            error_code="collection_evidence_invalid",
        )
    return ("true" if raw else "false"), raw


@dataclass(frozen=True)
class NativeCollectionEvidence:
    """Validated evidence for one selected assistant message.

    ``content_identity_sha256`` deliberately excludes ``observed_at``.  A host can
    therefore reread the same stable native message at a later time without turning
    an idempotent observation into an immutable-content conflict.
    """

    schema: str
    adapter_contract_id: str
    requested_conversation_id: str
    loaded_conversation_id: str
    assistant_message_id: str
    submitted_user_message_id: str
    role: str
    generation_status: str
    generation_finality_provenance: str
    raw_truncated: str
    normalized_truncated: bool | None
    raw_outer_truncated: str
    normalized_outer_truncated: bool | None
    outer_integrity_provenance: str
    text: str
    observed_at: str
    evidence_sha256: str
    content_identity_sha256: str

    @property
    def complete_and_untruncated(self) -> bool:
        return (
            self.generation_status == "completed"
            and self.normalized_truncated is False
            and self.normalized_outer_truncated is False
        )

    @property
    def has_known_truncation(self) -> bool:
        return self.normalized_truncated is not None and self.normalized_outer_truncated is not None

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> "NativeCollectionEvidence":
        value = strict_json_object(raw)
        return cls.from_mapping(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NativeCollectionEvidence":
        # `truncated` and the nested outer `truncated` are intentionally optional
        # syntactically: an allowlisted future contract may normalize omission.  All
        # other fields are exact and mandatory.
        required = {
            "schema",
            "adapter_contract_id",
            "requested_conversation_id",
            "loaded_conversation_id",
            "assistant_message_id",
            "submitted_user_message_id",
            "role",
            "generation_status",
            "generation_finality_provenance",
            "selected_result_outer_integrity",
            "text",
            "observed_at",
        }
        allowed = required | {"truncated"}
        actual = set(value)
        if actual != required and actual != allowed:
            raise CollectionEvidenceError(
                "Native collection evidence has an invalid key set",
                details={"missing_keys": sorted(required - actual), "unknown_keys": sorted(actual - allowed)},
                error_code="collection_evidence_invalid",
            )
        schema = value.get("schema")
        if schema != NATIVE_COLLECTION_SCHEMA:
            raise CollectionEvidenceError(
                "Native collection evidence uses an unsupported schema",
                error_code="collection_evidence_invalid",
            )
        adapter_id = _safe_native_id(value.get("adapter_contract_id"), field="adapter_contract_id")
        contract = adapter_contract(adapter_id)
        requested = _safe_native_id(
            value.get("requested_conversation_id"), field="requested_conversation_id"
        )
        loaded = _safe_native_id(value.get("loaded_conversation_id"), field="loaded_conversation_id")
        assistant_id = _safe_native_id(value.get("assistant_message_id"), field="assistant_message_id")
        submitted_id = _safe_native_id(
            value.get("submitted_user_message_id"), field="submitted_user_message_id"
        )
        if value.get("role") != "assistant":
            raise CollectionEvidenceError(
                "Selected native result is not an assistant message",
                error_code="collection_evidence_invalid",
            )
        if value.get("generation_status") != "completed":
            raise CollectionEvidenceError(
                "Selected native result is not generation-final",
                error_code="collection_message_not_complete",
            )
        finality = value.get("generation_finality_provenance")
        if not isinstance(finality, str) or finality not in contract.generation_finality_provenance:
            raise CollectionEvidenceError(
                "Generation finality lacks trusted adapter provenance",
                error_code="collection_message_not_complete",
            )
        outer = value.get("selected_result_outer_integrity")
        if not isinstance(outer, Mapping) or set(outer) not in ({"provenance"}, {"provenance", "truncated"}):
            raise CollectionEvidenceError(
                "Selected result outer integrity has an invalid key set",
                error_code="collection_evidence_invalid",
            )
        outer_provenance = outer.get("provenance")
        if not isinstance(outer_provenance, str) or not outer_provenance or any(
            ord(char) < 32 for char in outer_provenance
        ):
            raise CollectionEvidenceError(
                "Selected result outer integrity provenance is invalid",
                error_code="collection_evidence_invalid",
            )
        raw_truncated, normalized_truncated = _raw_truncation(
            value,
            key="truncated",
            can_normalize_omission=contract.omitted_message_truncated_is_false,
        )
        raw_outer, normalized_outer = _raw_truncation(
            outer,
            key="truncated",
            can_normalize_omission=contract.omitted_outer_truncated_is_false,
        )
        text = value.get("text")
        if not isinstance(text, str):
            raise CollectionEvidenceError(
                "Native collection text must be a string",
                error_code="collection_evidence_invalid",
            )
        normalized_text = normalize_newlines(text)
        observed_at = _utc(value.get("observed_at"), field="observed_at")
        canonical = canonical_json_bytes(value)
        identity = {
            "schema": schema,
            "adapter_contract_id": adapter_id,
            "requested_conversation_id": requested,
            "loaded_conversation_id": loaded,
            "assistant_message_id": assistant_id,
            "submitted_user_message_id": submitted_id,
            "role": "assistant",
            "generation_status": "completed",
            "generation_finality_provenance": finality,
            "raw_truncated": raw_truncated,
            "normalized_truncated": normalized_truncated,
            "raw_outer_truncated": raw_outer,
            "normalized_outer_truncated": normalized_outer,
            "outer_integrity_provenance": outer_provenance,
            "text_sha256": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
        }
        return cls(
            schema=schema,
            adapter_contract_id=adapter_id,
            requested_conversation_id=requested,
            loaded_conversation_id=loaded,
            assistant_message_id=assistant_id,
            submitted_user_message_id=submitted_id,
            role="assistant",
            generation_status="completed",
            generation_finality_provenance=finality,
            raw_truncated=raw_truncated,
            normalized_truncated=normalized_truncated,
            raw_outer_truncated=raw_outer,
            normalized_outer_truncated=normalized_outer,
            outer_integrity_provenance=outer_provenance,
            text=normalized_text,
            observed_at=observed_at,
            evidence_sha256=hashlib.sha256(canonical).hexdigest(),
            content_identity_sha256=hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
        )

    def receipt_fields(self) -> dict[str, Any]:
        """Return body-free, immutable-relevant collection receipt metadata."""

        return {
            "collection_schema": self.schema,
            "adapter_contract_id": self.adapter_contract_id,
            "assistant_message_id": self.assistant_message_id,
            "submitted_user_message_id": self.submitted_user_message_id,
            "collection_evidence_sha256": self.evidence_sha256,
            "collection_content_identity_sha256": self.content_identity_sha256,
            "collection_observed_at": self.observed_at,
            "collection_status": self.generation_status,
            "generation_finality_provenance": self.generation_finality_provenance,
            "raw_truncated": self.raw_truncated,
            "normalized_truncated": self.normalized_truncated,
            "raw_outer_truncated": self.raw_outer_truncated,
            "normalized_outer_truncated": self.normalized_outer_truncated,
            "outer_integrity_provenance": self.outer_integrity_provenance,
            "response_byte_length": len(self.text.encode("utf-8")),
            "response_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
        }
