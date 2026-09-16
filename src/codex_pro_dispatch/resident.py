"""One canonical resident owner. No transport, timer, or send capability."""
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path

from . import core


invocation = ContextVar("resident_invocation", default=None)


def read(paths, locked):
    locked.validate(paths)
    path = paths.state_dir / "resident-owner.json"
    if not os.path.lexists(path):
        return None
    v = core.read_json(path)
    if (set(v) != {"version", "generation", "owner", "parent", "worker", "inflight", "qualification"}
            or type(v["version"]) is not int or v["version"] != 1
            or type(v["generation"]) is not int or v["generation"] < 1):
        raise core.StateError("Invalid resident owner record; preserve it")
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


def write(paths, locked, v):
    core.atomic_write_json(paths.state_dir / "resident-owner.json", v, _locked=locked)


def matches(v, credentials):
    return (isinstance(credentials, dict) and v is not None
            and all(credentials.get(k) == v[k] for k in ("generation", "owner", "parent", "worker")))


def operational(v):
    return {k: val for k, val in v.items() if k != "qualification"} if v else None


def guard(paths, locked, request=None, *, configuration=False):
    """Called inside the SAME transaction as each conflicting mutation."""
    v = read(paths, locked)
    if v is None:
        if invocation.get() is not None:
            raise core.StateError("Resident authority disappeared")
        return
    c = invocation.get()
    if configuration:
        raise core.BusyError("Worker reconfiguration is unsupported while resident ownership is enrolled")
    if (not matches(v, c) or v["inflight"] is None
            or c.get("invocation") != v["inflight"]["invocation"]
            or request != v["inflight"]["request"]):
        raise core.BusyError("Resident owns this worker; use its reserved invocation")


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
            return {"ok": True, "owner": v}
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
            v = dict(version=1, generation=c["generation"] + 1, owner=c["owner"],
                     parent=c["parent"], worker=c["worker"], inflight=None, qualification=qualification)
            write(paths, locked, v)
            return result("collect_only" if receipt and receipt["status"] != "prepared"
                          else "ready", "owner_acquired")
        if not matches(v, c):
            raise core.StateError("Resident owner replaced")
        if action == "check":
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
