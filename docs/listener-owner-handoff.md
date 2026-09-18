# Guarded owner takeover and cooperative handoff

The primary replacement path is the installed `resident-takeover-packet`,
executed by the replacement task. Follow
[new-owner-handoff.md](../skills/codex-pro-dispatch/references/new-owner-handoff.md).
Host-idle takeover preserves uncertain requests as collect-only, cancels
unarmed work through `resident settle`, and fences the previous generation.
It writes only the owner record during commit. Native identity is captured in
the replacement task, never supplied by a shell argument. A late in-flight send
may finish in the original worker; it never permits a resend.

The following cooperative procedure is a fallback only for an active old task.


Use this graceful path when the current listener task is available and its
conversation has become too large. The worker pool must already use a schema-3
canonical owner. This operation does not enroll or migrate a legacy owner.

1. Create the replacement Codex task. Obtain its exact task ID from native task
   metadata. The handoff packet independently checks that it exists as a local
   Codex task before committing. ChatGPT conversations are not valid targets.
2. In the current owner task, finish active requests and collectors, then run
   the documented graceful resident stop and join its serving execution.
   Confirm its final result is `resident_stopped` and `resident_joined`. The
   generated serving call records `resident-joined.json` only after awaiting
   the complete serving function, including cleanup. A closure audit alone
   is insufficient. Older serving packets without this barrier cannot hand off;
   do not manufacture the file or call the internal recorder yourself.
3. Still in the old task, generate a packet using the installed activation script:

   ```text
   node "$ACT" resident-handoff-packet NEW_NATIVE_TASK_ID
   ```

   Execute the returned `calls.handoff` verbatim in the current task. It checks
   `nodeRepl.requestMeta.threadId` against the original in-memory owner, verifies
   the join barrier, reads the destination through the native task tool, then
   rechecks the current native context before the locked commit. Missing,
   foreign, or malformed task results fail before any ownership change.
   Plain `pro-dispatch resident handoff` always refuses, even with copied owner
   credentials or caller-supplied attestation flags. The Python commit function
   is private to this native path and is not an operator command.
4. Pass the returned owner receipt to the new task. It follows the ordinary
   installed open/serve instructions, which verify its native parent identity.
   Preserve each task's model and reasoning unless explicitly asked to change them.

The operation requires a matching private session descriptor, graceful closure
audit and native join barrier, no socket or waiter, idle worker slots, no active receipts or claimed
requests, and no recovery owner. Under the canonical lock it advances generation,
changes parent, mints an internal owner ID, clears the session binding and records
the previous parent in the existing qualification history. Old credentials become
stale immediately. Requests, receipts, queue records, pool configuration and
cooldowns are preserved. An active cooldown allows handoff but still blocks sends.

If the old task is unavailable or closure failed, stop this procedure. Existing
`recover-start` remains unchanged and does not transfer the parent. Do not clear
state, rerun enrollment, resend work, or substitute a new task into old credentials.

This uses the same cooperative native execution boundary as open/serve. It does
not introduce cryptographic task attestation or protect against arbitrary code
execution or direct state editing by another process running as the same user.
Native task existence is checked immediately before commit, not reserved forever;
if the target is deleted afterward, the guarded takeover status check fails closed.
