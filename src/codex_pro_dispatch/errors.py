"""Stable, body-safe errors exposed by Codex Pro Dispatch.

Error details intentionally contain identifiers, categories, and hashes only.  Native
responses, prompts, Git stderr, and result bodies must never be copied into an error
or durable receipt.
"""

from __future__ import annotations

from typing import Any, Mapping


class DispatchError(RuntimeError):
    """Expected, user-facing error with a stable machine-readable code."""

    exit_code = 2
    error_code = "dispatch_error"

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})
        if error_code is not None:
            self.error_code = error_code


class ConfigurationError(DispatchError):
    error_code = "configuration_invalid"


class BusyError(DispatchError):
    exit_code = 3
    error_code = "dispatch_busy"


class StateError(DispatchError):
    exit_code = 4
    error_code = "state_transition_invalid"


class MarkerError(DispatchError):
    exit_code = 5
    error_code = "result_marker_invalid"


class CooldownError(DispatchError):
    exit_code = 6
    error_code = "native_cooldown_active"


class CollectionEvidenceError(DispatchError):
    exit_code = 7
    error_code = "collection_evidence_invalid"


class TruncationError(CollectionEvidenceError):
    exit_code = 8
    error_code = "collection_truncated"


class ArtifactProtocolError(DispatchError):
    exit_code = 9
    error_code = "artifact_contract_invalid"


class ArtifactVerificationError(DispatchError):
    exit_code = 10
    error_code = "artifact_verification_failed"


class ChunkProtocolError(DispatchError):
    exit_code = 11
    error_code = "chunk_envelope_invalid"


class ReceiptMigrationError(DispatchError):
    exit_code = 12
    error_code = "receipt_migration_failed"
