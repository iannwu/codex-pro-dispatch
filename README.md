# Codex Pro Dispatch

Dispatch coding assignments from Codex to ChatGPT Pro in the native macOS app, then return the completed response to the same Codex task.

> **Status:** experimental v0.1 proof of concept. The transport and safety logic are covered by deterministic tests. Native ChatGPT Classic roundtrip behavior still requires acceptance testing on a real Mac because GitHub Actions cannot drive a signed-in desktop session.

## Why

Codex can orchestrate a repository locally, while ChatGPT Pro can serve as a high-reasoning implementation worker. The missing piece is a reliable, auditable handoff that does not require a person to copy prompts and responses between apps.

```text
Codex Desktop
    -> pro-dispatch
    -> ChatGPT Classic, dedicated Pro thread
    -> Pro response
    -> JSON or raw stdout
    -> same Codex task continues
```

The first release is intentionally transport-only. Codex still applies patches, inspects diffs, runs tests, and enforces completion. ChatGPT Pro receives no filesystem or shell access from this project.

## Safety boundary

v0.1:

- uses macOS Accessibility, not private ChatGPT APIs
- sends one prompt exactly once
- never automatically resends after a timeout
- refuses to overwrite a nonempty input draft
- serializes dispatches with a local file lock
- opens no TCP port
- optionally exposes a mode-`0600` Unix-domain socket for Codex Desktop
- stores prompts, responses, hashes, and status in local mode-`0600` receipts
- runs no repository command and edits no repository file
- handles no password, cookie, API key, or session token

Read [SECURITY.md](SECURITY.md) before extending the bridge with local editing or command execution.

## Requirements

- macOS
- ChatGPT Classic installed and signed in
- a dedicated ChatGPT Pro worker thread open in a conversation window
- Python 3.10 or later
- Xcode Command Line Tools, which provide Swift
- Accessibility permission for the process that runs the bridge

The new ChatGPT/Codex desktop app can remain open as the orchestrator. ChatGPT Classic is used as a separately targetable worker process for this proof of concept.

## Install

```bash
git clone https://github.com/iannwu/codex-pro-dispatch.git
cd codex-pro-dispatch
./install.sh
```

Make sure `~/.local/bin` is on `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Configure ChatGPT Classic

Open ChatGPT Classic on a dedicated Pro thread, then list detectable apps:

```bash
pro-dispatch apps
```

Configure the exact running app by name:

```bash
pro-dispatch configure \
  --app-name "ChatGPT Classic" \
  --transport direct
```

When multiple conversation windows are open, add a stable title substring:

```bash
pro-dispatch configure \
  --app-name "ChatGPT Classic" \
  --window-title "Pro Worker" \
  --transport direct
```

Run diagnostics:

```bash
pro-dispatch doctor
```

If `doctor` reports an Accessibility error, grant permission in:

```text
System Settings -> Privacy & Security -> Accessibility
```

Grant it to Terminal, iTerm, Warp, or whichever host starts the bridge, then fully restart that host.

## Direct smoke test

```bash
pro-dispatch smoke --direct --timeout 300
```

A successful result includes:

```json
{
  "ok": true,
  "smoke_verified": true
}
```

## Dispatch an assignment

From a file:

```bash
pro-dispatch send \
  --prompt-file assignment.md \
  --timeout 3600 \
  --raw
```

From stdin:

```bash
cat assignment.md | pro-dispatch send --timeout 3600 --raw
```

Default JSON output includes the assignment ID, receipt path, elapsed time, status, and response.

## Timeout behavior

A timeout is indeterminate. The prompt may still be running in ChatGPT. `pro-dispatch` records the assignment and exits without resubmitting it.

Collect the eventual response later:

```bash
pro-dispatch collect dispatch-20260820T120000Z-ab12cd34 --raw
```

## Codex Desktop via a local daemon

A subprocess launched by Codex Desktop may not inherit macOS Accessibility permission. In that case, start the bridge in an authorized Terminal tab:

```bash
pro-dispatch configure \
  --app-name "ChatGPT Classic" \
  --transport daemon

pro-dispatch serve
```

The daemon binds only a local Unix-domain socket under the private state directory. In another shell or from Codex Desktop:

```bash
pro-dispatch ping
pro-dispatch send --prompt-file assignment.md --daemon --timeout 3600 --raw
```

No TCP listener is created.

## Commands

```text
pro-dispatch apps        List candidate native ChatGPT/OpenAI apps
pro-dispatch configure   Save the app, window, transport, and socket target
pro-dispatch doctor      Check helpers, permissions, target window, and state mode
pro-dispatch send        Submit one assignment exactly once
pro-dispatch collect     Read a response after timeout without resending
pro-dispatch smoke       Verify a nonce roundtrip
pro-dispatch serve       Run the Unix-socket bridge daemon
pro-dispatch ping        Check daemon availability
```

## Local state

Configuration:

```text
~/.config/codex-pro-dispatch/config.json
```

Receipts, lock, and default socket:

```text
~/.local/state/codex-pro-dispatch/
```

Override the state root with `CODEX_PRO_DISPATCH_STATE_DIR` or standard XDG variables.

Prompts and responses may contain private code. Receipt and transcript paths are ignored by Git, but you remain responsible for where your state directory is stored and synchronized.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/pro-dispatch lib/codex_pro_dispatch/*.py
bash -n install.sh uninstall.sh
```

On macOS:

```bash
for file in bin/cgpt-*; do
  swiftc -typecheck "$file"
done
```

See [docs/acceptance.md](docs/acceptance.md) for the real-app acceptance matrix and [docs/architecture.md](docs/architecture.md) for the dispatch transaction.

## Attribution

The macOS Accessibility approach and substantial helper logic are derived from [aurolabs-ai/claude-chatgpt-bridge](https://github.com/aurolabs-ai/claude-chatgpt-bridge), created by Aurolabs / Roberto Romano and released under the MIT License. See [NOTICE](NOTICE).

## License

MIT
