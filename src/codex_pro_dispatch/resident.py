"""One canonical resident owner. No transport, timer, or send capability."""
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import hashlib
import json
import os
import re
import socket
import stat
import time
import uuid
from pathlib import Path

from . import core
from .native_storage import IntegrityError


invocation = ContextVar("resident_invocation", default=None)
collector_operation = ContextVar("resident_collector_operation", default=None)
settlement_operation = ContextVar("resident_settlement_operation", default=None)


@contextmanager
def collector_observation():
    """Temporarily permit the one existing-receipt publication path."""
    token = collector_operation.set("observe")
    try:
        yield
    finally:
        collector_operation.reset(token)


def read(paths, locked):
    locked.validate(paths)
    path = paths.state_dir / "resident-owner.json"
    if not os.path.lexists(path):
        return None
    v = core.read_json(path)
    if v.get("version") in (3, 4):
        return core.authority_snapshot(paths, _locked=locked)[0]
    keys = {"version", "generation", "owner", "parent", "worker", "inflight", "qualification"}
    if (type(v.get("version")) is not int or v["version"] not in (1, 2)
            or set(v) != (keys | {"session"} if v["version"] == 2 else keys)
            or type(v["generation"]) is not int or v["generation"] < 1):
        raise core.StateError("Invalid resident owner record; preserve it")
    if v.get("session") is not None:
        validate_session(v["session"])
    if not isinstance(v["qualification"], dict):
        raise core.StateError("Missing resident enrollment evidence")
    for k in ("owner", "parent", "worker"):
        core.validate_identifier(v[k], field=k)
    if v["inflight"] is not None:
        f = v["inflight"]
        if not isinstance(f, dict) or set(f) != {"invocation", "request"}:
            raise core.StateError("Invalid resident invocation")
        for k in f:
            core.validate_identifier(f[k], field=k)
    if core.load_worker(paths, _locked=locked).conversation_id != v["worker"]:
        raise core.StateError("Resident worker configuration changed")
    return v


def validate_pool_owner(v, pool):
    """Pure schema validator. Never load authority or acquire a lock here."""
    expected = {"version", "generation", "owner", "parent", "worker_pool_sha256",
                "slots", "session", "qualification"}
    if v.get("version") == 4:
        expected |= {"worker_pool_json", "send_fence"}
        fence = v.get("send_fence")
        if (not isinstance(fence, dict) or set(fence) != {"profile", "armed_since_barrier"}
                or type(fence.get("profile")) is not int or fence["profile"] != 1
                or type(fence.get("armed_since_barrier")) is not bool):
            raise core.StateError("Invalid listener send fence; preserve evidence")
    if set(v) != expected or type(v.get("generation")) is not int or v["generation"] < 1:
        raise core.StateError("Invalid resident pool owner record; preserve it")
    for field in ("owner", "parent"):
        core.validate_identifier(v.get(field), field=field)
    if (not isinstance(v.get("worker_pool_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", v["worker_pool_sha256"])):
        raise core.StateError("Invalid resident pool hash")
    if pool.file_sha256 != v["worker_pool_sha256"]:
        raise core.StateError("Resident pool hash differs")
    if not isinstance(v.get("qualification"), dict):
        raise core.StateError("Missing resident enrollment evidence")
    if v.get("session") is not None:
        validate_session(v["session"])
    if not isinstance(v.get("slots"), list) or len(v["slots"]) != len(pool.workers):
        raise core.StateError("Resident pool slots do not match configured capacity")
    allowed = {"slot", "worker_conversation_id", "request", "invocation", "phase"}
    seen_slots = set()
    seen_workers = set()
    by_slot = {worker.slot: worker for worker in pool.workers}
    for index, raw in enumerate(v["slots"]):
        if not isinstance(raw, dict) or set(raw) != allowed:
            raise core.StateError("Invalid resident pool slot record")
        slot = raw.get("slot")
        worker_id = raw.get("worker_conversation_id")
        configured = pool.workers[index]
        if (slot != configured.slot or worker_id != configured.conversation_id
                or slot not in by_slot or by_slot[slot].conversation_id != worker_id):
            raise core.StateError("Resident pool slot worker differs from configuration")
        if slot in seen_slots or worker_id in seen_workers:
            raise core.StateError("Resident pool contains duplicate slot binding")
        seen_slots.add(slot)
        seen_workers.add(worker_id)
        if raw["request"] is not None:
            core.validate_identifier(raw["request"], field="request")
        if raw["invocation"] is not None:
            f = raw["invocation"]
            if not isinstance(f, dict) or set(f) != {"invocation", "request"}:
                raise core.StateError("Invalid resident pool invocation")
            core.validate_identifier(f["invocation"], field="invocation")
            core.validate_identifier(f["request"], field="request")
            if raw["request"] != f["request"]:
                raise core.StateError("Resident pool invocation request differs")
        if raw["phase"] not in {"idle", "reserved", "collect_only", "running", "cancel_pending"}:
            raise core.StateError("Invalid resident pool slot phase")
        if raw["request"] is None and raw["invocation"] is not None:
            raise core.StateError("Resident pool invocation lacks request")
        if raw["request"] is None and raw["phase"] != "idle":
            raise core.StateError("Unoccupied resident pool slot is not idle")
        if raw["request"] is not None and raw["phase"] == "idle":
            raise core.StateError("Occupied resident pool slot is idle")
        if raw["phase"] == "cancel_pending" and raw["invocation"] is not None:
            raise core.StateError("Cancellation-pending resident slot has an invocation")
        if raw["phase"] == "collect_only" and raw["invocation"] is not None:
            raise core.StateError("Collector-only resident slot has an invocation")
    return v


def validate_session(s):
    if (not isinstance(s, dict) or set(s) != {"directory", "session_id", "descriptor_sha256"}
            or not isinstance(s["directory"], str) or not s["directory"].startswith("/")
            or "\0" in s["directory"]
            or not isinstance(s["session_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", s["session_id"])
            or not isinstance(s["descriptor_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", s["descriptor_sha256"])):
        raise core.StateError("Invalid canonical session binding")


def _bound_session_location(owner):
    session = owner.get("session") if isinstance(owner, dict) else None
    if session is None:
        return None, None
    validate_session(session)
    directory = Path(session["directory"])
    try:
        resolved = directory.resolve(strict=True)
        info = directory.lstat()
    except OSError:
        return session, None
    if (str(resolved) != session["directory"] or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid()):
        raise core.StateError("Bound resident session is not private physical storage")
    return session, directory


def _require_bound_descriptor(paths, owner, session, directory):
    from .queue import read_private
    try:
        raw_descriptor = read_private(directory / "session.json", limit=16384)
    except (FileNotFoundError, core.DispatchError) as exc:
        raise core.StateError(
            "Bound resident session lacks durable closure evidence"
        ) from exc
    if hashlib.sha256(raw_descriptor).hexdigest() != session["descriptor_sha256"]:
        raise core.StateError("Bound resident session descriptor changed")
    try:
        descriptor = json.loads(raw_descriptor)
    except (ValueError, UnicodeError) as exc:
        raise core.StateError("Bound resident closure evidence is not valid JSON") from exc
    if not isinstance(descriptor, dict) or descriptor.get("resident") is not True:
        raise core.StateError("Bound resident descriptor is not a resident session")
    if (descriptor.get("sessionId") != session["session_id"]
            or descriptor.get("parent") != owner.get("parent")
            or descriptor.get("configDir") != str(paths.config_dir)
            or descriptor.get("stateDir") != str(paths.state_dir)):
        raise core.StateError("Bound resident descriptor identity differs")
    return raw_descriptor, descriptor


def _require_closure_audit(session, directory):
    from .queue import read_private
    try:
        raw_audit = read_private(directory / "transport-audit.json", limit=1048576)
    except (FileNotFoundError, core.DispatchError) as exc:
        raise core.StateError(
            "Bound resident session lacks durable closure evidence"
        ) from exc
    try:
        audit = json.loads(raw_audit)
    except (ValueError, UnicodeError) as exc:
        raise core.StateError("Bound resident closure evidence is not valid JSON") from exc
    if not isinstance(audit, dict) or set(audit) != {"sessionId", "reason", "events"}:
        raise core.StateError("Bound resident closure audit shape differs")
    if (audit["sessionId"] != session["session_id"]
            or not isinstance(audit["reason"], str) or not audit["reason"]
            or not isinstance(audit["events"], list)):
        raise core.StateError("Bound resident closure audit is not durable")
    if os.path.lexists(directory / "wake.sock"):
        raise core.StateError("Bound resident session still has a listener")


def _unix_listener_is_live(path):
    """True only when a process currently accepts on the bound wake socket."""
    if not os.path.lexists(path):
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.2)
        sock.connect(str(path))
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError as exc:
        if exc.errno in {errno.ECONNREFUSED, errno.ENOENT, errno.ECONNRESET,
                         errno.EPIPE, errno.EINVAL, getattr(errno, "ENOTSOCK", errno.EINVAL)}:
            return False
        raise core.StateError("Bound resident listener liveness is inconclusive") from exc
    finally:
        sock.close()


def require_closed_session(paths, owner, *, _locked):
    """Require durable closure evidence for a previously bound native session."""
    session, directory = _bound_session_location(owner)
    if session is None:
        return
    if directory is None:
        raise core.StateError("Bound resident session cannot be physically verified")
    _require_bound_descriptor(paths, owner, session, directory)
    _require_closure_audit(session, directory)
    _locked.validate(paths)


def inspect_session_for_explicit_recovery(paths, owner, *, _locked):
    """Inspect retained session evidence for recover-start.

    Operator physical-quiescence evidence already attests local termination.
    A present transport-audit keeps the graceful closure proof unchanged. A
    missing audit is the crash/reboot boundary: preserve leftover files, refuse
    a live wake.sock, and never treat absence as unsent.
    """
    session, directory = _bound_session_location(owner)
    if session is None:
        return "unbound"
    if directory is None:
        return "session_absent"
    _require_bound_descriptor(paths, owner, session, directory)
    if os.path.lexists(directory / "transport-audit.json"):
        _require_closure_audit(session, directory)
        _locked.validate(paths)
        return "graceful_audit"
    if _unix_listener_is_live(directory / "wake.sock"):
        raise core.StateError("Bound resident session still has a listener")
    _locked.validate(paths)
    return "no_graceful_audit"


def exclusion_proof(paths, locked, owner, physical=None):
    """One bounded lineage barrier. Terminal receipts are never execution joins."""
    locked.validate(paths)
    if physical is not None:
        expected = hashlib.sha256(core._authority_file_bytes(
            paths.state_dir / "resident-owner.json")).hexdigest() if owner else None
        if (physical.get("expected_owner_sha256") != expected
                or physical.get("physical_quiescence") is not True
                or physical.get("kind") not in {"fresh_deployment", "legacy_quiescence"}
                or physical.get("config_dir") != str(paths.config_dir)
                or physical.get("state_dir") != str(paths.state_dir)
                or any(not isinstance(physical.get(k), str) or not physical[k].strip()
                       for k in ("implementation", "observations", "authorization"))):
            raise core.StateError("Physical exclusion evidence differs from expected owner")
        if owner:
            inspect_session_for_explicit_recovery(paths, owner, _locked=locked)
        return {"kind": "physical_quiescence", "evidence": physical}
    if owner is None or owner.get("version") != 4:
        raise core.StateError("legacy_exclusion_unknown: restart the Mac, do not resume old listeners, then confirm the restart")
    if owner["send_fence"]["armed_since_barrier"] is False:
        return {"kind": "unused_profile_1"}
    require_closed_session(paths, owner, _locked=locked)
    if owner.get("session") is None:
        raise core.StateError("original_execution_join_required")
    from .queue import read_private
    directory = Path(owner["session"]["directory"])
    if any(directory.glob("waiting-*")):
        raise core.StateError("original_execution_join_required")
    try:
        joined = json.loads(read_private(directory / "resident-joined.json", limit=16384))
        audit = json.loads(read_private(directory / "transport-audit.json", limit=1048576))
    except (OSError, ValueError) as exc:
        raise core.StateError("original_execution_join_required") from exc
    expected = {k: owner[k] for k in ("generation", "owner", "parent")}
    expected["sessionId"] = owner["session"]["session_id"]
    if (not isinstance(joined, dict) or set(joined) != set(expected) | {"invocation"}
            or any(joined.get(k) != value for k, value in expected.items())
            or audit.get("reason") != "resident_stopped"):
        raise core.StateError("original_execution_join_required")
    core.validate_identifier(joined["invocation"], field="invocation")
    return {"kind": "original_execution_join", "joined": joined}


def write(paths, locked, v, *, physical=None):
    path = paths.state_dir / "resident-owner.json"
    prior = read(paths, locked) if os.path.lexists(path) else None
    rotation = prior is None or any(v.get(k) != prior.get(k)
                                   for k in ("generation", "owner", "parent", "worker_pool_sha256"))
    if prior and prior.get("version") == 4 and v.get("version") != 4:
        raise core.StateError("Resident schema downgrade forbidden")
    if v.get("version") == 4:
        if rotation or not prior or prior.get("version") != 4:
            if prior and v.get("generation") != prior["generation"] + 1:
                raise core.StateError("Listener rotation must advance exactly one generation")
            barrier = exclusion_proof(paths, locked, prior, physical)
            v["send_fence"] = {"profile": 1, "armed_since_barrier": False}
            v["qualification"] = dict(v["qualification"], last_exclusion=barrier)
        elif prior["send_fence"]["armed_since_barrier"] and not v["send_fence"]["armed_since_barrier"]:
            raise core.StateError("Listener send fence cannot be cleared without rotation")
        pool = core._pool_from_value(json.loads(v["worker_pool_json"]),
                                     file_sha256=core.sha256_text(v["worker_pool_json"]))
        validate_pool_owner(v, pool)
    core.atomic_write_json(path, v, _locked=locked)


def supervision_state(paths, owner):
    """Project read-only Stop supervision state for the current owner."""
    if owner is None:
        return "absent"
    path = (paths.state_dir / "resident-supervision" /
            f"terminal-{owner['generation']}-{owner['owner']}.json")
    if not os.path.lexists(path):
        return "active"
    from .queue import read_private
    try:
        value = json.loads(read_private(path, limit=16384))
    except (ValueError, UnicodeError, core.DispatchError, OSError) as exc:
        raise core.StateError("Terminal resident supervision evidence is invalid") from exc
    expected = {"version": 1, "generation": owner["generation"],
                "owner": owner["owner"], "parent": owner["parent"],
                "session": owner.get("session"), "state": "terminally_detached",
                "send_authorized": False}
    if value != expected:
        raise core.StateError("Terminal resident supervision evidence differs")
    return "terminally_detached"


def _handoff_from_native(paths, credentials):
    """Internal commit used only by the native context gate, never a CLI action.

    Like other native operations, this is cooperative same-UID code, not a
    security boundary against arbitrary Python or filesystem modification.
    """
    with core.state_lock(paths, create=False) as locked:
        core.reservation_guard(paths, locked)
        return _commit_handoff(paths, locked, read(paths, locked), credentials)


def _commit_handoff(paths, locked, v, credentials):
    """Commit a native-authorized rotation under the canonical owner lock."""
    if v is None or v.get("version") not in (3, 4):
        raise core.StateError("Handoff requires a schema-3 resident owner")
    if type(credentials.get("generation")) is not int or not matches(v, credentials):
        raise core.StateError("Resident owner replaced")
    new_parent = credentials.get("new_parent")
    if (not isinstance(new_parent, str)
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", new_parent)
            or new_parent == v["parent"]):
        raise core.ConfigurationError("A different native Codex task ID is required")
    marker = _recovery_marker(paths, v)
    if marker is not None and marker[0] == "current":
        raise core.BusyError("Recovery owner must finish before handoff")
    if any(slot["phase"] != "idle" or slot["request"] is not None
           or slot["invocation"] is not None for slot in v["slots"]):
        raise core.BusyError("Resident slots must be idle before handoff")
    from .queue import Queue, read_private
    if (core.active_assignments(paths, _locked=locked)
            or any(row["state"] == "claimed" for row in Queue(paths).records(_locked=locked))):
        raise core.BusyError("Active requests must finish before handoff")
    if v.get("session") is None:
        raise core.StateError("Handoff requires a bound gracefully closed session")
    require_closed_session(paths, v, _locked=locked)
    directory = Path(v["session"]["directory"])
    audit = json.loads(read_private(directory / "transport-audit.json", limit=1048576))
    if audit["reason"] != "resident_stopped":
        raise core.StateError("Handoff requires graceful resident_stopped closure")
    if any(directory.glob("waiting-*")):
        raise core.BusyError("Resident waiter must finish before handoff")
    try:
        joined = json.loads(read_private(directory / "resident-joined.json", limit=16384))
    except (OSError, ValueError) as exc:
        raise core.StateError("Native serving completion barrier required") from exc
    expected = {key: v[key] for key in ("generation", "owner", "parent")}
    expected["sessionId"] = v["session"]["session_id"]
    if (not isinstance(joined, dict) or set(joined) != set(expected) | {"invocation"}
            or any(joined.get(key) != value for key, value in expected.items())):
        raise core.StateError("Native serving completion barrier differs")
    core.validate_identifier(joined["invocation"], field="invocation")
    qualification = dict(v["qualification"])
    history = qualification.get("handoffs", [])
    if not isinstance(history, list):
        raise core.StateError("Invalid resident handoff history")
    qualification["handoffs"] = [*history, {
        "previous_parent": v["parent"], "parent": new_parent,
        "previous_generation": v["generation"], "generation": v["generation"] + 1,
        "session": v["session"],
    }]
    replacement = dict(v, generation=v["generation"] + 1, parent=new_parent,
                       owner=uuid.uuid4().hex, session=None, qualification=qualification)
    write(paths, locked, replacement)
    return {"ok": True, "state": "ready", "reason": "owner_handed_off",
            "owner": operational(replacement), "send_authorized": False}


def _takeover_result(outcome, **extra):
    return {"ok": outcome in {"committed", "already_owner"}, "outcome": outcome,
            "send_authorized": False, **extra}


def _takeover_owner_read(raw, parent):
    """Validate the one unedited host read.  This intentionally knows no queue state."""
    try:
        value = json.loads(raw)
        if "owner_read_failure" in value:
            return "old_owner_unreadable"
        thread = value["thread"]
        if (value.get("schemaVersion") != 1 or not isinstance(thread, dict)
                or thread.get("id") != parent or thread.get("kind") != "codex"
                or thread.get("hostId") != "local"
                or value.get("truncated") is True or value.get("textTruncated") is True
                or thread.get("truncated") is True or thread.get("textTruncated") is True):
            return "old_owner_unreadable"
        status = thread.get("status")
        kind = status.get("type") if isinstance(status, dict) else None
    except (TypeError, ValueError, KeyError, RecursionError):
        return "old_owner_unreadable"
    if kind == "idle":
        return None
    if not isinstance(kind, str):
        return "old_owner_status_unsupported"
    if kind in {"active", "working"}:
        return "old_owner_active"
    if kind == "notLoaded":
        return "old_owner_not_loaded"
    return "old_owner_status_unsupported"


def _takeover_binding(v, request):
    """Resolve exact provenance through chronological committed parent changes."""
    q = v.get("qualification", {})
    history, handoffs = q.get("takeover_history", []), q.get("handoffs", [])
    if not isinstance(history, list) or not isinstance(handoffs, list):
        raise core.StateError("request_evidence_invalid")
    audits = [*history, q.get("takeover")]
    slot = next((item for item in v.get("slots", []) if item.get("request") == request), None)
    required_list = "cancel_prepared" if slot and slot.get("phase") == "cancel_pending" else "collect_only"
    events = [a for a in [*audits, *handoffs] if isinstance(a, dict)]
    if any(type(a.get("previous_generation")) is not int for a in events):
        raise core.StateError("request_evidence_invalid")
    events.sort(key=lambda a: a["previous_generation"])
    found = None
    for i, audit in enumerate(events):
        bindings = audit.get("request_bindings", {})
        binding = bindings.get(request) if isinstance(bindings, dict) else None
        if binding is None:
            continue
        if (not isinstance(binding, dict) or set(binding) != {"prior_parent", "slot", "worker"}
                or request not in audit.get(required_list, [])):
            raise core.StateError("request_evidence_invalid")
        if found is not None and found != binding:
            raise core.StateError("request_evidence_invalid")
        parent = audit.get("replacement_parent")
        generation = audit["previous_generation"] + 1
        for later in events[i + 1:]:
            if later["previous_generation"] < generation or later.get("previous_parent") != parent:
                raise core.StateError("request_evidence_invalid")
            parent = later.get("replacement_parent", later.get("parent"))
            generation = later["previous_generation"] + 1
        if parent != v["parent"] or generation > v["generation"]:
            raise core.StateError("request_evidence_invalid")
        if found is None and binding["prior_parent"] != audit.get("previous_parent"):
            raise core.StateError("request_evidence_invalid")
        found = binding
    return found


def _takeover_validate_packet(packet):
    if not isinstance(packet, dict) or type(packet.get("schema_version")) is not int or packet.get("schema_version") != 1 or set(packet) != {
            "schema_version", "replacement_task_id", "expected", "owner_read_text", "evidence_path"}:
        return None
    expected = packet["expected"]
    if not isinstance(expected, dict) or set(expected) != {
            "generation", "owner", "parent", "worker_pool_sha256"}:
        return None
    if (type(expected["generation"]) is not int or expected["generation"] < 1
            or not isinstance(packet["owner_read_text"], str)
            or len(packet["owner_read_text"].encode("utf-8")) > 4 * 1024 * 1024
            or not isinstance(packet["evidence_path"], str)
            or not os.path.isabs(packet["evidence_path"])):
        return None
    if not isinstance(packet["replacement_task_id"], str) or not re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", packet["replacement_task_id"]):
        return None
    try:
        for key in ("replacement_task_id",):
            core.validate_identifier(packet[key], field=key)
        for key in ("owner", "parent", "worker_pool_sha256"):
            core.validate_identifier(expected[key], field=key)
    except core.DispatchError:
        return None
    return expected


def _takeover_evidence(path, raw):
    """The native side created immutable evidence before invoking this helper."""
    try:
        info = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or Path(path).read_text(encoding="utf-8") != raw):
            return False
    except (OSError, UnicodeError):
        return False
    return True


def _takeover_slot_record(paths, locked, v, slot, records):
    """Classify an occupied slot without changing request files.

    The deliberately narrow prepared predicate is the only route to cancellation.
    Every malformed or unfamiliar post-claim shape becomes collect-only.
    """
    from .queue import Queue
    request = slot["request"]
    if request is None:
        return "idle", None, None
    broker = Queue(paths)
    record = records.get(request)
    if record is None:
        return "idle", None, None
    # Missing queue records and invalid receipt associations are distinct cases.
    receipt = broker.receipt(record, _locked=locked) if record["state"] != "queued" else None
    binding = _takeover_binding(v, request)
    recorded_parent = record.get("parent_task_id")
    if recorded_parent is not None and recorded_parent != v["parent"]:
        if not (slot["phase"] in {"collect_only", "cancel_pending"}
                and isinstance(binding, dict)
                and binding.get("prior_parent") == recorded_parent
                and binding.get("slot") == slot["slot"]
                and binding.get("worker") == slot["worker_conversation_id"]):
            raise core.StateError("request_evidence_invalid")
    if (record["state"] not in {"queued", "claimed", "published"}
            and (receipt or {}).get("status") not in {"complete", "abandoned", "failed"}):
        raise core.StateError("request_evidence_invalid")
    if record["state"] != "queued" and (record.get("worker_slot") != slot["slot"]
            or record.get("worker_conversation_id") != slot["worker_conversation_id"]):
        raise core.StateError("request_evidence_invalid")
    if receipt is not None and receipt.get("result_protocol") != core.BOUNDED_RESULT_PROTOCOL:
        raise core.StateError("request_evidence_invalid")
    audit_binding = binding or {"prior_parent": recorded_parent or v["parent"],
                                "slot": slot["slot"], "worker": slot["worker_conversation_id"]}
    exact_prepared = (slot["phase"] == "reserved" and record["state"] == "claimed"
                      and record.get("receipt_established") is True and isinstance(receipt, dict)
                      and receipt.get("status") == "prepared" and receipt.get("no_resend") is not True
                      and receipt.get("submission_count") == 0 and "armed_at" not in receipt)
    cancellable = (slot["phase"] == "cancel_pending" or
                   (slot["phase"] == "reserved" and record["state"] == "queued" and receipt is None) or
                   (slot["phase"] == "reserved" and record["state"] == "claimed" and receipt is None
                    and record.get("receipt_established") is not True) or exact_prepared)
    if cancellable:
        return "cancel_pending", audit_binding, record
    return "collect_only", audit_binding, record


def _takeover_deleted_owner_read(raw, parent):
    """Accept only the exact native local missing-task response, never errors in general."""
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate host evidence key")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        return False
    expected = {"owner_read_failure": {"isError": True, "content": [{
        "type": "text", "text": f"No Codex thread found for threadId: {parent}. "
                                "Hosts without a readable match: local"}]}}
    return (value == expected
            and value["owner_read_failure"]["isError"] is True)


def _takeover_quiescent(paths, locked, owner):
    """Prove the narrow unbound case under the generation's mutation lock.

    A bound session can retain a native continuation even without a socket or
    PID. Do not infer its termination from missing physical files or a failed
    host read. Active/uncertain receipts remain untouched and block this path.
    Every cooperative bind, reserve, claim and arm must recheck this generation
    under the same lock, so late old-owner work cannot escape the fence.
    """
    from .queue import Queue
    locked.validate(paths)
    if (owner["session"] is not None
            or any(slot["phase"] != "idle" or slot["request"] is not None
                   or slot["invocation"] is not None for slot in owner["slots"])
            or os.path.lexists(paths.state_dir / "native-client")
            or os.path.lexists(paths.state_dir / "wake.sock")
            or _recovery_marker(paths, owner) is not None
            or core.active_assignments(paths, _locked=locked)
            or core.active_cooldown(paths, _locked=locked)
            or any(row["state"] == "claimed" for row in Queue(paths).records(_locked=locked))):
        return False
    return True


def _takeover_from_native(paths, packet):
    """Private native-packet transition.  It performs exactly one owner-file write."""
    expected = _takeover_validate_packet(packet)
    if expected is None or not _takeover_evidence(packet.get("evidence_path", ""),
                                                   packet.get("owner_read_text", "")):
        return _takeover_result("evidence_stale")
    replacement = packet["replacement_task_id"]
    with core.state_lock(paths, create=False) as locked:
        core.reservation_guard(paths, locked)
        v = read(paths, locked)
        if v is None or v.get("version") not in (3, 4):
            return _takeover_result("schema_incompatible")
        if v["parent"] == replacement:
            return _takeover_result("already_owner", generation=v["generation"], parent=v["parent"],
                                    owner=v["owner"])
        if replacement == expected["parent"] or not matches(v, expected):
            return _takeover_result("expected_state_stale")
        host = _takeover_owner_read(packet["owner_read_text"], expected["parent"])
        # The native missing-task response is the only deletion evidence.
        if host == "old_owner_unreadable" and _takeover_deleted_owner_read(
                packet["owner_read_text"], expected["parent"]):
            try:
                if not _takeover_quiescent(paths, locked, v):
                    return _takeover_result("old_owner_quiescence_unproven")
            except (core.DispatchError, OSError):
                return _takeover_result("old_owner_quiescence_unproven")
        elif host:
            return _takeover_result(host)
        from .queue import Queue
        broker = Queue(paths)
        try:
            records = {row["request_id"]: row for row in broker.records(_locked=locked)}
            active = {row["assignment_id"]: row for row in core.active_assignments(paths, _locked=locked)}
            for rid, receipt in active.items():
                row = records.get(rid)
                if (row is None or row.get("state") not in {"claimed", "published"}
                        or receipt.get("result_protocol") != core.BOUNDED_RESULT_PROTOCOL):
                    raise core.StateError("request_evidence_invalid")
            occupied = {slot["request"]: slot for slot in v["slots"] if slot["request"] is not None}
            if any(rid not in occupied for rid in active):
                raise core.StateError("request_evidence_invalid")
            for rid, row in records.items():
                if row["state"] == "claimed":
                    slot = occupied.get(rid)
                    if (slot is None or row.get("worker_slot") != slot["slot"]
                            or row.get("worker_conversation_id") != slot["worker_conversation_id"]):
                        raise core.StateError("request_evidence_invalid")
            marker = paths.state_dir / "resident-recovery.json"
            markers = []
            if os.path.lexists(marker):
                marker_value = _recovery_marker(paths, v)[1]
                markers.append({"path": str(marker), "generation": marker_value["generation"],
                                "parent": marker_value.get("parent")})
            slots, cancel, collect, bindings, transitions = [], [], [], {}, {}
            for original in v["slots"]:
                slot = dict(original)
                phase, binding, _ = _takeover_slot_record(paths, locked, v, slot, records)
                transitions[slot["slot"]] = {"from": original["phase"], "to": phase,
                                             "request": slot["request"] if phase != "idle" else None}
                slot["invocation"] = None
                if phase == "idle":
                    slot["request"] = None
                elif phase == "cancel_pending":
                    cancel.append(slot["request"]); bindings[slot["request"]] = binding
                else:
                    collect.append(slot["request"]); bindings[slot["request"]] = binding
                slot["phase"] = phase
                slots.append(slot)
        except core.DispatchError:
            return _takeover_result("request_evidence_invalid")
        qualification = dict(v["qualification"])
        previous = qualification.get("takeover")
        if previous is not None:
            history = qualification.get("takeover_history", [])
            if not isinstance(history, list):
                return _takeover_result("request_evidence_invalid")
            qualification["takeover_history"] = [*history, previous]
        qualification["takeover"] = {
            "at": core.utc_now(), "owner_read_outcome": host or "idle",
            "previous_parent": v["parent"],
            "previous_generation": v["generation"], "previous_owner": v["owner"],
            "replacement_parent": replacement,
            "evidence_sha256": hashlib.sha256(packet["owner_read_text"].encode()).hexdigest(),
            "evidence_path": packet["evidence_path"], "cancel_prepared": cancel,
            "collect_only": collect, "request_bindings": bindings,
            "slot_transitions": transitions, "markers_retired": markers,
        }
        new = dict(v, generation=v["generation"] + 1, owner=uuid.uuid4().hex,
                   parent=replacement, session=None, slots=slots, qualification=qualification)
        write(paths, locked, new)
        return _takeover_result("committed", generation=new["generation"], parent=new["parent"],
                                owner=new["owner"], cancel_prepared=cancel, collect_only=collect)


def matches(v, credentials):
    if not isinstance(credentials, dict) or v is None:
        return False
    if v.get("version") in (3, 4):
        return all(credentials.get(k) == v[k]
                   for k in ("generation", "owner", "parent", "worker_pool_sha256"))
    return all(credentials.get(k) == v[k]
               for k in ("generation", "owner", "parent", "worker"))


@contextmanager
def settlement_scope(operation):
    token = settlement_operation.set(operation)
    try:
        yield
    finally:
        settlement_operation.reset(token)


def _settlement_binding(paths, locked, v, c, request, operation):
    """Authorize only a current takeover audit entry and one fixed helper operation."""
    if (c.get("collector_only") is True or c.get("takeover_settlement") is not True
            or not isinstance(request, str) or c.get("request") != request
            or operation not in {"prepare_cancel", "abandon", "abandoned", "release", "end"}):
        raise core.BusyError("settlement_out_of_scope")
    audit = v.get("qualification", {}).get("takeover")
    if not isinstance(audit, dict):
        raise core.BusyError("settlement_out_of_scope")
    binding = audit.get("request_bindings", {}).get(request)
    slot = next((item for item in v["slots"] if item["request"] == request), None)
    if (not isinstance(binding, dict) or slot is None
            or c.get("request_parent") != binding.get("prior_parent")
            or slot.get("slot") != binding.get("slot")
            or slot.get("worker_conversation_id") != binding.get("worker")):
        raise core.BusyError("settlement_out_of_scope")
    if operation == "prepare_cancel" and (slot["phase"] != "cancel_pending"
                                             or request not in audit.get("cancel_prepared", [])):
        raise core.BusyError("settlement_out_of_scope")
    if operation in {"abandon", "abandoned", "release"} and slot["phase"] not in {"cancel_pending", "collect_only"}:
        raise core.BusyError("settlement_out_of_scope")
    if operation == "end" and slot["phase"] not in {"cancel_pending", "collect_only"}:
        raise core.BusyError("settlement_out_of_scope")
    listed = audit.get("cancel_prepared" if slot["phase"] == "cancel_pending" else "collect_only", [])
    if request not in listed:
        raise core.BusyError("settlement_out_of_scope")
    from .queue import Queue
    broker = Queue(paths)
    record = broker.load(request, _locked=locked) if os.path.lexists(broker.path(request)) else None
    receipt = (core.load_assignment(request, paths, _locked=locked)
               if os.path.lexists(core.assignment_path(request, paths)) else None)
    if record is not None and record["state"] != "queued":
        receipt = broker.receipt(record, _locked=locked)
        if (record.get("parent_task_id") != binding["prior_parent"]
                or record.get("worker_slot") != binding["slot"]
                or record.get("worker_conversation_id") != binding["worker"]):
            raise core.BusyError("settlement_out_of_scope")
    if receipt and (receipt.get("parent_task_id") != binding["prior_parent"]
                    or receipt.get("worker_slot") != binding["slot"]
                    or receipt.get("worker_conversation_id") != binding["worker"]):
        raise core.BusyError("settlement_out_of_scope")
    state, status = (record or {}).get("state"), (receipt or {}).get("status")
    prepared = (status == "prepared" and receipt.get("no_resend") is not True
                and receipt.get("submission_count") == 0 and "armed_at" not in receipt)
    permitted = False
    if operation == "prepare_cancel":
        permitted = state == "claimed" and (receipt is None or prepared) and record.get("receipt_established") is not True
    elif operation in {"abandon", "abandoned"}:
        permitted = (prepared and state == "claimed" and record.get("receipt_established") is True) if slot["phase"] == "cancel_pending" else status in core.ACTIVE_STATUSES
    elif operation == "release":
        permitted = status == "abandoned" and state in {"claimed", "released"}
    elif operation == "end":
        permitted = ((status == "abandoned" and state == "released")
                     or (receipt is None and state in {None, "queued"})) if slot["phase"] == "cancel_pending" else status in {"complete", "abandoned", "failed"}
    if not permitted:
        raise core.BusyError("settlement_out_of_scope")
    return slot, binding


def operational(v):
    return {k: val for k, val in v.items() if k not in {"qualification", "worker_pool_json", "send_fence"}} if v else None


def _pool_slot(v, slot):
    if v is None or v.get("version") not in (3, 4):
        raise core.StateError("Resident pool ownership is not active")
    return next((item for item in v["slots"] if item["slot"] == slot), None)


def is_collector_only(v, credentials=None):
    if v is None or v.get("version") not in (3, 4):
        return False
    if isinstance(credentials, dict) and credentials.get("collector_only") is True:
        return True
    return any(item["phase"] == "collect_only" for item in v["slots"])


def reserve_slot(paths, locked, slot, request, worker, generation):
    """Reserve a pool slot before publishing a claim, without releasing it."""
    v = read(paths, locked)
    c = invocation.get()
    if v is None or v.get("version") not in (3, 4) or not matches(v, c):
        raise core.StateError("Resident pool owner changed before slot reservation")
    if c.get("collector_only") is True or c.get("takeover_settlement") is True:
        raise core.StateError("Collector-only recovery cannot reserve a slot")
    if c.get("generation") != generation or c.get("request") not in {None, request}:
        raise core.StateError("Resident slot reservation identity differs")
    selected = _pool_slot(v, slot)
    if selected is None or selected["worker_conversation_id"] != worker:
        raise core.StateError("Resident slot worker differs")
    if selected["request"] is not None:
        if selected["phase"] in {"collect_only", "cancel_pending"}:
            raise core.BusyError("Resident pool slot is collector-only")
        if selected["request"] == request and selected["invocation"] is None:
            selected["invocation"] = {"invocation": c["invocation"], "request": request}
            selected["phase"] = "reserved"
            write(paths, locked, v)
            return v
        if selected["request"] == request and selected["invocation"] == {
            "invocation": c["invocation"], "request": request
        }:
            return v
        raise core.BusyError("Resident pool slot is already reserved")
    selected["request"] = request
    selected["invocation"] = {"invocation": c["invocation"], "request": request}
    selected["phase"] = "reserved"
    write(paths, locked, v)
    return v


def mark_running(paths, locked, slot, request, generation, invocation_id):
    """Record that the reserved slot crossed the final arm fence."""
    v = read(paths, locked)
    c = invocation.get()
    if (v is None or v.get("version") not in (3, 4) or not matches(v, c)
            or c.get("collector_only") is True or c.get("takeover_settlement") is True
            or c.get("generation") != generation
            or c.get("invocation") != invocation_id
            or c.get("request") != request
            or c.get("slot") not in {None, slot}):
        raise core.StateError("Resident pool owner changed before send phase")
    selected = _pool_slot(v, slot)
    if (selected is None or selected["request"] != request
            or selected["invocation"] != {"invocation": invocation_id, "request": request}):
        raise core.StateError("Resident pool slot is not reserved for send phase")
    if selected["phase"] != "reserved":
        raise core.StateError("Resident pool slot is not in the reserved phase")
    selected["phase"] = "running"
    if v["version"] == 4:
        v["send_fence"]["armed_since_barrier"] = True
    write(paths, locked, v)


def _collector_mutex(paths, locked, credentials, request):
    path = paths.state_dir / "resident-recovery.json"
    current = read(paths, locked)
    marker = _recovery_marker(paths, current) if current is not None else None
    if marker is None:
        raise core.StateError("Collector-only recovery is not open")
    if marker[0] == "stale":
        raise core.StateError("marker_stale")
    value = marker[1]
    expected = {"schema_version", "mode", "owner", "parent", "generation",
                "pool_sha256", "opened_at"}
    if (set(value) != expected or value.get("schema_version") != 1
            or value.get("mode") != "collector-only"
            or value.get("owner") != credentials.get("owner")
            or value.get("parent") != credentials.get("parent")
            or value.get("generation") != credentials.get("generation")
            or value.get("pool_sha256") != credentials.get("worker_pool_sha256")):
        raise core.StateError("Collector recovery owner differs")
    bound = False
    if current is not None and matches(current, credentials):
        if current.get("version") in (3, 4):
            slot = next((item for item in current["slots"]
                         if item["request"] == request and item["phase"] == "collect_only"), None)
            bound = slot is not None
            if bound:
                from .queue import Queue
                broker = Queue(paths)
                record = broker.load(request, _locked=locked)
                broker.receipt(record, _locked=locked)  # Validate association before granting observation.
                prior = record.get("parent_task_id")
                supplied = credentials.get("request_parent", credentials.get("parent"))
                bound = (supplied == prior and record.get("worker_slot") == slot["slot"]
                         and record.get("worker_conversation_id") == slot["worker_conversation_id"])
                if prior != current["parent"]:
                    audit = current.get("qualification", {}).get("takeover", {})
                    audit_binding = _takeover_binding(current, request)
                    bound = (bound and request in audit.get("collect_only", [])
                             and isinstance(audit_binding, dict)
                             and prior == audit_binding.get("prior_parent")
                             and slot["slot"] == audit_binding.get("slot")
                             and slot["worker_conversation_id"] == audit_binding.get("worker"))

        else:
            inflight = current.get("inflight")
            bound = isinstance(inflight, dict) and inflight.get("request") == request
    if (not isinstance(credentials, dict)
            or credentials.get("collector_only") is not True
            or credentials.get("takeover_settlement") is True
            or (current.get("version") in (3, 4) and credentials.get("request") != request) or not bound):
        raise core.StateError("Collector-only recovery request is not bound")


def validate_collector_observation(paths, locked, request):
    credentials = invocation.get()
    if not isinstance(credentials, dict) or credentials.get("collector_only") is not True:
        raise core.StateError("Collector credentials are required")
    _collector_mutex(paths, locked, credentials, request)


def collector_open(paths, locked, credentials):
    """Acquire a single durable collector-only recovery mutex."""
    path = paths.state_dir / "resident-recovery.json"
    existing = _recovery_marker(paths, v) if (v := read(paths, locked)) is not None else None
    if existing is not None and existing[0] == "current":
        raise core.BusyError("A collector-only recovery owner already exists")
    if existing is not None:
        if not matches(v, credentials):
            raise core.StateError("Resident owner replaced")
        _retire_recovery_marker(paths, v)
    if v is None:
        raise core.StateError("Collector-only recovery requires resident ownership")
    if not matches(v, credentials):
        raise core.StateError("Resident owner replaced")
    payload = {
        "schema_version": 1,
        "mode": "collector-only",
        "owner": v["owner"],
        "parent": v["parent"],
        "generation": v["generation"],
        "pool_sha256": v.get("worker_pool_sha256"),
        "opened_at": core.utc_now(),
    }
    core.atomic_write_json(path, payload, _locked=locked)
    return {"ok": True, "mode": "collector-only", "recovery": payload}


def recovery_start(paths, locked, credentials):
    """Fence an old local owner after explicit physical-quiescence evidence."""
    v = read(paths, locked)
    if v is None or v.get("version") not in (3, 4):
        raise core.StateError("Pool recovery requires a schema-3 resident owner")
    if not matches(v, credentials):
        raise core.StateError("Resident owner replaced")
    evidence_file = credentials.get("evidence_file")
    expected_hash = credentials.get("evidence_sha256")
    if not isinstance(evidence_file, str) or not isinstance(expected_hash, str):
        raise core.ConfigurationError("Explicit recovery requires physical-quiescence evidence")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise core.ConfigurationError("Invalid recovery evidence hash")
    from .native_storage import read_evidence
    try:
        raw = read_evidence(evidence_file)
    except (OSError, IntegrityError) as exc:
        raise core.ConfigurationError("Recovery evidence integrity rejected") from exc
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise core.StateError("Recovery evidence hash differs")
    try:
        proof = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise core.StateError("Recovery evidence is not valid JSON") from exc
    if (not isinstance(proof, dict) or proof.get("kind") not in {"fresh_deployment", "legacy_quiescence"}
            or proof.get("config_dir") != str(paths.config_dir)
            or proof.get("state_dir") != str(paths.state_dir)
            or proof.get("physical_quiescence") is not True
            or any(not isinstance(proof.get(k), str) or not proof[k].strip()
                   for k in ("implementation", "observations", "authorization"))):
        raise core.StateError("Recovery evidence is not bound to this authority")
    # Physical quiescence fences local continuation. Graceful audits stay
    # required when present; a missing audit is crash/reboot recovery, not unsent.
    if v["version"] == 4:
        exclusion_proof(paths, locked, v, proof)
    marker = _recovery_marker(paths, v)
    if marker is not None and marker[0] == "current":
        raise core.BusyError("A collector-only recovery owner already exists")
    session_closure = inspect_session_for_explicit_recovery(paths, v, _locked=locked)
    generation = v["generation"] + 1
    new_owner = credentials.get("new_owner", v["owner"])
    core.validate_identifier(new_owner, field="owner")
    slots = [dict(item) for item in v["slots"]]
    from .queue import Queue
    broker = Queue(paths)
    for item in slots:
        if item["request"] is None:
            continue
        # Once inherited, a request can never become sendable through recovery.
        if item["phase"] in {"collect_only", "cancel_pending"}:
            item["invocation"] = None
            continue
        record = (broker.load(item["request"], _locked=locked)
                  if os.path.lexists(broker.path(item["request"])) else None)
        receipt = (broker.receipt(record, _locked=locked)
                   if record and record["state"] != "queued" else None)
        prepared = (
            isinstance(receipt, dict)
            and receipt.get("status") == "prepared"
            and receipt.get("no_resend") is not True
        )
        queued_unsent = isinstance(record, dict) and record.get("state") == "queued"
        claimed_before_receipt = (
            isinstance(record, dict)
            and record.get("state") == "claimed"
            and receipt is None
            and record.get("receipt_established") is not True
        )
        # A begin reservation with no receipt has not crossed arm. Keep it
        # sendable, or release a missing request so the slot cannot deadlock.
        if record is None:
            item["request"] = None
            item["invocation"] = None
            item["phase"] = "idle"
            continue
        if claimed_before_receipt:
            record["owner_generation"] = generation
            # recover-start owns this same canonical transaction. Queue.save
            # correctly rejects an unreserved recovery invocation, so rewrite
            # only this validated pre-receipt claim through the locked store.
            core.atomic_write_json(
                broker.path(record["request_id"]), record, _locked=locked
            )
        item["invocation"] = None
        item["phase"] = (
            "reserved" if prepared or queued_unsent or claimed_before_receipt
            else "collect_only"
        )
    replacement = dict(v, generation=generation, owner=new_owner, slots=slots, session=None)
    write(paths, locked, replacement, physical=proof if v["version"] == 4 else None)
    collect_ids, prepared_ids = _recovered_request_groups(slots)
    result = _pool_result(
        replacement,
        "collect_only" if collect_ids else "ready",
        "owner_fenced",
        request_ids=collect_ids,
        prepared_ids=prepared_ids,
    )
    result["evidence_sha256"] = expected_hash
    result["session_closure"] = session_closure
    result["send_authorized"] = False
    return result


def admit(paths, locked, v, c):
    """Publish client handoff while start/begin are excluded by the same lock.

    This does not reserve native work or send. After a partial publication,
    keep every artifact; only the existing owner continuation may proceed.
    """
    from .queue import Queue, read_private
    from .native_storage import Directory, decode
    bound = v.get("session")
    if not bound or c.get("session") != bound:
        raise core.StateError("Resident session binding differs")
    directory = Path(bound["directory"])
    raw_descriptor = read_private(directory / "session.json", limit=16384)
    descriptor = decode(raw_descriptor)
    helper = Path(__file__).resolve().parents[2] / "skills/codex-pro-dispatch/scripts/pro-dispatch"
    expected = dict(resident=True, sessionId=bound["session_id"], parent=v["parent"],
                    worker=v["worker"], configDir=str(paths.config_dir),
                    stateDir=str(paths.state_dir), helper=str(helper))
    if (hashlib.sha256(raw_descriptor).hexdigest() != bound["descriptor_sha256"]
            or any(descriptor.get(k) != val for k, val in expected.items())):
        raise core.StateError("Session descriptor binding differs")
    command = c.get("command")
    if not isinstance(command, str) or len(command.encode()) > 4096:
        raise core.ConfigurationError("Invalid resident command")
    record = decode(command.encode())
    keys = {"sessionId", "ordinal", "requestId", "clientSessionId", "nonce", "deadlineAt",
            "promptSha256", "pid", "ppid"}
    if (not isinstance(record, dict) or set(record) != keys
            or record["sessionId"] != bound["session_id"]
            or type(record["ordinal"]) is not int or not 1 <= record["ordinal"] <= 64
            or not isinstance(record["nonce"], str) or not re.fullmatch(r"[a-f0-9]{32}", record["nonce"])
            or any(type(record[k]) is not int or record[k] < 1 for k in ("pid", "ppid", "deadlineAt"))
            or type(c.get("retry")) is not bool):
        raise core.ConfigurationError("Invalid resident command")
    now = time.time() * 1000
    if not now < record["deadlineAt"] <= now + descriptor["idleMs"]:
        raise core.StateError("Command rendezvous expired or invalid")
    prompt = read_private(Path(c.get("prompt_file", "")))
    broker = Queue(paths)
    broker.validate_submission(record["requestId"], prompt, record["clientSessionId"])
    if hashlib.sha256(prompt).hexdigest() != record["promptSha256"]:
        raise core.StateError("Prompt changed before admission")
    if (v["inflight"] is not None or core.active_assignment(paths, _locked=locked)
            or core.active_cooldown(paths, _locked=locked)
            or any(r["state"] == "claimed" or r["request_id"] == record["requestId"]
                   for r in broker.records(_locked=locked))
            or os.path.lexists(paths.state_dir / "native-client")
            or os.path.lexists(core.assignment_path(record["requestId"], paths))):
        raise core.BusyError("Authority occupied; no command-ready signal")
    ordinal = record["ordinal"]
    name = f"command-{ordinal}" + ("-retry-" + record["nonce"] if c["retry"] else "")
    marker = f"waiting-{ordinal}.{bound['session_id']}"
    with Directory(directory) as folder:
        names = os.listdir(folder.fd)
        if any(n in names for n in (name, name + ".json", f"ready-{ordinal}.json",
                                   f"command-observed-{ordinal}.json", "transport-audit.json")):
            raise core.StateError("Existing rendezvous artifact; do not reuse")
        gone = f"Resident is not waiting for ordinal {ordinal}; do not publish"
        try:
            info = os.stat(marker, dir_fd=folder.fd, follow_symlinks=False)
        except FileNotFoundError:
            raise core.StateError(gone) from None
        age = time.time() - info.st_mtime
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid() or not -1 <= age <= 5):
            raise core.StateError(f"Stale resident readiness for ordinal {ordinal}; do not publish")
        if os.listdir(directory / marker):
            raise core.StateError("Conflicting resident readiness")
        try:
            os.rename(marker, name, src_dir_fd=folder.fd, dst_dir_fd=folder.fd)
        except FileNotFoundError:
            raise core.StateError(gone) from None
        # From this point, failure leaves a permanent ticket. Never roll back.
        os.fsync(folder.fd)
        with Directory(directory / name) as ticket:
            ticket.write("prompt.txt", prompt, immutable=True)
            ticket.write("ready.tmp", command.encode(), immutable=True)
            os.rename("ready.tmp", name + ".json", src_dir_fd=ticket.fd, dst_dir_fd=folder.fd)
            os.fsync(ticket.fd)
        os.fsync(folder.fd)
    return {"ok": True, "published": True, "send_authorized": False}


def _pool_evidence(paths, credentials):
    evidence_file = credentials.get("evidence_file")
    evidence_sha256 = credentials.get("evidence_sha256")
    if not isinstance(evidence_file, str) or not isinstance(evidence_sha256, str):
        raise core.ConfigurationError("Evidence file and SHA-256 are required")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise core.ConfigurationError("Invalid evidence SHA-256")
    from .native_storage import read_evidence
    try:
        raw = read_evidence(evidence_file)
    except (OSError, IntegrityError) as exc:
        raise core.ConfigurationError("Evidence integrity rejected") from exc
    if hashlib.sha256(raw).hexdigest() != evidence_sha256:
        raise core.StateError("Evidence hash differs")
    try:
        proof = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise core.StateError("Evidence is not valid JSON") from exc
    if (not isinstance(proof, dict)
            or proof.get("kind") not in {"fresh_deployment", "legacy_quiescence"}
            or proof.get("config_dir") != str(paths.config_dir)
            or proof.get("state_dir") != str(paths.state_dir)
            or any(not isinstance(proof.get(k), str) or not proof[k].strip()
                   for k in ("implementation", "observations", "authorization"))):
        raise core.StateError("Evidence binding or authorization missing")
    return evidence_sha256, proof


def _pool_credentials(c, v, *, slot=None, request=None, collector_only=False):
    result = {
        "generation": v["generation"], "owner": v["owner"], "parent": v["parent"],
        "worker_pool_sha256": v["worker_pool_sha256"],
    }
    if slot is not None:
        result["slot"] = slot
    if request is not None:
        result["request"] = request
    if c.get("invocation") is not None:
        result["invocation"] = c["invocation"]
    if collector_only:
        result["collector_only"] = True
    return result


def _recovered_request_groups(slots):
    collect_ids = []
    prepared_ids = []
    for item in slots:
        request = item.get("request")
        if request is None:
            continue
        if item.get("phase") == "collect_only":
            collect_ids.append(request)
        elif item.get("phase") == "reserved" and item.get("invocation") is None:
            prepared_ids.append(request)
    return collect_ids, prepared_ids


def _other_claim_occupies(paths, locked, slot, request_id):
    from .queue import Queue
    for record in Queue(paths).records(_locked=locked):
        if record["state"] != "claimed" or record["request_id"] == request_id:
            continue
        if (record.get("worker_slot") == slot["slot"]
                or record.get("worker_conversation_id") == slot["worker_conversation_id"]):
            return True
    return False


def _bind_retained_pool_claims(paths, locked, slots):
    from .queue import Queue
    broker = Queue(paths)
    by_worker = {item["worker_conversation_id"]: item for item in slots}
    for record in broker.records(_locked=locked):
        if record["state"] != "claimed":
            continue
        item = by_worker.get(record.get("worker_conversation_id"))
        if item is None:
            raise core.StateError("Unresolved queue worker is not retained by pool")
        if item["request"] not in {None, record["request_id"]}:
            raise core.StateError("Multiple retained claims share one worker slot")
        try:
            receipt = broker.receipt(record, _locked=locked)
        except core.DispatchError:
            phase = (
                "reserved" if record.get("receipt_established") is not True
                else "collect_only"
            )
        else:
            prepared = (
                isinstance(receipt, dict)
                and receipt.get("status") == "prepared"
                and receipt.get("no_resend") is not True
            )
            phase = "reserved" if prepared else "collect_only"
        item["request"] = record["request_id"]
        item["invocation"] = None
        item["phase"] = phase


def _pool_result(v, state, reason, *, request_ids=None, prepared_ids=None):
    collect_ids = list(request_ids or [])
    sendable_ids = list(prepared_ids or [])
    bindings = {}
    if isinstance(v, dict):
        audit = v.get("qualification", {}).get("takeover", {})
        raw = audit.get("request_bindings", {}) if isinstance(audit, dict) else {}
        for rid in collect_ids:
            slot = next((item for item in v["slots"] if item["request"] == rid), None)
            if isinstance(raw.get(rid), dict):
                bindings[rid] = raw[rid]
            elif slot is not None:
                bindings[rid] = {"prior_parent": v["parent"], "slot": slot["slot"],
                                 "worker": slot["worker_conversation_id"]}
    return {
        "ok": True, "state": state, "reason": reason,
        "owner": operational(v),
        "request_id": (collect_ids[0] if collect_ids
                       else sendable_ids[0] if sendable_ids else None),
        "request_ids": collect_ids,
        "prepared_ids": sendable_ids,
        "recovery_bindings": bindings,
        "automatic_startup": "unsupported",
        "send_authorized": False,
    }


def _pool_admit(paths, locked, v, c):
    """Publish one command ticket while any configured slot remains free."""
    from .queue import Queue, read_private
    from .native_storage import Directory, decode
    bound = v.get("session")
    if not bound or c.get("session") != bound:
        raise core.StateError("Resident session binding differs")
    directory = Path(bound["directory"])
    raw_descriptor = read_private(directory / "session.json", limit=16384)
    descriptor = decode(raw_descriptor)
    helper = Path(__file__).resolve().parents[2] / "skills/codex-pro-dispatch/scripts/pro-dispatch"
    expected = dict(resident=True, sessionId=bound["session_id"], parent=v["parent"],
                    configDir=str(paths.config_dir), stateDir=str(paths.state_dir),
                    helper=str(helper), workerPoolSha256=v["worker_pool_sha256"])
    if (hashlib.sha256(raw_descriptor).hexdigest() != bound["descriptor_sha256"]
            or any(descriptor.get(k) != val for k, val in expected.items())):
        raise core.StateError("Session descriptor binding differs")
    command = c.get("command")
    if not isinstance(command, str) or len(command.encode()) > 4096:
        raise core.ConfigurationError("Invalid resident command")
    record = decode(command.encode())
    keys = {"sessionId", "ordinal", "requestId", "clientSessionId", "nonce", "deadlineAt",
            "promptSha256", "pid", "ppid"}
    if (not isinstance(record, dict) or set(record) != keys
            or record["sessionId"] != bound["session_id"]
            or type(record["ordinal"]) is not int or not 1 <= record["ordinal"] <= 64
            or not isinstance(record["nonce"], str) or not re.fullmatch(r"[a-f0-9]{32}", record["nonce"])
            or any(type(record[k]) is not int or record[k] < 1 for k in ("pid", "ppid", "deadlineAt"))
            or type(c.get("retry")) is not bool):
        raise core.ConfigurationError("Invalid resident command")
    now = time.time() * 1000
    if not now < record["deadlineAt"] <= now + descriptor["idleMs"]:
        raise core.StateError("Command rendezvous expired or invalid")
    prompt = read_private(Path(c.get("prompt_file", "")))
    broker = Queue(paths)
    broker.validate_submission(record["requestId"], prompt, record["clientSessionId"])
    if hashlib.sha256(prompt).hexdigest() != record["promptSha256"]:
        raise core.StateError("Prompt changed before admission")
    capacity = len(v["slots"])
    occupied = sum(1 for item in v["slots"] if item["request"] is not None)
    claimed = [r for r in broker.records(_locked=locked) if r["state"] == "claimed"]
    active = core.active_assignments(paths, _locked=locked)
    # Same requestId already queued is a duplicate rendezvous, not native
    # submit-then-begin. Parked-client submit happens after ready-N.
    if (occupied >= capacity or len(claimed) >= capacity or len(active) >= capacity
            or core.active_cooldown(paths, _locked=locked)
            or any(r["request_id"] == record["requestId"] for r in broker.records(_locked=locked))
            or os.path.lexists(paths.state_dir / "native-client")
            or os.path.lexists(core.assignment_path(record["requestId"], paths))):
        raise core.BusyError("Authority occupied; no command-ready signal")
    ordinal = record["ordinal"]
    name = f"command-{ordinal}" + ("-retry-" + record["nonce"] if c["retry"] else "")
    marker = f"waiting-{ordinal}.{bound['session_id']}"
    with Directory(directory) as folder:
        names = os.listdir(folder.fd)
        if any(n in names for n in (name, name + ".json", f"ready-{ordinal}.json",
                                   f"command-observed-{ordinal}.json", "transport-audit.json")):
            raise core.StateError("Existing rendezvous artifact; do not reuse")
        gone = f"Resident is not waiting for ordinal {ordinal}; do not publish"
        try:
            info = os.stat(marker, dir_fd=folder.fd, follow_symlinks=False)
        except FileNotFoundError:
            raise core.StateError(gone) from None
        age = time.time() - info.st_mtime
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid() or not -1 <= age <= 5):
            raise core.StateError(f"Stale resident readiness for ordinal {ordinal}; do not publish")
        if os.listdir(directory / marker):
            raise core.StateError("Conflicting resident readiness")
        try:
            os.rename(marker, name, src_dir_fd=folder.fd, dst_dir_fd=folder.fd)
        except FileNotFoundError:
            raise core.StateError(gone) from None
        os.fsync(folder.fd)
        with Directory(directory / name) as ticket:
            ticket.write("prompt.txt", prompt, immutable=True)
            ticket.write("ready.tmp", command.encode(), immutable=True)
            os.rename("ready.tmp", name + ".json", src_dir_fd=ticket.fd, dst_dir_fd=folder.fd)
            os.fsync(ticket.fd)
        os.fsync(folder.fd)
    return {"ok": True, "published": True, "send_authorized": False}


def _retire_recovery_marker(paths, v):
    path = paths.state_dir / "resident-recovery.json"
    if not os.path.lexists(path):
        return []
    marker = _recovery_marker(paths, v)
    if marker[0] == "current":
        return []
    value = marker[1]
    retired = paths.state_dir / "markers" / "retired"
    retired.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(retired, 0o700)
    destination = retired / f"{value['generation']}-resident-recovery.json"
    if os.path.lexists(destination):
        if os.path.lexists(path):
            raise core.StateError("request_evidence_invalid")
    else:
        os.rename(path, destination)
        for directory in (retired, paths.state_dir):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return [str(destination)]


def _recovery_marker(paths, v):
    """Generation-aware marker reader.  Readers never delete evidence."""
    path = paths.state_dir / "resident-recovery.json"
    if not os.path.lexists(path):
        return None
    try:
        value = core.read_json(path)
    except core.DispatchError as exc:
        raise core.StateError("request_evidence_invalid") from exc
    expected = {"schema_version", "mode", "owner", "parent", "generation", "pool_sha256", "opened_at"}
    if (set(value) != expected or value.get("schema_version") != 1
            or value.get("mode") != "collector-only" or type(value.get("generation")) is not int
            or value["generation"] < 1
            or any(not isinstance(value.get(key), str) or not value[key]
                   for key in ("owner", "parent", "opened_at"))
            or (v.get("version") in (3, 4) and not isinstance(value.get("pool_sha256"), str))):
        raise core.StateError("request_evidence_invalid")
    if value["generation"] > v["generation"]:
        raise core.StateError("request_evidence_invalid")
    return ("current" if value["generation"] == v["generation"] else "stale", value)


def _settle_takeover(paths, locked, v, c):
    if c.get("collector_only") is True or c.get("takeover_settlement") is True:
        raise core.StateError("Settlement requires current owner credentials")
    retired = _retire_recovery_marker(paths, v)
    audit = v.get("qualification", {}).get("takeover")
    if not isinstance(audit, dict):
        return {"ok": True, "settled": [], "collect_only": [], "markers_retired": retired,
                "send_authorized": False}
    from .queue import Queue
    broker = Queue(paths)
    settled = []
    for request in audit.get("cancel_prepared", []):
        binding = audit.get("request_bindings", {}).get(request)
        if not isinstance(binding, dict):
            raise core.StateError("request_evidence_invalid")
        slot = _pool_slot(v, binding.get("slot"))
        if slot is None or slot.get("request") != request or slot.get("phase") != "cancel_pending":
            continue
        credential = dict(c, takeover_settlement=True, request=request,
                          request_parent=binding["prior_parent"])
        token = invocation.set(credential)
        try:
            with settlement_scope("prepare_cancel"):
                record = None
                try:
                    record = broker.load(request, _locked=locked)
                except core.DispatchError:
                    record = None
                receipt = broker.receipt(record, _locked=locked) if record and record["state"] != "queued" else None
                if (record and record["state"] == "claimed" and receipt is None
                        and record.get("receipt_established") is not True):
                    worker = next((item for item in core.load_worker_pool(paths, _locked=locked).workers
                                   if item.slot == slot["slot"]), None)
                    if worker is None:
                        raise core.StateError("request_evidence_invalid")
                    core.prepare_assignment(record["prompt"], parent_task_id=binding["prior_parent"],
                        assignment_id=request, queue_claim_token=record["queue_claim_token"],
                        worker_config=worker, worker_slot=worker.slot,
                        owner_generation=record.get("owner_generation", v["generation"]),
                        paths=paths, _locked=locked)
                    receipt = broker.receipt(record, _locked=locked)
                if (record and record["state"] == "claimed" and receipt is not None
                        and receipt.get("status") == "prepared"
                        and record.get("receipt_established") is not True):
                    # Resume a crash after receipt creation but before claim bookkeeping.
                    record["receipt_established"] = True
                    broker.save(record, _locked=locked)
            if receipt is not None and receipt.get("status") in core.ACTIVE_STATUSES:
                with settlement_scope("abandon"):
                    core.abandon_assignment(request, reason="takeover", paths=paths, _locked=locked)
                receipt = broker.receipt(record, _locked=locked)
            if record is not None and record.get("state") == "claimed" and receipt and receipt.get("status") == "abandoned":
                with settlement_scope("release"):
                    broker.release(request, binding["prior_parent"], _locked=locked)
            # The two no-receipt forms are safe to retire without manufacturing a receipt.
            current = _pool_slot(v, slot["slot"])
            if current and current.get("request") == request:
                with settlement_scope("end"):
                    _settlement_binding(paths, locked, v, credential, request, "end")
                    current["request"] = None
                    current["invocation"] = None
                    current["phase"] = "idle"
                    write(paths, locked, v)
            settled.append(request)
        finally:
            invocation.reset(token)
    return {"ok": True, "settled": settled,
            "collect_only": list(audit.get("collect_only", [])),
            "markers_retired": retired, "send_authorized": False}


def _pool_control(action, c, paths, locked, v):
    pool = core.load_worker_pool(paths, _locked=locked)
    if action == "enroll":
        migrating = (
            v is not None and v.get("version") in {1, 2}
            and v.get("inflight") is None and c.get("generation") == 0
        )
        if v is not None and not migrating:
            return _pool_result(v, "blocked", "already_enrolled")
        if migrating:
            require_closed_session(paths, v, _locked=locked)
        elif c.get("generation") != 0:
            return _pool_result(v, "blocked", "already_enrolled")
        evidence_sha256, proof = _pool_evidence(paths, c)
        owner = c.get("owner")
        parent = c.get("parent")
        core.validate_identifier(owner, field="owner")
        core.validate_identifier(parent, field="parent")
        qualification = {"sha256": evidence_sha256, "evidence": proof}
        slots = [{"slot": worker.slot, "worker_conversation_id": worker.conversation_id,
                  "request": None, "invocation": None, "phase": "idle"}
                 for worker in pool.workers]
        _bind_retained_pool_claims(paths, locked, slots)
        value = {
            "version": 3, "generation": 1, "owner": owner, "parent": parent,
            "worker_pool_sha256": pool.file_sha256, "slots": slots,
            "session": None, "qualification": qualification,
        }
        write(paths, locked, value)
        return _pool_result(value, "ready", "owner_enrolled")
    if v is None:
        return _pool_result(None, "blocked", "enrollment_required")
    if v.get("version") not in (3, 4):
        return _pool_result(v, "blocked", "legacy_owner_requires_explicit_migration")
    if action == "settle":
        if not matches(v, c):
            raise core.StateError("Resident pool owner replaced")
        return _settle_takeover(paths, locked, v, c)
    if c.get("takeover_settlement") is True and action != "end":
        raise core.BusyError("settlement_out_of_scope")
    if action == "end" and matches(v, c):
        slot = next((item for item in v["slots"] if item["request"] == c.get("request")), None)
        if slot and slot["phase"] in {"cancel_pending", "collect_only"}:
            audit = v.get("qualification", {}).get("takeover", {})
            binding = audit.get("request_bindings", {}).get(c.get("request"))
            if binding:
                scoped = c if c.get("takeover_settlement") is True else dict(
                    c, collector_only=False, takeover_settlement=True,
                    request_parent=c.get("request_parent", binding.get("prior_parent")))
                _settlement_binding(paths, locked, v, scoped, c.get("request"), "end")
                slot.update(request=None, invocation=None, phase="idle")
                write(paths, locked, v)
                return {"ok": True, "owner": operational(v), "send_authorized": False}
        if c.get("takeover_settlement") is True:
            raise core.BusyError("settlement_out_of_scope")
    if c.get("collector_only") is True and action not in {"end", "collector-close", "check", "inspect"}:
        raise core.BusyError("Collector-only recovery cannot mutate owner")
    if action == "start":
        # Scalar start CAS-installs the opening attempt as owner. Pool start
        # must do the same: openResident presents a fresh attempt, not the
        # enrolled owner string. Identity is parent + pool hash + generation.
        core.validate_identifier(c.get("owner"), field="owner")
        if (c.get("parent") != v["parent"]
                or c.get("worker_pool_sha256") != v["worker_pool_sha256"]):
            raise core.StateError("Resident pool owner replaced")
        if type(c.get("generation")) is not int or c["generation"] < 0:
            raise core.ConfigurationError("Expected resident generation required")
        live = [item for item in v["slots"]
                if item["invocation"] is not None and item["phase"] != "collect_only"]
        if live:
            return _pool_result(v, "busy", "original_invocation_not_finished",
                                request_ids=[item["request"] for item in live])
        settling = [item["request"] for item in v["slots"] if item["phase"] == "cancel_pending"]
        if settling:
            return _pool_result(v, "blocked", "takeover_settlement_required", request_ids=settling)
        recovering, prepared = _recovered_request_groups(v["slots"])
        if v.get("session") is not None:
            return _pool_result(v, "blocked", "automatic_startup_unsupported",
                                request_ids=recovering, prepared_ids=prepared)
        if c.get("generation") != v["generation"]:
            return _pool_result(v, "busy", "owner_changed", request_ids=recovering,
                                prepared_ids=prepared)
        idle = [item for item in v["slots"] if item["phase"] == "idle"]
        if recovering and not idle:
            return _pool_result(v, "collect_only", "recovery_collection_required",
                                request_ids=recovering, prepared_ids=prepared)
        replacement = dict(
            v, generation=v["generation"] + 1, owner=c["owner"], session=None,
            slots=[dict(item) for item in v["slots"]],
        )
        write(paths, locked, replacement)
        return _pool_result(replacement, "ready", "owner_acquired",
                            request_ids=recovering, prepared_ids=prepared)
    if not matches(v, c):
        raise core.StateError("Resident pool owner replaced")
    if action == "inspect":
        return {"ok": True, "owner": v, "automatic_startup": "unsupported"}
    if action == "check":
        return {"ok": True, "owner": operational(v), "automatic_startup": "unsupported"}
    if action == "bind-session":
        live = any(item["invocation"] is not None and item["phase"] != "collect_only"
                   for item in v["slots"])
        if v["session"] is not None or live:
            raise core.BusyError("Session binding requires a fresh unreserved owner")
        s = c.get("session")
        validate_session(s)
        directory = Path(s["directory"])
        if str(directory.resolve(strict=True)) != s["directory"]:
            raise core.StateError("Physical session directory required")
        from .queue import read_private
        raw = read_private(directory / "session.json", limit=16384)
        descriptor = json.loads(raw)
        expected = {
            "sessionId": s["session_id"], "parent": v["parent"],
            "configDir": str(paths.config_dir), "stateDir": str(paths.state_dir),
            "workerPoolSha256": v["worker_pool_sha256"],
        }
        if (hashlib.sha256(raw).hexdigest() != s["descriptor_sha256"]
                or not isinstance(descriptor, dict)
                or descriptor.get("resident") is not True
                or any(descriptor.get(k) != val for k, val in expected.items())):
            raise core.StateError("Session descriptor binding differs")
        value = dict(v, session=s)
        write(paths, locked, value)
        return {"ok": True, "owner": operational(value), "send_authorized": False}
    if action in {"admit", "begin", "end"}:
        if action == "admit":
            return _pool_admit(paths, locked, v, c)
        request_id = c.get("request")
        invocation_id = c.get("invocation")
        core.validate_identifier(request_id, field="request")
        core.validate_identifier(invocation_id, field="invocation")
        slot_id = c.get("slot")
        if slot_id is None and action == "begin":
            already = next((item for item in v["slots"]
                            if item["request"] == request_id), None)
            if already is not None:
                slot_id = already["slot"]
            else:
                idle = next((item for item in v["slots"]
                             if item["phase"] == "idle"
                             and not _other_claim_occupies(paths, locked, item, request_id)),
                            None)
                if idle is None:
                    raise core.BusyError("No idle resident pool slot")
                slot_id = idle["slot"]
        core.validate_identifier(slot_id, field="worker_slot")
        slot = _pool_slot(v, slot_id)
        if slot is None:
            raise core.StateError("Unknown resident pool slot")
        if action == "begin" and _other_claim_occupies(paths, locked, slot, request_id):
            raise core.BusyError("Resident pool slot is occupied by another claim")
        if c.get("worker") not in {None, slot["worker_conversation_id"]}:
            raise core.StateError("Resident slot worker differs")
        from .queue import Queue
        broker = Queue(paths)
        native_client = paths.state_dir / "native-client" / "requests" / (request_id + ".json")
        if os.path.lexists(broker.path(request_id)) or os.path.lexists(native_client):
            record = broker.load(request_id, _locked=locked)
        elif action == "begin" and not os.path.lexists(core.assignment_path(request_id, paths)):
            # Native pool serve reserves after command admission and before
            # parked-client submit. Helper tests may submit first. A missing
            # queue record has not crossed arm; recovery releases the slot.
            record = None
        else:
            record = broker.load(request_id, _locked=locked)
        if record is None:
            pass
        elif action == "begin" and record["state"] == "queued" and slot["request"] in {None, request_id}:
            # Slot reservation may precede the locked claim write. Recovered
            # begin-without-claim work is still queued on its reserved slot.
            pass
        elif (record.get("parent_task_id") != v["parent"]
                and not (slot["phase"] == "collect_only"
                         and isinstance(_takeover_binding(v, request_id), dict)
                         and record.get("parent_task_id") == _takeover_binding(v, request_id).get("prior_parent"))
                or record.get("worker_conversation_id") not in {
                    None, slot["worker_conversation_id"]
                }
                or record.get("worker_slot", slot_id) not in {None, slot_id}):
            raise core.StateError("Resident slot request association differs")
        if action == "begin":
            if slot["phase"] in {"collect_only", "cancel_pending"}:
                raise core.BusyError(
                    "Collector-only recovery cannot begin a reserved request"
                )
            if slot["request"] is not None:
                if slot["request"] == request_id and slot["invocation"] is None:
                    slot["invocation"] = {"invocation": invocation_id, "request": request_id}
                    slot["phase"] = "reserved"
                    write(paths, locked, v)
                    return {"ok": True, "owner": operational(v), "send_authorized": False,
                            "worker_slot": slot_id}
                if slot["request"] == request_id and slot["invocation"] == {
                    "invocation": invocation_id, "request": request_id
                }:
                    return {"ok": True, "owner": operational(v), "send_authorized": False,
                            "worker_slot": slot_id}
                raise core.BusyError("Resident pool slot is already reserved")
            if record is not None and record["state"] not in {"queued", "claimed"}:
                raise core.StateError("Resident begin requires a queued or claimed request")
            slot["request"] = request_id
            slot["invocation"] = {"invocation": invocation_id, "request": request_id}
            slot["phase"] = "reserved"
            write(paths, locked, v)
            return {"ok": True, "owner": operational(v), "send_authorized": False,
                    "worker_slot": slot_id}
        if slot["phase"] == "collect_only":
            receipt = broker.receipt(record, _locked=locked) if record.get("state") not in {
                "queued", "cancelled"
            } else None
            if receipt is None or receipt.get("status") in core.ACTIVE_STATUSES:
                raise core.BusyError("Resident slot remains unresolved")
            slot["request"] = None
            slot["invocation"] = None
            slot["phase"] = "idle"
            write(paths, locked, v)
            return {"ok": True, "owner": operational(v), "send_authorized": False,
                    "worker_slot": slot_id}
        if slot["invocation"] != {"invocation": invocation_id, "request": request_id}:
            raise core.StateError("Only the original final continuation can release")
        receipt = broker.receipt(record, _locked=locked)
        if receipt is None or receipt.get("status") in core.ACTIVE_STATUSES:
            raise core.BusyError("Resident slot remains unresolved")
        slot["request"] = None
        slot["invocation"] = None
        slot["phase"] = "idle"
        write(paths, locked, v)
        return {"ok": True, "owner": operational(v), "send_authorized": False,
                "worker_slot": slot_id}
    if action == "collector-open":
        result = collector_open(paths, locked, c)
        result["credentials"] = _pool_credentials(c, v, collector_only=True)
        return result
    if action == "collector-close":
        if c.get("collector_only") is not True:
            raise core.StateError("Collector credentials are required to close recovery")
        path = paths.state_dir / "resident-recovery.json"
        if not os.path.lexists(path):
            return {"ok": True, "closed": False, "send_authorized": False}
        marker = _recovery_marker(paths, v)
        if marker[0] == "stale":
            _retire_recovery_marker(paths, v)
            return {"ok": True, "closed": False, "send_authorized": False}
        value = marker[1]
        if (set(value) != {"schema_version", "mode", "owner", "parent", "generation",
                           "pool_sha256", "opened_at"}
                or value.get("schema_version") != 1
                or value.get("mode") != "collector-only"
                or any(value.get(k) != v.get(k)
                       for k in ("owner", "parent", "generation"))
                or value.get("pool_sha256") != v.get("worker_pool_sha256")):
            raise core.StateError("Collector recovery owner differs")
        core._remove_file(path)
        return {"ok": True, "closed": True, "send_authorized": False}
    if action in {"recover-start", "serve-open"}:
        return recovery_start(paths, locked, c)
    if action == "rollback-check":
        raise core.StateError("Older scalar-worker runtime rollback is fenced while worker pool exists")
    raise core.ConfigurationError("Unknown resident pool operation")


def guard(paths, locked, request=None, *, configuration=False, operation=None):
    """Called inside the SAME transaction as each conflicting mutation."""
    v = read(paths, locked)
    if v is None:
        if invocation.get() is not None:
            raise core.StateError("Resident authority disappeared")
        return
    c = invocation.get()
    if configuration:
        raise core.BusyError("Worker reconfiguration is unsupported while resident ownership is enrolled")
    if v.get("version") in (3, 4):
        if not matches(v, c):
            # A recovery collector is a separate, explicitly opened mode. It
            # can publish existing post-arm work only and can never claim,
            # prepare, arm, release, or end a slot.
            if (isinstance(c, dict) and c.get("collector_only") is True
                    and c.get("generation") == v["generation"]
                    and c.get("owner") == v["owner"]
                    and c.get("parent") == v["parent"]
                    and c.get("worker_pool_sha256") == v["worker_pool_sha256"]):
                if request is not None and collector_operation.get() == "observe":
                    _collector_mutex(paths, locked, c, request)
                    return
                raise core.BusyError("Collector-only recovery cannot mutate this request")
            raise core.BusyError("Resident pool owner replaced")
        if isinstance(c, dict) and c.get("takeover_settlement") is True:
            _settlement_binding(paths, locked, v, c, request, operation or settlement_operation.get())
            return
        if c.get("collector_only") is True:
            if request is not None and collector_operation.get() == "observe":
                _collector_mutex(paths, locked, c, request)
                return
            raise core.BusyError("Collector-only recovery cannot mutate this request")
        if c.get("invocation") is None:
            raise core.BusyError("Resident pool invocation is not reserved")
        if request is not None and c.get("request") not in {None, request}:
            raise core.BusyError("Resident pool request identity differs")
        selected = None
        if c.get("slot") is not None:
            selected = _pool_slot(v, c["slot"])
        if selected is None:
            selected = next((item for item in v["slots"]
                             if item["request"] == (request or c.get("request"))), None)
        if selected is None or selected["request"] != (request or c.get("request")):
            raise core.BusyError("Resident pool slot is not reserved for this request")
        if selected["phase"] in {"collect_only", "cancel_pending"}:
            raise core.BusyError("Resident pool request is collector-only")
        if selected["invocation"] != {
            "invocation": c.get("invocation"), "request": selected["request"]
        }:
            raise core.BusyError("Resident pool invocation identity differs")
        if c.get("worker") not in {None, selected["worker_conversation_id"]}:
            raise core.BusyError("Resident pool worker identity differs")
        return
    if isinstance(c, dict) and c.get("collector_only") is True:
        if request is not None and collector_operation.get() == "observe":
            _collector_mutex(paths, locked, c, request)
            return
        raise core.BusyError("Collector-only recovery cannot mutate this request")
    if (not matches(v, c) or v["inflight"] is None
            or c.get("invocation") != v["inflight"]["invocation"]
            or request != v["inflight"]["request"]):
        raise core.BusyError("Resident owns this worker; use its reserved invocation")


def claim_serve_existing(paths, locked, v, c, *, replace_unused=False):
    """Consume pristine serving recovery under the canonical mutation lock.

    Native object/task proof is supplied only by the activation path. This
    helper cannot serve, bind, rotate ownership, or authorize any worker send.
    Explicit unused replacement may detach only this validated consumed session.
    An incomplete/ambiguous claim is intentionally never removable or reusable.
    """
    from .queue import Queue
    locked.validate(paths)
    fields = {"generation", "owner", "parent", "session",
              "worker_pool_sha256" if v and v.get("version") in (3, 4) else "worker"}
    if (set(c) != fields or type(c.get("generation")) is not int
            or not matches(v, c) or c.get("session") != v.get("session")):
        raise core.StateError("Serve-existing owner or session differs")
    session, directory = _bound_session_location(v)
    if session is None or directory is None:
        raise core.StateError("Serve-existing bound session missing")
    _, descriptor = _require_bound_descriptor(paths, v, session, directory)
    helper = Path(__file__).resolve().parents[2] / "skills/codex-pro-dispatch/scripts/pro-dispatch"
    if descriptor.get("helper") != str(helper):
        raise core.StateError("Serve-existing helper differs")
    # A fresh socket can inherit completed, fenced work without ever serving.
    # Only these receipt-bound collect-only slots may accompany a lost body.
    # Unused replacement remains idle-only; this exception only resumes serve.
    broker = Queue(paths)
    recovered = []
    for slot in v.get("slots", []):
        if slot["phase"] == "idle" and slot["request"] is None and slot["invocation"] is None:
            continue
        if (replace_unused or slot["phase"] != "collect_only"
                or slot["invocation"] is not None or not slot["request"]):
            raise core.BusyError("Serve-existing requires idle or completed collect-only slots")
        row = broker.load(slot["request"], _locked=locked)
        receipt = broker.receipt(row, _locked=locked)
        if (row["state"] not in {"claimed", "published", "acknowledged"}
                or not receipt or receipt.get("status") != "complete"
                or receipt.get("no_resend") is not True
                or receipt.get("worker_slot") != slot["slot"]
                or receipt.get("worker_conversation_id") != slot["worker_conversation_id"]
                or type(receipt.get("owner_generation")) is not int
                or receipt["owner_generation"] >= v["generation"]):
            raise core.BusyError("Serve-existing recovery receipt is not completed and fenced")
        recovered.append(slot["request"])
    if (v.get("inflight") is not None
            or core.active_assignments(paths, _locked=locked)
            or core.active_cooldown(paths, _locked=locked)
            or any(row["state"] == "claimed" and row["request_id"] not in recovered
                   for row in broker.records(_locked=locked))
            or _recovery_marker(paths, v) is not None
            or os.path.lexists(paths.state_dir / "native-client")):
        raise core.BusyError("Serve-existing requires idle canonical state")
    # Exact allowlist rejects unknown, malformed, historical, and partial
    # claims, waiters, tickets, readiness, requests, audits and failure evidence.
    inventory = {"session.json", "wake.sock"}
    if replace_unused:
        from .queue import read_private
        inventory.add("resident-serve-existing.json")
        if json.loads(read_private(directory / "resident-serve-existing.json")) != c:
            raise core.StateError("Consumed serving claim differs")
    if {item.name for item in directory.iterdir()} != inventory:
        raise core.StateError("Serve-existing session has prior or ambiguous evidence")
    info = (directory / "wake.sock").lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise core.StateError("Serve-existing socket missing or malformed")
    marker = directory / ("resident-unused-replacement.json" if replace_unused
                          else "resident-serve-existing.json")
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(json.dumps(c, sort_keys=True, separators=(",", ":")).encode())
            output.flush()
            os.fsync(output.fileno())
    finally:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    if replace_unused:
        # The native caller fences the retained object before entering this
        # transaction. Preserve its descriptor and consume marker forever.
        write(paths, locked, dict(v, session=None))
        return {"ok": True, "replaced": True, "send_authorized": False}
    result = {"ok": True, "claimed": True, "send_authorized": False}
    if recovered:
        plan = _pool_result(v, "collect_only", "serve_existing", request_ids=recovered)
        result["recovery"] = {k: plan[k] for k in
                              ("request_id", "request_ids", "prepared_ids", "recovery_bindings")}
    return result


def control(action, credentials, paths=None):
    """Start is CAS; begin reserves; end is the original final continuation only.

    A crashed in-flight invocation is NOT released by elapsed time, idle worker,
    completed answer, or a different invocation. Its native work may still run.
    """
    paths = paths or core.default_paths()
    if not isinstance(credentials, dict):
        raise core.ConfigurationError("Resident credentials must be an object")
    c = credentials
    with core.state_lock(paths, create=False) as locked:
        core.reservation_guard(paths, locked)
        v = read(paths, locked)
        if action == "inspect":
            return {"ok": True, "owner": v,
                    "owner_state": supervision_state(paths, v)}
        if supervision_state(paths, v) == "terminally_detached" and action != "recover-start":
            raise core.StateError("Resident owner is terminally detached; explicit recovery required")
        if action == "claim-serve-existing":
            return claim_serve_existing(paths, locked, v, c)
        if action == "replace-unused-serving":
            return claim_serve_existing(paths, locked, v, c, replace_unused=True)
        if action in {"handoff", "takeover"}:
            raise core.StateError("Handoff requires the native handoff packet; CLI credentials are not authorization")
        if core.worker_pool_active(paths, _locked=locked) or (v and v.get("version") in (3, 4)):
            return _pool_control(action, c, paths, locked, v)
        for k in ("owner", "parent", "worker"):
            core.validate_identifier(c.get(k), field=k)
        if type(c.get("generation")) is not int or c["generation"] < 0:
            raise core.ConfigurationError("Expected resident generation required")
        if action in {"start", "enroll"}:
            from .queue import Queue, read_private
            active = core.active_assignment(paths, _locked=locked)
            broker = Queue(paths)
            queue = broker.records(_locked=locked)
            selected = None
            receipt = None
            def result(state, reason):
                return {"ok": True, "state": state, "reason": reason, "owner": operational(v),
                        "request_id": (v["inflight"]["request"] if v and v["inflight"]
                                       else selected["request_id"] if selected else None),
                        "request_ids": ([selected["request_id"]] if selected else []) +
                        [r["request_id"] for r in sorted(queue, key=lambda r: (r["created_at"], r["request_id"]))
                         if r["state"] == "queued" and r is not selected]}
            qualification = v["qualification"] if v else None
            if action == "enroll":
                if v is not None or c["generation"] != 0:
                    return result("blocked", "already_enrolled")
                raw = read_private(Path(c.get("evidence_file", "")), limit=16384)
                if hashlib.sha256(raw).hexdigest() != c.get("evidence_sha256"):
                    raise core.StateError("Enrollment evidence hash differs")
                proof = json.loads(raw)
                bindings = dict(parent=c["parent"], worker=c["worker"],
                                config_dir=str(paths.config_dir), state_dir=str(paths.state_dir))
                if (not isinstance(proof, dict) or any(proof.get(k) != val for k, val in bindings.items())
                        or proof.get("kind") not in {"fresh_deployment", "legacy_quiescence"}
                        or any(not isinstance(proof.get(k), str) or not proof[k].strip()
                               for k in ("implementation", "observations", "authorization"))):
                    raise core.StateError("Enrollment evidence binding or qualification missing")
                # Records the operator's accepted evidence, not a machine proof
                # that its physical observations are true. Never auto-enroll.
                qualification = dict(sha256=c["evidence_sha256"], evidence=proof)
            elif v is None:
                return result("blocked", "enrollment_required")
            if v and v["inflight"] is not None:
                return result("busy", "original_invocation_not_finished")
            if c["generation"] != (v["generation"] if v else 0):
                return result("busy", "owner_changed")
            if v and (v["parent"] != c["parent"] or v["worker"] != c["worker"]):
                return result("blocked", "owner_identity_changed")
            if core.load_worker(paths, _locked=locked).conversation_id != c["worker"]:
                return result("blocked", "worker_changed")
            claims = [r for r in queue if r["state"] == "claimed"]
            if len(claims) > 1:
                return result("blocked", "multiple_claims")
            if claims:
                selected = claims[0]
                if (selected["parent_task_id"] != c["parent"]
                        or selected["worker_conversation_id"] != c["worker"]):
                    return result("blocked", "request_identity_changed")
                receipt = broker.receipt(selected, _locked=locked)
            if active and (not selected or active["assignment_id"] != selected["request_id"]):
                return result("blocked", "unbound_request")
            if receipt and receipt["status"] == "abandoned":
                return result("blocked", "abandoned_claim_requires_release")
            if selected is None:
                selected = next(iter(sorted((r for r in queue if r["state"] == "queued"),
                                            key=lambda r: (r["created_at"], r["request_id"]))), None)
                if selected and os.path.lexists(core.assignment_path(selected["request_id"], paths)):
                    return result("blocked", "queued_request_has_receipt")
            if core.active_cooldown(paths, _locked=locked):
                return result("blocked", "cooldown")
            v = dict(version=2, generation=c["generation"] + 1, owner=c["owner"],
                     parent=c["parent"], worker=c["worker"], inflight=None, session=None, qualification=qualification)
            write(paths, locked, v)
            return result("collect_only" if receipt and receipt["status"] != "prepared"
                          else "ready", "owner_acquired")
        if not matches(v, c):
            raise core.StateError("Resident owner replaced")
        if action == "check":
            return {"ok": True, "owner": operational(v)}
        if action == "admit":
            return admit(paths, locked, v, c)
        if action == "collector-open":
            return collector_open(paths, locked, c)
        if action == "collector-close":
            path = paths.state_dir / "resident-recovery.json"
            if not os.path.lexists(path):
                return {"ok": True, "closed": False, "send_authorized": False}
            marker = _recovery_marker(paths, v)
            if marker[0] == "stale":
                _retire_recovery_marker(paths, v)
                return {"ok": True, "closed": False, "send_authorized": False}
            value = marker[1]
            if (value.get("mode") != "collector-only"
                    or value.get("owner") != v.get("owner")
                    or value.get("parent") != v.get("parent")
                    or value.get("generation") != v.get("generation")):
                raise core.StateError("Collector recovery owner differs")
            core._remove_file(path)
            return {"ok": True, "closed": True, "send_authorized": False}
        if action in {"recover-start", "serve-open"}:
            raise core.StateError(
                "Serving takeover after runtime loss requires an explicit pool recovery"
            )
        if action == "rollback-check":
            return {"ok": True, "rollback": "legacy_scalar_runtime_compatible",
                    "automatic_startup": "unsupported", "send_authorized": False}
        if action == "bind-session":
            from .queue import read_private
            if v["version"] != 2 or v["inflight"] is not None or v["session"] is not None:
                raise core.BusyError("Session binding requires a fresh unreserved owner")
            s = c.get("session")
            validate_session(s)
            directory = Path(s["directory"])
            if str(directory.resolve(strict=True)) != s["directory"]:
                raise core.StateError("Physical session directory required")
            raw = read_private(directory / "session.json", limit=16384)
            descriptor = json.loads(raw)
            expected = dict(sessionId=s["session_id"], parent=v["parent"], worker=v["worker"],
                            configDir=str(paths.config_dir), stateDir=str(paths.state_dir))
            if (hashlib.sha256(raw).hexdigest() != s["descriptor_sha256"]
                    or not isinstance(descriptor, dict) or descriptor.get("resident") is not True
                    or any(descriptor.get(k) != val for k, val in expected.items())):
                raise core.StateError("Session descriptor binding differs")
            v["session"] = s
            write(paths, locked, v)
            return {"ok": True, "owner": operational(v)}
        for k in ("invocation", "request"):
            core.validate_identifier(c.get(k), field=k)
        f = {k: c[k] for k in ("invocation", "request")}
        if action == "begin":
            if v["inflight"] is not None:
                raise core.BusyError("Resident invocation already reserved")
            active = core.active_assignment(paths, _locked=locked)
            if active and active["assignment_id"] != c["request"]:
                raise core.BusyError("Another request remains unresolved")
            v["inflight"] = f
        elif action == "end":
            if v["inflight"] != f:
                raise core.StateError("Only the original final continuation can release")
            v["inflight"] = None
        else:
            raise core.ConfigurationError("Unknown resident operation")
        write(paths, locked, v)
        return {"ok": True, "owner": operational(v)}
