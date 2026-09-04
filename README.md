# Codex Pro Dispatch

[![CI](https://github.com/iannwu/codex-pro-dispatch/actions/workflows/ci.yml/badge.svg)](https://github.com/iannwu/codex-pro-dispatch/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/iannwu/codex-pro-dispatch)](https://github.com/iannwu/codex-pro-dispatch/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-black.svg)](#requirements)

An independent macOS safety wrapper for a supported Codex desktop workflow. It hands one bounded implementation, review, or research job to a dedicated ChatGPT Pro conversation with at most one native send attempt, collect-only recovery, and independent verification of the result.

**Desktop-only:** the dispatch workflow runs only inside the official ChatGPT desktop app for macOS with Codex. It does not run from ChatGPT on the web, Codex CLI alone, an IDE extension, Windows, or Linux. The Codex CLI is used only to install and manage the plugin.

**Version: v1.2.0 stable.** Long results now continue in bounded chunks and are
reassembled exactly without weakening the existing no-resend contract. See the
[v1.2.0 long-result receipt](docs/releases/v1.2.0-long-result-acceptance.md) and
the prior [v1.1.0 full-matrix receipt](docs/releases/v1.1.0-acceptance.md).

This project is independent and unofficial. It is not affiliated with, endorsed by, or maintained by OpenAI.

## What it does

```text
Codex parent task
    -> one bounded, marked assignment
    -> dedicated ChatGPT Pro conversation
    -> marked response or authorized GitHub commit
    -> parent-side validation and exact task restoration
```

Codex Pro Dispatch provides the safety protocol around that handoff:

- stable worker and parent-task identity
- at-most-one native send attempt per assignment
- exact native read-back verification
- no automatic resend after ambiguity, timeout, or restart
- result markers and stale-response rejection
- same-worker follow-ups with new assignment IDs
- exact reassembly of long results from bounded continuation chunks; 10,000
  bytes is a per-response generation target, not a hard acceptance gate
- independent verification of worker-reported repository changes

It is not a model router or a standalone ChatGPT transport. The repository supplies the workflow, plugin package, and local receipt state machine; the supported Codex host supplies native conversation controls.

For an instruction-level audit, read the complete [skill protocol](skills/codex-pro-dispatch/SKILL.md); its linked references define native recovery and GitHub verification. The [v1.2.0 design spec](docs/specs/long-result-transport-v1.2.0.md) explains the deliberately narrow long-result protocol.

## Who it is for

- **People:** Codex users who want a deliberate, inspectable way to ask a dedicated ChatGPT Pro conversation for a bounded second implementation, review, or research pass.
- **Agents:** Codex tasks that can prove the required native capabilities, preserve exact thread identity, and fail closed instead of guessing or resending.

If you only want another Codex subagent, use Codex's native subagent tools. If your host cannot expose the native Chat/Codex controls below, this skill is not compatible.

## Requirements

| Requirement | Supported contract |
| --- | --- |
| OS | macOS |
| Host | Official ChatGPT desktop app with Codex; desktop workflow only |
| Installer | A current Codex CLI that exposes `codex plugin marketplace` and `codex plugin add` |
| Account | ChatGPT account or workspace where the user can visibly select Pro |
| Runtime | Python 3.9 or newer; no third-party Python packages |
| Invocation | Explicit `$codex-pro-dispatch` invocation |
| Worker | One dedicated Chat conversation with Pro visibly selected |
| Native capabilities | Current parent-task ID; list/resolve chats; exact-ID send; exact user-message read-back; completed-response read; exact-ID open/restore |
| Connector | None for prompt-only review or research; repository writes require a write-capable GitHub connector/tool in the Pro worker |
| Verification | Repository-write tasks also require parent-side access to fetch and inspect the reported remote commit |

Every invocation checks those six semantic capabilities before configuring a worker or preparing an assignment. Exact tool names may change between app builds. Missing capability means stop—never a fallback to browser, Accessibility, AppleScript, CDP, or clipboard automation.

The native Chat/Codex controls are not a GitHub connector. This plugin supplies neither one: the desktop host supplies the conversation controls, while external-service connectors are installed and authorized separately by the user or workspace.

Before installing, these commands should succeed:

```bash
codex --version
codex plugin --help
python3 --version  # must be 3.9+
git --version
```

### Compatibility status

| Surface | Status |
| --- | --- |
| Local state machine | Tested on macOS and Linux in CI |
| Plugin manifest | Validated against the current Codex plugin schema |
| Manual skill discovery | `$HOME/.agents/skills` |
| Native end-to-end workflow | v1.1.0 full matrix and v1.2.0 long-result gate passed; compatibility remains build-sensitive |
| Current maintainer app build | `26.901.31953` (`7868`) on macOS 26.6.2; v1.2.0 long-result gate passed |

See [docs/compatibility.md](docs/compatibility.md) for the exact capability contract and tested-build policy.

## Install

OpenAI's current guidance packages reusable skills as plugins. This repository includes the plugin manifest and marketplace catalog needed for a normal Codex install. See the official [skills](https://developers.openai.com/codex/skills) and [plugin packaging](https://developers.openai.com/plugins/build/plugins) documentation.

Install the stable release from the immutable `v1.2.0` tag:

```bash
codex plugin marketplace add iannwu/codex-pro-dispatch --ref v1.2.0
codex plugin add codex-pro-dispatch@codex-pro-dispatch
```

Restart Codex if the plugin does not appear, then invoke `$codex-pro-dispatch` explicitly.

To remove the plugin while retaining private receipts:

```bash
codex plugin remove codex-pro-dispatch@codex-pro-dispatch
codex plugin marketplace remove codex-pro-dispatch
```

For source development or audit-first installation, clone and pin the same immutable tag, then use the transparent symlink installer:

```bash
git clone https://github.com/iannwu/codex-pro-dispatch.git
cd codex-pro-dispatch
git checkout v1.2.0
./install.sh
```

The source installer creates two visible symlinks:

```text
~/.local/bin/pro-dispatch
~/.agents/skills/codex-pro-dispatch
```

It does not use `sudo`, install dependencies, start a daemon, alter model routing, or launch at login. Keep the checkout in place while installed. Add `~/.local/bin` to `PATH` if needed:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then restart Codex if the skill does not appear and invoke `$codex-pro-dispatch` explicitly.

When upgrading an installation made before v1.1 from the same checkout, `install.sh` safely migrates its owned legacy `$CODEX_HOME/skills/codex-pro-dispatch` symlink. It refuses regular files and symlinks owned by another checkout.

To uninstall a source installation while retaining private receipts:

```bash
./uninstall.sh
```

To also purge worker configuration and receipts:

```bash
./uninstall.sh --purge-state
```

Purge is irreversible and is refused while an assignment is unresolved.

## First run

In a Codex task, say:

```text
Use $codex-pro-dispatch to check compatibility and set up my dedicated ChatGPT Pro worker.
```

The skill will:

1. Verify all six native host capabilities.
2. Ask you to create or choose one dedicated Chat conversation.
3. Ask you to visibly select Pro.
4. Save that conversation's stable ID and run the local health check.

The native interface does not machine-verify the selected model, so Pro selection is stored honestly as user-confirmed.

## GitHub connector for repository work

You do **not** need a connector when the Pro worker only reviews or researches the prompt you send it.

If you want the worker to create a branch or commit, all of the following must be true:

1. A GitHub connector or tool is enabled for the dedicated Pro worker and authorized for the exact repository.
2. That connector exposes the required write action. Read-only repository access is not enough; connector capabilities and workspace policy can vary.
3. The relevant repository and starting commit exist on GitHub. The Pro conversation cannot see uncommitted files, local-only branches, or your Codex worktree unless you explicitly provide that content through an approved tool.
4. The Codex parent can independently fetch and inspect the returned commit, using local Git credentials or another read path. Worker claims are never accepted without verification.
5. The first write test uses a disposable, unprotected branch. Never use the connector's first test against `main`, another protected branch, or a private repository containing sensitive material.

This plugin does not install, authenticate, or broaden permissions for the GitHub connector. Repository owners and workspace administrators may need to approve the connector, organization SSO, and repository access separately.

## Use it

```text
Use $codex-pro-dispatch to send this bounded implementation to my ChatGPT Pro worker. Ask it to commit only to the named branch, then independently verify the commit and tests here.
```

The workflow arms a durable receipt immediately before transport and then permits at most one native send attempt. It verifies delivery only through exact native read-back.

That distinction matters: if the app stops after arming but before transport, the assignment may have zero sends and still become permanently collect-only. This favors duplicate prevention over guaranteed delivery. Start a fresh assignment only after bounded inspection and explicit user authorization.

## Common first-run problems

| Symptom | Likely cause and fix |
| --- | --- |
| `codex plugin` is unknown | Update the Codex CLI. Plugin installation requires a build with plugin marketplace support. |
| Plugin installed but `$codex-pro-dispatch` is missing | Restart the ChatGPT desktop app, confirm the plugin is enabled, then invoke the skill explicitly in a new Codex task. |
| Compatibility check reports missing native controls | This app build or task surface cannot run the workflow. Use the supported macOS desktop surface; there is no web, CLI-only, IDE, or UI-automation fallback. |
| Pro cannot be selected | The account or workspace does not currently expose the required Pro setting. The helper cannot select or verify it for you. |
| Worker cannot see the repository or latest code | Grant the GitHub connector access to that repository and push the required starting commit. Local and uncommitted files are invisible to the worker. |
| Worker can read GitHub but cannot commit | The connector is read-only, lacks repository permission, or is blocked by organization/SSO policy. Use prompt-only review mode or obtain write access before retrying on a new assignment. |
| `python3` is missing or older than 3.9 | Install a supported Python and make sure `python3` resolves to it before invoking the skill. |
| Source install works but `pro-dispatch` is not found | Add `$HOME/.local/bin` to `PATH`, or let the skill use its bundled helper by absolute path. |
| An assignment started on v1.1 reports `legacy-active-assignment` after upgrading | v1.2 can inspect, recover, or explicitly abandon the old receipt, but it cannot continue or complete it. Switch back to v1.1 to finish that assignment, or abandon it before starting a new v1.2 assignment; never resend it automatically. |
| A new dispatch says another assignment is active | Recover or explicitly abandon the existing assignment. Do not delete its receipt or resend it. |
| Dispatch is `armed`, `indeterminate`, or timed out | Run recovery against the saved worker. Never resend the same assignment; it may already have been delivered. |

For support requests, include redacted versions and capability details listed in [SUPPORT.md](SUPPORT.md), never prompts, conversation IDs, or assignment receipts.

## Recovery

A timeout, restart, stale UI, or `thread not loaded` result never authorizes a resend. Recover the existing assignment:

```bash
pro-dispatch recover '<assignment-id>'
```

The skill opens the saved worker ID, verifies any existing outbound message, collects only a matching completed response, and restores the exact parent task. An unusual-activity HTTP 403 remains collect-only and starts a fixed 30-minute cooldown before any fresh assignment.

## Safety and privacy

The helper stores only worker identity, parent and assignment IDs, timestamps, state transitions, markers, prompt/response hashes, and an OpenAI request ID when one is available for unusual-activity HTTP 403 recovery. Config directories use mode `0700`; receipt and lock files use `0600`.

The helper does not retain prompt bodies, response transcripts, raw diagnostic bodies, credentials, cookies, browser profiles, or repository source. Diagnostic commands store only a category and SHA-256 hash. `doctor` durably redacts raw diagnostic bodies left by releases before v1.1. During a dispatch, the skill may need short-lived prompt, read-back, response, and error files. It requires a private temporary directory, restrictive permissions, minimal output, and cleanup after parent restoration. Host and terminal logs remain outside the helper's storage guarantee.

`worker reset --force` and `purge --yes --force` are break-glass commands. They can erase recovery identity or unresolved receipts and therefore destroy the workflow's no-resend evidence. They are not part of normal operation.

Read [SECURITY.md](SECURITY.md) before using the skill with private repositories.

## Development

The runtime and unit tests use only the Python standard library. Contributors
need Python 3.9+, Bash, and a current Codex installation. OpenAI's optional
skill validator also imports PyYAML; install it in a virtual environment before
running the final command below:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install PyYAML
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/pro-dispatch skills/codex-pro-dispatch/scripts/pro-dispatch src/codex_pro_dispatch/*.py
bash -n install.sh uninstall.sh
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/codex-pro-dispatch
```

The live release gate is [docs/acceptance.md](docs/acceptance.md). Contributions are welcome through issues and pull requests; Iann Wu remains the sole merge and release authority. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
