# Native acceptance matrix

The unit tests verify state, markers, privacy, and no-resend behavior. They cannot exercise the official app's live native conversation controls.

Run this matrix against the exact release-candidate commit before marking a release stable. Record the app version/build, macOS version, native capability names, candidate SHA, and results in a redacted release receipt.

The [v1.2.2-rc.1 recovery receipt](releases/v1.2.2-rc.1-recovery.md) records
the exercised recovery subset. It does not waive any A–L stable-release gate.

The [v1.2.2 release receipt](releases/v1.2.2-acceptance.md) records the expanded
run and the maintainer's release-specific acceptance of unverified clipboard
preservation. That exception is not an unconditional A–L pass and does not
change this checklist for subsequent releases.

The 2026-08-24 development run is historical evidence only. It was not performed against the current release candidate and must not be represented as the current release result.

## A. Setup

### A0. Host capability preflight

- Confirm the six semantic capabilities in [compatibility.md](compatibility.md).
- Run `pro-dispatch doctor --route direct` without an assertion and confirm it fails closed.
- Run `pro-dispatch prepare --parent-task-id acceptance-preflight --assignment-id acceptance-preflight` without `--native-controls-confirmed` and confirm it fails before reading stdin.
- After the semantic check, configure the direct worker and run `pro-dispatch doctor --route direct --native-controls-confirmed`.

Expected:

- a missing native capability stops before worker or assignment state is written
- unasserted direct `doctor` exits nonzero and reports `native_controls_confirmed: false`
- unasserted `prepare` exits nonzero without reading the prompt and creates no assignment receipt
- the asserted health check succeeds only when local state is healthy

### A1. Worker configuration

- Select two dedicated Codex chats, normally `Pro Dispatch - Codex 01` and `Pro Dispatch - Codex 02`, distinct from all current and historical Claude bridge workers.
- Choose the model and reasoning effort in each.
- Resolve their stable conversation IDs in preference order.
- Run `pro-dispatch worker direct-pool-set --conversation-id <codex-01-id> --conversation-id <codex-02-id> --confirm-worker --native-controls-confirmed`.

Expected:

- `pro-dispatch worker direct-show` returns both exact IDs in `workers` with `model_confirmation: user-confirmed-worker`; scalar `worker` is null and `worker_count` is two.
- The route registry is mode `0600` and retains separate direct bindings and bridge destinations.
- The conversation is reported as user-confirmed; the model is not machine-verified.
- Existing legacy scalar worker and bridge pool configuration remain unchanged.
- Configure the separate Claude pair through supported listener start/replace with both worker flags. Report each route’s result separately.
- Dispatch and collect one identifiable native request in each of the four chats, checking each outbound request/result association. Verify Codex direct while the listener is stopped. Record live concurrency separately from fixture concurrency and report host limitations.
- Existing singleton direct setup through `worker direct-set` still passes its full lifecycle.

### A2. Resident owner liveness

For resident candidates, use a disposable native session before any live send:

- Before opening, run the one-time no-dispatch qualification in the Listener
  task. Confirm the installed synchronous Stop hook blocks an attempted final
  response while the original qualification cell is pending, and that the
  Listener itself exposes the declared executor and continuation controls.
  A missing hook, changed turn, or missing control must leave the listener
  unavailable without creating admission or sending a request.
- Await repeated idle admission observations in the same serving evaluation.
  Verify no independent waiting process, model turn, chat read or navigation.
- Stop renewing admission while the native runtime remains alive. Verify that
  the owner-loss detector removes unclaimed readiness, closes the socket and
  preserves a zero-event failure audit. It must not reopen or submit anything.
- Exercise cleanup while receive is waiting. Verify close finishes without a
  deadlock and preserves the observed-command and ready records.
- Confirm an accepted slow request is not cancelled by the admission detector.
- While a request is active, verify every yield waits on the original serve
  cell and an attempted owner finalization remains blocked until joined cleanup.

Record native-host results separately from synthetic tests. Nonrenewal does not
prove host cancellation, kernel-loss cleanup or recovery after a laptop reboot.
Missing closure evidence is never proof of physical termination or permission
to take over. Reuse requires the runtime’s explicit exclusion or reconciled-send
proof; uncertain sends remain blocked.

### A3. Canonical resident ownership

For ownership candidates, qualify first enrollment from trusted fresh-deployment
or legacy-quiescence evidence. An empty queue, missing globals or a stale socket
does not qualify an existing deployment. Record the accepted evidence and exact
candidate implementation. Do not enroll merely to unblock this test.

- Race replacement against reservation. Exactly one wins; a replaced owner
  cannot admit, claim, arm or send. Once reserved, replacement stays blocked
  through the final native cleanup, even after the answer is complete.
- Verify joined pre-arm failures resume the same request, and joined post-arm
  failures remain collect-only. Queued requests, partial claims and completed
  but unpublished answers must survive replacement.
- Verify uncertain native calls, pending helpers and unconfirmed cleanup retain
  ownership and their evidence. Only the original joined continuation releases.
- Run a real Claude roundtrip, collect twice, compare preserved answer hashes,
  then acknowledge. Verify no foreground navigation or routine progress messages.

Report synthetic and native results separately. These tests do not establish
automatic startup after a reboot or safe migration of an unfenced legacy owner.

Local source tests may exercise collector-only recovery, crash-without-audit
recover-start, and a two-slot scheduler against fixtures. They never mark native
two-worker overlap, app reopen collection, laptop reboot, or legacy migration
PASS. Those gates stay NOT RUN or BLOCKED until a disposable live qualification
is authorized.

## B. One-turn roundtrip

Run this direct roundtrip with the Claude bridge listener unavailable. No owner
acquisition, listener startup, or Stop qualification should be needed. Canonical
receipt integrity and account cooldowns still apply to both routes.

Submit:

```text
Reply with exactly:
CODEX_TO_CHAT_PRO_OK_7319
```

Expected:

- receipt is durably `armed` before the single native send
- one user message is submitted
- exact response is collected
- the exact parent Codex task is restored
- clipboard is unchanged

## C. Thread-not-loaded recovery

1. Submit one marked assignment.
2. Cause or reproduce `thread not loaded` during collection.
3. Run recovery without resending.
4. Open the exact worker by ID and collect the existing response.

Expected:

- original assignment appears once
- existing response is recovered
- assignment reaches `complete`

## D. Same-worker continuation

Turn 1:

```text
Remember this exact fact for my next message:
ORCHID-CLOCK-7319

Reply exactly:
STORED_7319
```

Turn 2 in the same conversation:

```text
What exact fact did I ask you to remember in my previous message?

Reply with only the fact.
```

Expected:

- Turn 1 returns `STORED_7319`
- Turn 2 returns `ORCHID-CLOCK-7319`
- both use the same conversation ID
- each has a unique assignment ID
- parent task is restored after each collection

## E. At-most-once uncertainty

Interrupt or terminate the app after `pro-dispatch arm` and before submission recording, including the boundary immediately after the native send.

Expected:

- after restart the assignment remains `armed`; recovery explicitly records `indeterminate` if the native-send outcome is still unknown
- the durable receipt already has `no_resend: true` before the native send
- no automatic resend occurs
- recovery opens the saved worker and checks for the existing response
- an interruption before transport may produce zero sends; the assignment still remains collect-only

## F. Marker isolation

Place a stale assistant response in the worker before a new assignment.

Expected:

- stale response is rejected
- a mismatched assignment marker is rejected
- a native user-message read-back for another assignment is rejected as
  `stale-readback` before mutating the current receipt; the matching message
  can later verify without a resend
- only the expected marker completes the assignment

## G. GitHub worker proof

Give the worker one disposable branch assignment.

Expected:

- the ChatGPT worker performs the write through its own GitHub connector
- worker returns branch and commit SHA
- parent independently verifies the remote commit and unchanged protected refs
- parent does not substitute its own write

## H. Foreground behavior

While another app is frontmost, run a several-minute dispatch.

Expected behavior:

- submission and waiting do not interrupt foreground work
- collection is deferred when focus state is available
- collection may briefly foreground ChatGPT after the user returns
- clipboard remains unchanged
- exact parent task is restored

## I. Unusual-activity HTTP 403 cooldown

On a disposable armed assignment, simulate recording the exact native unusual-activity HTTP 403 and its request ID with `pro-dispatch unusual-activity`.

Expected:

- the receipt reports HTTP status `403`, error kind `openai-unusual-activity`, the request ID, and `cooldown_seconds: 1800`
- the original assignment remains collect-only and is never resent
- after user-authorized abandonment, a fresh `prepare` fails with `CooldownError` until the recorded 30-minute deadline
- `status`, `doctor`, and `recover` expose the cooldown details
- preparation succeeds after the deadline without an automatic send

## J. Private transient files

Run a complete dispatch containing a unique, non-secret sentinel in the prompt and response.

Expected:

- every transient prompt, read-back, response, and reason file is created inside one mode-`0700` temporary directory
- transient files are mode `0600`
- cleanup runs after exact parent restoration and on simulated failure
- the sentinel is absent from the checkout and the helper's config/state directories after cleanup

## K. Legacy diagnostic redaction

Place a disposable pre-v1.1 receipt containing unique, non-secret sentinel values in the legacy `last_error` and `reason` fields, then run `pro-dispatch doctor --native-controls-confirmed`.

Expected:

- `doctor` reports the number of migrated receipts
- each raw diagnostic body is replaced durably with a category and SHA-256 hash
- the sentinel is absent from status output and the on-disk receipt
- corrupt receipts still produce structured unhealthy JSON and a nonzero exit

## L. Long-result continuation

Ask the worker for a deterministic result longer than 30,000 characters.

Expected:

- the initial response requests continuation without returning partial content
- each continuation uses the same worker and a new assignment ID
- accepted chunks are appended byte-for-byte in order and fsynced privately
- a complete response above the 10,000-byte generation guideline is accepted
- the final assembled bytes match the requested result exactly
- an explicit truncation report, missing footer, wrong index, or partial local
  write stops collection without resending or advancing
- the exact parent task is restored and transient files are removed

## Release gate

The candidate is ready to be called stable only when A through L pass on the exact candidate commit. H may retain the documented brief foreground collection limitation, but missing native capabilities, clipboard changes, duplicate submission, cooldown bypass, wrong-thread collection, stale response acceptance, incorrect chunk assembly, sensitive temp-file residue, unredacted legacy diagnostics, or failed parent restoration are blockers.

## Guarded takeover contract regression coverage

The isolated takeover suites are `tests/test_resident_takeover.py` and
`tests/test_parked_takeover.mjs`. They use temporary authorities and synthetic
native tools, including real helper and runner execution, without live sends.

| Contract | Regression evidence |
| --- | --- |
| T1, T11 | Clean takeover byte audit, exact owner keys, real command admission; request files remain unchanged. |
| T2, T3 | Every uncertain receipt state remains collect-only; old and new arm attempts refuse; active/working refuse unchanged and idle commits. |
| T4 | Prepared, receiptless claim, and reserved queued variants; injected crashes before/after abandon, release, and end converge. |
| T5 | Barrier at real lock acquisition produces one commit and one stale loser. |
| T6 | Old claim, begin, arm, publish, submitted, indeterminate, ambiguous, release, abandon, end, session bind, collector open, and handoff refuse unchanged. |
| T7 | Pending observation retains the slot; late answer publishes with one verified submission and no new arm. |
| T8, T9 | Classification and marker-generation matrices, association failure, orphan and legacy receipts, malformed host and packet evidence, and public takeover refusal. |
| T10 | Prepared and armed requests retain their worker slots through cancellation and collect-only classification. |
| T12 | Real runner collects pending inherited work without sending, then arms and sends once on the idle sibling; cooldown and single-worker cases block. |
| T13 | Single-worker cancellation frees capacity; the real runner sends only the fresh request once. |
| T14 | Operation denial, mixed flags, stored-input hash checks, operator abandon/release/end, idempotent completion denial, exact ancestry across successive takeovers, and history-only denial. |
| T15 | Stale marker archival, current marker exclusion, stale arm/handoff/recovery readers, and collector `marker_stale`. |
| T16 | Generated call order, native identity, real helper commit, exact evidence bytes, replay, current native temp directory, and chmod-before-open failure. |

The desktop release harness R1-R5 remains a separate live qualification gate.
These isolated tests do not claim to prove desktop host lifecycle guarantees.
