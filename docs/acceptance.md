# Native macOS acceptance matrix

GitHub Actions can typecheck the Swift helpers and run deterministic orchestration tests, but it cannot drive a signed-in ChatGPT desktop session. The following checks must pass on a real Mac before v0.1 is described as operational.

Use one dedicated ChatGPT Classic conversation window and do not manually interact with it during a dispatch.

## A. Installation and targeting

### A1. App discovery

```bash
pro-dispatch apps
```

Expected:

- ChatGPT Classic appears exactly once with a nonempty bundle ID and PID.
- The new unified ChatGPT/Codex app, if running, appears as a distinct candidate or is distinguishable by name and bundle ID.
- `accessibility_trusted` accurately reflects macOS permission.

### A2. Wrong-app refusal

Configure a nonexistent bundle ID and run `doctor`.

Expected: structured failure before any input or Send action.

### A3. Settings-window avoidance

Open ChatGPT Classic Settings alongside a conversation window.

Expected: `doctor` selects the conversation window, not Settings. If more than one conversation window exists, configure `--window-title` and prove the wrong window is refused.

### A4. Draft protection

Type an unsent draft into the worker thread, then run a dispatch.

Expected: `DraftPresentError`; draft unchanged; no new user message; no send receipt.

## B. Roundtrip and fidelity

### B1. Exact nonce

```bash
pro-dispatch smoke --direct --timeout 300
```

Expected: `smoke_verified=true` and exact marker captured.

### B2. Same-thread continuation

Send two prompts where the second asks Pro to repeat a fact supplied only in the first.

Expected: second response demonstrates the same visible thread was used.

### B3. TypeScript block integrity

Ask for a fenced, numbered 20-line TypeScript block with deliberate indentation, quotes, backticks, and blank lines.

Expected: captured output matches the visible response byte-for-byte after newline normalization. No missing line, duplicated line, collapsed indentation, or Copy-button label.

### B4. Unified-diff integrity

Ask for a long unified diff containing multiple files, hunks, context lines, plus/minus lines, and quoted strings.

Expected: capture is complete and ordered. Compare SHA-256 of copied visible response with the receipt response.

### B5. Long-response virtualization

Request a response longer than the currently visible viewport and scroll neither app manually.

Expected: full response is captured. If Accessibility exposes only rendered content, v0.1 is blocked until a response-level Copy fallback is implemented.

## C. Delivery semantics

### C1. Timeout without duplicate submission

Run a realistic prompt with `--timeout 1`.

Expected:

- exactly one user message appears
- receipt status is `timed_out`
- no automatic retry appears later
- `collect <assignment-id>` returns the eventual response without adding a message

### C2. Indeterminate send failure

Interrupt or terminate the sender immediately around the Send action.

Expected: no retry. Receipt is `send_indeterminate`; operator inspects the thread or uses `collect`.

### C3. Concurrent dispatch rejection

Start one long dispatch, then immediately start another from a second shell.

Expected: second exits quickly with `BusyError`; it does not queue and does not send.

### C4. Manual-interaction warning

Manually add a message during an active dispatch.

Expected current v0.1 behavior: result may be ambiguous. Document this as unsupported and require a dedicated untouched worker thread. A future version may add stronger assignment-marker correlation.

## D. Daemon mode

### D1. Accessibility inheritance workaround

Start `pro-dispatch serve` from an Accessibility-authorized Terminal. Dispatch from Codex Desktop with `--daemon`.

Expected: roundtrip succeeds even when the Codex child process lacks Accessibility permission.

### D2. Socket permissions

```bash
stat -f '%Sp %N' ~/.local/state/codex-pro-dispatch/dispatch.sock
```

Expected: owner-only read/write socket permissions.

### D3. No TCP listener

Inspect listening TCP sockets while the daemon runs.

Expected: no listener attributable to Codex Pro Dispatch.

### D4. Stale socket

Terminate the daemon uncleanly, then restart it.

Expected: stale Unix socket is removed only after a failed local ping; a live daemon is never replaced.

## E. Clipboard and localization

### E1. Clipboard restoration

Place text and one non-text clipboard item on the pasteboard, force the paste fallback, dispatch, then inspect the clipboard.

Expected: prior clipboard items are restored.

### E2. English controls

Expected: English `Send` and Stop controls are detected.

### E3. Unsupported locale

Switch to an unlisted locale.

Expected: bounded `Send button not found` failure, no fabricated success. Add the exact localized label with a regression test before claiming support.

## Release gate

v0.1 is handoff-ready for independent code review when CI is green. It is user-ready only after A1-A4, B1-B5, C1-C3, and D1-D3 pass on the target Mac. Any B3-B5 failure is a release blocker because coding assignments require lossless response capture.
