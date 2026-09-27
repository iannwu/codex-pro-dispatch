# Start or replace a listener

Use this procedure when the user asks to start a listener, replace its task, or
use new worker chats. It extends the existing skill; there is no separate daemon
or scheduler. The user's start request authorizes the entire supported procedure
and its bounded retries. Do not ask again whether to start or implement recovery.
Ask only for genuinely ambiguous destinations or a missing physical fact.
Never infer readiness from a successful ownership change.

1. Resolve the physical installed `pro-dispatch` and sibling
   `parked-activation.mjs`. Confirm that `pro-dispatch listener start --help`
   exists. An older installation needs this capability installed, not a manual
   edit to its state. Run from the Listener task's working directory and normal
   `CODEX_HOME`. Startup runs its configuration diagnostic once; use
   `pro-dispatch listener check` only for troubleshooting. The default host is
   the bundled ChatGPT app binary. `--codex /absolute/path/to/codex` must name an
   explicitly verified Listener host, never a guessed PATH alternative.
2. Discover the requested ChatGPT conversations with native task tools. Resolve
   titles to exact IDs. If titles are ambiguous, ask which ID. The user's chosen
   conversation is confirmation of that destination, not verification of a model.
3. Check that this Listener task exposes the generated packet's `functions.exec`
   executor, `functions.wait` continuation, and declared native tools. Missing
   tools means `unsupported_listener_surface`. Do not translate the packet into
   direct node REPL calls. Complete the actual client's existing filesystem
   access preflight before advertising it can connect.
4. Generate `pro-dispatch listener start --worker-1 <exact-id>` and optionally
   `--worker-2 <exact-id>`. `--client-root` selects the existing private client
   root, default `~/.cpd-client`. Save stdout to a new private packet file in a
   private directory and compute its raw SHA-256. Do not overwrite an old packet.
5. Run the installed activation script's `packet-call <packet-file> start
   <raw-sha256>`. Pass the returned `arguments` unchanged to its declared
   executor. This captures native task/turn identity, verifies exact ChatGPT IDs,
   and applies the guarded transaction. Startup never sends a worker prompt.
6. Follow the returned `next_action`. On `next_action`, use the returned
   `next_packet` and `next_packet_sha256` with `packet-call` for `qualify`, then
   `open`, then `serve`, in that order. Complete the real Stop-hook roundtrip:
   wait for `resident_supervision_probe_required`, attempt one final response,
   and resume the exact qualification cell after the hook blocks it. Never
   synthesize the probe or treat qualification as complete early. Failed
   qualification leaves a closed, non-ready owner. A changed native turn needs
   a fresh startup packet.
7. Supervise the original serve cell with `functions.wait` after every yield.
   Never replay a consumed serve call. Check `resident-status` and report ready
   only when the current bound session has live admission. Report its capacity,
   session path, ordinal, and generated rendezvous command.

Report all returned `details.blockers` together. Resolve request recovery and
cooldown before asking for a physical action; a reboot must not reveal a known
second blocker that could have been reported first.

`ready` means the same selected pool already has an observed live admission
waiter; do not acquire again. `stale` means regenerate from canonical state.
`commit_unknown` and `stale` both mean regenerate start once from canonical
state. Stop on repeated uncertainty or contention. The same committed operation
is idempotent in the same turn; a new turn can safely rotate an unused owner.
If a session is already bound, use the
existing native context and exact serve-existing eligibility; never invent a
replacement for a lost acknowledgment.

## Configuration diagnostic

`hook_preflight` describes a fresh process reading configuration, not the
running desktop host. Its `ready` state means configured only, never listener
readiness. Real native Stop qualification and live admission remain mandatory.

- `missing`: install the resident hook from the installed release.
- `duplicate` or `wrong_definition`: repair the displayed definitions first.
  Do not ask for trust approval to repair a structural error.
- `disabled`: enable the exact resident hook in `/hooks`.
- `untrusted` or `modified`: the user must review and trust its current definition
  in `/hooks`. If also disabled, report both actions together. Never write the
  trust hash or forge an observed Stop event.
- `unverified`: preserve the diagnostic and continue the normal generated native
  qualification. Discovery errors cannot authorize admission, but are not proof
  of a denied hook. Do not guess a different binary or request trust without
  evidence. There is no bypass flag for a known blocked configuration.

Startup reports known hook and idle-pool blockers together before generating a
packet. The native transaction still rechecks canonical authority under lock.
After the user repairs configuration, regenerate the normal startup packet.
If real qualification fails despite trusted configuration, retain its error and
check host reload and Node resolution. Do not ask for the same trust again.
A fresh turn is not proof the running host reloaded configuration. If reload is
needed, the user must restart the app and open a new Listener task; never restart
it automatically while other work may be active. Reinstallation preserves an
unchanged hook definition. Script-content updates alone do not explain a changed
hook definition hash; report the cause only when supported by evidence.

## Exclusion blockers

Schema-3 idle slots, task deletion, `notLoaded`, an error reading the task, a
missing socket, a stopped transport, and ordinary start authorization are not
proof that every earlier sender terminated. Do not ask to reopen a deleted task.

For a consumed legacy session, startup first checks the Mac's actual boot time
under the canonical lock. If the owner and all retained session evidence predate
that boot by at least five minutes (both modification and change timestamps), the exact
consumed claim matches, and the socket is not live, startup can migrate to fresh
destinations automatically. It records machine evidence, not user confirmation,
and retains the old destinations as exposed. Let the runtime make this decision;
do not add `--confirm-quiescent` or ask for another reboot when this check passes.
This assumes ordinary system timekeeping. Large wall-clock corrections and
administrative timestamp manipulation are outside this cooperative-storage proof.

If legacy execution still cannot be excluded, the fallback is: restart the Mac, do not
resume old listeners, then explicitly confirm that physical termination occurred.
Only after the user supplies that fact may the next command include
`--confirm-quiescent '<their factual observation>'`. The runtime binds this
evidence to the current snapshot and canonical paths. Never add this flag based
on "you have permission", elapsed time, or a guessed reboot. Fresh deployment
likewise needs a factual observation that no old native executor exists. There
is no reboot automation.

For a schema-4 listener that has armed work, use the original generated graceful
stop and wait for its original continuation to record the join. If that
execution is inaccessible, physical termination is the fallback. Profile-2 listeners can also be replaced with disjoint fresh worker chats even
when an old execution cannot be joined. The runtime retains every exposed old
destination and rejects its later reuse without physical recovery. A valid join
excludes only the current session, never earlier exposed destinations. The
runtime makes this decision; do not compute or edit the set yourself. Legacy
schema 3 and profile 1 require physical migration before this policy applies;
schema 3 can use the machine-observed restart described above.

Destination changes require an idle pool. Unresolved work keeps its existing
worker and no-resend status; use the named existing recovery action first.
Startup does not cancel, abandon, bulk-settle, reset, or purge it. Queued rows and
published answers remain unchanged. Cooldown retains its original expiry.

After schema-4 migration, use schema-4-capable code. Reverting destinations is
another forward guarded replacement, never restoring old owner/receipt files.
