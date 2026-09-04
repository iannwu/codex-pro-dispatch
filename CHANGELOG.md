# Changelog

All notable changes to Codex Pro Dispatch are documented here.

## [1.2.1] - 2026-09-04

### Fixed

- Ignore a stale native read-back from a different assignment without changing
  the current receipt; wait for and verify the matching message without resending.

## [1.2.0] - 2026-09-04

### Added

- Continue long ChatGPT Pro results in bounded same-worker chunks and reassemble
  the accepted payload bytes exactly before returning them to the parent task.
- Require an exact per-assignment end marker and reject explicitly truncated,
  incomplete, malformed, or out-of-sequence responses.

### Changed

- Treat 10,000 UTF-8 bytes as a response-generation target rather than a hard
  acceptance gate, so small model overshoots do not force another dispatch.
- Bound continuation chains to 16 chunks, preserve one durable receipt per
  native message, and keep assembly private and transient.

## [1.1.0] - 2026-08-26

### Added

- Public plugin packaging with a validated manifest and repo marketplace catalog.
- Per-invocation semantic host-capability preflight and fail-closed `doctor` behavior.
- Diagnostic categories and hashes plus automatic redaction of raw diagnostic bodies left by earlier releases.
- Explicit compatibility contract, release-evidence policy, secure transient-file rules, and community contribution guidance.

### Changed

- Correct the delivery contract from exactly-once to at-most-one native send attempt with exact read-back verification.
- Install personal skills under the documented `$HOME/.agents/skills` location.
- Safely migrate an owned pre-v1.1 skill symlink while refusing unowned paths.
- Require explicit invocation until the native workflow has a stable public compatibility record.
- Reframe the landing page around requirements, honest limitations, safety, and agent/human quick starts.
- Document the macOS desktop-only boundary, connector requirements, remote-code visibility, and common first-run failures.
- Document optional OpenAI request IDs in the receipt data inventory.

### Security

- Document break-glass force deletion and the recovery guarantees it destroys.
- Require private mode-`0700` temporary storage, mode-`0600` files, and cleanup on all exits.
- Preserve source-install idempotency when a custom `CODEX_HOME` resolves to the agents skill directory.

### Fixed

- Keep the desktop-only health contract test portable across macOS and Linux CI.

## [1.0.0] - 2026-08-26

### Added

- Public Codex skill for delegating one bounded assignment to a dedicated, user-confirmed ChatGPT Pro worker in the official combined desktop app.
- Durable no-automatic-resend state machine with assignment markers, exact native read-back verification, same-worker continuation, and exact parent-task restoration.
- Collect-only recovery for timeouts, app restarts, ambiguous submissions, and stale or mismatched responses.
- Independent parent-side verification requirements for worker-reported branches, commits, tests, and CI results.
- Source-visible installer, uninstaller, local state helper, security policy, acceptance matrix, and macOS/Linux CI.

### Fixed

- Preserve native unusual-activity HTTP 403 details and optional OpenAI request IDs instead of collapsing them into a generic `systemError`.
- Enforce a fixed 30-minute cooldown before any fresh assignment after that HTTP 403, including after user-authorized abandonment of the affected receipt.

### Incident anchor

- The v1 safety patch was prompted by a native send failure whose visible `systemError` masked an unusual-activity HTTP 403. PR #2 made the failure explicit, kept the assignment collect-only, and added a cooldown that cannot be bypassed by abandoning the receipt or changing workers.

[1.2.1]: https://github.com/iannwu/codex-pro-dispatch/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/iannwu/codex-pro-dispatch/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/iannwu/codex-pro-dispatch/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/iannwu/codex-pro-dispatch/compare/v0.1.0...v1.0.0
