# Architecture

## Components

```text
pro-dispatch
  Python orchestration, locking, receipts, timeout policy, response delta

cgpt-list-apps
  Swift helper that lists candidate native ChatGPT/OpenAI applications

cgpt-read-app
  Swift Accessibility reader for the configured app and conversation window

cgpt-send
  Swift Accessibility sender with draft protection and pasteboard restoration

cgpt-wait-idle
  Swift waiter that requires new conversation activity and stable idle state

pro-dispatch serve
  Optional terminal-hosted Unix-socket daemon for Codex Desktop callers that do not inherit Accessibility permission
```

## Invocation modes

Direct mode runs the Swift Accessibility helpers in the caller process tree. Daemon mode sends the assignment over a mode-`0600` Unix socket to a terminal-hosted `pro-dispatch serve` process. Both modes execute the same dispatch transaction and share the same lock and receipt directory.

## Dispatch transaction

1. Acquire the process-wide nonblocking dispatch lock.
2. Read a baseline snapshot from the configured native app and conversation window.
3. Refuse to continue if the target input contains a draft.
4. Create a unique assignment ID and private receipt before submission.
5. Wrap the prompt with the assignment ID and an explicit no-fabricated-tests reminder.
6. Send the prompt exactly once.
7. Record the `sent` state before waiting.
8. Wait for new conversation activity, a larger message-group boundary, an idle Send state, and stable visible text.
9. Read a second snapshot.
10. Extract only assistant text whose group index is at or after the baseline boundary.
11. Persist the response and hashes atomically.
12. Return JSON or raw text to the caller.

## Timeout contract

Timeout is an indeterminate delivery result, not permission to resend. The prompt may still be processing in the native app. The tool records the assignment and exits without another send. `pro-dispatch collect` later reads from the original baseline boundary and never submits a message.

A helper failure during submission is also treated as indeterminate because the Send action may have occurred immediately before the process failed. The tool records `send_indeterminate` and does not retry.

## Response boundary

The baseline uses the number of Accessibility message groups visible before submission. New assistant text is selected by group index after that boundary, while thinking markers are excluded. This prevents older responses from being returned as the current assignment result.

The native app may change its Accessibility tree or virtualize long content. The deterministic extractor is tested, but exact long-code and unified-diff fidelity is a mandatory live acceptance test rather than a claim established by CI.

## Concurrency

All direct and daemon dispatches use the same `flock` lock. The daemon is threaded so a second caller reaches the lock and receives a bounded `BusyError` rather than silently queuing behind a long assignment.

## Local state

- config directory: mode `0700`
- config file: mode `0600`
- state directory: mode `0700`
- receipts: mode `0600`
- lock file: mode `0600`
- Unix socket: mode `0600`

Writes use a temporary file in the destination directory, `fsync`, and atomic replacement.

## Trust boundaries

ChatGPT Pro is an implementation worker, not a source of verified repository truth. During the transport-only phase, the calling Codex agent remains responsible for applying changes, reviewing diffs, running tests, and enforcing the completion gate.
