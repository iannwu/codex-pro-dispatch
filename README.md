# Codex Pro Dispatch

> Local README draft for the resident candidate. Not published to GitHub.
> Public v1.2.2 installation commands do not include the Claude registration
> and resident-listener features described here.

## Claude Code registration (local resident candidate)

Install the Codex source runtime first using `./install.sh` in a trusted copy
of this release. Then register that same copy globally for Claude Code:

```sh
python3 ~/.agents/skills/codex-pro-dispatch/scripts/register-claude.py
```

This creates `~/.claude/skills/codex-pro-dispatch` pointing to the shared
Codex installation. It is user-global, not project-local. Repeating it is
safe; unrelated files or links are refused. No listener starts, request is
sent, or Claude permission setting is changed. Use `/codex-pro-dispatch` in
a fresh Claude Code task to check discovery. A missing listener produces a
copy-paste Codex setup prompt, not an improvised launcher.

If starting from a downloaded copy in Claude before Codex is installed, run
`python3 skills/codex-pro-dispatch/scripts/register-claude.py` from that copy.
It reports the missing prerequisite and the prompt to give Codex. It does not
install the Codex runtime itself. This registration expects the documented
source installation, not an arbitrary marketplace cache path.

To unregister only Claude, run the same command with `--remove`, preferably
before uninstalling Codex. Codex uninstall does not remove this separate
registration; without the Codex source link it becomes inactive. The runtime
and canonical request records are never deleted by registration.

[![CI](https://github.com/iannwu/codex-pro-dispatch/actions/workflows/ci.yml/badge.svg)](https://github.com/iannwu/codex-pro-dispatch/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/iannwu/codex-pro-dispatch)](https://github.com/iannwu/codex-pro-dispatch/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS-black.svg)](#requirements)

An independent macOS safety wrapper for a supported Codex desktop workflow. It hands one bounded implementation, review, or research job to a dedicated ChatGPT Pro conversation with at most one native send attempt, collect-only recovery, and independent verification of the result.

**Desktop-only:** the dispatch workflow runs only inside the official ChatGPT desktop app for macOS with Codex. It does not run from ChatGPT on the web, Codex CLI alone, an IDE extension, Windows, or Linux. The Codex CLI is used only to install and manage the plugin.

**Version: v1.3.0-rc.1.** Local source candidate with an opt-in resident
Claude-to-native-Pro broker. Actual Claude completed a single-request test and
two requests through one listener with a 185-second quiet gap. Each request had
one verified send, matching repeated collections and acknowledgement; the listener
then stopped cleanly. Fresh local Claude Code skill discovery also passed.
Real-work acceptance is still pending. These checks do not prove indefinite
availability, restart survival, zero model overhead or public-release readiness.

The [v1.2.2 release receipt](docs/releases/v1.2.2-acceptance.md) records historical
standalone checks and their clipboard-verification exception, not qualification
of this candidate. Results verify bounded native summaries; original source bytes
and generation finality remain unverified.

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
| Parked client | Node.js with the standard-library modules checked by the source installer; no npm packages |
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
| Native end-to-end workflow | v1.2.2 recovery checks passed with a disclosed clipboard-verification exception; compatibility remains build-sensitive |
| Current maintainer app build | `26.901.41600` (`7982`); bounded native recovery passed short, long-result, GitHub-read, and GitHub-write tests; see the [release receipt](docs/releases/v1.2.2-acceptance.md) |

See [docs/compatibility.md](docs/compatibility.md) for the exact capability contract and tested-build policy.

## Install

The marketplace/tag commands below describe the historical v1.2.2 release.
They do not install this untagged candidate. For the candidate's source-link
installation and recovery boundaries, read the
[broker release reference](skills/codex-pro-dispatch/references/native-request-broker.md).

OpenAI's current guidance packages reusable skills as plugins. This repository includes the plugin manifest and marketplace catalog needed for a normal Codex install. See the official [skills](https://developers.openai.com/codex/skills) and [plugin packaging](https://developers.openai.com/plugins/build/plugins) documentation.

To install v1.2.2, tested on build `7982`:

```bash
codex plugin marketplace add iannwu/codex-pro-dispatch --ref v1.2.2
codex plugin add codex-pro-dispatch@codex-pro-dispatch
```

This release validates bounded native summaries. It does not establish original
source-byte integrity or generation finality. See the
[release receipt](docs/releases/v1.2.2-acceptance.md) for the acceptance evidence
and clipboard-verification exception.

If the plugin does not appear, stop and report discovery failure. This candidate
does not use app restart as an installation or qualification step.

To remove the plugin while retaining private receipts:

```bash
codex plugin remove codex-pro-dispatch@codex-pro-dispatch
codex plugin marketplace remove codex-pro-dispatch
```

For source development or audit-first installation,
clone and pin its immutable tag, then use the transparent symlink installer:

```bash
git clone https://github.com/iannwu/codex-pro-dispatch.git
cd codex-pro-dispatch
git checkout v1.2.2
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

Invoke `$codex-pro-dispatch` explicitly only when the skill is available.
If discovery fails, stop without restarting the app or changing permissions.

When upgrading an installation made before v1.1 from the same checkout, `install.sh` safely migrates its owned legacy `$CODEX_HOME/skills/codex-pro-dispatch` symlink. It refuses regular files and symlinks owned by another checkout.

To uninstall a source installation while retaining private receipts:

```bash
./uninstall.sh
```

To also purge worker configuration and receipts:

```bash
./uninstall.sh --purge-state
```

Purge is irreversible. Without `--force`, it refuses unresolved assignments and
active cooldowns. Any existing queue storage also blocks purge, including with
`--force`. The integrity restrictions under Safety and privacy apply to forced
operations as well.

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

## Claude listener: setup and Q&A

### Try one real, low-risk request

After global registration, start a fresh local Claude Code task and say:

> Use /codex-pro-dispatch to ask native Pro to review the plan below. Return
> its three biggest concerns. Do not change files. If no listener is ready,
> give me the documented Codex setup prompt. Never resend an uncertain request.
>
> [Paste your plan here.]

Claude checks client access and owner-provided readiness. If no listener is
ready, it gives you a prompt to paste into a Codex desktop task. Codex checks
ownership and native capabilities, follows documented resident setup or guarded
recovery, and supplies the session path and exact client command. Claude obtains
any required command permission before executing. Setup is not send permission.

### What is the listener?

It is the local handoff point between Claude and the native Codex owner.
Claude submits a request; Codex sends it to the configured Pro conversation
and returns a validated answer. Creating a Pro chat does not start a listener.

### Do I need two installations?

No. Codex owns one source installation. Claude gets a user-global link to it,
so fresh local Claude Code tasks can discover `/codex-pro-dispatch` across
projects. This does not cover Claude web, Cowork or cloud sessions.
Registration neither grants shell permissions nor starts the listener.

### Must I reopen it for every request?

Not while the same resident listener remains healthy and accepting requests.
Two sequential requests through one listener were tested. If the owning runtime
ends or the listener stops, documented setup or recovery is needed again.
Unattended startup after shutdown is not provided by this candidate.

### Is leaving Codex open enough? Does task mean conversation?

A Codex task is the conversation, but its active execution runs the listener.
An open app or visible task alone does not prove it is serving. The owner must
keep the serving execution active. Archiving, ending or reloading the task,
or restarting the app, must not be assumed to preserve service.

### Does it expire after two hours? What is the 45-second limit?

Opt-in resident mode removes the old finite session's two-hour age limit.
It retains request deadlines and service limits. The pickup deadline starts
when Claude executes rendezvous, not while command approval is pending, and
is separate from Pro's response time. The 64-admission ceiling and finite
pickup, reply and active-observation budgets still apply. See the
[resident protocol](skills/codex-pro-dispatch/references/native-activation.md#resident-opt-in-candidate).
This is not a guarantee of forever-running service.

### Does it wake a model every few minutes while idle?

The resident flow uses ordinary code to wait for requests, not scheduled idle
model polling. Active requests and model observations can consume tokens.
Zero total overhead and production costs have not been established.

### What if Pro is slow, rate-limited, or a request times out?

Ten minutes is an observation checkpoint, not proof of failure. Inspect the
existing request and follow its recovery state. If a send may have occurred,
recover collect-only. Do not resend, replace the request or clear receipts.
A local timeout does not cancel a request already sent to Pro.

### Can I archive 01/02 and create new chats with the same names?

Names are labels; routing uses saved conversation IDs. A new chat is a different
worker even with the same name. Ask Codex to follow documented worker replacement,
resolving old assignments first. Recovery stays bound to the original worker.
Names 01 and 02 alone do not enable parallel dispatch.

### What happens in Claude Auto mode?

Claude enforces its own permissions. Preflight approval is not send approval.
If a command is denied, stop and report it without changing modes or trying an
alternate execution route. User-managed scoped approval may be needed. One
successful invocation does not establish permission persistence across tasks.

### How do I stop it?

Ask the owner or client to use documented resident-stop. Preserve in-flight
or uncertain work for recovery. A clean stop does not remove the installation
or authorize a resend. Reopening requires ownership and recovery checks,
not replaying a consumed setup command.

## GitHub connector for repository work

The Claude resident client is limited to prompt-only consultations. The repository
write workflow below belongs to standalone Codex dispatch, not permission for a
Claude broker request to execute commands or change repositories.

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
| Plugin installed but `$codex-pro-dispatch` is missing | Report discovery failure. Do not restart the app or use another transport as part of this candidate's qualification. |
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

Standalone receipts do not contain prompt or response bodies. The Claude broker
does retain private request and answer bodies for collection until explicit
acknowledgement. Acknowledgement removes queue bodies, not identity/fingerprint
tombstones or all session evidence. Private session prompt snapshots, saved client
proof and host logs may remain; do not claim acknowledgement erases every copy.
Diagnostic commands store a category and SHA-256 hash. `doctor` redacts older
stored diagnostic bodies. Use private directories and restrictive permissions,
and preserve unresolved recovery evidence. Host and terminal logs are outside
the helper's storage guarantee.

`worker reset --force` and `purge --yes --force` are break-glass commands. They can erase recovery identity or unresolved receipts and therefore destroy the workflow's no-resend evidence. They are not part of normal operation.

### Intentional break-glass compatibility change

The reduced broker candidate no longer supports forced deletion through corrupt
or unclassifiable assignment state. Both reset and purge refuse such state even
with `--force`, preserving the worker configuration and receipt files. Unsupported
`native-client` storage or a receipt carrying the `native_client` field also
blocks mutation. These checks are not bypassed by the force flag.

For readable, supported standalone state, the existing explicitly authorized
force behavior remains: reset may bypass the active-assignment check, and purge
may bypass active-assignment and cooldown checks. That remains destructive and
must never be used as automatic dispatch recovery. Queue storage separately
blocks purge even when empty or acknowledged.

An unreadable receipt is unknown state, not evidence that no send occurred.
Preserve the files and resolve corruption through a separately reviewed,
receipt-aware recovery procedure. Do not delete, rename, or edit records merely
to bypass the guard. This candidate adds no automatic repair, quarantine,
migration, ownership release, or resend permission.

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

## Current-host recovery

The recovery release retains the lean footer/chunk protocol and adds
`complete --native-read-file` to validate the complete native history response.
It reports `bounded_native_summary` verification, not original source-byte or
generation-finality verification. The 20K reader boundary, visible truncation,
wrong worker/message association, and missing footer all reject collection.
The [release receipt](docs/releases/v1.2.2-acceptance.md) records exact short
dispatches, a 36,485-byte five-chunk result, independently verified GitHub writes,
real app-restart recovery, and uninterrupted background submission.
