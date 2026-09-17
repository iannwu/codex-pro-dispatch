"""Pool migration preserves history while retaining unresolved worker ownership."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_pro_dispatch import core
from codex_pro_dispatch.queue import Queue


class PoolMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = core.RuntimePaths(self.root / "config", self.root / "state")
        core.save_worker("retired-worker", confirm_worker=True, paths=self.paths)
        self.legacy = self.paths.worker_file.read_bytes()
        self.evidence = self.root / "evidence.json"
        self.evidence.write_text(json.dumps({
            "kind": "legacy_quiescence", "config_dir": str(self.paths.config_dir),
            "state_dir": str(self.paths.state_dir), "physical_quiescence": True,
            "implementation": "isolated fixture", "observations": "no live worker",
            "authorization": "test-only maintenance",
        }))
        self.evidence.chmod(0o600)
        self.queue = Queue(self.paths)

    def activate(self, retain=False):
        return core.activate_worker_pool([
            {"slot": "a", "conversation_id": "retired-worker" if retain else "new-a",
             "label": "A", "model_confirmation": "user-confirmed-worker", "configured_at": "fixture"},
            {"slot": "b", "conversation_id": "new-b", "label": "B",
             "model_confirmation": "user-confirmed-worker", "configured_at": "fixture"},
        ], expected_legacy_sha256=hashlib.sha256(self.legacy).hexdigest(),
            evidence_file=self.evidence,
            evidence_sha256=hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
            paths=self.paths)

    def claim(self, rid):
        self.queue.submit(rid, b"fixture prompt", "client")
        return self.queue.claim("parent", confirmed=True, request_id=rid)

    def history(self, rid, claim):
        return json.dumps({
            "schemaVersion": 1,
            "thread": {"id": "retired-worker", "kind": "chatgpt", "status": {"type": "idle"}},
            "turns": [{"id": "turn-" + rid, "items": [
                {"id": "turn-" + rid, "type": "userMessage",
                 "content": [{"type": "text", "text": claim["wrapped_prompt"]}]},
                {"id": "answer-" + rid, "type": "agentMessage",
                 "text": core.result_marker(rid) + "\nfixture answer\n" + core.end_marker(rid)},
            ]}],
        }).encode()

    def publish(self, rid):
        claim = self.claim(rid)
        core.arm_assignment(rid, self.paths)
        core.mark_submitted(rid, claim["wrapped_prompt"], self.paths)
        return self.queue.publish(rid, "parent", confirmed=True,
                                  native_read=self.history(rid, claim))

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for directory in (self.paths.config_dir, self.paths.state_dir)
                for p in directory.rglob("*") if p.is_file()}

    def assert_refused_unchanged(self, message):
        before = self.snapshot()
        with self.assertRaisesRegex(core.StateError, message):
            self.activate()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.paths.worker_pool_file.exists())

    def test_historical_records_allow_retirement_and_durable_collection(self):
        self.publish("acknowledged")
        self.queue.cleanup("acknowledged", acknowledge=True)
        self.claim("released")
        core.abandon_assignment("released", reason="fixture retired", paths=self.paths)
        self.queue.release("released", "parent")
        answer = self.publish("published")["answer"]
        self.queue.submit("cancelled", b"fixture prompt", "client")
        self.queue.cleanup("cancelled")
        self.queue.submit("queued", b"fixture prompt", "client")
        before = self.snapshot()
        self.activate()
        after = self.snapshot()
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertEqual(set(after) - set(before), {"config/worker-pool.json"})
        self.assertEqual(self.queue.collect("published")["answer"], answer)
        self.queue.cleanup("published", acknowledge=True)
        for rid in ("acknowledged", "released", "published"):
            with self.subTest(rid=rid):
                self.assertFalse(self.queue.collect(rid)["body_available"])
                with self.assertRaises(core.StateError):
                    self.queue.claim("parent", confirmed=True, request_id=rid)
        claim = self.queue.claim("parent", confirmed=True, request_id="queued")
        self.assertEqual(claim["worker_conversation_id"], "new-a")

    def test_unretained_claim_blocks_before_send(self):
        self.claim("live")
        self.assert_refused_unchanged("Unresolved queue worker")

    def test_published_answer_alone_allows_retirement_before_acknowledgement(self):
        answer = self.publish("published")["answer"]
        before = self.queue.path("published").read_bytes()
        self.activate()
        self.assertEqual(self.queue.path("published").read_bytes(), before)
        self.assertEqual(self.queue.collect("published")["answer"], answer)
        self.queue.cleanup("published", acknowledge=True)
        self.assertEqual(self.queue.status("published")["state"], "acknowledged")

    def test_unretained_claim_blocks_after_arm(self):
        self.claim("live")
        core.arm_assignment("live", self.paths)
        self.assert_refused_unchanged("Unresolved queue worker")

    def test_interrupted_publication_claim_blocks_despite_completed_receipt(self):
        claim = self.claim("live")
        core.arm_assignment("live", self.paths)
        core.mark_submitted("live", claim["wrapped_prompt"], self.paths)
        core.complete_assignment("live", b"", self.paths, native_read=self.history("live", claim))
        self.assertEqual(core.load_assignment("live", self.paths)["status"], "complete")
        self.assertEqual(self.queue.load("live")["state"], "claimed")
        self.assert_refused_unchanged("Unresolved queue worker")

    def test_unreleased_claim_blocks_despite_abandoned_receipt(self):
        self.claim("live")
        core.abandon_assignment("live", reason="fixture", paths=self.paths)
        self.assert_refused_unchanged("Unresolved queue worker")

    def test_retained_claim_remains_collect_only_after_arm(self):
        self.claim("live")
        core.arm_assignment("live", self.paths)
        before = self.snapshot()
        self.activate(retain=True)
        self.assertEqual({key: self.snapshot()[key] for key in before}, before)
        resumed = self.queue.claim("parent", confirmed=True, request_id="live")
        self.assertEqual(resumed["action"], "collect_only")
        self.assertFalse(resumed["send_authorized"])

    def test_active_receipt_still_blocks_even_with_published_history(self):
        self.publish("published")
        path = core.assignment_path("published", self.paths)
        receipt = core.load_assignment("published", self.paths)
        for status in sorted(core.ACTIVE_STATUSES):
            with self.subTest(status=status):
                # Inconsistent state must still fail the independent receipt gate.
                with core.state_lock(self.paths) as locked:
                    core.atomic_write_json(path, dict(receipt, status=status), _locked=locked)
                self.assert_refused_unchanged("Unresolved receipt worker")


if __name__ == "__main__":
    unittest.main()
