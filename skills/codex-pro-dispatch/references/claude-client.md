# Claude Code client setup

For a request to install/register this skill in Claude, run `python3` on this
skill's `scripts/register-claude.py`. It defaults to user-global registration,
links to the existing Codex source installation, and refuses unrelated targets.
It does not grant shell permissions or activate anything. `--remove` removes
only that registration. If the Codex installation is missing, report the
script's prerequisite prompt, not a successful install. Do not install a
second runtime or change Claude permission settings.

Use the client workflow in [native-activation.md](native-activation.md), not
the standalone native owner workflow. After client preflight, check the explicit
owner-provided session with `node "$ACT" resident-status "$SESSION"` as documented
there. A socket or folder is not an available worker. Two chat threads or
session folders do not enable concurrent dispatch. An explicit worker pool may
expose two configured conversations on one listener after live qualification;
that is not implied by unit tests. Never race listeners, rewrite autosend logic
to route around `busy`, or use another state home. A busy current owner needs
to finish its existing request, not another listener. If no current listener is available
(not merely busy), say:

Once Claude starts rendezvous, that Claude invocation and the resident runner
exclusively own collection, durable save, and acknowledgement. Other
coordinators may read canonical status and notify Claude, but must not collect,
save, acknowledge, or start a replacement lifecycle while rendezvous is active.

> No Codex listener is ready. In a Codex desktop task, paste: "Use
> codex-pro-dispatch to set up a resident listener for my Claude requests.
> Check the installed paths, actual Claude client preflight and canonical
> ownership first. Follow the documented open/serve or guarded recovery path,
> share the session path and exact rendezvous command in commentary, then keep
> this dedicated owner turn active by automatically waiting on the original
> serve cell until shutdown. Do not send a final response while it is running.
> Do not ask for periodic confirmation or send routine progress messages.
> Do not send a test request or resend uncertain work."

Give any verified client directory/preflight result with that prompt. If
preflight has not passed, explain that prerequisite first. Do not invent a
session path or claim that installation, an old folder, or a socket alone is
readiness. Claude cannot open the native listener itself. A new task can
discover these instructions, but availability still depends on the owning runtime.
