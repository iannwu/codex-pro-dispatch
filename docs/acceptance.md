# Native acceptance matrix

Unit tests exercise the receipt state machine, strict parsers, chunk spool, and
bare-Git verifier. They cannot prove the official app's native conversation
behavior. Run this matrix against the exact v1.2.0 candidate commit before calling
it compatible or stable. Record app version/build, macOS version/architecture,
native capability names, adapter-contract ID, candidate SHA, and redacted results.

The 2026-08-24 v1.1 development run and v1.1.0 receipt are historical only; they
are not v1.2.0 acceptance evidence.

## A. Host setup and adapter contract

### A0. Capability preflight

- Confirm every capability in [compatibility.md](compatibility.md), including one
  selected-message collection operation carrying both exact worker IDs, exact
  submitted-user association, item-level finality provenance, message truncation,
  and selected-result outer integrity.
- Run `pro-dispatch doctor` without `--native-controls-confirmed`; it must fail.
- Run `prepare` without `--native-controls-confirmed` and an unreadable prompt
  path; it must fail before reading the path or creating a receipt.
- After completing the semantic preflight, configure a dedicated user-confirmed Pro
  worker and run `doctor --native-controls-confirmed`.

Expected: missing capability fails before state writes; the assertion is not
treated as discovery; healthy asserted invocation reports `local_ok: true`.

### A1. Adapter omission and finality contract

- Capture native evidence with both explicit false truncation values and complete
  item-level finality; it must be accepted.
- Omit message or outer truncation on the shipped adapter; each must fail closed.
- If testing a future normalization adapter, attach authoritative version-scoped
  host contract/inspected deployment evidence proving every shortening reports
  true. Mere examples are insufficient.
- Try a turn-level/enclosing finality indication without selected-message finality;
  it must fail.

Expected: raw/normalized values, requested/loaded worker IDs, message association,
and provenance are redacted/body-free receipt data; no caller flag can override
them.

## B. Inline one-turn roundtrip and reread integrity

Submit an inline prompt that requests exactly `CODEX_TO_CHAT_PRO_OK_7319`.

Expected:

- receipt is armed before one native send and read-back proves the exact existing
  user message;
- only one matching, untruncated selected assistant message completes;
- an evidence reread with the same source/content and a later `observed_at` is
  idempotent;
- a changed accepted source ID or content is an immutable conflict;
- completion, result materialization, and parent restoration are distinct states;
- clipboard is unchanged and exact parent is restored.

## C. Truncation and response-only regression

For an inline assignment, deliberately cause a native reader prefix above its
limit. Test separately message `truncated: true`, outer `truncated: true`, each
field omitted, an incomplete generation, and a stale/mismatched marker.

Expected:

- no visible prefix, marker, `--response-file`, `complete`, or response-only
  legacy field completes a new/unresolved receipt;
- `collect` preserves no-resend state and fails closed with recoverable evidence;
- only an allowlisted adapter contract that supports complete reread upgrade may
  accept a later complete reread of the same selected source;
- the original assignment appears once and is never resent.

## D. At-most-once send/read-back recovery and cooldown

Interrupt the app after `arm`, before recording submission, immediately after the
native send boundary, and during a pending response. Exercise leading-byte drift,
one trailing-LF extraction artifact, `thread not loaded`, stale UI, and an
unusual-activity HTTP 403.

Expected:

- every affected turn remains collect-only; recovery never calls send;
- a correct late read-back can verify the existing message, but no other mismatch
  is repaired by resend;
- unusual activity records HTTP 403, optional request ID, and a fixed 1,800-second
  cooldown that blocks fresh prepares even after user-authorized abandonment;
- existing collection/artifact verification stays read-only during cooldown.

## E. Logical dispatch and response-rejected recovery

Use both an exact two-line control response and a complete response with explicit
truncation. Also try the control string with whitespace, trailing data, embedded
payload text, unverified outbound send, and incomplete generation.

Expected:

- only exact `result-marker + LF + [CODEX_PRO_DISPATCH_CHUNKED_REQUIRED_V1]`
  transitions;
- after verified submission and proven completed generation, the helper atomically
  sets predecessor `response_rejected` and prepares exactly one successor under
  the lock;
- uncertain sends cannot transition and a rejected predecessor cannot be armed or
  sent again;
- logical result completion remains separate from delivery/parent restoration and
  navigation retry cannot reopen content or permit sends.

## F. Chunked lossless transport

Collect at least five chunks totaling at least 50,000 UTF-8 bytes. Restart between
chunks and test an interrupted spool write. Include arbitrary Markdown containing
result/chunk markers, quotes, backslashes, CRLF input, a final chunk, and a
truncated later chunk requiring retransmission from the last accepted boundary.

Expected:

- every child turn is separately prepared, armed, sent at most once, read back,
  and collected with its own exact integrity evidence;
- a chunk is exactly four LF lines with strict canonical JSON body containing only
  `payload`; protocol-looking Markdown is data;
- complete serialized assistant size (including JSON escapes) respects the limit;
- decoded canonical-LF payload hashes/chains are correct and final reassembly is
  byte concatenation with no inserted separator;
- duplicate, altered, gap, replay, chain mismatch, malformed frame, missing
  footer, wrong final count, truncated prefix, orphan spool, and corrupt journal
  all fail closed;
- spool files are private, recoverable from exact journals, materialized only to an
  exclusive private output, and removed only after exact parent restoration.

## G. Explicit Git artifact transport

Use a disposable repository with private and public contract fixtures. Before the
worker receives write authority, validate canonical remote/repository ID/visibility,
prepared base/path/branch, every protected ref, explicit write confirmation, and
public retention acknowledgement. Exercise a readable canonical manifest and
artifact discovery with chat evidence unavailable.

Expected:

- no artifact mode is selected automatically; a read-only assignment never gains
  Git authority;
- parent verifier uses a private bare repo and exact remote objects, never a
  checkout or worker-supplied content;
- exactly one parent at prepared base, one added `100644` UTF-8 Markdown path,
  exact commit message/path/blob hash/size, branch stability, and protected refs
  are proven;
- reject wrong repository/URL/base/parent/commit message, branch movement, merge,
  added extra/modified/deleted/renamed paths, symlink/submodule/executable,
  BOM/CR/NUL/binary/missing LF, size/hash mismatch, base rewrite, artifact merge,
  and disallowed protected-ref movement;
- base fast-forward that retains prepared base and excludes artifact commit is
  recorded as `base_advanced`, not treated as an implicit integration write;
- no PR, merge, tag, release, deployment, branch deletion, or implicit write is
  performed.

## H. Schema-v1 migration and privacy repair

Prepare fixtures for every legacy root status: `prepared`, `armed`, `submitted`,
`pending`, `indeterminate`, `ambiguous`, `abandoned`, `failed`, and `complete`.
Include legacy response-only collection fields, raw diagnostic bodies, and a
legacy unusual-activity cooldown.

Expected:

- each unresolved state maps explicitly under the lock before mutation;
- response-only collection becomes audit data, not v2 completion;
- completed v1 receipt remains byte-for-byte immutable and reports historical
  `marker-only` / `unverifiable` completion;
- cooldown survives without resend/migration bypass;
- `doctor` redacts raw diagnostics, never response/result bodies into receipts,
  and corrupt receipts report structured unhealthy output.

## I. Parent restoration and foreground behavior

While another app is frontmost, run a multi-minute dispatch and a completed
chunked result. Simulate failed navigation then retry it.

Expected:

- submit/wait avoids interruption when focus state allows; collection can briefly
  foreground ChatGPT only under the documented limitation;
- exact parent ID is restored after completion and failures;
- retrying restoration changes delivery state only, never immutable result or send
  authority; chunk cleanup remains blocked until restoration is recorded;
- clipboard is unchanged.

## J. Release gate

The candidate is not ready to be called stable until A–I pass on the exact
candidate commit, the full repository suite and installer/skill checks pass, and
a redacted receipt names the actual host/adapter contract. Any missing native
field, unaccepted omission normalization, duplicate send, marker-prefix
completion, wrong-thread/message association, false item finality, incorrect
chunk reassembly, artifact verification gap, protected-ref drift, private-body
leak, cleanup defect, or parent-restoration failure is a blocker.
