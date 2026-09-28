import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import {dirname, join} from 'node:path';
import {execFile, execFileSync, spawn} from 'node:child_process';
import {once} from 'node:events';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';

// Issue 29: the composed startup. Real canonical state, helper, hook script,
// activation and serving; a synthetic native host. Every Stop below is a
// simulated hook run and never counts as native acceptance evidence.
const root = fileURLToPath(new URL('../', import.meta.url));
const scripts = root + 'skills/codex-pro-dispatch/scripts/';
const activation = scripts + 'parked-activation.mjs', supervisor = scripts + 'resident-supervision.mjs';
const a = await import(activation);
const AF = Object.getPrototypeOf(async function () {}).constructor, J = JSON.stringify;
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const mcp = value => ({content: [{type: 'text', text: J(value)}]});
const quote = v => "'" + String(v).replace(/'/g, "'\\''") + "'";
const P = '01a0f000-0000-7000-8000-000000000001', Q = '01a0f000-0000-7000-8000-000000000002';

function run(file, args, input, env = {}) {
  return new Promise(resolve => {
    const child = execFile(file, args, {env: {...process.env, ...env}, maxBuffer: 8388608, timeout: 60000},
      (error, out, err) => resolve({exit_code: error ? (Number.isInteger(error.code) ? error.code : 1) : 0,
        output: error ? out + err : out}));
    child.stdin.end(input);
  });
}
const hook = async (parent, turn, active = false) => JSON.parse((await run('node', [supervisor, 'stop'],
  J({hook_event_name: 'Stop', session_id: parent, turn_id: turn, stop_hook_active: active}))).output);
const python = code => JSON.parse(execFileSync('python3', ['-c',
  'import sys,json\nsys.path.insert(0,"src")\nfrom codex_pro_dispatch import core,listener\npaths=core.default_paths()\n' + code],
{cwd: root, encoding: 'utf8', env: process.env}));
const planNow = () => python('print(json.dumps(listener.plan(paths,listener.configured_workers(paths))))');
const commitAs = (parent, turn) => python('print(json.dumps(listener.commit(paths,listener.plan(paths,' +
  'listener.configured_workers(paths)),' + J(parent) + ',' + J(turn) + ')))');

async function fixture(t, {pool = true} = {}) {
  const data = JSON.parse(execFileSync('python3', ['-c', `
import sys,json
sys.path[:0]=['tests','src']
from test_three_feature import ThreeFeatureTests
from codex_pro_dispatch import listener
x=ThreeFeatureTests();x.setUp()
${pool ? "x.activate()\nlistener.commit(x.paths,listener.plan(x.paths,['worker-a','worker-b'],'Isolated fixture has no old executors'),'prior-parent','prior-turn')" : ''}
x.temp._finalizer.detach()
print(json.dumps({'root':str(x.paths.state_dir.parent)}))
`], {cwd: root, encoding: 'utf8', env: {...process.env, TMPDIR: '/tmp'}}));
  const previous = process.env.CODEX_PRO_DISPATCH_HOME;
  process.env.CODEX_PRO_DISPATCH_HOME = data.root;
  const f = {root: data.root, clients: data.root + '/clients', owner: data.root + '/state/resident-owner.json', closers: []};
  t.after(async () => {
    for (const close of f.closers) await close().catch(() => {});
    if (previous === undefined) delete process.env.CODEX_PRO_DISPATCH_HOME;
    else process.env.CODEX_PRO_DISPATCH_HOME = previous;
    await fs.rm(data.root, {recursive: true, force: true});
  });
  return f;
}

// observer: 'reject' (the host refuses the readiness call), 'unobserved' (no
// waiter seen), 'yield' (the helper outlives its first exec and must be drained).
// refuse(title) makes the host reject that node_repl call.
function host(f, parent, turn, {stopHook = true, env = {}, observer = null, refuse = () => false} = {}) {
  const g = {}, meta = {threadId: parent, 'x-codex-turn-metadata': {turn_id: turn}};
  const events = [], hooks = [], calls = {sends: 0, observers: 0, drains: 0}, yielded = new Map();
  let waiter = null, inflight = 0;
  const tools = {
    async exec_command({cmd}) {
      if (cmd.includes('PlistBuddy')) return {exit_code: 0, output: 'fixture host build\n'};
      if (cmd.includes(' resident-ready ')) {
        calls.observers++;
        if (observer === 'reject') throw Error('Host refused a concurrent tool call');
        if (observer === 'unobserved') return {exit_code: 0, output: J({admissionObserved: false, state: 'not_waiting'})};
        if (observer === 'yield') {
          yielded.set(calls.observers, run('/bin/sh', ['-c', cmd], undefined, env));
          return {session_id: calls.observers, output: ''};
        }
      }
      return await run('/bin/sh', ['-c', cmd], undefined, env);
    },
    async write_stdin({session_id}) {
      if (!yielded.has(session_id)) throw Error('No yielded helper expected');
      calls.drains++;
      const done = await yielded.get(session_id);
      yielded.delete(session_id);
      return done;
    },
    async mcp__node_repl__js({code, title}) {
      if (refuse(title)) throw Error('Host refused ' + title);
      const lines = [];
      await new AF('nodeRepl', 'globalThis', 'console', 'parkedSocket', 'parkedBinding', code)(
        {tmpDir: f.root, requestMeta: meta}, g, {log: v => lines.push(String(v))}, g.parkedSocket, g.parkedBinding);
      return {content: [{type: 'text', text: lines.join('\n')}]};
    },
    async mcp__codex_app__read_thread({threadId}) {
      return mcp({schemaVersion: 1, thread: {id: threadId, kind: 'chatgpt', status: {type: 'idle'}}, turns: []});
    },
    async mcp__codex_app__send_message_to_thread() { calls.sends++; throw Error('Startup must never send'); },
    async mcp__codex_app__navigate_to_codex_page() { throw Error('Startup must never navigate'); }
  };
  // Counted so a finished cell provably leaves no host tool call running.
  for (const [name, fn] of Object.entries(tools))
    tools[name] = async (...args) => { inflight++; try { return await fn(...args); } finally { inflight--; } };
  // The simulated host runs the Stop hook only after the requested final attempt.
  const text = value => {
    events.push(value);
    if (value?.action === 'finalize' && stopHook) hooks.push(hook(parent, turn));
    if (waiter?.predicate(value)) { const resolve = waiter.resolve; waiter = null; resolve(value); }
  };
  f.closers.push(async () => {
    await a.stopResidentAdmission(g.parkedResident ?? {}).catch(() => {});
    await g.parkedSocket?.close('test_cleanup');
  });
  return {g, meta, events, hooks, calls, tools, text, yielded, inflight: () => inflight,
    launch: code => new AF('tools', 'text', 'yield_control', code)(tools, text, async () => {}),
    until(predicate, ms = 60000) {
      const prior = events.find(predicate);
      if (prior) return Promise.resolve(prior);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(Error('Missing lifecycle event after ' + J(events.at(-1)))), ms);
        timer.unref(); // A race loser's abandoned guard must not hold the runner open.
        waiter = {predicate, resolve: value => { clearTimeout(timer); resolve(value); }};
      });
    }};
}

async function bootstrap() {
  const md = await fs.readFile(root + 'skills/codex-pro-dispatch/references/listener-start.md', 'utf8');
  const match = /## Start\n[\s\S]*?```js\n([\s\S]*?)```/.exec(md);
  assert(match, 'listener-start.md must carry the constant bootstrap');
  assert.match(match[1], /^\/\/ @exec: \{"yield_time_ms":60000\}/);
  return match[1];
}
// The constant bootstrap names the installed CLI path. Point it at this
// checkout with the isolated hook-discovery fixture and a private client root.
async function installedHome(f) {
  const home = f.root + '/home';
  await fs.mkdir(home + '/.local/bin', {recursive: true, mode: 0o700});
  await fs.writeFile(home + '/.local/bin/pro-dispatch', '#!/bin/sh\nexec python3 ' + quote(root + 'bin/pro-dispatch') +
    ' "$@" --codex ' + quote(root + 'tests/fake_hook_codex.py') + ' --client-root ' + quote(f.clients) + '\n', {mode: 0o700});
  return home;
}
// The 120-second Stop deadline elapses at once, for tests that end there.
function shortDeadline(t) {
  const realTimeout = globalThis.setTimeout;
  return t.mock.method(globalThis, 'setTimeout', (callback, delay, ...args) =>
    realTimeout(callback, delay === 120000 ? 20 : delay, ...args));
}
// A python3 on PATH that intercepts only the guarded commit: 'lost' commits
// once but loses the answer, 'unknown' never answers, 'stale' always reports
// stale. Every other python3 call runs unchanged. Returns a commit counter.
async function flakyCommit(t, f, mode) {
  const dir = f.root + '/flaky-python', counter = dir + '/commits';
  const real = execFileSync('python3', ['-c', 'import sys;print(sys.executable)'], {encoding: 'utf8'}).trim();
  await fs.mkdir(dir, {mode: 0o700});
  await fs.writeFile(dir + '/python3', `#!/bin/sh
case "$2" in *"listener.commit("*)
  n=$(( $(cat ${quote(counter)} 2>/dev/null || echo 0) + 1 )); echo "$n" > ${quote(counter)}
  case ${quote(mode)} in
  lost) if [ "$n" = 1 ]; then ${quote(real)} "$@" >/dev/null; exit 1; fi;;
  unknown) cat >/dev/null; exit 1;;
  stale) cat >/dev/null; echo '{"ok":true,"state":"stale","reason":"expected_state_changed","send_authorized":false}'; exit 0;;
  esac;;
esac
exec ${quote(real)} "$@"
`, {mode: 0o700});
  const path = process.env.PATH;
  process.env.PATH = dir + ':' + path;
  t.after(() => { process.env.PATH = path; });
  return async () => Number(await fs.readFile(counter, 'utf8').catch(() => '0'));
}
// App restart as observed on 2026-09-28: task P acquired and served the owner
// in turn-0, then that native execution disappeared. A child process opens the
// real session and is killed, so no cleanup runs: the descriptor, a socket file
// nobody accepts on and a stale waiter remain, with no audit or join. Recovery
// must not depend on whether a real old execution survives.
async function lostExecution(f, {armed = false} = {}) {
  commitAs(P, 'turn-0');
  const owner = JSON.parse(await fs.readFile(f.owner, 'utf8'));
  const status = JSON.parse((await run('python3', [root + 'bin/pro-dispatch', 'status', '--current'])).output);
  await fs.mkdir(f.clients, {recursive: true, mode: 0o700});
  const directory = await fs.realpath(await fs.mkdtemp(f.clients + '/pro-session-'));
  await fs.chmod(directory, 0o700);
  const trusted = {helper: scripts + 'pro-dispatch', configDir: status.paths.config_dir, stateDir: status.paths.state_dir,
    parent: P, workers: status.worker_pool.workers.map(w => ({slot: w.slot, conversation_id: w.conversation_id})),
    worker_pool_sha256: owner.worker_pool_sha256, workerPoolSha256: owner.worker_pool_sha256, resident: true,
    maxConcurrentRequests: 2, leaseMs: null, idleMs: 45000, replyMs: 3900000, maxSnapshots: 6,
    observationMs: 50000, activeJobMs: 3600000};
  const child = spawn(process.execPath, ['--input-type=module', '-e', `
const s=await (await import(${J(pathToFileURL(scripts + 'parked-socket.mjs').href)})).openSession(${J(directory)},${J(trusted)});
await (await import("node:fs/promises")).mkdir(${J(directory)}+"/waiting-1."+s.config.sessionId,{mode:448});
console.log(s.config.sessionId);setInterval(()=>{},1000);`], {stdio: ['ignore', 'pipe', 'inherit']});
  const exited = once(child, 'exit');
  let line = '';
  for await (const chunk of child.stdout) { line += chunk; if (line.includes('\n')) break; }
  const sessionId = line.trim();
  const bound = await run('python3', [root + 'bin/pro-dispatch', 'resident', 'bind-session', J({
    generation: owner.generation, owner: owner.owner, parent: P, worker_pool_sha256: owner.worker_pool_sha256,
    session: {directory, session_id: sessionId, descriptor_sha256: sha(await fs.readFile(directory + '/session.json'))}})]);
  child.kill('SIGKILL');
  await exited;
  assert.equal(bound.exit_code, 0, bound.output);
  const stale = new Date(Date.now() - 3600000);
  await fs.utimes(directory + '/waiting-1.' + sessionId, stale, stale);
  assert((await fs.lstat(directory + '/wake.sock')).isSocket());
  if (armed) python('from codex_pro_dispatch import resident\nwith core.state_lock(paths) as locked:\n' +
    ' v=resident.read(paths,locked);v["send_fence"]["armed_since_barrier"]=True;resident.write(paths,locked,v)\nprint("{}")');
  return {directory, owner: JSON.parse(await fs.readFile(f.owner, 'utf8'))};
}
async function files(directory) {
  const names = (await fs.readdir(directory)).sort();
  return await Promise.all(names.map(async name => {
    const st = await fs.lstat(join(directory, name));
    return [name, st.isFile() ? sha(await fs.readFile(join(directory, name))) : st.isSocket() ? 'socket' : 'directory'];
  }));
}
async function receipts(directory) {
  const names = (await fs.readdir(directory)).filter(n => n.startsWith('receipt-')).sort();
  return await Promise.all(names.map(async name => ({name, value: JSON.parse(await fs.readFile(join(directory, name), 'utf8'))})));
}

test('constant bootstrap runs one execution to live admission; a repeat start observes it; stop is graceful', async t => {
  const f = await fixture(t), home = await installedHome(f);
  const code = await bootstrap();
  assert.doesNotMatch(code, /worker-a|worker-b|[a-f0-9]{64}/, 'bootstrap carries no worker IDs or digests');
  const h = host(f, P, 'turn-1', {env: {HOME: home, CPD_TEST_MODE: 'normal', CPD_TEST_HOOK: '{}'}});
  const running = h.launch(code);
  const ready = await h.until(v => v?.state === 'ready' && v.action === 'wait');
  assert.equal(ready.admission_observed, true);
  assert.equal(ready.capacity, 2);
  assert.equal(ready.generation, 2);
  assert.equal(ready.resident_status.state, 'admission_observed');
  const owner = JSON.parse(await fs.readFile(f.owner, 'utf8'));
  assert.equal(owner.parent, P);
  assert.equal(owner.session.directory, ready.session_directory);
  assert.equal(ready.operation_id, owner.qualification.startup.operation_id);

  // Same pool from another task: observed without acquisition or a second sender.
  const before = await fs.readFile(f.owner);
  const other = host(f, Q, 'turn-q');
  const repeat = await a.listenerLifecycleCall(planNow(), f.clients);
  await other.launch(repeat.arguments.code);
  const observed = other.events.at(-1);
  assert.deepEqual([observed.state, observed.action, observed.reason], ['ready', 'stop', 'existing_service_observed']);
  assert.equal(observed.session_directory, ready.session_directory);
  assert.equal(other.hooks.length, 0);
  assert.deepEqual(await fs.readFile(f.owner), before);
  await assert.rejects(fs.stat(join(repeat.evidence_path, 'activation.json')), {code: 'ENOENT'});

  const stopped = await run(process.execPath, [activation, 'resident-stop', ready.session_directory]);
  assert.equal(stopped.exit_code, 0, stopped.output);
  await running;
  assert.equal(h.inflight(), 0);

  const actions = h.events.map(e => e.action);
  assert(actions.every(action => ['wait', 'finalize', 'stop'].includes(action)));
  assert.equal(actions.filter(action => action === 'finalize').length, 1);
  assert.equal(actions.indexOf('stop'), actions.length - 1);
  assert(h.events.every(e => e.schema_version === 1 && e.kind === 'listener_lifecycle'));
  const decisions = await Promise.all(h.hooks);
  assert.equal(decisions.length, 1);
  assert.match(decisions[0].reason, /^Supervision preflight only/);
  const acquired = h.events.findIndex(e => e.reason === 'owner_acquired');
  assert(acquired > 0);
  assert(h.events.slice(0, acquired).every(e => e.operation_id === null));
  assert(h.events.slice(acquired).every(e => e.operation_id === ready.operation_id));
  assert.deepEqual([h.events.at(-1).state, h.events.at(-1).reason], ['stopped', 'resident_stopped']);
  assert.equal(h.calls.sends, 0);
  await fs.stat(join(ready.session_directory, 'resident-joined.json'));

  const recorded = await receipts(ready.evidence_path);
  assert.deepEqual(recorded.map(r => r.name.replace(/^receipt-\d{3}-/, '')),
    ['starting.json', 'qualifying.json', 'armed.json', 'starting.json', 'starting.json', 'ready.json', 'stopped.json']);
  assert(recorded.every(r => r.value.task === P && r.value.turn === 'turn-1'));
  assert(recorded.slice(1).every(r => r.value.operation_id === ready.operation_id));
  assert.equal(recorded[0].value.revision.activation_sha256, sha(await fs.readFile(activation)));
  assert.equal(recorded[0].value.host_build.output, 'fixture host build\n');
  const armed = recorded[2].value;
  assert.equal(armed.action, 'finalize');
  assert.equal(armed.deadlineAt - armed.armedAt, 120000);
});

// Conservative host: a yield without a pending call cannot deliver anything.
// Wall-clock timings are scaled by 10; native startup itself runs unchanged.
for (const initialWait of [60000, 1000]) test('pending-call delivery with initial wait ' + initialWait, async t => {
  const f = await fixture(t), home = await installedHome(f);
  const h = host(f, P, 'turn-buffered', {stopHook: false,
    env: {HOME: home, CPD_TEST_MODE: 'normal', CPD_TEST_HOOK: '{}'}});
  const realTimeout = globalThis.setTimeout;
  t.mock.method(globalThis, 'setTimeout', (callback, delay, ...args) =>
    realTimeout(callback, delay === 120000 ? 12000 : delay, ...args));
  let pending = false, pendingTimer, modelTimer, completed = false, flushes = 0;
  const buffered = [], batches = [], hooks = [];
  function deliver() {
    if (!pending) return;
    pending = false; clearTimeout(pendingTimer);
    const batch = buffered.splice(0); batches.push(batch);
    if (completed) return;
    modelTimer = realTimeout(() => {
      const last = batch.at(-1);
      if (last?.action === 'finalize') hooks.push(hook(P, 'turn-buffered'));
      else if (last?.action !== 'stop') wait(3000);
    }, 5200);
  }
  function wait(ms) { pending = true; pendingTimer = realTimeout(deliver, ms); }
  t.after(() => { completed = true; clearTimeout(pendingTimer); clearTimeout(modelTimer); });
  const code = (await bootstrap()).replace('"yield_time_ms":60000', '"yield_time_ms":' + initialWait);
  wait(initialWait / 10);
  const running = new AF('tools', 'text', 'yield_control', code)(h.tools, value => {
    h.text(value); buffered.push(value);
  }, async () => { flushes++; assert.equal(h.inflight(), 0); deliver(); });
  const outcome = await Promise.race([h.until(v => v?.state === 'ready' && v.action === 'wait'),
    running.then(() => h.events.at(-1))]);
  completed = true; clearTimeout(pendingTimer); clearTimeout(modelTimer);
  assert.equal(flushes, 1);
  assert.equal(h.calls.sends, 0);
  if (initialWait === 60000) {
    assert.equal(outcome.admission_observed, true);
    assert.equal(batches[0].at(-1).action, 'finalize');
    assert.equal(hooks.length, 1);
    await Promise.all(hooks);
    const stopped = await run(process.execPath, [activation, 'resident-stop', outcome.session_directory]);
    assert.equal(stopped.exit_code, 0, stopped.output);
  } else {
    assert.equal(outcome.reason, 'qualification_timeout', 'old early return must reproduce the timeout');
    assert.equal(hooks.length, 0);
    assert.equal(JSON.parse(await fs.readFile(f.owner, 'utf8')).session, null);
  }
  await running;
});

test('missing yield_control rejects lifecycle before acquisition', async t => {
  const f = await fixture(t), h = host(f, P, 'turn-no-flush');
  const before = await fs.readFile(f.owner);
  const call = await a.listenerLifecycleCall(planNow(), f.clients);
  await assert.rejects(new AF('tools', 'text', call.arguments.code)(h.tools, h.text), /yield_control required/);
  assert.deepEqual(await fs.readFile(f.owner), before);
  assert.equal(h.hooks.length, 0);
  assert.equal(h.calls.sends, 0);
});

test('qualification timeout: terminal receipt, no readiness, late Stop never qualifies, moved authority stays blocked', async t => {
  const f = await fixture(t), h = host(f, P, 'turn-1', {stopHook: false});
  const clock = shortDeadline(t);
  const call = await a.listenerLifecycleCall(planNow(), f.clients);
  await h.launch(call.arguments.code);
  clock.mock.restore();

  const last = h.events.at(-1);
  assert.deepEqual([last.state, last.action, last.reason], ['failed', 'stop', 'qualification_timeout']);
  assert.doesNotMatch(last.diagnostic, /new Listener task/);
  assert.match(last.diagnostic, /when the finalize instruction reached the model/);
  assert.match(last.diagnostic, /new turn of this same Listener task/);
  assert(last.armed_at <= last.finalize_yielded_at);
  assert(last.finalize_yielded_at < last.deadline_at);
  assert.equal(h.events.filter(e => e.action === 'finalize').length, 1);
  assert(!h.events.some(e => e.state === 'ready'));
  const owner = JSON.parse(await fs.readFile(f.owner, 'utf8'));
  assert.deepEqual([owner.generation, owner.parent, owner.session], [2, P, null]);
  const terminalRaw = await fs.readFile(last.terminal_receipt, 'utf8'), terminal = JSON.parse(terminalRaw);
  assert.deepEqual([terminal.kind, terminal.operation, terminal.generation, terminal.owner, terminal.parent,
    terminal.turn, terminal.waiter, terminal.qualified, terminal.send_authorized],
  ['resident_supervision_terminal', owner.qualification.startup.operation_id, 2, owner.owner, P,
    'turn-1', 'closed', false, false]);
  assert((await receipts(call.evidence_path)).some(r => r.value.state === 'failed' && r.value.reason === 'qualification_timeout'));

  // A late Stop is released for this exact idle acquisition and never qualifies.
  assert.deepEqual(await hook(P, 'turn-1'), {});
  assert.equal(await fs.readFile(last.terminal_receipt, 'utf8'), terminalRaw);
  const status = JSON.parse((await run('python3', [root + 'bin/pro-dispatch', 'status', '--current'])).output);
  const trusted = {parent: P, configDir: status.paths.config_dir, stateDir: status.paths.state_dir};
  await assert.rejects(a.requireResidentSupervision(h.g, h.meta, trusted));

  // Direct open out of sequence cannot advertise readiness or bind a session.
  const packet = join(call.evidence_path, 'activation.json');
  const open = await a.readResidentPacketCall(packet, 'open', sha(await fs.readFile(packet)));
  await assert.rejects(h.launch(open.arguments.code), /supervision proof|Supervision/i);
  assert.equal(JSON.parse(await fs.readFile(f.owner, 'utf8')).session, null);

  // A repeated launch in the same turn is refused before any acquisition.
  const bytes = await fs.readFile(f.owner);
  const again = host(f, P, 'turn-1', {stopHook: false});
  Object.assign(again.g, h.g);
  await again.launch((await a.listenerLifecycleCall(planNow(), f.clients)).arguments.code);
  assert.deepEqual([again.events.at(-1).state, again.events.at(-1).reason], ['blocked', 'probe_already_attempted_this_turn']);
  assert.deepEqual(await fs.readFile(f.owner), bytes);

  // Moved authority in the same task: the old receipt releases nothing.
  commitAs(P, 'turn-2');
  const moved = await hook(P, 'turn-1');
  assert.equal(moved.decision, 'block');
  assert.match(moved.reason, /terminal receipt/);
  // Authority in another task: this task owns nothing and is not trapped.
  commitAs(Q, 'turn-q');
  assert.deepEqual(await hook(P, 'turn-1'), {});
  assert.equal(await fs.readFile(last.terminal_receipt, 'utf8'), terminalRaw);
  await assert.rejects(a.requireResidentSupervision(h.g, h.meta, trusted));
});

test('missing native surface fails before identity capture, receipts, or owner mutation', async t => {
  const f = await fixture(t), h = host(f, P, 'turn-1');
  const before = await fs.readFile(f.owner);
  const call = await a.listenerLifecycleCall(planNow(), f.clients);
  for (const missing of ['mcp__codex_app__read_thread', 'mcp__node_repl__js', 'exec_command']) {
    const partial = {...h.tools};
    delete partial[missing];
    await assert.rejects(new AF('tools', 'text', call.arguments.code)(partial, h.text), /unsupported_listener_surface/);
  }
  assert.equal(h.events.length, 0);
  assert.deepEqual(await fs.readFile(f.owner), before);
  assert.deepEqual((await receipts(call.evidence_path)).length, 0);
});

test('constant bootstrap reports a blocked start without acquiring anything', async t => {
  const f = await fixture(t, {pool: false}), home = await installedHome(f);
  const h = host(f, P, 'turn-1', {env: {HOME: home, CPD_TEST_MODE: 'normal', CPD_TEST_HOOK: '{}'}});
  await h.launch(await bootstrap());
  assert.equal(h.events.length, 1);
  const [event] = h.events;
  assert.deepEqual([event.state, event.action, event.reason], ['blocked', 'stop', 'listener_start_blocked']);
  assert.deepEqual(event.details.blockers.map(b => b.reason), ['pool_not_configured']);
  await assert.rejects(fs.stat(f.owner), {code: 'ENOENT'});
  await assert.rejects(fs.stat(f.clients), {code: 'ENOENT'});
});

// Plugin copies sit where the host installs them (observed with codex-cli
// 0.158.0-alpha.2): $CODEX_HOME/plugins/cache/<marketplace>/<plugin>/<version>.
// These are isolated fixtures, not verification of an actual plugin install.
const pluginCopy = (codexHome, version) => codexHome + '/plugins/cache/codex-pro-dispatch/codex-pro-dispatch/' + version;
const entry = '/skills/codex-pro-dispatch/scripts/pro-dispatch';
async function stub(path, reason) {
  await fs.mkdir(dirname(path), {recursive: true, mode: 0o700});
  await fs.writeFile(path, '#!/bin/sh\necho ' + quote(J({error: reason})) + '\n', {mode: 0o700});
}

test('constant bootstrap selects one installation and never falls back to another copy', async t => {
  const f = await fixture(t, {pool: false}), code = await bootstrap();
  const launch = async (name, setup) => {
    const home = f.root + '/select-' + name, codexHome = home + '/.codex';
    await fs.mkdir(home, {mode: 0o700});
    await setup(home, codexHome);
    const h = host(f, P, 'turn-' + name, {env: {HOME: home, CODEX_HOME: codexHome}});
    await h.launch(code);
    assert.equal(h.events.length, 1);
    return h.events[0];
  };
  // The source-install link wins whenever it exists.
  let e = await launch('source', async (home, c) => {
    await stub(home + '/.local/bin/pro-dispatch', 'source_selected');
    await stub(pluginCopy(c, '1.3.1') + entry, 'plugin_selected');
  });
  assert.equal(e.reason, 'source_selected');
  e = await launch('plugin', (home, c) => stub(pluginCopy(c, '1.3.1') + entry, 'plugin_selected'));
  assert.equal(e.reason, 'plugin_selected');
  // A broken source link is reported; the plugin copy is not used instead.
  e = await launch('broken', async (home, c) => {
    await fs.mkdir(home + '/.local/bin', {recursive: true});
    await fs.symlink(home + '/removed-checkout/pro-dispatch', home + '/.local/bin/pro-dispatch');
    await stub(pluginCopy(c, '1.3.1') + entry, 'plugin_selected');
  });
  assert.deepEqual([e.state, e.action, e.reason], ['blocked', 'stop', 'listener_start_unavailable']);
  assert([126, 127].includes(e.exit_code), String(e.exit_code)); // macOS /bin/sh: 126
  assert.match(e.output, /\.local\/bin\/pro-dispatch/);
  assert.doesNotMatch(e.output, /plugin_selected/);
  // No source link and two plugin copies, or none: nothing is chosen.
  for (const versions of [['1.3.1', '1.3.2'], []]) {
    e = await launch('versions-' + versions.length, async (home, c) => {
      for (const version of versions) await stub(pluginCopy(c, version) + entry, 'plugin_selected');
    });
    assert.deepEqual([e.reason, e.exit_code], ['listener_start_unavailable', 127]);
    assert.match(e.output, /not installed exactly once/);
    assert.doesNotMatch(e.output, /plugin_selected/);
  }
  await assert.rejects(fs.stat(f.owner), {code: 'ENOENT'});
});

test('constant bootstrap runs a plugin copy whose hook check names that copy', async t => {
  const f = await fixture(t, {pool: false});
  const home = f.root + '/plugin-home', codexHome = home + '/.codex', copy = pluginCopy(codexHome, '1.3.1');
  for (const part of ['.codex-plugin', 'VERSION', 'bin', 'hooks', 'skills', 'src'])
    await fs.cp(root + part, copy + '/' + part, {recursive: true, filter: path => !path.includes('__pycache__')});
  const h = host(f, P, 'turn-1', {env: {HOME: home, CODEX_HOME: codexHome}});
  await h.launch(await bootstrap());
  assert.equal(h.events.length, 1);
  const [event] = h.events;
  assert.deepEqual([event.state, event.action, event.reason], ['blocked', 'stop', 'listener_start_blocked']);
  // Hook discovery uses the host's own CLI when present; either way the check
  // is bound to the copy that ran, and nothing is acquired.
  const preflight = event.details.hook_preflight;
  assert(preflight.expected_command.includes(await fs.realpath(copy) + '/skills/codex-pro-dispatch/scripts/resident-supervision.mjs'));
  assert.equal(preflight.codex_home, codexHome);
  const reasons = event.details.blockers.map(b => b.reason);
  assert(reasons.includes('pool_not_configured'));
  assert(reasons.every(r => r === 'pool_not_configured' || r.startsWith('listener_hook_')), J(reasons));
  await assert.rejects(fs.stat(f.owner), {code: 'ENOENT'});
  await assert.rejects(fs.stat(home + '/.cpd-client'), {code: 'ENOENT'});
});

for (const lost of [false, true])
test('two concurrent starts leave exactly one serving execution' + (lost ? ' after a lost execution' : ''), async t => {
  const f = await fixture(t);
  if (lost) await lostExecution(f);
  const hosts = [host(f, P, 'turn-p'), host(f, Q, 'turn-q')];
  const calls = [await a.listenerLifecycleCall(planNow(), f.clients), await a.listenerLifecycleCall(planNow(), f.clients)];
  const runs = hosts.map((h, i) => h.launch(calls[i].arguments.code));
  // A loser can only end (action stop). The winner reaches live admission.
  const winner = await Promise.race(hosts.map(h => h.until(v => v?.state === 'ready' && v.action === 'wait', 90000).then(() => h)));
  const loser = hosts.find(h => h !== winner);
  const end = await loser.until(v => v?.action === 'stop', 90000);
  assert.notEqual(end.state, 'stopped');
  const owner = JSON.parse(await fs.readFile(f.owner, 'utf8'));
  const live = winner.events.findLast(v => v.state === 'ready' && v.action === 'wait');
  assert.equal(owner.parent, winner.meta.threadId);
  assert.equal(owner.session.directory, live.session_directory);
  const status = JSON.parse((await run(process.execPath, [activation, 'resident-ready', live.session_directory, '25000'])).output);
  assert.equal(status.admissionObserved, true);
  assert.equal((await run(process.execPath, [activation, 'resident-stop', live.session_directory])).exit_code, 0);
  await Promise.all(runs);
  assert.deepEqual([winner.events.at(-1).state, winner.events.at(-1).action], ['stopped', 'stop']);
  assert(hosts.every(h => h.calls.sends === 0));
  t.diagnostic('loser ended as ' + J({state: end.state, reason: end.reason}));
});

// Fable review P1: the readiness observer is auxiliary. Refused or empty, it
// only reports "not observed"; the serve stays owned and awaited by this cell.
for (const observer of ['reject', 'unobserved']) {
  test(`readiness observer ${observer}: serving stays awaited and is never reported ready`, async t => {
    const f = await fixture(t), h = host(f, P, 'turn-1', {observer});
    let settled = false;
    const running = h.launch((await a.listenerLifecycleCall(planNow(), f.clients)).arguments.code)
      .finally(() => { settled = true; });
    const missed = await h.until(v => v?.reason === 'admission_not_observed');
    assert.deepEqual([missed.state, missed.action, missed.admission_observed], ['starting', 'wait', false]);
    if (observer === 'reject') {
      assert.match(missed.observer_error, /Host refused a concurrent tool call/);
      assert.equal(h.calls.observers, 1);
    } else {
      assert.equal(missed.observer_error, null);
      assert.equal(h.calls.observers, 3);
    }
    // The serve the observer missed is live, and this cell is still running it.
    const live = JSON.parse((await run(process.execPath, [activation, 'resident-ready', missed.session_directory, '25000'])).output);
    assert.equal(live.admissionObserved, true);
    assert.equal(settled, false);
    assert(!h.events.some(e => e.state === 'ready' || e.action === 'stop'));
    assert.equal((await run(process.execPath, [activation, 'resident-stop', missed.session_directory])).exit_code, 0);
    await running;
    assert.deepEqual([h.events.at(-1).state, h.events.at(-1).action, h.events.at(-1).reason],
      ['stopped', 'stop', 'resident_stopped']);
    assert.equal(h.events.filter(e => e.action === 'stop').length, 1);
    assert(!h.events.some(e => e.admission_observed === true));
    assert.equal(h.inflight(), 0);
    assert.equal(h.calls.sends, 0);
    await fs.stat(join(missed.session_directory, 'resident-joined.json'));
  });
}

test('a yielded observer is drained before readiness; relayed serve events never restate admission', async t => {
  const f = await fixture(t), h = host(f, P, 'turn-1', {observer: 'yield'});
  const running = h.launch((await a.listenerLifecycleCall(planNow(), f.clients)).arguments.code);
  const ready = await h.until(v => v?.state === 'ready' && v.action === 'wait');
  assert.equal(ready.admission_observed, true);
  assert(h.calls.observers >= 1);
  assert.equal(h.calls.drains, h.calls.observers);
  assert.equal(h.yielded.size, 0);
  assert.equal((await run(process.execPath, [activation, 'resident-stop', ready.session_directory])).exit_code, 0);
  await running;
  assert.equal(h.inflight(), 0);
  assert.deepEqual(h.events.filter(e => e.admission_observed === true).map(e => e.reason), ['current_owner_waiting']);
  assert(h.events.some(e => e.reason === 'serve_event' && e.state === 'ready' && e.admission_observed === false));
  assert.deepEqual([h.events.at(-1).state, h.events.at(-1).reason], ['stopped', 'resident_stopped']);
});

test('an open failure after qualification stops with the acquired owner unbound', async t => {
  const f = await fixture(t), h = host(f, P, 'turn-1', {refuse: title => title === 'Resident pool open'});
  await h.launch((await a.listenerLifecycleCall(planNow(), f.clients)).arguments.code);
  await Promise.all(h.hooks);
  const last = h.events.at(-1);
  assert.deepEqual([last.state, last.action, last.reason], ['failed', 'stop', 'open_failed']);
  assert.match(last.error, /Host refused Resident pool open/);
  assert(h.events.some(e => e.reason === 'stop_observed'));
  assert(!h.events.some(e => e.state === 'ready' || e.reason === 'serve_event'));
  const owner = JSON.parse(await fs.readFile(f.owner, 'utf8'));
  assert.deepEqual([owner.parent, owner.generation, owner.session], [P, 2, null]);
  assert.equal(h.calls.observers, 0);
  assert.equal(h.inflight(), 0);
  assert.equal(h.calls.sends, 0);
});

test('a serve failure stops as serve_failed with no readiness and no running observer', async t => {
  const f = await fixture(t);
  const h = host(f, P, 'turn-1', {observer: 'unobserved', refuse: title => title === 'Resident pool owner'});
  await h.launch((await a.listenerLifecycleCall(planNow(), f.clients)).arguments.code);
  const last = h.events.at(-1);
  assert.deepEqual([last.state, last.action, last.reason], ['failed', 'stop', 'serve_failed']);
  assert.match(last.error, /Host refused Resident pool owner/);
  assert(h.events.some(e => e.reason === 'listener_open_not_yet_waiting'));
  assert(!h.events.some(e => e.state === 'ready'));
  assert.equal(h.events.filter(e => e.action === 'stop').length, 1);
  assert.equal(h.inflight(), 0);
  assert.equal(h.calls.sends, 0);
});

test('a stale start regenerates once from canonical state for the same destinations', async t => {
  const f = await fixture(t), h = host(f, P, 'turn-1', {stopHook: false});
  const call = await a.listenerLifecycleCall(planNow(), f.clients);
  commitAs(Q, 'turn-q'); // canonical authority moves after the plan was taken
  shortDeadline(t);
  await h.launch(call.arguments.code);
  const owner = JSON.parse(await fs.readFile(f.owner, 'utf8'));
  assert.deepEqual([owner.parent, owner.generation], [P, 3]);
  assert.deepEqual(JSON.parse(owner.worker_pool_json).workers.map(w => w.conversation_id), ['worker-a', 'worker-b']);
  const acquired = h.events.find(e => e.reason === 'owner_acquired');
  assert.deepEqual([acquired.operation_id, acquired.generation], [owner.qualification.startup.operation_id, 3]);
  assert.deepEqual([h.events.at(-1).state, h.events.at(-1).reason], ['failed', 'qualification_timeout']);
});

test('a commit whose answer was lost is re-inspected as the same operation, never acquired twice', async t => {
  const f = await fixture(t), h = host(f, P, 'turn-1', {stopHook: false});
  const commits = await flakyCommit(t, f, 'lost');
  shortDeadline(t);
  await h.launch((await a.listenerLifecycleCall(planNow(), f.clients)).arguments.code);
  assert.equal(await commits(), 2);
  const owner = JSON.parse(await fs.readFile(f.owner, 'utf8'));
  assert.deepEqual([owner.parent, owner.generation], [P, 2]);
  assert.equal(h.events.find(e => e.reason === 'owner_acquired').operation_id, owner.qualification.startup.operation_id);
  assert.deepEqual([h.events.at(-1).state, h.events.at(-1).reason], ['failed', 'qualification_timeout']);
});

for (const [mode, reason] of [['unknown', 'commit_unknown'], ['stale', 'expected_state_changed']]) {
  test(`a repeated ${mode} commit stops after one retry without acquiring`, async t => {
    const f = await fixture(t), h = host(f, P, 'turn-1');
    const before = await fs.readFile(f.owner);
    const commits = await flakyCommit(t, f, mode);
    const call = await a.listenerLifecycleCall(planNow(), f.clients);
    await h.launch(call.arguments.code);
    assert.equal(await commits(), 2);
    assert.equal(h.events.length, 2);
    assert.deepEqual([h.events.at(-1).state, h.events.at(-1).action, h.events.at(-1).reason], ['failed', 'stop', reason]);
    assert.deepEqual(await fs.readFile(f.owner), before);
    assert.equal(h.hooks.length, 0);
    await assert.rejects(fs.stat(join(call.evidence_path, 'activation.json')), {code: 'ENOENT'});
    assert.equal(h.inflight(), 0);
  });
}

// Synthetic app-restart recovery. The Stops are simulated hook runs, not host
// evidence; they prove the runtime path, not real desktop behavior.
test('app restart: a later turn of the owner task recovers the same chats and fences the lost generation', async t => {
  const f = await fixture(t), home = await installedHome(f), lost = await lostExecution(f);
  const evidence = await files(lost.directory);
  const prompt = await hook(P, 'turn-1', true);
  assert.equal(prompt.decision, 'block');
  assert.match(prompt.reason, /served by an earlier turn[\s\S]*\/references\/listener-start\.md /);
  const h = host(f, P, 'turn-1', {env: {HOME: home, CPD_TEST_MODE: 'normal', CPD_TEST_HOOK: '{}'}});
  const running = h.launch(await bootstrap());
  const ready = await h.until(v => v?.state === 'ready' && v.action === 'wait');
  assert.equal(ready.admission_observed, true);
  const owner = JSON.parse(await fs.readFile(f.owner, 'utf8'));
  assert.deepEqual([owner.generation, owner.parent, owner.worker_pool_sha256, owner.qualification.startup.native_turn],
    [lost.owner.generation + 1, P, lost.owner.worker_pool_sha256, 'turn-1']);
  assert.equal(owner.qualification.last_exclusion.kind, 'destination_exclusion');
  assert.equal(owner.session.directory, ready.session_directory);
  assert.notEqual(ready.session_directory, lost.directory);
  assert.deepEqual(await files(lost.directory), evidence);
  const check = await run('python3', [root + 'bin/pro-dispatch', 'resident', 'check', J(lost.owner)]);
  assert.notEqual(check.exit_code, 0, 'the lost generation is fenced');
  assert.deepEqual((await Promise.all(h.hooks)).map(d => d.reason.split(':')[0]), ['Supervision preflight only']);
  // The recovered owner is served by this turn and keeps the original-cell rule.
  for (const active of [false, true]) assert.match((await hook(P, 'turn-1', active)).reason, /ORIGINAL/);
  assert.equal((await run(process.execPath, [activation, 'resident-stop', ready.session_directory])).exit_code, 0);
  await running;
  assert.deepEqual([h.events.at(-1).state, h.events.at(-1).reason], ['stopped', 'resident_stopped']);
  assert.equal(h.calls.sends, 0);
  assert.deepEqual(await hook(P, 'turn-1'), {});
});

test('app restart with the old execution still serving: the later turn observes it and changes nothing', async t => {
  const f = await fixture(t), h0 = host(f, P, 'turn-0');
  const serving = h0.launch((await a.listenerLifecycleCall(planNow(), f.clients)).arguments.code);
  const ready = await h0.until(v => v?.state === 'ready' && v.action === 'wait');
  const before = await fs.readFile(f.owner);
  assert.equal((await hook(P, 'turn-1', true)).decision, 'block');
  const h1 = host(f, P, 'turn-1');
  await h1.launch((await a.listenerLifecycleCall(planNow(), f.clients)).arguments.code);
  const observed = h1.events.at(-1);
  assert.deepEqual([observed.state, observed.action, observed.reason], ['ready', 'stop', 'existing_service_observed']);
  assert.deepEqual(await fs.readFile(f.owner), before);
  assert.deepEqual(await hook(P, 'turn-1', true), {});
  assert.match((await hook(P, 'turn-0', true)).reason, /ORIGINAL/);
  assert.equal((await run(process.execPath, [activation, 'resident-stop', ready.session_directory])).exit_code, 0);
  await serving;
  assert.equal(h0.calls.sends + h1.calls.sends, 0);
});

test('a lost generation that armed a send keeps its chats: one report, nothing acquired, the turn is released', async t => {
  const f = await fixture(t), home = await installedHome(f);
  await lostExecution(f, {armed: true});
  assert.equal((await hook(P, 'turn-1', true)).decision, 'block');
  const before = await fs.readFile(f.owner);
  const h = host(f, P, 'turn-1', {env: {HOME: home, CPD_TEST_MODE: 'normal', CPD_TEST_HOOK: '{}'}});
  await h.launch(await bootstrap());
  const last = h.events.at(-1);
  assert.deepEqual([last.state, last.action], ['blocked', 'stop']);
  assert.match(last.reason, /original_execution_join_required/);
  assert.deepEqual(await fs.readFile(f.owner), before);
  assert.equal(h.hooks.length, 0);
  assert.deepEqual(await hook(P, 'turn-1', true), {});
  // Each later turn gets one prompt; no turn loops.
  assert.equal((await hook(P, 'turn-2', true)).decision, 'block');
  assert.deepEqual(await hook(P, 'turn-2', true), {});
  assert.equal(h.calls.sends, 0);
});
