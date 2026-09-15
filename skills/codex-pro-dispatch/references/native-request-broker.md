# Finite-session native broker release candidate

Status: 1.3.0-rc.1 source candidate. Actual-Pro candidate qualification and
installed actual-Claude smoke remain release gates. Installation is not activation.

## Bounded current-state inspection

Native runner, activation and client checks use `status --current`. Its complete
JSON is bounded to 8192 UTF-8 bytes and excludes the historical assignments array.
All historical receipts and cooldowns are still validated under the canonical
lock. Corruption or oversized current state fails closed, never via truncated
JSON or an empty active slot. Plain `status` retains explicit full-history output.
This change neither claims a queued request nor reopens a closed listener.

## Authority and activation

Use one canonical configuration/state authority, one configured user-confirmed
Pro conversation, and one active native operation. Another task's unresolved
assignment or outstanding claim blocks takeover. Pro02's consultation exception
does not designate a production worker.

The trusted initial native instruction binds helper path, canonical paths,
worker ID, owner-parent ID, session limits, and permitted tool sequence.
External requests supply consultation data and stable request IDs, not native
commands or destinations. Client session metadata is not parent identity.

Use only the reviewed parked runner/socket and official native controls.
Do not use the excluded experimental request/native-broker commands, scheduled
activation, heartbeat, private endpoints, app restart, or permission changes.
Do not improvise a session-start instruction from the standalone skill.

## Client interface

The operator supplies SESSION, the physical private directory of an opened
native socket session. Opening a socket is not arming a dispatch. ORDINAL is the
next receive number, starting at 1. REQUEST is stable; CLIENT_SESSION is
informational and never selects the native parent.

PROMPT must be a physical absolute path with no symlink components, including
macOS /tmp or /var aliases. Its immediate parent must be owned and mode 0700;
the UTF-8 file must be owned, regular, single-link, and mode 0600. File mode
alone is insufficient. Create a separate private input directory rather than
changing permissions on an existing lifecycle/shared directory. Resolve only
that newly created directory to its physical path, then place the prompt there.

```bash
CLIENT="$HOME/.agents/skills/codex-pro-dispatch/scripts/parked-client.mjs"
ACTIVATION="$HOME/.agents/skills/codex-pro-dispatch/scripts/parked-activation.mjs"
node "$ACTIVATION" rendezvous "$SESSION" "$ORDINAL" "$REQUEST" "$PROMPT" "$CLIENT_SESSION"
node "$CLIENT" "$SESSION" collect "$REQUEST"
```

Actual Claude runs rendezvous once as one foreground command. It validates
storage/UTF-8, snapshots exact prompt bytes privately, announces command-ready,
waits for native readiness, and invokes the existing client once without another
model turn. It does not claim or arm. Core request validation remains authoritative.
The parent must use the command-ready gate in native-activation.md before receive.
Do not use the old separate ready-then-submit sequence.

Rendezvous records and the private prompt snapshot remain in the session
directory for evidence/recovery. Acknowledgement of the queue does not delete
these separate artifacts. Remove only owned session artifacts after required
preservation and confirmed inactivity; never delete attempt markers to retry.

The JSON state is authoritative, not exit zero alone. Queue acceptance means
local storage, not a Pro send. A published answer must retain its verification
fields. Socket output cannot replace the Python broker's answer.

After durably saving an answer, acknowledge explicitly:

```bash
node "$CLIENT" "$SESSION" acknowledge "$REQUEST"
```

Acknowledgement removes retained bodies, not identity/fingerprint tombstones.
Keep the same ID and original content across retries. After possible arming,
never retry submission as a new send or create a replacement ID automatically.
An explicitly requested recovery operation is:

```bash
node "$CLIENT" "$SESSION" observe "$REQUEST"
```

Observe is collect-only for existing post-arm work. Do not call it periodically
from model turns. Slow-Pro snapshot observation belongs inside one awaited,
bounded native orchestration; exhaustion means pending, not cancellation.
Ten minutes is an observation point, never a generation cutoff.

## Closed listener with an untouched queued request

A queued status alone does not authorize another delivery. Use the separate
closed queued-session procedure in native-activation.md only when the immediate
listener has a complete matching closure audit, its socket is absent, and the
canonical eligibility check proves the exact retained request has no dispatch
receipt. Do not use observe for queued work or silently relax ordinary rendezvous
to accept an existing request.

After that explicitly authorized replacement succeeds, actual Claude uses:

```bash
node "$ACTIVATION" resume "$SESSION" 1 "$REQUEST"
```

SESSION is the new listener directory; REQUEST is the original queued ID.
No new prompt, replacement request or queue submission is made. The existing
fingerprint, client identity and raw prompt hash remain authoritative. The parent
must obtain the matching command-ready result before starting one receive.

Replacement and resume have separate exclusive attempt markers. Preserve both,
including after timeout or failure. Resume eligibility never grants send
permission: the existing native runner must still claim and successfully arm
once before its first send. Any post-arm state remains collect-only.

After validated recovery and successful actual-Claude terminal completion,
the same listener may handle the preauthorized distinct Job B through its next
ordinary command-ready/rendezvous sequence. This exception neither retries an
old send nor starts an idle heartbeat or a general retry service.

## Availability and navigation

Advertise only a measured finite session window. Expiry stops new admission,
not an already armed operation. Retain the session descriptor and compatible
helper so durable collection can still work after the listener closes.
Stop/crash/suspension recovery and continuous availability are not qualified.

The trusted runner navigates to the recorded parent by exact ID. It does not
independently verify foreground state or preserve intervening user navigation.
Agree that fixed-parent policy before live activation. Keep navigation failure
separate from a durably published answer; never resend to repair navigation.

## Packaging and reversible source-link transition

Use an immutable reviewed checkout outside disposable test directories.
The installer requires Python and Node, creates only owned source links, and
does not install dependencies, configure a worker, or start a native session.
It refuses another checkout's links. Do not add a force-link workaround.

Before a real transition, record both existing link targets and revisions,
preserve state, and resolve incompatible active assignments/claims with their
owner. Run the old checkout's plain uninstall, then the candidate installer.
Do not use --purge-state during a transition.

Rollback reverses those owned-link operations using the preserved checkouts.
It does not roll back receipts, undo sends, or erase queue records. The older
v1.2.2 CLI lacks queue commands; do not downgrade while its inability to honor
broker state could permit conflicting work. Retain the compatible helper for
collection/recovery until the operation is legitimately resolved.

Results remain bounded_native_summary with source_bytes_verified:false and
generation_finality_verified:false. Validate substantive answer claims separately.
Neither an idle chat nor a completed enclosing turn proves generation finality.

Follow [trusted native activation](native-activation.md).
Qualification and production ownership remain deployment gates.
## Activation routing

The default workflow below remains finite: manual request-specific gating, a two-hour lease and finite pickup deadlines. For explicitly requested setup-once operation, use the [resident opt-in](native-activation.md#resident-opt-in-candidate) instead. Do not mix the two workflows.

Resident availability means the original awaited outer `calls.serve` evaluation is still running. An open app or socket alone does not establish availability. Live resident qualification remains pending; synthetic tests do not establish continuous or indefinite service.
