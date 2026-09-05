# Official-app native conversation protocol

Use this reference for setup, a native send, collection, or recovery. The
official combined ChatGPT/Codex desktop app supplies the native controls; exact
tool names are not part of the protocol. Do not replace unavailable controls with
shell UI automation, a browser, CDP, AppleScript, Accessibility, or clipboard
injection.

## Preflight and stable identities

On every invocation, prove the semantic capabilities listed in `SKILL.md` before
writing worker or assignment state. In particular, a collection read must bind:

- requested and loaded worker conversation IDs;
- the selected assistant message ID and the exact submitted user message ID;
- assistant role and message-level completed-generation provenance;
- raw item `truncated` and raw selected-result outer truncation, each with trusted
  adapter provenance; and
- exact assistant text from that same read operation.

Do not call an enclosing turn "complete" evidence for an item. A missing
truncation field stays unknown unless the reviewed helper release allowlists the
adapter contract and explicitly proves omission semantics. The current adapter
does not normalize omissions.

The user selects Pro visibly in one dedicated Chat conversation. Save its stable
conversation ID with `worker set`; a title and a claimed model are never identity
or machine-verifiable model evidence.

## One turn: arm, send once, then read back

1. Capture the stable parent Codex task ID.
2. `prepare` the selected explicit result mode using a private prompt file.
3. Use the returned exact `turn_id`, worker ID, and wrapped prompt.
4. Immediately before native transport, run
   `pro-dispatch arm '<assignment-id>' --turn-id '<turn-id>'`.
5. Send the returned prompt at most once to the returned worker ID.
6. Read the existing native user message bytes and stable ID without reconstructing
   or editing them. Run `submitted` with both.

Arming happens before transport and permanently prohibits resending that turn. A
crash before, during, or after the native send can leave an unknown outcome or
zero known sends. That is deliberately collect-only.

If exact read-back differs by any byte, do not resend. Only a receipt-explicit
single trailing-LF extraction artifact permits one re-read of the same existing
native message. For a native-send outcome that may have happened, record
`indeterminate`; later `submitted` verification is allowed only for the existing
message, never through a second send.

If native diagnostics show unusual activity, save the untrusted detail in a
private reason file and call `unusual-activity`. Report HTTP 403 explicitly,
preserve the optional request ID, and respect the 30-minute cooldown. Do not
collapse it into generic `systemError`.

## Collection protocol

1. Open the saved worker by exact ID and wait for the loaded ID to match.
2. If outbound verification is incomplete, identify the existing native user
   message by the receipt marker and verify it through `submitted`; do not send.
3. Ask the host for one native collection evidence object for the selected result.
4. Save that JSON and use `collect` with the exact `turn_id` and a private output
   file when the result will materialize.
5. Restore the exact parent task after outcome handling.

An update timestamp is permission to attempt collection, not proof of result
identity. A later reading of the same accepted message can have a different
`observed_at`; this is idempotent only when all immutable source/content identity
remains the same. Changed accepted content or a different source is a conflict.

For inline output, an exact two-line `CHUNKED_REQUIRED` control or a proven
truncated response can create a `response_rejected` predecessor plus exactly one
prepared recovery child. That transition requires verified outbound delivery and
proven generation completion under the receipt lock. The caller still must arm and
send the child. No uncertain send can create a child.

For chunked output, use the strict frame from `long-results.md` and collect every
child separately. A visible truncated prefix is never a chunk. For artifact output,
collect a valid manifest only as a hint and then use parent-side bare-Git
verification; `--discover` is permitted only for an already-authorized artifact
receipt with unreadable chat evidence.

## Failure mapping

| Native situation | Required action |
| --- | --- |
| Crash/interruption after arm | Recover exact worker collect-only; do not send that turn again |
| Exact native read-back exists | `submitted` with exact bytes and native user ID |
| Send acknowledged but read-back temporarily absent | `indeterminate`; wait and later verify the existing message |
| One trailing-LF read-back artifact | Re-extract the same native message once; never resend |
| Other read-back drift | Keep `indeterminate`; do not retry or resend |
| `thread not loaded` | Open exact worker ID, wait, and recover read-only |
| Wrong worker loaded | Stop and reject collection |
| Missing/mismatched marker or malformed collection evidence | Record `ambiguous`/fail closed; never resend |
| Message or outer truncation true/unknown | Reject it; re-read only where adapter contract permits or use an authorized chunk child |
| Native unusual-activity HTTP 403 | `unusual-activity`; collect-only and fixed cooldown |
| Parent restoration fails | Preserve immutable result/delivery state and report parent ID; navigation retry cannot reopen content |

## Foreground behavior

Current official-app collection can briefly foreground ChatGPT and move the
pointer. Submit and wait in the background where possible; if the user is working
in another app, defer collection until focus returns unless the user explicitly
permits interruption. The clipboard must remain unchanged.
