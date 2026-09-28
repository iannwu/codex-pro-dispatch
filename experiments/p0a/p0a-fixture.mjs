// P0a DISPOSABLE EXPERIMENT FIXTURE (issue #29). Not production code: never
// installed, never referenced by the skill, the release manifest or tests/.
//
// Question: can ONE functions.exec closure call native tools, stay live across
// yields, deliver an armed-before-signal partial output, observe a genuine host
// Stop from an interleaved final attempt, and keep running in that same cell?
//
// mode "dry":  no Stop and no files anywhere. Proves live yield, partial output
//              delivery and native calls issued after yields. Cannot pass P0a.
// mode "stop": the INSTALLED resident-supervision.mjs prepareSupervision()
//              writes one challenge keyed by this task/turn in the canonical
//              supervision directory, and only the host's own Stop hook writes
//              the matching observed copy. Canonical owner, pool, queue and
//              sessions are never written. Needs coordinator authorization.
//
// The REPL side keeps its state under globalThis.__cpdP0a, never the
// production keys (parkedSupervision, listenerStartup, parkedOpenBusy).
import * as fs from 'node:fs/promises';
import {watch} from 'node:fs';
import {dirname, join} from 'node:path';
import {homedir} from 'node:os';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createHash, randomBytes} from 'node:crypto';
import {execFile} from 'node:child_process';

const here = fileURLToPath(import.meta.url);
const J = JSON.stringify;
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const id = v => typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v);
const probes = g => (g.__cpdP0a ??= new Map());
const OBSERVATION_MS = 45000; // production waitSupervision deadline
const DRY_MS = 8000;

function identity(meta) {
  const parent = meta?.threadId, turn = meta?.['x-codex-turn-metadata']?.turn_id;
  if (!id(parent) || !id(turn)) throw Error('p0a_identity_missing');
  return {parent, turn};
}
async function pinned(path, expected) {
  if (await fs.realpath(path) !== path || sha(await fs.readFile(path)) !== expected)
    throw Error('p0a_pin_changed: ' + path);
}
// Read-only canonical reads through the supervision module's own helper.
function canonical(supervision, args) {
  return new Promise((resolve, reject) => execFile('python3', [join(dirname(supervision), 'pro-dispatch'), ...args],
    {timeout: 5000, maxBuffer: 1048576}, (error, out) => {
      try {
        if (error) throw error;
        const v = JSON.parse(out);
        if (v?.ok !== true) throw Error('rejected');
        resolve(v);
      } catch (e) { reject(Error('p0a_canonical_read_failed: ' + args.join(' ') + ': ' + (e?.message || e))); }
    }));
}
// Armed means the watcher and deadline exist; the caller signals only after.
function observe(directory, path, expected, ms) {
  return new Promise(resolve => {
    let done = false;
    const finish = value => {
      if (done) return;
      done = true; clearTimeout(timer); watcher.close();
      resolve({...value, at: Date.now()});
    };
    const check = async () => {
      try {
        const raw = await fs.readFile(path, 'utf8');
        finish(raw === expected ? {observed: true} : {observed: false, reason: 'observed_differs'});
      } catch (e) { if (e.code !== 'ENOENT') finish({observed: false, reason: String(e?.message || e)}); }
    };
    const watcher = watch(directory, () => void check());
    watcher.on('error', e => finish({observed: false, reason: 'watch_error: ' + e.message}));
    const timer = setTimeout(() => finish({observed: false, reason: 'qualification_timeout'}), ms);
    void check();
  });
}

export async function arm(g, meta, mode, ms, supervision, supervisionSha256) {
  const {parent, turn} = identity(meta);
  const probe = randomBytes(16).toString('hex');
  if (mode === 'dry') {
    const result = new Promise(resolve => setTimeout(() => resolve({observed: false, dry: true, at: Date.now()}), ms));
    const armedAt = Date.now();
    probes(g).set(probe, {mode, parent, turn, result});
    return {kind: 'p0a_armed', mode, probe, parent, turn, armedAt, deadlineAt: armedAt + ms, canonicalWrites: []};
  }
  if (mode !== 'stop') throw Error('p0a_mode_invalid');
  await pinned(supervision, supervisionSha256);
  const status = await canonical(supervision, ['status', '--current']);
  const {owner} = await canonical(supervision, ['resident', 'inspect']);
  if (owner?.parent === parent) throw Error('p0a_task_is_listener_owner: run P0a in a disposable task');
  const trusted = {parent, configDir: status.paths.config_dir, stateDir: status.paths.state_dir};
  const sup = await import(pathToFileURL(supervision).href + '?p0a=' + supervisionSha256);
  const state = {};
  await sup.prepareSupervision(state, meta, trusted); // unchanged production body
  const directory = join(trusted.stateDir, 'resident-supervision'), key = sha(parent + '\0' + turn);
  const challenge = join(directory, key + '.json'), observed = join(directory, key + '.observed.json');
  const raw = await fs.readFile(challenge, 'utf8');
  if (raw !== J(state.parkedSupervision)) throw Error('p0a_challenge_differs');
  const result = observe(directory, observed, raw, ms);
  const armedAt = Date.now();
  probes(g).set(probe, {mode, parent, turn, trusted, sup, state, observed, result});
  return {kind: 'p0a_armed', mode, probe, parent, turn, armedAt, deadlineAt: armedAt + ms,
    nonce: state.parkedSupervision.nonce, challengeSha256: sha(raw), canonicalWrites: [challenge],
    ownerParentDiffers: true};
}

// Issued before the final attempt; stays in flight across the Stop.
export async function settle(g, meta, probe) {
  const p = probes(g).get(probe);
  if (!p) throw Error('p0a_state_lost: REPL restarted or a different REPL process');
  const {parent, turn} = identity(meta);
  const outcome = await p.result;
  p.settled = {...outcome, parent, turn};
  return {kind: 'p0a_settled', probe, ...p.settled};
}

// Issued after the Stop continuation: proves a native call after resume and
// records the turn identity that a following open stage would receive.
export async function verify(g, meta, probe) {
  const p = probes(g).get(probe);
  if (!p?.settled) throw Error('p0a_state_lost: settle has not completed in this REPL');
  probes(g).delete(probe);
  const now = identity(meta);
  const same = v => v.parent === p.parent && v.turn === p.turn;
  const receipt = {kind: 'p0a_verified', mode: p.mode, probe, armParent: p.parent, armTurn: p.turn,
    settleTurn: p.settled.turn, verifyTurn: now.turn, identityStable: same(p.settled) && same(now),
    observed: p.settled.observed, reason: p.settled.reason ?? null, observedAt: p.settled.at};
  if (p.mode === 'stop') {
    receipt.observedSha256 = p.settled.observed ? sha(await fs.readFile(p.observed)) : null;
    try { await p.sup.requireSupervision(p.state, meta, p.trusted); receipt.productionRequire = 'verified'; }
    catch (e) { receipt.productionRequire = 'failed: ' + (e?.message || e); }
    receipt.pass = p.settled.observed === true && receipt.identityStable && receipt.productionRequire === 'verified';
  } else {
    receipt.pass = p.settled.dry === true && receipt.identityStable;
  }
  return receipt;
}

const HOST = '/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" -c "Print :CFBundleVersion" ' +
  '/Applications/ChatGPT.app/Contents/Info.plist; ' +
  '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex --version; ' +
  '/usr/bin/sw_vers -productVersion; /usr/bin/uname -m';

// The closure is constant for a given fixture, mode and supervision pin.
export async function bootstrap({mode, supervision = null, observationMs = OBSERVATION_MS, dryMs = DRY_MS}) {
  if (!['dry', 'stop'].includes(mode)) throw Error('mode must be dry or stop');
  const fixtureSha256 = sha(await fs.readFile(here));
  let supervisionSha256 = null;
  if (mode === 'stop') {
    if (typeof supervision !== 'string' || await fs.realpath(supervision) !== supervision)
      throw Error('stop mode needs the physical installed resident-supervision.mjs path');
    supervisionSha256 = sha(await fs.readFile(supervision));
  }
  const ms = mode === 'stop' ? observationMs : dryMs;
  const load = `const fs=await import("node:fs/promises"),crypto=await import("node:crypto");` +
    `if(await fs.realpath(${J(here)})!==${J(here)}||crypto.createHash("sha256").update(await fs.readFile(${J(here)})).digest("hex")!==${J(fixtureSha256)})throw Error("p0a fixture pin changed");` +
    `const p=await import(${J(pathToFileURL(here).href + '?sha256=' + fixtureSha256)});`;
  const repl = call => `{${load}console.log(JSON.stringify(await p.${call}));}`;
  const armCode = repl(`arm(globalThis,nodeRepl.requestMeta,${J(mode)},${ms},${J(supervision)},${J(supervisionSha256)})`);
  const settleCode = repl('settle(globalThis,nodeRepl.requestMeta,PROBE)');
  const verifyCode = repl('verify(globalThis,nodeRepl.requestMeta,PROBE)');
  const instruction = mode === 'stop' ?
    'P0a Stop probe armed in this cell. Attempt exactly one final response now. When the Stop hook blocks it, call functions.wait on this SAME cell ID until it completes. Never call functions.exec again for this probe.' :
    'P0a dry probe: no Stop. Keep calling functions.wait on this SAME cell ID until it completes. Do not attempt a final response before completion.';
  const code = `// @exec: {"yield_time_ms":1000}
if(typeof tools!=="object"||tools===null||typeof text!=="function"||["exec_command","mcp__node_repl__js"].some(n=>typeof tools[n]!=="function"))throw Error("unsupported_listener_surface");
function value(r){if(r?.isError===true||r?.status==="failed"||r?.content?.length!==1||r.content[0].type!=="text")throw Error("p0a native call failed: "+JSON.stringify(r).slice(0,1000));return JSON.parse(r.content[0].text);}
const started=Date.now();let surface=null;try{surface=Object.keys(tools).sort();}catch{}
try{
text({kind:"p0a",mode:${J(mode)},state:"starting",action:"wait",surface,at:Date.now()});
const host=await tools.exec_command({cmd:${J(HOST)},login:false,tty:false,yield_time_ms:10000,max_output_tokens:2000});
text({kind:"p0a",state:"host_build",action:"wait",exit_code:host?.exit_code??null,output:host?.output??null,at:Date.now()});
const armed=value(await tools.mcp__node_repl__js({code:${J(armCode)},timeout_ms:60000,title:"P0a arm"}));
text({...armed,state:${J(mode === 'stop' ? 'qualifying' : 'armed')},action:${J(mode === 'stop' ? 'finalize' : 'wait')},instruction:${J(instruction)},emittedAt:Date.now()});
const settled=value(await tools.mcp__node_repl__js({code:${J(settleCode)}.replace("PROBE",()=>JSON.stringify(armed.probe)),timeout_ms:60000,title:"P0a settle"}));
text({...settled,action:"wait",returnedAt:Date.now()});
const verified=value(await tools.mcp__node_repl__js({code:${J(verifyCode)}.replace("PROBE",()=>JSON.stringify(armed.probe)),timeout_ms:60000,title:"P0a verify"}));
const after=await tools.exec_command({cmd:"/bin/date -u +%Y-%m-%dT%H:%M:%SZ",login:false,tty:false,yield_time_ms:10000,max_output_tokens:200});
text({...verified,state:verified.pass?"observed":"failed",action:"stop",postResumeExec:after?.exit_code??null,elapsedMs:Date.now()-started});
}catch(e){text({kind:"p0a",state:"failed",action:"stop",error:String(e?.message||e)});throw e;}`;
  return {kind: 'p0a_bootstrap', mode, observationMs: ms, fixture: here, fixtureSha256,
    supervision, supervisionSha256, codeBytes: Buffer.byteLength(code), codeSha256: sha(code), code};
}

// The installed hook definition names the exact script the host runs.
export async function installedSupervision(codexHome = process.env.CODEX_HOME || join(homedir(), '.codex')) {
  const hooks = JSON.parse(await fs.readFile(join(codexHome, 'hooks.json'), 'utf8'));
  const commands = (hooks?.hooks?.Stop ?? []).flatMap(group => group?.hooks ?? [])
    .map(hook => hook?.command).filter(c => typeof c === 'string' && c.includes('resident-supervision.mjs'));
  const match = commands.length === 1 && /^node "?(\/[^"]+\/resident-supervision\.mjs)"? stop$/.exec(commands[0]);
  if (!match) throw Error('p0a: expected exactly one installed resident Stop hook, found ' + J(commands));
  return match[1];
}

if (typeof process !== 'undefined' && process.argv[1] && await fs.realpath(process.argv[1]) === here) {
  try {
    const [action, mode, ...rest] = process.argv.slice(2);
    if (action !== 'bootstrap' || rest.length) throw Error('usage: p0a-fixture.mjs bootstrap dry|stop');
    console.log(J(await bootstrap({mode, supervision: mode === 'stop' ? await installedSupervision() : null})));
  } catch (e) { console.error(J({ok: false, error: String(e?.message || e)})); process.exitCode = 1; }
}
