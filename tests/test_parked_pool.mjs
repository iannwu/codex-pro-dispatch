import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import * as net from "node:net";
import * as os from "node:os";
import { randomBytes } from "node:crypto";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { openSession } from "../skills/codex-pro-dispatch/scripts/parked-socket.mjs";
import {
  poolRecoveryPlan, recoverPoolRequests
} from "../skills/codex-pro-dispatch/scripts/parked-activation.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const helper = root + "bin/pro-dispatch";

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

async function waitForFile(path) {
  for (let i = 0; i < 500; i++) {
    try {
      await fs.lstat(path);
      return;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw Error("Missing fixture file: " + path);
}

function connect(directory, config, requestId) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(directory + "/wake.sock");
    const callId = randomBytes(16).toString("hex");
    let raw = "";
    socket.on("connect", () => socket.write(JSON.stringify({
      sessionId: config.sessionId, token: config.token, callId, requestId,
      operation: "run"
    }) + "\n"));
    socket.on("data", buffer => { raw += buffer.toString("utf8"); });
    socket.on("error", reject);
    socket.on("end", () => {
      try { resolve(JSON.parse(raw)); }
      catch (error) { reject(error); }
    });
  });
}

function connectRaw(directory, payload) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(directory + "/wake.sock");
    let raw = "";
    socket.on("connect", () => socket.write(JSON.stringify(payload) + "\n"));
    socket.on("data", buffer => { raw += buffer.toString("utf8"); });
    socket.on("error", reject);
    socket.on("close", () => resolve(raw));
  });
}

test("one listener accepts two overlapping socket deliveries", async t => {
  const temporary = await fs.mkdtemp(os.tmpdir() + "/parked-pool-unit-");
  const directory = await fs.realpath(temporary);
  const home = directory + "/authority";
  const sessionDir = directory + "/session";
  const env = { ...process.env, CODEX_PRO_DISPATCH_HOME: home };
  await fs.mkdir(sessionDir, { mode: 448 });
  await execute("python3", [helper, "worker", "set", "--conversation-id", "worker-a",
    "--confirm-worker", "--native-controls-confirmed"], env);
  const socket = await openSession(sessionDir, {
    helper, configDir: home + "/config", stateDir: home + "/state",
    parent: "fixture-parent",
    workers: [
      { slot: "slot-a", conversation_id: "worker-a" },
      { slot: "slot-b", conversation_id: "worker-b" }
    ],
    maxConcurrentRequests: 2,
    resident: true, leaseMs: null, idleMs: 45000, replyMs: 30000
  });
  t.after(async () => {
    await socket.close("unit_finished").catch(() => {});
    await fs.rm(directory, { recursive: true, force: true });
  });
  assert.equal(socket.config.maxConcurrentRequests, 2);
  const firstWait = socket.receive();
  await waitForFile(sessionDir + "/ready-1.json");
  const secondWait = socket.receive();
  await waitForFile(sessionDir + "/ready-2.json");
  const firstClient = connect(sessionDir, socket.config, "request-a");
  const secondClient = connect(sessionDir, socket.config, "request-b");
  const [first, second] = await Promise.all([firstWait, secondWait]);
  assert.ok(first && second, "both receives must accept a delivery");
  assert.deepEqual(
    [first.requestId, second.requestId].sort(),
    ["request-a", "request-b"]
  );
  const thirdClient = await connect(sessionDir, socket.config, "request-c");
  assert.equal(thirdClient.status, "busy");
  const finished = await Promise.all([
    socket.finish(first.callId, {
      request_id: first.requestId, observation: "published",
      restoration: { status: "not_requested" }
    }),
    socket.finish(second.callId, {
      request_id: second.requestId, observation: "published",
      restoration: { status: "not_requested" }
    })
  ]);
  assert.equal(finished[0].closed, false);
  assert.equal(finished[1].closed, false);
  const replies = await Promise.all([firstClient, secondClient]);
  assert.deepEqual(replies.map(value => value.status).sort(), ["published", "published"]);
});

test("one-worker pool listener rejects a second overlapping delivery", async t => {
  const temporary = await fs.mkdtemp(os.tmpdir() + "/parked-pool-one-");
  const directory = await fs.realpath(temporary);
  const sessionDir = directory + "/session";
  await fs.mkdir(sessionDir, { mode: 448 });
  const socket = await openSession(sessionDir, {
    helper: helper, configDir: directory + "/config", stateDir: directory + "/state",
    parent: "fixture-parent",
    workers: [{ slot: "slot-a", conversation_id: "worker-a" }],
    maxConcurrentRequests: 1,
    resident: true, leaseMs: null, idleMs: 45000, replyMs: 30000
  });
  t.after(async () => {
    await socket.close("unit_finished").catch(() => {});
    await fs.rm(directory, { recursive: true, force: true });
  });
  assert.equal(socket.config.maxConcurrentRequests, 1);
  const firstWait = socket.receive();
  await waitForFile(sessionDir + "/ready-1.json");
  const firstClient = connect(sessionDir, socket.config, "request-a");
  const first = await firstWait;
  assert.equal(first.requestId, "request-a");
  const secondClient = await connect(sessionDir, socket.config, "request-b");
  assert.equal(secondClient.status, "busy");
  await socket.finish(first.callId, {
    request_id: first.requestId, observation: "published",
    restoration: { status: "not_requested" }
  });
  const firstReply = await firstClient;
  assert.equal(firstReply.status, "published");
});

test("pool receive timeout withdraws its waiter and leaves later admission usable", async t => {
  const temporary = await fs.mkdtemp(os.tmpdir() + "/parked-pool-timeout-");
  const directory = await fs.realpath(temporary);
  const sessionDir = directory + "/session";
  await fs.mkdir(sessionDir, { mode: 448 });
  const socket = await openSession(sessionDir, {
    helper, configDir: directory + "/config", stateDir: directory + "/state",
    parent: "fixture-parent",
    workers: [
      { slot: "slot-a", conversation_id: "worker-a" },
      { slot: "slot-b", conversation_id: "worker-b" }
    ],
    maxConcurrentRequests: 2,
    resident: true, leaseMs: null, idleMs: 45000, replyMs: 30000
  });
  t.after(async () => {
    await socket.close("unit_finished").catch(() => {});
    await fs.rm(directory, { recursive: true, force: true });
  });

  assert.equal(await socket.receive(20), null);
  const nextWait = socket.receive();
  await waitForFile(sessionDir + "/ready-2.json");
  const client = connect(sessionDir, socket.config, "request-after-timeout");
  const delivery = await nextWait;
  assert.equal(delivery.requestId, "request-after-timeout");
  await socket.finish(delivery.callId, {
    request_id: delivery.requestId, observation: "published",
    restoration: { status: "not_requested" }
  });
  assert.equal((await client).status, "published");
});

test("pool close drains outstanding receives without leaving admissions", async t => {
  const temporary = await fs.mkdtemp(os.tmpdir() + "/parked-pool-close-");
  const directory = await fs.realpath(temporary);
  const sessionDir = directory + "/session";
  await fs.mkdir(sessionDir, { mode: 448 });
  const socket = await openSession(sessionDir, {
    helper, configDir: directory + "/config", stateDir: directory + "/state",
    parent: "fixture-parent",
    workers: [
      { slot: "slot-a", conversation_id: "worker-a" },
      { slot: "slot-b", conversation_id: "worker-b" }
    ],
    maxConcurrentRequests: 2,
    resident: true, leaseMs: null, idleMs: 45000, replyMs: 30000
  });
  t.after(async () => {
    await socket.close("unit_finished").catch(() => {});
    await fs.rm(directory, { recursive: true, force: true });
  });

  const first = socket.receive();
  const second = socket.receive();
  await waitForFile(sessionDir + "/ready-2.json");
  await socket.close("operator_stop");
  assert.deepEqual(await Promise.all([first, second]), [null, null]);
  assert.equal(JSON.parse(await fs.readFile(
    sessionDir + "/transport-audit.json", "utf8"
  )).reason, "operator_stop");
});

test("finite pool lease retires an idle nonresident session", async t => {
  const temporary = await fs.mkdtemp(os.tmpdir() + "/parked-pool-lease-");
  const directory = await fs.realpath(temporary);
  const sessionDir = directory + "/session";
  await fs.mkdir(sessionDir, { mode: 448 });
  const socket = await openSession(sessionDir, {
    helper, configDir: directory + "/config", stateDir: directory + "/state",
    parent: "fixture-parent",
    workers: [{ slot: "slot-a", conversation_id: "worker-a" }],
    maxConcurrentRequests: 1,
    leaseMs: 20, idleMs: 45000, replyMs: 30000
  });
  t.after(async () => {
    await socket.close("unit_finished").catch(() => {});
    await fs.rm(directory, { recursive: true, force: true });
  });

  await waitForFile(sessionDir + "/transport-audit.json");
  assert.equal(await socket.receive(), null);
  assert.equal(JSON.parse(await fs.readFile(
    sessionDir + "/transport-audit.json", "utf8"
  )).reason, "lease_expired");
});

test("pool readiness collision rejects receive and removes its waiter", async t => {
  const temporary = await fs.mkdtemp(os.tmpdir() + "/parked-pool-ready-");
  const directory = await fs.realpath(temporary);
  const sessionDir = directory + "/session";
  await fs.mkdir(sessionDir, { mode: 448 });
  const socket = await openSession(sessionDir, {
    helper, configDir: directory + "/config", stateDir: directory + "/state",
    parent: "fixture-parent",
    workers: [{ slot: "slot-a", conversation_id: "worker-a" }],
    maxConcurrentRequests: 1,
    resident: true, leaseMs: null, idleMs: 45000, replyMs: 30000
  });
  t.after(async () => {
    await socket.close("unit_finished").catch(() => {});
    await fs.rm(directory, { recursive: true, force: true });
  });

  await fs.writeFile(sessionDir + "/ready-1.json", "occupied", { mode: 384 });
  await assert.rejects(() => socket.receive(), /EEXIST/);
  const client = await connect(sessionDir, socket.config, "request-no-waiter");
  assert.equal(client.status, "not_ready");
});

test("pool listener rejects a forged frame without consuming readiness", async t => {
  const temporary = await fs.mkdtemp(os.tmpdir() + "/parked-pool-forged-");
  const directory = await fs.realpath(temporary);
  const sessionDir = directory + "/session";
  await fs.mkdir(sessionDir, { mode: 448 });
  const socket = await openSession(sessionDir, {
    helper, configDir: directory + "/config", stateDir: directory + "/state",
    parent: "fixture-parent",
    workers: [{ slot: "slot-a", conversation_id: "worker-a" }],
    maxConcurrentRequests: 1,
    resident: true, leaseMs: null, idleMs: 45000, replyMs: 30000
  });
  t.after(async () => {
    await socket.close("unit_finished").catch(() => {});
    await fs.rm(directory, { recursive: true, force: true });
  });

  const waiting = socket.receive();
  await waitForFile(sessionDir + "/ready-1.json");
  assert.equal(await connectRaw(sessionDir, {
    sessionId: socket.config.sessionId, token: "wrong-token",
    callId: "a".repeat(32), requestId: "request-forged", operation: "run"
  }), "");
  const client = connect(sessionDir, socket.config, "request-valid");
  const delivery = await waiting;
  assert.equal(delivery.requestId, "request-valid");
  await socket.finish(delivery.callId, {
    request_id: delivery.requestId, observation: "published",
    restoration: { status: "not_requested" }
  });
  assert.equal((await client).status, "published");
});

test("pool recovery plan keeps prepared work off the collector-only list", () => {
  assert.deepEqual(poolRecoveryPlan({
    recovery: ["request-armed"], preparedRecovery: ["request-prepared"]
  }), { collect: ["request-armed"], sendable: ["request-prepared"] });
  assert.throws(
    () => poolRecoveryPlan({ recovery: ["request-a"], preparedRecovery: ["request-a"] }),
    /Prepared recovery cannot be collector-only/
  );
});

test("prepared recovery sends once and collector-only never sends", async () => {
  const events = [];
  await recoverPoolRequests(
    { collect: ["request-armed"], sendable: ["request-prepared"] },
    {
      openCollector: async () => events.push("open"),
      closeCollector: async () => events.push("close"),
      collect: async request => {
        events.push("collect:" + request);
        return { ok: true, observation: "published", worker_slot: "slot-a" };
      },
      sendPrepared: async request => {
        events.push("send:" + request);
        return { ok: true, observation: "published", worker_slot: "slot-b" };
      },
      endCollected: async request => events.push("end-collect:" + request),
      endPrepared: async request => events.push("end-send:" + request)
    }
  );
  assert.deepEqual(events, [
    "open", "collect:request-armed", "end-collect:request-armed", "close",
    "send:request-prepared", "end-send:request-prepared"
  ]);
  await assert.rejects(() => recoverPoolRequests(
    { collect: ["request-prepared"], sendable: [] },
    {
      openCollector: async () => {},
      closeCollector: async () => {},
      collect: async () => ({ ok: true, observation: "not_submitted" }),
      sendPrepared: async () => { throw Error("must not send"); },
      endCollected: async () => {},
      endPrepared: async () => {}
    }
  ), /Prepared unsent work cannot be recovered collector-only/);
  await assert.rejects(() => recoverPoolRequests(
    { collect: [], sendable: ["request-prepared"] },
    {
      openCollector: async () => { throw Error("sendable recovery must not open a collector"); },
      closeCollector: async () => { throw Error("sendable recovery must not close a collector"); },
      collect: async () => { throw Error("must not collect"); },
      sendPrepared: async () => ({ ok: true, observation: "not_submitted" }),
      endCollected: async () => {},
      endPrepared: async () => {}
    }
  ), /did not perform its eligible first send/);
  const pendingEvents = [];
  await recoverPoolRequests(
    { collect: [], sendable: ["request-prepared"] },
    {
      openCollector: async () => { throw Error("sendable recovery must not open a collector"); },
      closeCollector: async () => { throw Error("sendable recovery must not close a collector"); },
      collect: async () => { throw Error("must not collect"); },
      sendPrepared: async request => {
        pendingEvents.push("send:" + request);
        return { ok: true, observation: "pending", worker_slot: "slot-a" };
      },
      endCollected: async () => {},
      endPrepared: async () => pendingEvents.push("ended")
    }
  );
  assert.deepEqual(pendingEvents, ["send:request-prepared"]);
});

test("pending collector does not block a ready sibling", async () => {
  const mixed = [];
  await assert.rejects(() => recoverPoolRequests(
    { collect: ["request-pending"], sendable: ["request-prepared"] },
    {
      openCollector: async () => mixed.push("open"),
      closeCollector: async () => mixed.push("close"),
      collect: async request => {
        mixed.push("collect:" + request);
        return { ok: true, observation: "pending", worker_slot: "slot-a" };
      },
      sendPrepared: async request => {
        mixed.push("send:" + request);
        return { ok: true, observation: "published", worker_slot: "slot-b" };
      },
      endCollected: async request => mixed.push("end-collect:" + request),
      endPrepared: async request => mixed.push("end-send:" + request)
    }
  ), /Recovery remains collect-only/);
  assert.deepEqual(mixed, [
    "open", "collect:request-pending", "close",
    "send:request-prepared", "end-send:request-prepared"
  ]);
  const both = [];
  await assert.rejects(() => recoverPoolRequests(
    { collect: ["request-pending", "request-complete"], sendable: [] },
    {
      openCollector: async () => both.push("open"),
      closeCollector: async () => both.push("close"),
      collect: async request => {
        both.push("collect:" + request);
        if (request === "request-pending")
          return { ok: true, observation: "pending", worker_slot: "slot-a" };
        return { ok: true, observation: "published", worker_slot: "slot-b" };
      },
      sendPrepared: async () => { throw Error("must not send"); },
      endCollected: async request => both.push("end-collect:" + request),
      endPrepared: async () => { throw Error("must not end prepared"); }
    }
  ), /Recovery remains collect-only/);
  assert.deepEqual(both, [
    "open", "collect:request-pending", "collect:request-complete",
    "end-collect:request-complete", "close"
  ]);
});
