# Resident admission belongs to the serving execution

## Goal and scope

Claude submits through one resident runner without per-request setup, idle model
polling, focus changes, or duplicate sends. Preserve the existing guarded reopen
paths. Automatic startup after reboot and general orphan takeover are not part
of this patch. User authorized the direct patch and Terra High review loop.

## Evidence and decision

The old `resident-next` subprocess refreshes readiness independently of the
outer serving evaluation. Killing that evaluation can leave a fresh marker with
no consumer. Killing the helper leaves a stale marker without native closure.

Alternatives: keep the independent helper (retains the bug); add a cross-writer
takeover protocol (withdrawn as unsafe and too broad); move the existing wait
into bounded native calls (selected, reviewed by Terra High).

The same outer evaluation awaits repeated native idle observations. Each wait
retires its marker atomically after an idle observation; a winning client claim
retains its ticket and existing publication deadline. No model call is involved.
A native owner-loss detector is renewed only by authenticated calls from that
evaluation. It never renews itself or authorizes replacement. Its delay is not
a session lifetime or a deadline for Pro. Stop detection before running an
accepted job; that job retains its existing at-most-once and recovery rules.

Owner loss aborts admission and closes the socket before joining its writer and
persisting failure once. Every later native action checks the failed owner. An accepted or
observed request still blocks zero-send replacement. Kernel loss remains an
unqualified recovery case, not permission to infer an empty mailbox.

A returned stop keeps the detector armed until cleanup completes. If the outer
caller disappears in that gap, closure records failure with the stop preserved.
Each post-claim helper authority read has a 10-second bound. Readiness still
retires between observations; a client may receive a pre-publication refusal
during that short gap. No automatic retry or second watcher is added.

## Acceptance

- Idle observations repeat without subprocesses, model turns, sends or navigation.
- Losing the outer continuation retires readiness and closes the native socket.
- A paused claimant cannot claim after retirement; a winning claim is preserved.
- A closed zero-send residence can use existing same-runtime recovery once.
- Slow accepted work is not cancelled by the idle owner detector.
- Existing duplicate, wrong-owner, post-arm and ambiguous-evidence tests pass.
- Terra High reviews the final diff and tests; native-host qualification is
  reported separately from synthetic transport tests.

## Office Hours outcome

DONE_WITH_CONCERNS: scope is reduced to the evidenced lifecycle failure.
The native host's cancellation behavior must be tested, not inferred from unit
tests. No new supervisor, queue, authority directory, or automatic restart.

## Verification

Terra High's initial final code review passed with no actionable P1/P2 findings.
Automated checks: all 162 Python tests and 199 Node tests passed, including the
claim-at-observation-boundary regression. Skill validation passed.

Two isolated actual native-runtime checks passed: a 25-second idle observation
followed by nonrenewal closed admission at the 60-second detector boundary;
cleanup released a waiting receive while preserving its ready/observed records.
Both recorded zero socket events, touched no canonical queue and sent nothing.
These checks do not qualify forced host cancellation, kernel death or reboot.
The installed candidate and production runner remain unchanged.

Cursor Fable's independent review passed but identified a native post-claim
verification gap and the returned-stop cleanup gap. Follow-up native qualification
exercised the actual post-claim status and queue reads successfully in the desktop
runtime (422 ms), using canonical authority read-only and a disposable command
fixture. No ready gate, queue write or send occurred. A second actual native
check withheld cleanup after a returned stop: the detector closed the socket
at 60,003 ms, preserving the stop with failure evidence and zero events.
All 200 Node tests and 162 Python tests pass after the follow-up; skill validation,
Python compilation, shell syntax and whitespace checks pass. Native Terra High's
follow-up review passed with no actionable P1/P2 findings and 3/3 targeted tests.
