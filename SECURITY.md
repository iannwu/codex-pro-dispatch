# Security

Codex Pro Dispatch is a source-visible Codex skill, plugin package, and small local state helper. It does not include a browser, desktop automation engine, persistent service, model proxy, MCP server, or account credential store.

## Supported versions

| Version | Security support |
| --- | --- |
| 1.1.x | Supported after stable release |
| 1.0.x and earlier | Upgrade required; public-contract corrections are not backported |

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

Config and state directories use mode `0700`. JSON and lock files use mode `0600`.

Diagnostic commands store a category and SHA-256 hash, not the raw reason body. The v1.1 `doctor` check durably redacts raw diagnostic fields left by earlier releases before reporting health.

Prompt, native read-back, response, and error bodies may pass through short-lived files during a dispatch. The skill requires one private mode-`0700` temporary directory, mode-`0600` files, minimal output, and cleanup after parent restoration on success or failure. Those transient files and host/terminal logs are outside the receipt-store guarantee.

## At-most-once behavior

Immediately before a native send attempt, an assignment moves durably from `prepared` to `armed`. The `armed` receipt sets `no_resend` before transport. The workflow then permits at most one native send attempt. A crash before transport may therefore leave an assignment with zero sends and no resend path. A timeout, app restart, retrieval failure, or ambiguous native result remains collection-only.

Completion requires one recorded submission and an exact verified outbound-prompt hash. A valid-looking result marker alone cannot complete an unverified assignment. Completed receipts are immutable.

## Break-glass deletion

`worker reset --force` and `purge --yes --force` can bypass unresolved-assignment guards. They may destroy the stable worker identity or receipts needed for collect-only recovery. Use them only with explicit user authorization to repair corrupt local state, after explaining that the no-resend evidence and recovery guarantee will be lost.

## External mutations

The Chat Pro worker may perform GitHub actions only when the assignment authorizes them. The parent Codex task must independently verify the remote branch, commit, changed files, protected refs, and CI results.

The parent must not silently substitute its own write and attribute it to Pro.

## Native control limitations

The host product, not this repository, supplies the exact native Chat/Codex control API. The skill checks the required semantic capabilities on every invocation. If any is missing or changed, it fails closed before dispatch. It must not substitute ChatGPT Web, Classic, CDP, Accessibility, AppleScript, or clipboard automation.

Current collection can briefly foreground ChatGPT and move the pointer. No clipboard use is permitted.

## Reporting a vulnerability

Do not open a public issue containing account data, conversation IDs, private repository names, prompts, responses, or assignment receipts.

Use GitHub's **Report a vulnerability** form in this repository's Security tab. Include the affected version, impact, minimal reproduction, and suggested mitigation when available. Redact secrets and private content. The maintainer aims to acknowledge reports within seven days and will coordinate disclosure after a fix or mitigation is available.
