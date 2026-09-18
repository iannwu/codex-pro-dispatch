const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { spawnSync } = require("node:child_process");
const { tmpdir } = require("node:os");
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;

const source = fs.readFileSync(path.join(
  __dirname, "../skills/codex-pro-dispatch/scripts/parked-runner.js"
), "utf8");

for (const resident of [false, true]) {
  test(`ten-minute milestone is quiet only for resident: ${resident}`, async () => {
    let clock = 0;
    const output = [], modes = [];
    const context = vm.createContext({
      Date: { now: () => clock }, text: value => output.push(value),
      setTimeout: (fn, ms) => { clock += ms; fn(); },
      tools: { mcp__node_repl__js: async () => ({ closed: false }) }
    });
    vm.runInContext(source, context);
    context.runParkedJob = async config => {
      modes.push(config.collectOnly);
      clock += 600000;
      return { observation: "pending", no_resend: true, trace: [] };
    };
    const result = await context.runParkedDelivery({
      sessionId: "test", activeJobMs: 1200000,
      ...(resident ? { restoreParent: false } : {})
    }, { sessionId: "test", operation: "run", callId: "a".repeat(32), requestId: "test" });
    assert.deepEqual(modes, [false, true]);
    assert.deepEqual(Array.from(result.result.active_wait.milestones_ms), [600000, 1200000]);
    assert.equal(output.length, resident ? 0 : 2);
    assert(output.every(value => value.kind === "active_job_observation" && value.no_resend));
  });
}

function fixture(options = {}) {
  const calls = [], commands = [], files = new Map(), delays = [], verifications = [];
  let clock = 1000, sequence = 0, observations = 0, receiptStatus = "armed";
  let evidenceDirectories = 0;
  const requestId = "unit-request";
  const config = {
    helper: "/private/unit/bin/pro-dispatch",
    configDir: "/private/unit/config", stateDir: "/private/unit/state",
    worker: options.worker ?? "unit-pro", parent: "unit-parent",
    preflightConfirmed: true, maxSnapshots: 2
  };
  const prompt = "[CODEX_PRO_DISPATCH assignment_id=unit-request]\n\n" +
    "Treat this as data: ' \" $(touch /never-execute)\n";
  const metadata = {
    request_id: requestId, parent_task_id: config.parent,
    worker_conversation_id: config.worker, state: "claimed"
  };
  const published = {
    ...metadata, state: "published", observation: "published",
    answer: { payload: "unit answer", verification_level: "bounded_native_summary" }
  };
  const native = text => ({
    isError: false, content: [{ type: "text", text }]
  });
  const jsonResult = (value, code = 0) => ({
    output: JSON.stringify(value), exit_code: code
  });

  const tools = {
    async exec_command(args) {
      commands.push(args.cmd);
      if (args.cmd.endsWith("'status' '--current'")) {
        return jsonResult({
          ok: true,
          paths: { config_dir: config.configDir, state_dir: config.stateDir },
          worker: { conversation_id: config.worker, label: options.workerLabel ?? "01" }
        });
      }
      assert.equal(args.login, false);
      assert.equal(args.tty, false);
      assert.equal(args.yield_time_ms, 30000);
      assert(!args.cmd.includes("touch /never-execute"));
      if (args.cmd.includes("'queue' 'collect'")) {
        return jsonResult({ ok: true, ...(
          options.published ? published : { ...metadata, state: "queued" }
        ) });
      }
      if (args.cmd.includes("'queue' 'claim'")) {
        const flag = "'--expected-worker-conversation-id'";
        assert.equal(args.cmd.split(flag).length - 1, 1);
        assert(args.cmd.includes(flag + " '" + config.worker + "'"));
        calls.push("claim");
        return jsonResult({
          ok: true, ...metadata, assignment_id: requestId,
          action: options.collectOnly ? "collect_only" : "arm_then_send_once",
          wrapped_prompt: options.collectOnly ? undefined : prompt
        });
      }
      if (args.cmd.includes("'arm'")) {
        calls.push("arm");
        return jsonResult({ ok: true, assignment: {
          assignment_id: requestId, parent_task_id: config.parent,
          worker_conversation_id: config.worker,
          status: "armed", no_resend: true
        } });
      }
      if (args.cmd.includes("'queue' 'observe'")) {
        const match = args.cmd.match(/'--native-read-file' '([^']+)'/);
        if (!match) {
          if (options.staged) {
            receiptStatus = "complete";
            return jsonResult({ ok: true, ...published });
          }
          return jsonResult({
            ok: false, error_type: "StateError",
            error: "No native snapshot or staged history"
          }, 4);
        }
        assert(files.has(match[1]), "observer must receive preserved raw history");
        const document = JSON.parse(files.get(match[1]));
        assert.equal(document.thread.id, config.worker);
        observations++;
        if (options.alwaysPending || observations === 1 && !options.collectOnly) {
          return jsonResult({
            ok: true, ...metadata, observation: "pending", no_resend: true
          });
        }
        receiptStatus = "complete";
        return jsonResult({ ok: true, ...published });
      }
      if (args.cmd.includes("'status'")) {
        return jsonResult({ ok: true, assignment: { status: receiptStatus } });
      }
      if (args.cmd.includes("'indeterminate'")) {
        calls.push("indeterminate");
        return jsonResult({ ok: true });
      }
      throw Error("Unexpected helper command: " + args.cmd);
    },
    async write_stdin() { throw Error("Unexpected process continuation"); },
    async mcp__node_repl__js(args) {
      assert.equal(args.timeout_ms, 30000);
      if (args.code.includes("mkdtemp(")) {
        return native(JSON.stringify({ directory: "/private/unit/evidence-" + (++evidenceDirectories) }));
      }
      // Evidence code runs against an in-memory file system with injectable
      // faults, so durability claims depend on what the code actually does.
      const verifying = args.title === "Verify native evidence";
      const ambiguous = !verifying && options.evidenceAck && !options.evidenceAckUsed &&
        (options.evidenceAckAfterSend ? calls.includes("send") : true);
      const fault = verifying ? options.verifySyncFails :
        ambiguous ? (options.evidenceDropped ? "write" : options.evidenceCorrupted ? "corrupt" : null) : null;
      let opened = null;
      const store = { async open(target, flags) {
        const directory = !target.endsWith(".json");
        opened ??= target;
        if (flags === "wx") {
          assert(!files.has(target), "evidence must not be overwritten");
          files.set(target, null);
        } else if (!directory && !files.has(target)) throw Error("ENOENT: " + target);
        return {
          async writeFile(data) {
            if (fault === "write") throw Error("EIO write");
            files.set(target, fault === "corrupt" ? data.slice(0, -1) : data);
          },
          async readFile() { return files.get(target); },
          async sync() { if (fault === (directory ? "parent" : "file")) throw Error("EIO fsync " + target); },
          async close() {}
        };
      } };
      const lines = [];
      let failed = null;
      try {
        await new AsyncFunction("store", "console", args.code.replace('await import("node:fs/promises")', "store"))(
          store, { log: value => lines.push(String(value)) });
      } catch (error) {
        if (fault === "write") files.delete([...files.keys()].find(key => files.get(key) === null));
        failed = error;
      }
      if (verifying) verifications.push(opened);
      if (failed && !ambiguous) return { isError: true, content: [{ type: "text", text: failed.message }] };
      // An ambiguous acknowledgment may follow a real write (persisted) or a
      // lost one (dropped); the runner must tell them apart by verification.
      if (ambiguous) { options.evidenceAckUsed = true; return options.evidenceAck; }
      return native(lines.join("\n"));
    },
    async mcp__codex_app__read_thread(args) {
      calls.push("read");
      assert.deepEqual(JSON.parse(JSON.stringify(args)), {
        threadId: config.worker, turnLimit: 2, maxOutputCharsPerItem: 20000
      });
      if (options.readEnvelope !== undefined) return options.readEnvelope;
      return native(JSON.stringify({
        schemaVersion: 1,
        thread: {
          kind: "chatgpt",
          id: options.wrongWorker ? "wrong-pro" : config.worker,
          status: { type: "idle" }
        },
        turns: [], sequence: ++sequence, text: prompt
      }));
    },
    async mcp__codex_app__send_message_to_thread(args) {
      calls.push("send");
      assert.equal(args.threadId, config.worker);
      assert.equal(args.prompt, prompt);
      assert.deepEqual(Object.keys(args).sort(), ["prompt", "threadId"]);
      if (options.sendError) {
        return { isError: true, content: [{ type: "text", text: "unit transport error" }] };
      }
      if (options.sendAck !== undefined) return options.sendAck;
      return native(JSON.stringify({ threadId: config.worker }));
    },
    async mcp__codex_app__navigate_to_codex_page(args) {
      calls.push("navigate");
      assert.equal(args.threadId, config.parent);
      return options.navigationError ? { isError: true } :
        native(JSON.stringify({ threadId: config.parent }));
    }
  };
  const context = {
    tools, Date: { now: () => clock++ },
    setTimeout(callback, milliseconds) {
      delays.push(milliseconds);
      clock += milliseconds;
      queueMicrotask(callback);
    }
  };
  vm.runInNewContext(source, context, { filename: "parked-runner.js" });
  return {
    config, calls, commands, files, delays, verifications, tools,
    run: id => context.runParkedJob(config, id || requestId)
  };
}

test("fresh job: one arm/send, raw snapshots, pending then publication", async () => {
  const f = fixture(), result = await f.run();
  assert.equal(result.observation, "published");
  assert.deepEqual(f.calls, ["claim", "read", "arm", "send", "read", "read", "navigate"]);
  assert.deepEqual(f.delays, [10000]);
  assert.equal(result.restoration.status, "native_navigation_returned");
  assert.equal(result.restoration.foreground_verified, false);
  assert.equal(result.pending_helper_session, null);
});

for (const options of [{}, {alwaysPending:true}, {sendError:true}, {collectOnly:true}])
test("background policy never navigates: " + JSON.stringify(options), async () => {
  const f = fixture(options);
  f.config.restoreParent = false;
  const result = await f.run();
  assert(!f.calls.includes("navigate"));
  assert.equal(result.restoration.status, "not_requested");
  assert.equal(result.restoration.foreground_verified, false);
});

for (const value of [null, 0, "false", {}])
test("invalid navigation policy fails before tools: " + JSON.stringify(value), async () => {
  const f = fixture();
  f.config.restoreParent = value;
  await assert.rejects(f.run(), /Invalid trusted/);
  assert.deepEqual(f.commands, []);
});

test("post-arm recovery never arms or sends", async () => {
  const f = fixture({ collectOnly: true }), result = await f.run();
  assert.equal(result.observation, "published");
  assert.deepEqual(f.calls, ["claim", "read", "navigate"]);
});

test("staged recovery uses stored history without a new native read", async () => {
  const f = fixture({ collectOnly: true, staged: true }), result = await f.run();
  assert.equal(result.observation, "published");
  assert.deepEqual(f.calls, ["claim"]);
});

test("already published collection performs no native work", async () => {
  const f = fixture({ published: true }), result = await f.run();
  assert.equal(result.state, "published");
  assert.deepEqual(f.calls, []);
  assert.equal(result.restoration.status, "not_requested");
});

test("send error preserves uncertainty and never attempts a second send", async () => {
  const f = fixture({ sendError: true }), result = await f.run();
  assert.equal(result.observation, "blocked");
  assert.equal(result.no_resend, true);
  assert.deepEqual(f.calls, ["claim", "read", "arm", "send", "indeterminate", "navigate"]);
});

test("observation slice ends pending without cancellation or resend", async () => {
  const f = fixture({ alwaysPending: true }), result = await f.run();
  assert.equal(result.observation, "pending");
  assert.equal(f.calls.filter(value => value === "send").length, 1);
  assert(!f.calls.includes("indeterminate"));
  assert.deepEqual(f.delays, [10000]);
});

test("navigation failure does not discard the published answer", async () => {
  const f = fixture({ navigationError: true }), result = await f.run();
  assert.equal(result.observation, "published");
  assert.equal(result.answer.payload, "unit answer");
  assert.equal(result.restoration.status, "failed_or_unverified");
});

test("wrong native target prevents arm and send", async () => {
  const f = fixture({ wrongWorker: true }), result = await f.run();
  assert.equal(result.observation, "blocked");
  assert(!f.calls.includes("arm"));
  assert(!f.calls.includes("send"));
});

test("invalid request identity is rejected before tool execution", async () => {
  const f = fixture();
  await assert.rejects(f.run("bad/id"), /Invalid trusted/);
  assert.deepEqual(f.commands, []);
});

test("replacement worker IDs, not labels, govern a fresh runner", async () => {
  for (const [worker, workerLabel] of [
    ["stable-new-pro-01", "pro-dispatch 01"],
    ["stable-new-pro-02", "pro-dispatch 01"],
    ["stable-new-pro-01", "renamed consultation"]
  ]) {
    const f = fixture({ worker, workerLabel });
    const result = await f.run();
    assert.equal(result.observation, "published");
    assert.equal(f.commands.filter(cmd =>
      cmd.includes("'queue' 'claim'")).length, 1);
    assert.equal(f.calls.filter(value => value === "send").length, 1);
    assert(f.commands.some(cmd => cmd.includes("'--expected-worker-conversation-id' '" + worker + "'")));
  }
});

test("recovery keeps the session worker and never repeats arm or send", async () => {
  const options = { worker: "stable-new-pro", alwaysPending: true };
  const f = fixture(options);
  const first = await f.run();
  assert.equal(first.observation, "pending");
  const preserved = new Map(f.files);
  options.collectOnly = true;
  options.alwaysPending = false;
  options.workerLabel = "renamed 02";
  const second = await f.run();
  assert.equal(second.observation, "published");
  assert.notEqual(second.evidence_directory, first.evidence_directory);
  for (const [file, raw] of preserved) assert.equal(f.files.get(file), raw);
  const nativeCalls = f.calls.slice();
  options.published = true;
  assert.equal((await f.run()).state, "published");
  assert.deepEqual(f.calls, nativeCalls);
  assert.equal(f.calls.filter(value => value === "send").length, 1);
  assert.equal(f.calls.filter(value => value === "arm").length, 1);
  const claims = f.commands.filter(cmd => cmd.includes("'queue' 'claim'"));
  assert.equal(claims.length, 2);
  for (const cmd of claims) assert(cmd.includes("'--expected-worker-conversation-id' 'stable-new-pro'"));
});

test("a mismatched claimed worker is rejected before native work", async () => {
  const f = fixture({ worker: "stable-new-pro" });
  const original = f.tools.exec_command;
  f.tools.exec_command = async args => {
    const response = await original(args);
    if (args.cmd.includes("'queue' 'claim'")) {
      const value = JSON.parse(response.output);
      value.worker_conversation_id = "other-stable-pro";
      return { ...response, output: JSON.stringify(value) };
    }
    return response;
  };
  const result = await f.run();
  assert.equal(result.failed_phase, "claim");
  assert.equal(result.error, "Broker identity mismatch");
  assert.equal(result.no_resend, false);
  for (const name of ["read", "arm", "send"]) assert(!f.calls.includes(name));
});

// raw_before_decode: an undecodable history envelope is preserved whole,
// together with the exception's name/message/stack, before any refusal.
for (const [label, envelope] of [
  ["two text blocks", { isError: false, content: [
    { type: "text", text: "{}" }, { type: "text", text: "{}" }] }],
  ["image block", { isError: false, content: [{ type: "image", data: "AA==" }] }],
  ["empty content", { isError: false, content: [] }],
  ["null result", null]
])
test("undecodable native history is preserved before refusal: " + label, async () => {
  const f = fixture({ readEnvelope: envelope }), result = await f.run();
  assert.equal(result.observation, "blocked");
  assert.equal(result.failed_phase, "pre-send-read");
  assert.equal(result.no_resend, false);
  assert(!f.calls.includes("arm") && !f.calls.includes("send"));
  const record = JSON.parse(f.files.get(result.error_evidence));
  assert.deepEqual(record.error.detail, envelope);
  assert.equal(record.error.name, "Error");
  assert.equal(record.error.message, "Unsupported or failed native tool result");
  assert.equal(typeof record.error.stack, "string");
  assert.equal(record.worker_conversation_id, f.config.worker);
  // The whole envelope and its operation identity were preserved first.
  const preserved = JSON.parse(f.files.get(record.envelope_evidence));
  assert.equal(preserved.operation, "read_thread");
  assert.equal(preserved.request_id, "unit-request");
  assert.deepEqual(preserved.result, envelope);
  assert(record.envelope_evidence < result.error_evidence);
});

test("successful reads and sends preserve their original envelopes with operation identity", async () => {
  const f = fixture(), result = await f.run();
  assert.equal(result.observation, "published");
  const records = [...f.files.values()].map(raw => JSON.parse(raw));
  const reads = records.filter(v => v.operation === "read_thread");
  const sends = records.filter(v => v.operation === "send_message_to_thread");
  assert.equal(reads.length, f.calls.filter(v => v === "read").length);
  assert.equal(sends.length, 1);
  assert.deepEqual(sends[0].result, { isError: false, content: [{ type: "text",
    text: JSON.stringify({ threadId: "unit-pro" }) }] });
  for (const read of reads) assert.equal(read.result.content[0].type, "text");
  assert.equal(f.verifications.length, 0);
});

// official_success_variants: documented acknowledgments complete one request;
// a foreign thread or unsupported carrier is preserved and refused, no resend.
const textAck = value => ({ isError: false, content: [{ type: "text", text: JSON.stringify(value) }] });
for (const [label, sendAck, outcome] of [
  ["incident threadId", textAck({ threadId: "unit-pro" }), "published"],
  ["empty object", { content: [{ type: "text", text: "{}" }] }, "published"],
  ["foreign thread", textAck({ threadId: "other-pro" }), "blocked"],
  ["threadId plus extra field", textAck({ threadId: "unit-pro", ok: true }), "blocked"],
  ["arbitrary object without identity", textAck({ ok: false, error: "denied" }), "blocked"],
  ["array", textAck([]), "blocked"],
  ["no content", { isError: false, content: [] }, "blocked"],
  ["two text blocks", { isError: false, content: [{ type: "text", text: "{}" }, { type: "text", text: "{}" }] }, "blocked"],
  ["non-object text", { isError: false, content: [{ type: "text", text: "sent" }] }, "blocked"]
])
test("send acknowledgment variant: " + label, async () => {
  const f = fixture({ sendAck }), result = await f.run();
  assert.equal(result.observation, outcome);
  assert.equal(f.calls.filter(value => value === "send").length, 1);
  if (outcome === "blocked") {
    assert.equal(result.failed_phase, "send");
    assert.equal(result.no_resend, true);
    assert(f.calls.includes("indeterminate"));
    const record = JSON.parse(f.files.get(result.error_evidence));
    assert.deepEqual(record.error.detail, sendAck);
    assert.equal(record.error.message, "Unsupported native send acknowledgment; never resend");
  }
});

// Ambiguous evidence acknowledgments are verified against the private file's
// exact bytes; a persisted write continues, an unconfirmed one stops.
for (const evidenceAck of [{ isError: false, content: [] }, { isError: true, content: [{ type: "text", text: "EIO" }] }])
test("ambiguous evidence acknowledgment is verified, never rewritten: " + JSON.stringify(evidenceAck), async () => {
  const f = fixture({ evidenceAck }), result = await f.run();
  assert.equal(result.observation, "published");
  assert.equal(f.verifications.length, 1);
  assert.deepEqual(Array.from(result.trace.filter(e => e.kind === "evidence_verified"), e => e.path), f.verifications);
  assert.equal(f.calls.filter(value => value === "send").length, 1);
});

test("persisted evidence whose bytes differ is never trusted or rewritten", async () => {
  const f = fixture({ evidenceAck: { isError: false, content: [] }, evidenceCorrupted: true });
  const result = await f.run();
  assert.equal(result.observation, "blocked");
  assert.equal(result.failed_phase, "pre-send-read");
  assert.equal(result.error, "Evidence persistence unconfirmed");
  assert(!f.calls.includes("arm") && !f.calls.includes("send"));
  assert.equal(f.verifications.length, 1);
  const record = JSON.parse(f.files.get(result.error_evidence));
  assert.equal(record.error.detail.verification.content[0].text, "Evidence bytes differ");
  // The truncated file is retained untouched as evidence of the fault.
  assert.throws(() => JSON.parse(f.files.get(f.verifications[0])));
});

test("unconfirmed pre-arm evidence persistence stops before arm and send", async () => {
  const f = fixture({ evidenceAck: { isError: true, content: [{ type: "text", text: "EIO" }] }, evidenceDropped: true });
  const result = await f.run();
  assert.equal(result.observation, "blocked");
  assert.equal(result.failed_phase, "pre-send-read");
  assert.equal(result.error, "Evidence persistence unconfirmed");
  assert.equal(result.no_resend, false);
  assert(!f.calls.includes("arm") && !f.calls.includes("send"));
  assert.equal(f.verifications.length, 1);
  const record = JSON.parse(f.files.get(result.error_evidence));
  assert.equal(record.error.detail.acknowledgment.isError, true);
  assert.match(record.error.detail.verification.content[0].text, /^ENOENT/);
});

// Readable bytes alone are not durability: the verification must also sync
// the file and its directory, and a failed sync stops progress.
for (const verifySyncFails of ["file", "parent"])
test("verified bytes with a failed " + verifySyncFails + " sync stop before arm", async () => {
  const f = fixture({ evidenceAck: { isError: false, content: [] }, verifySyncFails });
  const result = await f.run();
  assert.equal(result.observation, "blocked");
  assert.equal(result.failed_phase, "pre-send-read");
  assert.equal(result.error, "Evidence persistence unconfirmed");
  assert(!f.calls.includes("arm") && !f.calls.includes("send"));
  assert.equal(f.verifications.length, 1);
  assert.equal(f.files.get(f.verifications[0]).length > 0, true);
  const record = JSON.parse(f.files.get(result.error_evidence));
  assert.match(record.error.detail.verification.content[0].text, /EIO fsync/);
});

test("verified bytes with a failed sync after send enter post-arm failure handling", async () => {
  const f = fixture({ evidenceAck: { isError: false, content: [] }, evidenceAckAfterSend: true, verifySyncFails: "parent" });
  const result = await f.run();
  assert.equal(result.observation, "blocked");
  assert.equal(result.failed_phase, "send");
  assert.equal(result.no_resend, true);
  assert.deepEqual(f.calls, ["claim", "read", "arm", "send", "indeterminate", "navigate"]);
});

test("unconfirmed send-evidence persistence enters post-arm failure handling", async () => {
  const f = fixture({ evidenceAck: { isError: false, content: [] }, evidenceDropped: true, evidenceAckAfterSend: true });
  const result = await f.run();
  assert.equal(result.observation, "blocked");
  assert.equal(result.failed_phase, "send");
  assert.equal(result.error, "Evidence persistence unconfirmed");
  assert.equal(result.no_resend, true);
  assert.deepEqual(f.calls, ["claim", "read", "arm", "send", "indeterminate", "navigate"]);
});

test("cross-realm and non-Error failures serialize structurally", async () => {
  const f = fixture();
  const original = f.tools.mcp__codex_app__read_thread;
  const foreign = vm.runInNewContext('Object.assign(new Error("foreign realm"), { cause: new TypeError("inner") })');
  assert(!(foreign instanceof Error));
  f.tools.mcp__codex_app__read_thread = async () => { throw foreign; };
  let result = await f.run();
  let record = JSON.parse(f.files.get(result.error_evidence));
  assert.equal(record.error.message, "foreign realm");
  assert.equal(typeof record.error.stack, "string");
  assert.equal(record.error.cause.message, "inner");
  f.tools.mcp__codex_app__read_thread = async () => { throw "plain string"; };
  result = await f.run();
  record = JSON.parse(f.files.get(result.error_evidence));
  assert.deepEqual(record.error, { message: "plain string" });
  f.tools.mcp__codex_app__read_thread = original;
});

test("real CLI fences a replaced session and completes only on its new stable worker", async t => {
  const root = fs.realpathSync(path.join(__dirname, ".."));
  const home = fs.realpathSync(fs.mkdtempSync(path.join(tmpdir(), "parked-cas-")));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const authority = path.join(home, "authority"), helper = path.join(root, "bin/pro-dispatch");
  const env = { ...process.env, CODEX_PRO_DISPATCH_HOME: authority,
    PYTHONPATH: path.join(root, "src"), PYTHONDONTWRITEBYTECODE: "1" };
  const id = "unit-request", nextWorker = "unit-replacement-pro";
  const quote = value => "'" + value.replace(/'/g, "'\\''") + "'";
  function execute(file, args, input = "") {
    const r = spawnSync(file, args, { env, input, encoding: "utf8", timeout: 15000 });
    assert.ifError(r.error);
    assert(Number.isInteger(r.status), "Synthetic CLI did not exit");
    return { exit_code: r.status, output: r.status === 0 ? r.stdout : r.stderr };
  }
  function cli(args, input) {
    const r = execute("python3", [helper, ...args], input);
    assert.equal(r.exit_code, 0, r.output);
    return JSON.parse(r.output);
  }
  function image() {
    const result = {};
    function visit(dir) {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const file = path.join(dir, entry.name);
        if (entry.isDirectory()) visit(file);
        else {
          assert(entry.isFile());
          result[path.relative(authority, file)] = fs.readFileSync(file).toString("hex");
        }
      }
    }
    visit(authority);
    return result;
  }
  cli(["worker", "set", "--conversation-id", "unit-pro", "--label", "01",
    "--confirm-pro", "--native-controls-confirmed"]);
  cli(["queue", "submit", "--request-id", id, "--prompt-file", "-",
    "--client-session-id", "unit-client"], "Synthetic canonical request\n");
  const queued = fs.readFileSync(path.join(authority, "state/queue", id + ".json"));
  let replaced = false, afterReplacement;
  function driver(worker, replaceBeforeClaim = false) {
    const f = fixture({ worker, workerLabel: "01" });
    Object.assign(f.config, { helper,
      configDir: path.join(authority, "config"), stateDir: path.join(authority, "state") });
    const evidenceDir = fs.mkdtempSync(path.join(home, "evidence-"));
    fs.chmodSync(evidenceDir, 0o700);
    let wrapped, sent;
    const native = text => ({ isError: false, content: [{ type: "text", text }] });
    f.tools.exec_command = async args => {
      assert.equal(args.login, false); assert.equal(args.tty, false);
      assert(args.cmd.startsWith("'python3' " + quote(helper) + " "));
      f.commands.push(args.cmd);
      const claim = args.cmd.includes("'queue' 'claim'");
      if (claim) {
        const flag = "'--expected-worker-conversation-id'";
        assert.equal(args.cmd.split(flag).length - 1, 1);
        assert(args.cmd.includes(flag + " " + quote(worker)));
        if (replaceBeforeClaim && !replaced) {
          cli(["worker", "set", "--conversation-id", nextWorker,
            "--expected-conversation-id", "unit-pro", "--label", "01",
            "--confirm-pro", "--native-controls-confirmed"]);
          replaced = true; afterReplacement = image();
        }
      }
      // Only the runner's trusted helper command runs, under synthetic authority.
      const result = execute("/bin/sh", ["-c", args.cmd]);
      if (claim && result.exit_code === 0) wrapped = JSON.parse(result.output).wrapped_prompt;
      return result;
    };
    const save = f.tools.mcp__node_repl__js;
    f.tools.mcp__node_repl__js = async args => {
      if (args.code.includes("mkdtemp(")) return native(JSON.stringify({ directory: evidenceDir }));
      const before = new Set(f.files.keys());
      const result = await save(args);
      const target = JSON.parse(result.content[0].text).path;
      assert.equal(path.dirname(target), evidenceDir);
      for (const [saved, bytes] of f.files) {
        if (before.has(saved)) continue;
        assert.equal(path.dirname(saved), evidenceDir);
        fs.writeFileSync(saved, bytes, { flag: "wx", mode: 0o600 });
      }
      return result;
    };
    f.tools.mcp__codex_app__read_thread = async args => {
      f.calls.push("read"); assert.equal(args.threadId, worker);
      return native(JSON.stringify({
        schemaVersion: 1, thread: { id: worker, kind: "chatgpt", status: { type: "idle" } },
        turns: sent === undefined ? [] : [{ id: "unit-turn", items: [
          { id: "unit-turn", type: "userMessage", content: [{ type: "text", text: sent }] },
          { id: "unit-answer", type: "agentMessage", text:
            `[CODEX_PRO_DISPATCH_RESULT assignment_id=${id}]\ncanonical answer\n` +
            `[CODEX_PRO_DISPATCH_END assignment_id=${id}]` }
        ] }]
      }));
    };
    f.tools.mcp__codex_app__send_message_to_thread = async args => {
      f.calls.push("send");
      assert.equal(sent, undefined); assert.equal(typeof wrapped, "string");
      assert.equal(args.threadId, worker); assert.equal(args.prompt, wrapped);
      const receipt = cli(["status", id]).assignment;
      assert.equal(receipt.status, "armed"); assert.equal(receipt.no_resend, true);
      sent = args.prompt;
      return native(JSON.stringify({ threadId: worker }));
    };
    return f;
  }
  const stale = driver("unit-pro", true), blocked = await stale.run();
  assert.equal(replaced, true);
  assert.equal(blocked.failed_phase, "claim"); assert.equal(blocked.no_resend, false);
  assert.equal(blocked.error, "Helper rejected operation");
  assert.deepEqual(image(), afterReplacement);
  assert.deepEqual(fs.readFileSync(path.join(authority, "state/queue", id + ".json")), queued);
  assert(!fs.existsSync(path.join(authority, "state/assignments", id + ".json")));
  assert(!stale.calls.includes("read")); assert(!stale.calls.includes("send"));
  assert(!stale.commands.some(cmd => cmd.includes("'arm'")));
  const fresh = driver(nextWorker), result = await fresh.run();
  assert.equal(result.observation, "published");
  const receipt = cli(["status", id]).assignment;
  assert.equal(receipt.worker_conversation_id, nextWorker);
  assert.equal(receipt.submission_count, 1); assert.equal(receipt.no_resend, true);
  const first = cli(["queue", "collect", id]), second = cli(["queue", "collect", id]);
  assert.deepEqual(first.answer, JSON.parse(JSON.stringify(result.answer))); assert.deepEqual(second.answer, first.answer);
  const calls = fresh.calls.slice();
  assert.equal((await fresh.run()).state, "published");
  assert.deepEqual(fresh.calls, calls);
  assert.equal(fresh.calls.filter(value => value === "send").length, 1);
  assert.equal(fresh.commands.filter(cmd => cmd.includes("'arm'")).length, 1);
  assert.equal(fresh.commands.filter(cmd => cmd.includes("'queue' 'claim'")).length, 1);
});
