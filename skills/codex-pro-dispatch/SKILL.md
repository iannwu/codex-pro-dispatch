---
name: codex-pro-dispatch
description: Dispatch a bounded implementation, review, or research assignment from a Codex task to a dedicated ChatGPT Pro conversation in the official combined desktop app, recover the result without resending, and return to the parent Codex task. Use when the user asks Codex to delegate work to ChatGPT Pro; do not use for ordinary local coding.
metadata:
  short-description: Dispatch work to official-app ChatGPT Pro
---

# Codex Pro Dispatch

Use the official combined ChatGPT/Codex desktop app as the transport:

```text
Codex parent task
  -> dedicated ChatGPT Pro worker conversation
  -> worker response or GitHub commit
  -> exact Codex parent task
```

This skill relies on the host's native Chat and Codex conversation controls. It does not install another browser, app, daemon, model provider, MCP connector, or Accessibility bridge.

## Hard boundaries

- Use only the official combined ChatGPT/Codex desktop app.
- Do not use ChatGPT Web, Codex Web GPT, ChatGPT Classic, CDP, AppleScript, Accessibility automation, or the clipboard.
- Never resend automatically after a timeout, app restart, retrieval error, or `thread not loaded` result.
- Use one configured worker conversation and one unresolved assignment at a time.
- The user must visibly select Pro in the worker once. Native controls may not expose the selected model, so do not claim machine verification.
- Chat Pro may use its own GitHub connector when the assignment authorizes repository work. The parent Codex task must independently verify every reported branch, commit, file change, and CI result.
- Restore the exact parent Codex task after collection, including after failures.

## Before the first dispatch

If no worker is configured, read [references/native-protocol.md](references/native-protocol.md), then:

1. Ask the user to create or select one dedicated Chat conversation.
2. Ask the user to visibly select Pro in that conversation.
3. Resolve that conversation's stable ID with native conversation controls.
4. Save it:

```bash
pro-dispatch worker set \
  --conversation-id '<conversation-id>' \
  --label 'Codex Pro Dispatch Worker' \
  --confirm-pro
```

Do not infer Pro selection from the conversation title.

## Normal dispatch

1. Record the exact current Codex parent task ID.
2. Put the bounded assignment in a temporary UTF-8 file.
3. Prepare it:

```bash
pro-dispatch prepare \
  --parent-task-id '<parent-task-id>' \
  --prompt-file '<prompt-file>'
```

4. Read the JSON result and resolve `worker_conversation_id`. Immediately before the native send, durably arm the assignment:

```bash
pro-dispatch arm '<assignment-id>'
```

Do not call the native send unless arming succeeds. Once arming succeeds, `no_resend` is permanent for that assignment, including across an app crash.
5. Send `wrapped_prompt` exactly once to `worker_conversation_id` using native conversation controls.
6. After native submission is confirmed, read back the exact submitted user message from that worker conversation using native controls and save those bytes to a temporary UTF-8 file. Do not reconstruct it from the prepared JSON.
7. Verify the read-back before recording submission:

```bash
pro-dispatch submitted '<assignment-id>' \
  --sent-prompt-file '<native-read-back-file>'
```

The helper compares the read-back bytes with the prepared `wrapped_prompt` hash. If they differ by any byte, including whitespace or a newline, it records one observed send as `indeterminate`, sets `no_resend`, and returns an error. Never repair the text by resending it.

If the error reports `readback_correction_allowed: true`, the temporary read-back file was proven to equal the expected prompt plus exactly one trailing newline. Re-extract the same existing native user message without adding that file artifact, then run `pro-dispatch submitted` once more against the corrected file. This is read-back verification, not a second submission; `submission_count` remains one. Do not strip, normalize, or retry any other mismatch.

If the send may have occurred but confirmation failed, do not retry. Run:

```bash
pro-dispatch indeterminate '<assignment-id>' --reason-file '<reason-file>'
```

Write the exact error to the temporary UTF-8 reason file without interpolating it into a shell command. Use `--reason-file` for native errors and other untrusted text.

If the native send reports `systemError`, inspect the error payload exposed by the native control. If that payload is unavailable, inspect the official app's local log only around that single send, read-only, for the HTTP status, response detail, and request ID; do not dump broad logs. If diagnostics identify an HTTP 403 whose response reports unusual activity, preserve the exact response in the reason file and record it with the dedicated command:

```bash
pro-dispatch unusual-activity '<assignment-id>' \
  --request-id '<OpenAI-request-id>' \
  --reason-file '<reason-file>'
```

Report the blocker as an unusual-activity HTTP 403 and include the request ID when available. Do not reduce this to a generic `systemError`. The command keeps the assignment collect-only and starts a fixed 30-minute cooldown. During that cooldown, continue only read-only recovery of the existing assignment; never resend it. Even if the user authorizes abandoning the failed assignment and creating a fresh one, `pro-dispatch prepare` must remain blocked until the cooldown expires. Do not bypass or shorten the cooldown by changing workers.

If the app stops after `arm`—whether before, during, or after the native send—recover collect-only. Never send that assignment again. Only the user may authorize abandoning it and preparing a fresh assignment after bounded inspection of the exact worker.

8. Wait using the worker conversation's native metadata or timestamp. Do not repeatedly reopen the worker while it is generating.
9. When the worker has updated, open the worker by its exact conversation ID and wait until that exact thread is loaded.
10. Read only the newest completed assistant response associated with the assignment. Save it to a temporary UTF-8 file.
11. Validate and complete:

```bash
pro-dispatch complete '<assignment-id>' --response-file '<response-file>'
```

12. Use the returned `payload` as the worker result.
13. Restore the exact saved parent Codex task.
14. If the worker reported GitHub mutations, follow [references/github-verification.md](references/github-verification.md).

## Recovery without resending

On timeout, `thread not loaded`, app restart, stale UI, or response ambiguity:

1. Run:

```bash
pro-dispatch recover '<assignment-id>'
```

2. Open the saved worker conversation ID directly.
3. Wait for that exact thread to load.
4. Inspect the recovery fields, including `outbound_prompt_verified`, `wrapped_prompt_sha256`, `sent_prompt_sha256`, and `readback_correction_allowed`. If `outbound_prompt_verified` is not true, locate the existing submitted user message by its exact assignment marker. Save that native read-back to a temporary UTF-8 file and run:

```bash
pro-dispatch submitted '<assignment-id>' \
  --sent-prompt-file '<native-read-back-file>'
```

This recovery command verifies the already-existing message; it does not send anything. It is allowed from `indeterminate` or `ambiguous` while `submission_count` is zero. It is also allowed with `submission_count` one only when the receipt explicitly has `readback_correction_allowed: true`, or an older receipt's stored mismatch hash proves the same single-trailing-newline artifact, and the corrected read-back exactly matches the prepared hash. Never call the native send control during this recovery step.

5. Read the newest completed assistant response.
6. Validate it with `pro-dispatch complete`.
7. Restore the saved parent task ID.

Never send the original assignment again. If the response cannot be matched to the exact result marker, record the issue and stop:

```bash
pro-dispatch ambiguous '<assignment-id>' --reason-file '<reason-file>'
```

## Same-worker continuation

For a repair or review follow-up, keep the same Chat conversation but create a new assignment ID:

```bash
pro-dispatch prepare \
  --parent-task-id '<parent-task-id>' \
  --continuation-of '<completed-assignment-id>' \
  --prompt-file '<follow-up-file>'
```

The follow-up must still be submitted once and validated through its own result marker.
Run `pro-dispatch arm '<new-assignment-id>'` immediately before its native send.

## Foreground behavior

Submission and waiting may occur in the background. Current result collection can briefly foreground ChatGPT and move the pointer.

- If another application is frontmost, defer collection when native focus state is available.
- Collect when the user returns to ChatGPT/Codex, unless the user explicitly permits interruption.
- Do not use the clipboard.
- Always return to the exact parent task.

## Local state

`pro-dispatch` stores only private configuration and receipts:

- worker conversation ID and user-confirmed Pro status
- assignment ID
- parent Codex task ID
- state transitions and timestamps
- prompt and response hashes

It does not store ChatGPT cookies, account credentials, repository source, or full transcripts.

Useful commands:

```bash
pro-dispatch worker show
pro-dispatch status
pro-dispatch recover '<assignment-id>'
pro-dispatch unusual-activity '<assignment-id>' --request-id '<id>' --reason-file '<reason-file>'
pro-dispatch abandon '<assignment-id>' --reason-file '<reason-file>'
pro-dispatch worker reset
pro-dispatch doctor
```

If native conversation controls are unavailable, stop with the exact blocker. Do not silently substitute another transport.
