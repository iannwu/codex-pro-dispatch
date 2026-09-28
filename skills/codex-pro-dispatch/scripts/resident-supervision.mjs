// Stop is a finalization guard, never a resident owner or an admission renewer.
import * as fs from 'node:fs/promises';
import {constants, watch} from 'node:fs';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash, randomBytes} from 'node:crypto';
import {execFile} from 'node:child_process';

const here = fileURLToPath(import.meta.url);
const dir = await fs.realpath(dirname(here));
const uid = (await fs.stat(dir)).uid;
const helper = join(dir, 'pro-dispatch');
const hookFile = join(dir, '../../../hooks/hooks.json');
const id = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const hash = value => createHash('sha256').update(value).digest('hex');
const loadedGuardSha256 = hash(await fs.readFile(here));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const fail = () => { throw Error('Resident supervision proof missing or changed; no availability'); };

async function cli(args) {
  return await new Promise((resolve, reject) => {
    execFile('python3', [helper, ...args], {timeout: 3000, maxBuffer: 1048576}, (error, out, stderr) => {
      try {
        if (error) {
          const diagnostic = {operation: args.join(' '), code: error.code ?? null,
            signal: error.signal ?? null, killed: error.killed === true,
            stderr: (stderr ?? '').slice(0, 2048)};
          const failure = new Error('Canonical supervision read failed: ' + JSON.stringify(diagnostic), {cause: error});
          failure.name = 'SupervisionReadError';
          throw failure;
        }
        const value = JSON.parse(out);
        if (value?.ok !== true) throw Error('Canonical supervision read rejected');
        resolve(value);
      } catch (error) { reject(error); }
    });
  });
}
async function authority() {
  const status = await cli(['status', '--current']);
  const {owner} = await cli(['resident', 'inspect']);
  return {status, owner};
}
async function privateDirectory(path) {
  const st = await fs.lstat(path);
  if (await fs.realpath(path) !== path || !st.isDirectory() || st.uid !== uid || (st.mode & 0o077)) fail();
}
async function bytes(path, limit = 16384) {
  if (await fs.realpath(path) !== path) fail();
  const handle = await fs.open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const st = await handle.stat();
    if (!st.isFile() || st.uid !== uid || (st.mode & 0o077) || st.size > limit) fail();
    const raw = await handle.readFile();
    if (raw.length > limit) fail();
    return raw;
  } finally { await handle.close(); }
}
async function json(path, limit) { return JSON.parse((await bytes(path, limit)).toString('utf8')); }
async function absent(path) {
  try { await fs.lstat(path); }
  catch (error) { if (error.code === 'ENOENT') return; throw error; }
  throw Error('Resident work has not joined');
}
async function exclusive(path, value) {
  const raw = JSON.stringify(value);
  // Publish only a complete, synced record. fs.watch must never observe an
  // empty or partially written challenge/attestation at its final path.
  const temporary = path + '.' + randomBytes(16).toString('hex') + '.tmp';
  const handle = await fs.open(temporary, 'wx', 0o600);
  try {
    try { await handle.writeFile(raw); await handle.sync(); }
    finally { await handle.close(); }
    try { await fs.link(temporary, path); }
    catch (error) {
      if (error.code !== 'EEXIST' || (await bytes(path)).toString('utf8') !== raw) throw error;
    }
  } finally { await fs.unlink(temporary); }
  const parent = await fs.open(dirname(path), 'r');
  try { await parent.sync(); } finally { await parent.close(); }
}

async function fingerprints() {
  if (hash(await fs.readFile(here)) !== loadedGuardSha256) fail();
  return {guardSha256: loadedGuardSha256, hooksSha256: hash(await fs.readFile(hookFile))};
}
function location(paths, parent, turn) {
  const directory = join(paths.state_dir, 'resident-supervision');
  const key = hash(parent + '\0' + turn);
  return {directory, challenge: join(directory, key + '.json'), observed: join(directory, key + '.observed.json')};
}
// One genuine Stop probe per task turn; a later start needs a new turn.
export async function probeAttempted(stateDir, parent, turn) {
  try { await fs.lstat(location({state_dir: stateDir}, parent, turn).challenge); return true; }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}
function terminalFiles(paths, owner, turn) {
  const directory = join(paths.state_dir, 'resident-supervision');
  const stem = owner.generation + '-' + owner.owner;
  return {directory, pending: join(directory, 'pending-' + stem + '-' + hash(turn) + '.json'),
    terminal: join(directory, 'terminal-' + stem + '.json')};
}
function terminalRecord(owner) {
  return {version: 1, generation: owner.generation, owner: owner.owner,
    parent: owner.parent, session: owner.session ?? null,
    state: 'terminally_detached', send_authorized: false};
}
async function terminalStop(owner, status, event) {
  if (!Number.isSafeInteger(owner.generation) || owner.generation < 1 ||
      !id(owner.owner) || !id(owner.parent)) fail();
  const files = terminalFiles(status.paths, owner, event.turn_id);
  await fs.mkdir(files.directory, {mode: 0o700}).catch(error => { if (error.code !== 'EEXIST') throw error; });
  await privateDirectory(files.directory);
  const pending = {version: 1, generation: owner.generation, owner: owner.owner,
    parent: owner.parent, turn: event.turn_id, ownerSha256: hash(JSON.stringify(owner)),
    statusSha256: hash(JSON.stringify(status))};
  try {
    const observed = await json(files.pending);
    if (!same(observed, pending)) fail();
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    await exclusive(files.pending, pending);
    return false;
  }
  if (event.stop_hook_active !== true) return false;
  await exclusive(files.terminal, terminalRecord(owner));
  const current = await authority();
  if (!same(current.owner, owner) || !same(current.status, status)) fail();
  return true;
}
function nativeIdentity(meta, trusted) {
  const parent = meta?.threadId, turn = meta?.['x-codex-turn-metadata']?.turn_id;
  if (!id(parent) || !id(turn) || parent !== trusted.parent) fail();
  return {parent, turn};
}
async function context(meta, trusted) {
  const identity = nativeIdentity(meta, trusted);
  const {status, owner} = await authority();
  const paths = status.paths;
  if (paths?.config_dir !== trusted.configDir || paths?.state_dir !== trusted.stateDir) fail();
  await privateDirectory(paths.config_dir);
  await privateDirectory(paths.state_dir);
  return {identity, paths, owner, files: location(paths, identity.parent, identity.turn)};
}

// Called only through the pinned activation recipe with actual native metadata.
// Evidence is kept under the existing canonical state directory, not a new owner.
export async function prepareSupervision(g, meta, trusted) {
  const {identity, paths, owner, files} = await context(meta, trusted);
  if (owner?.inflight != null || owner?.slots?.some(slot => slot.invocation !== null)) fail();
  if (g.parkedSupervision?.parent === identity.parent && g.parkedSupervision?.turn === identity.turn) {
    await checkChallenge(g.parkedSupervision, paths, files);
    return {kind: 'resident_supervision_probe', admissionObserved: false};
  }
  await fs.mkdir(files.directory, {mode: 0o700}).catch(error => { if (error.code !== 'EEXIST') throw error; });
  await privateDirectory(files.directory);
  const record = {version: 1, ...identity, configDir: paths.config_dir, stateDir: paths.state_dir,
    nonce: randomBytes(16).toString('hex'), ...await fingerprints()};
  await exclusive(files.challenge, record);
  g.parkedSupervision = Object.freeze(record);
  return {kind: 'resident_supervision_probe', admissionObserved: false};
}
async function checkChallenge(record, paths, files) {
  await privateDirectory(files.directory);
  if (!record || record.version !== 1 || !id(record.parent) || !id(record.turn) ||
      !/^[a-f0-9]{32}$/.test(record.nonce) || record.configDir !== paths.config_dir ||
      record.stateDir !== paths.state_dir ||
      Object.keys(record).sort().join(',') !== 'configDir,guardSha256,hooksSha256,nonce,parent,stateDir,turn,version' ||
      !same({guardSha256: record.guardSha256, hooksSha256: record.hooksSha256}, await fingerprints()) ||
      !same(await json(files.challenge), record)) fail();
}
export async function requireSupervision(g, meta, trusted) {
  const {identity, paths, files} = await context(meta, trusted);
  const record = g.parkedSupervision;
  if (record?.parent !== identity.parent || record?.turn !== identity.turn) fail();
  await checkChallenge(record, paths, files);
  if (!same(await json(files.observed), record)) fail();
  return {kind: 'resident_supervision_verified', admissionObserved: false, sendAuthorized: false};
}
// Feature qualification for an explicitly requested retained continuation only.
// This does not attest a new turn's identity or extend an admission lease.
// The activation caller must still run every original continuation check.
export async function requireRetainedSupervision(g, meta, trusted) {
  const {identity, paths} = await context(meta, trusted);
  const record = g.parkedSupervision;
  if (record?.parent !== identity.parent) fail();
  const files = location(paths, record.parent, record.turn);
  await checkChallenge(record, paths, files);
  if (!same(await json(files.observed), record)) fail();
}
// Exactly one record ever occupies the observed slot: the hook's copy of the
// challenge (a genuine Stop) or the waiter's terminal receipt (deadline first).
// The link in exclusive() is the single arbitration, so a late Stop can never
// turn a timeout into qualification, and a timeout never erases a real Stop.
const OBSERVATION_MS = 45000;
const TERMINAL_KEYS = 'armedAt,deadlineAt,generation,kind,nonce,operation,owner,parent,qualified,reason,send_authorized,turn,version,waiter';
function qualificationTerminal(challenge, armedAt, deadlineAt, binding) {
  return {version: 1, kind: 'resident_supervision_terminal', parent: challenge.parent, turn: challenge.turn,
    nonce: challenge.nonce, reason: 'qualification_timeout', armedAt, deadlineAt,
    operation: binding?.operation ?? null, generation: binding?.generation ?? null, owner: binding?.owner ?? null,
    waiter: 'closed', qualified: false, send_authorized: false};
}
function terminalFor(value, challenge) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).sort().join(',') === TERMINAL_KEYS && value.version === 1 &&
    value.kind === 'resident_supervision_terminal' && value.reason === 'qualification_timeout' &&
    value.parent === challenge.parent && value.turn === challenge.turn && value.nonce === challenge.nonce &&
    value.waiter === 'closed' && value.qualified === false && value.send_authorized === false &&
    Number.isSafeInteger(value.armedAt) && Number.isSafeInteger(value.deadlineAt) && value.deadlineAt > value.armedAt &&
    (value.operation === null || /^[a-f0-9]{64}$/.test(value.operation)) &&
    (value.generation === null || (Number.isSafeInteger(value.generation) && value.generation > 0)) &&
    (value.owner === null || id(value.owner));
}
function qualificationTimeout(path) {
  return Object.assign(Error('Stop hook did not qualify; no availability'), {code: 'qualification_timeout', terminal: path});
}
// Register the watcher and deadline before the caller asks for a final
// response. The promise stays in this REPL; settle awaits the same one.
export async function armSupervision(g, meta, trusted, binding = null) {
  const {identity, paths, files} = await context(meta, trusted);
  const record = g.parkedSupervision;
  if (record?.parent !== identity.parent || record?.turn !== identity.turn) fail();
  await checkChallenge(record, paths, files);
  const current = g.parkedSupervisionWait;
  if (current?.parent === identity.parent && current?.turn === identity.turn) return current.armed;
  if (binding !== null && (typeof binding !== 'object' || Array.isArray(binding) ||
      Object.keys(binding).sort().join(',') !== 'generation,operation,owner' ||
      !/^[a-f0-9]{64}$/.test(binding.operation) || !Number.isSafeInteger(binding.generation) ||
      binding.generation < 1 || !id(binding.owner))) fail();
  // One genuine probe per task turn: a closed waiter is never re-armed.
  try { if (terminalFor(await json(files.observed), record)) throw Error('Qualification already ended for this turn; start again in a new turn'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const armedAt = Date.now(), deadlineAt = armedAt + OBSERVATION_MS;
  const terminal = qualificationTerminal(record, armedAt, deadlineAt, binding);
  const promise = new Promise((resolve, reject) => {
    let done = false, busy = false, again = false;
    const finish = error => {
      if (done) return;
      done = true; clearTimeout(timer); watcher.close();
      error ? reject(error) : resolve();
    };
    const watcher = watch(files.directory, () => { again = true; void check(); });
    const timer = setTimeout(() => void expire(), OBSERVATION_MS);
    watcher.on('error', finish);
    async function decide() {
      const observed = await json(files.observed);
      if (same(observed, record)) return finish();
      if (terminalFor(observed, record)) return finish(qualificationTimeout(files.observed));
      fail();
    }
    async function check() {
      if (done || busy) return;
      busy = true; again = false;
      try { await decide(); } catch (error) { if (error.code !== 'ENOENT') finish(error); }
      finally { busy = false; if (again && !done) void check(); }
    }
    async function expire() {
      try { await exclusive(files.observed, terminal); }
      catch (error) { if (error.code !== 'EEXIST') return finish(error); }
      try { await decide(); } catch (error) { finish(error); }
    }
    void check();
  });
  promise.catch(() => {});
  const armed = {kind: 'resident_supervision_armed', parent: identity.parent, turn: identity.turn,
    nonce: record.nonce, challengeSha256: hash(JSON.stringify(record)), armedAt, deadlineAt, admissionObserved: false};
  g.parkedSupervisionWait = Object.freeze({parent: identity.parent, turn: identity.turn, armed, promise});
  return armed;
}
export async function settleSupervision(g, meta, trusted) {
  const identity = nativeIdentity(meta, trusted), current = g.parkedSupervisionWait;
  if (current?.parent !== identity.parent || current?.turn !== identity.turn) fail();
  await current.promise;
  return {kind: 'resident_supervision_stop_observed', admissionObserved: false};
}
// Event-driven, bounded preflight with no socket, readiness marker, or native send.
export async function waitSupervision(g, meta, trusted) {
  await armSupervision(g, meta, trusted);
  return await settleSupervision(g, meta, trusted);
}

// Inspect the complete current-status projection, not just its scalar alias:
// active_assignment is also null when multiple assignments remain unresolved.
function unboundOwner(owner, status) {
  const slots = owner.slots, pool = status.worker_pool, active = status.active_assignments;
  return [3, 4].includes(owner.version) && owner.session === null && owner.inflight == null &&
    Number.isSafeInteger(owner.generation) && owner.generation > 0 &&
    id(owner.owner) && id(owner.parent) &&
    /^[a-f0-9]{64}$/.test(owner.worker_pool_sha256) &&
    Array.isArray(slots) && slots.length >= 1 && slots.length <= 2 &&
    Array.isArray(pool?.workers) && pool.workers.length === slots.length &&
    pool.file_sha256 === owner.worker_pool_sha256 &&
    slots.every((slot, i) => slot !== null && typeof slot === 'object' &&
      Object.keys(slot).sort().join(',') === 'invocation,phase,request,slot,worker_conversation_id' &&
      id(slot.slot) && id(slot.worker_conversation_id) && slot.invocation === null &&
      pool.workers[i]?.slot === slot.slot &&
      pool.workers[i]?.conversation_id === slot.worker_conversation_id &&
      ((slot.phase === 'idle' && slot.request === null) ||
       (slot.phase === 'collect_only' && id(slot.request)))) &&
    ['slot', 'worker_conversation_id'].every(key => new Set(slots.map(s => s[key])).size === slots.length) &&
    new Set(slots.filter(s => s.request !== null).map(s => s.request)).size ===
      slots.filter(s => s.request !== null).length &&
    Array.isArray(active) && active.length <= 1 &&
    same(status.active_assignment, active.length === 1 ? active[0] : null);
}

function retainedProvenance(owner, receipt, slot) {
  const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
  const ids = v => Array.isArray(v) && v.every(id) && new Set(v).size === v.length;
  const q = owner.qualification;
  if (!object(q) || !object(q.takeover) ||
      !Array.isArray(q.takeover_history === undefined ? [] : q.takeover_history) ||
      !Array.isArray(q.handoffs === undefined ? [] : q.handoffs)) fail();
  // Follow committed parent changes, not only the latest takeover. A request
  // from generation 22 can survive several later takeovers and same-parent starts.
  const takeovers = [...(q.takeover_history === undefined ? [] : q.takeover_history), q.takeover];
  const events = [...takeovers.map(a => ({a, takeover: true})),
    ...(q.handoffs === undefined ? [] : q.handoffs).map(a => ({a, takeover: false}))];
  if (events.some(({a, takeover}) => !object(a) ||
      !Number.isSafeInteger(a.previous_generation) || a.previous_generation < 1 ||
      !id(a.previous_parent) || !id(takeover ? a.replacement_parent : a.parent))) fail();
  events.sort((x, y) => x.a.previous_generation - y.a.previous_generation);
  let generation = 0, parent = null, found = false, latest = null;
  const rid = receipt.assignment_id;
  for (const {a, takeover} of events) {
    if (a.previous_generation < generation ||
        (parent !== null && a.previous_parent !== parent)) fail();
    const nextParent = takeover ? a.replacement_parent : a.parent;
    if (nextParent === a.previous_parent) fail();
    if (takeover) {
      latest = a;
      if (!id(a.previous_owner) || !ids(a.collect_only) || !ids(a.cancel_prepared) ||
          a.collect_only.some(r => a.cancel_prepared.includes(r)) ||
          !object(a.request_bindings) || !object(a.slot_transitions)) fail();
      const b = a.request_bindings[rid];
      if (b !== undefined || a.collect_only.includes(rid) || found) {
        if (!object(b) || Object.keys(b).sort().join(',') !== 'prior_parent,slot,worker' ||
            !a.collect_only.includes(rid) || a.cancel_prepared.includes(rid) ||
            b.prior_parent !== receipt.parent_task_id || b.slot !== slot.slot ||
            b.worker !== slot.worker_conversation_id) fail();
        const transition = a.slot_transitions[slot.slot];
        if (!object(transition) ||
            Object.keys(transition).sort().join(',') !== 'from,request,to' ||
            transition.request !== rid || transition.to !== 'collect_only' ||
            !['running', 'reserved', 'collect_only'].includes(transition.from)) fail();
        if (!found && (a.previous_parent !== receipt.parent_task_id ||
            receipt.owner_generation < generation ||
            receipt.owner_generation > a.previous_generation)) fail();
        found = true;
      }
    } else {
      // Graceful handoff cannot carry an unresolved request. Earlier handoffs
      // may establish the parent in which this request was originally armed.
      if (found || a.generation !== a.previous_generation + 1) fail();
    }
    generation = a.previous_generation + 1;
    parent = nextParent;
  }
  if (!found || latest !== q.takeover || parent !== owner.parent ||
      generation > owner.generation) fail();
}

async function unboundStopProof(owner, status) {
  if (!unboundOwner(owner, status)) return null;
  const receipt = status.active_assignment;
  if (receipt === null) return {queue: null};
  const slot = owner.slots.find(s => s.request === receipt.assignment_id);
  if (!slot || slot.phase !== 'collect_only' || !id(receipt.assignment_id) ||
      receipt.worker_slot !== slot.slot ||
      receipt.worker_conversation_id !== slot.worker_conversation_id ||
      !id(receipt.parent_task_id) || !Number.isSafeInteger(receipt.owner_generation) ||
      receipt.owner_generation < 1 || receipt.owner_generation >= owner.generation ||
      !['armed', 'submitted', 'pending', 'ambiguous', 'indeterminate'].includes(receipt.status) ||
      receipt.no_resend !== true || ![0, 1].includes(receipt.submission_count)) fail();
  retainedProvenance(owner, receipt, slot);
  // Queue.status reuses Queue.receipt's canonical association validator,
  // including exact owner_generation, parent, worker, slot and prompt hashes.
  const queue = await cli(['queue', 'status']);
  if (!Array.isArray(queue.requests) ||
      queue.requests.some(r => !r || !id(r.request_id)) ||
      new Set(queue.requests.map(r => r.request_id)).size !== queue.requests.length) fail();
  const claims = queue.requests.filter(r => r.state === 'claimed');
  if (claims.length !== 1) fail();
  const claim = claims[0];
  if (claim.request_id !== receipt.assignment_id ||
      claim.dispatch_status !== receipt.status ||
      claim.send_authorized !== false || claim.send_may_have_occurred !== true ||
      ['owner_generation', 'parent_task_id', 'worker_slot', 'worker_conversation_id']
        .some(key => claim[key] !== receipt[key])) fail();
  return {queue};
}

const block = reason => ({decision: 'block', reason});
const waitReason = 'The resident owner has not joined. Use functions.wait on the ORIGINAL returned serve cell, not a new serve call. Never resend, reopen, reset receipts, clear locks, or renew admission from this hook. If the original execution is unavailable, preserve evidence and report the host limitation through commentary; owner-loss closure remains mandatory.';
const terminalReason = 'Listener qualification ended without a Stop observation, and canonical authority or work no longer matches its terminal receipt. Preserve evidence. Do not reopen, resend, or retry qualification in this turn.';
const recoveryReason = 'This listener was served by an earlier turn of this task, so functions.wait cannot continue that execution from this turn. Recover now: call functions.exec once with the unchanged Start code in ' +
  join(dir, '../references/listener-start.md') + ' and follow only each output\'s action. The runtime fences the old owner generation before any new send, keeps unresolved requests and their no-resend state, and reports a blocker instead when replacement is unsafe. Do not edit state, pass flags, or wait on the old cell. After a stop action, report its result; this turn may then end.';

// The supported startup opens and serves only in the turn that acquired the
// owner (openResident and serve refuse any other turn). Owners acquired any
// other way have no known serving turn and keep the original-cell rule.
function servingTurn(owner) {
  const startup = owner.qualification?.startup, acquired = startup?.acquired;
  return acquired?.generation === owner.generation && acquired?.owner === owner.owner &&
    acquired?.parent === owner.parent && startup.native_task === owner.parent &&
    id(startup.native_turn) ? startup.native_turn : null;
}
// A task runs one turn at a time, so a Stop from another turn cannot keep the
// serving turn's execution alive; blocking it forever only traps the task.
// Block once per owner and turn to route recovery through the normal start,
// whose locked generation rotation fences the old execution whether or not it
// still exists, then release the reentry. The marker proves no termination
// and grants no authority.
async function otherTurnStop(owner, paths, event) {
  const directory = join(paths.state_dir, 'resident-supervision');
  const record = {version: 1, generation: owner.generation, owner: owner.owner, parent: owner.parent,
    session: owner.session, turn: event.turn_id};
  const marker = join(directory, 'other-turn-' + owner.generation + '-' + owner.owner + '-' + hash(JSON.stringify(record)) + '.json');
  await fs.mkdir(directory, {mode: 0o700}).catch(error => { if (error.code !== 'EEXIST') throw error; });
  await privateDirectory(directory);
  let seen = true;
  try { if (!same(await json(marker), record)) fail(); }
  catch (error) { if (error.code !== 'ENOENT') throw error; seen = false; }
  if (!same((await cli(['resident', 'inspect'])).owner, owner)) fail();
  if (!seen) { await exclusive(marker, record); return block(recoveryReason); }
  return event.stop_hook_active === true ? {} : block(recoveryReason);
}

// A timed-out startup leaves its acquired, unbound owner recoverable. Release
// the turn only while that exact acquisition is still current and fully idle;
// the receipt itself qualifies nothing and never authorizes a send.
async function terminalQualificationStop(owner, status, terminal) {
  const startup = owner.qualification?.startup;
  if (terminal.operation === null || owner.version !== 4 || owner.generation !== terminal.generation ||
      owner.owner !== terminal.owner || owner.parent !== terminal.parent ||
      startup?.operation_id !== terminal.operation || startup?.native_task !== terminal.parent ||
      startup?.native_turn !== terminal.turn || !unboundOwner(owner, status) ||
      owner.slots.some(slot => slot.phase !== 'idle' || slot.request !== null) ||
      status.active_assignment !== null || status.active_assignments.length !== 0) return block(terminalReason);
  const queue = await cli(['queue', 'status']);
  if (!Array.isArray(queue.requests) || queue.requests.some(r => r?.state === 'claimed')) return block(terminalReason);
  const [current, currentStatus, currentQueue] = await Promise.all([
    cli(['resident', 'inspect']), cli(['status', '--current']), cli(['queue', 'status'])]);
  if (!same(current.owner, owner) || !same(currentStatus, status) || !same(currentQueue, queue)) fail();
  return {}; // Finalization only; the owner stays acquired and unqualified.
}
async function joinedProof(owner, status, session) {
  await privateDirectory(session.directory);
  const raw = await bytes(join(session.directory, 'session.json'));
  const descriptor = JSON.parse(raw.toString('utf8'));
  if (hash(raw) !== session.descriptor_sha256 || descriptor.sessionId !== session.session_id ||
      descriptor.parent !== owner.parent || descriptor.configDir !== status.paths.config_dir ||
      descriptor.stateDir !== status.paths.state_dir || descriptor.resident !== true) fail();
  const joined = await json(join(session.directory, 'resident-joined.json'));
  if (Object.keys(joined).sort().join(',') !== 'generation,invocation,owner,parent,sessionId' ||
      joined.generation !== owner.generation || joined.owner !== owner.owner || joined.parent !== owner.parent ||
      joined.sessionId !== session.session_id || !id(joined.invocation)) fail();
  const audit = await json(join(session.directory, 'transport-audit.json'), 1048576);
  if (audit.sessionId !== session.session_id || audit.reason !== 'resident_stopped' ||
      !Array.isArray(audit.events)) fail();
  await absent(join(session.directory, 'wake.sock'));
  if ((await fs.readdir(session.directory)).some(name => name.startsWith('waiting-'))) fail();
  // A joined proof from a superseded generation cannot release another owner.
  if (!same((await cli(['resident', 'inspect'])).owner, owner)) fail();
}
export async function stopDecision(event) {
  try {
    if (event?.hook_event_name !== 'Stop' || !id(event.session_id))
      return block('Invalid Stop identity; preserve resident state and do not advertise availability.');
    const {owner} = await cli(['resident', 'inspect']);
    if (!id(event.turn_id)) {
      if (!owner || owner.parent !== event.session_id) return {};
      return block(waitReason + ' Native Stop turn identity is missing.');
    }
    let status;
    try { status = await cli(['status', '--current']); }
    catch (error) {
      // A conclusively absent or different owner must not trap ordinary tasks
      // merely because a worker has not been configured. Qualification still
      // fails closed: its native caller requires the missing canonical status.
      if (!owner || owner.parent !== event.session_id) return {};
      throw error;
    }
    const files = location(status.paths, event.session_id, event.turn_id);
    let challenge, terminal = null;
    try { challenge = await json(files.challenge); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (challenge !== undefined) {
      if (challenge.parent !== event.session_id || challenge.turn !== event.turn_id) fail();
      await checkChallenge(challenge, status.paths, files);
      // Only the first, actual host Stop event attests the preflight. Never
      // treat stop_hook_active as permission to finalize an active owner.
      try {
        await absent(files.observed);
        await exclusive(files.observed, challenge);
        return block('Supervision preflight only: call functions.wait on the ORIGINAL yielded qualification cell, then continue the activation recipe only after its successful completion. Do not replay qualification. No listener availability or send is authorized yet.');
      } catch (error) {
        // An existing proof must match byte-for-byte; no timestamp refresh.
        // A terminal receipt means the waiter's deadline won the single slot.
        const observed = await json(files.observed);
        if (terminalFor(observed, challenge)) terminal = observed;
        else if (!same(observed, challenge)) throw error;
      }
    }
    if (!owner || owner.parent !== event.session_id) return {};
    if (terminal) return await terminalQualificationStop(owner, status, terminal);
    // This exemption runs only AFTER pending qualification challenges.
    const proof = await unboundStopProof(owner, status);
    if (proof !== null) {
      // Read-only and bounded. Recheck the full active set (the scalar alias
      // hides multiple assignments), provenance, queue bindings and paths.
      const [current, currentStatus, currentQueue] = await Promise.all([
        cli(['resident', 'inspect']), cli(['status', '--current']),
        proof.queue === null ? null : cli(['queue', 'status'])
      ]);
      if (!same(current.owner, owner) || !same(currentStatus, status) ||
          !same(currentQueue, proof.queue)) fail();
      return {}; // Finalization only; no admission, collection, or send authority.
    }
    if (owner.inflight != null || owner.slots?.some(slot => slot.request !== null || slot.invocation !== null) ||
        status.active_assignment != null || status.active_assignments?.length) {
      // The host sets stop_hook_active only on a continuation created by this
      // hook. Require one identical reentry before releasing the dead turn.
      // The marker is supervision evidence only and grants no runtime action.
      if (await terminalStop(owner, status, event)) return {};
      return block(waitReason);
    }
    const session = owner.session;
    if (!session) return block(waitReason);
    try { await joinedProof(owner, status, session); }
    catch (error) {
      const serving = servingTurn(owner);
      if (serving === null || serving === event.turn_id) throw error;
      // Legacy pristine-session recovery can legitimately bind a later serving
      // turn. Its retained claim means startup.native_turn is not sufficient
      // to identify that execution; keep the original-cell rule for it.
      await absent(join(session.directory, 'resident-serve-existing.json'));
      await absent(join(session.directory, 'resident-unused-replacement.json'));
      return await otherTurnStop(owner, status.paths, event);
    }
    return {};
  } catch (error) {
    return block(waitReason + ' Supervision evidence could not be verified.' +
      (error.name === 'SupervisionReadError' ? ' ' + error.message : ''));
  }
}

if (typeof process !== 'undefined' && process.argv[1] &&
    await fs.realpath(process.argv[1]) === here) {
  if (process.argv[2] !== 'stop' || process.argv.length !== 3) {
    console.error('Use the installed synchronous Stop hook.'); process.exitCode = 2;
  } else {
    try {
      let raw = '';
      for await (const chunk of process.stdin) {
        raw += chunk;
        if (Buffer.byteLength(raw) > 1048576) throw Error('Oversized Stop event');
      }
      console.log(JSON.stringify(await stopDecision(JSON.parse(raw))));
    } catch { console.log(JSON.stringify(block(waitReason + ' Stop input was invalid.'))); }
  }
}
