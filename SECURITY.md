# Security

Codex Pro Dispatch uses macOS Accessibility permission to read and control a native ChatGPT window. Accessibility is a powerful operating-system capability. Treat this bridge like local developer automation, not like an untrusted web utility.

## v0.1 security boundary

The initial release:

- opens no TCP listening port
- sends no API key, cookie, token, or password
- targets one configured bundle ID, application name, and optional window-title substring
- refuses to overwrite a nonempty input draft
- serializes dispatches with a local file lock
- submits each prompt at most once
- runs no repository command
- edits no repository file
- stores configuration and receipts with mode `0600`
- creates state directories with mode `0700`

## Local daemon

`pro-dispatch serve` creates `dispatch.sock` under the configured state directory. The state directory is mode `0700` and the socket is mode `0600`. It accepts only local filesystem-socket connections and performs the same locking, receipt, timeout, and no-resend checks as direct mode.

The daemon does not authenticate over a network because it does not expose a network endpoint. Any local process running as the same user may be able to reach the socket, so do not run untrusted software under the same macOS account while the daemon is active.

## Recommended use

- Use a dedicated ChatGPT worker thread and one conversation window.
- Review prompts before dispatching private source or credentials.
- Never include `.env` content, private keys, access tokens, session cookies, or production secrets.
- Keep local receipt directories out of Git and cloud-sync folders unless you explicitly accept that exposure.
- Grant Accessibility only to the minimum host process needed for the bridge.
- Remove Accessibility permission when you no longer use the tool.

## Future local editing and testing

Any future feature that lets ChatGPT Pro read files, edit files, or execute commands must be a separate, explicit capability with:

- workspace confinement
- command allowlists
- path normalization and traversal rejection
- no implicit credential access
- no deployment commands by default
- per-assignment audit receipts
- an external completion gate that cannot report success when required commands failed

Do not expose an unrestricted shell or workspace API over a public tunnel.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose local files, credentials, or desktop control. Contact the repository owner privately through the security contact available on the GitHub profile, and include a minimal reproduction without real secrets.
