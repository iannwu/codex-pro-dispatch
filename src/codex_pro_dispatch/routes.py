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
DIRECT_LABELS = ("Pro Dispatch - Codex 01", "Pro Dispatch - Codex 02")


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
    fields = {"version", "direct_bindings", "legacy_standalone_workers", "bridge_workers", "listener_tasks"}
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise core.StateError("dispatch_routes_invalid: schema")
    if value["version"] == 1 and set(value) == fields | {"direct_worker"}:
        # Normalize in memory only. Read commands never rewrite legacy bytes.
        value = {key: item for key, item in value.items() if key != "direct_worker"} | {
            "version": 2, "direct_workers": [] if value["direct_worker"] is None else [value["direct_worker"]]}
    elif value["version"] != 2 or set(value) != fields | {"direct_workers"}:
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
    current = value["direct_workers"]
    if not isinstance(current, list) or len(current) > 2:
        raise core.StateError("direct_worker_invalid: active worker count")
    if any(worker not in bindings for worker in current):
        raise core.StateError("direct_worker_invalid: current binding not retained")
    if len({worker["conversation_id"] for worker in current}) != len(current):
        raise core.StateError("direct_worker_invalid: duplicate active conversation")
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
    return validate({"version": 2, "direct_workers": [],
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


def _configure_direct(paths, conversation_ids, labels, expected_ids, *, confirm_worker, singleton=False):
    _ids(conversation_ids, "conversation_id")
    _ids(expected_ids, "expected_conversation_id")
    if len(labels) != len(conversation_ids) or any(
            not isinstance(label, str) or not label.strip() or len(label) > 120 for label in labels):
        raise core.ConfigurationError("Direct configuration requires one nonempty label per conversation")
    labels = [label.strip() for label in labels]
    if not confirm_worker:
        raise core.ConfigurationError("Direct worker requires --confirm-worker")
    from .resident import invocation
    if invocation.get() is not None:
        raise core.StateError("Direct configuration requires an ordinary native caller")
    with core.state_lock(paths) as locked:
        registry = read(paths, locked) or initial(paths, locked)
        current = registry["direct_workers"]
        if singleton and len(current) == 2:
            raise core.ConfigurationError("direct_pool_configured: use worker direct-pool-set")
        if expected_ids != [worker["conversation_id"] for worker in current]:
            raise core.StateError("Direct replacement requires exact ordered expected_conversation_id list")
        # Validate all canonical receipts before diagnosing busy or idempotence.
        active = core.active_assignments(paths, execution_route="direct", _locked=locked)
        requested = list(zip(conversation_ids, labels))
        if requested == [(worker["conversation_id"], worker["label"]) for worker in current]:
            return current
        workers = []
        added = []
        for conversation_id, label in requested:
            worker = next((w for w in current if w["conversation_id"] == conversation_id and w["label"] == label), None)
            if worker is None:
                worker = {"binding_id": secrets.token_hex(16), "conversation_id": conversation_id,
                          "label": label, "model_confirmation": "user-confirmed-worker", "configured_at": core.utc_now()}
                added.append(worker)
            workers.append(worker)
        updated = validate(dict(registry, direct_workers=workers, direct_bindings=[*registry["direct_bindings"], *added]))
        if active:
            raise core.BusyError("Direct workers have unresolved work", details=capacity(registry, active))
        core.atomic_write_json(path(paths), updated, _locked=locked)
        return workers


def set_direct(paths, conversation_id, *, label="Codex Direct Pro", confirm_worker=False,
               expected_conversation_id=None):
    return _configure_direct(paths, [conversation_id], [label],
                             [] if expected_conversation_id is None else [expected_conversation_id],
                             confirm_worker=confirm_worker, singleton=True)[0]


def set_direct_pool(paths, conversation_ids, *, labels=None, confirm_worker=False,
                    expected_conversation_ids=None):
    if len(conversation_ids) != 2 or labels is not None and len(labels) != 2:
        raise core.ConfigurationError("direct_pool_invalid: requires exactly two conversation IDs and zero or two labels")
    return _configure_direct(paths, conversation_ids, list(DIRECT_LABELS) if labels is None else labels,
                             [] if expected_conversation_ids is None else expected_conversation_ids,
                             confirm_worker=confirm_worker)


def reset_direct(paths):
    from .resident import invocation
    if invocation.get() is not None:
        raise core.StateError("Direct configuration requires an ordinary native caller")
    with core.state_lock(paths) as locked:
        registry = read(paths, locked)
        if not registry:
            return False
        active = core.active_assignments(paths, execution_route="direct", _locked=locked)
        if active:
            raise core.BusyError("Direct workers have unresolved work", details=capacity(registry, active))
        if not registry["direct_workers"]:
            return False
        core.atomic_write_json(path(paths), dict(registry, direct_workers=[]), _locked=locked)
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


def capacity(registry, active):
    """Capacity before account cooldown, including retained retired bindings."""
    workers = registry["direct_workers"] if registry else []
    occupied = {value["worker_conversation_id"] for value in active}
    available = [worker for worker in workers if worker["conversation_id"] not in occupied]
    remaining = min(len(available), max(0, 2 - len(active)))
    return {"worker_count": len(workers), "workers": workers,
            "worker": workers[0] if len(workers) == 1 else None,
            "active_assignment": active[0] if len(active) == 1 else None,
            "active_assignments": active, "available_workers": available,
            "remaining_capacity": remaining,
            "availability": "not_configured" if not workers else "ready" if remaining else "busy"}


def status(paths):
    try:
        with core.state_lock(paths, create=False) as locked:
            registry = read(paths, locked)
            active = core.active_assignments(paths, execution_route="direct", _locked=locked)
            cooldown = core.active_cooldown(paths, _locked=locked)
    except FileNotFoundError:
        from .native_storage import Directory
        for directory in (paths.config_dir, paths.state_dir):
            if os.path.lexists(directory):
                with Directory(directory.absolute()) as checked:
                    if os.listdir(checked.fd):
                        raise core.StateError("dispatch_routes_integrity: canonical lock is missing")
        registry, active, cooldown = None, [], None
    details = capacity(registry, active)
    return {"ok": bool(details["worker_count"]), "local_ok": bool(details["worker_count"]),
            "execution_route": "direct", **details,
            "active_cooldown": cooldown,
            "next_action": "worker direct-pool-set --conversation-id <chat-01-id> --conversation-id <chat-02-id> --confirm-worker --native-controls-confirmed" if not details["worker_count"] else
                           "wait for account cooldown" if cooldown else
                           "collect existing assignment" if not details["remaining_capacity"] else "perform actual caller native preflight"}


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
