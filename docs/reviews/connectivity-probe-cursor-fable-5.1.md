# Cursor Fable 5.1 review of the connectivity probe spec

Date: 2026-09-05. Requested Cursor model: `claude-fable-5-1-thinking-high`.
Cursor reported: `Claude Fable 5.1 300K High`.
Transport: Cursor CLI, authenticated session, terminal `result` event with
`subtype: success`, `is_error: false`, and nonempty review; process exit 0.
Duration: approximately six minutes. This is an independent review requested by
the user, not the completed /spec quality gate or authorization to implement.

Reviewed draft SHA-256: `bb5e431a0d0fe9021059fb17fcdfd3f2a8f20d17d305155bbde743475c305b6c`.
The draft is unchanged by this review. The reviewer used a disposable snapshot
of the uncommitted v1.2 candidate and relevant tests/docs.

## Parent assessment

- Accept the reservation concern: terminal probe outcomes should release worker
  reservation independently of navigation metadata. A transient navigation error
  is recoverable, so “un-clearable” below is too strong; persistent failure still
  blocks production without an appropriate normal release path in the draft.
- Accept the missing unusual-activity transitions, input-budget mismatch,
  timeout/release wording, and explicit observation-result taxonomy. These need
  concrete spec changes before implementation.
- Qualify the fixture finding: our native read already showed the user ID equal
  to the turn ID and its assistant in the same turn. The inspected `x_i` also
  appends assistant items to the preceding user turn. Add a sanitized fixture
  for the implementer; do not relax matching to a different turn on speculation.
- Do not adopt the proposed send-only HTTP 403 rule without revision. Existing
  production code has no send-vs-read restriction on armed/recoverable turns;
  genuine unusual-activity responses during native reads should not become a
  route around cooldown protection. Distinguish unusual activity from ordinary
  read/auth errors, and define legal state transitions explicitly.
- The reviewer’s smaller CLI is a useful simplification proposal, not a measured
  40% saving. Test coverage should follow required behavior rather than fixed
  count reductions. A two-turn live read and reuse of the 4 MiB parser bound are
  sensible; recovery pagination should remain bounded and useful for late reads.

## Reviewer output

The completed review below is preserved without its two introductory progress
sentences. Findings are the reviewer's judgments; qualifications above record
where the parent checked them against code and the earlier native observation.

# Review: `docs/specs/connectivity-probe.md` (v1.2 candidate snapshot, 2026-09-05)

Reviewer stance: independent principal-engineer read-through, grounded against `src/codex_pro_dispatch/{core,transport,cli,collection,errors}.py`, `tests/`, `skills/codex-pro-dispatch/SKILL.md`, `references/native-protocol.md`, and `docs/native-read-investigation-2026-09-05.md`. No files modified, no tests run, no network.

## Verdict

**Not ready to implement as drafted — approve after blocking changes.** Executability: **6/10**.

The safety core (shared `state_lock`, arm-before-send permit, nonce-correlated exact match, cooldown union, no production-adapter relaxation) is correct and consistent with the current code. The draft is held back by (a) one reservation rule that creates an un-clearable block on production after a *successful* smoke test or a pre-arm abandon, (b) an undefined `unusual-activity` transition, (c) matcher shape assumptions the cited investigation does not actually record, and (d) scope roughly 2× what a smoke test needs.

## Verified-against-code notes (spec's "Current state" table)

All six citations are accurate: `cli.py:75` `build_parser`, `cli.py:530` doctor branch, `core.py:307-319` `state_lock`, `core.py:322` `save_worker`, `transport.py:1728` `prepare_assignment_v2`, `collection.py:151-162` adapter allowlist. `transport.py:795-796` confirms `_validate_v2` rejects `record_type != "dispatch"`, so the spec's decision to store probes in a separate `probes/` directory rather than `assignments/` is required, not optional — co-locating would make `list_assignments_v2` (`transport.py:1546-1568`) fail every production call.

---

## Prioritized findings

### P1 — Reservation gate on parent restoration creates a block with no non-break-glass exit

**Where:** spec lines 222-223 ("An unresolved probe includes … a reply-observed or abandoned probe awaiting parent restoration"), 216 (abandon only from `prepared/armed/indeterminate`), 95 (`parent-restored` requires `--native-probe-controls-confirmed`), 249-251 (force commands never invoked automatically).

**Proven failure scenarios:**
1. Probe reaches `reply_observed`. Skill attempts restoration; the host navigation tool errors. Spec says "report it separately; the observation remains recorded" (line 59). The probe is now unresolved forever: `abandon` is illegal from `reply_observed`, and the only clearing path is `parent-restored`, which the spec requires to be asserted via the skill's reduced preflight. Every subsequent `pro-dispatch prepare` returns `probe_busy`. The user's *successful* smoke test has disabled production dispatch.
2. `probe prepare` succeeds, process dies, user runs `probe abandon` (no flag needed pre-arm). No navigation ever occurred, yet the abandoned probe still "awaits parent restoration" and blocks production until someone asserts restoration of a parent they never left.

**Mismatch with current code:** production treats delivery as orthogonal to reservation. `DISPATCH_ACTIVE_STATUSES` (`transport.py:85`) is `{prepared, active, recoverable, verifying}`; `complete`/`abandoned` are terminal regardless of `delivery.parent_restoration_status` (`transport.py:4412-4436`). The probe spec is stricter than production here without a safety justification — the reservation's purpose (one unresolved send in the worker) is already satisfied once `reply_observed` or `abandoned` is recorded.

**Exact correction:** Replace lines 222-223 with: "An unresolved probe is one in `prepared`, `armed`, or `indeterminate`. `reply_observed` and `abandoned` are terminal for reservation purposes. `parent_restored_at` and a `parent_restoration_status: not_attempted | restored | failed` field are delivery metadata, never a reservation input." Add `probe parent-restored PROBE_ID --failed` (or `--restored/--failed`) so a failed attempt is durably recorded rather than silently unresolved. Update acceptance criterion 9 to drop "pending restoration" from the reservation list.

### P2 — `unusual-activity` has a CLI command but no state transition

**Where:** spec line 92 (command exists), lines 211-217 (diagram omits it), 240-245 (cooldown semantics only).

**Failure scenario:** Implementer must guess whether a 403 is legal from `prepared` (no send has occurred, so it shouldn't be), whether it moves the probe to `indeterminate`, and whether a 403 encountered on `read_thread` (not on send) counts. Production only records 403 from armed-or-later turns (`transport.py:2257-2301` via `_record_collect_only`, which requires armed/submitted/pending/indeterminate/ambiguous at `transport.py:2187-2200`).

**Exact correction:** Add to the diagram: `armed / indeterminate --unusual-activity--> indeterminate (+cooldown)`. State: "Rejected from `prepared`, `reply_observed`, `abandoned` with exit 4. Only a 403 returned by the native *send* is recorded; a 403 on a read is a tool error handled by read-only recovery." Also state that re-recording from `indeterminate` is the idempotent path that may add a missing request ID (mirrors `transport.py:2272-2278`).

### P2 — Matcher structural assumptions are unverified in the cited evidence

**Where:** spec lines 156-161 ("Its ID must equal the turn ID, as in the inspected ChatGPT projection"; "In that turn require exactly one subsequent `agentMessage`"), 152-153 (`schemaVersion: 1`, `thread.kind: chatgpt`).

**Concern (not proven either way):** `docs/native-read-investigation-2026-09-05.md` records field names `textTruncated`, type/ID/text, and that `x_i` builds synthetic turns "including user-only turns" (line 30) — which suggests a user message and its reply may land in *separate* synthetic turns. The investigation never states user-message-ID == turn-ID nor that the assistant message is in the same turn. If either is wrong, the matcher can never pass; the smoke test fails closed but is useless, and the 14 matcher tests would be validating an imaginary schema.

**Exact correction:** Before "Observation matching", add a "Reference shape" subsection containing one sanitized `read_thread` envelope (IDs replaced, text replaced, structure exact) captured from the inspected build, and state: "The matcher is defined against this fixture; if the live shape differs, the spec is amended, not the matcher relaxed." Change "In that turn" to whichever is true after the fixture is captured, or specify "the first `agentMessage` following the matched `userMessage` in document order, in the same turn or the immediately following turn" only if the fixture shows separate turns.

### P2 — Read budget vs. input bound, and unnecessary surrogate-split exposure

**Where:** spec lines 146-147 (`turnLimit: 10`, `maxOutputCharsPerItem: 20000`), 112 (input bounded to 1 MiB).

**Failure scenario (arithmetic, not speculation):** the dedicated worker's history contains prior production turns with up to 12,000-byte inline results and chunk payloads. 10 turns × ≥2 items × 20,000 chars can reach 400,000 chars before JSON escaping; with non-ASCII escaped as `\uXXXX` that is up to ~2.4 MB. A read of a busy worker can exceed 1 MiB and return `probe_read_invalid` — a false smoke-test failure. Production uses 4 MiB (`collection.py:20`). Separately, the investigation (line 48-52) showed a slice boundary inside an unrelated message can split a surrogate pair and fail the *whole* read; every extra unrelated turn in the window is extra exposure.

**Exact correction:** `turnLimit: 2` for the live loop (the probe is the newest turn), bound input at `MAX_EVIDENCE_BYTES` (4 MiB) by reusing `strict_json_object(raw, maximum_bytes=...)`. Recovery paging: drop the "five pages" clause; allow `turnLimit` up to 10 in a single read for recovery only.

### P2 — Timeout leaves a reservation the user isn't told about

**Where:** spec lines 55-57 (timeout message), 223-224 ("Timeout alone never releases the reservation"), 224-226 (abandon after arm needs `--acknowledge-possible-send` and user authorization).

**Failure scenario:** 120 s elapse with no reply. The user message says only "no message was resent." The user's next production dispatch fails with `probe_busy` and the skill must then ask for authorization to abandon a probe the user thought was finished. This is correct at-most-once behaviour; the gap is that the spec's user-facing contract hides it.

**Exact correction:** Timeout message: "No matching reply observed within 120 seconds. The probe remains collect-only and reserves the worker; no message was resent. Say 'abandon the probe' to release it (this acknowledges a send may have occurred), or ask me to re-check later." Add to acceptance criterion 4: "The timeout report names the release path."

### P3 — Ambiguities that will otherwise be decided ad hoc

- **Observation outcome taxonomy** (lines 156-163, 178-182). "Reject duplicated candidate turns" is ambiguous between no-match (exit 0) and error (exit 7). Define three outcomes: `reply_observed` (exit 0), `no_match` (exit 0, state unchanged), `observation_rejected` (exit 7, `probe_read_invalid`, state unchanged) for duplicate candidates / nontext / truncation-true / wrong worker. `probe_observation_conflict` (accepted IDs differ, or same IDs with different text) → exit 4, state unchanged. Currently "same assistant ID, different text" is unaddressed.
- **`kind: connectivity_probe` on error outputs** (line 124). `main()` (`cli.py:591-601`) emits a fixed error shape without `kind`. Say "all *success* outputs" or extend the error emitter.
- **`--prompt-file` is an output here, an input in production** (`cli.py:108`). Rename to `--prompt-out PATH` to prevent copy-paste misuse.
- **Prompt reconstruction** (lines 262-263, 261-263). State explicitly: "`observe` reconstructs prompt and expected reply from the nonce in the probe ID and verifies both against the stored SHA-256s before matching." The receipt design already implies this; make it a requirement so no one adds a `--prompt-file` to `observe`.
- **Directory fsync primitive** (lines 189-191). `atomic_write_json` (`core.py:154-171`) fsyncs the file, not the directory; `_fsync_directory` lives in `transport.py:2595`. Say "move `_fsync_directory` to `core.py` and call it after `atomic_write_json` in probe arm."
- **"Normal cleanup retains receipts carrying an active cooldown"** (line 245). There is no probe cleanup command, and production non-force purge *does* delete abandoned receipts with cooldowns (`transport.py:4646-4666` only blocks on active dispatch). Either delete the sentence or specify: "non-force `purge` refuses with exit 6 while any probe or production cooldown is active."
- **Lock reentrancy** (lines 236-238). Make the "readers are lock-free" rule a hard requirement with rationale: `state_lock` opens a fresh descriptor per call and `flock` treats independent descriptors as conflicting, so a nested `state_lock` in one process deadlocks. Add a unit test asserting probe readers never acquire the lock.

---

## What is sound

- Reusing the single `state_lock` for probe/production exclusion; checking both registries under it in `prepare_assignment_v2`, `save_worker`, `reset_worker`, `purge_local_state_v2`.
- Separate `probes/` receipt family (required, per `_validate_v2` record_type check).
- Arm consumes the permit durably before the send; recovery never re-grants; crash after arm is collect-only. Matches `arm_assignment_v2` semantics and `AT_MOST_ONCE_SAFETY`.
- Nonce-in-ID correlation with byte-exact reply equality; rejection of substring, user-echo, preview, cross-turn, and timestamp inference; idle as an operational guard only.
- Invariant `generation_finality_verified: false` / `production_collection_verified: false`; no `complete`/`completed` vocabulary; collection allowlist untouched (`collection.py:151-162`).
- Cooldown union with fixed non-restartable 1,800 s deadline, mirroring `transport.py:2272-2301`; abandon does not clear it.
- Body-safety posture and helper-written prompt file (removes agent transcription drift).
- Implementation order (state/coordination before any send path).
- Rollback drain rule (no downgrade with unresolved probe or active probe cooldown) — correct, since old `active_cooldown_v2` cannot see `probes/`.

## Smallest adequate implementation scope

Keep:
- `probe.py`: receipt schema, `prepared → armed → {reply_observed | indeterminate | abandoned}` transitions, lock-free `active_probe()` / `probe_cooldown()` readers, matcher against a committed fixture.
- CLI: `probe prepare`, `probe arm`, `probe observe`, `probe unusual-activity` (cooldown parity is a safety requirement), `probe abandon`, `probe status [ID]`.
- Hooks: `prepare_assignment_v2`, `arm_assignment_v2`, `save_worker`, `reset_worker`, `purge_local_state_v2` (enumerate `probes/*.json`), cooldown union in `active_cooldown_v2`; additive `active_probe`/`probe_cooldown` fields in `status` and `doctor`.
- Skill: one explicit "smoke test" branch before production preflight; `native-protocol.md` short probe section; one README paragraph.
- Tests: ~8 lifecycle, ~8 matcher, ~4 CLI, ~4 cross-workflow (two-process prepare/arm race, production prepare/arm blocked, worker set blocked, cooldown union), 1 live.

Drop or defer:
- `probe recover` (duplicate of `status`), `probe indeterminate` (armed is already collect-only; abandon records the reason hash), `parent-restored` as a reservation gate (record as metadata or in the skill report only), five-page recovery paging, edits to `SECURITY.md`/`docs/compatibility.md`/`docs/acceptance.md` beyond one line each.

Estimated effect: ~40% fewer tests and CLI surface; the 9–13 h estimate becomes ~6–8 h.

## Ready to implement?

**No.** Blocking changes, in order:

1. Redefine "unresolved probe" as `prepared | armed | indeterminate` only; make restoration delivery metadata with a recordable `failed` state (P1).
2. Add the `unusual-activity` transition and its legal source states to the state machine (P2).
3. Commit a sanitized `read_thread` reference fixture and define the matcher against it (P2).
4. Set `turnLimit: 2` for the live loop and raise the input bound to 4 MiB (P2).
5. Rewrite the timeout user message to name the reservation and the release path (P2).

The P3 items can be fixed in the same revision without further review.
