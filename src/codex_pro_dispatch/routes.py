"""Small canonical destination registry, protected by the existing state lock.

Like native_storage, this is cooperative same-UID authority, not protection from
arbitrary code rewriting private files. No transport or independent state exists.
"""
import os
import secrets
import re
from . import core

BRIDGE_FIELDS = frozenset({"queue_claim_token", "worker_slot", "owner_generation", "safety_observer"})
IMMUTABLE = ("execution_route", "direct_binding_id", "worker_conversation_id", "parent_task_id",
             "queue_claim_token", "worker_slot", "owner_generation", "safety_observer")


def path(paths):
    return paths.config_dir / "dispatch-routes.json"


def _ids(values, field):
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise core.StateError("dispatch_routes_invalid: " + field)
    for value in values:
        core.validate_identifier(value, field=field)
    if len(set(values)) != len(values):
        raise core.StateError("dispatch_routes_invalid: duplicate " + field)


def validate(value):
    if (not isinstance(value, dict) or set(value) != {"version", "direct_worker", "direct_bindings", "legacy_standalone_workers", "bridge_workers", "listener_tasks"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise core.StateError("dispatch_routes_invalid: schema")
    _ids(value["bridge_workers"], "bridge_workers")
    _ids(value["listener_tasks"], "listener_tasks")
    _ids(value["legacy_standalone_workers"], "legacy_standalone_workers")
    bindings = value["direct_bindings"]
    if not isinstance(bindings, list):
        raise core.StateError("direct_worker_invalid: history")
    seen = set()
    for worker in bindings:
        if (not isinstance(worker, dict) or set(worker) != {"binding_id", "conversation_id", "label",
                                                          "model_confirmation", "configured_at"}):
            raise core.StateError("direct_worker_invalid: schema")
        for field in ("binding_id", "conversation_id"):
            core.validate_identifier(worker[field], field=field)
        if worker["binding_id"] in seen:
            raise core.StateError("direct_worker_invalid: duplicate binding")
        seen.add(worker["binding_id"])
        if (not isinstance(worker["label"], str) or not worker["label"].strip()
                or len(worker["label"]) > 120 or worker["model_confirmation"] != "user-confirmed-worker"
                or not isinstance(worker["configured_at"], str)):
            raise core.StateError("direct_worker_invalid: confirmation, timestamp or label")
        core._parse_utc(worker["configured_at"], field="configured_at")
        if worker["conversation_id"] in value["bridge_workers"] or worker["conversation_id"] in value["legacy_standalone_workers"]:
            raise core.StateError("dispatch_route_overlap")
    if value["direct_worker"] is not None and value["direct_worker"] not in bindings:
        raise core.StateError("direct_worker_invalid: current binding not retained")
    return value


def read(paths, locked):
    locked.validate(paths)
    if not os.path.lexists(path(paths)):
        return None
    try:
        return validate(core.read_json(path(paths)))
    except (core.DispatchError, TypeError, KeyError, ValueError) as exc:
        raise core.StateError("dispatch_routes_integrity: invalid or unsafe route registry") from exc


def initial(paths, locked):
    """Bootstrap only from validated canonical witnesses; never infer a direct chat."""
    from . import resident
    owner = resident.read(paths, locked)
    _, pool = core.authority_snapshot(paths, _locked=locked)
    workers = set(w.conversation_id for w in pool.workers) if pool else set()
    if os.path.lexists(paths.worker_file):
        workers.add(core.load_worker(paths, _locked=locked).conversation_id)
    standalone = sorted(workers) if owner is None and pool is None else []
    tasks = {owner["parent"]} if owner else set()
    if owner:
        workers.update(owner.get("send_fence", {}).get("unexcluded_workers", []))
        if owner.get("worker"):
            workers.add(owner["worker"])
    # Retained queue associations preserve every historically exposed bridge
    # destination, including published and acknowledged work.
    from .queue import Queue
    for row in Queue(paths).records(_locked=locked):
        if row["state"] not in {"queued", "cancelled"}:
            workers.add(core.validate_identifier(row.get("worker_conversation_id"), field="worker"))
    for receipt in core.list_assignments(paths, _locked=locked):
        if receipt.get("execution_route") == "direct" or "direct_binding_id" in receipt:
            raise core.StateError("dispatch_routes_missing: direct receipt survives")
        classification = route(receipt, paths, locked, registry=None)
        if classification == "legacy" and receipt["worker_conversation_id"] not in standalone and receipt["status"] in core.ACTIVE_STATUSES:
            raise core.StateError("Unresolved receipt lacks determinable legacy binding")
        if classification == "bridge" or receipt["status"] in core.ACTIVE_STATUSES:
            workers.add(receipt["worker_conversation_id"])
    return validate({"version": 1, "direct_worker": None,
                     "direct_bindings": [], "legacy_standalone_workers": standalone,
                     "bridge_workers": sorted(workers), "listener_tasks": sorted(tasks)})


def register_bridge(paths, locked, workers=(), tasks=()):
    registry = read(paths, locked) or initial(paths, locked)
    updated = dict(registry,
                   bridge_workers=sorted(set(registry["bridge_workers"]) | set(workers)),
                   listener_tasks=sorted(set(registry["listener_tasks"]) | set(tasks)))
    validate(updated)
    if updated != registry or not os.path.lexists(path(paths)):
        core.atomic_write_json(path(paths), updated, _locked=locked)
    return updated


def route(receipt, paths, locked, *, registry=...):
    """Validate immutable provenance without consulting bridge lifecycle if registered."""
    if registry is ...:
        registry = read(paths, locked)
    name = receipt.get("execution_route")
    if "execution_route" in receipt and name not in ("direct", "bridge"):
        raise core.StateError("Invalid receipt execution_route")
    bridge = any(key in receipt for key in BRIDGE_FIELDS)
    historical = (name is None and "execution_route" not in receipt
                  and "direct_binding_id" not in receipt and not bridge
                  and receipt.get("status") in core.TERMINAL_STATUSES)
    if historical and (receipt.get("worker_conversation_id") is None
                       or receipt.get("parent_task_id") is None):
        return "historical"
    worker = core.validate_identifier(receipt.get("worker_conversation_id"), field="worker_conversation_id")
    core.validate_identifier(receipt.get("parent_task_id"), field="parent_task_id")
    if name == "direct":
        binding = core.validate_identifier(receipt.get("direct_binding_id"), field="direct_binding_id")
        direct = next((b for b in registry["direct_bindings"] if b["binding_id"] == binding), None) if registry else None
        if bridge or registry is None or worker in registry["bridge_workers"]:
            raise core.StateError("Invalid direct receipt provenance")
        if direct is None or direct["conversation_id"] != worker:
            raise core.StateError("direct_worker_binding_mismatch")
        return "direct"
    if "direct_binding_id" in receipt:
        raise core.StateError("Invalid receipt direct_binding_id")
    if name == "bridge" or bridge:
        if "safety_observer" in receipt and (not isinstance(receipt["safety_observer"], str) or not re.fullmatch(r"[a-f0-9]{64}", receipt["safety_observer"])):
            raise core.StateError("Invalid original safety observer")
        if ("worker_slot" in receipt) != ("owner_generation" in receipt):
            raise core.StateError("Incomplete bridge slot provenance")
        if "worker_slot" in receipt:
            core.validate_identifier(receipt["worker_slot"], field="worker_slot")
        if "owner_generation" in receipt and (type(receipt["owner_generation"]) is not int
                                               or receipt["owner_generation"] < 0):
            raise core.StateError("Invalid bridge receipt generation")
        if "queue_claim_token" in receipt and (not isinstance(receipt["queue_claim_token"], str)
                                                or not receipt["queue_claim_token"]):
            raise core.StateError("Invalid bridge receipt claim")
        if not any(key in receipt for key in ("queue_claim_token", "worker_slot", "owner_generation")):
            raise core.StateError("Bridge receipt lacks provenance")
        if registry and worker not in registry["bridge_workers"]:
            raise core.StateError("Bridge receipt worker is not registered")
        from .queue import Queue
        request = core.validate_identifier(receipt.get("assignment_id"), field="assignment_id")
        row = Queue(paths).load(request, _locked=locked)
        if row["state"] in {"queued", "cancelled"}:
            raise core.StateError("Bridge receipt lacks a retained queue association")
        for key in ("parent_task_id", "worker_conversation_id", "queue_claim_token",
                    "worker_slot", "owner_generation", "prompt_sha256", "wrapped_prompt_sha256", "result_protocol"):
            if row.get(key) != receipt.get(key):
                raise core.StateError("Bridge receipt provenance differs from canonical claim")
        return "bridge"
    # Pre-registry standalone receipts retain their validated legacy binding.
    if registry:
        if worker not in registry["legacy_standalone_workers"]:
            if receipt.get("status") in core.TERMINAL_STATUSES:
                return "historical"
            raise core.StateError("Invalid legacy receipt binding")
    elif historical and not os.path.lexists(paths.worker_file):
        return "historical"
    elif core.load_worker(paths, _locked=locked).conversation_id != worker:
        if receipt.get("status") in core.TERMINAL_STATUSES:
            return "historical"
        raise core.StateError("Invalid legacy receipt binding")
    return "legacy"


def guard(paths, locked, assignment_id, *, value=None, operation=None):
    from . import resident
    if value is None:
        value = core.load_assignment(assignment_id, paths, _locked=locked)
    name = route(value, paths, locked)
    if name == "historical":
        raise core.StateError("legacy_read_only: historical binding is not known")
    if name == "legacy" and read(paths, locked) is not None:
        if operation in {"armed", "arm"} or value.get("status") == "prepared" and operation != "abandoned":
            raise core.StateError("legacy_collect_only: cannot arm or resend migrated standalone work")
        if resident.invocation.get() is not None:
            raise core.StateError("Resident invocation cannot mutate legacy standalone work")
        return name
    if name == "direct":
        if resident.invocation.get() is not None:
            raise core.StateError("Resident invocation cannot mutate direct work")
    else:
        resident.guard(paths, locked, assignment_id, operation=operation)
    return name


def set_direct(paths, conversation_id, *, label="Codex Direct Pro", confirm_worker=False,
               expected_conversation_id=None):
    core.validate_identifier(conversation_id, field="conversation_id")
    if not confirm_worker:
        raise core.ConfigurationError("Direct worker requires --confirm-worker")
    from .resident import invocation
    if invocation.get() is not None:
        raise core.StateError("Direct configuration requires an ordinary native caller")
    with core.state_lock(paths) as locked:
        registry = read(paths, locked) or initial(paths, locked)
        current = registry["direct_worker"]
        if current and expected_conversation_id != current["conversation_id"]:
            raise core.StateError("Direct replacement requires exact expected_conversation_id")
        if expected_conversation_id is not None and (not current or current["conversation_id"] != expected_conversation_id):
            raise core.StateError("Direct worker does not match expected_conversation_id")
        for receipt in core.list_assignments(paths, _locked=locked):
            if route(receipt, paths, locked, registry=registry) == "direct" and receipt["status"] in core.ACTIVE_STATUSES:
                raise core.BusyError("Direct worker has unresolved work", details={"assignment_id": receipt["assignment_id"]})
        if current and conversation_id == current["conversation_id"]:
            return current
        worker = {"binding_id": secrets.token_hex(16), "conversation_id": conversation_id,
                  "label": label.strip(), "model_confirmation": "user-confirmed-worker", "configured_at": core.utc_now()}
        updated = validate(dict(registry, direct_worker=worker, direct_bindings=[*registry["direct_bindings"], worker]))
        core.atomic_write_json(path(paths), updated, _locked=locked)
        return worker


def reset_direct(paths):
    from .resident import invocation
    if invocation.get() is not None:
        raise core.StateError("Direct configuration requires an ordinary native caller")
    with core.state_lock(paths) as locked:
        registry = read(paths, locked)
        if not registry:
            return False
        for receipt in core.list_assignments(paths, _locked=locked):
            if route(receipt, paths, locked, registry=registry) == "direct" and receipt["status"] in core.ACTIVE_STATUSES:
                raise core.BusyError("Direct worker has unresolved work", details={"assignment_id": receipt["assignment_id"]})
        if registry["direct_worker"] is None:
            return False
        core.atomic_write_json(path(paths), dict(registry, direct_worker=None), _locked=locked)
        return True


def stop_check(paths, parent):
    core.validate_identifier(parent, field="parent_task_id")
    try:
        with core.state_lock(paths, create=False) as locked:
            registry = read(paths, locked)
    except FileNotFoundError:
        # A never-configured installation has no lock to acquire. Validate any
        # existing directories without creating state, then retain legacy flow.
        from .native_storage import Directory
        for directory in (paths.config_dir, paths.state_dir):
            if os.path.lexists(directory):
                with Directory(directory.absolute()):
                    pass
        if os.path.lexists(path(paths)):
            raise core.StateError("dispatch_routes_integrity: canonical lock is missing")
        registry = None
    state = "legacy" if registry is None else "registered" if parent in registry["listener_tasks"] else "unrelated"
    return {"ok": True, "state": state,
            "paths": {"config_dir": str(paths.config_dir), "state_dir": str(paths.state_dir)}}


def status(paths):
    with core.state_lock(paths, create=False) as locked:
        registry = read(paths, locked)
        worker = registry["direct_worker"] if registry else None
        active = core.active_assignments(paths, execution_route="direct", _locked=locked)
        cooldown = core.active_cooldown(paths, _locked=locked)
        return {"ok": worker is not None, "local_ok": worker is not None, "execution_route": "direct", "worker": worker,
                "active_assignment": active[0] if active else None, "active_assignments": active,
                "active_cooldown": cooldown,
                "next_action": "worker direct-set --conversation-id <exact-chat-id> --confirm-worker --native-controls-confirmed" if worker is None else
                               "collect existing assignment" if active else "perform actual caller native preflight"}


def observer_identity(caller, receipt, request):
    """Original runner binding within the existing cooperative same-UID model."""
    if not isinstance(caller, dict) or caller.get("collector_only") or caller.get("takeover_settlement"):
        raise core.StateError("Original safety observer required")
    for key in ("owner", "parent", "invocation", "request"):
        core.validate_identifier(caller.get(key), field=key)
    if (type(caller.get("generation")) is not int or caller["generation"] < 1
            or caller["request"] != request or caller["parent"] != receipt.get("parent_task_id")
            or caller.get("worker") not in {None, receipt.get("worker_conversation_id")}
            or caller.get("slot") not in {None, receipt.get("worker_slot")}
            or receipt.get("owner_generation") is not None and caller["generation"] != receipt["owner_generation"]):
        raise core.StateError("Original safety observer identity differs")
    identity = {key: caller[key] for key in ("generation", "owner", "parent", "invocation", "request")}
    identity.update(worker=receipt["worker_conversation_id"], slot=receipt.get("worker_slot"),
                    claim=receipt.get("queue_claim_token"))
    if not isinstance(identity["claim"], str) or not identity["claim"]:
        raise core.StateError("Original safety observer claim missing")
    from .native_storage import canonical
    return core.sha256_text(canonical(identity).decode("utf-8"))


def original_observer(paths, locked, receipt):
    """Permit only a retained exact observer after missing/corrupt lifecycle."""
    from . import resident
    from .queue import Queue
    if route(receipt, paths, locked) != "bridge":
        raise core.StateError("Original bridge safety observer required")
    caller = resident.invocation.get()
    observed = observer_identity(caller, receipt, receipt["assignment_id"])
    if receipt.get("safety_observer") != observed:
        raise core.StateError("Original safety observer identity differs")
    row = Queue(paths).load(receipt["assignment_id"], _locked=locked)
    if row["state"] != "claimed":
        raise core.StateError("Original safety observer claim is no longer active")
    for key in ("parent_task_id", "worker_conversation_id", "worker_slot", "owner_generation",
                "queue_claim_token", "prompt_sha256", "wrapped_prompt_sha256", "result_protocol"):
        if row.get(key) != receipt.get(key):
            raise core.StateError("Original safety observer claim differs")
    try:
        current = resident.read(paths, locked)
    except (core.DispatchError, OSError, ValueError):
        return
    if current is not None:
        # A readable authority rejection is not lifecycle corruption, including
        # valid replacement generations and slots that no longer belong to us.
        resident.guard(paths, locked, receipt["assignment_id"])
