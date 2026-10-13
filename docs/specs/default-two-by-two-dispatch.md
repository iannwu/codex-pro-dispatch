# Default 2×2 Pro dispatch

Status: specification amendment, not implemented or live-verified.
Date: 2026-10-12.
Base: independent-native-routes.md, implemented at commit 3d17e0b337a7d68742c6080f1cf12bbed359e309 in PR #31.

## Purpose and scope

Make the normal setup two ChatGPT Pro destinations for native Codex and two separate destinations for the Claude bridge. Users select the four chats once. Subsequent dispatches choose an available chat automatically within the correct pair. A stopped Claude listener must not prevent Codex from dispatching directly.

This amends the base spec wherever it specifies one direct worker or one unresolved direct assignment. All other route isolation, native evidence, receipt ownership, account cooldown, and at-most-once send requirements remain in force.

| Caller | Default chat labels | Path | Capacity |
| --- | --- | --- | --- |
| Native Codex | Pro Dispatch - Codex 01; Pro Dispatch - Codex 02 | Direct native dispatch, no listener | Two concurrent jobs, one per chat |
| Claude | Pro Dispatch Claude 01; Pro Dispatch Claude 02 | Existing queue and native listener | Two concurrent jobs, one per chat |

Labels guide setup only. Exact native conversation IDs determine routing. Do not embed this installation’s IDs in shipped defaults. Selected Pro model identity remains subject to the existing native capability and user-confirmation contract.

“Default” means the recommended new-install setup and normal selection behavior. Existing valid one-worker installations remain supported without forced reconfiguration. Chat creation and native model selection remain explicit setup steps, not promises made by the CLI.

## Verified gap

Inspected the installed source at the base commit:

- `routes.validate`, `set_direct`, `reset_direct`, and `status` use a single `direct_worker`. `direct_bindings` is retained history, not an active worker pool.
- `core.active_assignments` rejects more than one unresolved direct assignment.
- `core.prepare_assignment` selects one direct binding and rejects any existing direct assignment, even on a different destination.
- `cli.py` exposes singleton direct-set/show/reset behavior.
- The Claude bridge already supports two workers. It needs enrollment of the chosen pair, not a second queue or a concurrency redesign.

Creating or renaming chats alone therefore cannot implement this setup.

## Runtime contract

### Registry and migration

Introduce registry version 2. Replace `direct_worker` with ordered `direct_workers`, containing zero, one, or two existing-format full binding records. Preserve `direct_bindings`, `bridge_workers`, `legacy_standalone_workers`, and `listener_tasks` and their validation rules. Active conversation IDs must be unique, and each active record must occur in retained binding history.

Version 1 remains readable. Normalize its null worker to an empty active list, or its worker to a one-element active list, without rewriting files on status/read operations. On the next successful configuration mutation, atomically write version 2 under the existing canonical state lock. Preserve every historical binding ID and receipt byte; migration never grants send authority or resets a job.

Current and historical direct and bridge destinations remain disjoint in both configuration directions. Reusing a previously registered destination within its original route is allowed under existing replacement rules. Do not infer a route from a chat title.

### Configuration surface

Add `worker direct-pool-set` for atomic pair configuration. Accept exactly two ordered `--conversation-id` arguments and two corresponding `--label` arguments, with the existing `--confirm-worker` and actual caller native preflight requirements. Pair labels are optional, defaulting to the Codex labels above; supplying labels requires two.

For replacement, repeated `--expected-conversation-id` arguments must exactly match the current ordered active list. Omission means the caller expects an empty list. Validate the complete expected and requested lists under the lock before writing. An identical request with matching expectations is a read-only success, including while work is active. Any actual configuration change requires all unresolved direct work to be settled. A failed write leaves the complete previous registry intact.

Keep `worker direct-set` compatible for zero/one-worker configuration. When two workers are configured it must return `direct_pool_configured` and point to direct-pool-set; it must not silently collapse the pair. `direct-reset` clears the entire current direct list only when there is no unresolved direct work, retaining history. No force option or manual JSON-edit recovery is added.

`direct-show`, direct status, and direct doctor expose `workers` as the authoritative ordered list. Preserve scalar `worker` for zero/one-worker clients (null or the single record); with two workers it is null and `worker_count=2` makes that intentional. Update all first-party consumers to read `workers`. Similarly, scalar `active_assignment` is populated only when there is exactly one active direct job; `active_assignments` is authoritative.

The Claude pair is configured through the supported listener start/replace workflow. Never edit owner or pool files directly. Pair setup is two independent route configuration operations, not a four-chat transaction: report each route’s configuration result separately. Failed bridge activation must not undo or block a valid Codex pair.

### Deterministic selection and reservation

For a fresh direct request, validate canonical receipts and shared cooldown, enumerate active direct jobs, select the first available binding in configured order, and persist the prepared assignment while holding the same existing state lock. Selection outside the lock followed by a later write is insufficient.

There is no round-robin cursor or new queue. Prefer 01 when both are free, otherwise use 02. Configuration order, not title sorting, defines preference. Never borrow a Claude chat, start a listener, or open a browser when the direct pair is busy.

Every status in the existing `ACTIVE_STATUSES` set reserves its exact conversation ID, including prepared, armed, and uncertain work. Validate at most one unresolved direct job per conversation ID and at most two unresolved direct jobs overall, including historical bindings. Two different binding records for the same conversation do not create additional capacity.

When both direct slots are occupied and no shared cooldown applies, return `direct_pool_busy`, including their assignment IDs, conversation IDs, and statuses. Do not send, queue, or silently retry. A free current worker is unusable if the global two-job direct limit is already reached.

Direct status always returns `worker_count`, `workers`, `active_assignments` (including retired bindings), `available_workers` (unoccupied configured bindings), `remaining_capacity`, and `active_cooldown`. Remaining capacity is the smaller of the number of available configured workers and two minus the number of active direct jobs. It describes capacity before cooldown, not permission to send. For zero configured workers it is zero. Preserve `ok` and `local_ok` as true whenever at least one current direct worker is configured, including a two-worker pair with scalar `worker=null`, busy capacity, or cooldown; otherwise both are false.

Report `availability=not_configured|busy|ready` independently of `active_cooldown`. A busy worker must not make a different available worker appear unavailable. Validate registry and canonical receipt integrity first. For a valid fresh request, missing configuration takes precedence; with configuration present, shared cooldown takes precedence over capacity/busy errors. A continuation uses its retained binding even without current configuration, then checks shared cooldown before its original-worker and global-capacity limits. Include capacity details when reporting cooldown so simultaneous busy and cooldown conditions remain visible. Malformed evidence is an integrity error, never a busy or cooldown diagnosis.

### Continuation, recovery, and isolation

Every receipt retains immutable `execution_route`, `direct_binding_id`, worker conversation ID, and parent task identity. All outbound readback, result collection, and completion are tied to that receipt, regardless of current defaults.

An explicit continuation of a completed direct receipt uses its original retained binding, including after removal from the current pair, as in the base spec. It counts toward the global two-job limit. If that exact chat is busy, return `direct_worker_busy`; do not redirect the continuation to the other chat. Existing ownership and completed-prior-receipt checks still apply.

An armed job with uncertain send outcome remains collect-only. Restarting Codex must reconstruct occupancy from receipts; it must not clear a slot, resend the prompt, or move it to another chat. Once authoritative evidence permits the existing terminal transition, that chat becomes available again. This amendment does not introduce automatic resend or a timeout-based unlock.

Direct preparation, arm, collection, configuration, status, and ordinary task finalization must continue to work with a stopped or malformed Claude owner/pool/session when shared registry and receipt evidence are valid. An uncertain Codex job on 01 must not block new work on free 02. Two occupied Codex chats must not consume Claude capacity, and vice versa.

Retain the shared account-level 403 cooldown and canonical receipt integrity checks across both routes. These are deliberately shared evidence, unlike listener lifecycle readiness. Preserve the existing registered-listener-only Stop hook behavior.

## Acceptance tests

1. Public CLI configures two direct bindings atomically; duplicate IDs, wrong counts, mismatched expected lists, and current/historical cross-route overlap fail without partial writes.
2. Repeating identical setup preserves binding IDs and succeeds without mutation. Actual replacement/reset while any direct job is unresolved fails and preserves evidence.
3. Version-1 empty and singleton registries normalize correctly. Successful mutation writes version 2 while existing terminal and active receipts retain valid original bindings. Read-only commands leave version-1 bytes unchanged. Fault-injected writes preserve the previous complete file.
4. Three concurrently released direct prepare processes yield exactly two persisted winners on distinct configured chats and one busy result. Lock contention must not create two reservations on one chat.
5. Both winners independently complete the full prepare, arm, submitted, pending, collect, and complete lifecycle. Completion of one does not mutate or release the other.
6. Prepared or indeterminate work on 01 reserves 01 while fresh work can use 02. Both occupied produces explicit direct_pool_busy with no extra receipt or send.
7. Continuation stays on its original binding. Busy original chat fails even when the other chat is free. Continuation on a retired binding consumes capacity, and duplicate occupancy is checked by conversation ID across binding history.
8. Restart between preparation, arm, and outbound recording retains occupancy and original collection identity. An uncertain send is never replayed or switched to another worker.
9. Two direct jobs and two bridge jobs coexist in canonical fixtures. Each route validates and mutates only its own jobs. A stopped or malformed bridge lifecycle does not prevent either direct worker from completing, including ordinary Stop finalization.
10. Global cooldown blocks both routes; malformed canonical safety receipts remain diagnosed. Neither case is mislabeled as a listener prerequisite. A fixture with two active direct receipts and unexpired cooldown returns cooldown for new admission while status exposes busy availability and the cooldown independently. Public CLI status assertions cover zero, one, and two configured workers, including Boolean/scalar compatibility and every required capacity field.
11. CLI help, setup skill, standalone examples, diagnostics, and packaging tests describe 2×2 as the default. A singleton installation still works. Tests assert behavior through public commands, not documentation strings alone.
12. After implementation and review, bind this installation’s exact four IDs and run separately reported live native acceptance: dispatch and collect one identifiable request through each configured chat, verify each result belongs to its own outbound request, and verify Codex direct while the listener is stopped. Record concurrency verification separately; fixtures do not prove live host scheduling. Report any host limitation rather than claiming live success from unit tests.

## Implementation and validation sequence

1. Amend registry parsing/configuration and compatibility tests.
2. Replace global singleton admission with per-conversation reservation and a two-job route limit. Preserve continuation and recovery semantics.
3. Update CLI consumers, diagnostics, skill/setup guidance, and regression fixtures.
4. Run focused tests, then the existing full Python and Node suites plus compilation, shell syntax, CLI help, and packaging/pin checks.
5. Use the previously requested Sol 6.1 xhigh builder and fresh Terra high review through native Codex agents. Resolve material review findings and rerun affected tests before installation.
6. Install the reviewed revision, configure native IDs, and record the live checks above separately from fixture results.

Likely files: `src/codex_pro_dispatch/routes.py`, `core.py`, `cli.py`; `tests/test_direct_routes.py` and relevant CLI/skill tests; `skills/codex-pro-dispatch/SKILL.md`, `references/standalone-dispatch.md`, and any native-protocol/setup examples that encode singleton assumptions. Inspect other registry consumers before changing the schema. Do not redesign the queue or listener lifecycle to accommodate direct workers.

Estimated scope: a focused follow-up to PR #31, with migration, concurrency, and receipt recovery as the main review risks. No delivery-time guarantee is implied.

## Rollback and exclusions

Before installation preserve the reviewed prior binary and private configuration snapshot. Prefer a forward fix after version-2 state is written. Never run a version-1-only binary against version-2 canonical state, and never restore an old snapshot over newer receipts. Downgrade requires a separately verified lossless conversion after work is settled; reset/delete is not rollback.

Out of scope: sharing destinations across caller families, more than two workers per route, a direct waiting queue, new transports or daemons, model switching, listener recovery redesign, automatic chat creation, changes to account limits, and deleting retained history. This document does not claim any runtime change, configuration, live test, merge, or release has occurred.
