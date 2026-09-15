"""Private local requests. This module has no transport or send capability."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import functools

from . import core
from .native_storage import Directory, IntegrityError, decode


def check(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise core.ConfigurationError("Queue storage requires private owned directories and regular files")
    if not directory and info.st_nlink != 1:
        raise core.ConfigurationError("Queue storage rejects hard links")


def directory(path):
    core._secure_directory(path)


def read_private(path, limit=4 * 1024 * 1024):
    try:
        with Directory(path.parent.absolute()) as parent:
            return parent.read(path.name, limit)
    except FileNotFoundError:
        raise
    except (IntegrityError, OSError) as exc:
        raise core.ConfigurationError("Private file integrity failure") from exc


def snapshot(function):
    @functools.wraps(function)
    def read(self, *args, _locked=None, **kwargs):
        with core.state_lock(self.paths, token=_locked, create=False) as token:
            return function(self, *args, _locked=token, **kwargs)
    return read


class Queue:
    def __init__(self, paths=None):
        self.paths = paths or core.default_paths()
        self.root = self.paths.state_dir / "queue"

    @contextlib.contextmanager
    def locked(self, create=True, *, _locked=None):
        with core.state_lock(self.paths, token=_locked, create=create) as locked:
            if create:
                core.reservation_guard(self.paths, locked)
            for path in (self.root, self.paths.config_dir, self.paths.assignments_dir):
                if create:
                    directory(path)
                elif os.path.lexists(path):
                    with Directory(path.absolute()):
                        pass
            for path in (self.paths.worker_file, *self.paths.assignments_dir.glob("*.json")):
                if os.path.lexists(path):
                    check(path)
            yield locked

    def path(self, rid):
        core.validate_identifier(rid, field="request_id")
        return self.root / (rid + ".json")

    @snapshot
    def load(self, rid, *, _locked):
        self.path(rid)
        if os.path.lexists(self.paths.state_dir / "native-client" / "requests" / (rid + ".json")):
            raise core.StateError("Request belongs to unsupported native-client storage; preserve it")
        try:
            record = decode(read_private(self.path(rid), limit=32 * 1024 * 1024))
        except FileNotFoundError:
            raise core.StateError("Queue request not found") from None
        except (ValueError, UnicodeError):
            raise core.StateError("Invalid queue record") from None
        if not isinstance(record, dict) or record.get("request_id") != rid:
            raise core.StateError("Queue record identity mismatch")
        states = {"queued", "claimed", "published", "acknowledged", "cancelled", "released"}
        if type(record.get("schema_version")) is not int or record["schema_version"] != 1 or not isinstance(record.get("state"), str) or record["state"] not in states:
            raise core.StateError("Unsupported queue record schema or lifecycle")
        if not all(isinstance(record.get(k), str) and record[k] for k in ("fingerprint", "created_at")):
            raise core.StateError("Queue record is missing submission metadata")
        if record["state"] in {"queued", "claimed", "published"} and not isinstance(record.get("prompt"), str):
            raise core.StateError("Queue record is missing retained prompt")
        if record["state"] not in {"queued", "cancelled"}:
            fields = ("parent_task_id", "worker_conversation_id", "queue_claim_token", "prompt_sha256", "wrapped_prompt_sha256", "result_protocol")
            if not all(isinstance(record.get(k), str) and record[k] for k in fields):
                raise core.StateError("Queue record is missing ownership association")
            if not all(core.IDENTIFIER_PATTERN.fullmatch(record[k]) for k in ("parent_task_id", "worker_conversation_id")):
                raise core.StateError("Queue ownership identity is invalid")
        if "native_read" in record and not isinstance(record["native_read"], str):
            raise core.StateError("Invalid staged history record")
        if record["state"] == "published" and (not isinstance(record.get("answer"), dict) or not isinstance(record.get("native_read"), str)):
            raise core.StateError("Published queue record is missing its answer or history")
        return record

    def save(self, record, *, _locked):
        _locked.validate(self.paths)
        core.reservation_guard(self.paths, _locked)
        path = self.path(record["request_id"])
        core.atomic_write_json(path, record, _locked=_locked)

    def clean_temps(self, rid, *, _locked):
        _locked.validate(self.paths)
        # Legacy cleanup only; new descriptor-writer failure temps are retained.
        for path in self.root.glob(f".{core.sha256_text(rid)}.request-*.tmp"):
            check(path)
            path.unlink()
        fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @snapshot
    def records(self, *, _locked):
        return [self.load(p.stem, _locked=_locked) for p in sorted(self.root.glob("*.json"))]

    def receipt(self, r, *, _locked):
        path = core.assignment_path(r["request_id"], self.paths)
        if not os.path.lexists(path):
            if r.get("receipt_established") or r["state"] in {"published", "acknowledged", "released"}:
                raise core.StateError("Bound dispatch receipt is missing; operator recovery required")
            return None
        directory(path.parent)
        check(path)
        receipt = core.load_assignment(r["request_id"], self.paths, _locked=_locked)
        for key in ("parent_task_id", "worker_conversation_id", "queue_claim_token", "prompt_sha256", "wrapped_prompt_sha256", "result_protocol"):
            if key not in r or receipt.get(key) != r[key]:
                raise core.StateError("Queue receipt association mismatch")
        if receipt.get("continuation_of"):
            raise core.StateError("Queue cannot bind a continuation receipt")
        return receipt

    def metadata(self, r, *, _locked):
        result = {k: r[k] for k in ("request_id", "state", "created_at", "fingerprint", "client_session_id", "parent_task_id", "worker_conversation_id", "blocked_reason") if k in r}
        receipt = self.receipt(r, _locked=_locked) if r["state"] not in {"queued", "cancelled"} else None
        result.update(dispatch_status=receipt["status"] if receipt else None,
                      sent_verified=bool(receipt and receipt.get("outbound_prompt_verified")),
                      send_may_have_occurred=bool(receipt and receipt["status"] != "prepared"),
                      send_authorized=False)
        return result

    def validate_submission(self, rid, raw, client_session_id=None):
        """Input checks only: no state writes, admission, or send authority."""
        self.path(rid)
        if client_session_id is not None:
            core.validate_identifier(client_session_id, field="client_session_id")
        try:
            prompt = raw.decode("utf-8")
        except UnicodeError:
            raise core.ConfigurationError("Prompt must be UTF-8") from None
        wrapped = core.wrap_prompt(prompt, rid)
        if len(wrapped.encode("utf-16-le")) // 2 >= 20000:
            raise core.ConfigurationError("Wrapped prompt reaches the native read limit")
        return prompt

    def submit(self, rid, raw, client_session_id=None):
        prompt = self.validate_submission(rid, raw, client_session_id)
        fingerprint = hashlib.sha256(json.dumps([prompt, client_session_id], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        with self.locked() as locked:
            if os.path.lexists(self.path(rid)):
                r = self.load(rid, _locked=locked)
                if r["fingerprint"] != fingerprint:
                    raise core.StateError("Request ID already has different content or metadata")
                return self.metadata(r, _locked=locked)
            if os.path.lexists(self.paths.state_dir / "native-client" / "requests" / (rid + ".json")):
                raise core.StateError("Request ID belongs to native client")
            if os.path.lexists(core.assignment_path(rid, self.paths)):
                raise core.StateError("Request ID already exists as a dispatch assignment")
            r = dict(schema_version=1, request_id=rid, state="queued", created_at=core.utc_now(), fingerprint=fingerprint,
                     client_session_id=client_session_id, prompt=prompt)
            self.save(r, _locked=locked)
            return self.metadata(r, _locked=locked)

    def status(self, rid=None):
        if rid is not None:
            self.path(rid)
        with self.locked(create=False) as locked:
            return self.metadata(self.load(rid, _locked=locked), _locked=locked) if rid else {"requests": [self.metadata(r, _locked=locked) for r in self.records(_locked=locked)]}

    def collect(self, rid):
        self.path(rid)
        with self.locked(create=False) as locked:
            r = self.load(rid, _locked=locked)
            return dict(self.metadata(r, _locked=locked), **({"answer": r["answer"]} if r["state"] == "published" else {"body_available": False}))

    def resume_check(self, rid, fingerprint, worker_conversation_id,
                     client_session_id, raw_prompt_sha256):
        """Read-only eligibility snapshot, never claim or send permission.

        The caller must separately prove the closed native session association.
        Queued records have no owner yet; this check does not assign one.
        """
        for field, value in (
            ("request_id", rid),
            ("worker_conversation_id", worker_conversation_id),
            ("client_session_id", client_session_id),
        ):
            if not isinstance(value, str):
                raise core.ConfigurationError("Invalid queued-resume identity")
            core.validate_identifier(value, field=field)
        for value in (fingerprint, raw_prompt_sha256):
            if (not isinstance(value, str) or len(value) != 64 or
                    any(c not in "0123456789abcdef" for c in value)):
                raise core.ConfigurationError("Expected lowercase SHA-256")

        with self.locked(create=False) as locked:
            core.reservation_guard(self.paths, locked)
            # A missing active slot does not prove this ID never had a receipt.
            # read_private also rejects stale write temporaries and unsafe paths.
            try:
                read_private(core.assignment_path(rid, self.paths))
            except FileNotFoundError:
                pass
            else:
                raise core.StateError("Queued resume requires no dispatch receipt")
            if core.active_assignment(self.paths, _locked=locked):
                raise core.BusyError("Another dispatch is unresolved")
            if core.active_cooldown(self.paths, _locked=locked):
                raise core.CooldownError("Active cooldown blocks queued resume")
            worker = core.load_worker(self.paths, _locked=locked)
            if worker.conversation_id != worker_conversation_id:
                raise core.StateError("Configured worker differs from queued-resume expectation")

            # Decode the selected record strictly before scanning the queue.
            # This keeps ambiguous JSON a configuration failure rather than
            # letting the queue scan turn it into a generic state failure.
            selected_path = self.path(rid)
            if not os.path.lexists(selected_path):
                raise core.StateError("Existing queued request is required")
            r = core.read_json(selected_path)
            records = self.records(_locked=locked)
            if any(r["state"] == "claimed" for r in records):
                raise core.BusyError("Outstanding queue claim blocks queued resume")
            if not any(r["request_id"] == rid for r in records):
                raise core.StateError("Existing queued request is required")

            # Accept only the original submit shape, not a rolled-back claim.
            if set(r) != {
                "schema_version", "request_id", "state", "created_at",
                "fingerprint", "client_session_id", "prompt",
            } or r["state"] != "queued":
                raise core.StateError("Resume requires an untouched queued request")
            if r["client_session_id"] != client_session_id:
                raise core.StateError("Queued client-session identity differs")
            try:
                raw = r["prompt"].encode("utf-8")
                actual_fingerprint = hashlib.sha256(json.dumps(
                    [r["prompt"], r["client_session_id"]],
                    ensure_ascii=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                actual_prompt_hash = hashlib.sha256(raw).hexdigest()
                wrapped = core.wrap_prompt(r["prompt"], rid)
                wrapped_units = len(wrapped.encode("utf-16-le")) // 2
            except UnicodeError as exc:
                raise core.StateError("Queued request contains invalid text") from exc
            if (r["fingerprint"] != fingerprint or
                    actual_fingerprint != fingerprint or
                    actual_prompt_hash != raw_prompt_sha256):
                raise core.StateError("Queued request fingerprint or raw prompt hash differs")
            if wrapped_units >= 20000:
                raise core.StateError("Queued request reaches the native read limit")
            locked.validate(self.paths)
            return {
                "request_id": rid,
                "state": "queued",
                "fingerprint": actual_fingerprint,
                "client_session_id": client_session_id,
                "raw_prompt_sha256": actual_prompt_hash,
                "worker_conversation_id": worker.conversation_id,
                "worker_model_confirmation": worker.model_confirmation,
                "assignment_absent": True,
                "resume_eligible": True,
                "send_authorized": False,
                "paths": {
                    "config_dir": str(self.paths.config_dir),
                    "state_dir": str(self.paths.state_dir),
                },
            }

    def cleanup(self, rid, acknowledge=False):
        self.path(rid)
        with self.locked() as locked:
            r = self.load(rid, _locked=locked)
            if acknowledge:
                receipt = self.receipt(r, _locked=locked)
                if receipt is None or receipt["status"] != "complete":
                    raise core.StateError("Acknowledgement requires the bound completed receipt")
            target = "acknowledged" if acknowledge else "cancelled"
            if r["state"] == target:
                self.clean_temps(rid, _locked=locked)
                return self.metadata(r, _locked=locked)
            if r["state"] != ("published" if acknowledge else "queued"):
                raise core.StateError("Request cannot be acknowledged or cancelled in its current state")
            r["state"] = target
            for key in ("prompt", "native_read", "answer"):
                r.pop(key, None)
            self.save(r, _locked=locked)
            self.clean_temps(rid, _locked=locked)
            return self.metadata(r, _locked=locked)

    def claim(self, parent, confirmed=False, request_id=None, *,
              expected_worker_conversation_id=None):
        core.validate_identifier(parent, field="parent_task_id")
        if request_id is not None:
            self.path(request_id)
        if expected_worker_conversation_id is not None:
            core.validate_identifier(expected_worker_conversation_id,
                                     field="expected_worker_conversation_id")
        if not confirmed:
            raise core.StateError("Broker requires the live desktop six-capability preflight")
        with self.locked() as locked:
            core.reservation_guard(self.paths, locked)
            worker = (core.load_worker(self.paths, _locked=locked)
                      if expected_worker_conversation_id is not None else None)
            if worker is not None and worker.conversation_id != expected_worker_conversation_id:
                raise core.StateError(
                    "Configured worker does not match expected session worker",
                    details={"expected_worker_conversation_id": expected_worker_conversation_id,
                             "worker_conversation_id": worker.conversation_id},
                )
            records = self.records(_locked=locked)
            target = None
            if request_id is not None:
                target = self.load(request_id, _locked=locked)
                if target["state"] not in {"queued", "claimed"}:
                    raise core.StateError("Requested item is not claimable; collect its existing state")
            claimed = [r for r in records if r["state"] == "claimed"]
            if len(claimed) > 1:
                raise core.StateError("Multiple outstanding queue claims")
            if claimed:
                r = claimed[0]
                if request_id is not None and r["request_id"] != request_id:
                    raise core.BusyError("Another request has an outstanding claim",
                                         details={"request_id": r["request_id"]})
                if r["parent_task_id"] != parent:
                    raise core.StateError("Outstanding request belongs to another desktop parent")
                if (expected_worker_conversation_id is not None and
                        r["worker_conversation_id"] != expected_worker_conversation_id):
                    raise core.StateError("Claimed worker does not match expected session worker",
                                          details={"request_id": r["request_id"]})
            else:
                queued = sorted((r for r in records if r["state"] == "queued"), key=lambda r: (r["created_at"], r["request_id"]))
                if not queued:
                    return {"action": "empty", "send_authorized": False}
                r = target if target is not None else queued[0]
                if os.path.lexists(core.assignment_path(r["request_id"], self.paths)):
                    raise core.StateError("Refusing preexisting dispatch assignment")
                active = core.active_assignment(self.paths, _locked=locked)
                if active:
                    raise core.BusyError("Another dispatch is unresolved",
                                         details={"assignment_id": active.get("assignment_id"),
                                                  "status": active.get("status")})
                if worker is None:
                    worker = core.load_worker(self.paths, _locked=locked)
                r.update(state="claimed", parent_task_id=parent,
                         worker_conversation_id=worker.conversation_id,
                         queue_claim_token=secrets.token_hex(32), result_protocol=core.BOUNDED_RESULT_PROTOCOL,
                         prompt_sha256=core.sha256_text(core.normalize_newlines(r["prompt"]).strip()),
                         wrapped_prompt_sha256=core.sha256_text(core.wrap_prompt(r["prompt"], r["request_id"])))
                self.save(r, _locked=locked)  # Ownership and association precede receipt creation.
            receipt = self.receipt(r, _locked=locked)
            wrapped = core.wrap_prompt(r["prompt"], r["request_id"])
            if core.sha256_text(wrapped) != r["wrapped_prompt_sha256"]:
                raise core.StateError("Prepared prompt reconstruction changed; blocked")
            if receipt is None:
                if worker is None:
                    worker = core.load_worker(self.paths, _locked=locked)
                if worker.conversation_id != r["worker_conversation_id"]:
                    raise core.StateError("Configured worker changed during claim recovery")
                core.prepare_assignment(r["prompt"], parent_task_id=parent, assignment_id=r["request_id"], queue_claim_token=r["queue_claim_token"], paths=self.paths, _locked=locked)
                receipt = self.receipt(r, _locked=locked)
            if not r.get("receipt_established"):
                r["receipt_established"] = True
                self.save(r, _locked=locked)
            result = self.metadata(r, _locked=locked)
            result["assignment_id"] = r["request_id"]
            result["action"] = "arm_then_send_once" if receipt["status"] == "prepared" else "collect_only"
            if receipt["status"] == "prepared":
                result["wrapped_prompt"] = wrapped
            return result

    def release(self, rid, parent):
        self.path(rid)
        core.validate_identifier(parent, field="parent_task_id")
        with self.locked() as locked:
            r = self.load(rid, _locked=locked)
            if r.get("parent_task_id") != parent:
                raise core.StateError("Release requires the recorded desktop parent")
            receipt = self.receipt(r, _locked=locked)
            if receipt is None or receipt["status"] != "abandoned":
                raise core.StateError("Release requires an explicitly abandoned dispatch receipt")
            if r["state"] == "released":
                self.clean_temps(rid, _locked=locked)
                return self.metadata(r, _locked=locked)
            if r["state"] != "claimed":
                raise core.StateError("Only an outstanding claim can be released")
            r["state"] = "released"
            for key in ("prompt", "native_read", "answer", "blocked_reason"):
                r.pop(key, None)
            self.save(r, _locked=locked)
            self.clean_temps(rid, _locked=locked)
            return self.metadata(r, _locked=locked)

    def observe(self, rid, parent, confirmed=False, native_read=None):
        """Observe existing post-arm work; never claim, arm, or send.

        Pending snapshots do not mutate request/receipt records. A candidate
        must pass the existing native-summary and short-envelope validators.
        Publication and interrupted-publication recovery retain core authority.
        """
        self.path(rid)
        core.validate_identifier(parent, field="parent_task_id")
        if not confirmed:
            raise core.StateError("Broker requires the live desktop six-capability preflight")
        if native_read is not None and (
            not isinstance(native_read, bytes) or len(native_read) > 4 * 1024 * 1024
        ):
            raise core.ConfigurationError("Native history must be bytes within 4 MiB")

        with self.locked(create=False) as locked:
            r = self.load(rid, _locked=locked)
            if r.get("parent_task_id") != parent:
                raise core.StateError("Observation requires the recorded desktop parent")
            if r["state"] not in {"claimed", "published"}:
                raise core.StateError("Request is not observable; collect existing state")
            receipt = self.receipt(r, _locked=locked)
            if receipt is None:
                raise core.StateError("Bound dispatch receipt is missing")
            if receipt["status"] not in {
                "armed", "submitted", "pending", "indeterminate", "ambiguous", "complete"
            } or receipt.get("no_resend") is not True:
                raise core.StateError("Observation requires existing post-arm work")
            if receipt.get("result_protocol") != core.BOUNDED_RESULT_PROTOCOL:
                raise core.StateError("Observation requires bounded-footer-v1")

            staged = r.get("native_read")
            if staged is not None:
                staged_bytes = staged.encode("utf-8")
                if native_read is not None and native_read != staged_bytes:
                    raise core.StateError("Staged native history is immutable")
                native_read = staged_bytes
            elif native_read is None:
                # A complete receipt without a stage (core completed while the
                # queue publication was lost) is recovered from a read-only
                # history fetch that must match the immutable receipt below.
                raise core.StateError("No native snapshot or staged history")

            if len(native_read) > 4 * 1024 * 1024:
                raise core.ConfigurationError("Native history exceeds 4 MiB")

            try:
                response, association = core._native_response(native_read, receipt)
            except core.StateError as exc:
                reason = str(exc).partition(":")[0]
                if staged is None and reason in {
                    "native-worker-not-idle", "native-reply-not-observed"
                }:
                    return dict(
                        self.metadata(r, _locked=locked),
                        observation="pending",
                        reason_code=reason,
                        no_resend=True,
                    )
                raise

            parsed = core._parse_result(response, rid)
            if parsed.result_kind != "short":
                raise core.StateError(
                    "Queue observer requires a short result; operator resolution required"
                )

            if receipt.get("outbound_prompt_verified") is not True:
                # _native_response already validated the complete JSON, unique
                # matching turn, item ordering, and exact wrapped-prompt hash.
                # Extract its returned text without normalization/reconstruction.
                data = json.loads(native_read.decode("utf-8"))
                turn = next(
                    item for item in data["turns"]
                    if item["id"] == association["turn_id"]
                )
                sent_prompt = turn["items"][0]["content"][0]["text"]
                core.mark_submitted(
                    rid, sent_prompt, self.paths, _locked=locked
                )
            elif receipt.get("submission_count") != 1:
                raise core.StateError("Verified submission count is invalid")

            result = self.publish(
                rid, parent, confirmed=True,
                native_read=native_read, _locked=locked,
            )
            return dict(result, observation="published", no_resend=True)

    def publish(self, rid, parent, confirmed=False, native_read=None, *, _locked=None):
        self.path(rid)
        core.validate_identifier(parent, field="parent_task_id")
        if not confirmed:
            raise core.StateError("Broker requires the live desktop six-capability preflight")
        with self.locked(_locked=_locked) as locked:
            r = self.load(rid, _locked=locked)
            if r.get("parent_task_id") != parent:
                raise core.StateError("Publication requires the recorded desktop parent")
            if r["state"] not in {"claimed", "published"}:
                raise core.StateError("Request is not publishable; acknowledged bodies cannot be regenerated")
            receipt = self.receipt(r, _locked=locked)
            if receipt is None:
                raise core.StateError("Dispatch receipt is missing")
            staged = r.get("native_read")
            if native_read is None:
                if staged is None:
                    raise core.StateError("No staged native history")
                native_read = staged.encode("utf-8")
            if len(native_read) > 4 * 1024 * 1024:
                raise core.ConfigurationError("Native history exceeds 4 MiB")
            if staged is not None and native_read != staged.encode("utf-8"):
                raise core.StateError("Staged native history is immutable")
            try:
                response, _ = core._native_response(native_read, receipt)
                parsed = core._parse_result(response, rid)
                if parsed.result_kind != "short":
                    raise core.StateError("Queue v0 requires a short result; continuation requires operator resolution")
            except core.DispatchError:
                if r["state"] == "claimed":
                    r["blocked_reason"] = "native-result-rejected"
                    self.save(r, _locked=locked)
                raise
            completed = None
            if staged is None:
                if receipt["status"] == "complete":
                    # Immutable response/message-identity revalidation must
                    # precede staging, or a conflicting history would become
                    # an immutable poisoned stage. This call does not mutate.
                    completed = core.complete_assignment(rid, b"", self.paths, native_read=native_read, _locked=locked)
                r["native_read"] = native_read.decode("utf-8")
                self.save(r, _locked=locked)  # Durable before the core completion transition.
            value, payload = completed or core.complete_assignment(rid, b"", self.paths, native_read=native_read, _locked=locked)
            if value.get("verification_level") != "bounded_native_summary":
                raise core.StateError("Queue requires native verification")
            if r["state"] == "published":
                return dict(self.metadata(r, _locked=locked), answer=r["answer"])
            r["answer"] = {k: value[k] for k in ("assignment_id", "parent_task_id", "worker_conversation_id", "payload_sha256", "response_sha256", "verification_level", "outbound_prompt_verified", "source_bytes_verified", "generation_finality_verified")}
            r["answer"].update(payload=payload, result_kind="short")
            r["state"] = "published"
            r.pop("blocked_reason", None)
            self.save(r, _locked=locked)
            return dict(self.metadata(r, _locked=locked), answer=r["answer"])
