// @exec: {"yield_time_ms":1000}
// Structural failure record shared by the runner and the generated resident
// code. Tool exceptions can come from another realm, so no instanceof checks;
// Error instances otherwise serialize as {}.
globalThis.describeFailure = function describeFailure(error, depth = 0) {
  if (error === null || typeof error !== "object") {
    return error === undefined ? null : { message: String(error) };
  }
  if (typeof error.message !== "string" && typeof error.stack !== "string")
    return error;
  return {
    name: typeof error.name === "string" ? error.name : null,
    message: String(error.message),
    stack: typeof error.stack === "string" ? error.stack : null,
    cause: depth < 3 ? describeFailure(error.cause, depth + 1) : null,
    detail: error.detail === undefined ? null : error.detail
  };
};

globalThis.runParkedJob = async function runParkedJob(config, requestId) {
  const identifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  const poolWorkers = Array.isArray(config.workers) ? config.workers.map((worker, index) =>
    typeof worker === "string" ? {slot: "worker-" + (index + 1), conversation_id: worker} : worker
  ) : [];
  const poolMode = poolWorkers.length > 0;
  let selectedWorker = typeof config.worker === "string" ? config.worker : null;
  let selectedSlot = config.workerSlot || null;
  const workerId = () => selectedWorker;
  const validPath = value =>
    typeof value === "string" && value.startsWith("/") &&
    !/[\0\r\n]/.test(value);
  if (!identifier.test(requestId) ||
      !identifier.test(config.parent) ||
      (!poolMode && !identifier.test(config.worker)) ||
      (poolMode && (!config.workers.length || config.workers.length > 2 ||
        poolWorkers.some(worker => !identifier.test(worker?.slot) ||
          !identifier.test(worker?.conversation_id)) ||
        new Set(poolWorkers.map(worker => worker.conversation_id)).size !== poolWorkers.length)) ||
      !validPath(config.helper) ||
      !validPath(config.configDir) || !validPath(config.stateDir) ||
      config.preflightConfirmed !== true ||
      (config.restoreParent !== undefined && typeof config.restoreParent !== "boolean") ||
      !Number.isInteger(config.maxSnapshots) ||
      config.maxSnapshots < 1 || config.maxSnapshots > 60 ||
      (config.observationMs !== undefined &&
       (!Number.isInteger(config.observationMs) ||
        config.observationMs < 10000 || config.observationMs > 600000))) {
    throw Error("Invalid trusted runner configuration or request ID");
  }

  if (!selectedWorker && !poolMode) throw Error("Trusted worker is required");

  const quote = value => "'" + String(value).replace(/'/g, "'\\''") + "'";
  const trace = [];
  let phase = "collect", directory = null, sequence = 0;
  let potentiallyArmed = false, published = false, nativeUsed = false;
  let pendingCommand = null, helperUncertain = false, result;

  function problem(message, detail) {
    const error = Error(message);
    error.detail = detail;
    return error;
  }

  // The deployed tools return one text block. The block text is the payload
  // to decode; the surrounding envelope is preserved before decoding.
  function nativeText(value) {
    if (!value || value.isError === true ||
        !Array.isArray(value.content) || value.content.length !== 1 ||
        value.content[0].type !== "text" ||
        typeof value.content[0].text !== "string") {
      throw problem("Unsupported or failed native tool result", value);
    }
    return value.content[0].text;
  }

  // A send acknowledgment is not a submission record. Only the two evidenced
  // success variants are accepted: the retained incident acknowledgment
  // {"threadId": <bound worker>} and the identity-free {} acknowledgment.
  // Everything else is preserved for diagnostic review, never resent.
  function acknowledged(sent) {
    if (!sent || sent.isError === true) {
      throw problem("Native send needs diagnostic review; never resend", sent);
    }
    let value;
    try { value = JSON.parse(nativeText(sent)); } catch { value = undefined; }
    const keys = value && typeof value === "object" && !Array.isArray(value) ?
      Object.keys(value) : null;
    if (!keys || !(keys.length === 0 ||
        (keys.length === 1 && value.threadId === workerId()))) {
      throw problem("Unsupported native send acknowledgment; never resend", sent);
    }
  }

  function bound(value) {
    if (value.request_id !== requestId ||
        value.parent_task_id !== config.parent ||
        value.worker_conversation_id !== workerId()) {
      throw problem("Broker identity mismatch", value);
    }
    return value;
  }

  function selectWorker(value) {
    if (!poolMode) return;
    const candidate = poolWorkers.find(worker =>
      worker.conversation_id === value.worker_conversation_id
    );
    if (!candidate) throw problem("Broker worker is not in the trusted pool", value);
    selectedWorker = candidate.conversation_id;
    selectedSlot = value.worker_slot || candidate.slot;
    config.worker = selectedWorker;
    config.workerSlot = selectedSlot;
    if (config.residentInvocation) {
      config.residentInvocation = {
        ...config.residentInvocation, worker: selectedWorker,
        slot: selectedSlot, request: requestId
      };
    }
  }

  async function helper(args) {
    const owner = config.residentInvocation === undefined ? [] :
      ["--resident-invocation", JSON.stringify(config.residentInvocation)];
    const command = ["python3", config.helper, ...owner, ...args].map(quote).join(" ");
    trace.push({ kind: "helper", operation: args.slice(0, 2), at: Date.now() });
    helperUncertain = true;
    let response = await tools.exec_command({
      cmd: command, login: false, tty: false,
      yield_time_ms: 30000, max_output_tokens: 20000
    });
    let output = response.output || "";
    if (response.exit_code === undefined) {
      if (!Number.isInteger(response.session_id)) {
        throw problem("Unresolved helper without execution identity", response);
      }
      pendingCommand = response.session_id;
      response = await tools.write_stdin({
        session_id: pendingCommand, chars: "",
        yield_time_ms: 30000, max_output_tokens: 20000
      });
      output += response.output || "";
      if (response.exit_code === undefined) {
        throw problem("Helper remains pending; preserve execution", {
          session_id: pendingCommand
        });
      }
      pendingCommand = null;
    }
    helperUncertain = false;
    let value;
    try { value = JSON.parse(output); }
    catch { throw problem("Helper output is not complete JSON", {
      exit_code: response.exit_code
    }); }
    if (response.exit_code !== 0 || value.ok !== true) {
      throw problem("Helper rejected operation", value);
    }
    return value;
  }

  async function evidence(raw) {
    if (typeof raw !== "string") throw Error("Evidence must be returned text");
    if (directory === null) {
      const code = `{
const fs=await import("node:fs/promises");
const created=await fs.mkdtemp(nodeRepl.tmpDir.replace(/\\/$/,"")+"/pro-native-");
await fs.chmod(created,0o700);
console.log(JSON.stringify({directory:await fs.realpath(created)}));
}`;
      const created = JSON.parse(nativeText(await tools.mcp__node_repl__js({
        code, timeout_ms: 30000, title: "Create private native evidence"
      })));
      if (!validPath(created.directory)) throw Error("Invalid evidence directory");
      directory = created.directory;
    }
    const path = directory + "/evidence-" + (++sequence) + ".json";
    const code = `{
const fs=await import("node:fs/promises");
const path=${JSON.stringify(path)};
const raw=${JSON.stringify(raw)};
const handle=await fs.open(path,"wx",0o600);
try { await handle.writeFile(raw,"utf8"); await handle.sync(); }
finally { await handle.close(); }
const parent=await fs.open(${JSON.stringify(directory)},"r");
try { await parent.sync(); } finally { await parent.close(); }
console.log(JSON.stringify({path}));
}`;
    const ack = await tools.mcp__node_repl__js({
      code, timeout_ms: 30000, title: "Preserve native evidence"
    });
    let saved;
    try { saved = JSON.parse(nativeText(ack)); } catch { saved = null; }
    if (saved?.path === path) return path;
    // The exclusive write may have completed although its acknowledgment did
    // not decode. Never rewrite it; confirm the exact bytes and sync the file
    // and its directory, and stop when durability still cannot be confirmed.
    const verification = await tools.mcp__node_repl__js({
      code: `{
const fs=await import("node:fs/promises");
const path=${JSON.stringify(path)};
const raw=${JSON.stringify(raw)};
const handle=await fs.open(path,"r");
try {
  if (await handle.readFile("utf8")!==raw) throw Error("Evidence bytes differ");
  await handle.sync();
} finally { await handle.close(); }
const parent=await fs.open(${JSON.stringify(directory)},"r");
try { await parent.sync(); } finally { await parent.close(); }
console.log(JSON.stringify({verified:true}));
}`, timeout_ms: 30000, title: "Verify native evidence"
    });
    let verified;
    try { verified = JSON.parse(nativeText(verification)); } catch { verified = null; }
    if (verified?.verified !== true) {
      throw problem("Evidence persistence unconfirmed", {
        path, acknowledgment: ack, verification
      });
    }
    trace.push({ kind: "evidence_verified", path });
    return path;
  }

  async function read() {
    if (!workerId()) throw Error("A pool worker must be selected before native read");
    nativeUsed = true;
    const startedAt = Date.now();
    const response = await tools.mcp__codex_app__read_thread({
      threadId: workerId(), turnLimit: 2, maxOutputCharsPerItem: 20000
    });
    // The original envelope and operation identity are preserved before any
    // extraction; the extracted history bytes follow for the helper.
    const envelope = await evidence(JSON.stringify({
      operation: "read_thread", request_id: requestId, phase,
      worker_conversation_id: workerId(), startedAt, returnedAt: Date.now(),
      result: response
    }));
    let raw;
    try { raw = nativeText(response); }
    catch (error) { error.evidence = envelope; throw error; }
    const path = await evidence(raw);
    let value;
    try { value = JSON.parse(raw); }
    catch { throw problem("Native inner history is not JSON", { path }); }
    if (value.schemaVersion !== 1 ||
        value.thread?.kind !== "chatgpt" ||
        value.thread?.id !== workerId()) {
      throw Error("Native history target mismatch");
    }
    trace.push({ kind: "read", startedAt, returnedAt: Date.now(), path });
    return { value, path };
  }

  async function observe(path) {
    const args = [
      "queue", "observe", requestId,
      "--parent-task-id", config.parent, "--native-controls-confirmed"
    ];
    if (path !== undefined) args.push("--native-read-file", path);
    const value = bound(await helper(args));
    if (!["pending", "published"].includes(value.observation)) {
      throw problem("Unexpected observation result", value);
    }
    if (value.observation === "published") published = true;
    return value;
  }

  async function collectHistory() {
    phase = "recover-staged";
    try {
      result = await observe();
    } catch (error) {
      // This exact, non-mutating error is the only fresh-snapshot fallback.
      if (error.detail?.error_type !== "StateError" ||
          error.detail?.error !== "No native snapshot or staged history") {
        throw error;
      }
    }

    if (!published) {
      const observationDeadline = Date.now() +
        (config.observationMs === undefined ? 50000 : config.observationMs);
      for (let index = 0; index < config.maxSnapshots; index++) {
        phase = "observe";
        const snapshot = await read();
        result = await observe(snapshot.path);
        if (published) break;
        if (index + 1 >= config.maxSnapshots ||
            Date.now() + 10000 > observationDeadline) break;
        trace.push({ kind: "observation_delay", milliseconds: 10000 });
        await new Promise(resolve => setTimeout(resolve, 10000));
      }
    }
  }

  try {
    const authority = await helper(["status", "--current"]);
    if (authority.paths?.config_dir !== config.configDir ||
        authority.paths?.state_dir !== config.stateDir)
      throw Error("Native runner authority paths differ from trusted session");
    const existing = await helper(["queue", "collect", requestId]);
    if (["published", "acknowledged"].includes(existing.state)) {
      selectWorker(existing);
      bound(existing);
      published = existing.state === "published";
      result = {
        ...existing,
        observation: existing.state === "published" ? "published" : "acknowledged"
      };
    } else if (config.collectOnly === true &&
               existing.send_may_have_occurred !== true) {
      result = { ...existing, observation: "not_submitted" };
    } else if (config.collectOnly === true) {
      if (existing.state !== "claimed")
        throw Error("Collect-only recovery requires an existing claimed request");
      selectWorker(existing);
      bound(existing);
      potentiallyArmed = true;
      await collectHistory();
    } else {
      const authorityWorkers = poolMode ?
        (authority.worker_pool?.workers || []).map(worker => worker.conversation_id) :
        [authority.worker?.conversation_id];
      if (!poolMode && authority.worker?.conversation_id !== config.worker)
        throw Error("Native runner worker differs from trusted session");
      if (poolMode && poolWorkers.some(worker => !authorityWorkers.includes(worker.conversation_id)))
        throw Error("Native runner worker pool differs from trusted session");
      phase = "claim";
      const claimArgs = ["queue", "claim", "--request-id", requestId];
      if (!poolMode) claimArgs.push(
        "--expected-worker-conversation-id", config.worker
      );
      claimArgs.push("--parent-task-id", config.parent, "--native-controls-confirmed");
      const claimed = await helper(claimArgs);
      if (poolMode && typeof claimed.worker_conversation_id === "string")
        selectWorker(claimed);
      const claim = claimed.action === "queued" ? claimed : bound(claimed);
      if (claim.action === "queued" &&
          (claim.request_id !== requestId || claim.state !== "queued"))
        throw problem("Queued claim identity mismatch", claim);
      if ((claim.action !== "queued" && claim.assignment_id !== requestId) ||
          !["arm_then_send_once", "collect_only", "queued"].includes(claim.action)) {
        throw problem("Unexpected claim result", claim);
      }

      if (claim.action === "queued") {
        result = { ...claim, observation: "not_submitted", no_resend: false };
      } else if (claim.action === "arm_then_send_once") {
        if (config.collectOnly === true)
          throw Error("Collect-only operation cannot arm or send");
        if (typeof claim.wrapped_prompt !== "string" ||
            !claim.wrapped_prompt.startsWith(
              "[CODEX_PRO_DISPATCH assignment_id=" + requestId + "]\n"
            )) throw Error("Missing bound wrapped prompt");

        phase = "pre-send-read";
        const before = await read();
        if (before.value.thread.status?.type !== "idle") {
          throw Error("Configured chat is not idle; no arm or send");
        }

        phase = "arm";
        // If the helper's outcome is lost, this invocation still cannot send.
        potentiallyArmed = true;
        const generation = config.residentInvocation?.generation;
        const armArgs = poolMode ? [
          "arm-for-send", selectedSlot, requestId,
          "--generation", String(Number.isInteger(generation) ? generation : claim.owner_generation),
          "--invocation", String(config.residentInvocation?.invocation || "")
        ] : ["arm", requestId];
        const armed = await helper(armArgs);
        const receipt = armed.assignment;
        if (!receipt || receipt.assignment_id !== requestId ||
            receipt.parent_task_id !== config.parent ||
            receipt.worker_conversation_id !== workerId() ||
            receipt.status !== "armed" || receipt.no_resend !== true) {
          throw Error("Arm result does not authorize this exact operation");
        }
        const sendPrompt = poolMode ? armed.wrapped_prompt : claim.wrapped_prompt;
        if (typeof sendPrompt !== "string" ||
            !sendPrompt.startsWith(
              "[CODEX_PRO_DISPATCH assignment_id=" + requestId + "]\n"
            )) throw Error("Missing bound wrapped prompt");

        phase = "send";
        nativeUsed = true;
        trace.push({ kind: "send_attempt", at: Date.now() });
        const sent = await tools.mcp__codex_app__send_message_to_thread({
          threadId: workerId(), prompt: sendPrompt
        });
        await evidence(JSON.stringify({
          operation: "send_message_to_thread", request_id: requestId, phase,
          worker_conversation_id: workerId(), returnedAt: Date.now(),
          result: sent
        }));
        acknowledged(sent);
      } else {
        potentiallyArmed = true;
      }

      await collectHistory();
    }
  } catch (error) {
    result = {
      ok: false, request_id: requestId, observation: "blocked",
      failed_phase: phase, error: String(error.message || error),
      no_resend: potentiallyArmed, diagnostic_review_required: true
    };
    try {
      result.error_evidence = await evidence(JSON.stringify({
        phase, request_id: requestId, parent_task_id: config.parent,
        worker_conversation_id: workerId(), worker_slot: selectedSlot, at: Date.now(),
        error: describeFailure(error), envelope_evidence: error.evidence ?? null
      }));
    } catch (persistence) {
      // The original error stays in the result; the persistence failure is
      // supplemental.
      result.evidence_write_failed = describeFailure(persistence);
    }

    // Preserve post-arm uncertainty, but never alter a completed receipt.
    if (potentiallyArmed && !published && !helperUncertain) {
      try {
        const status = await helper(["status", requestId]);
        if (["armed", "submitted", "pending", "ambiguous", "indeterminate"]
            .includes(status.assignment?.status)) {
          if (!result.error_evidence) throw Error("No private reason evidence");
          await helper([
            "indeterminate", requestId,
            "--reason-file", result.error_evidence
          ]);
        }
      } catch { result.receipt_update_requires_review = true; }
    }
  } finally {
    const restoration = {
      parent_task_id: config.parent,
      status: "not_requested",
      foreground_verified: false
    };
    if (nativeUsed && config.restoreParent !== false) {
      try {
        const navigation = await tools.mcp__codex_app__navigate_to_codex_page({
          threadId: config.parent
        });
        if (!navigation || navigation.isError === true) {
          throw Error("Native navigation rejected");
        }
        restoration.status = "native_navigation_returned";
        restoration.evidence_file = await evidence(JSON.stringify(navigation));
      } catch {
        restoration.status = "failed_or_unverified";
      }
    }
  result = {
      ...result, request_id: requestId,
      worker_conversation_id: workerId(), worker_slot: selectedSlot,
      owner_generation: config.residentInvocation?.generation ?? null,
      restoration,
      evidence_directory: directory, evidence_retained: directory !== null,
      pending_helper_session: pendingCommand, helper_quiescent: !helperUncertain, trace
    };
  }
  return result;
};

globalThis.runParkedDelivery = async function runParkedDelivery(config, delivery) {
  if (!delivery || delivery.sessionId !== config.sessionId ||
      !["run", "observe"].includes(delivery.operation) ||
      !/^[a-f0-9]{32}$/.test(delivery.callId))
    throw Error("Unbound socket delivery");

  const budget = config.activeJobMs === undefined ? 0 : config.activeJobMs;
  if (!Number.isInteger(budget) || budget < 0 || budget > 3600000)
    throw Error("Invalid active observation budget");

  const started = Date.now(), passes = [], milestones = [];
  let result, nextMilestone = 600000;
  try {
    do {
      result = await runParkedJob({
        ...config,
        collectOnly: delivery.operation === "observe" || passes.length > 0
      }, delivery.requestId);
      passes.push({
        observation: result.observation || result.state,
        evidence_directory: result.evidence_directory,
        restoration: result.restoration,
        pending_helper_session: result.pending_helper_session,
        send_attempts: (result.trace || []).filter(
          event => event.kind === "send_attempt"
        ).length
      });

      const elapsed = Math.max(0, Date.now() - started);
      if (result.observation !== "pending" ||
          result.pending_helper_session != null ||
          result.restoration?.status === "failed_or_unverified") break;

      while (nextMilestone <= elapsed && nextMilestone <= budget) {
        milestones.push(nextMilestone);
        if (config.restoreParent !== false && typeof text === "function") text({
          kind: "active_job_observation",
          request_id: delivery.requestId,
          milestone_ms: nextMilestone,
          elapsed_ms: elapsed,
          state: "pending",
          no_resend: true
        });
        nextMilestone += 600000;
      }
      if (budget === 0 || elapsed >= budget) break;
      await new Promise(resolve =>
        setTimeout(resolve, Math.min(10000, budget - elapsed))
      );
    } while (Date.now() - started < budget);
  } catch (error) {
    result = {
      request_id: delivery.requestId, observation: "blocked",
      error: String(error.message || error), failure: describeFailure(error)
    };
  }

  result = {
    ...result,
    active_wait: {
      clock: "Date.now wall-clock milliseconds",
      budget_ms: budget,
      elapsed_ms: Math.max(0, Date.now() - started),
      observation_passes: passes.length,
      milestones_ms: milestones,
      passes
    }
  };
  const code = "console.log(JSON.stringify(await parkedSocket.finish(" +
    JSON.stringify(delivery.callId) + "," + JSON.stringify(result) + ")));";
  try {
    const transport = await tools.mcp__node_repl__js({
      code, timeout_ms: 30000, title: "Finish native socket delivery"
    });
    return { result, transport };
  } catch (error) {
    // Preserve the already-created result, especially its helper handle.
    throw Object.assign(Error("Native finish unconfirmed"), {cause: error, result});
  }
};

// One resident evaluation owns this bounded scheduler. Starting both promise
// chains before awaiting either is intentional: the queue lock assigns each
// request a distinct configured worker slot, while the native capability gate
// remains a separate live qualification. No second listener or parent task is
// created here.
globalThis.runParkedWorkerPool = async function runParkedWorkerPool(config, deliveries) {
  const identifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  if (!Array.isArray(config.workers) || config.workers.length < 1 ||
      config.workers.length > 2 || !Array.isArray(deliveries) ||
      deliveries.length < 1 || deliveries.length > config.workers.length)
    throw Error("A pool scheduler requires one or two workers and bounded deliveries");
  const requests = new Set();
  for (const delivery of deliveries) {
    if (!delivery || delivery.sessionId !== config.sessionId ||
        !identifier.test(delivery.requestId) || requests.has(delivery.requestId))
      throw Error("Pool delivery identity mismatch");
    requests.add(delivery.requestId);
  }
  const capacity = config.workers.length;
  const jobs = deliveries.map(delivery => runParkedDelivery({
    ...config, maxConcurrentRequests: capacity
  }, delivery));
  const results = await Promise.all(jobs);
  return {
    ok: results.every(value => value?.result?.ok === true),
    capacity,
    listener_count: 1,
    jobs: results
  };
};

globalThis.runParkedPool = globalThis.runParkedWorkerPool;
