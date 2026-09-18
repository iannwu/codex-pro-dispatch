import * as fs from "node:fs/promises";
import * as net from "node:net";
import { constants } from "node:fs";
import { randomBytes } from "node:crypto";
import { execFile } from "node:child_process";
import { loadSession } from "./parked-socket.mjs";

const [directory, operation, requestId, promptFile, clientSessionId] =
  process.argv.slice(2);

async function main() {
  if (!["submit", "resume", "observe", "collect", "acknowledge"].includes(operation) ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(requestId || ""))
    throw Error("Invalid client command");
  if ((await fs.lstat(directory)).uid !== process.getuid())
    throw Error("Session must belong to this user");
  const c = await loadSession(directory);

  function helper(args) {
    return new Promise((resolve, reject) => {
      const child = execFile("python3", [c.helper, ...args], {
        timeout: 35000, maxBuffer: 8 * 1024 * 1024
      }, (error, stdout, stderr) => {
        let value;
        try { value = JSON.parse(error ? stderr : stdout); }
        catch { reject(Error("Incomplete helper JSON")); return; }
        if (error || value.ok !== true) {
          reject(Error(value.error || "Helper failed")); return;
        }
        resolve(value);
      });
      child.stdin.end();
    });
  }

  const state = await helper(["status", "--current"]);
  if (state.paths?.config_dir !== c.configDir ||
      state.paths?.state_dir !== c.stateDir)
      throw Error("Client and native session authority paths differ");
  const stateWorkers = state.worker_pool?.workers?.map(worker => worker.conversation_id) ||
    (state.worker?.conversation_id ? [state.worker.conversation_id] : []);
  const hasTrustedWorker = worker => typeof worker === "string" && stateWorkers.includes(worker);

  if (operation === "acknowledge")
    return await helper(["queue", "acknowledge", requestId]);

  // Recovery of this queued ID never goes through queue submit again.
  if (operation === "submit" && c.queuedResume) {
    if (requestId === c.queuedResume.requestId)
      return { ...await helper(["queue", "collect", requestId]),
        wake: { status: "collect_only" } };
    const prior = await helper(["queue", "collect", c.queuedResume.requestId]);
    if (!["published", "acknowledged"].includes(prior.state) ||
        prior.dispatch_status !== "complete" || prior.sent_verified !== true ||
        prior.parent_task_id !== c.parent ||
        !hasTrustedWorker(prior.worker_conversation_id))
      throw Error("Complete the bound queued recovery before another request");
  }

  let resumeCommand = null;
  if (operation === "resume") {
    const r = c.queuedResume;
    if (!r || requestId !== r.requestId || promptFile !== "1" ||
        !/^[a-f0-9]{32}$/.test(clientSessionId || "") ||
        clientSessionId === r.nonce || c.sessionId === r.sessionId)
      throw Error("Only the bound queued request may explicitly resume");
    async function record(name) {
      const path = directory + "/" + name, d = await fs.lstat(directory);
      if (!d.isDirectory() || d.uid !== process.getuid() || (d.mode & 511) !== 448 ||
          await fs.realpath(path) !== path)
        throw Error("Private physical resume evidence required");
      const h = await fs.open(path,
        constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const a = await h.stat();
        if (!a.isFile() || a.uid !== d.uid || (a.mode & 511) !== 384 ||
            a.nlink !== 1 || a.size > 4096) throw Error("Invalid resume evidence");
        const buffer = Buffer.alloc(4097); let used = 0;
        while (used < buffer.length) {
          const {bytesRead} = await h.read(buffer, used, buffer.length - used, null);
          if (!bytesRead) break;
          used += bytesRead;
        }
        const b = await h.stat(), leaf = await fs.lstat(path), after = await fs.lstat(directory);
        if (used > 4096 || ["dev", "ino", "uid", "mode", "nlink", "size", "mtimeMs", "ctimeMs"]
            .some(k => a[k] !== b[k] || b[k] !== leaf[k]) ||
            ["dev", "ino", "uid", "mode"].some(k => d[k] !== after[k]) ||
            await fs.realpath(path) !== path) throw Error("Resume evidence changed");
        return buffer.subarray(0, used).toString("utf8");
      } finally { await h.close(); }
    }
    const raw = await record("command-1.json");
    if (raw !== await record("command-observed-1.json"))
      throw Error("Resume command was not observed exactly");
    const v = JSON.parse(raw), ready = JSON.parse(await record("ready-1.json"));
    if (Object.keys(v).sort().join(",") !==
        "clientSessionId,deadlineAt,fingerprint,nonce,operation,ordinal,pid,ppid,promptSha256,requestId,sessionId" ||
        v.operation !== "resume" || v.sessionId !== c.sessionId || v.ordinal !== 1 ||
        v.requestId !== requestId || v.clientSessionId !== r.clientSessionId ||
        v.nonce !== clientSessionId || v.fingerprint !== r.fingerprint ||
        v.promptSha256 !== r.promptSha256 || !Number.isSafeInteger(v.deadlineAt) ||
        v.deadlineAt > c.expiresAt || Date.now() >= v.deadlineAt ||
        ready.sessionId !== c.sessionId || ready.ordinal !== 1 ||
        !Number.isSafeInteger(ready.at) || ready.at >= v.deadlineAt)
      throw Error("Wrong or expired queued-resume command");
    const resumeWorker = r.workerConversationId || c.worker;
    if (!hasTrustedWorker(resumeWorker)) throw Error("Queued-resume worker is not configured");
    const checked = await helper([
      "queue", "resume-check", requestId, "--fingerprint", r.fingerprint,
      "--worker-conversation-id", resumeWorker, "--client-session-id", r.clientSessionId,
      "--raw-prompt-sha256", r.promptSha256
    ]);
    if (checked.resume_eligible !== true || checked.assignment_absent !== true ||
        checked.send_authorized !== false || checked.state !== "queued" ||
        checked.request_id !== requestId || checked.fingerprint !== r.fingerprint ||
        checked.client_session_id !== r.clientSessionId || checked.raw_prompt_sha256 !== r.promptSha256 ||
        checked.worker_conversation_id !== resumeWorker ||
        !["user-confirmed-worker", "user-confirmed-pro"].includes(checked.worker_model_confirmation) ||
        checked.paths?.config_dir !== c.configDir || checked.paths?.state_dir !== c.stateDir)
      throw Error("Canonical queued-resume check differs");
    resumeCommand = v;
  }

  if (operation === "submit") {
    if (!promptFile?.startsWith("/") || !clientSessionId)
      throw Error("Submit requires absolute prompt file and client session ID");
    if (!c.workers && !hasTrustedWorker(c.worker))
      throw Error("Configured worker changed");
    await helper([
      "queue", "submit", "--request-id", requestId,
      "--prompt-file", promptFile, "--client-session-id", clientSessionId
    ]);
  }

  const existing = await helper(["queue", "collect", requestId]);
  if (resumeCommand) {
    if (existing.state !== "queued" || existing.fingerprint !== c.queuedResume.fingerprint ||
        existing.client_session_id !== c.queuedResume.clientSessionId ||
        existing.dispatch_status !== null || existing.send_authorized !== false ||
        existing.send_may_have_occurred !== false || existing.sent_verified !== false)
      throw Error("Queued request changed before resume");
    try {
      await fs.lstat(directory + "/transport-audit.json");
      throw Error("Session is closed");
    } catch (e) { if (e.code !== "ENOENT") throw e; }
    if (Date.now() >= resumeCommand.deadlineAt) throw Error("Resume gate expired");
    const marker = await fs.open(directory + "/queued-resume.once.json", "wx", 384);
    try {
      await marker.writeFile(JSON.stringify(resumeCommand));
      await marker.sync();
    } finally { await marker.close(); }
    const parent = await fs.open(directory, "r");
    try { await parent.sync(); } finally { await parent.close(); }
    if (Date.now() >= resumeCommand.deadlineAt)
      throw Error("Resume gate expired after consumption; do not retry");
  }
  if (operation === "collect" ||
      ["published", "acknowledged", "cancelled", "released"].includes(existing.state))
    return existing;

  // A repeated submit of post-arm work is collection only, never another wake.
  if (operation === "submit" && existing.send_may_have_occurred)
    return { ...existing, wake: { status: "collect_only" } };
  if (operation === "observe" && !existing.send_may_have_occurred)
    return { ...existing, wake: { status: "not_submitted" } };
  if (!c.workers && !hasTrustedWorker(c.worker))
    throw Error("Configured worker changed");
  if (existing.parent_task_id && existing.parent_task_id !== c.parent)
    throw Error("Request belongs to another native parent");

  const callId = randomBytes(16).toString("hex");
  const wake = await new Promise(resolve => {
    const s = net.createConnection(directory + "/wake.sock");
    let raw = "", done = false;
    const finish = value => {
      if (done) return;
      done = true; clearTimeout(timer); s.destroy(); resolve(value);
    };
    const timer = setTimeout(() => finish({
      status: "unknown_collect_only", reason: "client_wait_expired"
    }), c.replyMs);
    s.on("connect", () => s.write(JSON.stringify({
      sessionId: c.sessionId, token: c.token, callId, requestId,
      operation: operation === "observe" ? "observe" : "run"
    }) + "\n"));
    s.on("data", b => {
      raw += b.toString("utf8");
      if (Buffer.byteLength(raw) > 8192)
        finish({ status: "unknown_collect_only", reason: "oversize_reply" });
    });
    s.on("error", () => finish({
      status: "unknown_collect_only", reason: "transport_error"
    }));
    s.on("end", () => {
      try {
        if (!raw.endsWith("\n") || raw.indexOf("\n") !== raw.length - 1)
          throw Error("frame");
        const value = JSON.parse(raw);
        if (value.sessionId !== c.sessionId || value.callId !== callId ||
            value.requestId !== requestId) throw Error("identity");
        finish(value);
      } catch {
        finish({ status: "unknown_collect_only", reason: "invalid_reply" });
      }
    });
    s.on("close", () => {
      if (!done) finish({ status: "unknown_collect_only", reason: "closed" });
    });
  });

  // Always collect through the broker. Never trust socket answer content.
  return { ...await helper(["queue", "collect", requestId]), wake };
}

main().then(value => console.log(JSON.stringify(value))).catch(error => {
  console.error(JSON.stringify({
    ok: false, request_id: requestId || null, error: String(error.message),
    recovery: "inspect_existing_request; do_not_automatically_resend"
  }));
  process.exitCode = 1;
});
