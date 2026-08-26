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
