# Security

Codex Pro Dispatch is a source-visible Codex skill, plugin package, and local
state helper. It does not include a browser, desktop automation engine, persistent
service, model proxy, MCP server, or account credential store.

## Supported versions

| Version | Security support |
| --- | --- |
| 1.2.x | Current unreleased candidate; host compatibility is not asserted until native acceptance completes |
| 1.1.x | Focused fail-closed collection patch; historical release support per published release policy |
| 1.0.x and earlier | Upgrade required; collection-contract corrections are not backported |

## Trust boundary

The user trusts the official combined ChatGPT/Codex desktop app, current Codex
task/native controls, dedicated Pro worker, any separately enabled connector, and
local OS account. Worker responses, chat manifests, native evidence input, stored
receipts, repository contents, and remote ref observations are all validated at
their respective boundaries. A worker claim is never verification.

## Collection integrity

Completion requires a strict native evidence envelope for one exact selected
assistant message and exact submitted native user message in the configured
worker. It records requested/loaded worker identities, item role, item-level
generation finality and trusted provenance, raw and normalized message truncation,
raw and normalized selected-result outer truncation, observation timestamp, and
body-free hashes/sizes.

The helper does not infer item finality from an enclosing turn, expose a standalone
caller truncation boolean, or normalize missing truncation based on an example.
Only a reviewed helper-owned version-scoped adapter contract can authorize an
omission as false. The shipped adapter deliberately requires explicit flags.
`observed_at` is excluded from immutable content identity; a same-content reread
is idempotent, while changed accepted source/content is a conflict. A truncated
prefix cannot complete.

## At-most-once turns and recovery

Immediately before native transport, each turn moves durably to `armed` and gains
no-resend authority. Every turn permits at most one native send attempt. A crash
may leave zero known sends; timeout, app restart, response ambiguity, or a failed
read-back remain collect-only.

After a verified send and proven completed generation, an exact chunk-control or
rejected/truncated result can atomically close that turn as `response_rejected`
and prepare one recovery successor. The successor is a distinct turn and must be
armed/sent by the host. An uncertain send never transitions. Immutable result
completion is separate from delivery and parent restoration, so navigation retry
cannot reopen content or send authority. Unusual-activity HTTP 403 keeps all work
collect-only and preserves the fixed cooldown.

## Chunked result bodies and spool

Chunked mode validates a four-line protocol whose only body field is a strict
canonical JSON `payload` string. It hashes decoded LF-normalized UTF-8 bytes and
reassembles payloads with no inserted separator. Protocol-looking Markdown is
data, not framing. Every accepted chunk has independent native evidence, a chain,
and an exclusive private spool file.

Chunk payload bodies are intentionally retained in a private `0700` state spool
with `0600` files only until verified materialization and recorded parent
restoration permit cleanup. They never enter JSON receipts or helper JSON output.
The receipt journal/hash reconciliation makes a crash after private rename
detectable; missing, altered, or orphaned spool files fail closed.

## Artifact mode and Git retention

Artifact mode is never automatic. It requires explicit assignment-scoped write
authorization, a strict canonical GitHub contract, confirmed worker capability,
and public-retention acknowledgement for public visibility. It rejects sensitive
public categories and does not create implicit writes.

Parent verification uses a private `0700` bare Git repository and exact remote
objects. It requires exactly one commit parent at prepared base, one added regular
`100644` UTF-8 Markdown path, exact commit/path/hash/size, protected-ref checks,
branch stability, and moving-base rules. It rejects a checkout as evidence,
rewrites, artifact merges, extra changes, unsafe object types/content, and branch
movement. Artifact discovery is the only pre-authorized exception to readable chat
evidence, and only for an already-prepared artifact contract. The helper never
creates a PR, merge, tag, release, deployment, or branch cleanup write.

Git content is durable. Do not use artifact mode for credentials, keys, tokens,
medical records, personal data, regulated data, or anything that cannot safely
remain in the contract's repository. Branch deletion/retention is outside this
helper and needs separate explicit authority.

## Stored data and diagnostics

Receipts store only worker/parent/assignment/turn identities, timestamps, state,
markers, hashes, byte lengths, Git object identity, and optional OpenAI request ID
for unusual activity. They do not store prompts, inline responses, chunk payloads,
artifact contents, raw diagnostics, ChatGPT cookies/sessions, GitHub tokens, API
keys, browser profiles, or repository source.

Config/state directories use `0700`; JSON and lock files use `0600`. Diagnostic
commands retain only a category and SHA-256 hash. `doctor` durably removes raw
diagnostic bodies left by older releases without rewriting immutable historical
v1 completion. Prompt, read-back, evidence, response, artifact-manifest, and
reason files must use a caller-owned private temporary directory and be removed
after parent restoration. Host/terminal logs are outside the receipt guarantee.

## Break-glass operations

`worker reset --force` and `purge --yes --force` can destroy recovery identity or
unresolved receipts. Use only with explicit user authorization after explaining
that at-most-once recovery evidence will be lost. They are not ordinary cleanup.

## Native control limitation

The host, not this repository, supplies the native Chat/Codex API. If any required
capability or adapter acceptance contract is absent/changed, fail closed before
dispatch. Never substitute ChatGPT Web, Classic, CDP, browser scraping,
Accessibility, AppleScript, or clipboard automation. Current collection can
briefly foreground ChatGPT; clipboard use is prohibited.

## Reporting a vulnerability

Do not open a public issue containing account data, conversation IDs, private
repository names, prompts, responses, artifact content, spool paths, or receipts.
Use the repository's GitHub **Report a vulnerability** form. Include affected
version, impact, minimal reproduction, and suggested mitigation when possible,
while redacting secrets/private content. The maintainer aims to acknowledge reports
within seven days and coordinate disclosure after a fix or mitigation is available.
