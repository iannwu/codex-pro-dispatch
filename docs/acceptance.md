# Native acceptance matrix

The unit tests verify state, markers, privacy, and no-resend behavior. They cannot exercise the official app's live native conversation controls.

Run this matrix before merging v0.1.

Latest live result: PASS on 2026-08-24 for the durable pre-send protocol at commit `eaff5feb83d0e78bf92af1120696c0c9445b9b34`. The follow-up reason-file hardening affects CLI input handling only and does not alter the native send, collection, or restoration protocol.

## A. Setup

### A1. Worker configuration

- Create one dedicated Chat conversation.
- Visibly select Pro.
- Resolve its stable conversation ID.
- Run `pro-dispatch worker set --conversation-id <id> --confirm-pro`.

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

## E. Exactly-once uncertainty

Interrupt or terminate the app after `pro-dispatch arm` and before submission recording, including the boundary immediately after the native send.

Expected:

- after restart the assignment remains `armed`; recovery explicitly records `indeterminate` if the native-send outcome is still unknown
- the durable receipt already has `no_resend: true` before the native send
- no automatic resend occurs
- recovery opens the saved worker and checks for the existing response

## F. Marker isolation

Place a stale assistant response in the worker before a new assignment.

Expected:

- stale response is rejected
- a mismatched assignment marker is rejected
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

Expected v0.1 behavior:

- submission and waiting do not interrupt foreground work
- collection is deferred when focus state is available
- collection may briefly foreground ChatGPT after the user returns
- clipboard remains unchanged
- exact parent task is restored

## Release gate

v0.1 is ready to merge only when A through G pass. H may retain the documented brief foreground collection limitation, but clipboard changes, duplicate submission, wrong-thread collection, stale response acceptance, or failed parent restoration are blockers.
