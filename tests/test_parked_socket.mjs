import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { openSession, loadSession } from "../skills/codex-pro-dispatch/scripts/parked-socket.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const helper = root + "bin/pro-dispatch";
const client = root + "skills/codex-pro-dispatch/scripts/parked-client.mjs";
const resident = { resident: true, leaseMs: null };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function missing(path) {
  await assert.rejects(fs.lstat(path), e => e.code === "ENOENT");
}

function execute(command, args, env) {
  return new Promise((resolve, reject) => {
    const child = execFile(command, args, {
      env, timeout: 35000, maxBuffer: 8 * 1024 * 1024
    }, (error, stdout, stderr) => {
      if (error) reject(Error(stderr || error.message));
      else {
        try { resolve(JSON.parse(stdout)); }
        catch { reject(Error("Invalid subprocess JSON: " + stdout)); }
      }
    });
    child.stdin.end();
  });
}

async function fixture(t, overrides = {}, create = openSession) {
  const temporary = await fs.mkdtemp(os.tmpdir() + "/parked-cli-unit-");
  const directory = await fs.realpath(temporary);
  const home = directory + "/authority";
  const sessionDir = directory + "/session";
  const env = { ...process.env, CODEX_PRO_DISPATCH_HOME: home };
  const cli = args => execute("python3", [helper, ...args], env);
  await fs.mkdir(sessionDir, { mode: 448 });
  await cli(["worker", "set", "--conversation-id", "fixture-pro",
    "--confirm-pro", "--native-controls-confirmed"]);
  const socket = await create(sessionDir, {
    helper, configDir: home + "/config", stateDir: home + "/state",
    worker: "fixture-pro", parent: "fixture-parent",
    leaseMs: 60000, idleMs: 10000, replyMs: 30000, ...overrides
  });
  const prompt = directory + "/prompt.txt";
  await fs.writeFile(prompt, "Return a fixture answer.", { mode: 384 });
  const pending = new Set();
  t.after(async () => {
    await socket.close("unit_finished");
    await Promise.allSettled([...pending]);
    await fs.rm(directory, { recursive: true });
  });
  const invoke = (operation, rid) => {
    const result = execute(process.execPath, [
      client, sessionDir, operation, rid,
      ...(operation === "submit" ? [prompt, "fixture-client"] : [])
    ], env);
    pending.add(result);
    result.then(() => pending.delete(result), () => pending.delete(result));
    return result;
  };

  async function claimAndArm(rid) {
    const claimed = await cli(["queue", "claim", "--request-id", rid,
      "--parent-task-id", "fixture-parent", "--native-controls-confirmed"]);
    await cli(["arm", rid]);
    return claimed;
  }

  async function publish(rid, claimed) {
    const document = {
      schemaVersion: 1,
      thread: {
        id: "fixture-pro", kind: "chatgpt", status: { type: "idle" }
      },
      turns: [{
        id: "fixture-turn-" + rid,
        items: [
          {
            id: "fixture-turn-" + rid, type: "userMessage",
            content: [{ type: "text", text: claimed.wrapped_prompt }]
          },
          {
            id: "fixture-answer-" + rid, type: "agentMessage",
            text: "[CODEX_PRO_DISPATCH_RESULT assignment_id=" + rid +
              "]\nfixture answer\n[CODEX_PRO_DISPATCH_END assignment_id=" +
              rid + "]"
          }
        ]
      }]
    };
    const file = directory + "/history-" + rid + ".json";
    await fs.writeFile(file, JSON.stringify(document), { mode: 384 });
    return await cli(["queue", "observe", rid,
      "--parent-task-id", "fixture-parent", "--native-controls-confirmed",
      "--native-read-file", file]);
  }

  return { directory, sessionDir, socket, cli, invoke, claimAndArm, publish };
}

test("real submit/observe/collect/ack; socket cannot forge an answer", async t => {
  const f = await fixture(t);
  const receiving = f.socket.receive();
  const clientResult = f.invoke("submit", "fixture-A");
  clientResult.catch(() => {});
  const delivery = await receiving;
  assert.equal(delivery.operation, "run");
  const queued = await f.cli(["queue", "collect", "fixture-A"]);
  assert.equal(queued.state, "queued");
  assert.equal(queued.send_authorized, false);
  const claim = await f.claimAndArm("fixture-A");
  const answer = await f.publish("fixture-A", claim);
  await f.socket.finish(delivery.callId, {
    ...answer, answer: { payload: "FORGED SOCKET ANSWER" }
  });
  const received = await clientResult;
  assert.equal(received.answer.payload, "fixture answer");
  assert.equal(received.answer.verification_level, "bounded_native_summary");
  assert.equal(received.answer.generation_finality_verified, false);
  assert.deepEqual((await f.invoke("collect", "fixture-A")).answer, received.answer);
  assert.deepEqual((await f.invoke("submit", "fixture-A")).answer, received.answer);
  await f.invoke("acknowledge", "fixture-A");
  await f.invoke("acknowledge", "fixture-A");
  const after = await f.invoke("collect", "fixture-A");
  assert.equal(after.state, "acknowledged");
  assert.equal(after.body_available, false);
});

test("pending recovery uses actual post-arm metadata and explicit observe", async t => {
  const f = await fixture(t);
  const firstWait = f.socket.receive();
  const firstClient = f.invoke("submit", "fixture-A");
  firstClient.catch(() => {});
  const first = await firstWait;
  const claim = await f.claimAndArm("fixture-A");
  await f.socket.finish(first.callId, {
    request_id: "fixture-A", observation: "pending"
  });
  const pending = await firstClient;
  assert.equal(pending.dispatch_status, "armed");
  assert.equal(pending.send_may_have_occurred, true);
  assert.equal(pending.sent_verified, false);
  assert.equal((await f.invoke("submit", "fixture-A")).wake.status, "collect_only");

  const secondWait = f.socket.receive();
  const secondClient = f.invoke("observe", "fixture-A");
  secondClient.catch(() => {});
  const second = await secondWait;
  assert.equal(second.operation, "observe");
  const answer = await f.publish("fixture-A", claim);
  await f.socket.finish(second.callId, answer);
  assert.equal((await secondClient).answer.payload, "fixture answer");
  const status = await f.cli(["status", "fixture-A"]);
  assert.equal(status.assignment.submission_count, 1);
  assert.equal(status.assignment.no_resend, true);
});

test("busy rejection stores B only in canonical queue, never auto-delivers it", async t => {
  const f = await fixture(t);
  const waiting = f.socket.receive();
  const a = f.invoke("submit", "fixture-A");
  a.catch(() => {});
  const delivery = await waiting;
  await f.claimAndArm("fixture-A");
  const b = await f.invoke("submit", "fixture-B");
  assert.equal(b.state, "queued");
  assert.equal(b.wake.status, "busy");
  await f.socket.finish(delivery.callId, {
    request_id: "fixture-A", observation: "pending"
  });
  await a;
  let settled = false;
  const next = f.socket.receive().then(value => { settled = true; return value; });
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(settled, false);
  assert.equal((await f.cli(["queue", "collect", "fixture-B"])).state, "queued");
  await f.socket.close("unit_no_automatic_drain");
  assert.equal(await next, null);
});

test("resident has no lease timer; legacy lease callbacks retain their behavior", async t => {
  const timers = [];
  async function trackedOpen(directory, config) {
    const original = globalThis.setTimeout;
    const spy = t.mock.method(globalThis, "setTimeout", (fn, ms, ...args) => {
      const handle = original(fn, ms, ...args);
      timers.push({ fn, ms, handle });
      return handle;
    });
    try { return await openSession(directory, config); }
    finally { spy.mock.restore(); }
  }
  // Instrument only openSession, not helper processes or test deadlines.
  const r = await fixture(t, resident, trackedOpen);
  assert.deepEqual(timers, []);
  assert.equal(r.socket.config.expiresAt, null);
  assert.deepEqual(await loadSession(r.sessionDir), r.socket.config);

  for (const active of [false, true]) {
    const started = Date.now();
    const f = await fixture(t, {}, trackedOpen);
    assert.equal(timers.length, active ? 2 : 1);
    const timer = timers.at(-1);
    assert.equal(timer.ms, 60000);
    assert.equal(f.socket.config.resident, undefined);
    assert(f.socket.config.expiresAt >= started + 60000);
    assert(f.socket.config.expiresAt <= Date.now() + 60000);
    assert.deepEqual(await loadSession(f.sessionDir), f.socket.config);
    let delivery, result;
    if (active) {
      const receiving = f.socket.receive();
      result = f.invoke("submit", "finite-A");
      delivery = await receiving;
      assert.equal(delivery?.requestId, "finite-A");
    }
    // Invoke the actual registered callback, without waiting a real lease.
    clearTimeout(timer.handle);
    timer.fn();
    if (active) {
      await missing(f.sessionDir + "/transport-audit.json");
      assert((await fs.lstat(f.sessionDir + "/wake.sock")).isSocket());
      const claim = await f.claimAndArm("finite-A");
      const answer = await f.publish("finite-A", claim);
      assert.equal((await f.socket.finish(delivery.callId, answer)).closed, true);
      assert.equal((await result).answer.payload, "fixture answer");
      assert.equal((await f.cli(["status", "finite-A"])).assignment.submission_count, 1);
    } else {
      await f.socket.close("join_lease_close");
    }
    const audit = JSON.parse(await fs.readFile(f.sessionDir + "/transport-audit.json", "utf8"));
    if (active) {
      // Socket close can win the existing finish/retirement ordering.
      assert(["expired_transport", "retired_after_job"].includes(audit.reason));
      assert.deepEqual(audit.events.map(e => [e.name, e.requestId]),
        [["accepted", "finite-A"], ["finished", "finite-A"]]);
    } else {
      assert.equal(audit.reason, "lease_expired");
      assert.deepEqual(audit.events, []);
    }
    await missing(f.sessionDir + "/wake.sock");
    assert.equal(await f.socket.receive(), null);
    await missing(f.sessionDir + "/ready-" + (active ? 2 : 1) + ".json");
  }
  await missing(r.sessionDir + "/transport-audit.json");
  assert((await fs.lstat(r.sessionDir + "/wake.sock")).isSocket());
});

test("creation and loading reject malformed lifetime tags and budgets", async t => {
  const f = await fixture(t), base = f.socket.config;
  const bad = [
    { resident: false }, { resident: "true" }, { resident: 1 }, { resident: null },
    { leaseMs: null }, { resident: true },
    { ...resident, leaseMs: undefined }, { ...resident, queuedResume: {} }
  ];
  for (const mode of [{}, resident]) {
    const budgets = mode.resident ?
      [["idleMs", 50000], ["replyMs", 3900000]] :
      [["leaseMs", 7200000], ["idleMs", 50000], ["replyMs", 3900000]];
    for (const [key, maximum] of budgets)
      for (const value of [undefined, null, 0, -1, 1.5, maximum + 1, Infinity, "1000"])
        bad.push({ ...mode, [key]: value });
  }
  for (const change of bad) {
    const d = await fs.mkdtemp(f.directory + "/bad-");
    const c = { ...base, ...change };
    const error = /Explicit resident lifetime|Finite transport budgets/;
    await assert.rejects(async () => {
      const unexpected = await openSession(d, c);
      // A regression must fail the assertion without leaking a listener.
      await unexpected.close("unexpected_valid_configuration");
    }, error, JSON.stringify(change));
    await missing(d + "/wake.sock");
    await missing(d + "/session.json");
    await fs.writeFile(d + "/session.json", JSON.stringify(c), { mode: 384 });
    await assert.rejects(loadSession(d), error, JSON.stringify(change));
  }
  const d = await fs.mkdtemp(f.directory + "/expiry-");
  for (const change of [
    { expiresAt: undefined }, { expiresAt: null }, { expiresAt: 0 },
    { expiresAt: -1 }, { expiresAt: 1.5 }, { expiresAt: Infinity },
    { expiresAt: Number.MAX_SAFE_INTEGER + 1 },
    { ...resident, expiresAt: undefined }, { ...resident, expiresAt: Date.now() }
  ]) {
    await fs.writeFile(d + "/session.json",
      JSON.stringify({ ...base, ...change }), { mode: 384 });
    await assert.rejects(loadSession(d), /Invalid session lifetime/);
  }
  // Historical finite descriptors remain loadable for collection.
  const expired = { ...base, expiresAt: 1 };
  await fs.writeFile(d + "/session.json", JSON.stringify(expired));
  assert.deepEqual(await loadSession(d), expired);
});

test("resident handles A, quiet without receive, then B with unchanged identity", async t => {
  const f = await fixture(t, resident);
  const descriptor = await fs.readFile(f.sessionDir + "/session.json");
  const ids = ["resident-A", "resident-B"];
  for (let i = 0; i < ids.length; i++) {
    if (i) {
      // Exceed the pickup window without starting another receive.
      await pause(f.socket.config.idleMs + 100);
      await missing(f.sessionDir + "/ready-2.json");
      await missing(f.sessionDir + "/transport-audit.json");
      assert((await fs.lstat(f.sessionDir + "/wake.sock")).isSocket());
    }
    const id = ids[i], receiving = f.socket.receive();
    const result = f.invoke("submit", id);
    const delivery = await receiving;
    assert.equal(delivery?.requestId, id);
    assert.equal(delivery.sessionId, f.socket.config.sessionId);
    const claim = await f.claimAndArm(id);
    const answer = await f.publish(id, claim);
    assert.equal((await f.socket.finish(delivery.callId, answer)).closed, false);
    const received = await result;
    assert.equal(received.answer.payload, "fixture answer");
    assert.deepEqual((await f.invoke("submit", id)).answer, received.answer);
    const status = (await f.cli(["status", id])).assignment;
    assert.equal(status.submission_count, 1);
    assert.equal(status.no_resend, true);
    const ready = JSON.parse(await fs.readFile(
      f.sessionDir + "/ready-" + (i + 1) + ".json", "utf8"));
    assert.equal(ready.ordinal, i + 1);
    assert.equal(ready.sessionId, f.socket.config.sessionId);
    assert.deepEqual(await fs.readFile(f.sessionDir + "/session.json"), descriptor);
    assert.deepEqual(await loadSession(f.sessionDir), f.socket.config);
  }
  await missing(f.sessionDir + "/ready-3.json");
  await f.socket.close("unit_resident_AB");
  const audit = JSON.parse(await fs.readFile(f.sessionDir + "/transport-audit.json", "utf8"));
  assert.deepEqual(audit.events.map(e => [e.name, e.requestId]),
    ids.flatMap(id => [["accepted", id], ["finished", id]]));
});

for (const explicit of [false, true])
test(explicit ? "resident explicit close is permanent and idempotent" :
  "resident receive retains finite idle expiry", async t => {
  const f = await fixture(t, { ...resident, idleMs: explicit ? 10000 : 50 });
  const receiving = f.socket.receive();
  if (explicit) {
    const closing = f.socket.close("unit_explicit_stop");
    assert.equal(f.socket.close("ignored_reason"), closing);
    await closing;
  }
  assert.equal(await receiving, null);
  await f.socket.close("join_existing_close");
  const path = f.sessionDir + "/transport-audit.json";
  const before = await fs.readFile(path);
  const audit = JSON.parse(before.toString("utf8"));
  assert.equal(audit.reason, explicit ? "unit_explicit_stop" : "idle_expired");
  assert.deepEqual(audit.events, []);
  await missing(f.sessionDir + "/wake.sock");
  await f.socket.close("another_reason");
  assert.equal(await f.socket.receive(), null);
  await missing(f.sessionDir + "/ready-2.json");
  assert.deepEqual(await fs.readFile(path), before);
});
