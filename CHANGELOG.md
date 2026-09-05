# Changelog

All notable changes to Codex Pro Dispatch are documented here.

## [1.2.0] - Unreleased

### Added

- Schema-v2 logical dispatch receipts with ordered turns, explicit `inline`,
  `chunked`, and `artifact` modes, separate immutable result/delivery/restoration
  state, and a terminal per-turn `response_rejected` state.
- Lossless read-only chunk transport: strict JSON payload framing, full serialized
  message limit, decoded canonical-LF byte hashing, chain validation, durable
  private spool/journal, materialization, and restoration-gated cleanup.
- Explicit artifact contracts and manifests plus private bare-Git verification of
  exact commit/tree/blob identity, protected refs, moving-base behavior, and
  public-retention acknowledgement.
- Schema-v1 migration/projection behavior, strict collection adapter contracts,
  v1.2 host acceptance matrix, security analysis, and adversarial migration,
  collection, chunk, artifact, and state-machine tests.

### Changed

- Replace response-only unresolved completion with mandatory native evidence.
  Evidence now retains raw/normalized message and outer truncation, exact worker/
  message association, and item-level finality provenance.
- Make observation identity independent of `observed_at`; an identical reread is
  idempotent, while changed accepted content/source is immutable conflict.
- Keep artifact selection explicit per assignment. `auto` is intentionally absent;
  only exact chunk control or proven truncation can prepare a read-only child.

### Security

- Fail closed on omitted truncation unless a helper-allowlisted, version-scoped
  adapter contract proves omission behavior; no caller boolean can override it.
- Preserve original-assignment no-resend across recovery children, legacy state,
  cooldowns, and parent-restoration retries.
- Keep result/prompt bodies out of JSON receipts and validate stored artifact
  manifests before any Git object operation.

## [1.1.1] - 2026-09-02

### Fixed

- Close the marker-bearing truncated-prefix defect: unresolved receipts can no
  longer complete from response text alone.
- Require one strict, versioned native collection-evidence envelope bound to the
  configured worker, exact submitted user message, stable assistant message,
  completed generation, and explicit collection-integrity fields.

### Security

- Record raw and normalized truncation evidence without a caller-controlled
  `--truncated` switch. Missing truncation remains unknown and fails closed unless
  a future helper-owned, allowlisted adapter contract explicitly proves otherwise.
- Keep observations at different timestamps idempotent by excluding `observed_at`
  from immutable content identity.

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

[1.1.0]: https://github.com/iannwu/codex-pro-dispatch/compare/v1.0.0...v1.1.0
[1.2.0]: https://github.com/iannwu/codex-pro-dispatch/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/iannwu/codex-pro-dispatch/compare/v1.1.0...v1.1.1
[1.0.0]: https://github.com/iannwu/codex-pro-dispatch/compare/v0.1.0...v1.0.0
