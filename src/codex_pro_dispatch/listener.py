"""Idle start-or-replace transactions. Native stages supply task/turn identity."""
import json
import uuid

from . import core, resident
from .queue import Queue


def digest(value):
    return core.sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def snapshot(paths, locked):
    owner, pool = core.authority_snapshot(paths, _locked=locked)
    expected = {
        "config_dir": str(paths.config_dir), "state_dir": str(paths.state_dir),
        "owner_sha256": core._sha256_authority_file(paths.state_dir / "resident-owner.json"),
        "pool_sha256": pool.file_sha256 if pool else None,
        "pool_witness_sha256": core._sha256_authority_file(paths.worker_pool_file),
        "worker_witness_sha256": core._sha256_authority_file(paths.worker_file),
    }
    return owner, pool, expected


def idle(paths, locked, owner):
    if owner and owner.get("version") not in (3, 4):
        raise core.StateError("Scalar owner requires existing explicit migration")
    if owner and any(s["phase"] != "idle" or s["request"] is not None
                     or s["invocation"] is not None for s in owner["slots"]):
        raise core.BusyError("pool_not_idle: use existing recovery for the occupied request")
    claims = [r for r in Queue(paths).records(_locked=locked) if r["state"] == "claimed"]
    active = core.active_assignments(paths, _locked=locked)
    if claims or active:
        request = (claims[0]["request_id"] if claims else active[0]["assignment_id"])
        raise core.BusyError("request_recovery_required", details={"request_id": request})
    if owner and resident._recovery_marker(paths, owner) is not None:
        raise core.BusyError("recovery_owner_present: finish existing recovery")
    if core.active_cooldown(paths, _locked=locked):
        raise core.BusyError("cooldown: retry after the stored cooldown")


def plan(paths, workers, confirmation=None):
    if not 1 <= len(workers) <= 2 or len(set(workers)) != len(workers):
        raise core.ConfigurationError("One or two distinct exact worker IDs required")
    for worker in workers:
        core.validate_identifier(worker, field="worker")
    with core.state_lock(paths) as locked:
        owner, pool, expected = snapshot(paths, locked)
        existing = {w.conversation_id: w for w in pool.workers} if pool else {}
        same = pool is not None and [w.conversation_id for w in pool.workers] == workers
        entries = ([core.worker_entry_payload(w) for w in pool.workers] if same else [
            dict(core.worker_entry_payload(existing[worker]), slot=f"slot-{chr(97+i)}")
            if worker in existing else {
                "slot": f"slot-{chr(97+i)}", "conversation_id": worker,
                "label": f"Pro dispatch {i+1:02d}",
                "model_confirmation": "user-confirmed-worker", "configured_at": core.utc_now(),
            } for i, worker in enumerate(workers)])
        physical = None
        if confirmation is not None:
            if not isinstance(confirmation, str) or not confirmation.strip():
                raise core.ConfigurationError("Factual physical-quiescence confirmation required")
            physical = {
                "kind": "legacy_quiescence" if owner else "fresh_deployment",
                "physical_quiescence": True, "expected_owner_sha256": expected["owner_sha256"],
                "config_dir": str(paths.config_dir), "state_dir": str(paths.state_dir),
                "implementation": "listener-start profile 1",
                "observations": confirmation,
                "authorization": "Explicit listener start with factual quiescence confirmation",
            }
        return {"version": 1, "expected": expected, "workers": entries,
                "physical": physical, "same_workers": same,
                "current_owner": resident.operational(owner)}


def commit(paths, packet, parent, turn):
    """Private native-context entry, never a CLI commit accepting guessed identity."""
    core.validate_identifier(parent, field="parent")
    core.validate_identifier(turn, field="turn")
    if not isinstance(packet, dict) or packet.get("version") != 1:
        raise core.ConfigurationError("Invalid listener startup packet")
    expected = packet["expected"]
    if expected["config_dir"] != str(paths.config_dir) or expected["state_dir"] != str(paths.state_dir):
        raise core.StateError("Startup authority paths differ")
    target = {"schema_version": 1, "workers": packet["workers"],
              "legacy_worker_sha256": expected["worker_witness_sha256"]}
    raw = json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    pool = core._pool_from_value(target, file_sha256=core.sha256_text(raw))
    operation = digest({"protocol": 1, "expected": expected, "target": target,
                        "parent": parent, "turn": turn})
    with core.state_lock(paths) as locked:
        core.reservation_guard(paths, locked)
        owner, _, actual = snapshot(paths, locked)
        startup = owner.get("qualification", {}).get("startup", {}) if owner else {}
        if (startup.get("operation_id") == operation and owner is not None
                and startup.get("acquired") == {k: owner[k] for k in ("generation", "owner", "parent")}):
            return {"ok": True, "state": "next_action", "reason": "already_committed",
                    "owner": resident.operational(owner), "operation_id": operation,
                    "send_authorized": False}
        if actual != expected:
            return {"ok": True, "state": "stale", "reason": "expected_state_changed",
                    "send_authorized": False}
        idle(paths, locked, owner)
        # Check before constructing authority. The writer independently checks again.
        resident.exclusion_proof(paths, locked, owner, packet.get("physical"))
        qualification = dict(owner["qualification"]) if owner else {}
        qualification.update(pool_witness_sha256=expected["pool_witness_sha256"], startup={
            "operation_id": operation, "predecessor": expected,
            "target_digest": pool.file_sha256, "native_task": parent, "native_turn": turn,
        })
        value = {"version": 4, "generation": owner["generation"]+1 if owner else 1,
                 "owner": uuid.uuid4().hex, "parent": parent,
                 "worker_pool_json": raw, "worker_pool_sha256": pool.file_sha256,
                 "slots": [{"slot": w.slot, "worker_conversation_id": w.conversation_id,
                            "request": None, "invocation": None, "phase": "idle"}
                           for w in pool.workers],
                 "session": None, "qualification": qualification,
                 "send_fence": {"profile": 1, "armed_since_barrier": False}}
        qualification["startup"]["acquired"] = {k: value[k] for k in ("generation", "owner", "parent")}
        resident.write(paths, locked, value, physical=packet.get("physical"))
        return {"ok": True, "state": "next_action", "reason": "owner_acquired",
                "owner": resident.operational(value), "operation_id": operation,
                "send_authorized": False}
