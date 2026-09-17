# Trusted native activation

Read the broker reference; require production ownership, native preflight,
trusted BROKER/PARENT IDs and canonical authority. Generate:

    node /absolute/skill/scripts/parked-activation.mjs packet BROKER PARENT WORKER

Execute decoded calls.open/receive/dispatch verbatim as outer functions.exec
bodies, never eval. Open once and verify IDs/turn/directory. Do not start receive
until the correlated command-ready gate below succeeds.
Rewait only on transport.closed:false without blocked result, pending helper
or navigation failure. Null delivery stops. Generation is not approval.

Launch actual Claude with exactly one allowed foreground command:

    node /absolute/skill/scripts/parked-activation.mjs rendezvous SESSION ORDINAL REQUEST PROMPT CLIENT_SESSION

Concurrently, the parent starts one ordinary, event-driven command:

    node /absolute/skill/scripts/parked-activation.mjs command-ready SESSION ORDINAL REQUEST

This parent command waits at most ten minutes for that explicitly launched
request's command-ready event. No native receive or idle timer runs during Claude
startup. Observe the existing Claude/local-wait executions with host waits no
longer than 60 seconds; do not poll or wake an idle native worker.

Only a successful, complete commandReady:true result matching SESSION's ID,
ORDINAL and REQUEST permits the parent to execute calls.receive once.
The client is already running and watching before this result appears, so native
readiness leads directly to the same process's single submit, without Claude
thinking again between readiness and submission.

Command readiness expires after one idleMs-sized rendezvous interval, independent
of the native receive interval. A failed/expired gate ends the attempt. Do not
start receive, repeat either command, or delete command-observed markers.
Existing native-ready files, duplicate attempts and wrong identities are rejected.
Readiness remains a point-in-time observation, not guaranteed client liveness.
The old ready command is diagnostic only, not a launch gate.

Require published answer and successful nonempty terminal; repeat collect then ack.
Job two requires A's answer and terminal completion, followed by the same
command-ready sequence for a distinct request and the next ordinal. Do not start
B's receive while its Claude process is still starting. No later user input is
needed to follow this preauthorized sequence.
Idle and active waiting have different rules. With no accepted request, use
one receive window only: idleMs is 45000 and outer/REPL waits request 60000.
No empty-session model check-ins or automatic idle rearming are permitted.
An unexpected early idle yield is not a demonstrated quiet availability path.

After a real request is accepted, dispatch yields after a requested 1000ms.
Keep the native task active and use supported functions.wait on that exact cell,
with each wait no longer than 60000ms, until that execution completes.
Do not execute dispatch again, repeat submit, or finalize the task while its
active execution is still required. These active-job check-ins are authorized.

The same dispatch automatically repeats collect-only observation passes after
pending results. Only the initial pass can reach the existing arm/send path.
Ten-minute milestones report progress, not cancellation. The one-hour active
observation budget is a qualification bound; exhaustion returns pending with
ownership intact. A tool already in flight is not cancelled by that budget.
The original client reply budget is 65 minutes. The two-hour admission ceiling
does not extend the 45-second idle receive window or promise continuous service.

Start the next receive only after actual work, verified socket completion and
the next command-ready gate. Neither gate extends or rearms an idle receive.
Preserve command/snapshot records, per-pass evidence, actual elapsed times,
check-in counts and attributable usage. Hidden usage remains unavailable.
Longer quiet idle/Stop behavior is unqualified. Broker deployment gates apply.

## Explicit replacement of a proven closed, unused listener

Normal packet generation retains the no-reopen rule. A retained parkedSocket
object, a null parkedDelivery, or an expired timestamp alone is not closure proof.
Do not clear globals, reset the REPL, restart the app, delete evidence, or reuse
the old listener to recover an expired receive.

For an explicitly authorized fresh attempt in the same native task, generate:

    node /absolute/skill/scripts/parked-activation.mjs closed-packet BROKER PARENT WORKER CLOSED_SESSION

CLOSED_SESSION is the old physical session directory, not a client-selected
destination. This narrow recovery accepts only a private matching session
descriptor and an idle_expired audit with zero events, plus an absent wake.sock.
Canonical ownership and cooldown checks still apply. Other closure reasons,
accepted jobs, missing evidence, and unknown closure are not recoverable here.

The generated open body revalidates the evidence and matches the descriptor
against the retained native socket object and broker binding. Only after proving
closure does it join the old object's existing close promise. It never closes
an unproven active listener merely to manufacture eligibility for replacement.

A native in-progress guard prevents concurrent opens. A per-packet consumed
identity and an exclusive, synced replacement-open.once.json in the old directory
prevent repeated replacement. Failure after consumption remains fail-closed.
Do not remove that marker or generate replacement attempts to bypass a failure.

The old session descriptor, audit, readiness, and attempt evidence remain intact.
The fresh listener receives a new directory, session ID and token; the binding
records the current turn of the same broker task. The old receive is not rearmed.
Generating a packet or opening the new listener does not claim, arm, or send.

After the new open succeeds, use the normal correlated command-ready rendezvous
before the one finite receive. Never replay an old command-ready record or
interpret replacement as authorization to retry an existing dispatch.

Verify this change in the disposable candidate before a fresh native attempt:

    node --test tests/test_parked_closed_session.mjs
    node --test tests/test_parked_runner.cjs tests/test_parked_*.mjs
    PYTHONPATH="$PWD/src" python3 -m unittest discover -s tests -v

## Explicit replacement for a proven closed, still-queued request

This separate recovery handles a listener that accepted one run delivery,
finished it blocked, and closed before any dispatch receipt existed. It does
not broaden closed-packet, ordinary submission, post-arm recovery, or automatic
retry behavior. Use the existing request ID and its original retained content.

The trusted operator supplies the exact immediate closed-session evidence:

```bash
node "$ACTIVATION" closed-queued-packet \
  "$BROKER" "$PARENT" "$WORKER" "$CLOSED_SESSION" \
  "$OLD_SESSION_ID" "$REQUEST" "$FINGERPRINT" "$OLD_CALL_ID" \
  "$CLIENT_SESSION" "$OLD_NONCE" "$RAW_PROMPT_SHA256"
```

ACTIVATION is the reviewed candidate's physical parked-activation.mjs path.
These identities and hashes come from the preserved private qualification
record, never from consultation text. CLOSED_SESSION identifies the immediate
failed listener, not an earlier ancestor. For A2, use A2's exact descriptor,
audit, command, observed-command and prompt snapshot; preserve A1 unchanged.

Packet generation requires a private matching descriptor, absent wake.sock,
and a retired_after_job audit containing exactly one accepted run event followed
by one finished blocked event for the expected call and request. Ordinal-1
command and observed-command records must match the expected session, request,
client identity, nonce and prompt hash. Native readiness must identify that
same session and ordinal. The private prompt snapshot must match the expected
raw UTF-8 hash. Missing, changed, additional or conflicting evidence rejects.

The canonical queue resume-check runs under the existing authority lock. It
requires the original queued-record shape, recomputes its fingerprint and raw
prompt hash, rejects every existing assignment receipt for that ID, and retains
the active-assignment, outstanding-claim, cooldown and foreign-state guards.
Its result is a read-only eligibility snapshot, not ownership or send permission.
It does not submit, claim, arm, publish, acknowledge or alter the request.

Execute the generated calls.open once in the same native broker task. It
rechecks canonical eligibility through the official command tool, then checks
the live broker binding, retained socket configuration and pinned closure
evidence. A pending or failed helper prevents opening; preserve any returned
helper execution identity rather than repeating the open.

Only proven closure permits joining the old socket's existing close promise.
An exclusive synced replacement-open.once.json is created in the immediate
closed directory before attempting the fresh listener. The in-progress guard
and consumed packet identities remain intact. Never clear globals or remove
an ancestor or immediate-session marker. A failed consumed open is not retryable
through another packet.

Successful replacement creates one new directory, session ID and token, with
the exact queuedResume expectations retained in its descriptor. It starts no
receive window and grants no claim or send permission. A session already
carrying queuedResume is ineligible for another closed-queued replacement.

### Command-ready first, then the existing request's first delivery

After verifying the new open result, launch actual Claude once with this single
foreground command, using the new SESSION and the existing REQUEST:

```bash
node "$ACTIVATION" resume "$SESSION" 1 "$REQUEST"
```

Concurrently, the parent runs the existing event-driven gate:

```bash
node "$ACTIVATION" command-ready "$SESSION" 1 "$REQUEST"
```

Resume takes no replacement prompt or client identity. It checks the bound
queued request, creates a fresh command nonce and announces operation:resume
with the original fingerprint and prompt hash. It neither copies a new prompt
into the queue nor calls queue submit. The lower-level client's resume command
is an internal rendezvous step; do not invoke it separately.

Start calls.receive exactly once only after a complete commandReady:true result
matches the new session, ordinal 1, request and resume expectations. The running
client then verifies native readiness and the exact observed command, rechecks
canonical queued eligibility, and exclusively syncs queued-resume.once.json
before making one socket delivery. Duplicate or expired attempts fail closed.

Execute calls.dispatch once for that accepted delivery. The existing runner
still requires canonical claim and successful arm in that invocation immediately
before its one native send. Eligibility checks do not span native calls and
cannot replace those final guards. If the request gained a receipt meanwhile,
this resume path must not be used to deliver it again.

Keep the active-job wait rules above: supported waits of at most 60 seconds on
the same execution are allowed during real work. Ten minutes is an observation
milestone, not a generation cutoff. A pending result preserves ownership and
requires collect-only recovery, never another queued-resume attempt.

Require a validated published answer, matching successful nonempty actual
Claude terminal result, and independently recorded navigation outcome. Repeat
collection to compare answer hashes without another wake. Do not acknowledge
the preserved A2 request as part of the recovery gate.

Only after A2 completion, successful terminal validation and verified reusable
transport may the preauthorized distinct Job B use ordinary rendezvous with
ordinal 2 on the same listener. Its command-ready gate must succeed before the
next receive. Ordinary submit of the bound A2 ID is collection only. Another
request is blocked until the bound recovery has completed; no automatic queue
drain, idle rearm, replacement ID or second listener is introduced.

Retain both old and new session evidence on failure. Published answers remain
independent of navigation success. Session expiry stops admission, not an
already armed Pro operation. These local fixtures do not qualify native Stop,
crash, suspension, hidden model accounting or continuous availability.
## Active-parent requirement and expired unobserved retries

For graceful rotation to a fresh owner task, see
[listener owner handoff](../../../docs/listener-owner-handoff.md).
For replacement from a new task, first follow [new-owner-handoff.md](new-owner-handoff.md) and its `resident-takeover-packet` path. The cooperative handoff below is a fallback only when the old task is active.

Only the current native owner may execute the `resident-handoff-packet` after
joining its graceful stop. Direct credential-only CLI handoff is rejected.
The native packet checks the current task, the target's existence, and the
post-join barrier. It preserves request evidence and does not expand recovery.

An open socket does not wake the native Codex parent. For EVERY request, the parent must run the matching `command-ready` waiter and, only after its successful result, execute the existing native `calls.receive` exactly once. Client publication alone cannot complete this sequence. This is not an autonomous desktop service.

For an intact expired original command whose ordinal has neither `ready-N.json` nor `command-observed-N.json`, an explicit retry is available while the same listener's lease remains live:

```text
parent: command-ready SESSION ORDINAL REQUEST ATTEMPT_ID
client: rendezvous-retry SESSION ORDINAL REQUEST ATTEMPT_ID
parent: after successful command-ready, execute calls.receive once
```

Use a fresh 32-character lowercase hexadecimal ATTEMPT_ID, shared by parent and client. Start the parent waiter before the client. The retry reuses the original request ID, client session and exact prompt bytes, not new caller content. Existing authority and queue checks still apply. Queued-resume listeners are excluded.

Original records remain untouched. Each retry gets an exclusive `command-N-retry-ATTEMPT_ID` directory and corresponding JSON record. All attempts compete for the SAME permanent `command-observed-N.json`. Only the client whose complete record matches that claim may proceed. Socket receive numbering is unchanged.

An unused retry ID may be tried after another unobserved timeout, but no attempt directory or claim may be removed or reused. A claim written after its deadline fails closed and remains consumed. Any observed or ready ordinal is ineligible, including one whose previously authorized native receiver is delayed. Inspect existing state instead of retrying a possible delivery.

This does not extend leases, recover a used listener closed with `lease_expired`, or change closed-listener replacement eligibility. `closed-packet` still requires the existing zero-event `idle_expired` proof.
## Resident opt-in (candidate)

This section overrides the manual per-request instructions only for an explicitly generated resident packet. Finite workflows above retain their own eligibility rules and are not a fallback around resident ownership. Resident mode removes the session-age lease, not pickup, reply or active-observation budgets, the 64-admission ceiling, or any at-most-once guard.

### Setup and replacement

One canonical `resident-owner.json` contains a generation, trusted parent/worker,
enrollment evidence, one session binding, and at most one in-flight invocation. It is updated under
the same lock as claim/arm/receipt mutations. Session files are evidence, not
replacement permission.

First enrollment is a one-time maintenance operation, not normal startup.
Ordinary startup returns `blocked: enrollment_required` when no owner exists.
An authorized operator must establish either a genuinely fresh deployment or
physical quiescence of ALL legacy executions affecting this canonical authority.
A new chat, empty queue, absent socket/global, elapsed time or an approval alone
does not establish that fact. Do not enroll the known unresolved legacy runtime.

Record accepted observations and their sources in a private JSON evidence file:
`kind` (`fresh_deployment` or `legacy_quiescence`), trusted `parent`, `worker`,
resolved `config_dir`, `state_dir`, inspected `implementation` revision,
`observations`, and explicit `authorization`. Keep competing legacy launches
stopped throughout this maintenance transition. The operator verifies physical
facts; the helper only validates bindings and hashes, not the truth of a claim.

After that qualification, the operator runs `pro-dispatch resident enroll` with
one JSON argument containing `generation:0`, a fresh `owner` identifier,
trusted `parent` and `worker`, `evidence_file` and `evidence_sha256`. It copies
the accepted evidence into canonical ownership. It neither opens nor sends.
Never manufacture evidence to clear this gate, delete the owner record or
reenroll to reset its generation. Worker reconfiguration/reset/purge while
enrolled is unsupported in this patch.

For both ordinary startup and replacement, after actual Claude's filesystem
preflight, the six native capability checks and authorization, generate:

```sh
node "$ACT" resident-packet "$PARENT" "$PARENT" "$WORKER" "$CLIENT_ROOT"
```

Resolve ACT and the helper to the same inspected physical candidate/install.
PARENT is the actual dedicated native task, not this development task.
CLIENT_ROOT is required, physical, private and shared with the actual client.

Execute decoded `calls.open` once, verbatim through outer `functions.exec`.
It compares the captured generation atomically. Its result is `ready`,
`collect_only`, `busy` or `blocked`. Only the first two permit executing that
packet's `calls.serve` once, in the same native task/turn. Preserve its cell ID
and follow the packet's `lifecycle` contract. Opening alone does not mean ready;
`ready` here is the canonical start disposition, not listener availability.

Resident service owns the dedicated Codex turn until shutdown. A yielded
`functions.exec` cell is not a detached daemon: ending the owner turn can stop
its native calls even if the cell still appears to run. Give the session path
and client command in commentary once. Do not send a final response while the
cell is running. After each yield, automatically call `functions.wait` with
that actual returned cell ID and `yield_time_ms:60000`, repeating in the same
turn until completion. This requires no user confirmation, scheduled task,
routine progress message, or manual reactivation. Waits supervise the original
execution; they do not poll the worker or publish new requests. Never replay
open/serve when a wait yields or its result is lost.

The first serve output, `resident_supervision_required`, repeats this contract
and explicitly does not assert readiness. Clients still require the current
bound `resident-status` admission observation and atomic rendezvous claim.
Only finalize after the original cell completes and its cleanup is checked.
If the host cannot keep this owner turn active, report resident service as
unavailable on that host; do not advertise indefinite detached availability.

The owner reserves an invocation before accepting a request, and retains it
through helper calls, native work, transport completion and cleanup. Replacement
is refused while that reservation exists. A stale generation cannot claim/arm.
No timer, answer completion or idle chat releases ownership. Only the original
final continuation releases after all its operations and cleanup have joined.
Unknown native work, unknown helper identity, pending helpers and unconfirmed
cleanup retain the reservation. Do not manually run begin/end to clear a lock.

Once safely replaceable, the same startup recipe preserves durable request IDs:
queued or partially prepared work resumes its first-send path; a post-arm or
completed-but-unpublished receipt is collect-only. Completed answers remain
collectable without blocking startup or requiring acknowledgement first.
Original session files remain intact. The resident-only closed/failed/terminal
packet variants and retained-object cancellation recipe are removed. Finite
legacy recovery above is unchanged and cannot bypass resident ownership.

### Service and failure

Serve handles gates, receive, dispatch and cleanup. Do not issue those operations
separately while it runs. Idle native observations are bounded to 25 seconds
inside the same evaluation. A non-self-renewing 60-second detector withdraws
admission if that evaluation disappears. It is disabled during accepted Pro work
and is never takeover permission. This is not reboot auto-start or a daemon.
The wait loop above is the operational continuation mechanism. It cannot
guarantee survival of host cancellation, app closure, task limits, or kernel
loss. Never replace it with a self-renewing native timer: that can leave a fresh
readiness marker without any execution able to dispatch the claimed request.

The service does not navigate or forward routine messages to development tasks.
Claude collects the canonical answer. Report actionable failures only.
Ten minutes remains an observation point, not a send retry or generation limit.

On failure, capture pending-helper identity before decoding transport, attempt
to join that known execution, stop/join admission, preserve a private failure
record and close the owned socket. A failed continuation admits no next request.
Keep the first failure in `resident-failure.json` and final operation identities
in `resident-failure-final.json`, even when the detector recorded failure first.
An unresolved helper or native call retains ownership even if the socket closes.
A fully joined failure may release, but its receipt still determines whether
recovery is pre-arm or collect-only. Cleanup never resets that receipt.

Native envelopes and operation identities remain in private evidence before
decoding. Only the evidenced send acknowledgements `{}` and
`{"threadId": <bound worker>}` are accepted; acknowledgement is not proof of
submission. Publication still uses the existing native validator. The final
`resident_closed` summary reports failure separately from immutable transport
audit reasons. Forced host termination remains unqualified.

### Bounded actual-Claude qualification

Authorize actual Claude once to perform the sequence below. The parent starts open/serve, preserves evidence and automatically joins the original cell with bounded `functions.wait` calls throughout qualification. Do not prepublish B or have the parent impersonate Claude.

Use a new private mode-0700 `PROOF` directory, mode-0600 prompt files, fresh request IDs `A` and `B`, a client session ID `CLIENT`, and distinct unpredictable response tokens. Pass the matching candidate paths and returned `SESSION` to Claude.

### Client access and send permission

Resolve the physical installed skill and helper paths first. Candidate tests do
not update installed links. Do not hand Claude an installed-skill prompt while
the native owner is using a different candidate.

Before opening a listener, have the actual Claude session run:

```sh
node "$ACT" client-preflight "$CLIENT_ROOT"
```

`CLIENT_ROOT` must already be a physical, owner-only directory legitimately
accessible to that Claude session. The check creates, reads and removes only
its own temporary probe. It never queues, arms or sends. The native owner can
pass this same directory as the required final argument to `resident-packet`;
the new listener is then created beneath it,
instead of an unrelated Codex temporary directory. Keep the directory path
short enough for the platform's local socket limit.

If this filesystem preflight is denied, stop before opening and report the
exact denial. A successful preflight does not authorize a send.

#### After Codex restart or laptop reboot

Automatic startup is unsupported. After restarting Codex or the laptop, open a
Codex task and paste this single owner-side instruction:

> Restore the codex-pro-dispatch listener using the installed skill. Inspect
> canonical state first. Preserve any armed or indeterminate request as
> collect-only and never resend it. Use `resident recover-start` only when its
> schema-3 ownership, canonical eligibility, and bound operator-evidence checks
> pass; otherwise recovery remains collect-only. Preserve unresolved receipts
> and evidence. Use the documented fresh-open path only when eligible, and
> report ready only after a live waiter is observed. Do not submit a test request.

This prompt invokes the existing resident recovery recipe; it does not authorize
a daemon, polling, automatic startup, a new request, or another send attempt.
If the installed skill cannot prove the recovery or fresh-open prerequisites,
it must report the exact blocker instead of altering canonical state. A live
waiter is required before reporting ready, not before guarded recovery begins.

After it passes, the authorized native owner opens once and starts serve once
as described above, in the same actual task/turn. Opening alone does not send,
but serve can process eligible requests within the authorized consultation
scope. Keep that owning execution active; a socket alone is not readiness.

Readiness is enforced, not assumed: while the native admission call waits for ordinal N
it keeps the empty owner-only directory `waiting-N.<session ID>` beside
`session.json` with a one-second heartbeat on its mtime. A resident
`rendezvous` (including retry and queued resume) claims that marker by
atomically renaming it into its command ticket, and only that claimed
directory ever becomes a ticket; the waiter retires the marker with an atomic
`rmdir` when its idle observation ends. The next observation is entered by the
same serving evaluation, not by an independent heartbeat. Exactly one side wins: if the waiter has retired, the
client fails with `Resident is not waiting for ordinal N` and creates no
command, ticket, queue entry or send, so the same request ID can be submitted
once a waiter exists; if the claim landed first, the waiter notices its marker is gone, stays
responsible for that publication (a stop is honored at the next ordinal), and
fails the residence with the claimed ticket kept if no command follows within
five seconds, whether or not a stop was ever published. A marker older than five
seconds, or one that is not an owner-only directory, is `Stale resident
readiness` and is never claimed. Losing only the outer evaluation stops marker
refresh and retires unclaimed readiness through the native detector. Losing the
native runtime itself has no proven teardown guarantee: a leftover marker remains
evidence and blocks another waiter. Existing closure and canonical checks still
apply; no timer authorizes deleting that marker or claiming ownership.

Use the returned `SESSION` to give Claude the exact rendezvous command below.
Claude must obtain any required command permission before executing it. Do
not require approval for that exact command before its session path exists.
The pickup deadline starts inside rendezvous, not while an approval is pending.
Do not prepublish a request to obtain approval. If permission is denied or the
test is cancelled, do not run it through another route; report the denial and
use the documented resident stop. Preserve any request evidence if execution
may already have begun. Never resend an uncertain request.

Do not change permission modes or settings on the user's behalf, disable
checks, or substitute interpreters. Approval for client-preflight is not
approval for rendezvous. A successful Auto-mode run proves only that invocation;
verify the saved rule and its scope before claiming it persists across sessions.

### Requests

Before preparing a new resident delivery, inspect the exact owner-provided path:

```sh
node "$ACT" resident-status "$SESSION"
```

This read-only snapshot checks the canonical owner's bound directory, session ID
and descriptor hash. Open records that binding once using internal `bind-session`,
before serve. Do not call it manually to adopt a folder. Startup reads old v1
owners but writes v2; older resident-owner operations reject v2 rather than
ignoring its binding. This is not a global two-worker migration fence.
`admission_observed` includes the currently observed ordinal;
it is not send permission or a guarantee that rendezvous will win. Rendezvous
uses internal `resident admit` to recheck the binding, prompt and queue, claim
the waiter, and publish the handoff under the same canonical lock as replacement.
If replacement wins first, admission creates nothing. If admission wins, the
complete handoff precedes replacement; the old owner's later `begin` still fails.
Partial handoff files are preserved, never rolled back or reused. This operation
does not queue, arm or send, and requires no extra operator approval or closure proof.
`busy` means the current authority has work or an invocation reserved. Wait for
that request's outcome using its existing watcher, not another listener.
`retired_owner`, `unbound_owner`, `descriptor_changed` or `installed_helper_mismatch` means
this path cannot be selected for a new delivery with this installed helper.
Do not retrofit bindings into old descriptors. An older live owner's requests
must drain through its existing runtime; coordinate an ordinary owner restart
before switching clients to this candidate. Do not hot-edit its files.
`not_waiting`, `stale_readiness`
and `malformed_preserve` do not prove that the owning execution or Pro stopped.
Keep the evidence and use the documented owner path, never delete a marker or
release ownership based on this snapshot. All results report
`sendAuthorized:false` and `replacementAuthorized:false`.

After explicit `worker-pool activate` maintenance, this candidate can report
`maxConcurrentRequests` equal to the configured pool size (one or two) for one
listener. Opening two listeners or keeping two chats still does not raise capacity. Live
native overlap, app reopen collection, laptop reboot, and legacy migration are
separate qualification gates; passing fixture tests does not enable them.
Ordinary reopen is collector-only. Automatic startup remains unsupported.
Explicit `resident recover-start` after operator physical-quiescence evidence
fences the old generation. A published `transport-audit.json` remains the
graceful closure proof and is still required when present. A crash or reboot
that left no audit is recoverable by that same maintenance action: inspect and
preserve the bound session, including leftover readiness markers and a stale
`wake.sock`, refuse a still-listening socket, and keep every possibly-sent
receipt collect-only. Missing audit, PID, or socket never authorizes a resend.
Live native overlap, app reopen collection, laptop reboot, and legacy migration
remain separate qualification gates.

1. Claude creates `PROOF/A.txt` asking for exactly its A token, then runs:

   ```sh
   node "$ACT" rendezvous "$SESSION" 1 "$A" "$PROOF/A.txt" "$CLIENT"
   ```

   Preserve the actual returned answer and timestamps. Verify and acknowledge A using the block below.

2. After A's verified answer, wait 180 seconds using one external sleep or equivalent independent elapsed time. Continue only the owner's automatic `functions.wait` calls on the original cell. Do not separately call native tools, issue parent gates or publish B during this interval. Then Claude independently creates `PROOF/B.txt` with the B token and runs:

   ```sh
   node "$ACT" rendezvous "$SESSION" 2 "$B" "$PROOF/B.txt" "$CLIENT"
   ```

   Verify and acknowledge B identically. Both requests must use the original `SESSION`.

For each answer, set `RID` and `EXPECTED` to its request ID and token. Run these collections only after the returned answer, before acknowledgment:

```sh
python3 "$HELPER" queue collect "$RID" > "$PROOF/$RID.collect1.json"
python3 "$HELPER" queue collect "$RID" > "$PROOF/$RID.collect2.json"
python3 "$HELPER" status "$RID" > "$PROOF/$RID.status.json"
python3 - "$PROOF" "$RID" "$EXPECTED" "$PARENT" "$WORKER" <<'PY'
import hashlib, json, sys
from pathlib import Path
d, rid, expected, parent, worker = sys.argv[1:]
d = Path(d)
q = [json.loads((d / f"{rid}.collect{i}.json").read_text()) for i in (1, 2)]
a = json.loads((d / f"{rid}.status.json").read_text())["assignment"]
for v in q:
    assert v["ok"] is True and v["state"] == "published"
    assert (v["request_id"], v["parent_task_id"], v["worker_conversation_id"]) == (rid, parent, worker)
    assert v["sent_verified"] is True and v["send_authorized"] is False
    assert v["send_may_have_occurred"] is True
    assert v["answer"]["verification_level"] == "bounded_native_summary"
raw = [v["answer"]["payload"].encode("utf-8") for v in q]
assert raw[0] == raw[1] and raw[0].decode("utf-8").strip() == expected
assert (a["assignment_id"], a["parent_task_id"], a["worker_conversation_id"]) == (rid, parent, worker)
assert a["status"] == "complete" and a["no_resend"] is True
assert type(a["submission_count"]) is int and a["submission_count"] == 1
assert a["submission_observed"] is True and a["outbound_prompt_verified"] is True
assert a["sent_prompt_sha256"] == a["wrapped_prompt_sha256"]
assert a["result_marker_validated"] is True
assert a["native_collection"]["worker_id"] == worker
print(rid, hashlib.sha256(raw[0]).hexdigest())
PY
```

Require successful exit and retain the printed hash. Only then run:

```sh
python3 "$HELPER" queue acknowledge "$RID"
```

Do not require source-byte or generation-finality verification flags to be true. Preserve the actual Claude transcript, repeated collection bytes, receipt, hashes and original serve output.

### Stop and acceptance

After verified B, Claude requests stop once:

```sh
node "$ACT" resident-stop "$SESSION"
```

This publishes an exclusive stop request; its successful return alone is not proof of closure. Retrieve the original serve cell using `functions.wait`, with each retrieval bounded to 60 seconds. Require serve completion, the matching `transport-audit.json` with `resident_stopped`, and absent `wake.sock`.

The audit must contain exactly A then B accepted/finished pairs; canonical receipts and native traces must show one submission/send each. Confirm unchanged session identity, no third readiness, no idle worker reads, no manual parent gates, and automatic same-cell supervision through B completion and shutdown. Preserve wait results to prove the owner turn remained active; cell existence alone is insufficient.

Time-box qualification observations to 25 minutes including the quiet interval. A ten-minute checkpoint is observation, not a generation deadline. If an answer remains unresolved, do not proceed to B or resend. Mark qualification inconclusive, request stop, preserve the owning cell and use the documented collect-only recovery. Stop drains active runner work under its existing budgets; it does not cancel Pro generation or promise immediate shutdown.

Successful qualification proves this measured two-request run only. It does not prove indefinite residence, survival across host shutdown, or automatic recovery after a stopped owner. Finite activation remains available under its existing eligibility checks, never by resetting globals, deleting evidence or bypassing a consumed marker.
