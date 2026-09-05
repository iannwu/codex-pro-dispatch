---
name: codex-pro-dispatch
description: Dispatch one bounded assignment from a Codex task to a dedicated ChatGPT Pro conversation in the official combined desktop app, collect an evidence-gated result without resending, and restore the exact parent task. Use only when the user explicitly asks to delegate work to ChatGPT Pro.
metadata:
  short-description: Dispatch work to official-app ChatGPT Pro
  version: "1.2.0"
---

# Codex Pro Dispatch

Use the official combined ChatGPT/Codex desktop app as a deliberately narrow
transport:

```text
Codex parent task
  -> dedicated ChatGPT Pro worker conversation
  -> inline result, read-only chunk sequence, or explicitly authorized Git artifact
  -> exact Codex parent task
```

This skill relies on native Chat and Codex conversation controls supplied by the
host. It does not install a browser, app, daemon, model provider, MCP connector,
or Accessibility bridge.

## Contract

Safely delegate one bounded assignment from the exact Codex parent task to a
user-confirmed ChatGPT Pro worker. Collect only an existing result, independently
verify any repository artifact, and Restore the exact parent Codex task. Fail
closed whenever native state, association, finality, truncation, or Git identity
is uncertain.

- `AT_MOST_ONCE_SAFETY`: At most one native send attempt is permitted for every
  durable turn. A recovery child is a distinct new turn, never a resend.
- `THREAD_IDENTITY`: worker, parent task, submitted user message, and selected
  assistant message use stable IDs, never titles or visual position.
- `COLLECTION_INTEGRITY`: completion requires one trusted native evidence object
  for the exact worker/message association, final generation, and both message and
  outer-result integrity signals.
- `RECOVERY_INTEGRITY`: an ambiguous send is collect-only. A proven rejected
  response may create exactly one recovery successor under the receipt lock.
- `VERIFICATION_BOUNDARY`: worker claims and chat manifests are untrusted until
  exact parent-side verification completes.

Hard fail if a path resends an armed turn, accepts a result from the wrong worker,
infers item finality from an enclosing turn, trusts a caller boolean, silently
changes a read-only assignment into a Git write, or restores the wrong parent.

## Hard boundaries

- Use only the official combined ChatGPT/Codex desktop app.
- Do not use ChatGPT Web, Codex Web GPT, ChatGPT Classic, CDP, AppleScript,
  Accessibility automation, browser scraping, or the clipboard.
- Do not use the clipboard for submission, collection, or recovery.
- Never resend automatically after a timeout, restart, retrieval error, or
  `thread not loaded` result.
- Use one configured worker and one unresolved logical dispatch at a time.
- The user visibly selects Pro. The host may not expose a trustworthy selected-model
  field, so store that only as user-confirmed.
- This plugin does not install or authenticate that connector. A repository-write
  assignment needs a separately authorized, write-capable GitHub connector in the
  worker, a remotely visible prepared base, and an independent parent read path.
  Read-only access, local-only branches, uncommitted files, and the parent worktree
  are insufficient.
- Do not print prompt, response, artifact, or diagnostic bodies in helper JSON or
  terminal logs. Receipts retain only identities, hashes, sizes, and state.

## Required host preflight

Before configuring a worker or preparing any assignment, confirm that this exact
Codex task can do all of the following semantically:

1. Read the stable ID of the current parent Codex task.
2. Resolve one Chat conversation by stable ID.
3. Send one user message to that exact Chat conversation ID.
4. Read the exact bytes and stable native ID of the already submitted user message.
5. From **one native collection operation**, read the requested and loaded worker
   IDs, selected assistant-message ID, associated submitted-user-message ID, role,
   message-level generation-finality provenance, exact text, raw message truncation,
   and selected-result outer-integrity provenance plus raw outer truncation.
6. Open the exact worker and the exact parent task by stable ID.

The host must provide item-level finality with trusted provenance. Do not invent it
from an enclosing turn. Missing truncation is unknown, not `false`, unless the
helper's allowlisted, version-scoped `adapter_contract_id` explicitly authorizes
that normalization. The shipped adapter requires both fields; examples, a host
title, or a caller flag are never enough. If any capability is absent, stop before
writing worker or assignment state. Artifact discovery is the sole exception: an
already-prepared artifact assignment may use its pre-authorized remote branch/path
without readable chat evidence, but only after exact Git verification.

Run the helper health assertion only after this preflight:

```bash
pro-dispatch doctor --native-controls-confirmed
```

Proceed only when it exits zero with `local_ok: true` and
`native_controls_confirmed: true`. The flag records this invocation's semantic
preflight; it is not automatic host discovery. Resolve the bundled helper relative
to this file (`scripts/pro-dispatch`) if `pro-dispatch` is not on `PATH`.

## Private transient files

Create one private temporary directory (`0700`) before writing a prompt, native
read-back, evidence, response, artifact manifest, or reason file. Use restrictive
`umask` and `0600` files. Keep every transient body in that private temporary
directory; do not use the repository, a shared directory, predictable names, or
the clipboard. Delete it in final cleanup after parent restoration, including on
failure. The receipt store itself never retains prompt or result bodies.

## Set up the dedicated worker

Read [references/native-protocol.md](references/native-protocol.md), ask the user
to create/select one dedicated conversation and visibly select Pro, resolve its
stable ID, then save it:

```bash
pro-dispatch worker set \
  --conversation-id '<conversation-id>' \
  --label 'Codex Pro Dispatch Worker' \
  --confirm-pro \
  --native-controls-confirmed
```

Do not infer the model selection from a title.

## Choose a result mode explicitly

There is no `auto` mode in v1.2.0.

- `inline` is the default for a short response. It can complete only with trusted,
  untruncated native evidence.
- `chunked` is a fully read-only, lossless multi-turn result transport. Each chunk
  child has its own marker, durable arm, native send/read-back, and collection
  evidence.
- `artifact` is an explicit per-assignment Git write. It requires the strict
  contract described in [references/long-results.md](references/long-results.md),
  explicit worker-write confirmation, and an explicit public-retention
  acknowledgement if the repository is public.

Do not choose artifact automatically after an inline or chunked failure. An exact
inline control response can require chunking; a proven truncation can create a
read-only retransmission child. Neither event authorizes a Git write.

## Prepare, arm, send once, and prove the outbound message

Put the bounded prompt in the private directory. Prepare an inline or chunked
dispatch explicitly:

```bash
pro-dispatch prepare \
  --parent-task-id '<parent-task-id>' \
  --result-mode inline \
  --native-controls-confirmed \
  --prompt-file '<prompt-file>'
```

For artifact mode, first verify all prerequisites in
[references/github-verification.md](references/github-verification.md), then pass
the exact contract and all assignment-specific confirmations:

```bash
pro-dispatch prepare \
  --parent-task-id '<parent-task-id>' \
  --result-mode artifact \
  --artifact-contract-file '<contract.json>' \
  --authorize-artifact-write \
  --worker-github-write-confirmed \
  --allow-public-artifact \
  --native-controls-confirmed \
  --prompt-file '<prompt-file>'
```

Omit `--allow-public-artifact` for a private contract. It is mandatory for a
public one and never permits secret, personal, or regulated content.

Read `assignment_id`, `worker_conversation_id`, and the returned `turn` object.
Immediately before the one native send for that turn, arm it:

```bash
pro-dispatch arm '<assignment-id>' --turn-id '<turn-id>'
```

Do not call the native send unless arming succeeds. Arming permanently records
no-resend authority before transport. Make at most one native send attempt for
the returned `wrapped_prompt` to the exact worker. A crash after arm can leave
zero known sends and is still collect-only.

Read back the exact existing user message and its native ID; do not reconstruct it
from JSON. Verify it without sending again:

```bash
pro-dispatch submitted '<assignment-id>' \
  --turn-id '<turn-id>' \
  --native-user-message-id '<native-user-message-id>' \
  --sent-prompt-file '<native-read-back-file>'
```

A byte mismatch records an indeterminate observed send and permits no resend. The
only bounded correction is a receipt-explicit, exactly-one-trailing-LF extraction
artifact; reread the same existing native user message and verify it once. Do not
strip, normalize, or retry any other mismatch.

If send certainty is lost, record the native reason from a private file:

```bash
pro-dispatch indeterminate '<assignment-id>' --turn-id '<turn-id>' --reason-file '<reason-file>'
```

Write untrusted diagnostic text to the reason file without interpolating it into a shell command.
For native unusual activity, retain the category and optional request ID without
retaining the body:

```bash
pro-dispatch unusual-activity '<assignment-id>' \
  --turn-id '<turn-id>' \
  --request-id '<OpenAI-request-id>' \
  --reason-file '<reason-file>'
```

Report an unusual-activity HTTP 403 as such, including the OpenAI request ID when
available. Do not reduce this to a generic `systemError`. It starts a fixed
30-minute cooldown. During it, only read-only recovery is allowed; even a
user-authorized abandon cannot enable a fresh prepare until the cooldown expires.

## Collect trusted results

Open the exact worker only after the native operation can return the complete
evidence envelope. Save the strict UTF-8 JSON object in the private directory.
It must include the helper-allowlisted `adapter_contract_id`, both worker IDs,
assistant and submitted-user-message IDs, `role: "assistant"`,
`generation_status: "completed"`, trusted finality provenance, raw message
`truncated`, raw `selected_result_outer_integrity.truncated`, its provenance,
exact text, and `observed_at`.

Use `collect`, not a body-only response path:

```bash
pro-dispatch collect '<assignment-id>' \
  --turn-id '<turn-id>' \
  --native-evidence-file '<native-evidence.json>' \
  --result-file '<private-result-file>'
```

The helper records raw truncation as `true`, `false`, or `omitted` and normalized
values as `true`, `false`, or `null`; it also records finality and outer-integrity
provenance. A new `observed_at` alone is an idempotent reread because immutable
content identity excludes observation time. Once accepted, a changed source
identity or changed content is an immutable conflict. A truncated prefix may be
upgraded by a complete reread only if the specific allowlisted adapter contract
supports that capability. Never supply a standalone `--truncated` or caller
boolean.

For inline, success materializes only the verified result file. The legacy
`complete` command remains a deprecated evidence-gated inline alias; it never
allows response-only completion.

### Exact chunk control and chunked collection

The only control response that changes an inline dispatch to chunked is exactly:

```text
[CODEX_PRO_DISPATCH_RESULT assignment_id=<turn-id>]
[CODEX_PRO_DISPATCH_CHUNKED_REQUIRED_V1]
```

The marker, one LF, and control line must be the whole normalized response. The
helper atomically marks the submitted predecessor `response_rejected` and creates
exactly one prepared successor under the receipt lock only after verified outbound
submission and proven generation completion. It never sends that child itself.
Uncertain sends cannot transition.

Every chunk response is exactly four LF-separated lines: its result marker, a
strict chunk header, one canonical JSON object containing only `{"payload":"..."}`,
and a matching footer. Marker-looking Markdown belongs in the JSON string as data.
The complete serialized assistant message, including JSON escapes, must meet the
byte limit. The helper hashes decoded canonical-LF UTF-8 payload bytes and
concatenates payloads with no inserted separator. It requires integrity evidence
for every chunk, private crash-safe spool records, contiguous index and chain, and
an exact final count. Read [references/long-results.md](references/long-results.md)
before sending a chunk child.

When `collect` returns `next_turn`, arm, send, read back, and collect that exact
new turn as above. Each child is separately armed and sent at most once. A
truncated chunk is rejected and may prepare one retransmission successor from the
last accepted boundary; it never accepts the visible prefix.

### Artifact collection and verification

Artifact mode remains artifact mode. A readable chat manifest is not completion:
collect its evidence first, then verify exact remote objects into a private result
file:

```bash
pro-dispatch artifact verify '<assignment-id>' --result-file '<private-result-file>'
```

`--discover` is allowed only for a pre-authorized artifact assignment when chat
evidence is unavailable. It checks the exact contract's remote repository, one
parent at the prepared base, one added `100644` UTF-8 Markdown blob at the exact
path, exact commit message/path/hash/size, protected refs, branch movement, and
moving-base rules from a private bare repository. It performs no remote write,
does not accept a checkout or worker claim, and never creates a PR, merge, tag,
release, deployment, or cleanup write.

## Recovery, delivery, and parent restoration

On timeout, restart, stale UI, or `thread not loaded`, run:

```bash
pro-dispatch recover '<assignment-id>'
```

Open only the saved worker ID. Inspect the returned active turn and its exact
read-back hashes. If needed, use `submitted` only to verify an already-existing
native message; never call the native send control during recovery. Recollect the
same selected assistant message with a new evidence object. `response_rejected`
is terminal for that turn's send authority; it is not logical-dispatch completion.

Immutable result completion is separate from delivery and parent restoration.
After materializing or consuming a verified result, restore the saved parent task
with native controls, then record that observation:

```bash
pro-dispatch result parent-restored '<assignment-id>' --native-controls-confirmed
```

Navigation retry cannot reopen content or permit a send. For a chunked completed
result, cleanup is allowed only after parent restoration:

```bash
pro-dispatch result cleanup '<assignment-id>'
```

It removes only verified private spool files. Artifact retention/branch cleanup
is outside this transport and always requires separately explicit authority.

## Legacy receipts and safety maintenance

Schema-v1 unresolved receipts migrate under the receipt lock before a mutable
operation; response-only fields remain audit metadata and cannot complete v2.
Completed v1 receipts are immutable historical `marker-only` / `unverifiable`
projections. Their cooldown remains effective. `doctor` redacts legacy raw
diagnostic bodies but does not turn historical completion into evidence.

If native conversation controls or the version-scoped adapter acceptance contract
are unavailable, stop with the exact blocker. There is no web, UI-automation, or
caller-boolean fallback.

## Local state and development

Receipts contain worker/parent/turn identities, timestamps, state transitions,
markers, hashes, byte lengths, and allowed artifact metadata. They never retain
prompts, responses, result bodies, credentials, cookies, or raw diagnostics.

Useful commands:

```bash
pro-dispatch worker show
pro-dispatch status
pro-dispatch recover '<assignment-id>'
pro-dispatch unusual-activity '<assignment-id>' --reason-file '<reason-file>'
pro-dispatch abandon '<assignment-id>' --reason-file '<reason-file>'
pro-dispatch doctor --native-controls-confirmed
```

`worker reset --force` and `purge --yes --force` are break-glass operations. They
can erase unresolved recovery evidence; require explicit user authorization and
explain that no-resend recovery guarantees are lost.

The runtime has no third-party Python dependency. Run the unit suite, syntax
checks, installer checks, and the skill validator described in the repository
README before proposing a release. Native acceptance is a separate host gate in
[docs/acceptance.md](../../docs/acceptance.md).
