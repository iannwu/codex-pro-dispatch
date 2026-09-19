# Codex Pro Dispatch

[![CI](https://github.com/iannwu/codex-pro-dispatch/actions/workflows/ci.yml/badge.svg)](https://github.com/iannwu/codex-pro-dispatch/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/iannwu/codex-pro-dispatch)](https://github.com/iannwu/codex-pro-dispatch/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Dispatch work from Codex or Claude Code to ChatGPT through the official native Mac apps.

**No browser control. No pop-ups. No routing issues. No computer-use takeover.**

Codex Pro Dispatch supports:

- ChatGPT Pro or Sol with the reasoning level you choose in ChatGPT
- up to two configured workers; concurrent dispatch requires live qualification
- recovery across interruptions and listener restarts after live qualification
- durable request identity and inspectable results

## How it works

```text
Codex       -> ChatGPT
Claude Code -> Codex listener -> ChatGPT
```

From Codex, the current task dispatches work directly to a dedicated ChatGPT
worker conversation and collects the result back into the originating task.

From Claude Code, a local request goes through a resident Codex listener. The
listener performs the native ChatGPT dispatch and returns the collected result
to Claude Code.

The bridge uses native conversation controls in the official ChatGPT desktop
app for macOS with Codex. It does not use browser automation, Accessibility,
AppleScript, CDP, clipboard automation, or a separate daemon.

This project is independent and unofficial. It is not affiliated with,
endorsed by, or maintained by OpenAI.

**Desktop-only:** It does not run from ChatGPT on the web, Codex CLI alone, IDE
extensions, Windows, or Linux.

## Install

**Version: v1.3.1.**

Run these commands in your terminal:

```bash
codex plugin marketplace add iannwu/codex-pro-dispatch --ref v1.3.1
codex plugin add codex-pro-dispatch@codex-pro-dispatch
```

You need macOS, Python 3.9 or newer, Git, and a Codex CLI with plugin support. No extra Python packages are required. The workflow runs inside the official ChatGPT desktop app for macOS with Codex. The CLI installs the plugin; it cannot run the dispatch workflow on its own.

This pins v1.3.1. App updates can affect compatibility, so every invocation checks the required native controls before proceeding. See [compatibility](docs/compatibility.md), the [native acceptance matrix](docs/acceptance.md), and the [v1.3.1 release receipt](docs/releases/v1.3.1-acceptance.md).

Restart the desktop app if the plugin does not appear.

## Try your first task

In a Codex task, paste:

```text
Use $codex-pro-dispatch to check compatibility and set up my dedicated ChatGPT worker.
```

Codex checks compatibility, helps you create or choose a dedicated ChatGPT conversation, and asks you to pick the model and reasoning effort you want in it. It saves the conversation ID so future requests return to the same conversation, and runs a local health check. Model selection is confirmed by you, not automatically verified by the plugin.

Once setup is complete, try a review using text already in your Codex task:

```text
Use $codex-pro-dispatch to ask my ChatGPT worker to review the implementation plan above. Include the plan in the assignment, ask for the three biggest risks, and bring the findings back here. Do not change any files.
```

You do **not** need a connector for reviewing content included in the prompt.

### Use it from Claude Code

For a source installation, first complete [Source installation and
removal](#source-installation-and-removal), then register the same global skill
for Claude Code:

```bash
python3 ~/.agents/skills/codex-pro-dispatch/scripts/register-claude.py
```

Then invoke `/codex-pro-dispatch` in a fresh local Claude Code task. Claude uses
the resident listener owned by Codex. If no listener is ready, it returns the
documented Codex setup prompt. Registration does not start a listener, grant
shell permission, or authorize a send. See the [Claude client guide](skills/codex-pro-dispatch/references/claude-client.md).

`./install.sh` links the helper and global skill, but does not install the
required Stop hook. To run a resident listener from source, use the source
checkout as the Listener project and review and trust its `.codex/hooks.json`.
The global source-skill link alone cannot qualify resident service. The packaged
plugin includes its own hook for plugin use.

Resident service keeps a dedicated Codex task turn active until shutdown. Codex
shares the session details once, then automatically waits on the original serve
execution. No periodic confirmation is needed. A completed owner turn or a
detached cell is not an available listener; app closure and host cancellation
are not supported availability guarantees.

Before a resident listener starts, the installed synchronous Stop hook and the
Listener task's executor and continuation are qualified in one no-dispatch
check. This keeps an owner from completing while active work still needs its
original serve execution. A failed qualification leaves the listener unavailable.

## What happens

```text
Your Codex task → ChatGPT conversation → Results back in your Codex task
```

You give Codex one clearly scoped assignment. The plugin saves a recovery record, tracks your Codex task and ChatGPT conversation, and allows at most one send attempt. It reads the sent message back through the app to verify delivery and checks that any returned response belongs to that assignment. When a matching result is available, Codex collects it and returns to the original task.

Long answers can be collected in pieces within the [documented limits](docs/specs/long-result-transport-v1.2.0.md). Follow-up requests use the same conversation, with a new assignment ID each time.

Use it to:

- Get a second opinion on a design or implementation plan.
- Review code or investigate a question using context supplied in the assignment.
- Delegate repository changes when the ChatGPT conversation has the necessary GitHub access.

The default workflow handles one unresolved assignment at a time. An explicit
worker pool can bind at most two user-confirmed conversations to one listener.
Native overlap and restart recovery are live qualification gates, not promises
from local tests. It does not start automatically after Codex reopen or reboot.

## Requirements and limits

- **Native macOS desktop only.** ChatGPT on the web, Codex CLI alone, IDE extensions, Windows, and Linux cannot run this workflow.
- **You choose the model in ChatGPT.** Select any available model and reasoning effort in the dedicated conversation before you dispatch. The plugin does not select, route, or verify models.
- **Compatible native controls.** The app must let Codex identify both conversations, send a message, read it back, collect a response, and restore the original task. If any required control is missing, the workflow stops.
- **Explicit invocation.** Use `$codex-pro-dispatch` in Codex or `/codex-pro-dispatch` in Claude Code.
- **Shared context is explicit.** ChatGPT does not automatically see your Codex task, local files, uncommitted changes, or worktree. Include the relevant material in the assignment or provide authorized repository access.

The workflow does not fall back to browser or UI automation when native controls are unavailable.

## Working with GitHub

For a review of pasted code or a plan, start with the prompt-only example above.

To ask ChatGPT to create a branch or commit, its conversation needs a write-capable GitHub connector authorized for the exact repository. Read access alone is not enough. The repository and starting commit must already be on GitHub, and Codex must be able to independently fetch and inspect the reported commit. Local and uncommitted files are invisible to the worker.

The plugin does not install the connector or grant permissions. Start the first write test on a disposable, unprotected branch in a repository without sensitive material. Do not use `main` or another protected branch.

After access is verified, specify the repository, starting commit, branch, and allowed changes:

```text
Use $codex-pro-dispatch to send the implementation described above to my ChatGPT worker. Include the specified repository, starting commit, and allowed changes. Commit only to the named branch, then independently verify the returned commit and tests here.
```

See the [GitHub verification protocol](skills/codex-pro-dispatch/references/github-verification.md) for the full requirements.

## Recovery and at-most-once delivery

Ask Codex to recover the existing assignment:

```text
Use $codex-pro-dispatch to recover my current assignment without resending it, collect any matching completed result, and return to this task.
```

A timeout or app restart does not mean the request failed to reach ChatGPT. Recovery checks the saved conversation and collects an existing result. It never automatically resends the assignment.

This favors preventing duplicates over guaranteed delivery. If the app stops right before the message goes out, it may never have been sent. The plugin still refuses to resend it. Inspect the assignment and explicitly authorize a fresh one before trying again.

Do not delete receipts or force-reset the worker to bypass an active assignment. If ChatGPT blocks a request as unusual activity (HTTP 403), the plugin waits 30 minutes before allowing a fresh assignment.

## Common first-run problems

| Problem | What to do |
| --- | --- |
| `codex plugin` is unknown | Update to a Codex CLI that supports plugin marketplaces. |
| The skill does not appear | Restart the desktop app, confirm the plugin is enabled, and invoke `$codex-pro-dispatch` explicitly. |
| Compatibility check fails | Check the [supported app capabilities](docs/compatibility.md). A missing native control cannot be bypassed. |
| The model you want is unavailable | Use an account or workspace that exposes it, or pick another available model. The plugin cannot enable models. |
| ChatGPT cannot see the code | Include the relevant content in the assignment, or provide authorized GitHub access and a remotely available starting commit. |
| ChatGPT can read GitHub but cannot commit | Check write permissions and organization or SSO policy. Use prompt-only review until access is verified. Recover any active assignment before starting a new one. |
| `legacy-active-assignment` appears after an upgrade | v1.2 can inspect, recover, or explicitly abandon a v1.1 assignment, but cannot continue or complete it. Finish it on v1.1, or explicitly abandon it before starting a new assignment. Never resend it automatically. |
| Another assignment is active | Recover it first. Do not resend it or delete its receipt. |

For help, follow [SUPPORT.md](SUPPORT.md). Share redacted version and capability information, not prompts, conversation IDs, or receipts.

## Verification and privacy

`submission_count: 0` means no verified outbound read-back was recorded, not proof that no native send occurred; a verified delivery can be `submitted` while result observation remains `pending`.

v1.3.1 adds a synchronous Stop-hook qualification and original-serve supervision for resident listeners. The release passed the live Claude-to-Listener acceptance check; see the [v1.3.1 release receipt](docs/releases/v1.3.1-acceptance.md). v1.3.0's resident client, configured worker pool, and deterministic prompt-admission checks remain documented in its [historical release receipt](docs/releases/v1.3.0-acceptance.md).

The plugin checks the answer it reads from the app’s native history, reported as `bounded_native_summary` verification. It cannot prove that this is an exact copy of the original response or independently establish that ChatGPT has finished generating. Reported repository changes require separate commit verification.

The v1.2.2 helper retains IDs, timestamps, state transitions, markers, hashes, and an OpenAI request ID when one is available for unusual-activity error recovery, rather than prompt bodies, response transcripts, credentials, or repository source. State directories use `0700` permissions; receipt and lock files use `0600`.

The workflow can use short-lived private files during collection. App and terminal logs are outside the helper's storage guarantee. Read [SECURITY.md](SECURITY.md) before working with sensitive material.

## Source installation and removal

For source inspection or development, install a pinned checkout instead of the plugin package:

```bash
git clone --branch v1.3.1 https://github.com/iannwu/codex-pro-dispatch.git
cd codex-pro-dispatch
./install.sh
```

This creates symlinks at `~/.local/bin/pro-dispatch` and `~/.agents/skills/codex-pro-dispatch`. Keep that checkout in place while installed. Add `~/.local/bin` to your `PATH` if needed. The installer does not use `sudo`, install dependencies, start a daemon, or launch at login.

To remove the plugin package while retaining recovery records:

```bash
codex plugin remove codex-pro-dispatch@codex-pro-dispatch
codex plugin marketplace remove codex-pro-dispatch
```

Source installations also expose `pro-dispatch recover '<assignment-id>'` for inspecting recovery state. Use the skill in the desktop app to collect the response and return to your task.

For a source installation, run `./uninstall.sh` from its checkout. Recovery records are retained by default. `./uninstall.sh --purge-state` irreversibly deletes worker configuration and receipts, and is refused while an assignment remains unresolved.

## Development and documentation

This README describes the v1.3.1 workflow.

- [Contributing](CONTRIBUTING.md): development and contribution guidance.
- [Skill protocol](skills/codex-pro-dispatch/SKILL.md): dispatch, verification, and recovery rules.
- [Compatibility](docs/compatibility.md): required capabilities and tested-build policy.
- [Release acceptance](docs/acceptance.md): live release checks.
- [Long-response design](docs/specs/long-result-transport-v1.2.0.md): collection and reassembly details.
- [Security](SECURITY.md) and [support](SUPPORT.md).

Runtime and unit tests use Python's standard library. From a source checkout:

```bash
python3 -m unittest discover -s tests -v
```

Iann Wu retains merge and release authority.

## License

[MIT](LICENSE)
