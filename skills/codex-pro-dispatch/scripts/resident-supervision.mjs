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
    execFile('python3', [helper, ...args], {timeout: 3000, maxBuffer: 1048576}, (error, out) => {
      try {
        if (error) throw Error('Canonical supervision read failed');
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
// Event-driven, bounded preflight with no socket, readiness marker, or native send.
export async function waitSupervision(g, meta, trusted) {
  const {paths, files} = await context(meta, trusted);
  await checkChallenge(g.parkedSupervision, paths, files);
  await new Promise((resolve, reject) => {
    let done = false, busy = false, again = false;
    const finish = error => {
      if (done) return;
      done = true; clearTimeout(timer); watcher.close();
      error ? reject(error) : resolve();
    };
    const watcher = watch(files.directory, () => { again = true; void check(); });
    const timer = setTimeout(() => finish(Error('Stop hook did not qualify; no availability')), 45000);
    watcher.on('error', finish);
    async function check() {
      if (done || busy) return;
      busy = true; again = false;
      try {
        if (!same(await json(files.observed), g.parkedSupervision)) fail();
        finish();
      } catch (error) { if (error.code !== 'ENOENT') finish(error); }
      finally { busy = false; if (again && !done) void check(); }
    }
    void check();
  });
  return {kind: 'resident_supervision_stop_observed', admissionObserved: false};
}

const block = reason => ({decision: 'block', reason});
const waitReason = 'The resident owner has not joined. Use functions.wait on the ORIGINAL returned serve cell, not a new serve call. Never resend, reopen, reset receipts, clear locks, or renew admission from this hook. If the original execution is unavailable, preserve evidence and report the host limitation through commentary; owner-loss closure remains mandatory.';
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
    let challenge;
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
        if (!same(await json(files.observed), challenge)) throw error;
      }
    }
    if (!owner || owner.parent !== event.session_id) return {};
    if (owner.inflight != null || owner.slots?.some(slot => slot.request !== null || slot.invocation !== null) ||
        status.active_assignment != null) return block(waitReason);
    const session = owner.session;
    if (!session) return block(waitReason);
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
    return {};
  } catch {
    return block(waitReason + ' Supervision evidence could not be verified.');
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
