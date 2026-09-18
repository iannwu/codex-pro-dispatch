import * as fs from "node:fs/promises";
import * as net from "node:net";
import { randomBytes } from "node:crypto";

const identifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

function workerConversationId(worker) {
  if (typeof worker === "string") return worker;
  if (!worker || typeof worker !== "object" || Array.isArray(worker) ||
      Object.keys(worker).sort().join(",") !== "conversation_id,slot" ||
      typeof worker.conversation_id !== "string" ||
      typeof worker.slot !== "string") return null;
  return worker.conversation_id;
}

function residentMode(c) {
  const resident = c.resident === true;
  if ((c.resident !== undefined && !resident) ||
      (resident && (c.leaseMs !== null || c.queuedResume !== undefined)))
    throw Error("Explicit resident lifetime requires null lease and no queued resume");
  for (const name of (resident ? ["idleMs", "replyMs"] : ["leaseMs", "idleMs", "replyMs"]))
    if (!Number.isInteger(c[name]) ||
        c[name] < 1 || c[name] > ({leaseMs:7200000,idleMs:50000,replyMs:3900000})[name])
      throw Error("Finite transport budgets required");
  if (c.maxConcurrentRequests !== undefined &&
      (!Number.isInteger(c.maxConcurrentRequests) ||
       c.maxConcurrentRequests < 1 || c.maxConcurrentRequests > 2))
    throw Error("Resident capacity must be one or two");
  if (Array.isArray(c.workers)) {
    const capacity = c.maxConcurrentRequests === undefined ?
      c.workers.length : c.maxConcurrentRequests;
    if (capacity < 1 || capacity > 2 || c.workers.length !== capacity ||
        c.workers.some(worker => !identifier.test(workerConversationId(worker) || "")) ||
        new Set(c.workers.map(workerConversationId)).size !== c.workers.length)
      throw Error("Pool resident capacity requires one or two distinct worker IDs");
  } else if (c.maxConcurrentRequests === 2) {
    throw Error("Two-worker resident capacity requires two distinct worker IDs");
  }
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
  if (!identifier.test(trusted.parent)) throw Error("Invalid trusted identity");
  const resident = residentMode(trusted);
  if (Array.isArray(trusted.workers) && trusted.workers.length) {
    const capacity = trusted.maxConcurrentRequests === undefined ?
      trusted.workers.length : trusted.maxConcurrentRequests;
    if (trusted.worker !== undefined)
      throw Error("Pool sessions use workers, not a scalar worker");
    if (capacity !== trusted.workers.length)
      throw Error("Pool session capacity must match the configured worker count");
    return await openConcurrentSession(directory, trusted, resident, capacity);
  }
  if (trusted.worker === undefined || !identifier.test(trusted.worker))
    throw Error("A single-worker session requires one trusted worker");

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
      // The first close reason is immutable. A resident that ends here did not
      // stop cleanly; finite sessions keep their historical label.
      if (retired || disposition === "blocked" ||
          result.pending_helper_session != null ||
          result.restoration?.status === "failed_or_unverified")
        await close(resident ? "resident_failed" : "retired_after_job");
      return { closed: closing !== null, reservedRequestId: reserved };
    }
  };
}

async function openConcurrentSession(directory, trusted, resident, capacity) {
  if (!Number.isInteger(capacity) || capacity < 1 || capacity > 2)
    throw Error("Resident capacity must be one or two");
  const config = {
    ...trusted, sessionId: randomBytes(16).toString("hex"),
    token: randomBytes(24).toString("hex"),
    expiresAt: resident ? null : Date.now() + trusted.leaseMs,
    maxConcurrentRequests: capacity
  };
  const socketPath = directory + "/wake.sock";
  const sockets = new Set(), seen = new Set(), events = [];
  const waiters = [], active = new Map(), reserved = new Set();
  let retired = false, closing = null, idleTimer, leaseTimer, ordinal = 0;
  const record = (name, extra = {}) => events.push({name, at: Date.now(), ...extra});

  function close(reason = "closed") {
    if (closing) return closing;
    retired = true;
    clearTimeout(idleTimer); clearTimeout(leaseTimer);
    while (waiters.length) {
      const waiter = waiters.shift();
      clearTimeout(waiter.timer);
      waiter.resolve(null);
    }
    for (const socket of sockets) socket.destroy();
    closing = (async () => {
      await new Promise(resolve => server.close(resolve));
      await fs.rm(socketPath, {force: true});
      await fs.writeFile(directory + "/transport-audit.json",
        JSON.stringify({sessionId: config.sessionId, reason, events}),
        {flag: "wx", mode: 384});
    })();
    return closing;
  }

  function armIdle() {
    clearTimeout(idleTimer);
    idleTimer = undefined;
    if (retired || active.size || !waiters.length) return;
    idleTimer = setTimeout(() => void close("idle_expired").catch(() => {}), config.idleMs);
  }

  const server = net.createServer(socket => {
    sockets.add(socket);
    let bytes = 0, text = "", framed = false;
    socket.setTimeout(1000, () => socket.destroy());
    socket.on("error", () => {});
    socket.on("close", () => sockets.delete(socket));
    socket.on("data", buffer => {
      if (framed) {socket.destroy(); return;}
      bytes += buffer.length;
      if (bytes > 2048) {socket.destroy(); return;}
      text += buffer.toString("utf8");
      const end = text.indexOf("\n");
      if (end < 0) return;
      framed = true;
      let request;
      try {request = JSON.parse(text.slice(0, end));}
      catch {socket.destroy(); return;}
      if (end !== text.length - 1 || !request ||
          Object.keys(request).sort().join(",") !==
            "callId,operation,requestId,sessionId,token" ||
          request.sessionId !== config.sessionId || request.token !== config.token ||
          !/^[a-f0-9]{32}$/.test(request.callId) ||
          !identifier.test(request.requestId) ||
          !["run", "observe"].includes(request.operation)) {
        socket.destroy(); return;
      }
      const reply = status => socket.end(JSON.stringify({
        sessionId: config.sessionId, callId: request.callId,
        requestId: request.requestId, status
      }) + "\n");
      if (retired) return reply("unavailable");
      if (seen.has(request.callId)) return reply("duplicate_delivery");
      if (reserved.has(request.requestId) && request.operation !== "observe")
        return reply("recovery_required");
      if ([...active.values()].some(value => value.requestId === request.requestId))
        return reply("duplicate_request");
      if (active.size >= capacity || !waiters.length)
        return reply(active.size >= capacity ? "busy" : "not_ready");
      seen.add(request.callId);
      clearTimeout(idleTimer);
      active.set(request.callId, {...request, socket});
      socket.setTimeout(config.replyMs, () => socket.destroy());
      record("accepted", {callId: request.callId, requestId: request.requestId,
        operation: request.operation});
      const waiter = waiters.shift();
      clearTimeout(waiter.timer);
      waiter.resolve({sessionId: config.sessionId, callId: request.callId,
        requestId: request.requestId, operation: request.operation});
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject); server.listen(socketPath, resolve);
  });
  server.on("error", () => void close("server_error").catch(() => {}));
  try {
    await fs.chmod(socketPath, 384);
    await fs.writeFile(directory + "/session.json", JSON.stringify(config),
      {flag: "wx", mode: 384});
  } catch (error) {await close("initialization_failed"); throw error;}
  if (!resident) leaseTimer = setTimeout(() => {
    retired = true;
    if (!active.size) void close("lease_expired").catch(() => {});
  }, config.leaseMs);

  return {
    config, close,
    async receive(waitMs = undefined) {
      if (retired || seen.size >= 64) {
        await close("session_finished"); return null;
      }
      if (active.size + waiters.length >= capacity)
        throw Error("Receive already occupied");
      if (waitMs !== undefined &&
          (!Number.isInteger(waitMs) || waitMs < 1 || waitMs > config.replyMs))
        throw Error("Invalid bounded receive wait");
      let entry;
      const pending = new Promise(resolve => {entry = {resolve, timer: undefined};});
      waiters.push(entry);
      armIdle();
      if (waitMs !== undefined) {
        entry.timer = setTimeout(() => {
          const index = waiters.indexOf(entry);
          if (index >= 0) waiters.splice(index, 1);
          entry.resolve(null);
          armIdle();
        }, waitMs);
      }
      const current = ++ordinal;
      try {
        await fs.writeFile(directory + "/ready-" + current + ".json",
          JSON.stringify({sessionId: config.sessionId, ordinal: current, at: Date.now()}),
          {flag: "wx", mode: 384});
      } catch (error) {
        const index = waiters.indexOf(entry);
        if (index >= 0) waiters.splice(index, 1);
        clearTimeout(entry.timer);
        throw error;
      }
      return await pending;
    },
    async finish(callId, result) {
      const current = active.get(callId);
      if (!current || result.request_id !== current.requestId)
        throw Error("Delivery/result mismatch");
      const status = result.observation || result.state;
      const allowed = ["published", "acknowledged", "pending", "not_submitted"];
      const disposition = allowed.includes(status) ? status : "blocked";
      record("finished", {callId, requestId: current.requestId, disposition});
      if (!current.socket.destroyed) {
        const ended = new Promise(resolve => current.socket.once("close", resolve));
        current.socket.setTimeout(1000, () => current.socket.destroy());
        current.socket.end(JSON.stringify({sessionId: config.sessionId, callId,
          requestId: current.requestId, status: disposition,
          restoration: result.restoration?.status || "not_requested"}) + "\n");
        await ended;
      }
      active.delete(callId);
      if (disposition === "pending") reserved.add(current.requestId);
      else reserved.delete(current.requestId);
      armIdle();
      if (disposition === "blocked" || result.pending_helper_session != null ||
          result.restoration?.status === "failed_or_unverified")
        await close("resident_failed");
      else if (retired && !active.size) await close("lease_expired");
      return {closed: closing !== null, reservedRequestIds: [...reserved],
        activeRequests: [...active.values()].map(value => value.requestId)};
    }
  };
}
