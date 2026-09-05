# Connectivity-only native Pro worker probe

Status: draft for user review. Not filed, quality-gated, or authorized for implementation by this document.
Verified against the local v1.2.0 candidate working tree on 2026-09-05.

## Context

A user asking to test pro-dispatch with a simple message currently cannot send
anything when the desktop host lacks full collection evidence. A fixed diagnostic
exchange can establish connectivity with a much narrower claim: a matching reply
was observed in the configured worker. It cannot establish completed generation,
lossless collection of arbitrary text, or machine-verified Pro selection.

Deliver one explicit `probe` workflow for that exchange. Preserve the strict
preflight and completion contract for inline, chunked, and artifact dispatch.

Success is observable: one native send attempt at most, a fresh token reply in
the matching worker and user turn, and a result labeled `connectivity_only`.
There is no latency baseline; live timing is measured and reported, not promised.

## Current state

| Component | Verified behavior | Required change |
| --- | --- | --- |
| `cli.py:75` / `build_parser` | Defines production commands; no diagnostic probe | Add a separate probe command family |
| `cli.py:530` / doctor branch | Checks local state and a caller assertion of full native capability | Surface active probes without promoting probe success into full native support |
| `core.py:308` / `state_lock` | One file lock protects local state transitions | Reuse it for probes and cross-workflow exclusion |
| `core.py:322` / `save_worker` | Refuses changes during unresolved production assignments | Also refuse changes during unresolved probes |
| `transport.py:1728` / `prepare_assignment_v2` | Checks production activity and cooldown under the lock | Also check active probes and their cooldowns |
| `collection.py:152` / adapter allowlist | Requires trusted message finality and outer integrity | No relaxation or new summary-based production adapter |
| Desktop build 7982 | Trims text and synthesizes completed turns | Use summaries only for the bounded diagnostic claim |

The investigation and source fingerprint are in
`docs/native-read-investigation-2026-09-05.md`. Increasing the native read budget
to 20,000 and enabling outputs did not supply the missing production metadata.
The existing eight collection tests passed during that investigation.

## Scope and product behavior

An explicit request for a simple test or smoke test may invoke the probe. An
ordinary production dispatch that fails preflight must not silently become one.
The first version requires an already configured, user-confirmed Pro worker.
Worker creation and relaxed worker setup are out of scope.

Before preparing, the skill establishes the parent task ID, resolves the configured
worker by exact ID, confirms native send/read/navigation tools are callable, and
reads the worker once. Require a ChatGPT worker with observed `idle` status.
This is a reduced diagnostic preflight; do not pass `--native-controls-confirmed`
or claim the production preflight succeeded.

Expected user-facing outcomes:

- Success: “Connectivity test passed: matching reply observed. Generation
  completion and full dispatch integrity were not verified.”
- Timeout: “No matching reply observed within 120 seconds. The probe remains
  collect-only; no message was resent.”
- Missing capability, busy state, or cooldown: state the exact blocker before
  sending. A native HTTP 403 retains the existing 30-minute cooldown behavior.
- Restoration failure: report it separately; the observation remains recorded.

## Fixed exchange

Generate 16 random bytes with `secrets.token_hex(16)`. Call the resulting 32
lowercase hexadecimal characters `nonce`; the helper-generated ID is
`probe-<nonce>`. Caller-selected IDs/nonces are not supported for new probes.

The complete ASCII prompt, without a terminal newline, is:

```text
Reply with exactly PRO_DISPATCH_PROBE_OK_<nonce> and no other text.
```

The complete expected ASCII reply, without a terminal newline, is:

```text
PRO_DISPATCH_PROBE_OK_<nonce>
```

No arbitrary prompt, attachments, result modes, continuation, Git writes, browser
transport, or model override is accepted. The nonce is a correlation identifier,
not a secret, authentication mechanism, or proof of selected model.

## CLI contract

These are proposed commands, not commands available in the current helper.

```text
pro-dispatch probe prepare --parent-task-id ID --native-probe-controls-confirmed --prompt-file PATH
pro-dispatch probe arm PROBE_ID
pro-dispatch probe observe PROBE_ID --native-read-file PATH
pro-dispatch probe indeterminate PROBE_ID --reason-file PATH
pro-dispatch probe unusual-activity PROBE_ID --reason-file PATH [--request-id ID]
pro-dispatch probe recover PROBE_ID
pro-dispatch probe status [PROBE_ID]
pro-dispatch probe parent-restored PROBE_ID --native-probe-controls-confirmed
pro-dispatch probe abandon PROBE_ID --reason-file PATH [--acknowledge-possible-send]
```

`prepare` writes the fixed prompt to a new regular file exclusively created with
mode 0600 inside a caller-created private 0700 temporary directory. Refuse an
existing path, a symlink, or a nonprivate directory. Return IDs, the path, hashes,
and status; never print prompt/reply/diagnostic bodies. An orphan prompt file after
a failed prepare grants no send authority. Write the prepared receipt last.

`arm` commits the permanent send-permit consumption before the caller invokes the
native send. Only a successful first arm response authorizes one attempt. Every
subsequent arm returns `probe_already_armed`; recovery never grants a new permit.

`observe` accepts the exact inner JSON returned by one native `read_thread`
operation, extracted without editing from its text content. It parses and matches
the raw summary itself; callers cannot submit just a reply or a `matched: true`
assertion. Input is bounded to 1 MiB and parsed with the strict JSON utilities.

`recover` and `status` return receipt metadata and `send_allowed: false` without
prompt bytes. The skill uses saved worker and parent IDs for collect-only work.

Return exit 0 for successful state operations and observations, including a valid
read with no match. A no-match response has `reply_observed: false`; exit 0 alone
is never a connectivity pass. Use existing exit categories: invalid input 2, busy
3, invalid transition/receipt 4, cooldown 6, invalid observation 7. Preserve the
existing production error codes. Probe-specific codes include `probe_busy`,
`probe_already_armed`, `probe_read_invalid`, and `probe_observation_conflict`.

All probe outputs include `kind: connectivity_probe` and
`verification_level: connectivity_only`. A successful observation also returns:

```json
{
  "ok": true,
  "kind": "connectivity_probe",
  "status": "reply_observed",
  "reply_observed": true,
  "verification_level": "connectivity_only",
  "generation_finality_verified": false,
  "production_collection_verified": false,
  "model_confirmation": "user-confirmed-pro",
  "send_allowed": false,
  "parent_restored": false
}
```

IDs and observation time accompany this projection. No `complete`, `completed`,
production delivery, artifact, or native-host-accepted claim is emitted.

## Observation matching

The skill calls `read_thread` with the saved worker ID, `turnLimit: 10`, and
`maxOutputCharsPerItem: 20000`. `includeOutputs` is unnecessary. Read-only recovery
may page at most five pages per invocation, newest first, to find the fixed user
prompt. It never sends to bring a probe back into the visible window.

The helper requires `schemaVersion: 1`, `thread.kind: chatgpt`, and a
`thread.id` exactly matching the receipt. Require valid turn/item shapes and
stable nonempty IDs. Reject unsupported schemas and malformed JSON, duplicate
keys, nonfinite values, or invalid UTF-8 without echoing the offending input.

Find exactly one turn containing exactly one `userMessage` whose sole text
content block equals the generated prompt byte-for-byte. Its ID must equal the
turn ID, as in the inspected ChatGPT projection. In that turn require exactly
one subsequent `agentMessage` whose entire returned text equals the expected
reply. Reject duplicated candidate turns, multiple user/assistant candidates,
nontext content in the candidate turn, changed accepted IDs, and truncation
explicitly true on a relevant block/item or envelope.

Do not trim, normalize newlines, perform substring matching, infer association
from timestamps, or accept the token from the preview, another turn, or the user
message itself. Ignore unrelated well-formed turns. Well-formed reads with no
matching prompt or reply return no-match and retain collect-only state. A worker
reported active returns no-match even if the token is visible; idle is an
operational guard, not evidence of message finality.

Missing truncation flags are permitted only for this diagnostic and recorded as
`omitted`, never normalized to false. Likewise retain missing outer integrity as
unknown. `turn.status` and `completedAt` are not generation evidence. Exact
equality applies to returned summary text; the report must not claim equality to
the original source bytes. This intentionally accommodates the lossy projection
for this fixed ASCII exchange only.

Repeat observation of the same user/assistant IDs and text is idempotent; changing
only observation time is allowed. Once accepted, a different matching message ID
is a conflict and cannot replace the recorded observation. A later no-match read
does not erase a prior observation. An ambiguous send can still be resolved to
reply-observed by reading the existing exchange.

## Receipt and lifecycle

Store probe receipts separately in `RuntimePaths.state_dir / probes / <id>.json`.
Add `RuntimePaths.probes_dir`. Use the shared state lock, private-directory/path
validation, and atomic JSON writing. The arm commit must flush/fsync the receipt
and containing directory before returning permission; failure returns no send
authority. Locking spans local state checks/writes only, never native tool waits.

Schema `probe_schema_version: 1`, `record_type: connectivity_probe` contains:

- Probe, worker, and parent IDs; user-confirmed model metadata.
- `status`: `prepared | armed | indeterminate | reply_observed | abandoned`.
- Creation/update/arm/observation timestamps; nullable matched native user and
  assistant IDs; SHA-256 of the fixed prompt and expected reply.
- `send_permit_consumed`: boolean, initially false and permanently true after
  arm. This measures consumed permission, not proven network send count.
- `parent_restored_at`: nullable timestamp; raw truncation observations as
  `true | false | omitted`; invariant false verification booleans above.
- Nullable diagnostic category/hash, request ID, and cooldown timestamps. No raw
  bodies, transcripts, authentication data, or arbitrary extra fields.

Validate schema, types, legal transitions, ID/filename consistency, immutable
fields, and private regular-file paths on every load. Invalid receipts block new
sends; they are not silently skipped. No migration of production receipts is
needed. Probe IDs must never resolve as production assignments.

```text
prepared --arm--> armed --observe match--> reply_observed
                     |                          |
                     +--uncertain--> indeterminate --observe match--+

prepared/armed/indeterminate --explicit abandon--> abandoned
```

The observation paths above both end at `reply_observed`; there is no resend
edge. `observe` before arm is rejected. `indeterminate` cannot reverse a recorded
observation. Parent restoration is orthogonal and idempotent for every state.
An unresolved probe includes prepared/armed/indeterminate states and a
reply-observed or abandoned probe awaiting parent restoration. Timeout alone
never releases the reservation. Abandon after arm requires the explicit
`--acknowledge-possible-send` flag and user authorization in the skill; before
arm the flag is unnecessary. Abandoned probes cannot rearm or accept new results.

## Shared exclusion, cooldown, and recovery

Probe prepare and arm both check, under the same lock, that no production
assignment or other probe is unresolved and no cooldown is active. Arm also
verifies that the configured worker still matches the prepared receipt.

Production prepare and every production arm (including chunk/recovery children)
must reject unresolved probes. Worker set/reset and non-force purge must respect
them too. Use a small probe registry reader to avoid circular imports: it must
not call production active/cooldown readers recursively. Keep each reader
lock-free internally; callers performing check-and-write already hold the lock.

Extend shared cooldown selection to take the latest unexpired cooldown from both
receipt families. A probe unusual-activity event sets a fixed 1,800-second
deadline on first record, preserves an available OpenAI request ID, and consumes
no new send permission. Re-recording cannot shorten or restart that deadline.
Abandon, observing a reply, or restoring the parent does not clear a cooldown.
Normal cleanup retains receipts carrying an active cooldown.

Expose active probe and probe cooldown metadata in `doctor` and top-level status
as additive fields. Existing full native capability flags retain their meaning.
Keep destructive force commands as explicit break-glass operations; extend their
existing warning/validation policy to probe recovery evidence. Never invoke them
automatically to get another test through. Enumerate validated files when purging;
do not introduce recursive deletion.

The skill waits up to 120 seconds from the send attempt, polling at roughly
10-second intervals after each completed read. The deadline stops scheduling new
reads; an in-flight host call can exceed it. Provide a user update by 60 seconds.
On timeout, restoration failure, tool error, or restart, preserve the receipt and
use only read-only recovery. No background automation is installed.

After any attempted navigation, restore the exact saved parent, then record that
observation. Always clean transient prompt/read/reason files in a finally block;
failed restoration must not leave transcripts behind. Receipts contain enough
identity/hash information to recover without retaining those files.

## Implementation order

1. **State and coordination:** add probe schema, transitions, validation, and
   shared exclusion/cooldown checks. This must land before any send workflow so
   concurrency cannot bypass production reservations.
2. **CLI and matcher:** add commands, exclusive prompt-file creation, exact
   summary matching, and honest projections. Reuse standard-library facilities.
3. **Skill and docs:** document the reduced diagnostic preflight and explicit
   entry point, bounded wait/recovery, and result wording. Preserve production
   preflight and acceptance criteria.
4. **Validation:** complete automated checks, then one live fixed-token test on
   the configured worker with no retries after arm.

Integrate against the v1.2 candidate work already present, including uncommitted
`transport.py`. A clean HEAD checkout alone does not include all inspected code.
Do not stash, revert, overwrite, or base implementation on stale v1.1 code.

## Acceptance criteria

1. An explicitly requested smoke test can pass reduced preflight on the inspected
   summary-only host while production preflight remains unsupported.
2. Only the fixed ASCII exchange is accepted; arbitrary prompt/model/result-mode
   arguments fail before any receipt or native send.
3. Concurrent probe/probe and probe/production prepares produce exactly one
   reservation; at most one arm response grants permission per probe.
4. Crash after successful arm, failed send, timeout, or repeated CLI calls never
   permit another send of that probe. Read-only recovery can observe its reply.
5. Wrong worker, stale nonce, user-token echo, substring reply, ambiguous messages,
   altered accepted IDs, and explicit relevant truncation never produce a pass.
6. Missing finality/outer fields can yield only `connectivity_only` observation;
   all production collection rejection tests continue to pass unchanged.
7. Observation is idempotent by message identity/text; output contains both
   invariant false verification booleans and the saved model-confirmation type.
8. HTTP 403 blocks new probes and production assignments for the shared fixed
   cooldown, including after abandonment and parent restoration.
9. Worker replacement, non-force reset/purge, production arms, invalid probe
   receipts, and pending restoration respect the shared reservation.
10. Prompt/read/reason bodies are absent from receipts, CLI JSON/errors, and
    normal terminal logs; private file modes and cleanup are verified on success,
    timeout, malformed reads, and failed restoration.
11. One live test records a matching reply and exact parent restoration. Report
    this as a probe pass, never v1.2 native acceptance or production readiness.

## Testing plan

| Layer | Cases | Minimum new cases |
| --- | --- | --- |
| Unit: receipt lifecycle | Arm once, crash/reload, invalid schemas/paths, idempotence/conflicts, abandonment, restoration, cooldown | 12 |
| Unit: summary matcher | Positive fixture, stale/wrong-worker/echo/substring/duplicate/nontext/truncated/active/missing fields/Unicode/invalid JSON | 14 |
| CLI integration | Complete local lifecycle, body-safe output, argument rejection, private/existing files, recovery, status, exit behavior | 8 |
| Cross-workflow integration | Two-process prepare/arm races, production children, worker changes, purge, shared cooldown and corrupt probes | 8 |
| Live host | One fixed-token send, observation, parent restoration and transient cleanup | 1 |

Use sanitized synthetic fixtures modeled on the observed native schema and
isolated `CODEX_PRO_DISPATCH_HOME` directories. Inject clocks for cooldown tests;
use separate processes for file-lock races. Live tests are explicit and excluded
from normal CI. Run the full existing unit suite, Python syntax checks, shell
installer checks, and skill validation once the implementation is complete.

## Rollback plan

Disable new probe preparation first. Resolve or explicitly abandon any existing
probe, restore its parent, and wait out active cooldowns before running an older
binary that cannot see probe receipts. Retain private receipts for audit.
Do not downgrade with an unresolved probe or active probe cooldown: older code
would not enforce its reservation. Revert only this feature's patch after that
drain; preserve the existing v1.2 work and production receipts.

## Effort estimate

Estimates, not measured runtimes: state/coordination 3–4 human engineer hours;
CLI/matcher 2–3; automated tests 3–4; skill/docs and live validation 1–2.
Total 9–13 human engineer hours; an agent-assisted implementation/review cycle
is approximately 2–4 hours plus host response time and any review revisions.

## Files reference

| Repository path | Change |
| --- | --- |
| `src/codex_pro_dispatch/probe.py` (new) | Schema, lock-free registry readers, transitions, summary matcher, projections |
| `src/codex_pro_dispatch/core.py` | Probe path, worker guards, shared durability primitives as needed |
| `src/codex_pro_dispatch/transport.py` | Production prepare/arm guards, combined cooldown, purge integration |
| `src/codex_pro_dispatch/cli.py` | Probe commands; additive status/doctor metadata |
| `tests/test_probe.py` (new) | Lifecycle and observation cases |
| `tests/test_probe_integration.py` (new) | Process races and production/worker/cooldown interaction |
| `tests/test_cli.py` | Probe CLI, privacy, file, and exit-code tests |
| `skills/codex-pro-dispatch/SKILL.md` | Explicit diagnostic branch before production preflight |
| `skills/codex-pro-dispatch/references/native-protocol.md` | Probe transport, waiting and recovery contract |
| `README.md`, `docs/compatibility.md`, `docs/acceptance.md`, `SECURITY.md` | Diagnostic use, limitations, distinct live gate, receipt retention |

## Out of scope

- A full native collection adapter, app modification, browser/clipboard fallback.
- Weakening existing collection evidence or migrating production receipt schemas.
- Automated Pro selection, new-worker setup, or alternate model/provider routing.
- Arbitrary prompt dispatch, artifact writes, chunk transport, background monitoring.
- Release/version changes, issue comments, PR creation, deployment, or unrelated
  changes to the in-progress v1.2 feature.

## Related

- `docs/native-read-investigation-2026-09-05.md`: observed host behavior and evidence.
- `docs/specs/long-result-transport-v1.2.0.md`: in-progress production transport.
- Duplicate search on 2026-09-05 for `probe smoke connectivity` returned no open matches.
