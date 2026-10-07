# Independent direct Codex and Claude bridge dispatch

Status: APPROVED FOR IMPLEMENTATION, user requested Astra medium implementation and Terra high review on 2026-10-07.
Base: 512a3c9ea29a6b5f1989eb1d9ee2b71b9d781024, the installed release and PR #28 head. This is a dependent change, not a replacement for #28.

## Context and outcome

Direct Codex has native ChatGPT tools. Claude needs a Codex listener to execute those tools. The helper currently makes resident ownership global: ordinary prepare, arm, receipt updates, active-assignment validation and Stop supervision depend on the listener. A stopped or malformed bridge therefore blocks an otherwise usable direct Codex caller.

Implement independent routes with disjoint worker conversations. Native Codex uses one explicitly configured direct worker. Claude retains its existing one/two-worker pool, invocation and generation protocol. The direct route must prepare, arm, record outbound readback, collect, recover and finish its native task while the bridge is unconfigured, running, occupied, closed or has malformed lifecycle metadata. Real account cooldowns, invalid canonical receipts and unresolved work on the same destination remain blocking evidence.

The user authorized coding and review. Do not configure a live ChatGPT destination or send a live test prompt as part of this implementation. The new direct destination is an explicit one-time configuration choice, never the stale scalar worker or an inferred title.

## Verified current state

| Component | Current coupling | Required boundary |
| --- | --- | --- |
| core.prepare_assignment and _save_assignment | resident.guard runs globally | Guard from immutable receipt route |
| core._transition | any pool requires arm-for-send | Ordinary arm valid for direct receipts |
| core.active_assignments | all receipts must fit pool, global capacity two | Validate direct and bridge occupancy independently |
| Queue._pool_occupancy, resident._pool_admit | all active receipts counted | Count only bridge receipts |
| active_cooldown | reads canonical receipts | Keep shared, never filter by route |
| mark_unusual_activity_403 | cannot save after resident guard fails | Exact original invocation can record safety evidence only |
| resident-supervision.mjs stopDecision | inspects owner before excluding unrelated tasks | Filter registered listener task first |
| CLI status/doctor and skill | bridge status can be mistaken for native capability | Independent route diagnostics and deterministic selection |

Files were inspected on 2026-10-07. The base has no source AGENTS.md. Follow the launching chat's AGENTS.md policies, including reduction, one canonical authority and protected installed runtime.

## Implementation contract

### 1. One small canonical route registry

Add config/dispatch-routes.json in the existing RuntimePaths.config_dir. Reuse private-path validation, native_storage decoding, atomic_write_json and the existing canonical state_lock. No alternate state home, queue, service or daemon.

The version-1 shape is:

```json
{
  "version": 1,
  "direct_worker": {
    "binding_id": "opaque-stable-identifier",
    "conversation_id": "exact-native-chat-id",
    "label": "Codex Direct Pro",
    "model_confirmation": "user-confirmed-worker",
    "configured_at": "UTC timestamp"
  },
  "direct_bindings": [],
  "legacy_standalone_workers": [],
  "bridge_workers": ["exact-native-chat-id"],
  "listener_tasks": ["exact-native-task-id"]
}
```

direct_worker may be null. direct_bindings is append-only history of full direct-worker records, including the current binding. legacy_standalone_workers is an append-only list of exact scalar worker IDs witnessed while no resident authority/pool owned them. Other lists contain unique valid IDs. Retain previously exposed bridge destinations, historical direct bindings and registered listener tasks conservatively; removing them is outside this change. No model identity is inferred from confirmation. binding_id changes only for explicit direct reconfiguration, never when the bridge rotates. Terminal direct receipts validate against retained history, not a new default worker.

On first creation, derive bridge destinations from valid existing canonical owner/pool/legacy witnesses and all determinable retained bridge receipts and queue associations, including terminal receipts and acknowledged or released claims. Reject ambiguous or malformed evidence rather than configuring a potentially overlapping worker. After establishment, direct operations read the registry and canonical safety receipts, not owner/pool/session files. Bridge enrollment, pool activation/replacement and listener task registration must update/check the registry under the same lock. Disjointness must hold in both directions, including earlier exposed destinations and surviving send authority. Never modify worker.json or worker-pool.json migration witnesses to add the direct worker.

Add worker direct-set, direct-show and direct-reset commands. direct-set reuses --conversation-id, --label, --confirm-worker, --expected-conversation-id and actual-caller native preflight semantics. Replacement requires the exact expected current ID and no unresolved direct work; it cannot make an indeterminate destination reusable. direct-reset has no force bypass. Legacy worker set/show/reset behavior stays compatible. Unknown fields, invalid values, unsafe permissions or conflicting route registration fail precisely.

### 2. Immutable receipt route and worker binding

New direct receipts record execution_route="direct" and direct_binding_id. New bridge receipts record execution_route="bridge". Do not bump the existing receipt schema solely for additive fields. Classify old receipts only through validated existing provenance: pool/queue/resident fields mean bridge; a legacy standalone receipt requires validated legacy binding. Invalid or contradictory provenance fails closed. A supplied CLI route flag is never mutation authority.

Preparation selects direct configuration for an ordinary native caller when it exists; a trusted resident invocation retains bridge selection. With no direct registration, old standalone behavior remains valid only when there is no resident pool. With a pool and no direct worker, ordinary prepare reports direct_worker_not_configured, not listener recovery.

Scope reservation_guard, _save_assignment and all assignment transitions using the bound route. A direct receipt must match its durable worker binding and preserve parent/worker identity. A resident invocation may not mutate direct work. Direct calls may not mutate bridge work or release its claims. Keep bridge generation, slot, arm-for-send and collector rules intact for bridge receipts. Follow-up and chunk-continuation requests stay on the original route, worker and retained binding. An explicit continuation of completed direct A selects its retained binding even if the current default is B or was reset. The ordinary fresh-request path still requires current direct configuration. One unresolved direct request remains the bound across historical bindings.

Enumerate canonical receipts globally, then validate/filter occupancy per route. Direct supports one unresolved assignment on its single worker. Bridge retains its existing capacity and exact slot rules. A distinct direct request must neither count toward bridge capacity nor become a bridge active_assignment alias. All active receipts must still be valid; malformed canonical safety evidence blocks admissions on both routes.

Apply the boundary across prepare, arm, submitted/outbound validation, pending, complete, ambiguous, unusual-activity, collect-only recovery, continuation and status. Changing prepare alone does not meet the contract.

### 3. Account safety observations survive bridge lifecycle failure

Keep active_cooldown global across both routes, including malformed timestamp rejection and the original fixed 30-minute expiry. A bridge HTTP 403 observed after lifecycle failure must still be durably recorded.

At bridge preparation, retain the exact original safety-observer identity from trusted resident invocation plus the existing queue claim binding in the receipt. Reuse generation, owner, parent, invocation, request, slot, worker and claim identity. Retain the canonical original identity or its canonical-JSON hash plus the existing claim association. This proves original binding within the existing cooperative same-UID trust model. CLI resident-invocation JSON is not cryptographic host authentication. Do not add a secret store or claim resistance to a malicious same-UID process that can already read and rewrite private state.

When the current resident guard fails solely due to lifecycle loss/corruption, permit mark_unusual_activity_403 only if the original trusted-runner invocation and receipt/claim identities match exactly and the receipt is in an existing eligible post-arm state. Queue.load supplies strict claim validation without reading bridge lifecycle files. A readable valid current owner that differs must deny fallback. A dedicated private safety-save function writes only the established 403 fields and optional OpenAI request ID, bypassing only resident lifecycle validation. It cannot prepare, send, acknowledge, release, rewrite ownership or complete. Repeating the same observation never renews cooldown. Wrong caller/worker/request/claim, removed/malformed claim or malformed canonical receipt rejects it. External request content never supplies invocation or claim authority. Do not expose a general skip-guard option.

### 4. Stop hook belongs to registered listener tasks

Register the intended native listener task in the route registry before qualification begins, during listener.commit or legacy prepareSupervision before challenge publication. Add a read-only registry-only CLI selector, routes stop-check --parent-task-id <host-event-session-id>. It derives RuntimePaths through the normal validated default_paths mechanism, uses create=False and private file/path integrity checks, and never reads resident owner, pool, session, queue or assignment files. It returns canonical paths and state=registered|unrelated|legacy. stopDecision calls this selector before resident inspect/status. Trusted host Stop event session_id/turn_id selects listener supervision. state=unrelated returns an ordinary non-blocking Stop decision without decoding bridge lifecycle files.

For registered listener tasks, retain all existing qualification and running-resident supervision behavior. With no registry, state=legacy retains the old hook path, not an exemption. A malformed or unsafe shared registry blocks Stop with a precise registry-integrity diagnosis because listener membership cannot then be established. This deliberate shared-integrity case is different from corrupt bridge lifecycle metadata. direct-set may create a valid registry with empty bridge_workers and listener_tasks when no bridge exists; it never registers the ordinary direct caller as a listener and never starts or requires one. Only listener.commit/prepareSupervision registers an intended listener task. Existing valid owner registration may be captured during initial registry bootstrap. Test actual qualification packets, registered listener Stop flow and unrelated-task finalization.

### 5. Diagnostics and deterministic skill routing

Add route-specific read-only status/doctor output without breaking bridge consumers of existing fields. Direct diagnostics must stay readable despite malformed bridge lifecycle files. Native controls remain an actual caller preflight, never a stored Boolean proof. If a direct worker is missing, show the one configuration action; if occupied, show its exact assignment and worker. A stopped listener is a bridge-only diagnostic.

Update SKILL.md and standalone-dispatch.md: ordinary Codex consultation uses direct native workflow; Claude uses claude-client and listener. Listener instructions and hook qualification must not run as direct preflight. Add a small setup example for the explicit direct worker. Native sends still use the documented tools, exact IDs and validation. Do not add browser fallback, auto-resend, timeout-based reservation release or periodic polling.

## Acceptance criteria

1. Public CLI direct-set plus full prepare/arm/submitted/pending/complete lifecycle succeeds with an enrolled but closed bridge pool.
2. Established direct lifecycle succeeds with malformed bridge owner, pool or session files and valid registry/receipts.
3. Direct succeeds with bridge workers occupied; a complete bridge lifecycle succeeds while the distinct direct worker remains armed or indeterminate.
4. Canonical malformed receipt JSON and malformed cooldown timestamp block both admissions. A valid bridge cooldown still blocks direct admission even if bridge owner metadata is malformed.
5. Authenticated original bridge 403 after owner failure saves cooldown; repeat does not extend it; forged identity fails and cannot gain any other mutation.
6. Direct/bridge overlapping registrations are rejected in both ordering directions and under concurrent configuration. Historical bridge workers cannot become direct workers, and historical direct workers cannot become bridge workers. Same-route direct A-to-B-to-A reuse is allowed with exact expected current ID, no unresolved direct work and retained immutable bindings; do not add a permanent same-route reuse gate.
7. Two concurrent direct preparations produce one winner. Re-arm/resend of an armed direct receipt fails. Lost output preserves collect-only recovery.
8. Continuation binds the original route/worker; cross-route mutation and forged execution_route/direct_binding_id changes are rejected.
9. An unrelated direct task finalizes through Stop with malformed bridge lifecycle files, while registered listener qualification/supervision remains enforced.
10. Missing/broken direct registration produces a precise direct configuration/integrity error, never a bridge-recovery instruction or automatic Chrome launch.
11. Existing Python and Node suites, packaging/pin tests, syntax checks and CLI help pass. Public CLI fixture tests, not textual skill assertions alone, prove isolation.
12. Fault injection before and after each registry/owner/pool write preserves disjointness. A failed bridge commit may conservatively retain reserved IDs/tasks but cannot grant a send, corrupt a prior direct binding or discard prior authority.

## Crash ordering and legacy classification

The registry is the authoritative destination reservation ledger. It is a single atomic JSON commit under state_lock. For bridge activation, replacement and enrollment, first atomically append/check target worker IDs and listener tasks, then write pool/owner state using existing transitions. If the later write fails, leave the conservative registry reservation intact. Never roll the registry back, infer the reservation away or write owner/pool first. A retry may reuse the same already-reserved bridge destination. Direct-set atomically changes the current direct pointer and appends its history in one registry write; reset clears only the current pointer, retaining history. Failed writes keep the previous complete registry. No multi-file transaction journal is needed because earlier reservations only restrict authority. Add fault injection at each boundary.

Registration history is not deleted by ordinary worker reset, purge or uninstall. Operations that would erase the registry while registered routes exist must report that route history is retained or reject the unsupported destructive cleanup. A later separately scoped physical-retirement workflow may remove history; it is not part of this task. If a defensive size limit is reached, reject new registrations without pruning old evidence or invalidating established operation.

| Stored receipt evidence | Classification and allowed use |
| --- | --- |
| execution_route=direct plus valid historical direct binding, exact worker and no bridge provenance | Direct; existing collection uses historical binding, new prepare uses current binding |
| execution_route=bridge plus consistent existing resident/queue/pool provenance | Bridge; existing generation/claim guards apply |
| No route, with consistent pool slot/generation or queue-claim provenance | Legacy bridge; retain exposed worker in registry, validate claim when required |
| No route/provenance, matching a scalar binding witnessed with no resident/pool | Legacy standalone; original post-arm exchange is collect/complete-only using retained original worker/parent, with no new arm or continuation after registry establishment |
| Old terminal receipt without determinable historical identity | Read-only diagnostic/cooldown evidence only; no continuation or new send authority inferred |
| Unresolved receipt lacking determinable binding, contradictory fields, unknown route, mismatched direct binding, or mixed direct/bridge provenance | Integrity error; preserve evidence and deny affected admission |

First registry creation captures valid existing scalar standalone identity only when no owner/pool claimed it, plus every determinable exposed bridge/direct destination. Never infer an old receipt's role from a newly replaced scalar configuration. Legacy installations with no registry retain existing behavior until explicit registry establishment. Account cooldown parsing reads all receipts regardless of classification.

After registry establishment, a proven legacy standalone receipt remains immutable legacy provenance, not a new direct binding. Existing armed/submitted/pending/ambiguous/indeterminate exchange may be observed, completed, marked pending/ambiguous/403 or explicitly abandoned on its original worker. Prepared legacy work cannot arm; completed legacy work cannot authorize a continuation. Report legacy_collect_only or legacy_read_only with the exact receipt rather than silently rewriting it. New direct work requires its own fresh direct-bound receipt. This conservative migration changes old-send eligibility only after explicit route registration; installations without the registry keep existing legacy behavior.

## Testing and delivery

Start with a failing full-lifecycle regression fixture using one direct worker and an enrolled two-worker bridge. Add focused concurrency, malformed-state, hook and original-403 observer tests as above. Reuse existing native evidence fixtures, private storage and queue/resident test helpers.

Run python3 -m unittest discover -s tests -v; node --test tests/*.mjs tests/*.cjs; Python compilation; bash -n install.sh uninstall.sh; CLI help. Capture final results outside the worktree. If a source pin or packaging checksum changes, update its reviewed pin and test the complete dependency chain. Do not relax assertions to pass.

Astra medium implements and fixes relevant failures. Terra high reviews the final diff against this spec for missing route guards, unsafe legacy inference, registry atomicity, hook leakage, shared cooldown loss and duplicate-send risk. Resolve material findings, repeat affected verification, then preserve a reviewable commit/PR. Do not install or merge automatically. A live Codex/Claude send remains a separately identified validation step; fixtures cannot be described as live end-to-end proof.

Rollback: keep existing witnesses and receipts intact. Revert code only after direct receipts are terminal or retain this version for collect-only recovery. Older binaries do not understand direct receipts alongside a pool, so silent downgrade while direct work is unresolved is unsupported. No state deletion is a rollback mechanism.

## Files and scope

Likely touched: src/codex_pro_dispatch/core.py, cli.py, resident.py, queue.py, package exports if needed; skills/codex-pro-dispatch/scripts/resident-supervision.mjs and listener startup registration path; SKILL.md and standalone-dispatch.md; focused Python/Node tests and required pin metadata. A small routes.py module is acceptable if it reduces duplication.

Sequence: registry and provenance, route-scoped core, reciprocal bridge filtering, safety observer, scoped hook, diagnostics/docs, complete tests and review. Approximate effort: 3-6 agent hours including review and full validation, with live host checks additional. This is an estimate, not a guarantee.

Out of scope: shared-worker scheduling, listener recovery redesign, model changes, new transport, another daemon/queue, worker creation, live destination reconfiguration, and deletion of existing evidence.
