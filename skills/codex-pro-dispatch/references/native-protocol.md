# Official-app native conversation protocol

Read this reference for first-time setup, collection, or recovery.

## Assumptions

- Chat and Codex are separate task surfaces inside the official combined desktop app.
- The host exposes native controls that can resolve conversation IDs, submit a message, observe conversation metadata, open a conversation by ID, read the latest completed assistant response, and return to a Codex task by ID.
- Exact tool names may vary by Codex build. Use the available native controls semantically. Do not replace them with shell UI automation.

Do not treat these assumptions as satisfied automatically. Complete the six-capability host preflight in `SKILL.md` on every invocation. If any capability is missing, stop before configuration or assignment preparation.

## Worker setup

1. The user opens one dedicated Chat conversation.
2. The user visibly selects Pro.
3. Resolve and save its stable conversation ID.
4. Treat the model as `user-confirmed-pro`, not machine verified.

A worker title is a label only. Conversation identity comes from the stable ID.

## Submission sequence

1. Capture the parent Codex task ID before leaving it.
2. Prepare the assignment with `pro-dispatch prepare`.
3. Resolve the configured worker by ID.
4. Immediately before sending, run `pro-dispatch arm '<assignment-id>'`. Do not send unless it succeeds.
5. Make at most one native send attempt for `wrapped_prompt`. After `arm`, the assignment is permanently collect-only if the app crashes or the send outcome is uncertain. An interruption before transport can therefore result in zero sends; this is deliberate fail-closed behavior.
6. After confirmation, use native controls to read back the exact submitted user message from the worker and save it as UTF-8 without reconstructing or editing it.
7. Run `pro-dispatch submitted '<assignment-id>' --sent-prompt-file '<native-read-back-file>'` so the helper compares the read-back bytes with the prepared `wrapped_prompt` hash.
8. If the hash differs, keep the helper's `indeterminate` collect-only state and never resend. If confirmation itself is indeterminate, record `indeterminate` and switch to collection-only recovery.

The helper intentionally blocks a second unresolved assignment.

## Waiting

Use a bounded poll of native conversation metadata. An update signal is only permission to collect. It is not proof that the response belongs to the assignment.

Do not reopen the worker repeatedly during generation. Do not retry a message because the UI is slow.

Native send acknowledgement may become visible through read-back after a delay. If the exact user message is temporarily absent, record `indeterminate`, wait, and recover without resending. Absence in an immediate read is not proof that the send failed.

If the native send reports `systemError`, first inspect any error payload exposed by that control. If no payload is exposed, inspect the official app log read-only around that one send for the HTTP status, response detail, and request ID. Do not emit unrelated log content. An unusual-activity HTTP 403 must be recorded with `pro-dispatch unusual-activity`, not collapsed into a generic transport error.

## Collection

1. Open the worker by exact conversation ID.
2. Wait until the loaded conversation ID equals the configured worker ID.
3. If outbound verification is incomplete, locate the existing user message by assignment marker and run `pro-dispatch submitted --sent-prompt-file` on its exact native read-back. This is verification, not a new send. If the first temporary file added exactly one trailing newline and the receipt reports `readback_correction_allowed: true`, re-extract the same native message without that artifact and verify it once more. Never normalize or retry other mismatches.
4. Read the newest completed assistant response.
5. Validate the first nonempty line against the assignment's exact result marker.
6. Reject stale or mismatched markers.
7. Restore the exact parent Codex task ID in a `finally`-style cleanup path.

If the native control reports `thread not loaded`, explicitly open the worker by ID and wait. Do not resend.

## Visible fallback

Current official-app collection may foreground ChatGPT and move the pointer. This is an accepted v1 limitation.

Prefer this behavior:

1. Submit and wait in the background.
2. If the user is working in another app, defer collection.
3. When ChatGPT/Codex is frontmost again, open the worker, collect, and restore the parent task.

The clipboard must remain unchanged.

## Failure mapping

| Native result | Required action |
| --- | --- |
| Crash or interruption after `arm` | Recover the exact worker collect-only; never send that assignment again |
| Send confirmed and exact read-back available | `pro-dispatch submitted --sent-prompt-file '<native-read-back-file>'` |
| Send acknowledged but read-back temporarily absent | `pro-dispatch indeterminate`; wait and late-verify the existing message without resend |
| Read-back file equals expected prompt plus one trailing newline | Re-extract the same native message without the file artifact and verify again; never resend |
| Read-back differs by any other byte | Keep the helper's `indeterminate` state; do not retry verification or resend |
| Send may have happened | `pro-dispatch indeterminate`; never resend |
| Native diagnostics show an unusual-activity HTTP 403 | Preserve the exact response and request ID with `pro-dispatch unusual-activity`; report HTTP 403 explicitly, remain collect-only, and enforce the 30-minute cooldown before any fresh assignment |
| Worker unchanged | Keep waiting within the bounded timeout |
| Thread not loaded | Open exact worker ID and wait |
| Wrong thread loaded | Stop and reject collection |
| Result marker missing or mismatched | `pro-dispatch ambiguous`; never resend |
| Parent restoration fails | Preserve assignment state and report the exact parent task ID |

## v1.2 bounded-result overlay

The v1.1 setup, submission, waiting, collection, recovery, visible fallback,
and failure mapping above remain in force. For each newly prepared v1.2
response, the native read additionally preserves exact response bytes and
explicit native truncation metadata when supplied, selects a stable assistant
item associated with the verified user message, and confirms its enclosing turn
is completed. An explicit `truncated: true` is rejection evidence; omitted
metadata is not silently treated as false.

When the native reader explicitly reports truncation, preserve the exact bytes
and pass `--truncated` to `complete`; otherwise omit that flag.

The response is valid UTF-8, must contain no CR byte, begins at byte zero with
the result marker, and ends as literal final bytes:

```text
[CODEX_PRO_DISPATCH_END assignment_id=<current-assignment-id>]
```

Never normalize newlines, strip body text, or scan opaque body bytes for
marker-looking examples. Ten thousand UTF-8 bytes is a generation guideline,
not an acceptance gate. The initial wrapper permits only a nonempty short body
or the exact no-body continuation-required control form. For a normalized body
whose first byte starts the exact continuation header, the wrapper permits only
the matching chunk form:

```text
[CODEX_PRO_DISPATCH_CONTINUE root_assignment_id=<root-assignment-id> next_index=<index>]
```

Pass both `expected-root-assignment-id` and `expected-chunk-index` to complete
a chunk. The root must match, index starts at canonical 1 and advances exactly
through at most 16, and final is exactly 0 or 1. Nonfinal bodies are nonempty;
an empty final body is valid only after a previous accepted nonempty chunk. The
chunk response has the exact raw form:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<current-assignment-id>]
[CODEX_PRO_DISPATCH_CHUNK root_assignment_id=<root-assignment-id> index=<index> final=<0-or-1>]
<chunk body>
[CODEX_PRO_DISPATCH_END assignment_id=<current-assignment-id>]
```

The parent appends the returned payload byte-for-byte to one private mode-0600
assembly file, then flushes and fsyncs before advancing the accepted index. At
chunk 16 with final=0, stop before chunk 17.

Malformed, truncated, wrong-worker, wrong-assignment, wrong-root, wrong-index,
or incomplete output is rejected before receipt completion. A response above
the 10,000-byte guideline is still accepted when its envelope is intact. Do not
append rejected output, advance the logical index, or resend it. The operator may make
exactly one operator-authorized replacement for a rejected native chunk at an
expected index: abandon the failed active assignment, set the in-task guard,
then prepare a new assignment for the same index with `continuation_of` the last
accepted assignment and send it once. A failed replacement stops collection.

If assembly opening, writing, flushing, fsyncing, or permission verification
fails, including after a partial write, discard the partial file and all
transient logical state. Do not reconstruct, replay, prepare another
continuation, or return a logical result. An app or parent restart also stops
collection and requires separately authorized fresh dispatch from the beginning.

New receipts carry `result_protocol: "bounded-footer-v1"`. Active legacy
receipts without it permit status, recover, or explicit abandon only; terminal
legacy receipts remain readable and immutable.
