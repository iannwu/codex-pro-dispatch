"""Issue 18: exact native delivery is independent of assistant completion."""
import copy
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_pro_dispatch import core, resident
from codex_pro_dispatch.queue import Queue


class OutboundDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="outbound-delivery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.paths = core.RuntimePaths(self.root / "config", self.root / "state")
        self.rid, self.parent, self.worker = "delivery-A", "delivery-parent", "delivery-worker"
        core.save_worker(self.worker, confirm_pro=True, paths=self.paths)
        self.queue = Queue(self.paths)
        self.queue.submit(self.rid, "Exact delivery: 中文 🙂\n".encode(), "delivery-client")
        claim = self.queue.claim(self.parent, True, self.rid)
        self.prompt = claim["wrapped_prompt"]
        core.arm_assignment(self.rid, self.paths)

    def document(self, status="active", assistant=False):
        items = [{"id": "delivery-user", "type": "userMessage",
                  "content": [{"type": "text", "text": self.prompt}]}]
        if assistant:
            items.append({"id": "delivery-answer", "type": "agentMessage",
                          "text": core.result_marker(self.rid) + "\nanswer\n"
                                  + core.end_marker(self.rid)})
        return {"schemaVersion": 1,
                "thread": {"id": self.worker, "kind": "chatgpt", "status": {"type": status}},
                "turns": [{"id": "delivery-user", "items": items}]}

    def observe(self, document):
        return self.queue.observe(self.rid, self.parent, True,
                                  json.dumps(document).encode("utf-8"))

    def receipt(self):
        return core.load_assignment(self.rid, self.paths)

    def bytes(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def assert_delivered_pending(self, observed):
        self.assertEqual(observed["observation"], "pending")
        self.assertEqual(observed["state"], "claimed")
        self.assertEqual(observed["dispatch_status"], "submitted")
        self.assertTrue(observed["sent_verified"])
        self.assertTrue(observed["send_may_have_occurred"])
        self.assertFalse(observed["send_authorized"])
        receipt = self.receipt()
        self.assertEqual(receipt["submission_count"], 1)
        self.assertTrue(receipt["submitted_at"])
        self.assertTrue(receipt["outbound_prompt_verified"])
        self.assertTrue(receipt["submission_observed"])
        self.assertTrue(receipt["no_resend"])
        self.assertEqual(receipt["sent_prompt_sha256"], receipt["wrapped_prompt_sha256"])
        self.assertNotIn("submission_may_have_occurred", receipt)
        self.assertNotIn("native_collection", receipt)
        record = self.queue.load(self.rid)
        self.assertNotIn("native_read", record)
        self.assertNotIn("answer", record)
        self.assertEqual(core.active_assignment(self.paths)["assignment_id"], self.rid)

    def test_active_user_only_records_once_across_a_repeated_read(self):
        document = self.document()
        before = self.bytes()
        self.assert_delivered_pending(self.observe(document))
        saved = self.bytes()
        self.assertEqual({k for k in saved if saved[k] != before.get(k)},
                         {"state/assignments/" + self.rid + ".json"})
        receipt = self.receipt()
        with patch.object(core, "mark_submitted", side_effect=AssertionError("Duplicate record")):
            self.assertEqual(self.observe(document)["observation"], "pending")
        self.assertEqual(self.bytes(), saved)
        self.assertEqual(self.receipt(), receipt)
        self.assertEqual(self.queue.claim(self.parent, True, self.rid)["action"], "collect_only")
        with self.assertRaises(core.StateError):
            core.arm_assignment(self.rid, self.paths)

    def test_idle_user_only_records_delivery_without_a_result(self):
        observed = self.observe(self.document("idle"))
        self.assert_delivered_pending(observed)
        self.assertEqual(observed["reason_code"], "native-reply-not-observed")

    def test_stale_or_missing_current_marker_is_write_free_then_exact_verifies(self):
        before = self.bytes()
        for status in ("active", "idle"):
            for text in (None, "old prompt", core.wrap_prompt("old", "delivery-old")):
                document = self.document(status)
                if text is None:
                    document["turns"] = []
                else:
                    document["turns"][0]["items"][0]["content"][0]["text"] = text
                self.assertEqual(self.observe(document)["observation"], "pending")
                self.assertEqual(self.bytes(), before)
        self.assert_delivered_pending(self.observe(self.document()))

    def test_current_marker_hash_mismatch_is_write_free_and_never_rearms(self):
        before = self.bytes()
        for status in ("active", "idle"):
            document = self.document(status)
            document["turns"][0]["items"][0]["content"][0]["text"] += "x"
            with self.assertRaisesRegex(core.StateError, "native-readback-mismatch"):
                self.observe(document)
            self.assertEqual(self.bytes(), before)
        self.assertEqual(self.receipt()["submission_count"], 0)
        self.assertNotIn("submitted_at", self.receipt())
        self.assertTrue(self.receipt()["no_resend"])
        with self.assertRaises(core.StateError):
            core.arm_assignment(self.rid, self.paths)

    def test_concurrent_observers_record_at_most_once(self):
        barrier = threading.Barrier(2)
        document = self.document()

        def observe():
            barrier.wait(timeout=5)
            return self.observe(document)

        with patch.object(core, "mark_submitted", wraps=core.mark_submitted) as submitted:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(observe) for _ in range(2)]
                results = [future.result(timeout=15) for future in futures]
        self.assertEqual(submitted.call_count, 1)
        for result in results:
            self.assert_delivered_pending(result)

    def test_failed_write_and_lost_acknowledgment_recover_collect_only(self):
        document = self.document()
        before = self.bytes()
        with patch.object(core, "mark_submitted", side_effect=OSError("Before write")):
            with self.assertRaises(OSError):
                self.observe(document)
        self.assertEqual(self.bytes(), before)
        original = core.mark_submitted

        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("After durable delivery write")

        with patch.object(core, "mark_submitted", side_effect=lost_ack):
            with self.assertRaises(OSError):
                self.observe(document)
        saved = self.bytes()
        with patch.object(core, "mark_submitted", side_effect=AssertionError("Duplicate record")):
            self.assert_delivered_pending(self.observe(document))
        self.assertEqual(self.bytes(), saved)

    def test_invalid_assistant_never_erases_verified_delivery_or_stages_history(self):
        queue_bytes = self.queue.path(self.rid).read_bytes()
        submitted_at = None
        for kind in ("footer", "type", "truncated", "extra", "cross-turn", "limit"):
            document = self.document("idle", True)
            turn = document["turns"][0]
            assistant = turn["items"][1]
            if kind == "footer":
                assistant["text"] += "\n"
            elif kind == "type":
                assistant["type"] = "userMessage"
            elif kind == "truncated":
                assistant["truncated"] = True
            elif kind == "extra":
                turn["items"].append({"id": "extra", "type": "agentMessage", "text": "extra"})
            elif kind == "cross-turn":
                document["turns"].append({"id": "other-turn", "items": [turn["items"].pop()]})
            else:
                assistant["text"] = "x" * 20000
            if kind == "cross-turn":
                self.assertEqual(self.observe(document)["observation"], "pending")
            else:
                with self.assertRaises(core.DispatchError):
                    self.observe(document)
            receipt = self.receipt()
            self.assertEqual(receipt["status"], "submitted")
            self.assertEqual(receipt["submission_count"], 1)
            self.assertTrue(receipt["outbound_prompt_verified"])
            submitted_at = submitted_at or receipt["submitted_at"]
            self.assertEqual(receipt["submitted_at"], submitted_at)
            self.assertEqual(self.queue.path(self.rid).read_bytes(), queue_bytes)
        result = self.observe(self.document("idle", True))
        self.assertEqual(result["observation"], "published")
        self.assertEqual(result["answer"]["payload"], "answer")
        saved = self.bytes()
        self.assertEqual(self.observe(self.document("idle", True)), result)
        self.assertEqual(self.bytes(), saved)
        self.assertEqual(self.receipt()["submitted_at"], submitted_at)

    def test_active_even_with_framed_assistant_cannot_publish(self):
        self.assert_delivered_pending(self.observe(self.document("active", True)))

    def test_outbound_identity_shape_and_truncation_reject_before_any_write(self):
        cases = []
        for kind in ("worker", "duplicate-turn", "duplicate-item", "duplicate-candidate",
                     "user-id", "user-order", "content", "status", "schema"):
            document = self.document()
            turn = document["turns"][0]
            user = turn["items"][0]
            if kind == "worker":
                document["thread"]["id"] = "wrong"
            elif kind == "duplicate-turn":
                document["turns"].append(copy.deepcopy(turn))
            elif kind == "duplicate-item":
                document["turns"].append({"id": "other", "items": [copy.deepcopy(user)]})
            elif kind == "duplicate-candidate":
                other = copy.deepcopy(turn)
                other["id"] = other["items"][0]["id"] = "other"
                document["turns"].append(other)
            elif kind == "user-id":
                user["id"] = "wrong"
            elif kind == "user-order":
                turn["items"].insert(0, {"id": "earlier", "type": "agentMessage", "text": "old"})
            elif kind == "content":
                user["content"].append({"type": "text", "text": "extra"})
            elif kind == "status":
                document["thread"]["status"] = "idle"
            else:
                document["schemaVersion"] = True
            cases.append(document)
        for scope_index in range(5):
            for key in ("truncated", "textTruncated"):
                for value in (True, "false", None):
                    document = self.document()
                    turn = document["turns"][0]
                    user = turn["items"][0]
                    (document, document["thread"], turn, user, user["content"][0])[scope_index][key] = value
                    cases.append(document)
        before = self.bytes()
        for document in cases:
            with self.assertRaises(core.DispatchError):
                self.observe(document)
            self.assertEqual(self.bytes(), before)
        for raw in (b'{"schemaVersion":1,"schemaVersion":1}', b'{"x":NaN}',
                    b'{"x":1e9999}', b'{"x":"\\ud800"}', b'\xef\xbb\xbf{}', b'{} trailing'):
            with self.assertRaises(core.MarkerError):
                self.queue.observe(self.rid, self.parent, True, raw)
            self.assertEqual(self.bytes(), before)

    def test_legacy_armed_collector_records_with_bound_authority_only(self):
        owner = {"version": 2, "generation": 1, "owner": "delivery-owner",
                 "parent": self.parent, "worker": self.worker,
                 "inflight": {"invocation": "delivery-call", "request": self.rid},
                 "session": None, "qualification": {}}
        with core.state_lock(self.paths) as locked:
            core.atomic_write_json(self.paths.state_dir / "resident-owner.json", owner, _locked=locked)
        credentials = {k: owner[k] for k in ("generation", "owner", "parent", "worker")}
        resident.control("collector-open", credentials, self.paths)
        credentials["collector_only"] = True
        before = self.bytes()
        for field, value in (("generation", 2), ("owner", "wrong"),
                             ("parent", "wrong"), ("worker", "wrong"),
                             ("request_parent", "wrong")):
            token = resident.invocation.set({**credentials, field: value})
            try:
                with self.assertRaises(core.DispatchError):
                    self.observe(self.document())
                self.assertEqual(self.bytes(), before)
            finally:
                resident.invocation.reset(token)
        token = resident.invocation.set(credentials)
        try:
            self.assert_delivered_pending(self.observe(self.document()))
            saved = self.bytes()
            self.assert_delivered_pending(self.observe(self.document()))
            self.assertEqual(self.bytes(), saved)
            self.assertEqual(saved["state/resident-owner.json"], before["state/resident-owner.json"])
            self.assertEqual(saved["state/resident-recovery.json"], before["state/resident-recovery.json"])
            self.assertIsNone(resident.collector_operation.get())
            with self.assertRaises(core.StateError):
                self.queue.claim(self.parent, True, self.rid)
            with self.assertRaises(core.DispatchError):
                core.arm_assignment(self.rid, self.paths)
        finally:
            resident.invocation.reset(token)


if __name__ == "__main__":
    unittest.main()
