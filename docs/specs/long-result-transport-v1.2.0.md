# Codex Pro Dispatch v1.2.0 — Lean Long-Result Continuation

## Problem and decision

The native ChatGPT conversation reader can truncate a long assistant message near 20,000 characters. Because the existing result marker appears at the beginning, a truncated prefix can still contain a valid marker and be mistaken for the complete result.

v1.2.0 makes one narrow reliability change:

- Every newly prepared response must end with an exact per-assignment end marker.
- A response explicitly reported by the native reader as `truncated: true` is always rejected.
- A response without the required final marker is always rejected.
- Every prompt asks the worker to aim below 10,000 UTF-8 bytes; this is guidance,
  not an acceptance gate.
- Long read-only work is returned through one explicit continuation protocol.

Implementation starts from the clean v1.1.0 code on `origin/main`. The current uncommitted v1.2 transport experiment is research only and is not the implementation baseline.

The native reader may omit the `truncated` field for normal results. v1.2.0 does not interpret omission as an undocumented `false`. Instead, completion requires the exact final marker, while an explicit `truncated: true` is always rejected. The 10,000-byte target reduces the chance of reaching the native reader's truncation boundary without turning a small model overshoot into a retry.

The existing v1.1.0 workflow remains intact: one configured Pro worker, one unresolved assignment at a time, durable arming before each send, exact user-message read-back, no automatic resend after uncertainty, stable thread identity, and exact parent-task restoration.

Each continuation is a new prepared user message with a new assignment ID. It receives its own durable arm and at most one native send attempt.

## What already exists

v1.1.0 already provides one-assignment `prepare`, durable `arm`, exact
read-back `submitted`, marker-based `complete`, and `continuation_of`. It also
provides the private temporary directory convention, one receipt per native
message, cooldown/ambiguity handling, and exact parent restoration. v1.2.0
reuses those seams; it does not create a second transport or state machine.

## NOT in scope

v1.2.0 does not add:

- Git artifacts or file transport
- GitHub connectors or repository-write permissions
- Repository verification
- Receipt schema migration
- Multiple result modes
- A generic transport abstraction
- Public retention policy
- Durable chunk journals or spools
- Elaborate crash replay
- Automatic retries or recovery loops
- Browser, Accessibility, AppleScript, CDP, clipboard, or UI automation
- Release, deployment, tagging, or publishing work

The supported transport remains the official combined ChatGPT and Codex desktop app on macOS. The long-result path is read-only and does not authorize external mutations.

## Exact protocol

### Shared size guidance and marker rules

Every v1.2.0 prepared prompt instructs the worker to:

1. Aim to keep the complete assistant message below 10,000 UTF-8 bytes.
2. Target no more than 6,000 characters of body text.
3. Begin at byte zero with the supplied result marker and end with the supplied
   exact end marker.
4. Emit only the form selected by the conditional wrapper below.

The parent does not reject a complete response solely for exceeding that
guideline because the model cannot count bytes precisely.

Every accepted native item must come from the configured worker conversation,
have a stable assistant item ID and completed enclosing turn, and not report
`truncated: true`.

### Conditional prompt wrapper

`wrap_prompt` reuses the existing prompt body and `continuation_of`; it adds no
mode field. If the normalized body starts at byte zero with the exact first
line

```text
[CODEX_PRO_DISPATCH_CONTINUE root_assignment_id=<root-assignment-id> next_index=<index>]
```

the wrapper requires **only** the chunk form for that root and index, including
the chunk header, `final` rule, and footer. It must not also tell the worker to
return a short result or continuation-required control response. For every
other body (the initial assignment), it requires **only** a nonempty short
result or the exact no-body continuation-required control response; it must
not advertise the chunk form. The skill creates the exact continuation body
below and prepares it with the existing `--continuation-of` relationship.

### Raw response grammar

The parser reads raw response bytes before UTF-8 decoding: they must be valid
UTF-8 and contain no CR byte. `LF` below is byte
`0x0a`. There is no BOM, leading blank line, newline conversion, normalization,
or stripping: the result marker starts at byte zero and the end marker is the
literal final byte sequence (so no LF, blank line, or other byte follows it).

For current assignment `a`, the exact ASCII structural lines are:

```text
R(a) = [CODEX_PRO_DISPATCH_RESULT assignment_id=a]
E(a) = [CODEX_PRO_DISPATCH_END assignment_id=a]
C(r) = [CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED root_assignment_id=r]
H(r,i,f) = [CODEX_PRO_DISPATCH_CHUNK root_assignment_id=r index=i final=f]
```

`a` and `r` use the existing assignment-ID grammar.

The accepted byte forms are:

```text
short   = R(a) LF B LF E(a)          (B is nonempty)
control = R(a) LF C(a) LF E(a)       (and nothing else)
chunk   = R(a) LF H(r,i,f) LF B LF E(a)
```

`B` is arbitrary valid UTF-8 bytes containing no CR and may contain leading or
trailing LFs. For short and chunk forms, the parser returns it by removing only
`R(a) + LF` and `LF + E(a)`; for a chunk it additionally removes
`H(r,i,f) + LF`. Control returns an empty payload. It never searches inside
`B`. Thus marker-looking result, control, chunk, and end-marker lines in `B`
are preserved byte for byte. A chunk body is nonempty unless `f=1` and a prior
accepted chunk already supplied nonempty body bytes.

### Normal short result

A complete short result is:

```text
[RESULT_MARKER]
<complete deliverable>
[CODEX_PRO_DISPATCH_END assignment_id=<assignment-id>]
```

The parent returns `B` through the normal v1.1.0 completion path.

### Continuation-required control response

When the complete deliverable will not fit safely, the initial worker response is exactly:

```text
[RESULT_MARKER]
[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED root_assignment_id=<root-assignment-id>]
[CODEX_PRO_DISPATCH_END assignment_id=<root-assignment-id>]
```

It contains no deliverable text.

After validating this response, the parent completes that individual assignment and deliberately prepares the first continuation. Preparation alone does not arm or send it.

### Continuation user message

Every continuation uses the same deterministic message body:

```text
[CODEX_PRO_DISPATCH_CONTINUE root_assignment_id=<root-assignment-id> next_index=<index>]

Return only chunk <index> of the same deliverable.
Continue from the last accepted boundary without repeating or summarizing accepted text.
Use the required chunk envelope.
Aim to keep the entire response below 10,000 UTF-8 bytes.
Set final=1 only when this chunk completes the deliverable.
Otherwise set final=0.
```

The normal v1.1.0 prompt wrapper adds the new assignment marker and completion instructions. The continuation is then armed and sent once as a new user message.

### Chunk response

Each chunk response is:

```text
[RESULT_MARKER]
[CODEX_PRO_DISPATCH_CHUNK root_assignment_id=<root-assignment-id> index=<index> final=<0-or-1>]
<chunk body>
[CODEX_PRO_DISPATCH_END assignment_id=<current-assignment-id>]
```

The parent accepts it only when:

- The root assignment ID matches.
- The first index is `1` and each later index equals the next expected index.
- `index` is canonical decimal `1` through `16` (no sign or leading zero), and
  `final` is exactly `0` or `1`.
- The body satisfies the raw grammar's empty-final rule.
- All shared identity, completion, truncation, and marker checks pass.

A logical result may contain at most 16 chunks. Reaching the limit with
`final=0` stops collection with an incomplete-result error. This single bound
prevents an uncooperative worker from creating an endless continuation chain.

### Parent-side assembly

`SKILL.md` orchestration, not the core or CLI, keeps the accepted chunk index
in task context and owns one mode-`0600` assembly file inside the private
mode-`0700` temporary directory that the v1.1.0 skill already creates. It
safely parses `complete` JSON, uses its existing `payload` as the body, and
appends to that file. The file is transient scratch space, not a receipt,
spool, journal, or recovery store.

For each valid chunk it:

1. Completes the individually validated native assignment and obtains its
   existing `payload` body.
2. Appends that body exactly as received, without normalization or an inserted
   separator, then flushes and fsyncs the assembly file.
3. Only after that write succeeds, advances the in-task accepted index and
   treats the chunk as part of the logical result.

When `final=0`, the parent prepares the next continuation and runs the normal v1.1.0 arm-and-send sequence once. This is forward progress to a new chunk, not a retry of a prior message.

When `final=1`, the exact assembly-file contents are returned to the original
parent task. The file is deleted with the existing private temporary directory
only after parent restoration succeeds or the normal cleanup path reports its
failure.

The data flow stays linear:

```text
native completed response
  -> identity + truncation metadata check
  -> exact UTF-8 size + envelope validation
  -> complete this assignment
  -> append + fsync body to the private temp file
  -> advance accepted index
       | final=0 and index<16 -> prepare/arm/send one new assignment
       ` final=1             -> restore parent with assembled result
```

## Minimal implementation shape

Do not add a new receipt schema or durable chunk store.

Existing v1.1.0 receipts remain one receipt per native user message. Existing fields already preserve assignment identity, worker identity, parent identity, `continuation_of`, send count, prompt hashes, result hashes, status, and no-resend state.

New transient parent state is limited to:

```text
root_assignment_id
accepted_chunk_index
assembly_file_path
last_accepted_assignment_id
recovery_used_for_index
```

These values exist only in the parent task while collection is running. The
byte limit, markers, and 16-chunk ceiling are constants; there is no persisted
chunk progress, journal, or replay state. `recovery_used_for_index` is empty
until an operator authorizes the one replacement for a rejected native chunk at
that expected index; it clears only after that replacement is accepted and the
index advances.

Do not add a new command. The parent writes the deterministic continuation message to a private prompt file and reuses the existing command:

```text
pro-dispatch prepare \
  --parent-task-id <parent-task-id> \
  --continuation-of <last-accepted-assignment-id> \
  --prompt-file <continuation-prompt-file> \
  --native-controls-confirmed
```

Extend `complete` with paired optional `--expected-root-assignment-id` and
`--expected-chunk-index` arguments. Supplying exactly one is an error. With
neither argument, completion accepts a short result or the exact control form;
with both, it accepts only a matching chunk. The pair chooses the grammar
before interpreting the body. In no-pair mode, a first body line beginning
`[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED ` selects control grammar and must
be the exact `C(a)` form; a wrong root fails with `control-root-mismatch` and
any other deviation fails. A first body line beginning
`[CODEX_PRO_DISPATCH_CHUNK ` is rejected as `chunk-arguments-required`.
Otherwise the remainder is short body and is never scanned. In paired mode,
the first body line must be the matching `H(r,i,f)` header; short, control, and
malformed chunk forms are rejected. This reserves only that first body-line
position: a literal control or chunk example after any body byte (including an
initial LF) remains opaque body. Validation happens before the assignment
becomes complete.

The JSON result retains the existing `payload` field as `B`: the complete
short body, the chunk body, or `""` for control. It adds only `result_kind`
(`short`, `continuation_required`, or `chunk`) and, for a chunk, `chunk_index`
and `final`. There is no `chunk_body` field.

Newly prepared receipts add one `result_protocol: "bounded-footer-v1"`
discriminator under the existing schema version. There is no schema bump,
backfill, or migration. A terminal legacy receipt remains readable and
immutable. For an active receipt without this discriminator, v1.2 permits only
`status`, `recover`, or explicit `abandon`; it rejects `arm`, `submitted`,
`pending`, `complete`, and continuation progression before any send with a
clear `legacy-active-assignment` error. The operator must finish it with v1.1.0
before upgrading or abandon it. v1.2 never accepts the old leading-marker-only
form.

## Failure handling and one replacement

Reject a response when it is truncated, incomplete, malformed, from the wrong
worker, for the wrong assignment, or has the wrong root ID or chunk index. The
10,000-byte target is generation guidance, not an acceptance gate.

Rejected text is not appended, the accepted index does not advance, and the
failed user message is never resent. There is no automatic replacement message
or recovery loop.

For one rejected native chunk response at expected index `i`, the operator may
authorize exactly one replacement if `recovery_used_for_index != i`:

1. Use the existing ambiguity/abandon path for the failed active assignment.
2. Set the in-task `recovery_used_for_index` to `i` before preparing anything.
3. Prepare a new assignment with a new ID, `--continuation-of
   <last-accepted-assignment-id>`, and the same `next_index=i` continuation
   body; then arm and send it once through the normal sequence.

The replacement is forward progress by a new user message, never a resend. If
it fails, or the guard is already `i`, stop and report the logical result as
incomplete. Do not prepare another replacement for that index. On success, its
body is appended, the index advances, and the guard clears.

If opening, writing, flushing, fsyncing, or permission-checking the assembly
file fails (including a partial write), do not advance the accepted index,
restore a logical result, or reuse that file—even for `final=1`. The
individually completed receipt, if completion already succeeded, remains
immutable; `last_accepted_assignment_id` stays at the prior accepted boundary.
Discard the partial assembly file and all transient logical-result state. This
local failure does not consume or permit the native-chunk replacement allowance:
there is no in-protocol next send or reconstruction. Only a separately
user-authorized fresh logical dispatch from the beginning may proceed later.

Because assembly is intentionally transient, an app or parent-process restart
also stops collection. v1.2.0 adds no crash replay; a later attempt requires a
separately user-authorized fresh logical dispatch from the beginning. An unseen
tail from a truncated initial response cannot be reconstructed.

## Test coverage map

```text
CODE PATHS                                      USER FLOWS
[+] core.wrap_prompt                            [+] Initial dispatch
    | initial -> short/control only                 | short -> return payload
    ` CONTINUE -> matching chunk only               ` control -> prepare chunk 1
[+] cli.complete                               [+] Chunk collection
    | exact byte read                              | valid next index -> append
    | paired expected root/index                   | invalid chunk -> one new replacement
    ` JSON keeps payload                           ` final -> restore exact parent
[+] core.complete_assignment                   [+] Fail-closed recovery
    | bounded raw-envelope parse                    | active legacy receipt -> stop
    | legacy-active guard                           | assembly/restart failure -> stop
    | idempotent identical replay                   ` chunk 16 nonfinal -> stop
    ` immutable conflict rejection
```

Every branch above maps to a focused test below. Prompt-wrapper changes also
require the native desktop acceptance run; no separate evaluation framework is
introduced.

## Failure modes

| Path | Realistic failure | Test | Handling and user result |
| --- | --- | --- | --- |
| Native collection | Host returns a truncated prefix | Planned | Reject; clear incomplete-result error |
| Raw parser | Wrong footer, encoding, framing, root, or index | Planned | Reject before receipt completion |
| Legacy state | Upgrade finds an active v1.1 receipt | Planned | Read/recover/abandon only; clear legacy error |
| Continuation | Worker repeats, skips, or never finishes | Planned | Reject or stop at chunk 16; never resend |
| Assembly | Private-file write or fsync fails | Planned | Discard transient state; no logical result or next send |
| App lifecycle | Parent or app restarts mid-result | Native acceptance | Stop; require a separately authorized fresh dispatch |

There are no silent untested failure paths in the plan.

## Execution strategy

Sequential implementation, no parallelization opportunity. The prompt,
parser, receipt transition, CLI contract, skill orchestration, and their tests
share one protocol boundary; parallel worktrees would create more merge and
contract-drift risk than time savings.

## Implementation Tasks

- [ ] **T1 (P1, human: ~1h / CC: ~15min)** — Protocol — Add conditional bounded response framing.
  - Surfaced by: Architecture review — initial and continuation prompts require mutually exclusive response forms.
  - Files: `src/codex_pro_dispatch/core.py`, `tests/test_core.py`
  - Verify: initial wrapper advertises short/control only; exact `CONTINUE` wrapper advertises chunk only.
- [ ] **T2 (P1, human: ~2h / CC: ~25min)** — Completion — Validate exact footer, bounds, modes, and legacy receipts.
  - Surfaced by: Code-quality review — v1.1 normalizes/strips responses and cannot distinguish footer-bound receipts.
  - Files: `src/codex_pro_dispatch/core.py`, `src/codex_pro_dispatch/cli.py`, `tests/test_core.py`, `tests/test_cli.py`
  - Verify: `python3 -m unittest tests.test_core tests.test_cli`
- [ ] **T3 (P1, human: ~2h / CC: ~25min)** — Skill — Orchestrate bounded chunks with transient assembly.
  - Surfaced by: Architecture/performance review — exact multi-call assembly needs private scratch storage, one replacement rule, and a finite bound.
  - Files: `skills/codex-pro-dispatch/SKILL.md`, `skills/codex-pro-dispatch/references/native-protocol.md`, `tests/test_skill_contract.py`
  - Verify: `python3 -m unittest tests.test_skill_contract`
- [ ] **T4 (P1, human: ~2h / CC: ~30min)** — Verification — Cover every branch and run exact long-result acceptance.
  - Surfaced by: Test review — byte boundaries, opaque marker examples, sequencing, idempotency, local failures, and live >30K reconstruction need explicit coverage.
  - Files: `tests/test_core.py`, `tests/test_cli.py`, `tests/test_skill_contract.py`
  - Verify: `python3 -m unittest discover -s tests`, then the native desktop acceptance described below.

## Acceptance tests

1. A complete result above the 10,000-byte guideline still completes; explicit
   native `truncated: true`, a missing footer, CR/CRLF, a leading
   byte before the marker, or bytes after the footer fail closed.
2. Raw slicing preserves leading/trailing body LFs and marker-looking result,
   control, chunk, and footer examples byte for byte; only the reserved first
   body-line structural position is interpreted.
3. The exact control form has no body, requires `root_assignment_id` equal to
   the current root assignment, and prepares but never arms or sends its first
   continuation. A control-root mismatch or extra control bytes is rejected.
4. The expected-root/index arguments must occur as a pair; no-pair chunk
   headers, paired short/control forms, wrong roots, noncanonical or skipped
   indices, invalid final flags, and wrong assignment footers are rejected.
5. Nonfinal chunks are nonempty. An empty final chunk is accepted only after
   earlier nonempty chunk data; it never completes an otherwise empty result.
6. Two and multi-chunk results append in order without an inserted separator.
   The 16th `final=0` chunk stops before preparing or sending chunk 17.
7. Repeating the same completed response is idempotent and does not append,
   advance, prepare, or send again; a changed response, parsed kind, root,
   index, or final flag is rejected by receipt immutability.
8. A rejected native chunk gets only one operator-authorized replacement with a
   new ID, the same expected index, and `continuation_of` the last accepted
   assignment; it is never resent, and a failed replacement stops collection.
9. Skill-contract coverage requires safe `complete` JSON parsing and a forced
   assembly open/write/flush/fsync/permission failure path, including a partial
   write: it discards all transient logical-result state, produces no logical
   result or in-protocol next send, and requires a separate user-authorized
   fresh logical dispatch before any later attempt.
10. New receipts carry `result_protocol`; active legacy receipts are blocked as
    specified, while terminal legacy receipts remain immutable and readable.
11. Existing v1.1.0 identity, read-back, cooldown, ambiguity, no-resend, and
    parent-restoration coverage remains green. Skill-contract tests require an
    initial wrapper with short/control-only instructions and an exact
    `CONTINUE` wrapper with chunk-only instructions, plus the footer and
    assembly/recovery rules. A native acceptance run rejects truncation,
    reconstructs a result over 30,000 characters from chunks guided below 10,000 bytes
    with exact bytes and parent restoration, and exercises multi-chunk
    write-failure stop-and-cleanup.

## Implementation cut list

Besides this specification, implementation changes exactly these seven files:

- `src/codex_pro_dispatch/core.py` — conditional initial/continuation prompt
  wrapping, footer-aware raw parsing, result kinds, legacy-active guard, and
  bounds for one assignment at a time.
- `src/codex_pro_dispatch/cli.py` — paired chunk expectations and the minimal
  result JSON additions while retaining `payload`.
- `skills/codex-pro-dispatch/SKILL.md` — exact prompt, collection, safe
  completion-JSON parsing, sequential continuation, transient assembly,
  one operator-authorized native-chunk replacement, failure-stop, cleanup, and
  parent-restoration instructions.
- `skills/codex-pro-dispatch/references/native-protocol.md` — native read and
  raw LF envelope and conditional-response contract.
- `tests/test_core.py` — wrapper, parser, state, sequencing, and idempotency
  boundaries.
- `tests/test_cli.py` — paired-flag and payload/result-kind JSON coverage.
- `tests/test_skill_contract.py` — exact conditional prompt, one-replacement,
  fail-stop assembly, and native workflow coverage.

No new modules, commands, schemas, migrations, spools, artifacts, generic
transports, or automatic retry machinery—and no changes outside these seven
implementation files—are in this cut.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | Not needed for this bounded reliability fix |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | CLEAR | Claude Opus 5 xhigh: green, no required cuts |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 12 issues found and folded; 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | No UI change |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | Existing CLI/skill surface retained |

**CROSS-MODEL:** Both reviews greenlight the seven-file implementation and reject artifact transport, durable chunk state, generic abstractions, and automatic retries.

**VERDICT:** ENG + OUTSIDE VOICE CLEARED — ready to implement.

NO UNRESOLVED DECISIONS
