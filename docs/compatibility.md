# Compatibility contract

Codex Pro Dispatch is a protocol and local state helper for a native workflow
supplied by the official combined ChatGPT/Codex desktop app. It is compatible only
when the current Codex task can prove every required semantic capability below.
The helper's Linux/macOS unit tests do not imply native workflow support on Linux,
ChatGPT on the web, Codex CLI alone, an IDE extension, or another app surface.

## Required native capabilities

| Capability | Evidence required before prepare or worker configuration |
| --- | --- |
| Parent identity | Stable current Codex parent-task ID |
| Worker identity | Resolve/open the configured Chat conversation by stable ID, never title/position |
| Native submission | One user-message send attempt to that exact worker |
| Outbound verification | Exact bytes and stable ID of the existing submitted user message |
| Item collection | One selected assistant-message ID, exact text, exact submitted-user association, role, item-level finality status/provenance, raw message truncation, and raw selected-result outer truncation/provenance from one native read |
| Navigation | Open exact worker and restore exact parent task by stable ID |

Tool names are implementation details. Similar looking API names are not enough:
their inputs, stable identities, and semantic outputs must meet the table. The
skill repeats this preflight every invocation and passes the result to
`pro-dispatch doctor --native-controls-confirmed`; that flag is an assertion, not
automatic tool inspection.

### Collection adapter acceptance

Native evidence names a helper-owned `adapter_contract_id`. The helper allowlists
that ID and binds it to one host/version-scoped contract. It records message and
outer truncation separately as raw `true`/`false`/`omitted` and normalized
`true`/`false`/`null` values.

An omission can normalize to `false` only if the released helper's exact adapter
contract proves the deployed host always reports shortening as `true`. Examples,
model prose, an observed successful run, a caller boolean, or a host title do not
establish that contract. The v1.2.0 shipped desktop adapter requires both fields,
so an omission currently fails closed. It allows a complete reread upgrade only
where the adapter contract explicitly supports it. Finality must describe the
selected item; finality inferred from an enclosing conversation/turn is rejected.

If an inline/chunk collection capability is missing, those modes are unsupported.
The only narrow exception is artifact discovery for an assignment that was already
prepared in explicit artifact mode: parent-side Git verification may prove the
pre-authorized branch/path even when readable chat evidence is unavailable.

## Supported environment

- macOS
- official combined ChatGPT/Codex desktop app
- ChatGPT account/workspace where the user visibly selects Pro in a dedicated Chat
  conversation
- Python 3.9 or newer
- explicit `$codex-pro-dispatch` invocation

## Repository artifacts and visibility

Prompt-only review and chunked mode need no external connector. Artifact mode
requires a separately installed/authorized GitHub connector in the dedicated Pro
worker that can perform the exact contract write. The plugin neither bundles nor
authenticates it.

The worker sees only repository state reachable through its authorized remote
tools. Local-only branches, uncommitted changes, and Codex worktrees are not
implicitly shared. The parent needs independent remote read access to verify
canonical repository identity, prepared base/protected refs, exact commit/tree/blob
objects, artifact branch stability, and moving-base rules in a private bare repo.

## Tested builds and acceptance status

| Skill version | App version | App build | macOS | Native matrix | Evidence |
| --- | --- | --- | --- | --- | --- |
| 1.1.0 | 26.820.60940 | 7119 | 26.6.2 | Passed | [Historical redacted receipt](releases/v1.1.0-acceptance.md) |
| 1.2.0 candidate | — | — | — | Not yet run | No host acceptance claim |

The historical v1.1.0 run is not evidence for v1.2.0. Until the v1.2.0 candidate
passes the matrix below on an inspected host and a redacted receipt is recorded,
unsupported hosts must fail closed.

## Release-evidence policy

Before any version is called stable, run [acceptance.md](acceptance.md) against
the exact candidate commit and retain a redacted receipt containing:

- candidate SHA and intended tag (if any);
- desktop app version/build, macOS version, architecture, and named native
  capability implementation;
- exact adapter-contract ID and the evidence supporting every omission
  normalization/reread capability it claims;
- pass/fail for inline, truncation, chunk, recovery, artifact, migration,
  retention, privacy, and parent-restoration cases;
- confirmation that private temporary/spool files were removed when permitted and
  clipboard/protected refs were unchanged.

Missing native fields, an unaccepted adapter claim, duplicate send, wrong-thread
read, stale-result acceptance, incorrect chunk reassembly, protected-ref movement,
uncleaned sensitive temporary material, or failed parent restoration blocks
stability.

## Unsupported fallbacks

Never replace missing capabilities with ChatGPT Web, Codex Web GPT, ChatGPT
Classic, CDP, AppleScript, Accessibility automation, browser scraping, clipboard
injection, title-based selection, a caller-supplied truncation flag, or a local
checkout presented as artifact proof.
