# Codex Pro Dispatch

Dispatch bounded work from a Codex task to a dedicated ChatGPT Pro conversation inside the official combined ChatGPT/Codex desktop app, then return the validated result to the exact parent task.

> Status: v0.1.1 release candidate. The durable pre-send protocol passed the full live native matrix on 2026-08-24. This patch explicitly records native unusual-activity HTTP 403 responses and enforces a 30-minute cooldown before any fresh assignment.

## Why

The official desktop app already contains both sides of the workflow:

```text
Codex Sol orchestrator
    -> dedicated ChatGPT Pro worker
    -> GitHub branch or analysis result
    -> Codex Sol verification and review
```

Codex Pro Dispatch adds the safety contract around that handoff:

- a stable worker conversation ID
- exactly-once submission
- assignment and result markers
- recovery without resending
- same-worker continuation
- exact parent-task restoration
- independent GitHub verification

## Deliberately not included

v0.1 does not install or depend on:

- ChatGPT Web or Codex Web GPT
- ChatGPT Classic
- a Codex model-provider or model-selector integration
- CDP, AppleScript, Accessibility automation, or clipboard injection
- Electron, a browser runtime, or a persistent daemon
- MCP, tunnels, local shell access, or filesystem access for Chat Pro

The host Codex build must expose the native Chat and Codex conversation controls used by the official combined app.

## Installation

Clone the source so every instruction and helper remains inspectable:

```bash
git clone https://github.com/iannwu/codex-pro-dispatch.git
cd codex-pro-dispatch
./install.sh
```

The installer creates only two symlinks:

```text
~/.local/bin/pro-dispatch
${CODEX_HOME:-~/.codex}/skills/codex-pro-dispatch
```

It does not use `sudo`, modify Codex model routing, install a service, or launch at login.

The links point to this checkout by absolute path. Keep the checkout in place while the skill is installed. Before moving or deleting it, uninstall first:

```bash
./uninstall.sh
```

To also erase the private worker configuration and assignment receipts:

```bash
./uninstall.sh --purge-state
```

Purging state is irreversible and is refused while an assignment is unresolved.

Ensure `~/.local/bin` is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## One-time setup

In Codex, invoke `$codex-pro-dispatch` and ask it to set up a worker.

The skill will ask you to:

1. Create or select one dedicated Chat conversation in the official app.
2. Visibly select Pro in that conversation.
3. Let Codex save the conversation's stable ID.

The model selector is not exposed by the native control interface, so the Pro selection is stored as user-confirmed rather than machine-verified.

## Normal use

Tell the Codex parent task something like:

```text
Use $codex-pro-dispatch to send this implementation assignment to my ChatGPT Pro worker. Have Pro commit only to the requested branch, then independently verify the result and run the local tests.
```

The skill prepares a marked assignment, submits it once, waits for the worker conversation to update, validates the marked response, and restores the exact parent task.

## Recovery

A timeout or `thread not loaded` result never authorizes a resend.

The skill reopens the exact saved worker conversation ID and collects the existing answer:

```bash
pro-dispatch recover '<assignment-id>'
```

If collection remains ambiguous, the assignment stays unresolved until it is explicitly completed or abandoned.

When native diagnostics identify an unusual-activity HTTP 403, the skill records the exact error and OpenAI request ID, remains collect-only, and starts a fixed 30-minute cooldown. Abandoning the old receipt does not bypass the cooldown, and no automatic retry occurs.

## CLI

The CLI manages private local state and deterministic validation. It does not control the ChatGPT UI itself.

```bash
pro-dispatch worker set --conversation-id '<id>' --confirm-pro
pro-dispatch worker show
pro-dispatch prepare --parent-task-id '<id>' --prompt-file assignment.md
pro-dispatch arm '<assignment-id>'
pro-dispatch submitted '<assignment-id>' --sent-prompt-file native-read-back.txt
pro-dispatch indeterminate '<assignment-id>' --reason-file reason.txt
pro-dispatch unusual-activity '<assignment-id>' --request-id '<id>' --reason-file reason.txt
pro-dispatch complete '<assignment-id>' --response-file response.txt
pro-dispatch recover '<assignment-id>'
pro-dispatch status
pro-dispatch doctor
```

Private state lives under standard XDG paths:

```text
~/.config/codex-pro-dispatch/worker.json
~/.local/state/codex-pro-dispatch/assignments/*.json
```

Set `CODEX_PRO_DISPATCH_HOME` to isolate both paths during tests.

## Known v0.1 limitation

Sending and waiting can happen in the background, but collecting a completed response may briefly foreground ChatGPT and move the pointer. The skill should defer collection while the user is active in another app when native focus state is available.

The clipboard is not used.

## Verification

Repository-verifiable checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/pro-dispatch src/codex_pro_dispatch/*.py
bash -n install.sh uninstall.sh
./bin/pro-dispatch --help
```

The live release gate is documented in [docs/acceptance.md](docs/acceptance.md).

## Security

Read [SECURITY.md](SECURITY.md). The project stores no ChatGPT cookies, credentials, browser profiles, private source, or full transcripts. Chat Pro uses only the tools exposed inside its official Chat conversation. The parent Codex task independently verifies any remote mutation.

## Experimental history

The repository's earlier branches preserve the Classic/Accessibility proof of concept. They are not part of this official-app skill architecture and should not be merged into this branch.
