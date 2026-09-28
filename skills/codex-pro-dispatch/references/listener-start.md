# Start or replace a listener

"Start listener" in the designated Listener task authorizes the whole supported
startup and its bounded internal retries. Do not ask again, and do not choose,
order, or repeat stages yourself. The runtime reads the enrolled worker IDs,
qualifies the Stop hook, opens, and serves inside one execution. Startup never
sends a worker prompt. Ownership is not readiness.

## Start

Call `functions.exec` once with exactly this code, unchanged. It takes no input.

```js
// @exec: {"yield_time_ms":1000}
let sh='p=$HOME/.local/bin/pro-dispatch;if [ ! -e "$p" ]&&[ ! -L "$p" ];then set -- "${CODEX_HOME:-$HOME/.codex}"/plugins/cache/codex-pro-dispatch/codex-pro-dispatch/*/skills/codex-pro-dispatch/scripts/pro-dispatch;[ $# = 1 ]&&[ -e "$1" ]||{ echo "codex-pro-dispatch is not installed exactly once: $*";exit 127;};p=$1;fi;exec "$p" listener start';
let r=await tools.exec_command({cmd:"/bin/sh -c '"+sh+"' 2>&1",login:false,tty:false,yield_time_ms:30000,max_output_tokens:20000}),out=r.output??"";
for(let i=0;i<3&&r.exit_code===undefined&&Number.isInteger(r.session_id);i++){r=await tools.write_stdin({session_id:r.session_id,chars:"",yield_time_ms:30000,max_output_tokens:20000});out+=r.output??"";}
let v=null;try{v=JSON.parse(out);}catch{}
if(r.exit_code!==0||v?.kind!=="resident_packet_call"||typeof v.arguments?.code!=="string")text({schema_version:1,kind:"listener_lifecycle",state:"blocked",action:"stop",reason:v?.error??"listener_start_unavailable",details:v?.details??null,exit_code:r.exit_code??null,output:v?null:out.slice(-4000)});
else await new (Object.getPrototypeOf(async()=>{}).constructor)("tools","text",v.arguments.code)(tools,text);
```

The code selects one installation by a fixed rule: the source-install link
`$HOME/.local/bin/pro-dispatch` whenever it exists, even if broken; otherwise
the single installed plugin copy
(`codex-pro-dispatch@codex-pro-dispatch`, under `${CODEX_HOME:-~/.codex}/plugins/cache`).
It never falls back from a broken selection to another copy. The selected
copy's hook check then requires the host's single resident Stop hook to be
that same copy's own hook.

Then follow only the `action` of the newest output:

- `wait`: call `functions.wait` on the cell ID that `functions.exec` returned.
  Keep doing so after every yield. For the output with `state: "ready"` and
  `admission_observed: true`, post one commentary line with `session_directory`
  and `capacity`, then keep waiting. Later `serve_event` outputs relay serving
  progress with `admission_observed: false`; they are not new readiness.
  A final answer is never the readiness signal.
- `finalize`: attempt exactly one final response right away. The Stop hook
  blocks it; then call `functions.wait` on that same cell. Never call
  `functions.exec` again for this start.
- `stop`: the execution is finished. Report `state`, `reason`, `diagnostic`,
  `details`, and `evidence_path`, then end the turn.

Never replay, split, or rewrite the generated code, call `node_repl` or
`packet-call` for startup, or pass worker IDs, hashes, or stages. Missing
`functions.exec`, `functions.wait`, or a declared native tool means
`unsupported_listener_surface`; nothing was acquired. `resident-status` stays
the authoritative readiness check.

Outcomes:

- `ready` with `stop` and `existing_service_observed`: this pool already has a
  live admission waiter elsewhere. Nothing was acquired.
- `blocked`: nothing was acquired. Report every `details.blockers` entry
  together, using the sections below. Resolve request recovery and cooldown
  before asking for a physical action. `pool_not_configured` means use the
  destination setup below first. `probe_already_attempted_this_turn` means this
  turn already ran its one Stop probe; start again in a new turn, which also
  observes a listener that is already serving.
- `starting` with `admission_not_observed` and action `wait`: serving continues,
  but no live admission waiter was seen (about 75 seconds), or the separate
  observer failed (`observer_error`). This is not readiness. Keep waiting;
  `resident-status` on `session_directory` is the readiness check.
- `failed` with `qualification_timeout`: follow its `diagnostic`. The owner
  stays acquired, unqualified and recoverable; nothing was opened or sent. An
  accepted final attempt does not prove the hook never ran: after the deadline
  the hook can allow it for this unserved owner. Do not ask for trust again
  unless `pro-dispatch listener check` reports `untrusted` or `modified`.
- `listener_start_unavailable` with `exit_code` 126 or 127: no usable
  installation was selected; nothing was acquired. If `output` names
  `.local/bin/pro-dispatch`, that source-install link is broken: rerun its
  checkout's `install.sh`, or remove that source installation to use the plugin.
  If `output` says `not installed exactly once`, there is no source link and not
  exactly one plugin copy: install the plugin with
  `codex plugin add codex-pro-dispatch@codex-pro-dispatch`. Report which one.
  Do not run a different `pro-dispatch` copy or a second start.
- Other `failed` or `stopped`: preserve the reported evidence. Never replay a
  consumed serve call or invent a replacement for a lost acknowledgment.

## Destination setup

Use this only when the user asks for different worker chats or no pool is
configured. Discover the requested ChatGPT conversations with native task tools
and resolve titles to exact IDs; if titles are ambiguous, ask which ID. Then run
`pro-dispatch listener start --worker-1 <exact-id> [--worker-2 <exact-id>]`
from this task's working directory and pass its returned `arguments.code`
unchanged to `functions.exec`. The same composed execution follows, with the
same three actions. `--client-root` selects the private client root, default
`~/.cpd-client`. `--codex /absolute/path` must name an explicitly verified
Listener host, never a guessed PATH alternative.

## Configuration diagnostic

`hook_preflight` describes a fresh process reading configuration, not the
running desktop host. Its `ready` state means configured only, never listener
readiness. Real native Stop qualification and live admission remain mandatory.

- `missing`: the selected installation's hook is not configured. The plugin
  bundles it (enable the plugin); a source checkout adds it with its `install.sh`.
  Then open a new Listener task.
- `duplicate` or `wrong_definition`: repair the displayed definitions first.
  Each hook shows its `source` and `sourcePath`, and `expected_command` shows
  the selected installation. Hooks from both a plugin and a source checkout
  mean two installations: keep one. Do not ask for trust approval to repair a
  structural error.
- `disabled`: enable the exact resident hook in `/hooks`.
- `untrusted` or `modified`: the user must review and trust its current definition
  in `/hooks`. If also disabled, report both actions together. Never write the
  trust hash or forge an observed Stop event.
- `unverified`: preserve the diagnostic and continue the normal generated native
  qualification. Discovery errors cannot authorize admission, but are not proof
  of a denied hook. Do not guess a different binary or request trust without
  evidence. There is no bypass flag for a known blocked configuration.

Startup reports known hook and idle-pool blockers together before acquiring
anything. The native transaction still rechecks canonical authority under lock.
After the user repairs configuration, start again with the same code.
If real qualification fails despite trusted configuration, retain its error and
do not ask for the same trust again. A fresh turn is not proof the running host
loaded the hook for this task. Observed on 2026-09-27: the unchanged, trusted
hook did not run in older Listener tasks but did run in a newly created one, in
the same running app. Open a new Listener task first; an app restart was not
needed then. Never restart the app automatically while other work may be
active. Reinstallation preserves an unchanged hook definition. Script-content
updates alone do not explain a changed hook definition hash; report the cause
only when supported by evidence.

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
Only after the user supplies that fact may you run `pro-dispatch listener start
--confirm-quiescent '<their factual observation>'` (plus any destination flags)
and pass its `arguments.code` unchanged to `functions.exec`. The runtime binds this
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
