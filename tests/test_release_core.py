from pathlib import Path
import tempfile
import unittest
from codex_pro_dispatch import core as c

class CorePortTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="pro-core-unit-")
        self.addCleanup(t.cleanup)
        p = Path(t.name).resolve()
        self.p = c.RuntimePaths(p/"config", p/"state")
        c.save_worker("unit-pro", confirm_pro=True, paths=self.p)

    def test_shared_token(self):
        with c.state_lock(self.p) as token:
            a = c.prepare_assignment("unit", parent_task_id="parent", paths=self.p,
                                     queue_claim_token="claim", _locked=token)
            c.arm_assignment(a.assignment_id, self.p, _locked=token)
            with self.assertRaises(c.StateError):
                c.arm_assignment(a.assignment_id, self.p, _locked=token)
            c.mark_submitted(a.assignment_id, a.wrapped_prompt, self.p, _locked=token)
            r, body = c.complete_assignment(a.assignment_id,
                c.result_marker(a.assignment_id)+"\nanswer\n"+c.end_marker(a.assignment_id),
                self.p, _locked=token)
            self.assertEqual((r["submission_count"], body), (1, "answer"))
            self.assertEqual(r["queue_claim_token"], "claim")
        with self.assertRaises(c.StateError):
            c.load_worker(self.p, _locked=token)

    def test_foreign_store_and_queue_refusal(self):
        before = self.p.worker_file.read_bytes()
        (self.p.state_dir/"native-client").mkdir(mode=0o700)
        for op in (lambda: c.reset_worker(force=True, paths=self.p),
                   lambda: c.purge_local_state(force=True, paths=self.p),
                   lambda: c.redact_stored_diagnostics(self.p)):
            with self.assertRaises(c.StateError): op()
        self.assertEqual(self.p.worker_file.read_bytes(), before)
        self.assertEqual(c.load_worker(self.p).conversation_id, "unit-pro")
        (self.p.state_dir/"native-client").rmdir()  # Empty fixture only.
        (self.p.state_dir/"queue").mkdir(mode=0o700)
        with self.assertRaises(c.StateError):
            c.purge_local_state(force=True, paths=self.p)
