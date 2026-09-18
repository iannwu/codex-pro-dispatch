const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(
  __dirname, "../skills/codex-pro-dispatch/scripts/parked-runner.js"
), "utf8");

test("synthetic pool scheduler lets B finish while A is still observing", async () => {
  const order = [];
  const context = vm.createContext({
    Date: { now: () => 1000 },
    setTimeout: (fn, ms) => { fn(); return 0; },
    tools: {
      mcp__node_repl__js: async args => ({
        isError: false,
        content: [{ type: "text", text: JSON.stringify({ closed: false, echo: args.code }) }]
      })
    }
  });
  vm.runInContext(source, context);
  context.runParkedDelivery = async (config, delivery) => {
    if (delivery.requestId === "request-a") {
      await new Promise(resolve => setImmediate(resolve));
      await new Promise(resolve => setImmediate(resolve));
      order.push("A");
      return {
        result: {
          ok: true, observation: "published", request_id: "request-a",
          worker_conversation_id: "worker-a", worker_slot: "slot-a",
          trace: [{ kind: "send_attempt" }]
        }
      };
    }
    order.push("B");
    return {
      result: {
        ok: true, observation: "published", request_id: "request-b",
        worker_conversation_id: "worker-b", worker_slot: "slot-b",
        trace: [{ kind: "send_attempt" }]
      }
    };
  };
  const started = Date.now();
  const result = await context.runParkedWorkerPool({
    sessionId: "sess",
    workers: [
      { slot: "slot-a", conversation_id: "worker-a" },
      { slot: "slot-b", conversation_id: "worker-b" }
    ]
  }, [
    { sessionId: "sess", operation: "run", callId: "a".repeat(32), requestId: "request-a" },
    { sessionId: "sess", operation: "run", callId: "b".repeat(32), requestId: "request-b" }
  ]);
  assert.equal(result.ok, true);
  assert.equal(result.capacity, 2);
  assert.equal(result.listener_count, 1);
  assert.deepEqual(order, ["B", "A"]);
  assert.equal(result.jobs.length, 2);
  assert.ok(Date.now() - started < 1000);
});

test("one-worker pool scheduler keeps capacity one", async () => {
  const context = vm.createContext({
    Date: { now: () => 1 },
    setTimeout: fn => fn(),
    tools: {
      mcp__node_repl__js: async args => ({
        isError: false,
        content: [{ type: "text", text: JSON.stringify({ closed: false, echo: args.code }) }]
      })
    }
  });
  vm.runInContext(source, context);
  context.runParkedDelivery = async (config, delivery) => {
    assert.equal(config.maxConcurrentRequests, 1);
    return {
      result: {
        ok: true, observation: "published", request_id: delivery.requestId,
        worker_conversation_id: "worker-a", worker_slot: "slot-a",
        trace: [{ kind: "send_attempt" }]
      }
    };
  };
  const result = await context.runParkedWorkerPool({
    sessionId: "sess",
    workers: [{ slot: "slot-a", conversation_id: "worker-a" }]
  }, [
    { sessionId: "sess", operation: "run", callId: "a".repeat(32), requestId: "request-a" }
  ]);
  assert.equal(result.ok, true);
  assert.equal(result.capacity, 1);
  assert.equal(result.listener_count, 1);
  await assert.rejects(() => context.runParkedWorkerPool({
    sessionId: "sess",
    workers: [{ slot: "slot-a", conversation_id: "worker-a" }]
  }, [
    { sessionId: "sess", operation: "run", callId: "a".repeat(32), requestId: "request-a" },
    { sessionId: "sess", operation: "run", callId: "b".repeat(32), requestId: "request-b" }
  ]), /one or two workers and bounded deliveries/);
});

test("pool scheduler rejects a third overlapping delivery in one batch", async () => {
  const context = vm.createContext({ Date: { now: () => 1 }, setTimeout: fn => fn() });
  vm.runInContext(source, context);
  await assert.rejects(() => context.runParkedWorkerPool({
    sessionId: "sess",
    workers: [
      { slot: "slot-a", conversation_id: "worker-a" },
      { slot: "slot-b", conversation_id: "worker-b" }
    ]
  }, [
    { sessionId: "sess", operation: "run", callId: "a".repeat(32), requestId: "request-a" },
    { sessionId: "sess", operation: "run", callId: "b".repeat(32), requestId: "request-b" },
    { sessionId: "sess", operation: "run", callId: "c".repeat(32), requestId: "request-c" }
  ]), /one or two workers and bounded deliveries/);
});
