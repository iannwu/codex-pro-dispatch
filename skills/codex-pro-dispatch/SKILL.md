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

4. Read the JSON result. Send `wrapped_prompt` exactly once to `worker_conversation_id` using native conversation controls.
5. After native submission is confirmed, read back the exact submitted user message from that worker conversation using native controls and save those bytes to a temporary UTF-8 file. Do not reconstruct it from the prepared JSON.
6. Verify the read-back before recording submission:

```bash
pro-dispatch submitted '<assignment-id>' \
  --sent-prompt-file '<native-read-back-file>'
```

The helper compares the read-back bytes with the prepared `wrapped_prompt` hash. If they differ by any byte, including whitespace or a newline, it records one observed send as `indeterminate`, sets `no_resend`, returns an error, and requires collection-only recovery. Never repair the text by resending it.

If the send may have occurred but confirmation failed, do not retry. Run:

```bash
pro-dispatch indeterminate '<assignment-id>' --reason '<exact error>'
```

7. Wait using the worker conversation's native metadata or timestamp. Do not repeatedly reopen the worker while it is generating.
8. When the worker has updated, open the worker by its exact conversation ID and wait until that exact thread is loaded.
9. Read only the newest completed assistant response associated with the assignment. Save it to a temporary UTF-8 file.
10. Validate and complete:

```bash
pro-dispatch complete '<assignment-id>' --response-file '<response-file>'
```

11. Use the returned `payload` as the worker result.
12. Restore the exact saved parent Codex task.
13. If the worker reported GitHub mutations, follow [references/github-verification.md](references/github-verification.md).

## Recovery without resending

On timeout, `thread not loaded`, app restart, stale UI, or response ambiguity:

1. Run:

```bash
pro-dispatch recover '<assignment-id>'
```

2. Open the saved worker conversation ID directly.
3. Wait for that exact thread to load.
4. Read the newest completed assistant response.
5. Validate it with `pro-dispatch complete`.
6. Restore the saved parent task ID.

Never send the original assignment again. If the response cannot be matched to the exact result marker, record the issue and stop:

```bash
pro-dispatch ambiguous '<assignment-id>' --reason '<exact blocker>'
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
pro-dispatch abandon '<assignment-id>' --reason 'user cancelled'
pro-dispatch worker reset
pro-dispatch doctor
```

If native conversation controls are unavailable, stop with the exact blocker. Do not silently substitute another transport.
