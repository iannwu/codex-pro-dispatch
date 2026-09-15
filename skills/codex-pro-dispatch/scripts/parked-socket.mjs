import * as fs from "node:fs/promises";
import * as net from "node:net";
import { randomBytes } from "node:crypto";

const identifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

function residentMode(c) {
  const resident = c.resident === true;
  if ((c.resident !== undefined && !resident) ||
      (resident && (c.leaseMs !== null || c.queuedResume !== undefined)))
    throw Error("Explicit resident lifetime requires null lease and no queued resume");
  for (const name of (resident ? ["idleMs", "replyMs"] : ["leaseMs", "idleMs", "replyMs"]))
    if (!Number.isInteger(c[name]) ||
        c[name] < 1 || c[name] > ({leaseMs:7200000,idleMs:50000,replyMs:3900000})[name])
      throw Error("Finite transport budgets required");
  return resident;
}

export async function loadSession(directory) {
  if (await fs.realpath(directory) !== directory)
    throw Error("Physical session directory required");
  const d = await fs.lstat(directory);
  const f = await fs.lstat(directory + "/session.json");
  if (!d.isDirectory() || (d.mode & 511) !== 448 ||
      !f.isFile() || (f.mode & 511) !== 384 ||
      f.nlink !== 1 || f.uid !== d.uid)
    throw Error("Private session storage required");
  const c = JSON.parse(await fs.readFile(directory + "/session.json", "utf8"));
  if (residentMode(c) ? c.expiresAt !== null :
      !Number.isSafeInteger(c.expiresAt) || c.expiresAt < 1)
    throw Error("Invalid session lifetime");
  return c;
}

export async function openSession(directory, trusted) {
  const d = await fs.lstat(directory);
  if (await fs.realpath(directory) !== directory ||
      !d.isDirectory() || (d.mode & 511) !== 448)
    throw Error("Physical private directory required");
  for (const name of ["helper", "configDir", "stateDir"])
    if (typeof trusted[name] !== "string" || !trusted[name].startsWith("/"))
      throw Error("Trusted absolute paths required");
  for (const name of ["parent", "worker"])
    if (!identifier.test(trusted[name])) throw Error("Invalid trusted identity");
  const resident = residentMode(trusted);

  const config = {
    ...trusted, sessionId: randomBytes(16).toString("hex"),
    token: randomBytes(24).toString("hex"),
    expiresAt: resident ? null : Date.now() + trusted.leaseMs
  };
  const socketPath = directory + "/wake.sock";
  const sockets = new Set(), seen = new Set(), events = [];
  let waiter = null, active = null, reserved = null, retired = false;
  let closing = null, idleTimer, leaseTimer, ordinal = 0;
  const record = (name, extra = {}) =>
    events.push({ name, at: Date.now(), ...extra });

  function close(reason = "closed") {
    if (closing) return closing;
    retired = true;
    clearTimeout(idleTimer); clearTimeout(leaseTimer);
    waiter?.(null); waiter = null;
    for (const s of sockets) s.destroy();
    closing = (async () => {
      await new Promise(resolve => server.close(resolve));
      await fs.rm(socketPath, { force: true });
      await fs.writeFile(directory + "/transport-audit.json",
        JSON.stringify({ sessionId: config.sessionId, reason, events }),
        { flag: "wx", mode: 384 });
    })();
    return closing;
  }

  const server = net.createServer(s => {
    sockets.add(s);
    let bytes = 0, text = "", framed = false;
    s.setTimeout(1000, () => s.destroy());
    s.on("error", () => {});
    s.on("close", () => {
      sockets.delete(s);
      if (retired && active?.socket === s)
        void close("expired_transport").catch(() => {});
    });
    s.on("data", b => {
      if (framed) { s.destroy(); return; }
      bytes += b.length;
      if (bytes > 2048) { s.destroy(); return; }
      text += b.toString("utf8");
      const end = text.indexOf("\n");
      if (end < 0) return;
      framed = true;
      let r;
      try { r = JSON.parse(text.slice(0, end)); }
      catch { s.destroy(); return; }
      if (end !== text.length - 1 || !r ||
          Object.keys(r).sort().join(",") !==
            "callId,operation,requestId,sessionId,token" ||
          r.sessionId !== config.sessionId || r.token !== config.token ||
          !/^[a-f0-9]{32}$/.test(r.callId) ||
          !identifier.test(r.requestId) ||
          !["run", "observe"].includes(r.operation)) {
        s.destroy(); return;
      }
      const reply = status => s.end(JSON.stringify({
        sessionId: config.sessionId, callId: r.callId,
        requestId: r.requestId, status
      }) + "\n");
      if (retired) return reply("unavailable");
      if (seen.has(r.callId)) return reply("duplicate_delivery");
      if (active) return reply("busy");
      if (reserved && (r.requestId !== reserved || r.operation !== "observe"))
        return reply("recovery_required");
      if (!waiter) return reply("not_ready");
      seen.add(r.callId);
      clearTimeout(idleTimer);
      active = { ...r, socket: s };
      s.setTimeout(config.replyMs, () => s.destroy());
      record("accepted", {
        callId: r.callId, requestId: r.requestId, operation: r.operation
      });
      const resolve = waiter; waiter = null;
      resolve({
        sessionId: config.sessionId, callId: r.callId,
        requestId: r.requestId, operation: r.operation
      });
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  server.on("error", () => void close("server_error").catch(() => {}));
  try {
    await fs.chmod(socketPath, 384);
    await fs.writeFile(directory + "/session.json", JSON.stringify(config),
      { flag: "wx", mode: 384 });
  } catch (error) {
    await close("initialization_failed"); throw error;
  }
  if (!resident) leaseTimer = setTimeout(() => {
    retired = true;
    // Expiry ends admission, not an active Pro operation.
    if (!active) void close("lease_expired").catch(() => {});
  }, config.leaseMs);

  return {
    config, close,
    async receive() {
      if (retired || seen.size >= 64) {
        await close("session_finished"); return null;
      }
      if (active || waiter) throw Error("Receive already occupied");
      let resolve;
      const pending = new Promise(r => { resolve = r; });
      waiter = resolve;
      idleTimer = setTimeout(
        () => void close("idle_expired").catch(() => {}), config.idleMs
      );
      await fs.writeFile(directory + "/ready-" + (++ordinal) + ".json",
        JSON.stringify({ sessionId: config.sessionId, ordinal, at: Date.now() }),
        { flag: "wx", mode: 384 });
      return await pending;
    },
    async finish(callId, result) {
      const a = active;
      if (!a || a.callId !== callId || result.request_id !== a.requestId)
        throw Error("Delivery/result mismatch");
      const status = result.observation || result.state;
      const allowed = ["published", "acknowledged", "pending", "not_submitted"];
      const disposition = allowed.includes(status) ? status : "blocked";
      reserved = disposition === "pending" ? a.requestId : null;
      record("finished", {
        callId, requestId: a.requestId, disposition
      });
      if (!a.socket.destroyed) {
        const ended = new Promise(resolve => a.socket.once("close", resolve));
        a.socket.setTimeout(1000, () => a.socket.destroy());
        a.socket.end(JSON.stringify({
          sessionId: config.sessionId, callId,
          requestId: a.requestId, status: disposition,
          restoration: result.restoration?.status || "not_requested"
        }) + "\n");
        await ended;
      }
      active = null;
      if (retired || disposition === "blocked" ||
          result.pending_helper_session != null ||
          result.restoration?.status === "failed_or_unverified")
        await close("retired_after_job");
      return { closed: closing !== null, reservedRequestId: reserved };
    }
  };
}
