# Native broker availability: Stage 1 boundary

Status: BLOCKED for autonomous reusable availability.
Reviewed behavior: 1.3.0-rc.1-handoff-prepared-r2.
Evidence: operator-reported qualification, not a new checkout audit.

## Available contract

The broker can process real requests while its exact native parent execution
and session are available. Packet generation is not activation. A healthy
doctor, existing socket path, configured worker, or surviving OS process does
not establish a runnable native receiver.

Start the finite receive window only after actual command readiness. Use
deterministic waiting inside that active request. Do not spend model tokens
checking an empty mailbox or automatically rearm an expired idle receive.

The approximately 45-second idle receive window is not a Pro generation
deadline. Ten minutes is an observation milestone, never cancellation,
abandonment, replacement, or permission to resend.

## Safety and recovery

Keep canonical request, parent, worker and receipt identities unchanged.
Names such as 01 and 02 are labels, not routing identities.

Only the existing native path may durably arm and then attempt one send.
After an uncertain arm or possible send, recovery is collect-only.
Retain complete available paired native evidence; incomplete or truncated
evidence cannot establish successful submission or answer validation.

A prepared request is not a sent request. Its recovery requires the existing
explicit, parent-bound preflight and one-shot procedure. Do not automatically
replay its previous listener, packet, command or socket.

App/process restart or loss of parent execution means receiver availability
is unknown. Reconcile existing state before another operation. Never clear
receipts, force-close active delivery or infer cancellation from chat archival.

Respect existing cooldowns. Slow or rate-limited Pro remains unresolved work.
An archived or recreated chat never substitutes for the original worker ID.

## What remains unqualified

No unattended request-triggered activation of the exact desktop task has
been demonstrated after idle expiry or parent finalization. Automatic A/B
use without per-request setup therefore remains unqualified.

Required evidence is a supported host activation contract followed by actual
Claude A, an idle boundary longer than 45 seconds, and independent Claude B.
No manual receive/rearm, copied session path, empty model wake or replacement
request may supply the missing activation.

For each request, retain one-send evidence and two identical validated
collections with matching answer hash. A stopped observation is inconclusive,
not failed Pro generation. Preserve all pending ownership.

## Qualification gate

Do not run another live send merely to re-prove the working transport.
First identify the supported event subscription, exact-task destination,
native-tool authorization and host lifecycle guarantees.

Then author a bounded A/B qualification against that actual interface.
No such interface or live sender script is introduced by this document.
