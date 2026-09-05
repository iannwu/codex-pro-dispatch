# Compatibility contract

Codex Pro Dispatch is a protocol and local state helper for a native workflow supplied by the official combined ChatGPT/Codex desktop app. It is compatible only when the current Codex task can prove every capability below.

The workflow is desktop-only. ChatGPT on the web, Codex CLI alone, IDE extensions, Windows, and Linux do not satisfy the supported host contract. The helper's tests may run elsewhere, but that does not make the native dispatch workflow available there.

## Required native capabilities

| Capability | Required evidence before dispatch |
| --- | --- |
| Parent identity | Read the stable ID of the current Codex parent task |
| Worker identity | List or resolve Chat conversations and address the configured worker by stable ID, never title or position |
| Native submission | Make one user-message send attempt to that exact worker |
| Outbound verification | Compare returned user-summary text exactly with the prepared wrapped prompt |
| Result read | Read the paired native summary, idle status, intact footer, and visible truncation metadata |
| Navigation | Open the exact worker and restore the exact parent task by stable ID |

Tool names are host implementation details. For example, a build may expose operations resembling `list_threads`, `send_message_to_thread`, `read_thread`, and exact-ID navigation. Similar names are not proof of compatibility: the semantic inputs, outputs, stable identities, and read-back behavior must all be present.

The skill repeats this preflight on every invocation. `pro-dispatch doctor --native-controls-confirmed` accepts the result as an assertion for that invocation; the standalone Python process cannot inspect the host's tool inventory itself.

The recovered workflow verifies bounded protocol envelopes in the returned history summary. It does not claim original source-byte integrity or authoritative generation finality. See the skill verification scope.

## Supported environment

- macOS
- official combined ChatGPT/Codex desktop app
- ChatGPT account where the user can visibly choose Pro in a dedicated Chat conversation
- Python 3.9 or newer
- explicit `$codex-pro-dispatch` invocation

## External connectors and repository visibility

Prompt-only review and research do not require an external connector. Repository mutations require a GitHub connector or tool in the dedicated Pro worker that is authorized for the exact repository and exposes the requested write action. This plugin does not bundle or authenticate that connector.

The worker can operate only on repository state visible through its authorized tools. Local-only branches, uncommitted changes, and Codex worktrees are not implicitly shared with the Pro conversation. Push the intended starting commit or provide the necessary context through an approved mechanism before dispatch.

The parent Codex task needs an independent read path to the remote repository so it can verify the reported branch, commit SHA, changed files, tests, and protected refs. A connector's availability does not prove its permissions; verify write mode first on a disposable unprotected branch.

The helper's deterministic state logic is tested on macOS and Linux, but Linux CI does not imply native workflow support on Linux.

## Tested builds

| Skill version | App version | App build | macOS | Native matrix | Evidence |
| --- | --- | --- | --- | --- | --- |
| 1.2.0 | 26.901.31953 | 7868 | 26.6.2 | Long-result gate passed; v1.1 safety matrix retained | [Redacted long-result receipt](releases/v1.2.0-long-result-acceptance.md) |
| 1.1.0 | 26.820.60940 | 7119 | 26.6.2 | Passed | [Redacted release receipt](releases/v1.1.0-acceptance.md) |

Earlier development runs are not treated as release evidence because they were not executed against the v1.1.0 release candidate with a complete build receipt.

## Release evidence policy

Before a version is called stable, the maintainer must run [acceptance.md](acceptance.md) against the exact candidate commit and publish a redacted receipt containing:

- candidate commit SHA and intended tag
- ChatGPT/Codex app version and build
- macOS version and architecture
- semantic native capabilities used, with tool names when public
- pass/fail result for every matrix section
- confirmation that temporary files were private and removed
- confirmation that the clipboard and protected Git refs were unchanged

Any missing capability, duplicate send, wrong-thread read, stale-result acceptance, uncleaned sensitive temp file, or failed parent restoration blocks stable release.

## Unsupported fallbacks

The skill must not replace missing native capabilities with ChatGPT Web, Codex Web GPT, ChatGPT Classic, CDP, AppleScript, Accessibility automation, clipboard injection, or title-based thread selection.
