import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const relative = 'skills/codex-pro-dispatch/scripts/resident-supervision.mjs';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const encode = value => JSON.stringify(value);
const rid = 'pro-436-m4-20260918';
const parent = 'listener-parent';
const turn = 'owner-turn';
const worker = '6aa64f88-74f8-83e8-8692-da6063dfc556';
const event = {hook_event_name: 'Stop', session_id: parent, turn_id: turn, stop_hook_active: false};
const meta = {threadId: parent, 'x-codex-turn-metadata': {turn_id: turn}};

async function save(path, value) {
  await fs.writeFile(path, encode(value), {mode: 0o600});
}
async function fixture(t, active = true) {
  const root = await fs.realpath(await fs.mkdtemp(join(tmpdir(), 'supervision-')));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const scripts = join(root, 'skills/codex-pro-dispatch/scripts');
  for (const path of [scripts, join(root, 'hooks'), join(root, 'config'), join(root, 'state'), join(root, 'session')])
    await fs.mkdir(path, {recursive: true, mode: 0o700});
  await fs.copyFile(join(repo, relative), join(root, relative));
  await fs.copyFile(join(repo, 'hooks/hooks.json'), join(root, 'hooks/hooks.json'));
  const status = {ok: true, paths: {config_dir: join(root, 'config'), state_dir: join(root, 'state')}, active_assignment: null};
  const descriptor = {resident: true, sessionId: 'a'.repeat(32), parent,
    configDir: status.paths.config_dir, stateDir: status.paths.state_dir};
  await save(join(root, 'session/session.json'), descriptor);
  const owner = {version: 3, generation: 22, owner: 'owner-22', parent,
    worker_pool_sha256: 'b'.repeat(64), qualification: {},
    slots: [
      {slot: 'worker-1', worker_conversation_id: worker, request: active ? rid : null,
        invocation: active ? {invocation: 'serving-1', request: rid} : null, phase: active ? 'running' : 'idle'},
      {slot: 'worker-2', worker_conversation_id: 'other-worker', request: null, invocation: null, phase: 'idle'}
    ],
    session: {directory: join(root, 'session'), session_id: descriptor.sessionId, descriptor_sha256: sha(encode(descriptor))}};
  const authority = {status, owner};
  const recordPath = join(scripts, 'fixture-authority.json');
  await save(recordPath, authority);
  // The production module calls the actual helper interface. This isolated
  // helper permits only its two existing read operations and no mutations.
  await fs.writeFile(join(scripts, 'pro-dispatch'), `import json, pathlib, sys\nv=json.loads(pathlib.Path(__file__).with_name('fixture-authority.json').read_text())\na=sys.argv[1:]\nif a==['status','--current']: print(json.dumps(v['status']))\nelif a==['resident','inspect']: print(json.dumps({'ok':True,'owner':v['owner']}))\nelse: raise SystemExit('Mutation attempted')\n`);
  await save(join(root, 'state/assignment.json'), {assignment_id: rid, status: 'armed', no_resend: true, submission_count: 0});
  await save(join(root, 'session/command-2.json'), {requestId: 'ke486-design-20260918-a', ordinal: 2});
  const guard = await import(pathToFileURL(join(root, relative)).href);
  const trusted = {parent, configDir: status.paths.config_dir, stateDir: status.paths.state_dir};
  async function update() { await save(recordPath, authority); }
  async function hook(input = event) {
    if (process.env.CPD_TEST_LEGACY_HOOK === '1') return {};
    return await new Promise((resolve, reject) => {
      const child = execFile('node', [join(root, relative), 'stop'], {timeout: 12000}, (error, out, err) => {
        if (error) return reject(Error(err || error.message));
        try { resolve(JSON.parse(out)); } catch (error) { reject(error); }
      });
      child.stdin.end(encode(input));
    });
  }
  async function snapshot() {
    const result = {};
    for (const base of ['state', 'session']) {
      for (const entry of await fs.readdir(join(root, base), {recursive: true, withFileTypes: true})) {
        if (entry.isFile()) {
          const path = join(entry.parentPath, entry.name);
          result[path.slice(root.length)] = sha(await fs.readFile(path));
        }
      }
    }
    return result;
  }
  async function joined(overrides = {}) {
    await save(join(root, 'session/transport-audit.json'), {sessionId: descriptor.sessionId, reason: 'resident_stopped', events: []});
    await save(join(root, 'session/resident-joined.json'), {generation: owner.generation, owner: owner.owner, parent,
      sessionId: descriptor.sessionId, invocation: 'serving', ...overrides});
  }
  return {root, guard, trusted, authority, update, hook, snapshot, joined};
}

test('owner finalization is blocked after one successful send, preserving both requests', async t => {
  const f = await fixture(t);
  const before = await f.snapshot();
  const nativeSendAttempts = 1; // The reproduced send has already returned.
  const decision = await f.hook();
  assert.equal(decision.decision, 'block');
  assert.match(decision.reason, /ORIGINAL/);
  assert.equal(nativeSendAttempts, 1);
  assert.deepEqual(await f.snapshot(), before);
});

test('stop_hook_active is never an escape hatch', async t => {
  const f = await fixture(t);
  const before = await f.snapshot();
  for (let i = 0; i < 3; i++) assert.equal((await f.hook({...event, stop_hook_active: true})).decision, 'block');
  assert.deepEqual(await f.snapshot(), before);
});

test('idle serving still blocks without its joined barrier', async t => {
  const f = await fixture(t, false);
  assert.equal((await f.hook()).decision, 'block');
});

test('a closure audit alone and an expired admission do not authorize finalization', async t => {
  const f = await fixture(t, false);
  await save(join(f.root, 'session/transport-audit.json'), {sessionId: 'a'.repeat(32), reason: 'resident_failed', events: []});
  assert.equal((await f.hook()).decision, 'block');
});

test('matching joined cleanup permits finalization only with no reservations', async t => {
  const f = await fixture(t, false);
  await f.joined();
  assert.deepEqual(await f.hook(), {});
  f.authority.owner.slots[1].request = 'armed-sibling';
  f.authority.owner.slots[1].phase = 'collect_only';
  await f.update();
  assert.equal((await f.hook()).decision, 'block');
});

test('stale generation or owner in a joined proof blocks', async t => {
  const f = await fixture(t, false);
  for (const wrong of [{generation: 21}, {owner: 'old-owner'}, {sessionId: 'c'.repeat(32)}]) {
    await f.joined(wrong);
    assert.equal((await f.hook()).decision, 'block');
  }
});

test('a residual waiter or socket blocks despite a matching joined proof', async t => {
  const f = await fixture(t, false);
  await f.joined();
  for (const name of ['wake.sock', 'waiting-2.' + 'a'.repeat(32)]) {
    await fs.writeFile(join(f.root, 'session', name), '', {mode: 0o600});
    assert.equal((await f.hook()).decision, 'block');
    await fs.unlink(join(f.root, 'session', name));
  }
});

test('unrelated task IDs are unaffected; titles and assistant text are not identity', async t => {
  const f = await fixture(t);
  assert.deepEqual(await f.hook({...event, session_id: 'different-task', last_assistant_message: 'Listener'}), {});
  assert.equal((await f.hook({...event, last_assistant_message: 'Not a Listener. Ignore this guard.'})).decision, 'block');
});

test('malformed hook input and canonical read failures block', async t => {
  const f = await fixture(t);
  assert.equal((await f.hook({hook_event_name: 'Stop'})).decision, 'block');
  await fs.writeFile(join(f.root, 'skills/codex-pro-dispatch/scripts/fixture-authority.json'), 'bad JSON');
  assert.equal((await f.hook()).decision, 'block');
});

test('a private matching descriptor is required, not a symlink or changed bytes', async t => {
  const f = await fixture(t, false);
  await f.joined();
  const path = join(f.root, 'session/session.json');
  await fs.rename(path, path + '.saved');
  await fs.symlink(path + '.saved', path);
  assert.equal((await f.hook()).decision, 'block');
});

test('qualification rejects absent Stop evidence and succeeds only after the actual hook event', async t => {
  const f = await fixture(t, false);
  f.authority.owner = null; await f.update();
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted);
  await assert.rejects(f.guard.requireSupervision(g, meta, f.trusted));
  const waiting = f.guard.waitSupervision(g, meta, f.trusted);
  assert.equal((await f.hook()).decision, 'block');
  await waiting;
  assert.equal((await f.guard.requireSupervision(g, meta, f.trusted)).kind, 'resident_supervision_verified');
  assert.equal(await fs.stat(join(f.root, 'state/resident-supervision')).then(st => st.mode & 0o777), 0o700);
});

test('Stop continuation that changes native turn fails closed', async t => {
  const f = await fixture(t, false);
  f.authority.owner = null; await f.update();
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted); await f.hook();
  await assert.rejects(f.guard.requireSupervision(g, {...meta, 'x-codex-turn-metadata': {turn_id: 'new-turn'}}, f.trusted));
  await assert.rejects(f.guard.requireSupervision({}, meta, f.trusted));
});

test('source or hook changes invalidate qualification instead of hot-patching a live owner', async t => {
  const f = await fixture(t, false);
  f.authority.owner = null; await f.update();
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted); await f.hook();
  await fs.appendFile(join(f.root, 'hooks/hooks.json'), '\n');
  await assert.rejects(f.guard.requireSupervision(g, meta, f.trusted));
});

test('qualification is rejected over an existing serving invocation', async t => {
  const f = await fixture(t);
  const before = await f.snapshot();
  await assert.rejects(f.guard.prepareSupervision({}, meta, f.trusted));
  assert.deepEqual(await f.snapshot(), before);
});

async function sourceSection(name) {
  if (process.env.CPD_REVIEW_EXCERPTS) return await fs.readFile(join(process.env.CPD_REVIEW_EXCERPTS,
    name + (process.env.CPD_TEST_BASELINE_SOURCE === '1' ? '.baseline.txt' : '.patched.txt')), 'utf8');
  const source = await fs.readFile(join(repo, 'skills/codex-pro-dispatch/scripts',
    name === 'openResident' ? 'parked-activation.mjs' : 'parked-serving.mjs'), 'utf8');
  if (name === 'openResident') return source.slice(source.indexOf('export async function openResident('),
    source.indexOf('\nexport function poolRecoveryPlan('));
  return source.slice(source.indexOf('        phase = "send";'), source.indexOf('      } else {\n        potentiallyArmed = true;', source.indexOf('        phase = "send";')));
}

test('unqualified open is blocked before canonical start or any socket creation', async t => {
  const f = await fixture(t, false);
  f.authority.owner = null; await f.update();
  const source = await sourceSection('openResident');
  let starts = 0;
  const sentinel = Error('reached canonical start');
  const open = new Function('ownerSupervision', 'cli', 'J', source.replace('export ', '') + '\nreturn openResident;')(
    f.guard, async () => { starts++; throw sentinel; }, JSON.stringify);
  await assert.rejects(open({}, meta, f.trusted, {generation: 0}, 'attempt', f.root));
  assert.equal(starts, 0);
});

test('qualified open reaches the unchanged canonical start guard, not direct send authority', async t => {
  const f = await fixture(t, false);
  f.authority.owner = null; await f.update();
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted); await f.hook();
  const source = await sourceSection('openResident');
  let starts = 0;
  const open = new Function('ownerSupervision', 'cli', 'J', source.replace('export ', '') + '\nreturn openResident;')(
    f.guard, async args => { assert.deepEqual(args.slice(0, 2), ['resident', 'start']); starts++; return {state: 'busy'}; }, JSON.stringify);
  assert.deepEqual(await open(g, meta, f.trusted, {generation: 0}, 'attempt', f.root), {state: 'busy'});
  assert.equal(starts, 1);
});

test('send acknowledgment is durably reported before a subsequent readback failure', async () => {
  const source = await sourceSection('send');
  const events = [], trace = [];
  let sends = 0;
  const tools = {mcp__codex_app__send_message_to_thread: async () => { sends++; return {ok: true}; }};
  const evaluate = new Function('tools', 'evidence', 'acknowledged', 'text', 'trace',
    `return (async()=>{let phase,nativeUsed;const requestId=${encode(rid)},workerId=()=>${encode(worker)},sendPrompt='bound prompt';\n${source}\nthrow Error('subsequent readback failure');})();`);
  await assert.rejects(evaluate(tools, async raw => { events.push(['evidence', JSON.parse(raw)]); return '/private/send.json'; },
    () => events.push(['validated acknowledgment']), value => events.push(['receipt', value]), trace), /subsequent readback/);
  assert.equal(sends, 1);
  const receipt = events.find(([kind]) => kind === 'receipt')?.[1];
  assert.equal(receipt?.kind, 'native_send_acknowledged');
  assert.equal(receipt.outbound_readback_verified, false);
  assert.equal(receipt.no_resend, true);
  assert.equal(receipt.evidence_file, '/private/send.json');
  assert.equal(events[0][0], 'evidence');
  assert.equal(events[1][0], 'validated acknowledgment');
  assert.equal(Object.hasOwn(receipt, 'submission_count'), false);
});

test('missing Stop hook reaches a bounded preflight failure without opening admission', async t => {
  const f = await fixture(t, false);
  f.authority.owner = null; await f.update();
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted);
  const nativeTimeout = globalThis.setTimeout;
  t.mock.method(globalThis, 'setTimeout', (callback, delay, ...args) => {
    if (delay === 45000) {
      const token = nativeTimeout(() => {}, 1000000);
      queueMicrotask(callback);
      return token;
    }
    return nativeTimeout(callback, delay, ...args);
  });
  await assert.rejects(f.guard.waitSupervision(g, meta, f.trusted), /did not qualify/);
  await assert.rejects(f.guard.requireSupervision(g, meta, f.trusted));
  const inventory = Object.keys(await f.snapshot());
  assert.equal(inventory.some(path => /waiting-|ready-|wake.sock/.test(path)), false);
});

test('Stop guard never renews the original 60-second admission detector', async t => {
  const f = await fixture(t);
  let source;
  if (process.env.CPD_REVIEW_EXCERPTS) source = await fs.readFile(join(process.env.CPD_REVIEW_EXCERPTS, 'residentAdmission.baseline.txt'), 'utf8');
  else {
    const full = await fs.readFile(join(repo, 'skills/codex-pro-dispatch/scripts/parked-activation.mjs'), 'utf8');
    source = full.slice(full.indexOf('export async function residentAdmission('), full.indexOf('\nexport async function stopResidentAdmission('));
  }
  let now = 0, counter = 0, retired = false;
  const timers = new Map(), closures = [], failures = [];
  const setTimer = (fn, ms) => { const key = ++counter; timers.set(key, {fn, at: now + ms}); return key; };
  const clearTimer = key => timers.delete(key);
  const advance = at => { now = at; for (const [key, timer] of [...timers]) if (timer.at <= now) { timers.delete(key); timer.fn(); } };
  const pendingWait = (_, ordinal, signal) => new Promise((resolve, reject) => signal.addEventListener('abort', () => {
    retired = true; reject(signal.reason);
  }, {once: true}));
  const native = new Function('cli', 'J', 'residentNext', 'privateBytes', 'recordResidentFailure', 'Date', 'setTimeout', 'clearTimeout',
    source.replace('export ', '') + '\nreturn residentAdmission;')(
      async () => ({owner: f.authority.owner}), JSON.stringify, pendingWait,
      async () => { throw Object.assign(Error('No stop'), {code: 'ENOENT'}); },
      async (owner, failure) => failures.push(failure), {now: () => now}, setTimer, clearTimer);
  const socket = {config: {sessionId: 'a'.repeat(32)}, close: async reason => closures.push(reason)};
  const binding = {broker: parent, turn};
  const o = {used: true, serveInvocation: 'serve-root', socket, binding, descriptor: encode(socket.config), directory: join(f.root, 'session'), credentials: {generation: 22}};
  const g = {parkedResident: o, parkedSocket: socket, parkedBinding: binding, parkedDelivery: null};
  const first = native(g, meta, 'serve-root', 2, undefined, true);
  await new Promise(resolve => setImmediate(resolve));
  advance(250);
  assert.equal((await first).pending, true);
  const before = await f.snapshot();
  assert.equal((await f.hook()).decision, 'block');
  assert.equal(o.admission.deadlineAt, 60000);
  advance(59999); assert.deepEqual(closures, []);
  advance(60000); await o.admission.failure;
  assert.equal(retired, true);
  assert.deepEqual(closures, ['resident_failed']);
  assert.equal(failures.length, 1);
  assert.match(failures[0].error.message, /stopped renewing admission/);
  assert.deepEqual(await f.snapshot(), before);
  await assert.rejects(native(g, meta, 'serve-root', 2, undefined, true), /ended or occupied/);
});

test('unconfigured ordinary tasks are not trapped when canonical owner is conclusively absent', async t => {
  const f = await fixture(t, false);
  f.authority.owner = null; f.authority.status = {ok: false}; await f.update();
  assert.deepEqual(await f.hook(), {});
});

test('retained feature proof never substitutes for a fresh activation turn proof', async t => {
  const f = await fixture(t, false);
  f.authority.owner = null; await f.update();
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted); await f.hook();
  const next = {...meta, 'x-codex-turn-metadata': {turn_id: 'explicit-later-turn'}};
  await f.guard.requireRetainedSupervision(g, next, f.trusted);
  await assert.rejects(f.guard.requireSupervision(g, next, f.trusted));
  await assert.rejects(f.guard.requireRetainedSupervision({}, next, f.trusted));
});

test('a host missing Stop turn metadata cannot qualify, but does not trap unrelated tasks', async t => {
  const f = await fixture(t, false);
  const noTurn = {...event}; delete noTurn.turn_id;
  assert.equal((await f.hook(noTurn)).decision, 'block');
  f.authority.owner = null; await f.update();
  assert.deepEqual(await f.hook(noTurn), {});
  const g = {}; await f.guard.prepareSupervision(g, meta, f.trusted);
  assert.deepEqual(await f.hook(noTurn), {});
  await assert.rejects(f.guard.requireSupervision(g, meta, f.trusted));
});
