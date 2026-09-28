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
const rid = 'incident-armed-A';
const parent = 'listener-parent';
const turn = 'owner-turn';
const worker = 'worker-pro-a';
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
  const status = {ok: true, paths: {config_dir: join(root, 'config'), state_dir: join(root, 'state')},
    active_assignment: null, active_assignments: []};
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
  status.worker_pool = {file_sha256: owner.worker_pool_sha256,
    workers: owner.slots.map(s => ({slot: s.slot, conversation_id: s.worker_conversation_id}))};
  const authority = {status, owner, queue: {ok: true, requests: []}};
  const recordPath = join(scripts, 'fixture-authority.json');
  await save(recordPath, authority);
  // The production module calls the actual helper interface. This isolated
  // helper permits only its existing read operations and no mutations.
  await fs.writeFile(join(scripts, 'pro-dispatch'), `import json, pathlib, sys\nv=json.loads(pathlib.Path(__file__).with_name('fixture-authority.json').read_text())\na=sys.argv[1:]\nif a==['status','--current']: print(json.dumps(v['status']))\nelif a==['resident','inspect']: print(json.dumps({'ok':True,'owner':v['owner']}))\nelif a==['queue','status']: print(json.dumps(v['queue']))\nelse: raise SystemExit('Mutation attempted')\n`);
  await save(join(root, 'state/assignment.json'), {assignment_id: rid, status: 'armed', no_resend: true, submission_count: 0});
  await save(join(root, 'session/command-2.json'), {requestId: 'incident-unobserved-B', ordinal: 2});
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
          if (/\/resident-supervision\/(?:pending|terminal)-/.test(path)) continue;
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

test('terminal serve-cell finalization is released on one unchanged reentry, preserving requests', async t => {
  const f = await fixture(t);
  f.authority.status.active_assignments = [
    {assignment_id: rid, status: 'submitted', no_resend: true, submission_count: 1},
    {assignment_id: 'incident-unobserved-B', status: 'submitted', no_resend: true, submission_count: 1}
  ];
  await f.update();
  const nativeSendAttempts = 1; // The reproduced send has already returned.
  const decision = await f.hook();
  assert.equal(decision.decision, 'block');
  assert.deepEqual(await f.hook({...event, stop_hook_active: true}), {});
  assert.deepEqual(await f.hook({...event, stop_hook_active: true}), {});
  assert.match(decision.reason, /ORIGINAL/);
  assert.equal(nativeSendAttempts, 1);
  assert.equal(f.authority.owner.slots[0].phase, 'running');
  assert.equal(f.authority.status.active_assignments.length, 2);
});

test('changed canonical state never qualifies a terminal reentry', async t => {
  const f = await fixture(t);
  assert.equal((await f.hook({...event, stop_hook_active: true})).decision, 'block');
  f.authority.owner.slots[1].request = 'new-request';
  f.authority.owner.slots[1].phase = 'reserved';
  await f.update();
  assert.equal((await f.hook({...event, stop_hook_active: true})).decision, 'block');
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
  const open = new Function('ownerSupervision', 'cli', 'J', 'assertResidentOpenConfiguration', source.replace('export ', '') + '\nreturn openResident;')(
    f.guard, async args => {
      if (args.join() === 'status,--current') return f.authority.status;
      starts++; throw sentinel;
    }, JSON.stringify, () => {});
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
  const open = new Function('ownerSupervision', 'cli', 'J', 'assertResidentOpenConfiguration', source.replace('export ', '') + '\nreturn openResident;')(
    f.guard, async args => {
      if (args.join() === 'status,--current') return f.authority.status;
      assert.deepEqual(args.slice(0, 2), ['resident', 'start']); starts++; return {state: 'busy'};
    }, JSON.stringify, () => {});
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
    if (delay === 120000) {
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

test('helper failure retains exit diagnostics and keeps Stop blocked without state writes', async t => {
  const f = await fixture(t, false);
  const before = await f.snapshot();
  await fs.writeFile(join(f.root, 'skills/codex-pro-dispatch/scripts/pro-dispatch'),
    'import sys\nsys.stderr.write("fixture read failure")\nsys.exit(23)\n');
  await assert.rejects(f.guard.prepareSupervision({}, meta, f.trusted), error => {
    assert.equal(error.name, 'SupervisionReadError');
    assert.match(error.message, /"code":23/);
    assert.match(error.message, /fixture read failure/);
    assert.equal(error.cause.code, 23);
    return true;
  });
  const decision = await f.hook();
  assert.equal(decision.decision, 'block');
  assert.match(decision.reason, /resident inspect/);
  assert.match(decision.reason, /"code":23/);
  assert.deepEqual(await f.snapshot(), before);
});

test('helper deadline retains termination diagnostics without granting availability', async t => {
  const f = await fixture(t, false);
  await fs.writeFile(join(f.root, 'skills/codex-pro-dispatch/scripts/pro-dispatch'),
    'import time\ntime.sleep(10)\n');
  await assert.rejects(f.guard.prepareSupervision({}, meta, f.trusted), error => {
    assert.equal(error.name, 'SupervisionReadError');
    assert.match(error.message, /"killed":true/);
    assert.match(error.message, /"signal":"SIGTERM"/);
    return true;
  });
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


// Issue 20: finalization of an unbound takeover is not completion of old work.
async function unboundTakeover(t) {
  const f = await fixture(t, false);
  const owner = f.authority.owner;
  owner.generation = 24;
  owner.owner = 'replacement-owner';
  owner.session = null;
  Object.assign(owner.slots[0], {request: rid, invocation: null, phase: 'collect_only'});
  await f.update();
  return f;
}

// Feed different canonical snapshots to successive read-only helper calls.
// Only test instrumentation writes the counter, outside the canonical state.
async function changeAfterFirstSnapshot(f, change, threshold = 3) {
  const scripts = join(f.root, 'skills/codex-pro-dispatch/scripts');
  const after = structuredClone(f.authority);
  change(after);
  await save(join(scripts, 'fixture-after.json'), after);
  await fs.writeFile(join(scripts, 'pro-dispatch'), `import json, pathlib, sys
p=pathlib.Path(__file__).parent
args=sys.argv[1:]
if args not in [['status','--current'],['resident','inspect'],['queue','status']]: raise SystemExit('Mutation attempted')
c=p/'read-count'
n=int(c.read_text())+1 if c.exists() else 1
c.write_text(str(n))
v=json.loads((p/('fixture-after.json' if n>=${threshold} else 'fixture-authority.json')).read_text())
print(json.dumps(v['status'] if args==['status','--current'] else v['queue'] if args==['queue','status'] else {'ok':True,'owner':v['owner']}))
`);
}

test('unbound takeover can finalize repeatedly without changing collect-only evidence', async t => {
  const f = await unboundTakeover(t);
  const before = await f.snapshot();
  const authorityBytes = await fs.readFile(join(f.root, 'skills/codex-pro-dispatch/scripts/fixture-authority.json'));
  for (const stop_hook_active of [false, true, true])
    assert.deepEqual(await f.hook({...event, stop_hook_active}), {});
  assert.deepEqual(await f.snapshot(), before);
  assert.deepEqual(await fs.readFile(join(f.root, 'skills/codex-pro-dispatch/scripts/fixture-authority.json')), authorityBytes);
});

test('unbound takeover supports all-idle and two collect-only slots', async t => {
  const f = await unboundTakeover(t);
  Object.assign(f.authority.owner.slots[1], {request: 'retained-sibling', phase: 'collect_only'});
  await f.update();
  assert.deepEqual(await f.hook(), {});
  for (const slot of f.authority.owner.slots) Object.assign(slot, {request: null, phase: 'idle'});
  await f.update();
  assert.deepEqual(await f.hook(), {});
});

test('unbound takeover never bypasses a live active assignment', async t => {
  const f = await unboundTakeover(t);
  f.authority.status.active_assignment = {assignment_id: rid, status: 'armed'};
  await f.update();
  const before = await f.snapshot();
  assert.equal((await f.hook()).decision, 'block');
  assert.deepEqual(await f.snapshot(), before);
});

test('unbound takeover never bypasses legacy inflight or a pool invocation', async t => {
  const f = await unboundTakeover(t);
  f.authority.owner.inflight = {request: rid, invocation: 'live-legacy'};
  await f.update();
  assert.equal((await f.hook()).decision, 'block');
  delete f.authority.owner.inflight;
  Object.assign(f.authority.owner.slots[0], {phase: 'running', invocation: {request: rid, invocation: 'live-pool'}});
  await f.update();
  assert.equal((await f.hook()).decision, 'block');
});

test('unbound takeover exception requires explicit null session and active-assignment evidence', async t => {
  const f = await unboundTakeover(t);
  for (const session of [undefined, false, 0, '']) {
    f.authority.owner.session = session;
    await f.update();
    assert.equal((await f.hook()).decision, 'block', String(session));
  }
  f.authority.owner.session = null;
  delete f.authority.status.active_assignment;
  await f.update();
  assert.equal((await f.hook()).decision, 'block');
});

test('unbound takeover exception does not release non-collect-only phases', async t => {
  const f = await unboundTakeover(t);
  for (const phase of ['reserved', 'running', 'cancel_pending']) {
    f.authority.owner.slots[0].phase = phase;
    await f.update();
    assert.equal((await f.hook()).decision, 'block', phase);
  }
});

test('unbound takeover exception is not applied to a bound collect-only session', async t => {
  const f = await fixture(t, false);
  Object.assign(f.authority.owner.slots[0], {request: rid, phase: 'collect_only'});
  await f.update();
  assert.equal((await f.hook()).decision, 'block');
  await f.joined();
  assert.equal((await f.hook()).decision, 'block');
});

test('unbound takeover still blocks a pending qualification challenge first', async t => {
  const f = await unboundTakeover(t);
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted);
  await assert.rejects(f.guard.requireSupervision(g, meta, f.trusted));
  const before = await f.snapshot();
  const waiting = f.guard.waitSupervision(g, meta, f.trusted);
  const decision = await f.hook({...event, stop_hook_active: true});
  assert.equal(decision.decision, 'block');
  assert.match(decision.reason, /qualification cell/);
  await waiting;
  await f.guard.requireSupervision(g, meta, f.trusted);
  const observed = await f.snapshot();
  assert.deepEqual(Object.keys(observed).filter(key => !(key in before)),
    ['/state/resident-supervision/' + sha(parent + '\0' + turn) + '.observed.json']);
  for (const [path, value] of Object.entries(before)) assert.equal(observed[path], value);
  assert.deepEqual(await f.hook({...event, stop_hook_active: true}), {});
  assert.deepEqual(await f.snapshot(), observed);
});

test('unbound takeover cannot bypass a malformed or mismatched qualification record', async t => {
  const f = await unboundTakeover(t);
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted);
  const path = join(f.root, 'state/resident-supervision', sha(parent + '\0' + turn));
  await fs.writeFile(path + '.json', 'bad JSON');
  assert.equal((await f.hook()).decision, 'block');
  await save(path + '.json', g.parkedSupervision);
  await save(path + '.observed.json', {...g.parkedSupervision, nonce: '0'.repeat(32)});
  assert.equal((await f.hook()).decision, 'block');
});

test('unbound takeover permits later qualification without reusing the earlier turn proof', async t => {
  const f = await unboundTakeover(t);
  const g = {};
  await f.guard.prepareSupervision(g, meta, f.trusted);
  await f.hook();
  assert.deepEqual(await f.hook(), {});
  const laterTurn = 'later-qualified-activation';
  const later = {...meta, 'x-codex-turn-metadata': {turn_id: laterTurn}};
  await assert.rejects(f.guard.requireSupervision(g, later, f.trusted));
  const next = {};
  await f.guard.prepareSupervision(next, later, f.trusted);
  assert.equal((await f.hook({...event, turn_id: laterTurn})).decision, 'block');
  await f.guard.waitSupervision(next, later, f.trusted);
  await f.guard.requireSupervision(next, later, f.trusted);
});

test('unbound takeover rechecks owner generation, session, and invocation before allowing finalization', async t => {
  const f = await unboundTakeover(t);
  await changeAfterFirstSnapshot(f, value => {
    value.owner.generation++;
    value.owner.session = {directory: join(f.root, 'session'), session_id: 'a'.repeat(32), descriptor_sha256: 'c'.repeat(64)};
    Object.assign(value.owner.slots[0], {phase: 'running', invocation: {request: rid, invocation: 'new-serving'}});
  });
  assert.equal((await f.hook()).decision, 'block');
});

test('unbound takeover rechecks active assignment and canonical paths', async t => {
  for (const change of [
    value => { value.status.active_assignment = {assignment_id: rid}; },
    value => { value.status.paths.state_dir += '-other'; }
  ]) {
    const f = await unboundTakeover(t);
    await changeAfterFirstSnapshot(f, change);
    assert.equal((await f.hook()).decision, 'block');
  }
});

async function retainedReceipt(t) {
  const f = await unboundTakeover(t);
  const owner = f.authority.owner;
  owner.generation = 27;
  const oldParent = 'original-listener';
  const slot = owner.slots[0];
  const audit = (generation, previous, replacement) => ({
    previous_generation: generation, previous_parent: previous,
    previous_owner: 'owner-' + generation, replacement_parent: replacement,
    collect_only: [rid], cancel_prepared: [],
    request_bindings: {[rid]: {prior_parent: oldParent, slot: slot.slot, worker}},
    slot_transitions: {[slot.slot]: {from: generation === 22 ? 'running' : 'collect_only',
      to: 'collect_only', request: rid}}
  });
  owner.qualification = {takeover_history: [
    audit(22, oldParent, 'parent-23'), audit(23, 'parent-23', 'parent-24'),
    audit(24, 'parent-24', 'parent-25'), audit(25, 'parent-25', 'parent-26')
  ], takeover: audit(26, 'parent-26', parent)};
  const receipt = {assignment_id: rid, status: 'armed', submission_count: 0,
    no_resend: true, owner_generation: 22, parent_task_id: oldParent,
    worker_slot: slot.slot, worker_conversation_id: worker};
  f.authority.status.active_assignment = receipt;
  f.authority.status.active_assignments = [receipt];
  f.authority.queue.requests = [{request_id: rid, state: 'claimed', dispatch_status: 'armed',
    owner_generation: 22, parent_task_id: oldParent, worker_slot: slot.slot,
    worker_conversation_id: worker, send_authorized: false, send_may_have_occurred: true}];
  await f.update();
  return f;
}

async function unchangedDecision(f, allowed) {
  await f.update();
  const before = await f.snapshot();
  const decision = await f.hook();
  if (allowed) assert.deepEqual(decision, {});
  else assert.equal(decision.decision, 'block');
  assert.deepEqual(await f.snapshot(), before);
}

test('retained receipt permits finalization without changing generation-22 evidence', async t => {
  const f = await retainedReceipt(t);
  for (const stop_hook_active of [false, true, true]) {
    const before = await f.snapshot();
    assert.deepEqual(await f.hook({...event, stop_hook_active}), {});
    assert.deepEqual(await f.snapshot(), before);
  }
});

test('retained receipt blocks multiple active receipts hidden by the scalar alias', async t => {
  const f = await retainedReceipt(t);
  f.authority.status.active_assignments.push({...f.authority.status.active_assignment,
    assignment_id: 'second-request', worker_slot: 'worker-2',
    worker_conversation_id: 'other-worker'});
  f.authority.status.active_assignment = null;
  await unchangedDecision(f, false);
});

test('retained receipt requires exact receipt, queue, and takeover bindings', async t => {
  for (const change of [
    v => { v.status.active_assignment.worker_slot = 'worker-2'; },
    v => { v.status.active_assignment.parent_task_id = 'wrong-parent';
      v.queue.requests[0].parent_task_id = 'wrong-parent'; },
    v => { v.status.active_assignment.owner_generation = 23;
      v.queue.requests[0].owner_generation = 23; },
    v => { v.queue.requests[0].worker_conversation_id = 'other-worker'; },
    v => { delete v.owner.qualification.takeover.request_bindings[rid]; },
    v => { v.owner.qualification.takeover.slot_transitions['worker-1'].to = 'running'; }
  ]) {
    const f = await retainedReceipt(t);
    change(f.authority);
    await unchangedDecision(f, false);
  }
});

test('retained receipt blocks bound or live current work', async t => {
  for (const change of [
    v => { v.owner.session = {directory: '/bound'}; },
    v => { v.owner.slots[0].phase = 'running'; },
    v => { v.owner.slots[0].invocation = {request: rid, invocation: 'live'}; },
    v => { v.owner.inflight = {request: rid, invocation: 'legacy-live'}; }
  ]) {
    const f = await retainedReceipt(t);
    change(f.authority);
    await unchangedDecision(f, false);
  }
});

test('retained receipt rereads owner, complete status, and queue', async t => {
  for (const change of [
    v => { v.owner.generation++; },
    v => { v.status.active_assignment.status = 'pending'; },
    v => { v.queue.requests[0].dispatch_status = 'pending'; }
  ]) {
    const f = await retainedReceipt(t);
    await changeAfterFirstSnapshot(f, change, 4);
    assert.equal((await f.hook()).decision, 'block');
  }
});

// Issue 29: one arbitration between a genuine Stop and the qualification
// deadline. Every Stop here is a simulated hook event, never native evidence.
const operation = 'c'.repeat(64);
function captureDeadline(t) {
  const realTimeout = globalThis.setTimeout, clock = {expire: null};
  const mock = t.mock.method(globalThis, 'setTimeout', (callback, delay, ...args) => {
    if (delay !== 120000) return realTimeout(callback, delay, ...args);
    clock.expire = callback;
    return realTimeout(() => {}, 0);
  });
  clock.restore = () => mock.mock.restore();
  return clock;
}
async function startupTurn(t, f, turnId) {
  const owner = f.authority.owner;
  Object.assign(owner, {version: 4, session: null, generation: 23, owner: 'owner-23',
    qualification: {startup: {operation_id: operation, native_task: parent, native_turn: turnId}}});
  for (const slot of owner.slots) Object.assign(slot, {request: null, invocation: null, phase: 'idle'});
  await f.update();
  const g = {}, turnMeta = {threadId: parent, 'x-codex-turn-metadata': {turn_id: turnId}};
  const binding = {operation, generation: 23, owner: 'owner-23'};
  const key = sha(parent + '\0' + turnId), directory = join(f.root, 'state/resident-supervision');
  return {g, meta: turnMeta, binding, challenge: join(directory, key + '.json'), observed: join(directory, key + '.observed.json'),
    event: {...event, turn_id: turnId}};
}
async function timedOut(t, f, turnId = turn) {
  const clock = captureDeadline(t), s = await startupTurn(t, f, turnId);
  await f.guard.prepareSupervision(s.g, s.meta, f.trusted);
  await f.guard.armSupervision(s.g, s.meta, f.trusted, s.binding);
  clock.expire();
  await assert.rejects(f.guard.settleSupervision(s.g, s.meta, f.trusted), e => e.code === 'qualification_timeout');
  clock.restore();
  return s;
}

test('deadline before Stop: terminal receipt, late Stop releases only the matching idle acquisition', async t => {
  const f = await fixture(t, false);
  const s = await timedOut(t, f);
  const terminal = JSON.parse(await fs.readFile(s.observed, 'utf8'));
  assert.deepEqual({operation: terminal.operation, generation: terminal.generation, owner: terminal.owner,
    parent: terminal.parent, turn: terminal.turn, waiter: terminal.waiter, qualified: terminal.qualified,
    send_authorized: terminal.send_authorized, reason: terminal.reason},
  {operation, generation: 23, owner: 'owner-23', parent, turn, waiter: 'closed', qualified: false,
    send_authorized: false, reason: 'qualification_timeout'});
  assert.equal(terminal.nonce, JSON.parse(await fs.readFile(s.challenge, 'utf8')).nonce);
  const before = await f.snapshot();
  assert.deepEqual(await f.hook(), {});
  assert.deepEqual(await f.hook({...event, stop_hook_active: true}), {});
  assert.deepEqual(await f.snapshot(), before);
  await assert.rejects(f.guard.requireSupervision(s.g, meta, f.trusted));
  await assert.rejects(f.guard.requireRetainedSupervision(s.g, meta, f.trusted));
  // One genuine probe per task turn: the closed waiter is never re-armed.
  delete s.g.parkedSupervisionWait;
  await assert.rejects(f.guard.armSupervision(s.g, meta, f.trusted, s.binding), /already ended/);
});

test('Stop before deadline: the observation keeps the slot and the deadline cannot erase it', async t => {
  const f = await fixture(t, false), clock = captureDeadline(t), s = await startupTurn(t, f, turn);
  await f.guard.prepareSupervision(s.g, s.meta, f.trusted);
  const armed = await f.guard.armSupervision(s.g, s.meta, f.trusted, s.binding);
  assert.equal(armed.deadlineAt - armed.armedAt, 120000);
  assert.equal(armed.challengeSha256, sha(await fs.readFile(s.challenge)));
  assert.match((await f.hook()).reason, /^Supervision preflight only/);
  clock.expire();
  await f.guard.settleSupervision(s.g, s.meta, f.trusted);
  clock.restore();
  assert.equal(await fs.readFile(s.observed, 'utf8'), await fs.readFile(s.challenge, 'utf8'));
  assert.equal((await f.guard.requireSupervision(s.g, meta, f.trusted)).kind, 'resident_supervision_verified');
});

test('concurrent Stop and deadline arbitrate exactly once on the observed slot', async t => {
  const f = await fixture(t, false), outcomes = [];
  for (let i = 0; i < 6; i++) {
    const clock = captureDeadline(t), s = await startupTurn(t, f, 'race-turn-' + i);
    await f.guard.prepareSupervision(s.g, s.meta, f.trusted);
    await f.guard.armSupervision(s.g, s.meta, f.trusted, s.binding);
    const [decision] = await Promise.all([f.hook(s.event),
      new Promise(resolve => setTimeout(() => { clock.expire(); resolve(); }, i * 40))]);
    const settled = await f.guard.settleSupervision(s.g, s.meta, f.trusted).then(() => true, e => {
      assert.equal(e.code, 'qualification_timeout'); return false; });
    clock.restore();
    const observed = await fs.readFile(s.observed, 'utf8'), challenge = await fs.readFile(s.challenge, 'utf8');
    if (settled) {
      assert.equal(observed, challenge);
      assert.match(decision.reason, /^Supervision preflight only/);
      await f.guard.requireSupervision(s.g, s.meta, f.trusted);
    } else {
      assert.equal(JSON.parse(observed).kind, 'resident_supervision_terminal');
      assert.deepEqual(decision, {});
      await assert.rejects(f.guard.requireSupervision(s.g, s.meta, f.trusted));
    }
    outcomes.push(settled);
  }
  assert.equal(outcomes.length, 6);
  t.diagnostic('Stop won (true) or deadline won (false): ' + JSON.stringify(outcomes));
});

test('terminal receipt never releases moved authority or live work', async t => {
  for (const change of [
    v => { v.owner.generation = 24; v.owner.owner = 'owner-24'; },
    v => { v.owner.qualification.startup.operation_id = 'd'.repeat(64); },
    v => { v.owner.qualification.startup.native_turn = 'later-turn'; },
    v => { v.owner.slots[0].phase = 'collect_only'; v.owner.slots[0].request = rid; },
    v => { v.status.active_assignment = {assignment_id: rid}; v.status.active_assignments = [{assignment_id: rid}]; },
    v => { v.queue.requests = [{request_id: rid, state: 'claimed'}]; }
  ]) {
    const f = await fixture(t, false);
    await timedOut(t, f);
    change(f.authority); await f.update();
    const before = await f.snapshot();
    assert.equal((await f.hook()).decision, 'block');
    assert.deepEqual(await f.snapshot(), before);
  }
});

test('terminal receipt rereads canonical state before release', async t => {
  const f = await fixture(t, false);
  await timedOut(t, f);
  await changeAfterFirstSnapshot(f, v => { v.owner.generation = 24; }, 4);
  assert.equal((await f.hook()).decision, 'block');
});

test('terminal receipt in a task that no longer owns authority does not trap it', async t => {
  const f = await fixture(t, false);
  await timedOut(t, f);
  f.authority.owner.parent = 'replacement-parent'; await f.update();
  assert.deepEqual(await f.hook(), {});
});

// App restart (2026-09-28): the owner was acquired and served in an earlier
// turn of this task; these Stop events come from a later turn of the same task.
function servedEarlier(f, servingTurn = 'serving-turn') {
  const o = f.authority.owner;
  o.qualification = {startup: {acquired: {generation: o.generation, owner: o.owner, parent: o.parent},
    native_task: o.parent, native_turn: servingTurn, operation_id: 'c'.repeat(64)}};
}
const laterTurnMarkers = async f => (await fs.readdir(join(f.root, 'state/resident-supervision')).catch(() => []))
  .filter(name => name.startsWith('other-turn-'));

test('a later owner-task turn is asked once to recover, then released; the serving turn still blocks', async t => {
  const f = await fixture(t, false);
  servedEarlier(f); await f.update();
  const before = await f.snapshot();
  const prompt = await f.hook({...event, stop_hook_active: true});
  assert.equal(prompt.decision, 'block');
  assert.match(prompt.reason, /served by an earlier turn of this task/);
  assert.match(prompt.reason, /\/references\/listener-start\.md /);
  assert.doesNotMatch(prompt.reason, /ORIGINAL/);
  assert.equal((await f.hook(event)).decision, 'block', 'only a hook continuation is released');
  for (let i = 0; i < 2; i++) assert.deepEqual(await f.hook({...event, stop_hook_active: true}), {});
  const after = await f.snapshot(), added = Object.keys(after).filter(path => !(path in before));
  assert.deepEqual(added.map(path => path.replace(/-[a-f0-9]{64}\.json$/, '')),
    ['/state/resident-supervision/other-turn-22-owner-22']);
  for (const path of Object.keys(before)) assert.equal(after[path], before[path]);
  // The turn that serves this owner keeps waiting on its original cell.
  for (let i = 0; i < 2; i++)
    assert.match((await f.hook({...event, turn_id: 'serving-turn', stop_hook_active: true})).reason, /ORIGINAL/);
});

test('after recovery in the same turn, the new owner keeps the original-cell rule', async t => {
  const f = await fixture(t, false);
  servedEarlier(f); await f.update();
  assert.equal((await f.hook({...event, stop_hook_active: true})).decision, 'block');
  Object.assign(f.authority.owner, {generation: 23, owner: 'owner-23'});
  servedEarlier(f, turn); await f.update();
  for (let i = 0; i < 2; i++)
    assert.match((await f.hook({...event, stop_hook_active: true})).reason, /ORIGINAL/);
  assert.equal((await laterTurnMarkers(f)).length, 1);
});

test('without a serving turn for the current owner, every turn keeps the original-cell rule', async t => {
  for (const change of [o => { o.qualification = {}; },
    o => { o.qualification.startup.acquired.generation = 21; },
    o => { o.qualification.startup.native_task = 'other-task'; }]) {
    const f = await fixture(t, false);
    servedEarlier(f); change(f.authority.owner); await f.update();
    for (let i = 0; i < 2; i++)
      assert.match((await f.hook({...event, stop_hook_active: true})).reason, /ORIGINAL/);
    assert.deepEqual(await laterTurnMarkers(f), []);
  }
});

test('a later turn never asks to recover a joined listener or a busy owner', async t => {
  const joined = await fixture(t, false);
  servedEarlier(joined); await joined.update(); await joined.joined();
  assert.deepEqual(await joined.hook(), {});
  // Unresolved work keeps the existing one-reentry release and its requests.
  const busy = await fixture(t);
  servedEarlier(busy);
  busy.authority.status.active_assignments = [{assignment_id: rid, status: 'submitted', no_resend: true, submission_count: 1}];
  await busy.update();
  assert.match((await busy.hook()).reason, /ORIGINAL/);
  assert.deepEqual(await busy.hook({...event, stop_hook_active: true}), {});
  assert.equal(busy.authority.owner.slots[0].phase, 'running');
  for (const f of [joined, busy]) assert.deepEqual(await laterTurnMarkers(f), []);
});

test('a later turn is not released when the owner changes during the check', async t => {
  const f = await fixture(t, false);
  servedEarlier(f); await f.update();
  await changeAfterFirstSnapshot(f, v => { v.owner.generation = 23; }, 3);
  const decision = await f.hook({...event, stop_hook_active: true});
  assert.equal(decision.decision, 'block');
  assert.match(decision.reason, /could not be verified/);
  assert.deepEqual(await laterTurnMarkers(f), []);
});


test('legacy serve-existing claims retain the original-cell rule in later turns', async t => {
  for (const name of ['resident-serve-existing.json', 'resident-unused-replacement.json']) {
    const f = await fixture(t, false);
    servedEarlier(f); await f.update();
    await save(join(f.root, 'session', name), {});
    for (let i = 0; i < 2; i++)
      assert.match((await f.hook({...event, stop_hook_active: true})).reason, /ORIGINAL/);
    assert.deepEqual(await laterTurnMarkers(f), []);
  }
});
