"""Lossless, read-only chunk transport primitives.

Only the payload is model-provided content.  It is encoded as one canonical JSON
object, so strings that happen to contain dispatch/chunk protocol markers remain
data and never get reparsed as framing.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from typing import Any

from .collection import canonical_json_bytes, normalize_newlines, strict_json_object
from .errors import ChunkProtocolError


CHUNK_PROTOCOL_SCHEMA = "codex-pro-dispatch.chunk/v1"
CHUNKED_REQUIRED_CONTROL = "[CODEX_PRO_DISPATCH_CHUNKED_REQUIRED_V1]"
CHUNK_PROTOCOL_PREFIX = "[CODEX_PRO_DISPATCH_CHUNK_V1 "
CHUNK_PROTOCOL_END_PREFIX = "[CODEX_PRO_DISPATCH_CHUNK_END_V1 "
CHAIN_ZERO_HEX = "0" * 64
CHUNK_MESSAGE_MAX_BYTES = 16_000
CHUNK_TARGET_BYTES = 10_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEADER = re.compile(
    r"^\[CODEX_PRO_DISPATCH_CHUNK_V1 "
    r"group_id=(?P<group_id>[A-Za-z0-9][A-Za-z0-9._:-]{0,127}) "
    r"index=(?P<index>[1-9][0-9]*) "
    r"previous_chain_sha256=(?P<previous>[0-9a-f]{64}) "
    r"final=(?P<final>[01]) "
    r"count=(?P<count>[0-9]+)\]$"
)
_FOOTER = re.compile(
    r"^\[CODEX_PRO_DISPATCH_CHUNK_END_V1 "
    r"group_id=(?P<group_id>[A-Za-z0-9][A-Za-z0-9._:-]{0,127}) "
    r"index=(?P<index>[1-9][0-9]*)\]$"
)


@dataclass(frozen=True)
class ChunkEnvelope:
    turn_id: str
    group_id: str
    index: int
    previous_chain_sha256: str
    final: bool
    count: int
    payload: str
    serialized_byte_length: int

    @property
    def payload_bytes(self) -> bytes:
        return self.payload.encode("utf-8")

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.payload_bytes).hexdigest()

    @property
    def chain_sha256(self) -> str:
        return chunk_chain(
            self.previous_chain_sha256, self.index, self.payload_bytes
        )


def _raise(code: str, message: str, **details: Any) -> None:
    raise ChunkProtocolError(message, details=details, error_code=code)


def result_marker(turn_id: str) -> str:
    if not _IDENTIFIER.fullmatch(turn_id):
        _raise("chunk_envelope_incomplete", "Chunk turn ID is invalid")
    return f"[CODEX_PRO_DISPATCH_RESULT assignment_id={turn_id}]"


def chunk_chain(previous_chain_sha256: str, index: int, payload: bytes) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", previous_chain_sha256):
        _raise("chunk_chain_mismatch", "Previous chunk chain digest is invalid")
    if index < 1:
        _raise("chunk_envelope_incomplete", "Chunk index must start at one")
    material = (
        b"codex-pro-dispatch/chunk-v1\0"
        + bytes.fromhex(previous_chain_sha256)
        + struct.pack(">Q", index)
        + struct.pack(">Q", len(payload))
        + payload
    )
    return hashlib.sha256(material).hexdigest()


def canonical_payload_json(payload: str) -> str:
    return canonical_json_bytes({"payload": normalize_newlines(payload)}).decode("utf-8")


def format_chunk_response(
    *,
    turn_id: str,
    group_id: str,
    index: int,
    previous_chain_sha256: str,
    final: bool,
    count: int,
    payload: str,
) -> str:
    """Build the only accepted chunk wire representation (useful for fixtures)."""

    if not _IDENTIFIER.fullmatch(group_id):
        _raise("chunk_envelope_incomplete", "Chunk group ID is invalid")
    if final:
        if count != index:
            _raise("chunk_final_count_mismatch", "Final chunk count must equal its index")
    elif count != 0:
        _raise("chunk_final_count_mismatch", "Nonfinal chunks must use count zero")
    response = "\n".join(
        (
            result_marker(turn_id),
            f"[CODEX_PRO_DISPATCH_CHUNK_V1 group_id={group_id} index={index} "
            f"previous_chain_sha256={previous_chain_sha256} final={1 if final else 0} count={count}]",
            canonical_payload_json(payload),
            f"[CODEX_PRO_DISPATCH_CHUNK_END_V1 group_id={group_id} index={index}]",
        )
    )
    wire_byte_length = len(response.encode("utf-8"))
    if wire_byte_length > CHUNK_MESSAGE_MAX_BYTES:
        _raise(
            "chunk_serialized_limit_exceeded",
            "Serialized chunk message exceeds the complete-message byte limit",
            maximum_bytes=CHUNK_MESSAGE_MAX_BYTES,
        )
    return response


def parse_chunk_response(response: str, *, turn_id: str) -> ChunkEnvelope:
    """Validate framing before yielding the decoded, canonical-LF payload."""

    # Count the complete serialized native assistant message first.  JSON
    # escaping and CRLF bytes are part of the actual wire size; normalization is
    # only for canonical parsing and hashing after this hard bound succeeds.
    wire_byte_length = len(response.encode("utf-8"))
    if wire_byte_length > CHUNK_MESSAGE_MAX_BYTES:
        _raise(
            "chunk_serialized_limit_exceeded",
            "Serialized chunk message exceeds the complete-message byte limit",
            maximum_bytes=CHUNK_MESSAGE_MAX_BYTES,
        )
    normalized = normalize_newlines(response)
    encoded = normalized.encode("utf-8")
    if len(encoded) > CHUNK_MESSAGE_MAX_BYTES:
        _raise(
            "chunk_serialized_limit_exceeded",
            "Serialized chunk message exceeds the complete-message byte limit",
            maximum_bytes=CHUNK_MESSAGE_MAX_BYTES,
        )
    lines = normalized.split("\n")
    if len(lines) != 4:
        _raise("chunk_envelope_incomplete", "Chunk envelope must contain exactly four LF-separated lines")
    if lines[0] != result_marker(turn_id):
        _raise("chunk_envelope_incomplete", "Chunk response does not start with its exact result marker")
    header = _HEADER.fullmatch(lines[1])
    footer = _FOOTER.fullmatch(lines[3])
    if header is None or footer is None:
        _raise("chunk_envelope_incomplete", "Chunk header or footer is invalid")
    group_id = header.group("group_id")
    index = int(header.group("index"))
    previous = header.group("previous")
    final = header.group("final") == "1"
    count = int(header.group("count"))
    if footer.group("group_id") != group_id or int(footer.group("index")) != index:
        _raise("chunk_envelope_incomplete", "Chunk footer does not match its header")
    if final and count != index:
        _raise("chunk_final_count_mismatch", "Final chunk count must equal its index")
    if not final and count != 0:
        _raise("chunk_final_count_mismatch", "Nonfinal chunks must use count zero")
    try:
        parsed = strict_json_object(lines[2].encode("utf-8"), maximum_bytes=CHUNK_MESSAGE_MAX_BYTES)
    except Exception as exc:
        if isinstance(exc, ChunkProtocolError):
            raise
        _raise("chunk_envelope_incomplete", "Chunk JSON payload is invalid")
        raise AssertionError("unreachable") from exc
    if set(parsed) != {"payload"} or not isinstance(parsed.get("payload"), str):
        _raise("chunk_envelope_incomplete", "Chunk JSON must contain only a string payload")
    payload = normalize_newlines(str(parsed["payload"]))
    # A canonical body avoids multiple byte representations for the same payload;
    # importantly, marker-looking payload text is escaped JSON data here.
    if lines[2] != canonical_payload_json(payload):
        _raise("chunk_envelope_incomplete", "Chunk payload JSON is not canonical")
    if not final and not payload:
        _raise("chunk_envelope_incomplete", "Nonfinal chunk payload may not be empty")
    return ChunkEnvelope(
        turn_id=turn_id,
        group_id=group_id,
        index=index,
        previous_chain_sha256=previous,
        final=final,
        count=count,
        payload=payload,
        # Report the received serialized message, rather than its canonical
        # LF-normalized representation.  This is the byte count the hard
        # complete-message limit actually evaluated.
        serialized_byte_length=wire_byte_length,
    )


def is_chunked_required_control(response: str, *, turn_id: str) -> bool:
    """Only the exact two-line control response may trigger inline escalation."""

    return normalize_newlines(response) == result_marker(turn_id) + "\n" + CHUNKED_REQUIRED_CONTROL


def continuation_prompt(
    *,
    assignment_id: str,
    turn_id: str,
    next_index: int,
    previous_chain_sha256: str,
    retransmission: bool,
) -> str:
    """Generate a child-only prompt; it never repeats the original assignment."""

    action = "Re-emit" if retransmission else "Continue with"
    return (
        f"[CODEX_PRO_DISPATCH_CONTINUATION group_id={assignment_id} turn_id={turn_id}]\n\n"
        f"{action} chunk {next_index} of the existing deliverable. Do not repeat accepted "
        "content or the original assignment. Return exactly four LF-separated lines:\n"
        f"{result_marker(turn_id)}\n"
        f"[CODEX_PRO_DISPATCH_CHUNK_V1 group_id={assignment_id} index={next_index} "
        f"previous_chain_sha256={previous_chain_sha256} final=<0-or-1> count=<0-or-{next_index}>]\n"
        '{"payload":"JSON-escaped Markdown only"}\n'
        f"[CODEX_PRO_DISPATCH_CHUNK_END_V1 group_id={assignment_id} index={next_index}]\n\n"
        "The body must be one canonical JSON object with only a payload string. Keep the entire "
        f"serialized assistant message at or below {CHUNK_MESSAGE_MAX_BYTES} UTF-8 bytes. "
        "Protocol-looking Markdown belongs inside payload as JSON data."
    )
