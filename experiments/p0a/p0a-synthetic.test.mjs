// SYNTHETIC TESTS ONLY. Every Stop event, native tool and REPL here is
// fabricated in-process. They check fixture wiring and can NEVER qualify P0a.
// Run explicitly: node --test experiments/p0a/p0a-synthetic.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname, resolve} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const fixture = await import(pathToFileURL(join(repo, 'experiments/p0a/p0a-fixture.mjs')).href);
const relative = 'skills/codex-pro-dispatch/scripts/resident-supervision.mjs';
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
const probeTask = 'synthetic-probe-task', probeTurn = 'synthetic-probe-turn';

async function world(t, {ownerParent = 'synthetic-listener-task'} = {}) {
  const root = await fs.realpath(await fs.mkdtemp(join(tmpdir(), 'p0a-synthetic-')));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const scripts = join(root, 'skills/codex-pro-dispatch/scripts');
  for (const path of [scripts, join(root, 'hooks'), join(root, 'config'), join(root, 'state')])
    await fs.mkdir(path, {recursive: true, mode: 0o700});
  await fs.copyFile(join(repo, relative), join(root, relative));
  await fs.copyFile(join(repo, 'hooks/hooks.json'), join(root, 'hooks/hooks.json'));
  const status = {ok: true, paths: {config_dir: join(root, 'config'), state_dir: join(root, 'state')},
    active_assignment: null, active_assignments: []};
  const owner = {version: 4, generation: 7, owner: 'owner-7', parent: ownerParent, session: null,
    slots: [{slot: 'slot-a', worker_conversation_id: 'worker-a', request: null, invocation: null, phase: 'idle'}]};
  await fs.writeFile(join(scripts, 'authority.json'), JSON.stringify({status, owner}), {mode: 0o600});
  // Read-only fake helper: any mutation attempt fails the test.
  await fs.writeFile(join(scripts, 'pro-dispatch'), `import json, pathlib, sys\nv=json.loads(pathlib.Path(__file__).with_name('authority.json').read_text())\na=sys.argv[1:]\nif a==['status','--current']: print(json.dumps(v['status']))\nelif a==['resident','inspect']: print(json.dumps({'ok':True,'owner':v['owner']}))\nelse: raise SystemExit('Mutation attempted')\n`);
  const supervision = join(root, relative);
  const supervisionDir = join(root, 'state/resident-supervision');
  const authorityBefore = sha(await fs.readFile(join(scripts, 'authority.json')));
  function hook(event) { // SYNTHETIC Stop: the test fabricates this event.
    return new Promise((resolve, reject) => {
      const child = execFile('node', [supervision, 'stop'], {timeout: 12000}, (error, out, err) => {
        if (error) return reject(Error(err || error.message));
        try { resolve(JSON.parse(out)); } catch (e) { reject(e); }
      });
      child.stdin.end(JSON.stringify({hook_event_name: 'Stop', session_id: probeTask, turn_id: probeTurn,
        stop_hook_active: false, ...event}));
    });
  }
  async function unchangedAuthority() {
    assert.equal(sha(await fs.readFile(join(scripts, 'authority.json'))), authorityBefore);
  }
  return {root, supervision, supervisionDir, hook, unchangedAuthority};
}

// SYNTHETIC host: runs the closure in-process with a fake REPL and fake tools.
async function run(code, {onText = () => {}, meta = () => ({threadId: probeTask, 'x-codex-turn-metadata': {turn_id: probeTurn}})} = {}) {
  const outputs = [], calls = [];
  const tools = {
    exec_command: async args => { calls.push(['exec_command', args.cmd]); return {exit_code: 0, output: 'SYNTHETIC'}; },
    async mcp__node_repl__js({code, title}) {
      calls.push(['mcp__node_repl__js', title]);
      let out = '';
      await new AsyncFunction('nodeRepl', 'console', code)({requestMeta: meta(title)}, {log: v => { out += v; }});
      return {content: [{type: 'text', text: out}]};
    }
  };
  let error = null;
  try { await new AsyncFunction('tools', 'text', code)(tools, v => { outputs.push(v); onText(v); }); }
  catch (e) { error = e; }
  return {outputs, calls, error};
}

test('SYNTHETIC stop mode: armed signal, fabricated Stop, same closure verifies with production require', async t => {
  const w = await world(t);
  const b = await fixture.bootstrap({mode: 'stop', supervision: w.supervision, observationMs: 5000});
  assert.ok(b.code.startsWith('// @exec: {"yield_time_ms":1000}\n'));
  let stop;
  const r = await run(b.code, {onText: v => { if (v.action === 'finalize') stop = w.hook({}); }});
  assert.equal(r.error, null);
  assert.deepEqual(r.outputs.map(o => [o.state ?? o.kind, o.action]),
    [['starting', 'wait'], ['host_build', 'wait'], ['qualifying', 'finalize'], ['p0a_settled', 'wait'], ['observed', 'stop']]);
  const decision = await stop;
  assert.equal(decision.decision, 'block');
  assert.match(decision.reason, /Supervision preflight only/);
  const [armed, , final] = r.outputs.slice(2);
  assert.ok(armed.armedAt <= armed.emittedAt);
  assert.equal(final.pass, true);
  assert.equal(final.productionRequire, 'verified');
  assert.equal(final.identityStable, true);
  assert.equal(final.observedSha256, armed.challengeSha256);
  assert.deepEqual((await fs.readdir(w.supervisionDir)).sort().map(n => n.replace(/^[a-f0-9]{64}/, 'KEY')),
    ['KEY.json', 'KEY.observed.json']);
  assert.deepEqual(r.calls.map(c => c[0]),
    ['exec_command', 'mcp__node_repl__js', 'mcp__node_repl__js', 'mcp__node_repl__js', 'exec_command']);
  await w.unchangedAuthority();
});

test('SYNTHETIC stop mode refuses the Listener owner task before writing a challenge', async t => {
  const w = await world(t, {ownerParent: probeTask});
  const b = await fixture.bootstrap({mode: 'stop', supervision: w.supervision, observationMs: 5000});
  const r = await run(b.code);
  assert.match(String(r.error), /p0a_task_is_listener_owner/);
  assert.equal(r.outputs.at(-1).action, 'stop');
  assert.equal(r.outputs.some(o => o.action === 'finalize'), false);
  await assert.rejects(fs.readdir(w.supervisionDir), {code: 'ENOENT'});
  await w.unchangedAuthority();
});

test('SYNTHETIC timeout: no Stop ends the closure failed; a late Stop blocks once, then releases', async t => {
  const w = await world(t);
  const b = await fixture.bootstrap({mode: 'stop', supervision: w.supervision, observationMs: 300});
  const r = await run(b.code);
  const final = r.outputs.at(-1);
  assert.equal(final.state, 'failed');
  assert.equal(final.reason, 'qualification_timeout');
  assert.equal(final.pass, false);
  assert.equal((await w.hook({})).decision, 'block');
  assert.deepEqual(await w.hook({stop_hook_active: true}), {});
  await w.unchangedAuthority();
});

test('SYNTHETIC turn drift after resume fails production require', async t => {
  const w = await world(t);
  const b = await fixture.bootstrap({mode: 'stop', supervision: w.supervision, observationMs: 5000});
  let stop;
  const r = await run(b.code, {
    onText: v => { if (v.action === 'finalize') stop = w.hook({}); },
    meta: title => ({threadId: probeTask, 'x-codex-turn-metadata': {turn_id: title === 'P0a verify' ? 'other-turn' : probeTurn}})
  });
  await stop;
  const final = r.outputs.at(-1);
  assert.equal(final.observed, true);
  assert.equal(final.identityStable, false);
  assert.match(final.productionRequire, /^failed/);
  assert.equal(final.pass, false);
});

test('SYNTHETIC dry mode writes nothing and never asks for a final response', async t => {
  const w = await world(t);
  const b = await fixture.bootstrap({mode: 'dry', dryMs: 200});
  const r = await run(b.code);
  assert.equal(r.error, null);
  assert.equal(r.outputs.some(o => o.action === 'finalize'), false);
  assert.equal(r.outputs.at(-1).pass, true);
  assert.equal(r.outputs.at(-1).action, 'stop');
  await assert.rejects(fs.readdir(w.supervisionDir), {code: 'ENOENT'});
});

test('SYNTHETIC pin and lost-state failures are explicit', async t => {
  const w = await world(t);
  const g = {}, meta = {threadId: probeTask, 'x-codex-turn-metadata': {turn_id: probeTurn}};
  await assert.rejects(fixture.arm(g, meta, 'stop', 300, w.supervision, '0'.repeat(64)), /p0a_pin_changed/);
  await assert.rejects(fs.readdir(w.supervisionDir), {code: 'ENOENT'});
  await assert.rejects(fixture.settle(g, meta, 'missing'), /p0a_state_lost/);
  await assert.rejects(fixture.verify(g, meta, 'missing'), /p0a_state_lost/);
});

test('SYNTHETIC installed hook path parser accepts only one resident Stop command', async t => {
  const home = await fs.mkdtemp(join(tmpdir(), 'p0a-home-'));
  t.after(() => fs.rm(home, {recursive: true, force: true}));
  const write = commands => fs.writeFile(join(home, 'hooks.json'), JSON.stringify({hooks: {Stop: [{hooks:
    commands.map(command => ({type: 'command', command}))}]}}));
  await write(['node /opt/x/scripts/resident-supervision.mjs stop']);
  assert.equal(await fixture.installedSupervision(home), '/opt/x/scripts/resident-supervision.mjs');
  await write(['node /a/resident-supervision.mjs stop', 'node /b/resident-supervision.mjs stop']);
  await assert.rejects(fixture.installedSupervision(home), /exactly one/);
  await write(['node relative/resident-supervision.mjs stop']);
  await assert.rejects(fixture.installedSupervision(home), /exactly one/);
});
