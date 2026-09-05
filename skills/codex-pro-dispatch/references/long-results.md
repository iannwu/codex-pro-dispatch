# Long-result transport reference (v1.2.0)

Read this reference before choosing `chunked` or `artifact` result mode. It is
an operator contract, not a model suggestion: malformed protocol data fails
closed and never authorizes a resend or a new Git write.

## Result-mode decision

Choose one mode in `pro-dispatch prepare`:

| Mode | Use for | Authority | Completion evidence |
| --- | --- | --- | --- |
| `inline` | A likely short result | Read-only | One untruncated native evidence envelope |
| `chunked` | A long, read-only result | Read-only, one send per child turn | Evidence plus every strict chunk and chain |
| `artifact` | One durable Markdown deliverable | Explicit, assignment-scoped Git write | Exact remote commit/tree/blob verification |

`auto` is intentionally unavailable. A control response or observed truncation
can only transition an inline/chunked logical dispatch into a separately armed
chunk child. It never changes the assignment to artifact mode.

## Collection evidence

All inline and chunk results require one strict JSON object from one native read:

```json
{
  "schema": "codex-pro-dispatch.native-collection/v1",
  "adapter_contract_id": "codex-desktop-native-collection/v1",
  "requested_conversation_id": "worker-stable-id",
  "loaded_conversation_id": "worker-stable-id",
  "assistant_message_id": "assistant-stable-id",
  "submitted_user_message_id": "native-user-stable-id",
  "role": "assistant",
  "generation_status": "completed",
  "generation_finality_provenance": "native-message-status",
  "truncated": false,
  "selected_result_outer_integrity": {
    "truncated": false,
    "provenance": "native-result-envelope"
  },
  "text": "exact assistant text",
  "observed_at": "2030-01-01T00:00:00.000Z"
}
```

The evidence must bind both requested and loaded worker IDs, the selected
assistant message, and the exact submitted native user message. The host must
state finality for the selected item with an adapter-allowed provenance; a turn
being complete is not item-level evidence.

`truncated` and the nested outer flag may syntactically be omitted only because a
future helper-owned adapter contract might authorize normalization. The current
adapter does not. Raw values are persisted as `true`, `false`, or `omitted`;
normalized values are `true`, `false`, or `null`. No CLI flag or caller boolean
may choose them. `observed_at` affects evidence audit hash, not immutable content
identity, so an identical reread at a later time is idempotent. Once a source is
accepted, changed source identity or normalized content is a conflict.

## Inline escalation control

The entire normalized assistant response must be exactly these two lines for an
inline result to require chunked recovery:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<turn-id>]
[CODEX_PRO_DISPATCH_CHUNKED_REQUIRED_V1]
```

No leading/trailing whitespace, extra line, or embedded control is valid. After a
verified outbound message and proven complete generation, the helper atomically
closes that predecessor as `response_rejected` and prepares one successor. The
skill must arm and send the successor; uncertain delivery never creates one.

## Chunk envelope and reassembly

Each assistant chunk is exactly four LF-separated lines:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<turn-id>]
[CODEX_PRO_DISPATCH_CHUNK_V1 group_id=<assignment-id> index=<n> previous_chain_sha256=<64-lowercase-hex> final=<0-or-1> count=<0-or-n>]
{"payload":"one canonical JSON string containing arbitrary Markdown"}
[CODEX_PRO_DISPATCH_CHUNK_END_V1 group_id=<assignment-id> index=<n>]
```

Rules:

- The body is one strict, canonical JSON object with only a string `payload`.
  All Markdown, including marker-looking text, is JSON data.
- The complete serialized assistant message, including JSON escaping and native
  line endings, must be no more than 16,000 UTF-8 bytes.
- Parse then LF-normalize the decoded payload. Hash those decoded UTF-8 bytes.
  Never hash JSON source text.
- Chunk 1 uses 64 zeroes as its preceding chain. The chain is SHA-256 of the
  protocol domain separator, preceding chain bytes, big-endian index/length, and
  decoded payload bytes.
- Nonfinal chunks use `final=0 count=0`; a final chunk uses `final=1 count=n`.
  Nonfinal payloads cannot be empty.
- Reassembly is exact byte concatenation of decoded payloads. The helper inserts
  **no** newline or other separator.
- Every chunk is associated with a separately verified native user message and
  a separately untruncated evidence envelope. It is spooled privately before its
  receipt records it, using an exact hash/length journal to recover a crash.

When a nonfinal chunk is accepted, `collect` returns a distinct `next_turn`.
Use its `turn_id` for `arm`, native send, `submitted`, and the next `collect`.
Never repeat the original assignment in a continuation. A rejected/truncated
chunk can request retransmission from the last verified boundary only; its visible
prefix is not data.

## Artifact contract

Artifact mode requires a strict UTF-8 JSON contract supplied by the parent before
the worker has write authority. Its key set is fixed:

```json
{
  "schema": "codex-pro-dispatch.artifact-contract/v1",
  "repository_id": 123,
  "repository": "owner/repository",
  "visibility": "private",
  "remote_url": "https://github.com/owner/repository.git",
  "base_branch": "main",
  "base_sha": "<40-lowercase-hex>",
  "branch": "codex-pro-dispatch/artifact-7319",
  "path": "artifacts/result.md",
  "commit_message": "docs: add requested artifact",
  "encoding": "utf-8",
  "media_type": "text/markdown",
  "allowed_change": "add-single-markdown",
  "artifact_max_bytes": 2097152,
  "sensitivity": "internal",
  "protected_refs": [
    {"ref":"refs/heads/main","sha":"<40-lowercase-hex>","allow_fast_forward":true}
  ],
  "prepared_at": "2030-01-01T00:00:00Z"
}
```

Before preparation, the parent verifies the canonical credential-free GitHub URL,
repository identity/visibility, current base and every protected ref, that the
target branch is absent, and that the requested Markdown path is absent at the
base. A public contract requires `--allow-public-artifact`; sensitivity `secret`,
`personal`, and `regulated` is rejected for public retention. The contract never
authorizes a PR, merge, tag, release, deployment, issue, setting, workflow, or
second write.

The worker creates exactly one branch commit with exactly one parent equal to the
prepared base and exactly one added regular `100644` UTF-8 Markdown blob. It must
not alter existing paths, create executable/symlink/submodule objects, or make a
merge commit. Its short chat manifest is only a hint; completion waits for parent
verification.

## Artifact manifest and parent verification

The readable manifest, if available, is a bounded canonical line protocol:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<assignment-id>]
[CODEX_PRO_DISPATCH_ARTIFACT_V1]
schema=codex-pro-dispatch.artifact-manifest/v1
assignment_id=<assignment-id>
repository_id=<decimal>
repository=<owner/repository>
remote_url=https://github.com/<owner/repository>.git
base_branch=<base-branch>
base_sha=<prepared-base-sha>
branch=<authorized-branch>
commit_sha=<artifact-commit-sha>
path=<authorized-markdown-path>
byte_length=<decimal>
content_sha256=<64-lowercase-hex>
encoding=utf-8
media_type=text/markdown
changed_path_count=1
commit_message=<authorized-message>
[CODEX_PRO_DISPATCH_ARTIFACT_END_V1]
```

The parent runs `pro-dispatch artifact verify` only after the artifact receipt is
ready. The verifier uses a private `0700` temporary bare repository; it does not
checkout a worktree or accept a local copy. It fetches exact object IDs and proves:

1. the artifact branch is still the exact reported commit;
2. the commit has one parent, exactly the prepared base, and the exact message;
3. the diff adds only the contract path and the base did not already contain it;
4. the tree entry is exactly regular `100644`, and its strict UTF-8 bytes have no
   BOM, CR, NUL, or missing final LF;
5. exact blob length and SHA-256 match the contract/manifest;
6. protected refs did not move except permitted base fast-forward; a base rewrite,
   artifact merge, branch movement, or protected-ref change fails closed.

An unchanged/fast-forwarded base is recorded separately from immutable artifact
identity. `--discover` is the only pre-authorized exception to readable chat
evidence: it verifies the same exact branch/path against the stored artifact
contract when no manifest can be safely collected. It has no implicit write.

## Delivery and cleanup

Result completion does not mean delivery or parent restoration. Materialization
copies only a verified inline/chunk/artifact result into an exclusive private file.
Record native parent restoration separately before chunk spool cleanup. Navigation
retries are read-only and cannot reopen completion or authorize a send. Artifact
branch retention and any deletion are deliberately outside this helper.
