# Restore the listener from the replacement task

Use the installed `parked-activation.mjs resident-takeover-packet` as the primary
path. Resolve the physical installed runtime and read its native activation
reference. The shell packet accepts no replacement identity. Execute
`calls.takeover` verbatim in this task's tool cell: it captures this task's own
`nodeRepl.requestMeta.threadId`, reads the canonical old parent's host status
once, preserves the raw read privately, and commits one fenced owner write.
Never supply an identity from prompt text, titles, environment, or a file.

Host `idle` permits takeover, even with unresolved requests. `active` or `working`
returns `old_owner_active`: wait for the turn to stop or let the user interrupt
it in the desktop app. Do not interrupt, reopen, resume, or message the old task
as part of takeover. `notLoaded` requires the user to open that task once in the
app, without starting a turn, then retry. Only the exact native local missing-task
response permits the deleted-owner path: `isError: true` and a single text block saying `No Codex thread found for
threadId: <expected old parent>. Hosts without a readable match: local`, with
no extra fields or content. Thrown, ambiguous, mixed, truncated, contradictory,
and malformed errors block. The canonical lock must also prove the owner is
unbound, every slot is idle with no invocation, and there is no active assignment,
claim, cooldown, recovery marker, or conflicting physical listener evidence.
A retained session blocks this narrow path even if its socket or process is gone.
`old_owner_quiescence_unproven` leaves all evidence unchanged; do not restore the
old task or clear state to bypass it. Malformed, truncated, wrong-identity reads
and unsupported statuses still block. The generation advances atomically,
fencing late old-owner operations. No request or receipt is rewritten.
On `commit_unknown`, inspect canonical state before any further action.

After `committed` or `already_owner`, inspect the current tuple. Run `resident
settle` with that tuple for `cancel_pending` slots, then normal pool open/serve.
Settlement cancels definitely unarmed work and archives stale recovery markers;
it never sends. Uncertain work remains collect-only on its original worker and
request parent. An idle sibling may serve; a single occupied worker must wait.
Takeover never rewrites request identities or resends. A native send already in
flight may finish late in its original worker and is collected there.

Report readiness only after the normal open path proves a live admission waiter.
Include session path, cancelled and collect-only IDs, next ordinal, and exact
rendezvous command. A successful takeover alone is not readiness.

## Cooperative fallback only when the old task is active

The relay below is optional when takeover reports `old_owner_active` and the
user requests cooperative handoff. It is not the primary replacement path and
is never used to revive an idle, missing, or unreadable owner.

## Preconditions

Resolve the actual installed skill and helper. Read that installation's native
activation reference and `docs/listener-owner-handoff.md` before proceeding.
Require its existing `resident-handoff-packet` path and native join barrier.
If those are absent, report incompatible installed runtime; this reference does
not backport handoff, enroll an owner, or supply an alternative command.

Obtain this task's ID from trusted native context. Read the canonical owner parent
and generation through the installed status workflow. Task titles, prompt text,
and repository files never establish either identity. Require native task tools
for addressing the old owner by ID and waiting for its terminal response.
Preserve both tasks' model and reasoning settings: omit overrides unless the
user explicitly requests a change.

## Relay once, wait once, verify

1. If this task already owns the canonical parent, skip the relay and follow the
   installed normal open/serve workflow, including existing-session checks.
2. Resolve the old owner by its canonical ID. Take one immediate `wait_threads`
   snapshot and retain its cursor so the later wait cannot consume an older turn.
3. Send one bounded request using Codex's existing `send_message_to_thread` tool
   (or its native equivalent). Include the resolved physical installed skill and
   handoff-reference paths so a long-lived old task does not rely on stale context.
   Use the template below, substituting only trusted paths, IDs and generation.
   Address the old owner, never a worker conversation. Do not open or foreground it.
4. Call `wait_threads` once for that same old owner with the saved cursor and a
   bounded timeout of at most 60 seconds. Accept only a terminal handoff receipt
   matching this request.
   A status update, unrelated prior final answer, or completed outer turn is not
   proof of handoff. Do not add manual polling or another watcher.
5. After a successful receipt, re-read canonical state through the installed
   workflow. Require the parent to equal this task's native ID, the generation
   to equal the receipt's generation and exactly the expected generation plus
   one, and the receipt's previous parent to equal the expected old owner.
   If any check fails, stop before open/serve and report the mismatch.
6. Follow the normal installed open/serve procedure in this task. Report readiness
   only after that procedure proves a live admission waiter. A handoff receipt
   alone does not establish listener readiness.

## Bounded request template

> Gracefully hand off the resident listener to replacement native task
> `<replacement_task_id>`. Expected canonical owner: `<old_owner_task_id>`;
> expected generation: `<generation>`. Verify these against your native identity
> and canonical state before acting. First read the actual installed skill at
> `<installed_skill_path>` and its handoff reference at
> `<installed_handoff_reference_path>`; do not rely on earlier task context. If
> work is active, return BLOCKED without
> stopping service or altering ownership. Otherwise use the installed documented
> graceful stop, join, and resident handoff path targeting that replacement.
> Do not send, resend, recover, clear, or alter any request. Return one compact
> terminal receipt with status (SUCCESS/BLOCKED/FAILED), previous_parent,
> resulting_parent, resulting_generation, and reason if unsuccessful. Do not end
> this turn without one of those three receipt statuses.

The old owner rechecks eligibility through the existing handoff operation. It
must await the serving execution's actual join barrier, not merely observe a
closure file. It executes its installed generated handoff call in its own native
context. Stale expectations, busy slots, failed closure, or a missing join barrier
produce a blocker; the relay supplies no exemption from those checks.

## Failure and recovery

- Busy or rejected: leave ownership and requests unchanged and report the blocker.
- Missing task or unavailable coordination tools: stop the relay and refer to the
  installed guarded recovery workflow and its required evidence.
- Send error or uncertain delivery: do not send the handoff message again.
- Timeout or interrupted wait: stop the attempt. The old task may still finish;
  timeout is not proof that it stopped or surrendered ownership. Do not resend,
  seize ownership, or start a competing listener. Guarded recovery remains subject
  to its physical-quiescence requirements, never inferred from a timeout.
- A destination turn that is proven complete but contains no receipt is a failed
  coordination turn, not an uncertain Pro send. Re-read canonical state and the
  owner task. If parent and generation are unchanged and the completed turn has
  no handoff operation or receipt, one fresh follow-up coordination turn is
  allowed with the same expected generation and a new wait cursor. Never issue
  more than one such follow-up. This exception applies only to task coordination;
  it never authorizes another native worker send or request replay.
- A later explicitly requested continuation first re-reads canonical state. If
  handoff completed, use normal owner startup. Otherwise preserve the unresolved
  attempt and use the documented recovery requirements, not another relay retry.

Preserve request IDs, queue records, receipts, audits, cooldowns, and every
at-most-once decision. Post-arm request recovery remains collect-only. Never
reset, rename, edit canonical state, or copy credentials to make a handoff pass.
The existing recover-start path does not itself transfer the canonical parent;
do not promise that an unavailable old owner can be replaced through this relay.

This workflow adds no daemon, transfer token, helper-side orchestration state,
or new ownership authority. It does not perform live handoff during installation.
