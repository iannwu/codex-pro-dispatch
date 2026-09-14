import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as vm from "node:vm";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { openSession } from "../skills/codex-pro-dispatch/scripts/parked-socket.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const helper = root + "bin/pro-dispatch";
const client = root + "skills/codex-pro-dispatch/scripts/parked-client.mjs";
const source = await fs.readFile(
  root + "skills/codex-pro-dispatch/scripts/parked-runner.js", "utf8"
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const plain = value => JSON.parse(JSON.stringify(value));

function execute(file, args, env) {
  return new Promise(resolve => {
    const child = execFile(file, args, {
      env, timeout: 35000, maxBuffer: 8 * 1024 * 1024
    }, (error, stdout, stderr) => resolve({
      stdout, stderr, output: stdout + stderr,
      exit_code: error ? (Number.isInteger(error.code) ? error.code : 1) : 0
    }));
    child.stdin.end();
  });
}

async function fixture(t, options = {}) {
  const created = await fs.mkdtemp(os.tmpdir() + "/parked-integration-");
  const directory = await fs.realpath(created);
  const home = directory + "/authority";
  const sessionDir = directory + "/session";
  const env = { ...process.env, CODEX_PRO_DISPATCH_HOME: home };
  const native = { reads: [], sends: [], navigations: [] };
  const commands = [], replCalls = [], results = [];
  const f = { ready: true, losePublicationReply: false };
  let prompt = null, yielded = false, held = null;

  async function cli(args) {
    const r = await execute("python3", [helper, ...args], env);
    assert.equal(r.exit_code, 0, r.stderr);
    return JSON.parse(r.stdout);
  }

  await fs.mkdir(sessionDir, { mode: 448 });
  await cli(["worker", "set", "--conversation-id", "fixture-pro",
    "--confirm-pro", "--native-controls-confirmed"]);
  if (options.largeHistory) {
    const history = home + "/state/assignments";
    await fs.mkdir(history, { recursive: true, mode: 448 });
    for (let i = 0; i < 64; i++) {
      const id = "historical-" + i;
      await fs.writeFile(history + "/" + id + ".json", JSON.stringify({
        schema_version: 1, assignment_id: id, status: "complete",
        created_at: "2026-01-01T00:00:00Z", fixture_padding: "x".repeat(4096)
      }), { flag: "wx", mode: 384 });
    }
  }
  const socket = await openSession(sessionDir, {
    helper, configDir: home + "/config", stateDir: home + "/state",
    worker: "fixture-pro", parent: "fixture-parent",
    leaseMs: 60000, idleMs: 10000, replyMs: 20000
  });
  const config = {
    ...socket.config, preflightConfirmed: true,
    maxSnapshots: 1, observationMs: 10000
  };
  const promptFile = directory + "/prompt.txt";
  await fs.writeFile(promptFile,
    "Review only. Literal data: NEVER_EXECUTE_INPUT ' \" $(false)\n",
    { mode: 384 });

  const textResult = text => ({
    isError: false, content: [{ type: "text", text }]
  });

  const tools = {
    async exec_command(args) {
      assert.equal(args.login, false);
      assert.equal(args.tty, false);
      assert.equal(args.yield_time_ms, 30000);
      assert(!args.cmd.includes("NEVER_EXECUTE_INPUT"));
      const entry = { command: args.cmd };
      commands.push(entry);
      // Execute the actual quoted helper command. No fabricated CLI JSON.
      const pending = execute("/bin/sh", ["-c", "exec " + args.cmd], env)
        .then(r => {
          Object.assign(entry, r);
          if (options.largeHistory)
            assert(Buffer.byteLength(r.output, "utf8") <= 8192,
              "Native helper output exceeded the bounded fixture transport");
          if (f.losePublicationReply &&
              args.cmd.includes("'queue' 'observe'") &&
              args.cmd.includes("'--native-read-file'") &&
              r.exit_code === 0 &&
              JSON.parse(r.stdout).observation === "published") {
            f.losePublicationReply = false;
            throw Error("UNIT: publication succeeded but its tool reply was lost");
          }
          return r;
        });
      if (options.yieldOnce && !yielded) {
        yielded = true;
        held = pending;
        held.catch(() => {});
        return { output: "", session_id: 71, wall_time_seconds: 0 };
      }
      return await pending;
    },
    async write_stdin(args) {
      assert.equal(args.session_id, 71);
      assert.equal(args.chars, "");
      assert.equal(args.yield_time_ms, 30000);
      assert(held);
      const pending = held;
      held = null;
      return await pending;
    },
    async mcp__node_repl__js(args) {
      assert.equal(args.timeout_ms, 30000);
      replCalls.push({ title: args.title, code: args.code });
      const output = [];
      // Run the exact generated evidence/finish code against real fs and
      // the real socket object. Only REPL host context/console are supplied.
      await new AsyncFunction("nodeRepl", "console", "parkedSocket", args.code)(
        { tmpDir: directory },
        { log: (...values) => output.push(values.map(String).join(" ")) },
        socket
      );
      return textResult(output.join("\n"));
    },
    async mcp__codex_app__read_thread(args) {
      assert.deepEqual(plain(args), {
        threadId: "fixture-pro", turnLimit: 2, maxOutputCharsPerItem: 20000
      });
      const document = {
        schemaVersion: 1,
        thread: {
          id: "fixture-pro", kind: "chatgpt",
          status: { type: prompt && !f.ready ? "running" : "idle" }
        },
        turns: prompt ? [{
          id: "fixture-turn",
          items: [
            {
              id: "fixture-turn", type: "userMessage",
              content: [{ type: "text", text: prompt }]
            },
            {
              id: "fixture-answer", type: "agentMessage",
              text: "[CODEX_PRO_DISPATCH_RESULT assignment_id=fixture-A]\n" +
                "integration answer\n" +
                "[CODEX_PRO_DISPATCH_END assignment_id=fixture-A]"
            }
          ]
        }] : []
      };
      // Deliberate formatting and final LF test unedited evidence preservation.
      const raw = JSON.stringify(document, null, 2) + "\n";
      native.reads.push(raw);
      if (options.pendingOnce && prompt && !f.ready) f.ready = true;
      return textResult(raw);
    },
    async mcp__codex_app__send_message_to_thread(args) {
      assert.equal(args.threadId, "fixture-pro");
      assert.deepEqual(Object.keys(args).sort(), ["prompt", "threadId"]);
      const state = await cli(["status", "fixture-A"]);
      assert.equal(state.assignment.status, "armed");
      assert.equal(state.assignment.no_resend, true);
      assert.equal(state.assignment.submission_count, 0);
      assert.equal(state.assignment.parent_task_id, "fixture-parent");
      prompt = args.prompt;
      native.sends.push(plain(args));
      return options.sendError
        ? { isError: true, content: [{ type: "text", text: "UNIT send failure" }] }
        : textResult(JSON.stringify({ threadId: "fixture-pro" }));
    },
    async mcp__codex_app__navigate_to_codex_page(args) {
      assert.equal(args.threadId, "fixture-parent");
      native.navigations.push(plain(args));
      return textResult(JSON.stringify({ threadId: args.threadId }));
    }
  };

  async function invoke(operation) {
    const args = [client, sessionDir, operation, "fixture-A"];
    if (operation === "submit") args.push(promptFile, "fixture-client");
    const r = await execute(process.execPath, args, env);
    assert.equal(r.exit_code, 0, r.stderr);
    return JSON.parse(r.stdout);
  }

  async function drive(operation = "submit") {
    const waiting = socket.receive();
    const receivingClient = invoke(operation);
    receivingClient.catch(() => {});
    const delivery = await waiting;
    assert(delivery);
    // A fresh outer isolate for each delivery, with the same persistent socket.
    const context = vm.createContext({ tools, setTimeout, clearTimeout });
    vm.runInContext(source, context, { filename: "parked-runner.js" });
    const returned = plain(await context.runParkedDelivery(config, delivery));
    const collected = await receivingClient;
    results.push({ delivery, returned, collected });
    return { returned, collected };
  }

  t.after(async () => {
    await socket.close("unit_finished");
    if (held) await held.catch(() => {});
    await fs.writeFile(directory + "/integration-evidence.json",
      JSON.stringify({
        scope: "UNIT: actual CLI/fs/socket; synthetic native tools",
        runnerSha256: createHash("sha256").update(source).digest("hex"),
        commands, replCalls, native, results
      }, null, 2), { flag: "wx", mode: 384 });
    t.diagnostic("Preserved private UNIT evidence: " + directory);
  });
  return Object.assign(f, {
    config, commands, native, drive, invoke, cli, directory,
    yielded: () => yielded
  });
}

test("actual delivery: CLI errors, evidence bytes, identity, collection", async t => {
  const f = await fixture(t, { yieldOnce: true });
  const { returned, collected } = await f.drive();
  assert(f.yielded());
  assert.equal(returned.result.observation, "published");
  assert.equal(collected.answer.payload, "integration answer");
  assert.equal(collected.answer.verification_level, "bounded_native_summary");
  assert.equal(collected.answer.generation_finality_verified, false);
  assert.equal(collected.answer.source_bytes_verified, false);
  assert.equal(collected.parent_task_id, "fixture-parent");
  assert.equal(collected.worker_conversation_id, "fixture-pro");
  assert.equal(f.native.sends.length, 1);
  assert.equal(f.native.reads.length, 2);

  const missing = f.commands.find(r =>
    r.stderr?.includes("No native snapshot or staged history"));
  assert(missing);
  assert.equal(missing.exit_code, 4);
  assert.equal(missing.stdout, "");
  assert.equal(JSON.parse(missing.stderr).error_type, "StateError");

  const status = await f.cli(["status", "fixture-A"]);
  assert.equal(status.assignment.submission_count, 1);
  assert.equal(status.assignment.native_collection.user_message_id, "fixture-turn");
  assert.equal(status.assignment.native_collection.assistant_message_id, "fixture-answer");
  const lastRaw = f.native.reads.at(-1);
  assert.equal(status.assignment.native_collection.read_sha256,
    createHash("sha256").update(lastRaw, "utf8").digest("hex"));

  const evidenceDir = returned.result.evidence_directory;
  assert.equal((await fs.stat(evidenceDir)).mode & 511, 448);
  const contents = [];
  for (const name of await fs.readdir(evidenceDir)) {
    const file = evidenceDir + "/" + name;
    assert.equal((await fs.stat(file)).mode & 511, 384);
    contents.push(await fs.readFile(file, "utf8"));
  }
  assert(contents.includes(lastRaw));
  assert.equal(returned.result.restoration.status, "native_navigation_returned");
  assert.equal(returned.result.restoration.foreground_verified, false);
  assert.equal(JSON.parse(returned.transport.content[0].text).reservedRequestId, null);

  assert.deepEqual((await f.invoke("collect")).answer, collected.answer);
  assert.deepEqual((await f.invoke("submit")).answer, collected.answer);
  assert.equal(f.native.sends.length, 1);
  await f.invoke("acknowledge");
  await f.invoke("acknowledge");
  assert.equal((await f.invoke("collect")).body_available, false);
});

test("pending then explicit actual collect-only recovery, without another send", async t => {
  const f = await fixture(t);
  f.ready = false;
  const first = await f.drive();
  assert.equal(first.returned.result.observation, "pending");
  assert.equal(first.collected.dispatch_status, "armed");
  assert.equal(first.collected.send_may_have_occurred, true);
  assert.equal((await f.invoke("submit")).wake.status, "collect_only");
  f.ready = true;
  const second = await f.drive("observe");
  assert.equal(second.returned.result.observation, "published");
  assert.equal(second.collected.answer.payload, "integration answer");
  assert.equal(f.native.sends.length, 1);
  assert.equal(f.commands.filter(r => r.command.includes("'arm'")).length, 1);
});

test("lost publication reply preserves the actual published answer", async t => {
  const f = await fixture(t);
  f.losePublicationReply = true;
  const { returned, collected } = await f.drive();
  assert.equal(returned.result.observation, "blocked");
  assert.equal(collected.state, "published");
  assert.equal(collected.answer.payload, "integration answer");
  assert.equal((await f.invoke("collect")).answer.payload, "integration answer");
  assert.equal(f.native.sends.length, 1);
  assert(!f.commands.some(r => r.command.includes("'indeterminate'")));
});

test("native send error uses real indeterminate CLI and preserved reason", async t => {
  const f = await fixture(t, { sendError: true });
  const { returned, collected } = await f.drive();
  assert.equal(returned.result.observation, "blocked");
  assert.equal(collected.dispatch_status, "indeterminate");
  assert.equal(collected.send_may_have_occurred, true);
  assert.equal(f.native.sends.length, 1);
  assert.equal(f.native.reads.length, 1);
  const reason = JSON.parse(await fs.readFile(
    returned.result.error_evidence, "utf8"));
  assert.equal(reason.phase, "send");
  const command = f.commands.find(r => r.command.includes("'indeterminate'"));
  assert.equal(command.exit_code, 0);
  assert.equal(JSON.parse(command.stdout).assignment.no_resend, true);
});

test("different runner authority is rejected before claim or native work", async t => {
  const f = await fixture(t);
  f.config.stateDir += "-wrong";
  const { returned, collected } = await f.drive();
  assert.equal(returned.result.observation, "blocked");
  assert.equal(collected.state, "queued");
  assert.equal((await f.cli(["status"])).active_assignment, null);
  assert(!f.commands.some(r => r.command.includes("'queue' 'claim'")));
  assert.equal(f.native.sends.length, 0);
  assert.equal(f.native.reads.length, 0);
});

test("active delivery automatically observes pending work without a second send", async t => {
  const f = await fixture(t, { pendingOnce: true });
  f.ready = false;
  f.config.activeJobMs = 60000;
  const { returned, collected } = await f.drive();
  assert.equal(returned.result.observation, "published");
  assert.equal(collected.answer.payload, "integration answer");
  assert.equal(returned.result.active_wait.observation_passes, 2);
  assert.equal(f.native.sends.length, 1);
  assert.equal(f.native.reads.length, 3);
  assert.equal(f.commands.filter(r => r.command.includes("'arm'")).length, 1);
  assert.equal(returned.result.active_wait.passes[0].observation, "pending");
  assert.equal(returned.result.active_wait.passes[1].observation, "published");
  assert.equal((await f.invoke("collect")).answer.payload, "integration answer");
});

test("active observation budget exhaustion preserves armed collect-only work", async t => {
  const f = await fixture(t);
  f.ready = false;
  // Unit-only tiny budget: this is not a native duration qualification.
  f.config.activeJobMs = 1;
  const { returned, collected } = await f.drive();
  assert.equal(returned.result.observation, "pending");
  assert.equal(returned.result.active_wait.observation_passes, 1);
  assert.equal(collected.dispatch_status, "armed");
  assert.equal(collected.send_may_have_occurred, true);
  assert.equal(f.native.sends.length, 1);
  assert.equal((await f.invoke("submit")).wake.status, "collect_only");
  assert.equal(f.commands.filter(r => r.command.includes("'arm'")).length, 1);
});

test("large history stays off the native wire without changing one-send behavior", async t => {
  const f = await fixture(t, { largeHistory: true });
  const history = await f.cli(["status"]);
  assert.equal(history.assignments.length, 64);
  assert(Buffer.byteLength(JSON.stringify(history), "utf8") > 117000);
  const { returned, collected } = await f.drive();
  assert.equal(returned.result.observation, "published");
  assert.equal(collected.answer.payload, "integration answer");
  const statusCall = f.commands.find(r => r.command.includes("'status' '--current'"));
  assert(statusCall);
  assert(!Object.hasOwn(JSON.parse(statusCall.stdout), "assignments"));
  assert.equal(f.native.sends.length, 1);
  assert.equal(f.commands.filter(r => r.command.includes("'arm'")).length, 1);
  assert.deepEqual((await f.invoke("collect")).answer, collected.answer);
  assert.equal((await f.cli(["status"])).assignments.length, 65);
});
