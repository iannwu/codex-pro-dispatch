// @exec: {"yield_time_ms":1000}
globalThis.runParkedJob = async function runParkedJob(config, requestId) {
  const identifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  const validPath = value =>
    typeof value === "string" && value.startsWith("/") &&
    !/[\0\r\n]/.test(value);
  if (!identifier.test(requestId) ||
      !identifier.test(config.parent) ||
      !identifier.test(config.worker) ||
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

  const quote = value => "'" + String(value).replace(/'/g, "'\\''") + "'";
  const trace = [];
  let phase = "collect", directory = null, sequence = 0;
  let potentiallyArmed = false, published = false, nativeUsed = false;
  let pendingCommand = null, result;

  function problem(message, detail) {
    const error = Error(message);
    error.detail = detail;
    return error;
  }

  function nativeText(value) {
    if (!value || value.isError === true ||
        !Array.isArray(value.content) || value.content.length !== 1 ||
        value.content[0].type !== "text" ||
        typeof value.content[0].text !== "string") {
      throw problem("Unsupported or failed native tool result", value);
    }
    return value.content[0].text;
  }

  function bound(value) {
    if (value.request_id !== requestId ||
        value.parent_task_id !== config.parent ||
        value.worker_conversation_id !== config.worker) {
      throw problem("Broker identity mismatch", value);
    }
    return value;
  }

  async function helper(args) {
    const command = ["python3", config.helper, ...args].map(quote).join(" ");
    trace.push({ kind: "helper", operation: args.slice(0, 2), at: Date.now() });
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
console.log(JSON.stringify({path}));
}`;
    const saved = JSON.parse(nativeText(await tools.mcp__node_repl__js({
      code, timeout_ms: 30000, title: "Preserve native evidence"
    })));
    if (saved.path !== path) throw Error("Evidence path mismatch");
    return path;
  }

  async function read() {
    nativeUsed = true;
    const startedAt = Date.now();
    const response = await tools.mcp__codex_app__read_thread({
      threadId: config.worker, turnLimit: 2, maxOutputCharsPerItem: 20000
    });
    const raw = nativeText(response);
    const path = await evidence(raw);
    let value;
    try { value = JSON.parse(raw); }
    catch { throw Error("Native inner history is not JSON"); }
    if (value.schemaVersion !== 1 ||
        value.thread?.kind !== "chatgpt" ||
        value.thread?.id !== config.worker) {
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

  try {
    const authority = await helper(["status", "--current"]);
    if (authority.paths?.config_dir !== config.configDir ||
        authority.paths?.state_dir !== config.stateDir)
      throw Error("Native runner authority paths differ from trusted session");
    const existing = await helper(["queue", "collect", requestId]);
    if (["published", "acknowledged"].includes(existing.state)) {
      bound(existing);
      published = existing.state === "published";
      result = existing;
    } else if (config.collectOnly === true &&
               existing.send_may_have_occurred !== true) {
      result = { ...existing, observation: "not_submitted" };
    } else {
      if (authority.worker?.conversation_id !== config.worker)
        throw Error("Native runner worker differs from trusted session");
      phase = "claim";
      const claim = bound(await helper([
        "queue", "claim", "--request-id", requestId,
        "--expected-worker-conversation-id", config.worker,
        "--parent-task-id", config.parent, "--native-controls-confirmed"
      ]));
      if (claim.assignment_id !== requestId ||
          !["arm_then_send_once", "collect_only"].includes(claim.action)) {
        throw problem("Unexpected claim result", claim);
      }

      if (claim.action === "arm_then_send_once") {
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
        const armed = await helper(["arm", requestId]);
        const receipt = armed.assignment;
        if (!receipt || receipt.assignment_id !== requestId ||
            receipt.parent_task_id !== config.parent ||
            receipt.worker_conversation_id !== config.worker ||
            receipt.status !== "armed" || receipt.no_resend !== true) {
          throw Error("Arm result does not authorize this exact operation");
        }

        phase = "send";
        nativeUsed = true;
        trace.push({ kind: "send_attempt", at: Date.now() });
        const sent = await tools.mcp__codex_app__send_message_to_thread({
          threadId: config.worker, prompt: claim.wrapped_prompt
        });
        await evidence(JSON.stringify(sent));
        if (!sent || sent.isError === true) {
          throw problem("Native send needs diagnostic review; never resend", sent);
        }
      } else {
        potentiallyArmed = true;
      }

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
  } catch (error) {
    result = {
      ok: false, request_id: requestId, observation: "blocked",
      failed_phase: phase, error: String(error.message || error),
      no_resend: potentiallyArmed, diagnostic_review_required: true
    };
    try {
      result.error_evidence = await evidence(JSON.stringify({
        phase, error: String(error.message || error), detail: error.detail ?? null
      }));
    } catch { result.evidence_write_failed = true; }

    // Preserve post-arm uncertainty, but never alter a completed receipt.
    if (potentiallyArmed && !published && pendingCommand === null) {
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
      ...result, request_id: requestId, restoration,
      evidence_directory: directory, evidence_retained: directory !== null,
      pending_helper_session: pendingCommand, trace
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
      error: String(error.message || error)
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
  const transport = await tools.mcp__node_repl__js({
    code, timeout_ms: 30000, title: "Finish native socket delivery"
  });
  return { result, transport };
};
