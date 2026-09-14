# Claude Code client setup

For a request to install/register this skill in Claude, run `python3` on this
skill's `scripts/register-claude.py`. It defaults to user-global registration,
links to the existing Codex source installation, and refuses unrelated targets.
It does not grant shell permissions or activate anything. `--remove` removes
only that registration. If the Codex installation is missing, report the
script's prerequisite prompt, not a successful install. Do not install a
second runtime or change Claude permission settings.

Use the client workflow in [native-activation.md](native-activation.md), not
the standalone native owner workflow. Check client preflight and owner-provided
readiness before publishing. If no ready resident listener is available, say:

> No Codex listener is ready. In a Codex desktop task, paste: "Use
> codex-pro-dispatch to set up a resident listener for my Claude requests.
> Check the installed paths, actual Claude client preflight and canonical
> ownership first. Follow the documented open/serve or guarded recovery path,
> keep the owning execution active, and return the session path and exact
> rendezvous command. Do not send a test request or resend uncertain work."

Give any verified client directory/preflight result with that prompt. If
preflight has not passed, explain that prerequisite first. Do not invent a
session path or claim that installation, an old folder, or a socket alone is
readiness. Claude cannot open the native listener itself. A new task can
discover these instructions, but availability still depends on the owning runtime.
