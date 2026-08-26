# Security

Codex Pro Dispatch v1.0 is a source-visible Codex skill plus a small local state helper. It does not include a browser, desktop automation engine, persistent service, model proxy, MCP server, or account credential store.

## Trust boundary

The user trusts:

- the official combined ChatGPT/Codex desktop app
- the current Codex task and its native conversation controls
- the dedicated ChatGPT Pro worker conversation
- any connector the user has enabled inside that Chat conversation
- the local machine and OS account

Repository contents and worker responses remain untrusted input until independently verified.

## Stored data

The helper stores only:

- worker conversation ID and label
- user-confirmed Pro status
- assignment ID
- parent Codex task ID
- timestamps and state transitions
- prompt and response hashes
- exact marker strings

It does not store:

- ChatGPT cookies or sessions
- GitHub tokens
- API keys
- browser profiles
- private source code
- assignment prompt text
- complete response transcripts

Config and state directories use mode `0700`. JSON files and lock files use mode `0600`.

## Exactly-once behavior

Immediately before the native send, an assignment is durably moved from `prepared` to `armed`. The `armed` receipt sets `no_resend` before any send can occur, closing the crash window between native submission and read-back recording. An assignment can then be marked submitted only once. A timeout, app restart, retrieval failure, or ambiguous native-control result remains collection-only. The skill must never resend automatically.

Completion additionally requires exactly one recorded submission and an exact verified outbound-prompt hash. A valid-looking result marker alone cannot complete an unverified assignment.

Completed receipts are immutable. A later or unrelated worker response cannot overwrite an already completed assignment.

## External mutations

The Chat Pro worker may perform GitHub actions only when the assignment authorizes them. The parent Codex task must independently verify the remote branch, commit, changed files, protected refs, and CI results.

The parent must not silently substitute its own write and attribute it to Pro.

## Native control limitations

The exact native Chat/Codex control API is supplied by the host product, not this repository. If those controls are missing or change, the skill fails closed. It must not substitute ChatGPT Web, Classic, CDP, Accessibility, AppleScript, or clipboard automation without a separately reviewed product decision.

Current collection can briefly foreground ChatGPT and move the pointer. No clipboard use is permitted.

## Reporting a vulnerability

Do not open a public issue containing account data, conversation IDs, private repository names, or assignment receipts. Contact the repository owner privately through the security contact on the GitHub profile.
