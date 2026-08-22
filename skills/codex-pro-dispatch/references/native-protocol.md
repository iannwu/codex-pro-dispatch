# Official-app native conversation protocol

Read this reference for first-time setup, collection, or recovery.

## Assumptions

- Chat and Codex are separate task surfaces inside the official combined desktop app.
- The host exposes native controls that can resolve conversation IDs, submit a message, observe conversation metadata, open a conversation by ID, read the latest completed assistant response, and return to a Codex task by ID.
- Exact tool names may vary by Codex build. Use the available native controls semantically. Do not replace them with shell UI automation.

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
4. Submit `wrapped_prompt` once.
5. Record `submitted` only after the native control confirms submission.
6. If confirmation is indeterminate, record `indeterminate` and switch to collection-only recovery.

The helper intentionally blocks a second unresolved assignment.

## Waiting

Use a bounded poll of native conversation metadata. An update signal is only permission to collect. It is not proof that the response belongs to the assignment.

Do not reopen the worker repeatedly during generation. Do not retry a message because the UI is slow.

## Collection

1. Open the worker by exact conversation ID.
2. Wait until the loaded conversation ID equals the configured worker ID.
3. Read the newest completed assistant response.
4. Validate the first nonempty line against the assignment's exact result marker.
5. Reject stale or mismatched markers.
6. Restore the exact parent Codex task ID in a `finally`-style cleanup path.

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
| Send confirmed | `pro-dispatch submitted` |
| Send may have happened | `pro-dispatch indeterminate`; never resend |
| Worker unchanged | Keep waiting within the bounded timeout |
| Thread not loaded | Open exact worker ID and wait |
| Wrong thread loaded | Stop and reject collection |
| Result marker missing or mismatched | `pro-dispatch ambiguous`; never resend |
| Parent restoration fails | Preserve assignment state and report the exact parent task ID |
