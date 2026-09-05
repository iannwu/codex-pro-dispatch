---
name: codex-pro-dispatch
description: Dispatch a bounded implementation, review, or research assignment from a Codex task to a dedicated ChatGPT Pro conversation in the official combined desktop app, recover the result without resending, and return to the parent Codex task. Use when the user asks Codex to delegate work to ChatGPT Pro; do not use for ordinary local coding.
metadata:
  short-description: Dispatch work to official-app ChatGPT Pro
  version: "1.2.2-rc.1"
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

## Contract

Goal: safely delegate one bounded assignment from the exact Codex parent task to a user-confirmed ChatGPT Pro worker, collect the existing result without duplicate submission, independently verify claimed repository work, and restore the exact parent task. Excellent behavior fails closed when native state is ambiguous.

Evaluate the workflow on these skill-specific dimensions:

- `AT_MOST_ONCE_SAFETY`: At most one native send attempt is allowed; no timeout, restart, read-back mismatch, or transport error can cause an automatic resend.
- `THREAD_IDENTITY`: the worker and parent task are resolved by stable identity, never titles or visual position.
- `RECOVERY_INTEGRITY`: every ambiguous state preserves collect-only recovery and rejects stale or mismatched results.
- `VERIFICATION_BOUNDARY`: worker claims remain untrusted until the parent independently verifies them.
- `OPERATOR_CLARITY`: another Codex instance can follow the workflow without guessing about permissions or native state.

Hard fail if any path permits an automatic resend after arming, accepts a result from the wrong worker, restores the wrong parent task, or treats a worker claim of repository mutation as verified evidence.

## Hard boundaries

- Use only the official combined ChatGPT/Codex desktop app.
- Do not use ChatGPT Web, Codex Web GPT, ChatGPT Classic, CDP, AppleScript, Accessibility automation, or the clipboard.
- Never resend automatically after a timeout, app restart, retrieval error, or `thread not loaded` result.
- Use one configured worker conversation and one unresolved assignment at a time.
- The user must visibly select Pro in the worker once. Native controls may not expose the selected model, so do not claim machine verification.
- Chat Pro may use its own GitHub connector when the assignment authorizes repository work. This plugin does not install or authenticate that connector. Before a write assignment, confirm that the worker exposes the required write action for the exact repository and that the starting commit is remotely visible. Read-only access, local-only branches, uncommitted changes, and the parent worktree are insufficient.
- The parent Codex task must have an independent read path and verify every reported branch, commit, file change, and CI result.
- Restore the exact parent Codex task after collection, including after failures.

## Verification scope on the current desktop host

This recovery uses the existing bounded-footer protocol, not the experimental
schema-v2 evidence adapter. `complete` means a valid protocol envelope was
observed. The native history reader can trim source text, cache history, and omit
finality/integrity metadata. Therefore results explicitly report
`verification_level: bounded_native_summary`, `generation_finality_verified: false`,
and `source_bytes_verified: false`. `outbound_prompt_verified` records equality
of returned summary text with the prepared prompt, not hidden source-byte proof.
Never describe an idle worker, a completed enclosing turn, or an end marker as
native generation-finality evidence. Full source-integrity verification remains
unavailable on this reader. Review answer correctness and Git claims independently.

Use `complete --native-read-file` for live collection. It checks worker identity,
the same-turn user/assistant relationship, the returned prompt hash, every
selected-scope `truncated`/`textTruncated` flag, both framing markers, and a response
strictly below the reader's 20,000 UTF-16-unit boundary. Preparation also rejects
a wrapped prompt at that boundary before creating a receipt; use a shorter prompt
or a pinned repository reference for large inputs. Omitted flags remain null,
not false. `--response-file` remains a lower-level compatibility/testing input;
it does not validate a native source. Never use it to bypass a rejected native read.

Poll with roughly ten seconds between completed reads and a bounded observation
budget. On timeout, report the assignment ID and its retained reservation; use
read-only recovery, never resend. For ordinary work use a ten-minute budget and
provide progress about once per minute; simple smoke tests use two minutes.
A native task-list update can precede visible history because the reader caches
conversation queries. Continue reading the same assignment; never send a dummy
message to refresh history or use a stale user message as read-back.
Only schedule another continuation after the previous exchange has passed native
validation, its payload was safely appended, and a fresh pre-send read reports idle.

## Required host preflight

Run this preflight at the start of every invocation, before configuring a worker or preparing an assignment. This repository supplies the safety protocol and local receipt helper; the host supplies the native transport.

Confirm that the current Codex task exposes all of these semantic capabilities:

1. Read the stable ID of the current parent Codex task.
2. List or resolve Chat conversations by stable ID.
3. Send one user message to an exact Chat conversation ID.
4. Read the submitted user message in the native history summary and compare its returned text exactly with the wrapped prompt.
5. Read a complete inner native history JSON containing the exact worker, paired user/assistant IDs, returned text, visible truncation flags, and `thread.status.type`. Require `idle` before sending and accepting a result.
6. Open an exact Chat or Codex task by stable ID so the parent can be restored.

Exact tool names may vary, but every capability must be available in the current task. Do not infer availability from macOS, app presence, an installed plugin, or a prior successful run. If any capability is missing, stop before writing worker configuration or assignment state and report the missing capability. Never substitute UI automation.

Resolve the bundled helper relative to this `SKILL.md`: use the absolute path to `scripts/pro-dispatch` inside the installed skill. A source-checkout install may also expose `pro-dispatch` on `PATH`. In every command below, use the absolute bundled path when `pro-dispatch` is not on `PATH`.

After the worker exists, run:

```bash
pro-dispatch doctor --native-controls-confirmed
```

Proceed only when it exits zero and returns both `local_ok: true` and `native_controls_confirmed: true`. The flag is an assertion that this invocation completed the semantic capability check; it is not automatic tool discovery.

## Private transient files

Some host controls require prompt, read-back, response, or error text to pass through files. Before writing any such content, create one private temporary directory with mode `0700`, set a restrictive `umask` so files are mode `0600`, and retain its exact path. Keep every transient file inside it. Delete that directory in a `finally`-style cleanup after the parent task is restored, including on failure. Never place transient content in the repository, a shared directory, or a predictable filename.

The helper's receipt store never retains prompt or response bodies. Temporary files and host/terminal logs are outside that receipt-store guarantee, so minimize their lifetime and avoid printing their contents.

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
  --confirm-pro \
  --native-controls-confirmed
```

Do not infer Pro selection from the conversation title.

## Normal dispatch

1. Record the exact current Codex parent task ID. For a repository-write assignment, first confirm the GitHub prerequisites in [references/github-verification.md](references/github-verification.md). If they fail, stop or change the assignment to prompt-only review with the user's agreement.
2. Put the bounded assignment in the private temporary directory as a UTF-8 file.
3. Prepare it:

```bash
pro-dispatch prepare \
  --parent-task-id '<parent-task-id>' \
  --native-controls-confirmed \
  --prompt-file '<prompt-file>'
```

4. Read the JSON result and resolve `worker_conversation_id`. Immediately before the native send, durably arm the assignment:

```bash
pro-dispatch arm '<assignment-id>'
```

Do not call the native send unless arming succeeds. Once arming succeeds, `no_resend` is permanent for that assignment, including across an app crash.
5. Make at most one native send attempt for `wrapped_prompt` to `worker_conversation_id`. Arming does not guarantee delivery: an interruption can leave the assignment with zero sends and permanently collect-only.
6. After native submission is confirmed, select the user message whose first line is exactly `[CODEX_PRO_DISPATCH assignment_id=<current-assignment-id>]` from the configured worker. A native read may still return an older completed turn while the new turn is active. If the matching message is absent, wait within a bounded timeout or record `indeterminate` and recover collect-only; do not pass an older message to `submitted`. Save the matching native bytes to the private temporary directory as UTF-8. Do not reconstruct them from the prepared JSON.
7. Verify the read-back before recording submission:

```bash
pro-dispatch submitted '<assignment-id>' \
  --sent-prompt-file '<native-read-back-file>'
```

The helper first rejects a leading assignment marker for another assignment as `stale-readback`, leaving the receipt unchanged and resending prohibited. Wait for the matching native message. Otherwise it compares the read-back bytes with the prepared `wrapped_prompt` hash. If they differ by any byte, including whitespace or a newline, it records one observed send as `indeterminate`, sets `no_resend`, and returns an error. Never repair the text by resending it.

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
10. Read the worker with `turnLimit: 2` and `maxOutputCharsPerItem: 20000`. Save the complete unedited inner JSON to a private mode-0600 file. Do not select or reconstruct an assistant-only response.
11. Validate and complete:

```bash
pro-dispatch complete '<assignment-id>' --native-read-file '<native-read-file>'
```

12. Use the returned `payload` as the worker result.
13. Restore the exact saved parent Codex task.
14. If the worker reported GitHub mutations, follow [references/github-verification.md](references/github-verification.md).
15. Delete the private temporary directory in the cleanup path.

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

5. Read the complete native history JSON for the saved worker and matching exchange, using the same 20,000-character limit.
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
  --native-controls-confirmed \
  --prompt-file '<follow-up-file>'
```

The follow-up still receives its own at-most-one native send attempt and must be validated through its own result marker.
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
pro-dispatch doctor --native-controls-confirmed
```

If native conversation controls are unavailable, stop with the exact blocker. Do not silently substitute another transport.

`worker reset --force` and `purge --yes --force` are break-glass operations. They can erase recovery identity or receipts for unresolved work, destroying the workflow's no-resend evidence. Never use them during normal operation; require explicit user authorization and explain that recovery guarantees will be lost.

## v1.2 bounded long-result overlay

This overlay applies to every newly prepared v1.2 response. It changes only the
result envelope and the explicit long-result path; every v1.1 send, read-back,
recovery, identity, cooldown, follow-up, foreground, verification, and
break-glass rule above remains mandatory.

### Native evidence and exact response envelope

One native collection read must establish the configured and loaded worker IDs,
a stable assistant item ID associated with the verified submitted user message,
exact returned response text, paired user/assistant IDs, an idle worker, and
explicit native truncation metadata when supplied. A completed enclosing turn
is synthetic on this host and never proves generation finality. Do not silently interpret an omitted `truncated` field
as false. An explicit `truncated: true` is always rejected.

If the native reader reports it, preserve the exact response bytes and invoke
the normal command with `--truncated`; otherwise omit that flag:

```bash
pro-dispatch complete '<assignment-id>' --response-file '<response-file>' --truncated
```

The parent independently verifies every reported branch, commit, file change,
and CI result for any separately authorized repository-write assignment.

Every accepted response is valid UTF-8, contains no CR byte, begins at byte
zero with the exact result marker, and ends with
this exact footer as the literal final byte sequence:

```text
[CODEX_PRO_DISPATCH_END assignment_id=<current-assignment-id>]
```

Never normalize newlines, strip body text, or search opaque body bytes for
marker-looking examples. Ten thousand UTF-8 bytes is a generation guideline,
not an acceptance gate; prompt the worker to target no more than 6,000 body
characters.

For an initial assignment, `wrap_prompt` permits only a nonempty short result
or this exact no-body control form; it must not advertise chunks:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<root-assignment-id>]
[CODEX_PRO_DISPATCH_CONTINUATION_REQUIRED root_assignment_id=<root-assignment-id>]
[CODEX_PRO_DISPATCH_END assignment_id=<root-assignment-id>]
```

If the normalized body begins at byte zero with this exact continuation line,
the wrapper permits only the matching chunk form; it must not advertise a short
result or the control form:

```text
[CODEX_PRO_DISPATCH_CONTINUE root_assignment_id=<root-assignment-id> next_index=<index>]
```

With neither expected chunk argument, `complete` accepts only a short result or
the exact control form. It returns the existing `payload` and a `result_kind`
of `short` or `continuation_required`. A control or chunk-looking literal after
any earlier body byte, including an initial LF, remains opaque body.

For a chunk, supply both arguments; supplying exactly one is an error:

```bash
pro-dispatch complete '<current-assignment-id>' \
  --native-read-file '<native-read-file>' \
  --expected-root-assignment-id '<root-assignment-id>' \
  --expected-chunk-index '<next-index>'
```

The chunk response must be only:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<current-assignment-id>]
[CODEX_PRO_DISPATCH_CHUNK root_assignment_id=<root-assignment-id> index=<index> final=<0-or-1>]
<chunk body>
[CODEX_PRO_DISPATCH_END assignment_id=<current-assignment-id>]
```

The root must match. The index is canonical decimal 1 through 16 and equals the
expected next index; `final` is exactly 0 or 1. A nonfinal body is nonempty. An
empty final body is valid only after an earlier accepted nonempty chunk. The JSON
keeps `payload` and adds `result_kind`; chunks also add `chunk_index` and
`final`; no second chunk-body JSON field exists.

New receipts add `result_protocol: "bounded-footer-v1"` under the existing
schema version. There is no migration or backfill. A terminal legacy receipt is
readable and immutable. An active receipt without that discriminator permits
only `status`, `recover`, or explicit `abandon`; `arm`, `submitted`, `pending`,
`complete`, and continuation progression fail with
`legacy-active-assignment`. v1.2 never accepts the old leading-marker-only
result form.

### Continuation and transient assembly

After the exact initial control response completes, initialize only this
transient parent-task context:

```text
root_assignment_id
accepted_chunk_index
assembly_file_path
last_accepted_assignment_id
recovery_used_for_index
```

Create one exclusive mode-0600 assembly file inside the existing private
mode-0700 directory. The control response prepares but never arms or sends its
first continuation. For each expected index, write this exact deterministic
body to a new private prompt file without extra bytes:

```text
[CODEX_PRO_DISPATCH_CONTINUE root_assignment_id=<root-assignment-id> next_index=<index>]

Return only chunk <index> of the same deliverable.
Continue from the last accepted boundary without repeating or summarizing accepted text.
Use the required chunk envelope.
Aim to keep the entire response below 10,000 UTF-8 bytes.
Set final=1 only when this chunk completes the deliverable.
Otherwise set final=0.
```

Prepare it with the existing command, then use the normal v1.1 arm-and-one-send
sequence exactly once:

```bash
pro-dispatch prepare \
  --parent-task-id '<parent-task-id>' \
  --continuation-of '<last-accepted-assignment-id>' \
  --prompt-file '<continuation-prompt-file>' \
  --native-controls-confirmed
```

Parse the helper's completion JSON with a real JSON parser; never use eval,
shell substitution, regex extraction, or line splitting. Require
`result_kind: "chunk"`, encode `payload` as UTF-8 without normalization, append
exactly those bytes to the assembly file, then flush, fsync, and verify mode
0600 before advancing the accepted index or last accepted assignment ID.

If opening, writing, flushing, fsyncing, or permission checking fails,
including after a partial write, do not advance, restore a logical result,
prepare another continuation, or reuse the file. Discard all transient logical
state. The completed native receipt remains immutable. A restart also stops
collection and requires separately authorized fresh dispatch from the beginning.

For a valid nonfinal chunk below index 16, prepare the next continuation. At
index 16 with final=0, stop with an incomplete-result error before preparing or
sending chunk 17. On final=1, restore the exact assembly bytes to the original
parent, then clean up only after restoration succeeds or its normal failure is
reported.

A rejected native chunk is never resent. The operator may authorize exactly one
replacement for an expected index only when `recovery_used_for_index` is not
that index: first use the existing ambiguity and explicit abandon path, set the
guard before preparation, then prepare a new assignment with a new ID, the same
continuation body, and `--continuation-of` the last accepted assignment. Send
that replacement once. If it fails, or the guard already matches, stop with an
incomplete result. Clear the guard only after a successful append and index
advance. Local assembly failure never consumes or permits this replacement.

Native desktop acceptance remains a release gate: reject truncation; reconstruct
a result over 30,000 characters from chunks guided below 10,000 bytes with exact bytes and
parent restoration; and exercise multi-chunk assembly write-failure
stop-and-cleanup.
