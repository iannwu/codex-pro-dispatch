# Native acceptance matrix

The unit tests verify state, markers, privacy, and no-resend behavior. They cannot exercise the official app's live native conversation controls.

Run this matrix against the exact release-candidate commit before marking a release stable. Record the app version/build, macOS version, native capability names, candidate SHA, and results in a redacted release receipt.

The 2026-08-24 development run is historical evidence only. It was not performed against the current release candidate and must not be represented as the current release result.

## A. Setup

### A0. Host capability preflight

- Confirm the six semantic capabilities in [compatibility.md](compatibility.md).
- Run `pro-dispatch doctor` without an assertion and confirm it fails closed.
- Run `pro-dispatch prepare --parent-task-id acceptance-preflight --assignment-id acceptance-preflight` without `--native-controls-confirmed` and confirm it fails before reading stdin.
- After the semantic check, configure the worker and run `pro-dispatch doctor --native-controls-confirmed`.

Expected:

- a missing native capability stops before worker or assignment state is written
- unasserted `doctor` exits nonzero and reports `native_controls_confirmed: false`
- unasserted `prepare` exits nonzero without reading the prompt and creates no assignment receipt
- the asserted health check succeeds only when local state is healthy

### A1. Worker configuration

- Create one dedicated Chat conversation.
- Visibly select Pro.
- Resolve its stable conversation ID.
- Run `pro-dispatch worker set --conversation-id <id> --confirm-pro --native-controls-confirmed`.

Expected:

- `worker show` returns the exact ID.
- The config file is mode `0600`.
- The model is reported as user-confirmed, not machine-verified.

## B. One-turn roundtrip

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

- Chat Pro performs the write through its own GitHub connector
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
