# Codex Pro Dispatch long-result transport specification v1.2.0

## Status and scope

- Repository reviewed: `iannwu/codex-pro-dispatch`
- Immutable review base: `adbdc8534b688adc4cda83d9711b1ce37af688e1`
- Review target: remote `main` at that SHA, not the detached v0.1 proof-of-concept checkout
- Deliverable: implementation-ready specification only
- Proposed public result modes: `inline`, `artifact`, and `chunked`
- Recommended release sequence: urgent fail-closed `v1.1.1`, followed by feature release `v1.2.0`

This specification addresses one integrity defect. The native task reader can return only a prefix of an assistant message, cap that result near 20,000 characters, and report `truncated: true`. The current helper receives response text but no trusted collection metadata. Since the required result marker is at the beginning, a truncated prefix can pass marker validation and be recorded as complete while silently losing the remainder.

The design preserves the existing at-most-one native send attempt per message. It never resends an armed message. Chunking is modeled honestly as a sequence of new, individually armed continuation messages under one logical dispatch. Artifact mode is separately authorized GitHub work, never an automatic fallback for a read-only assignment.

## 1. Executive decision

### 1.1 Chosen design

Implement three explicit result modes.

1. `inline`: default and backward-compatible for ordinary responses. The wrapped prompt states a conservative 12,000 UTF-8 byte soft budget. Completion requires one structured native collection-evidence object with explicit `truncated: false`.
2. `artifact`: explicit opt-in per assignment. ChatGPT Pro writes exactly one new UTF-8 Markdown file in exactly one commit on a pre-authorized disposable branch. Chat contains only a short canonical manifest. The parent verifies the exact remote Git commit, tree, and blob before completion.
3. `chunked`: fully read-only. ChatGPT Pro emits ordered Markdown segments in separate assistant messages. Each continuation request is a distinct child turn with its own durable arm, exact outbound read-back, result marker, and native `truncated: false` evidence. The helper reassembles verified chunks from a private crash-recovery spool.

Do not ship `auto` in v1.2.0. Automatic selection cannot safely invent Git write authorization, retention consent, or public-repository disclosure consent. Output length is also unknown before generation. Allow only two narrow escalations:

- An inline worker may return a short canonical `chunked-required` control response.
- A completed native collection with `truncated: true` may prepare a chunked result-retransmission child turn.

Preparation of a recovery child can be automatic, but the child must still be armed and sent once through the normal protocol. Neither escalation authorizes artifact writes.

### 1.2 Why this is the smallest safe design

A footer marker is insufficient because truncation can occur before the footer. Asking the model to stay short is insufficient because length cannot be predicted or enforced exactly. A caller-supplied optional boolean is insufficient because omission, a stale default, or wrapper drift can recreate the defect. Hashing the collected prefix proves only that prefix.

The completion invariant is:

> A new or migrated unresolved receipt may become complete only when the helper receives a required structured native evidence object proving that the selected assistant message is complete and not truncated, or when the separately verified artifact or chunk protocol proves the full result.

The helper cannot cryptographically prove that a JSON object came from the desktop host. The existing design already trusts the current Codex parent to pass exact native user-message read-back bytes. v1.2 extends that boundary to the full collection-evidence object and deliberately exposes no standalone truncation flag. A future signed host-evidence API can replace the adapter without changing receipt semantics.

## 2. Verified current implementation and defect

The following observations are from the pinned base.

### 2.1 Current state and marker model

- `SCHEMA_VERSION` is shared by worker configuration and assignment receipts. Active receipt states are `prepared`, `armed`, `submitted`, `pending`, `indeterminate`, and `ambiguous`; terminal states are `complete`, `abandoned`, and `failed` (`src/codex_pro_dispatch/core.py:18-27`).
- `wrap_prompt()` requires the result marker as the first final-response line. `parse_result()` validates that leading marker and rejects a different assignment marker later in the collected text, but receives no completion or truncation metadata (`src/codex_pro_dispatch/core.py:240-289`).
- `prepare_assignment()` permits one unresolved assignment and requires `continuation_of` to name a completed assignment in the same worker (`src/codex_pro_dispatch/core.py:526-606`).
- `arm_assignment()` durably sets `no_resend` before transport. `mark_submitted()` verifies exact native read-back and preserves collect-only recovery after ambiguity (`src/codex_pro_dispatch/core.py:622-748`).
- `complete_assignment()` accepts only assignment ID plus response text. Once outbound verification passes, it writes `status=complete`, response and payload hashes, and `result_marker_validated=true`. It has no field that can prove the native reader returned the entire assistant message (`src/codex_pro_dispatch/core.py:888-946`).
- `recovery_info()` exposes worker, parent, send hashes, no-resend state, continuation, diagnostics, and cooldown, but no assistant message ID, collection source, message status, or truncation evidence (`src/codex_pro_dispatch/core.py:947-989`).

### 2.2 Current CLI and native protocol

- The public `complete` command accepts `--response-file` and has no mandatory native metadata input (`src/codex_pro_dispatch/cli.py:145-154`).
- CLI dispatch reads that file, calls `complete_assignment()`, and returns the parsed payload (`src/codex_pro_dispatch/cli.py:273-279`).
- The skill preflight correctly asks for a completed-response read plus completion metadata, but the normal collection sequence saves only response text and invokes `pro-dispatch complete --response-file` (`skills/codex-pro-dispatch/SKILL.md:57-77,178-203`).
- The native protocol validates marker placement after reading the newest completed response but does not define a machine-readable metadata handoff to the helper (`skills/codex-pro-dispatch/references/native-protocol.md:48-69`).
- The compatibility contract already names completed-response text and metadata as a required host capability, providing the correct boundary to tighten (`docs/compatibility.md:9-24`).

### 2.3 Existing strengths to preserve

- Arming before transport closes the duplicate-send crash window even though it can produce zero sends (`skills/codex-pro-dispatch/SKILL.md:121-167`).
- Exact outbound read-back mismatch cannot be repaired by resending, while one proven trailing-newline extraction artifact has a bounded correction path (`src/codex_pro_dispatch/core.py:651-748`; `tests/test_core.py:100-220`).
- Late read-back can verify the already-existing user message without another native send (`tests/test_core.py:386-435`).
- Completed receipts are immutable when a later response hash differs (`tests/test_core.py:470-520`).
- The GitHub verification reference correctly treats worker claims as untrusted and requires independent parent verification, but currently specifies only claim-level checks rather than an exact artifact verifier (`skills/codex-pro-dispatch/references/github-verification.md:31-55`).
- Private atomic receipt writes, a global `fcntl` lock, hashed diagnostics, 30-minute HTTP 403 cooldown, exact worker identity, and exact parent restoration remain foundational behavior.

### 2.4 Defect statement

A marker-bearing prefix can currently complete:

1. The worker produces a long response beginning with the correct marker.
2. Native `read_thread` returns only the first 20,000 characters and `truncated: true`.
3. The caller writes the visible prefix to `--response-file` but does not pass the metadata.
4. `parse_result()` accepts the leading marker.
5. `complete_assignment()` records only the prefix hash and marks the receipt complete.
6. The missing suffix is unrecoverable through the receipt because completion is immutable and the body is not retained.

This is a fail-open collection-integrity defect, not an outbound-send defect.

## 3. Trust boundaries and invariants

### 3.1 Trusted components

Trust:

- the configured worker conversation ID;
- the exact current Codex parent task and supported native controls;
- the local user account, private state directory, lock, and helper binary;
- Git object identity and parent-side remote reads for artifact verification;
- explicit per-assignment user authorization.

Do not trust:

- worker prose claims;
- visible conversation titles or ordering;
- an optional caller boolean;
- a checked-out worktree or arbitrary local copy as artifact evidence;
- a branch name without object inspection;
- model-computed byte counts or hashes;
- GitHub connector presence as proof of write permission;
- current `main` remaining fixed after preparation;
- response text alone as proof of complete collection.

### 3.2 Non-negotiable invariants

1. Each armed native user message has at most one send attempt.
2. The original assignment prompt is never sent again after its first arm.
3. A chunk sequence may contain multiple sends, but every send belongs to a unique child turn and is reported honestly.
4. Only one logical dispatch is unresolved at a time.
5. Only one child turn within that dispatch can be unresolved at a time.
6. Missing, unknown, or true truncation evidence never means false.
7. Every accepted inline response and every chunk requires `status=completed` and `truncated=false` for the exact assistant message.
8. Artifact completion requires independent exact-object verification, not a worker manifest alone.
9. Completion is immutable by content identity.
10. Parent restoration runs on every success and failure path before transient cleanup.
11. JSON receipts retain metadata and hashes, not prompt or response bodies.
12. A read-only assignment never becomes a repository write without explicit artifact authorization.

## 4. Native collection evidence

### 4.1 Required host capability

Change the host preflight from a general completed-response read to these semantic capabilities:

- open the exact configured worker ID;
- prove the loaded worker ID;
- identify one stable assistant message ID;
- report role and generation status;
- return the exact collected text;
- explicitly report whether that selected message was truncated;
- report any enclosing collection truncation that can affect the message;
- report an observation timestamp;
- restore the exact parent task.

If the host cannot provide a stable assistant message ID plus explicit truncation metadata for that message, inline and chunked collection are unsupported. Artifact discovery can remain available only when the assignment was prepared in artifact mode and remote verification succeeds.

### 4.2 Canonical evidence envelope

The skill writes one private UTF-8 JSON file from a single native read result. The helper accepts only this schema:

```json
{
  "schema": "codex-pro-dispatch.native-collection/v1",
  "requested_conversation_id": "configured-worker-id",
  "loaded_conversation_id": "configured-worker-id",
  "assistant_message_id": "stable-native-message-id",
  "role": "assistant",
  "status": "completed",
  "truncated": false,
  "collection_truncated": false,
  "text": "exact native result text",
  "observed_at": "2026-09-03T00:00:00.000Z"
}
```

Validation rules:

- exact top-level key set and types;
- exact schema literal;
- requested and loaded conversation IDs equal the receipt worker ID;
- stable message ID is nonempty, bounded, contains no controls, and is stored on first accepted collection;
- role is exactly `assistant`;
- status is exactly `completed`;
- `truncated` is a required JSON boolean and exactly `false`;
- `collection_truncated`, when supported by the host, is required and exactly `false`; adapters without a distinct outer flag must set it equal to the message flag;
- text is a required string and is the only input parsed for markers/envelopes;
- timestamp is valid UTC and not earlier than the turn's verified submission time beyond a small documented clock-skew allowance;
- serialized envelope has a defensive maximum, default 4 MiB;
- unknown keys, missing keys, duplicate JSON keys, invalid UTF-8, NaN-like values, and trailing bytes fail closed.

Do not expose `--truncated`, `--not-truncated`, or a Python `truncated=False` convenience parameter. The whole evidence envelope is mandatory so worker identity, message identity, status, text, and truncation originate from the same native collection operation.

### 4.3 Evidence persistence

Receipts store only:

```text
collection_schema
assistant_message_id
collection_evidence_sha256
collection_observed_at
collection_status
collection_truncated
response_byte_length
response_sha256
payload_sha256
```

They do not store text. The temporary evidence file and inline result file live in the existing private mode-0700 dispatch directory as mode-0600 files and are deleted only after exact parent restoration.

Hash canonical JSON using sorted keys, compact separators, UTF-8, and no trailing newline. Hash response text after the existing LF normalization, but preserve the exact normalized bytes used for parsing and output.

### 4.4 Fail-closed behavior

- Missing evidence: `collection_metadata_missing`.
- Missing truncation field: `collection_truncation_unknown`.
- Either truncation flag true: `collection_truncated`.
- Status not completed: `collection_message_not_complete`.
- Wrong loaded/requested worker: `collection_wrong_worker`.
- Same message ID with different evidence/text hash: `collection_message_conflict`.
- Different message ID for an already bound turn: `collection_duplicate_response`.
- Marker or transport envelope mismatch: mode-specific protocol error.

These errors keep the dispatch recoverable and preserve all no-resend fields. Re-reading the same exact assistant message is allowed. A later native read of the same message ID with `truncated=false` may complete. No error authorizes resending the current turn.

## 5. Public output policies

### 5.1 Names and default

Use the public option:

```text
--result-mode inline|artifact|chunked
```

`inline` is the default. These names are direct, stable, and describe where the complete result lives. Do not rename them to vague terms such as `short`, `file`, or `multi`.

Persist both `requested_result_mode` and `effective_result_mode`. They differ only after an allowed inline-to-chunked escalation.

### 5.2 Inline

Preparation behavior:

- add a prompt instruction to target at most 12,000 UTF-8 bytes, including marker and framing;
- state that 12,000 is a soft budget, not completion evidence;
- permit one canonical `chunked-required` control response if a complete answer will not fit;
- do not permit artifact writes.

Completion behavior:

- require full evidence envelope with both truncation fields false;
- enforce a hard accepted inline text limit of 16,000 UTF-8 bytes to preserve margin under the observed native cap;
- if an untruncated response exceeds 16,000 bytes, return `inline_limit_exceeded` and offer a chunked recovery child;
- copy the payload to a private result file, return byte length/SHA-256, and keep body out of the receipt.

Existing short assignments remain one user message, one assistant message, one marker, and one parent restoration.

### 5.3 Artifact

Artifact mode is available only when all are true:

- the assignment explicitly selects it;
- the user explicitly authorizes one GitHub artifact commit;
- the configured Pro worker exposes write access to the exact repository;
- the starting repository and commit are remote and independently readable;
- the prepared contract passes exact remote checks;
- public repository retention is separately acknowledged when applicable.

A read-only prompt cannot be silently upgraded to artifact mode. Connector denial leaves the same receipt recoverable or requires user-authorized abandonment; it never causes a parent-side substitute commit.

### 5.4 Chunked

Chunked mode requires no external connector or repository write. The original turn requests chunk 1. Every accepted nonfinal chunk causes the helper to prepare exactly one next child turn. The skill must explicitly arm and send that child. Each child retains the established read-back and no-resend behavior.

### 5.5 Auto

Defer `auto`. A future safe auto mode could select only `inline` versus `chunked` based on an explicit worker control response or observed truncation. It must never select artifact mode. Do not expose the name until native acceptance proves deterministic behavior.

## 6. Receipt schema v2 and migration

### 6.1 Split schema constants

Replace the shared constant with:

```python
WORKER_SCHEMA_VERSION = 1
ASSIGNMENT_SCHEMA_VERSION = 2
NATIVE_COLLECTION_SCHEMA = 'codex-pro-dispatch.native-collection/v1'
ARTIFACT_CONTRACT_SCHEMA = 'codex-pro-dispatch.artifact-contract/v1'
ARTIFACT_MANIFEST_SCHEMA = 'codex-pro-dispatch.artifact-manifest/v1'
CHUNK_PROTOCOL_SCHEMA = 'codex-pro-dispatch.chunk/v1'
```

Worker configuration remains schema 1. Assignment receipts become schema 2.

### 6.2 Logical dispatch receipt

A v2 receipt contains one logical dispatch and an ordered `turns` array. Required top-level fields:

```text
schema_version=2
record_type=dispatch
assignment_id
status
requested_result_mode
effective_result_mode
worker_conversation_id
worker_label
worker_model_confirmation
parent_task_id
continuation_of
created_at
updated_at
prompt_sha256
no_original_resend
turns
artifact_contract
result
legacy
```

Each turn contains:

```text
turn_id
sequence
purpose
previous_turn_id
status
wrapped_prompt_sha256
response_marker
submission_count
no_resend
outbound_prompt_verified
sent_prompt_sha256
native_user_message_id
collection
chunk
created_at/updated_at and transition timestamps
```

Bodies never enter JSON.

Top-level statuses:

```text
prepared, active, recoverable, verifying, complete, abandoned, failed
```

Per-turn statuses preserve established send semantics:

```text
prepared, armed, submitted, pending, indeterminate, ambiguous, complete, failed
```

One dispatch may contain many completed turns, but no more than one turn may be unresolved. `continuation_of` remains a relationship between logical dispatches and still requires the prior dispatch to be complete. Chunk children use `previous_turn_id`; they do not overload `continuation_of`.

### 6.3 Migration

Migration runs under the existing global `fcntl` lock and uses atomic replacement.

- Completed v1 receipts remain terminal and byte-for-byte immutable. Normalize status output with `legacy.completion_basis=marker-only` and `legacy.collection_integrity=unverifiable`. Do not claim historical completeness.
- An unresolved v1 receipt migrates before its next state-changing command:
  - `requested_result_mode=inline` and `effective_result_mode=inline`;
  - one synthetic initial turn using the existing assignment ID;
  - preserve status, send counts, hashes, cooldown, timestamps, no-resend, worker, parent, and continuation identity;
  - set collection status to `not_started`;
  - set `legacy.origin_schema=1` and `legacy.collection_evidence_required=true`.
- A v1.1.1 receipt with additive collection fields maps those fields into the v2 turn.
- An unresolved legacy receipt cannot use response-only completion. It must provide new native evidence or enter chunked recovery.
- Corrupt or unsupported receipts fail closed.
- `doctor` reports counts for `legacy_complete_unverifiable`, `legacy_unresolved_migrated`, and migration failures.
- Never reopen or rewrite a completed v1 receipt.

### 6.4 Locking, concurrency, and network work

Keep the existing single lock file and atomic JSON writes. Do not hold the lock across Git network operations.

Artifact verification is two phase:

1. Under lock, validate state and record `status=verifying`, a random verification nonce, manifest hash, and expected contract hash.
2. Release lock, inspect remote Git objects, and recheck branch/ref state.
3. Reacquire lock and require the same nonce, hashes, state, and contract before committing verification metadata.

Concurrent verification with another nonce returns `verification_in_progress`. A crashed verifier is recoverable after a bounded stale-verification timeout and can resume without sending a message.

All result-file writes use a private temporary sibling, mode 0600, `fsync`, atomic rename, directory `fsync`, `O_NOFOLLOW` where supported, and refusal to overwrite an existing output path.

## 7. State machines

### 7.1 Inline

| State | Event | Next state | Send permission |
| --- | --- | --- | --- |
| `prepared` | arm initial turn | `active`, turn `armed` | One attempt for this turn |
| `active` | exact outbound read-back | turn `submitted` | No second attempt |
| `active` | still generating | turn `pending` | None |
| `active` | valid untruncated inline collection | `complete` | None |
| `active` | valid `chunked-required` control | new chunk child `prepared` | One attempt after child arm |
| `active` | completed collection with truncation | `recoverable`, retransmission child `prepared` | One attempt after child arm |
| unresolved | metadata missing/ambiguous | `recoverable` | No automatic send |
| `complete` | identical evidence replay | `complete` | None |
| `complete` | different evidence/content | immutable conflict | None |

### 7.2 Artifact

| State | Event | Next state |
| --- | --- | --- |
| `prepared` | arm/send/read-back | `active` |
| `active` | valid short untruncated manifest | `verifying` |
| `active` | manifest missing or truncated | `recoverable`; remote discovery remains allowed |
| `verifying` | exact remote verification succeeds | `complete` |
| `verifying` | branch absent | `recoverable` |
| `verifying` | wrong commit/tree/blob/path/ref | `recoverable` with stable error |
| `complete` | same commit/blob | idempotent |
| `complete` | different remote result | immutable conflict |

Artifact verification may complete without a readable chat manifest only when the pre-authorized branch/path independently verify. Record `completion_basis=artifact-discovery`. This is the explicit exception to native message completeness.

### 7.3 Chunked

| State | Event | Next state |
| --- | --- | --- |
| `prepared` | arm/send initial turn | `active` |
| `active` | valid nonfinal chunk n | spool chunk, prepare turn n+1 |
| `active` | valid final chunk n with count n | reassemble/hash, `complete` |
| `active` | completed response truncated | discard prefix, prepare retransmission from last verified boundary |
| `active` | envelope/chain error | `recoverable`; repair child only after completed-response evidence |
| `active` | send outcome indeterminate | `recoverable`; collect only, no replacement child |
| `active` | timeout/thread not loaded | unchanged collect-only recovery |
| `complete` | same chunks/evidence | idempotent |
| `complete` | new or changed chunk | immutable conflict |

The dispatch `send_attempt_total` equals the number of armed turns. It may exceed one. Each turn's `submission_count` remains at most one.

## 8. Artifact mode

### 8.1 Prepared artifact contract

`--artifact-contract-file` contains exactly:

```json
{
  "schema": "codex-pro-dispatch.artifact-contract/v1",
  "repository_id": 123456789,
  "repository": "owner/repository",
  "visibility": "private",
  "remote_url": "https://github.com/owner/repository.git",
  "base_branch": "main",
  "base_sha": "0123456789abcdef0123456789abcdef01234567",
  "branch": "codex/dispatch-result-example",
  "path": "docs/specs/result.md",
  "commit_message": "docs: add dispatched result",
  "encoding": "utf-8",
  "media_type": "text/markdown",
  "allowed_change": "add-single-markdown",
  "artifact_max_bytes": 2097152,
  "sensitivity": "internal",
  "protected_refs": [
    {
      "ref": "refs/heads/main",
      "sha": "0123456789abcdef0123456789abcdef01234567",
      "allow_fast_forward": true
    }
  ],
  "prepared_at": "2026-09-03T00:00:00.000Z"
}
```

Validate the exact key set and types. Rules:

- numeric repository ID;
- canonical lowercase `owner/repository`;
- canonical HTTPS GitHub remote, no user info, query, fragment, embedded credential, or alternate host;
- SHA values are lowercase 40-hex for current GitHub SHA-1 repositories;
- branch passes `git check-ref-format --branch`, is not the base branch, is not prefixed by `refs/`, and contains no controls;
- path is portable ASCII relative POSIX, ends in `.md`, has no empty, `.` or `..` segment, no backslash, no `.git` segment, no leading slash, and is at most 240 bytes;
- commit message is one printable UTF-8 line, 1 to 120 bytes;
- `allowed_change` is exactly `add-single-markdown`;
- protected refs are sorted, unique, and include the base/default branch;
- artifact byte limit is positive and capped by a project maximum;
- public repositories reject sensitive categories and require an explicit acknowledgement.

Preparation performs parent-side remote checks:

- repository metadata matches ID, name, URL, visibility, and default branch;
- base branch head equals `base_sha` at preparation;
- artifact branch does not exist;
- artifact path is absent from the base tree;
- worker connector write capability and explicit user authorization are recorded;
- parent independent read access succeeds;
- public repositories require `--allow-public-artifact`.

The receipt stores the canonical contract and its SHA-256, not arbitrary connector output.

### 8.2 Worker authorization

The wrapped prompt authorizes only:

- create the named disposable branch from the exact base SHA;
- add the one named UTF-8 Markdown file;
- create exactly one commit with the exact message;
- return the canonical manifest.

It expressly forbids:

- modifying the base branch or any other ref;
- force-pushing;
- any other added, modified, renamed, deleted, or mode-changed path;
- opening or modifying a PR;
- merging;
- tags, releases, issues, settings, workflows, deployments, or tasks;
- pasting the full artifact into chat;
- writing credentials, tokens, private keys, or unrelated sensitive content.

Authorization applies only to this assignment and cannot be inherited by a continuation.

### 8.3 Canonical chat manifest

The full assistant response is at most 4,096 UTF-8 bytes and consists of these exact LF-separated lines, with no code fence, blank line, unknown field, or trailing prose:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<assignment-id>]
[CODEX_PRO_DISPATCH_ARTIFACT_V1]
schema=codex-pro-dispatch.artifact-manifest/v1
assignment_id=<assignment-id>
repository_id=<decimal>
repository=<owner>/<repo>
remote_url=https://github.com/<owner>/<repo>.git
base_branch=<base-branch>
base_sha=<40-lowercase-hex>
branch=<branch>
commit_sha=<40-lowercase-hex>
path=<relative-markdown-path>
byte_length=<positive-decimal>
content_sha256=<64-lowercase-hex>
encoding=utf-8
media_type=text/markdown
changed_path_count=1
commit_message=<exact-single-line-message>
[CODEX_PRO_DISPATCH_ARTIFACT_END_V1]
```

Split fields at the first `=`. Reject duplicate or unknown keys, missing keys, noncanonical decimals/hex, controls, and any contract-bound value that differs from the receipt. The manifest is only a locator and worker report.

### 8.4 Exact parent verification

Implement `GitArtifactVerifier` in a private mode-0700 temporary directory with a bare Git repository and no worktree.

1. Validate the manifest against the receipt. For manifest-free recovery, use only the pre-authorized branch and path and record `artifact-discovery`.
2. `git ls-remote` the canonical remote. Require the branch head to equal the reported/discovered commit.
3. Initialize a private bare repository and fetch the exact base commit, artifact commit, current base head, and protected-ref heads without tags.
4. Inspect `git cat-file -p <commit>`:
   - exactly one parent;
   - parent exactly equals prepared base SHA;
   - commit message bytes equal the prepared message plus Git's terminating LF.
5. Inspect `git diff-tree --no-commit-id --name-status -r -z <base> <commit>`:
   - exactly one record;
   - status exactly `A`;
   - path bytes exactly equal the authorized path.
6. Confirm the base tree lacks the path.
7. Inspect `git ls-tree -z <commit> -- <path>`:
   - mode exactly `100644`;
   - type exactly `blob`;
   - exact path bytes.
   Reject `100755`, `120000`, `160000`, trees, symlinks, executables, and submodules.
8. Read `git cat-file blob <commit>:<path>`. Never trust a checkout or caller-supplied copy.
9. Validate strict UTF-8, no BOM, no NUL, LF-only line endings, final LF, and configured size limit.
10. Compute exact byte length and SHA-256. Require equality with manifest when present.
11. Re-run `ls-remote` for the artifact branch. Any movement during verification fails.
12. Verify refs:
   - base unchanged is valid;
   - base fast-forward from prepared SHA is valid when the artifact commit is not an ancestor of the new base head;
   - base rewrite/deletion or inclusion of artifact commit is rejected;
   - every other protected ref remains at its prepared SHA.
13. Store commit, tree, blob object ID, mode, length, SHA-256, branch observations, base state, verifier version, nonce, and verification time.
14. Materialize only the verified blob to an exclusive mode-0600 result file and `fsync` it.
15. Mark complete with `completion_basis=artifact-manifest` or `artifact-discovery`.

### 8.5 Stale and moving-base definitions

- `base_unchanged`: current base head equals prepared base SHA.
- `base_advanced`: prepared base SHA remains an ancestor of current base head, and artifact commit is not an ancestor of current base head. Transport verification remains valid. Integration may require a rebase outside this protocol.
- `base_rewritten`: prepared base SHA is not an ancestor of current base head. Fail closed.
- `artifact_merged`: artifact commit is an ancestor of current base head. The forbidden merge boundary may have been crossed. Fail closed.
- `artifact_stale`: authorized branch missing, branch no longer points to verified commit, or commit/path/blob differs from contract. Ordinary base advancement is not stale.

A later force-push cannot change a completed content-addressed commit. `status --audit-artifact` may report branch drift but cannot rewrite completion.

### 8.6 Retention and cleanup

- Public repository content is public immediately and may remain recoverable after branch deletion. Require public-retention acknowledgement. Reject `secret`, `personal`, or `regulated` sensitivity.
- Private/internal repository content remains visible to collaborators, administrators, backups, and audit systems. Branch deletion is not secure erasure.
- Never use artifact mode for credentials, private keys, tokens, medical records, or content that must not enter Git history.
- Do not delete the branch automatically. Keep it until the parent has consumed and independently recorded the result.
- Cleanup is a separate explicit action. Before branch deletion, require the head still equals the verified commit. Record observed deletion but never claim object erasure.
- Connector permission/SSO failure, branch race, repository transfer, or visibility mismatch leaves the receipt recoverable and never triggers a parent-side substitute write.

## 9. Fully read-only chunked mode

### 9.1 Turn model

The first user message contains the original assignment and chunk instructions. A nonfinal chunk causes the helper to prepare a continuation child containing only:

- dispatch and turn identity;
- next expected chunk index;
- previous accepted chain digest;
- instruction to continue the existing deliverable without repeating accepted content;
- exact response envelope.

It does not repeat the original assignment. Each child has a unique result marker and is separately armed. A crash after child arm preserves no-resend for that child.

### 9.2 Exact chunk envelope

Every assistant chunk is:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<turn-id>]
[CODEX_PRO_DISPATCH_CHUNK_V1 group_id=<assignment-id> index=<n> previous_chain_sha256=<64-lowercase-hex> final=<0-or-1> count=<0-or-n>]
<Markdown segment>
[CODEX_PRO_DISPATCH_CHUNK_END_V1 group_id=<assignment-id> index=<n>]
```

Rules:

- result marker is first nonempty line;
- chunk header is the next line;
- footer is the final nonempty line;
- header field order is exact;
- index starts at 1 and equals the next expected index;
- chunk 1 echoes 64 zeroes as the prior chain;
- nonfinal chunks use `final=0 count=0`;
- final chunk uses `final=1 count=<same n>`;
- payload is exact LF-normalized text between header and footer;
- payload cannot contain a line beginning with the dispatch result-marker prefix;
- empty nonfinal chunks are rejected; empty final is allowed only after at least one nonempty accepted chunk;
- requested target is 10,000 UTF-8 bytes; hard payload maximum is 16,000 bytes;
- helper, not model, counts bytes;
- `truncated=false` is mandatory for every chunk.

### 9.3 Hash chain

Define:

```text
chain_0 = 32 zero bytes
payload_n = exact normalized UTF-8 payload bytes
chain_n = SHA256(
    'codex-pro-dispatch/chunk-v1\0' ||
    chain_(n-1) ||
    uint64_be(n) ||
    uint64_be(len(payload_n)) ||
    payload_n
)
```

The helper computes `chain_n` and includes its hex in the next child prompt. The worker only echoes that digest. It is never asked to calculate SHA-256 or exact byte lengths.

Store per chunk: assistant message ID, evidence hash, index, byte length, payload SHA-256, previous chain, current chain, final flag, and spool filename.

### 9.4 Reassembly

Canonical result bytes are:

```python
b'\n'.join(payload_1, payload_2, ..., payload_n)
```

The helper-owned single LF between chunks is part of the result. Prompts instruct the worker to end chunks at Markdown block boundaries, but correctness does not depend on model character counting.

On final chunk:

- require `count == index`;
- require no gaps;
- verify every spool file against receipt hashes;
- stream reassembly to a private result file;
- compute total byte length and SHA-256 while writing;
- `fsync` file and directory;
- store chunk count, final chain, total length, total hash;
- mark complete.

The model's final count is only a finality assertion. Helper-derived totals are authoritative.

### 9.5 Durable spool and crash recovery

Add `RuntimePaths.spool_dir = state_dir / 'spool'`:

```text
state/spool/<assignment-id>/chunk-000001.part
state/spool/<assignment-id>/chunk-000002.part
```

Requirements:

- directories 0700, files 0600;
- refuse symlinks and preexisting unexpected paths;
- temporary sibling write, `fsync`, atomic rename, directory `fsync`;
- recoverable journal:
  1. receipt records `spool_write_pending` plus expected hash;
  2. atomic file write;
  3. receipt records `spooled`;
- restart reconciles pending entries by exact file hash;
- missing/corrupt chunks are not skipped; re-collect the exact assistant message ID without sending and rewrite only if evidence and payload hashes match;
- orphan files cause a state error and explicit quarantine/repair, never automatic append;
- chunk bodies persist only until result delivery and exact parent restoration, then `pro-dispatch result cleanup` removes verified spool files;
- cleanup does not alter completed hashes;
- disclose that filesystem deletion is not secure erasure.

Inline bodies remain transient. Artifact bodies remain in Git. JSON receipts never contain result bodies.

### 9.6 Duplicate, conflict, gap, and timeout behavior

- same assistant message ID and evidence hash: idempotent;
- same message ID with different text/evidence: `chunk_message_conflict`;
- different message ID for same turn/marker: `chunk_duplicate_response`;
- repeated index accepted only for exact already-accepted replay;
- higher index: `chunk_gap`;
- lower index: `chunk_replay`;
- prior-chain mismatch: `chunk_chain_mismatch`;
- final count mismatch: `chunk_final_count_mismatch`;
- missing footer or trailing prose: `chunk_envelope_incomplete`;
- native timeout or thread-not-loaded: current turn stays collect-only;
- native send indeterminate: do not prepare a replacement continuation;
- completed truncated response: prepare one result-retransmission child from last verified chunk boundary.

### 9.7 First response already truncated

A visible truncated prefix is not a chunk and cannot be accepted or reassembled. Record only evidence hash, prefix length/hash, message ID, and `collection_truncated=true`.

Recoverable options:

- a later native read of the same assistant message ID returns `truncated=false`, then collect normally;
- a pre-authorized artifact branch can be verified independently;
- inline/chunked mode can send a new child asking the same worker to retransmit the complete result in chunk protocol from chunk 1 or the last verified boundary.

The unseen suffix cannot be reconstructed from a prefix. A regenerated result cannot be proven byte-for-byte identical to that unseen original tail.

The retransmission is not a resend of the original assignment. It is a new child prompt referencing the existing dispatch and requesting re-emission of the result. It has a new turn ID, prompt hash, result marker, arm, and one send attempt. The original wrapped prompt is never sent again.

## 10. CLI and Python interfaces

### 10.1 Prepare

Inline:

```bash
pro-dispatch prepare \
  --parent-task-id '<parent-id>' \
  --result-mode inline \
  --native-controls-confirmed \
  --prompt-file '<prompt-file>'
```

Chunked uses `--result-mode chunked`.

Artifact:

```bash
pro-dispatch prepare \
  --parent-task-id '<parent-id>' \
  --result-mode artifact \
  --artifact-contract-file '<contract.json>' \
  --authorize-artifact-write \
  --worker-github-write-confirmed \
  --native-controls-confirmed \
  --prompt-file '<prompt-file>'
```

Public repositories additionally require `--allow-public-artifact`.

Prepare success JSON:

```json
{
  "ok": true,
  "status": "prepared",
  "assignment_id": "dispatch-example",
  "result_mode": "chunked",
  "worker_conversation_id": "worker-id",
  "parent_task_id": "parent-id",
  "turn": {
    "turn_id": "dispatch-example",
    "sequence": 1,
    "status": "prepared",
    "wrapped_prompt": "...",
    "wrapped_prompt_sha256": "..."
  },
  "receipt_path": "..."
}
```

### 10.2 Arm and outbound verification

Backward-compatible form works when exactly one current turn exists:

```bash
pro-dispatch arm '<assignment-id>'
```

Preferred explicit form:

```bash
pro-dispatch arm '<assignment-id>' --turn-id '<turn-id>'
pro-dispatch submitted '<assignment-id>' \
  --turn-id '<turn-id>' \
  --sent-prompt-file '<native-read-back-file>'
```

All current exact read-back, trailing-newline correction, indeterminate-send, and cooldown behavior remains per turn.

### 10.3 Collect

```bash
pro-dispatch collect '<assignment-id>' \
  --turn-id '<turn-id>' \
  --native-evidence-file '<native-collection.json>' \
  --result-file '<private-result-file>'
```

The skill should always provide `--result-file` so bodies do not enter terminal logs.

Inline success:

```json
{
  "ok": true,
  "status": "complete",
  "completion_basis": "native-inline",
  "assignment_id": "dispatch-example",
  "turn_id": "dispatch-example",
  "result_file": "/private/result.md",
  "byte_length": 8421,
  "sha256": "...",
  "collection": {
    "assistant_message_id": "message-id",
    "truncated": false
  }
}
```

Nonfinal chunk:

```json
{
  "ok": true,
  "status": "active",
  "action": "send_next_turn",
  "accepted_chunk": {
    "index": 2,
    "byte_length": 9981,
    "sha256": "...",
    "chain_sha256": "..."
  },
  "next_turn": {
    "turn_id": "dispatch-example.chunk.0003",
    "sequence": 3,
    "status": "prepared",
    "wrapped_prompt": "...",
    "wrapped_prompt_sha256": "..."
  }
}
```

Truncation returns nonzero and reports the safely prepared recovery turn when one exists:

```json
{
  "ok": false,
  "error_type": "TruncationError",
  "error_code": "collection_truncated",
  "details": {
    "assignment_id": "dispatch-example",
    "turn_id": "dispatch-example",
    "recoverable": true,
    "no_resend": true,
    "next_action": "arm_prepared_retransmission_turn",
    "next_turn": {
      "turn_id": "dispatch-example.chunk.0002",
      "status": "prepared"
    }
  }
}
```

### 10.4 Artifact verification

```bash
pro-dispatch artifact verify '<assignment-id>' \
  --result-file '<private-result-file>'

pro-dispatch artifact verify '<assignment-id>' \
  --discover \
  --result-file '<private-result-file>'
```

Success returns repository identity, base state, branch, commit/tree/blob IDs, mode, exact byte length/SHA-256, verification timestamps, and completion basis.

### 10.5 Result materialization and cleanup

```bash
pro-dispatch result materialize '<assignment-id>' \
  --result-file '<private-result-file>'

pro-dispatch result cleanup '<assignment-id>'
```

- `materialize` supports completed chunked receipts from verified spool and artifact receipts from the exact verified commit.
- Inline bodies are deliberately not retained after transient cleanup.
- `cleanup` removes only verified chunk files after parent restoration.
- Optional artifact branch cleanup is separate and requires explicit authorization plus an unchanged verified branch head.

### 10.6 Complete compatibility

For v1.1.1 and later:

- unresolved `complete --response-file` without native evidence fails with `collection_evidence_required`;
- a completed legacy receipt may be queried idempotently, but new text cannot rewrite it;
- the skill uses `collect`;
- retain `complete` only as a deprecated alias when supplied the same mandatory `--native-evidence-file`; remove response-only completion in the next major version.

### 10.7 Python API

Add:

```python
prepare_assignment(
    prompt,
    *,
    result_mode=ResultMode.INLINE,
    artifact_contract=None,
    ...,
)

collect_turn(
    assignment_id,
    turn_id,
    evidence: NativeCollectionEvidence,
    result_path=None,
) -> CollectionOutcome

verify_artifact(
    assignment_id,
    result_path,
    discover=False,
) -> ArtifactVerificationResult

materialize_result(assignment_id, result_path) -> ResultDescriptor
cleanup_result(assignment_id) -> CleanupResult
```

Do not expose a standalone `truncated` parameter.

### 10.8 Stable errors

Continue returning `ok`, `error`, `error_type`, and `details`; add stable `error_code`.

| Error class | Exit | Representative codes |
| --- | ---: | --- |
| Existing configuration/state errors | 2-6 | preserve existing behavior |
| `CollectionEvidenceError` | 7 | `collection_metadata_missing`, `collection_evidence_invalid`, `collection_wrong_worker`, `collection_message_not_complete`, `collection_truncation_unknown` |
| `TruncationError` | 8 | `collection_truncated` |
| `ArtifactProtocolError` | 9 | `artifact_contract_invalid`, `artifact_manifest_invalid`, `artifact_authorization_missing` |
| `ArtifactVerificationError` | 10 | `artifact_branch_missing`, `artifact_parent_mismatch`, `artifact_extra_paths`, `artifact_file_mode_invalid`, `artifact_hash_mismatch`, `protected_ref_changed` |
| `ChunkProtocolError` | 11 | `chunk_gap`, `chunk_replay`, `chunk_chain_mismatch`, `chunk_duplicate_response`, `chunk_envelope_incomplete` |
| `ReceiptMigrationError` | 12 | `legacy_receipt_unmigratable`, `receipt_schema_unsupported`, `spool_reconciliation_failed` |

Errors never expose bodies, credentials, remote tokens, contact data, or raw Git stderr. Persist categories and hashes only, preserving v1.1 diagnostic policy.

## 11. Security and failure analysis

### 11.1 Wrong worker or assignment

- Compare configured worker, requested worker, loaded worker, and receipt worker.
- Require the current turn's exact marker.
- Bind the first accepted stable assistant message ID.
- Reject another message ID for that turn.
- Never use title, visible position, or globally newest conversation as identity.

### 11.2 Marker and envelope attacks

- Validate native evidence before marker parsing.
- Keep result marker first.
- Artifact/chunk envelopes have exact headers, footers, field order, and field sets.
- Reject extra fields and trailing prose.
- Reserve marker-prefixed lines inside chunk payloads.
- Independently validate every path, URL, ref, SHA, count, and mode. Worker values are not commands.

### 11.3 Native truncation and stale reads

- `truncated=true` never completes.
- Missing flag never defaults false.
- Message must be completed.
- Stale thread reads cause no send.
- A completed truncated response can authorize a new result-retransmission child.
- An uncertain send cannot authorize a replacement child.

### 11.4 Artifact TOCTOU

- Preflight pins repository ID, canonical URL, visibility, base, branch absence, path absence, and protected refs.
- Verification reads branch head before and after object inspection.
- Exact commit/blob remains immutable if the branch later moves, but audit reports drift.
- Base fast-forward is valid; base rewrite or artifact merge is not.
- Network work occurs outside state lock under a nonce.
- Remotes cannot contain credentials.
- Categorize/hash Git stderr rather than storing raw text.
- Use bare Git and `cat-file`, never a worktree.

A local verifier cannot prove absence of every unrelated connector side effect without provider audit logs. Reduce risk through narrow authorization and do not claim broader proof.

### 11.5 Malformed Git objects

Reject wrong repository, branch, base, parent, commit, message, or path; merge commits; multiple commits; extra paths; modified/deleted/renamed paths; traversal/control/backslash paths; executable mode; symlink; submodule; directory at path; NUL/binary; invalid UTF-8; BOM; CRLF; missing final LF; length/hash mismatch; branch movement; protected-ref rewrite; or artifact merge.

### 11.6 Duplicate completion

- Identical native evidence, chunks, or artifact verification is idempotent.
- Different content against a complete receipt is an immutable conflict.
- A second verifier cannot replace a stored nonce/result.
- A complete dispatch cannot accept another chunk.

### 11.7 Interrupted sends and HTTP 403

Apply existing arm, no-resend, unusual-activity classification, request ID, and 1,800-second cooldown to every child. Cooldown blocks preparation of a new child or logical dispatch. Existing collection and artifact verification are read-only and remain allowed.

### 11.8 Restart and parent restoration

- Receipts and chunk spools are durable.
- `recover` returns group/current-turn identity, expected collection action, contract/chain state, spool health, no-resend fields, and exact parent task ID.
- Restore the exact parent in a finally path even when collection/verification fails.
- If restoration fails, retain temporary/spool content, report exact parent ID, and keep recoverable.
- On restart inspect the existing worker/receipt and never resend the current turn.

## 12. Concrete implementation file plan

### 12.1 Core and CLI

Modify `src/codex_pro_dispatch/core.py`:

- split worker and assignment schema versions;
- add v1-to-v2 normalization/migration;
- add logical-dispatch and child-turn helpers;
- preserve global lock and atomic writes;
- replace response-only completion with evidence-gated collection;
- add result descriptors, verification nonces, spool reconciliation, and richer recovery;
- preserve cooldown and exact outbound read-back behavior.

Current seams: schema/status at lines 18-27; marker parsing at 240-289; prepare/continuation at 526-606; arm at 622-648; outbound verification at 651-748; completion at 888-946; recovery at 947-989.

Add `src/codex_pro_dispatch/collection.py`:

- `NativeCollectionEvidence`;
- strict duplicate-key-rejecting JSON parser and canonicalizer;
- worker/message/time/status/truncation checks;
- inline/control parsing;
- collection errors.

Add `src/codex_pro_dispatch/chunked.py`:

- envelope parser;
- chain calculation;
- next-turn prompt generation;
- spool journal/write/reconcile;
- streaming final reassembly.

Add `src/codex_pro_dispatch/artifact.py`:

- artifact contract and manifest parser;
- path/ref/URL validators;
- private bare-Git preflight/verifier;
- protected-ref/base-state logic;
- exact blob materialization.

Modify `src/codex_pro_dispatch/cli.py`:

- `--result-mode` and artifact authorization flags;
- turn-aware `arm` and `submitted`;
- `collect`;
- `artifact verify`;
- `result materialize`, `result cleanup`, and optional audited branch cleanup;
- stable `error_code`;
- deprecate unsafe response-only `complete`.

Modify `src/codex_pro_dispatch/__init__.py` to export new public types/functions and update version metadata. No third-party Python runtime dependency is required; continue using the standard library plus existing `git` executable.

### 12.2 Skill and public documentation

Modify:

- `skills/codex-pro-dispatch/SKILL.md`: evidence preflight, result-mode selection, per-turn send semantics, artifact authorization/retention, chunk recovery, cleanup.
- `skills/codex-pro-dispatch/references/native-protocol.md`: canonical evidence envelope and truncation/retransmission behavior.
- `skills/codex-pro-dispatch/references/github-verification.md`: exact commit/tree/blob verification, moving-base rules, retention, cleanup.
- Add `skills/codex-pro-dispatch/references/long-results.md`: canonical manifest, chunk envelope/chain, operator examples.
- `docs/compatibility.md`: require stable assistant message ID and explicit truncation metadata; list artifact parent-Git dependency.
- `docs/acceptance.md`: add truncation, artifact, chunk, migration, crash, and cleanup matrices.
- `SECURITY.md`: Git retention and durable temporary chunk-body storage.
- `README.md`: modes, defaults, limitations, commands.
- `CHANGELOG.md`: separate v1.1.1 patch and v1.2.0 feature entries.
- Add candidate receipts under `docs/releases/` only after exact native acceptance.

### 12.3 Tests and version files

Modify existing:

- `tests/test_core.py`
- `tests/test_cli.py`
- `tests/test_skill_contract.py`
- `tests/test_install_scripts.py` only if command/help installation assumptions change.

Add:

- `tests/test_collection.py`
- `tests/test_chunked.py`
- `tests/test_artifact.py`
- `tests/test_receipt_migration.py`
- redacted v1 receipt fixtures;
- temporary Git fixture builder code, not binary repositories.

Update version-bearing files together as required by `CONTRIBUTING.md:43-54`:

- `VERSION`
- `src/codex_pro_dispatch/__init__.py`
- skill frontmatter
- `.codex-plugin/plugin.json`
- `README.md`
- `CHANGELOG.md`

CI continues standard-library tests and syntax checks on macOS and Ubuntu (`.github/workflows/ci.yml:1-35`).

## 13. Test and acceptance plan

### 13.1 Unit and state tests

Collection evidence:

- short inline success with explicit false flags;
- marker-bearing truncated prefix cannot complete;
- missing truncation field;
- outer truncation true;
- wrong requested/loaded worker;
- wrong role/status;
- observation predating submission;
- marker mismatch;
- identical evidence replay;
- same message ID with conflicting text;
- different response message for one turn;
- inline control escalation.

Receipt/state:

- one unresolved logical dispatch;
- one active child;
- each child arms/submits once;
- `continuation_of` still requires complete prior dispatch;
- chunk children do not use `continuation_of`;
- active cooldown blocks new children;
- completion immutability;
- concurrent collect/arm;
- crash at every atomic boundary.

Migration:

- every v1 active state;
- v1 complete remains immutable and reports unverifiable collection;
- v1.1.1 evidence maps correctly;
- cooldown/request ID preserved;
- corrupt/unknown schema fails closed;
- no body introduced.

Chunking:

- one, two, and many chunks;
- Unicode, tables, and code fences;
- target/hard size boundary;
- missing/extra header/footer;
- reserved marker in payload;
- gaps, replay, duplicates, conflicting duplicates;
- chain mismatch;
- final/count mismatch;
- first response truncated then chunk recovery;
- truncated later chunk from last verified boundary;
- restart after journal pending, rename, and before receipt commit;
- missing/corrupt/orphan spool;
- exact final length/hash;
- cleanup only after completion/restoration.

### 13.2 Adversarial Git fixtures

Create private temporary bare repositories and cover:

- valid single-file artifact over 20,000 bytes;
- exact byte length and SHA-256;
- wrong repository identity/URL/visibility;
- branch already exists;
- base mismatch during prepare;
- base fast-forward accepted;
- base rewrite rejected;
- artifact merged into base rejected;
- branch missing/moved, including movement during verification;
- wrong parent;
- merge commit;
- multiple commits;
- wrong commit message;
- path existing at base;
- extra file;
- modified/deleted/renamed file;
- executable `100755`;
- symlink `120000`;
- submodule `160000`;
- tree at path;
- traversal/control/backslash path;
- invalid UTF-8, BOM, NUL/binary, CRLF, missing final LF;
- length and SHA mismatch;
- protected-ref change;
- missing public acknowledgement;
- idempotent duplicate verify and immutable conflict.

Tests inspect exact Git objects and never trust a worktree.

### 13.3 CLI tests

- legacy commands remain usable where safe;
- unresolved response-only `complete` fails with `collection_evidence_required`;
- stable `error_code` is present;
- prepare examples for all modes;
- explicit turn selection;
- collect success/rejection;
- artifact discovery;
- result materialization/cleanup;
- raw reasons and Git stderr are not persisted;
- output files are 0600 and reject symlinks/existing paths;
- status/recover expose transport metadata without bodies.

### 13.4 Native acceptance

Run on exact candidate commit and record desktop app/build, macOS, architecture, and native tool names.

1. Short inline: one send, explicit `truncated=false`, exact payload/hash, parent restored.
2. Inline truncation: generate over 20,000 characters; native reports true; helper refuses completion; original assignment appears once; retransmission child completes via chunks.
3. Missing metadata simulation: helper fails closed and sends nothing.
4. Artifact: private disposable repository, result over 20,000 bytes, manifest under 4,096 bytes, exact object verification, no PR/merge/protected-ref change. Public mode separately verifies acknowledgement.
5. Chunked: at least 50,000 UTF-8 bytes over at least five chunks, app restart between chunks, exact reassembled length/hash, every chunk untruncated, send count equals armed child count.
6. Force duplicate/conflicting chunk and verify fail-closed behavior.
7. Reproduce thread-not-loaded, delayed read-back, HTTP 403 cooldown, and restoration.
8. Verify transient/spool permissions and cleanup.
9. Confirm no clipboard, Accessibility, AppleScript, CDP, browser automation, or scraping.

### 13.5 Release gates

v1.1.1:

- all existing tests plus collection-evidence tests pass on macOS and Ubuntu;
- native short inline passes;
- native truncated inline is rejected and recoverable;
- no unresolved receipt completes from response text alone;
- parent restoration and no-resend acceptance pass.

v1.2.0:

- all v1.1.1 gates;
- all adversarial artifact cases;
- over-20K artifact acceptance;
- over-50K chunk acceptance with exact reassembled hash;
- crash recovery at every journal boundary;
- v1 migration matrix;
- no result body in JSON receipts;
- no public artifact without explicit acknowledgement;
- new redacted candidate acceptance receipt before stable tag.

## 14. Rollout, versioning, rollback, and risks

### 14.1 Release recommendation

Ship two releases.

#### v1.1.1 fail-closed patch

Scope:

- native collection-evidence envelope;
- explicit `truncated=false` requirement;
- disable response-only unresolved completion;
- retain current receipt schema with additive collection fields;
- update skill, native, compatibility, security, and acceptance docs.

Rationale: silent data loss is already possible. The fix is small and independently testable and should not wait for transports.

#### v1.2.0 feature release

Scope:

- assignment schema v2;
- result modes;
- artifact contract, manifest, and verifier;
- chunk group, child turns, spool, and reassembly;
- migration, cleanup, security, and expanded acceptance.

This is MINOR because it adds user-visible capabilities. The repository defines MINOR as new capabilities and PATCH as backward-compatible fixes (`CONTRIBUTING.md:43-54`).

### 14.2 Phased order

1. Add failing marker-bearing truncation regression.
2. Implement v1.1.1 evidence parser and fail-closed completion.
3. Update protocol/docs and run exact native acceptance.
4. Release v1.1.1.
5. Add schema-v2 normalization and group/turn state with new modes disabled.
6. Port inline to v2.
7. Implement artifact contract, bare-Git verifier, and adversarial fixtures.
8. Implement chunk envelope, chain, spool journal, reassembly, and retransmission children.
9. Update all public/security/compatibility/release docs.
10. Run full local, CI, and native candidate gates; publish v1.2.0 only after redacted receipt.

### 14.3 Rollback

- Do not roll back v1.1.1 because that reopens silent truncation acceptance.
- Operational v1.2 rollback disables new artifact/chunk selection while retaining v1.2 readers and recovery code.
- Do not binary-downgrade while active schema-v2 receipts exist.
- Existing artifact/chunk receipts remain collectable during feature disablement.
- Never delete receipts/spool as rollback.
- Prefer a corrective v1.2.x patch over schema downgrade.

### 14.4 Prioritized risks

1. Native metadata may remain structurally rather than cryptographically attested.
2. The model may fail to follow chunk envelopes; protocol detects but cannot guarantee regeneration.
3. An unseen truncated suffix cannot be reconstructed exactly.
4. Chunk spool and Git artifact retention can expose sensitive output.
5. Git ref checks cannot prove absence of every non-ref/provider side effect without audit logs.
6. Legitimate concurrent protected-ref movement can cause conservative verification failure.
7. Host schemas and stable message IDs may change by desktop build.
8. Artifact branch cleanup may be mistaken for secure erasure.

## 15. Open decisions and recommended defaults

1. **Inline budget:** use 12,000 requested and 16,000 hard accepted UTF-8 bytes.
2. **Artifact maximum:** default 2 MiB, configurable only downward per assignment.
3. **Artifact branch cleanup:** manual and separate, never automatic.
4. **Public artifacts:** disabled unless explicitly acknowledged; sensitive categories always rejected.
5. **Chunk target:** 10,000 bytes with 16,000 hard maximum.
6. **Native evidence:** require stable assistant message ID now; do not support text-only hosts.
7. **Auto mode:** defer; future auto may choose only inline versus chunked.
8. **Legacy complete receipts:** preserve unchanged and label collection integrity unverifiable.

## 16. Explicit non-goals

- Increasing or bypassing the native reader limit.
- Browser, web, Accessibility, AppleScript, clipboard, CDP, or scraping transport.
- Automatically resending the original assignment.
- Describing a multi-turn chunk sequence as one send.
- Automatically selecting artifact mode.
- Multi-file, existing-file, binary, symlink, executable, or submodule artifacts.
- PRs, merges, tags, releases, issues, settings, workflows, deployments, or task creation as artifact transport.
- Secure-erasure guarantees for SSDs, Git history, provider backups, or audit logs.
- Model-computed authoritative hashes, byte lengths, or total sizes.
- A daemon, browser extension, model router, or external result service.
- Proving no unrelated connector action without provider audit evidence.
- Reopening or rewriting completed legacy receipts.

## 17. Final implementation acceptance statement

The transport is ready only when a correct leading marker inside a truncated prefix cannot complete, short inline behavior remains simple, an explicitly authorized single-file Git artifact is verified from exact remote objects, a fully read-only multi-turn result reassembles with helper-computed bytes and hashes, every native message remains at most once, the original assignment is never resent, and every recovery path restores the exact parent task without trusting worker claims.
