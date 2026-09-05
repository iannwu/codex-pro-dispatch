from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import codex_pro_dispatch as cpd
from codex_pro_dispatch.artifact import (
    ArtifactManifest,
    ArtifactVerificationResult,
    ProtectedRef,
    parse_artifact_manifest,
)


class BareArtifactVerifierTests(unittest.TestCase):
    """Exercise the object verifier against disposable local bare remotes.

    The production contract parser deliberately accepts only credential-free
    GitHub HTTPS URLs.  These tests construct the already-validated dataclass
    directly so Git object checks can be tested without any external network or
    repository write.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.work = self.root / "work"
        self.git("init", "--bare", str(self.remote))
        self.git("init", "--initial-branch=main", str(self.work))
        self.git("config", "user.email", "artifact-test@example.invalid", cwd=self.work)
        self.git("config", "user.name", "Artifact Test", cwd=self.work)
        (self.work / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "README.md", cwd=self.work)
        self.git("commit", "-m", "test: base", cwd=self.work)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.work)
        self.git("push", "origin", "main", cwd=self.work)
        self.base_sha = self.git("rev-parse", "HEAD", cwd=self.work).strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ("git", *args),
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout

    def contract(self, *, branch: str = "codex/result-7319") -> cpd.ArtifactContract:
        return cpd.ArtifactContract(
            repository_id=7319,
            repository="owner/repository",
            visibility="private",
            remote_url=str(self.remote),
            base_branch="main",
            base_sha=self.base_sha,
            branch=branch,
            path="docs/result.md",
            commit_message="docs: add dispatched result",
            encoding="utf-8",
            media_type="text/markdown",
            allowed_change="add-single-markdown",
            artifact_max_bytes=2 * 1024 * 1024,
            sensitivity="internal",
            protected_refs=(
                ProtectedRef(
                    ref="refs/heads/main",
                    sha=self.base_sha,
                    allow_fast_forward=True,
                ),
            ),
            prepared_at="2030-01-01T00:00:00.000Z",
        )

    def publish(
        self,
        contract: cpd.ArtifactContract,
        *,
        content: bytes = b"# Verified result\n",
        extra_path: str | None = None,
        executable: bool = False,
    ) -> tuple[str, bytes]:
        self.git("switch", "-c", contract.branch, contract.base_sha, cwd=self.work)
        target = self.work / contract.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if executable:
            os.chmod(target, 0o755)
        self.git("add", contract.path, cwd=self.work)
        if extra_path is not None:
            extra = self.work / extra_path
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text("not authorized\n", encoding="utf-8")
            self.git("add", extra_path, cwd=self.work)
        self.git("commit", "-m", contract.commit_message, cwd=self.work)
        commit = self.git("rev-parse", "HEAD", cwd=self.work).strip()
        self.git("push", "origin", f"HEAD:refs/heads/{contract.branch}", cwd=self.work)
        return commit, content

    def manifest(
        self, contract: cpd.ArtifactContract, *, commit_sha: str, content: bytes
    ) -> ArtifactManifest:
        return ArtifactManifest(
            assignment_id="dispatch-artifact-7319",
            repository_id=contract.repository_id,
            repository=contract.repository,
            remote_url=contract.remote_url,
            base_branch=contract.base_branch,
            base_sha=contract.base_sha,
            branch=contract.branch,
            commit_sha=commit_sha,
            path=contract.path,
            byte_length=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
            encoding="utf-8",
            media_type="text/markdown",
            changed_path_count=1,
            commit_message=contract.commit_message,
        )

    def test_preflight_and_verify_exact_single_markdown_object(self) -> None:
        contract = self.contract()
        verifier = cpd.GitArtifactVerifier()
        verifier.preflight(contract)
        commit, content = self.publish(contract)
        verified = verifier.verify(contract, manifest=self.manifest(contract, commit_sha=commit, content=content))
        self.assertEqual(verified.commit_sha, commit)
        self.assertEqual(verified.file_mode, "100644")
        self.assertEqual(verified.content, content)
        self.assertEqual(verified.content_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(verified.base_state, "base_unchanged")
        replayed = verifier.fetch_verified_blob(
            contract,
            commit_sha=verified.commit_sha,
            path=contract.path,
            blob_sha=verified.blob_sha,
            byte_length=verified.byte_length,
            content_sha256=verified.content_sha256,
        )
        self.assertEqual(replayed, content)

    def test_preflight_binds_all_protected_refs_and_refuses_existing_branch(self) -> None:
        contract = self.contract()
        verifier = cpd.GitArtifactVerifier()
        self.publish(contract)
        with self.assertRaises(cpd.ArtifactVerificationError) as raised:
            verifier.preflight(contract)
        self.assertEqual(raised.exception.error_code, "artifact_branch_exists")

    def test_preflight_rejects_a_changed_nonbase_protected_ref(self) -> None:
        self.git("switch", "-c", "protected", self.base_sha, cwd=self.work)
        self.git("push", "origin", "protected", cwd=self.work)
        protected_sha = self.git("rev-parse", "HEAD", cwd=self.work).strip()
        contract = replace(
            self.contract(branch="codex/result-protected-7319"),
            protected_refs=tuple(
                sorted(
                    (
                        ProtectedRef(
                            ref="refs/heads/main",
                            sha=self.base_sha,
                            allow_fast_forward=True,
                        ),
                        ProtectedRef(
                            ref="refs/heads/protected",
                            sha=protected_sha,
                            allow_fast_forward=False,
                        ),
                    ),
                    key=lambda item: item.ref,
                )
            ),
        )
        self.git("switch", "protected", cwd=self.work)
        (self.work / "protected.txt").write_text("changed\n", encoding="utf-8")
        self.git("add", "protected.txt", cwd=self.work)
        self.git("commit", "-m", "test: move protected ref", cwd=self.work)
        self.git("push", "origin", "protected", cwd=self.work)
        with self.assertRaises(cpd.ArtifactVerificationError) as raised:
            cpd.GitArtifactVerifier().preflight(contract)
        self.assertEqual(raised.exception.error_code, "protected_ref_changed")

    def test_verify_rejects_extra_paths_and_executable_modes(self) -> None:
        extra = self.contract(branch="codex/result-extra-7319")
        extra_commit, extra_content = self.publish(extra, extra_path="docs/extra.md")
        with self.assertRaises(cpd.ArtifactVerificationError) as raised:
            cpd.GitArtifactVerifier().verify(
                extra,
                manifest=self.manifest(extra, commit_sha=extra_commit, content=extra_content),
            )
        self.assertEqual(raised.exception.error_code, "artifact_extra_paths")

        # Return to main before making a sibling branch; no history is rewritten.
        self.git("switch", "main", cwd=self.work)
        executable = self.contract(branch="codex/result-exec-7319")
        executable_commit, executable_content = self.publish(executable, executable=True)
        with self.assertRaises(cpd.ArtifactVerificationError) as raised:
            cpd.GitArtifactVerifier().verify(
                executable,
                manifest=self.manifest(
                    executable, commit_sha=executable_commit, content=executable_content
                ),
            )
        self.assertEqual(raised.exception.error_code, "artifact_file_mode_invalid")

    def test_verify_rejects_noncanonical_markdown_bytes(self) -> None:
        invalid_contents = {
            "bom": b"\xef\xbb\xbf# title\n",
            "cr": b"# title\r\n",
            "nul": b"# title\x00\n",
            "no-final-lf": b"# title",
        }
        for suffix, content in invalid_contents.items():
            with self.subTest(suffix=suffix):
                self.git("switch", "main", cwd=self.work)
                contract = self.contract(branch=f"codex/result-{suffix}-7319")
                commit, published = self.publish(contract, content=content)
                with self.assertRaises(cpd.ArtifactVerificationError) as raised:
                    cpd.GitArtifactVerifier().verify(
                        contract,
                        manifest=self.manifest(
                            contract, commit_sha=commit, content=published
                        ),
                    )
                self.assertEqual(raised.exception.error_code, "artifact_content_invalid")

    def test_moving_base_fast_forward_is_allowed_but_merge_is_rejected(self) -> None:
        contract = self.contract(branch="codex/result-moving-7319")
        commit, content = self.publish(contract)
        self.git("switch", "main", cwd=self.work)
        (self.work / "README.md").write_text("base advanced\n", encoding="utf-8")
        self.git("add", "README.md", cwd=self.work)
        self.git("commit", "-m", "test: advance base", cwd=self.work)
        self.git("push", "origin", "main", cwd=self.work)
        verified = cpd.GitArtifactVerifier().verify(
            contract, manifest=self.manifest(contract, commit_sha=commit, content=content)
        )
        self.assertEqual(verified.base_state, "base_advanced")

        # A normal merge is intentionally forbidden evidence; no force operation
        # is used in this test.
        self.git("merge", "--no-ff", contract.branch, "-m", "test: merge artifact", cwd=self.work)
        self.git("push", "origin", "main", cwd=self.work)
        with self.assertRaises(cpd.ArtifactVerificationError) as raised:
            cpd.GitArtifactVerifier().verify(
                contract, manifest=self.manifest(contract, commit_sha=commit, content=content)
            )
        self.assertEqual(raised.exception.error_code, "artifact_merged")

    def test_contract_parser_rejects_nonportable_branch_and_manifest_is_strict(self) -> None:
        mapping = {
            "schema": cpd.ARTIFACT_CONTRACT_SCHEMA,
            "repository_id": 7319,
            "repository": "owner/repository",
            "visibility": "private",
            "remote_url": "https://github.com/owner/repository.git",
            "base_branch": "main",
            "base_sha": "a" * 40,
            "branch": ".hidden",
            "path": "docs/result.md",
            "commit_message": "docs: add dispatched result",
            "encoding": "utf-8",
            "media_type": "text/markdown",
            "allowed_change": "add-single-markdown",
            "artifact_max_bytes": 100,
            "sensitivity": "internal",
            "protected_refs": [
                {"ref": "refs/heads/main", "sha": "a" * 40, "allow_fast_forward": True}
            ],
            "prepared_at": "2030-01-01T00:00:00.000Z",
        }
        with self.assertRaises(cpd.ArtifactProtocolError):
            cpd.ArtifactContract.from_mapping(mapping)

        contract = self.contract(branch="codex/result-manifest-7319")
        response = "\n".join(
            (
                "[CODEX_PRO_DISPATCH_RESULT assignment_id=dispatch-artifact-7319]",
                "[CODEX_PRO_DISPATCH_ARTIFACT_V1]",
                "schema=codex-pro-dispatch.artifact-manifest/v1",
                "assignment_id=dispatch-artifact-7319",
                "repository_id=7319",
                "repository=owner/repository",
                f"remote_url={self.remote}",
                "base_branch=main",
                f"base_sha={self.base_sha}",
                "branch=codex/result-manifest-7319",
                f"commit_sha={'b' * 40}",
                "path=docs/result.md",
                "byte_length=1",
                f"content_sha256={'c' * 64}",
                "encoding=utf-8",
                "media_type=text/markdown",
                "changed_path_count=1",
                "commit_message=docs: add dispatched result",
                "[CODEX_PRO_DISPATCH_ARTIFACT_END_V1]",
            )
        )
        parsed = parse_artifact_manifest(
            response, assignment_id="dispatch-artifact-7319", contract=contract
        )
        self.assertEqual(parsed.branch, contract.branch)
        with self.assertRaises(cpd.ArtifactProtocolError):
            parse_artifact_manifest(
                response + "\nextra", assignment_id="dispatch-artifact-7319", contract=contract
            )


class _StaticArtifactVerifier:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.verify_calls = 0

    def preflight(self, contract: cpd.ArtifactContract) -> None:
        del contract

    def verify(
        self, contract: cpd.ArtifactContract, *, manifest: ArtifactManifest | None = None
    ) -> ArtifactVerificationResult:
        self.verify_calls += 1
        assert manifest is not None
        digest = hashlib.sha256(self.content).hexdigest()
        return ArtifactVerificationResult(
            branch=contract.branch,
            commit_sha=manifest.commit_sha,
            tree_sha="1" * 40,
            blob_sha="2" * 40,
            file_mode="100644",
            byte_length=len(self.content),
            content_sha256=digest,
            base_state="base_unchanged",
            branch_head_before=manifest.commit_sha,
            branch_head_after=manifest.commit_sha,
            content=self.content,
        )

    def fetch_verified_blob(self, contract: cpd.ArtifactContract, **identity: object) -> bytes:
        del contract, identity
        return self.content


class ArtifactTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = cpd.RuntimePaths(root / "config", root / "state")
        cpd.save_worker("worker-artifact-7319", confirm_pro=True, paths=self.paths)
        self.content = b"# independently verified artifact\n"
        self.verifier = _StaticArtifactVerifier(self.content)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mapping(self) -> dict[str, object]:
        return {
            "schema": cpd.ARTIFACT_CONTRACT_SCHEMA,
            "repository_id": 7319,
            "repository": "owner/repository",
            "visibility": "private",
            "remote_url": "https://github.com/owner/repository.git",
            "base_branch": "main",
            "base_sha": "a" * 40,
            "branch": "codex/dispatch-artifact-7319",
            "path": "docs/result.md",
            "commit_message": "docs: add dispatched result",
            "encoding": "utf-8",
            "media_type": "text/markdown",
            "allowed_change": "add-single-markdown",
            "artifact_max_bytes": 1024,
            "sensitivity": "internal",
            "protected_refs": [
                {"ref": "refs/heads/main", "sha": "a" * 40, "allow_fast_forward": True}
            ],
            "prepared_at": "2030-01-01T00:00:00.000Z",
        }

    def evidence(self, prepared: cpd.PreparedAssignment, text: str) -> cpd.NativeCollectionEvidence:
        return cpd.NativeCollectionEvidence.from_mapping(
            {
                "schema": cpd.NATIVE_COLLECTION_SCHEMA,
                "adapter_contract_id": "codex-desktop-native-collection/v1",
                "requested_conversation_id": "worker-artifact-7319",
                "loaded_conversation_id": "worker-artifact-7319",
                "assistant_message_id": "native-assistant-artifact-7319",
                "submitted_user_message_id": "native-user-artifact-7319",
                "role": "assistant",
                "generation_status": "completed",
                "generation_finality_provenance": "native-message-status",
                "truncated": False,
                "selected_result_outer_integrity": {
                    "truncated": False,
                    "provenance": "native-result-envelope",
                },
                "text": text,
                "observed_at": "2030-01-01T00:00:00.000Z",
            }
        )

    def manifest_text(self, assignment_id: str) -> str:
        return "\n".join(
            (
                f"[CODEX_PRO_DISPATCH_RESULT assignment_id={assignment_id}]",
                "[CODEX_PRO_DISPATCH_ARTIFACT_V1]",
                "schema=codex-pro-dispatch.artifact-manifest/v1",
                f"assignment_id={assignment_id}",
                "repository_id=7319",
                "repository=owner/repository",
                "remote_url=https://github.com/owner/repository.git",
                "base_branch=main",
                f"base_sha={'a' * 40}",
                "branch=codex/dispatch-artifact-7319",
                f"commit_sha={'b' * 40}",
                "path=docs/result.md",
                f"byte_length={len(self.content)}",
                f"content_sha256={hashlib.sha256(self.content).hexdigest()}",
                "encoding=utf-8",
                "media_type=text/markdown",
                "changed_path_count=1",
                "commit_message=docs: add dispatched result",
                "[CODEX_PRO_DISPATCH_ARTIFACT_END_V1]",
            )
        )

    def prepare_artifact(self, *, assignment_id: str = "dispatch-artifact-7319") -> cpd.PreparedAssignment:
        return cpd.prepare_assignment(
            "Create the approved Markdown.",
            parent_task_id="parent-artifact-7319",
            assignment_id=assignment_id,
            result_mode="artifact",
            artifact_contract=self.mapping(),
            authorize_artifact_write=True,
            worker_github_write_confirmed=True,
            artifact_verifier=self.verifier,
            paths=self.paths,
        )

    def test_artifact_transport_is_explicit_body_free_and_replay_uses_exact_blob(self) -> None:
        prepared = self.prepare_artifact()
        cpd.arm_assignment(prepared.assignment_id, self.paths, turn_id=prepared.turn_id)
        cpd.mark_submitted(
            prepared.assignment_id,
            prepared.wrapped_prompt,
            self.paths,
            turn_id=prepared.turn_id,
            native_user_message_id="native-user-artifact-7319",
        )
        collected = cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(prepared, self.manifest_text(prepared.assignment_id)),
            paths=self.paths,
        )
        self.assertEqual(collected.status, "verifying")
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertNotIn("independently verified artifact", json.dumps(receipt))
        output = Path(self.temporary.name) / "artifact-result.md"
        verified = cpd.verify_artifact(
            prepared.assignment_id, output, verifier=self.verifier, paths=self.paths
        )
        self.assertEqual(verified.content, self.content)
        self.assertEqual(output.read_bytes(), self.content)
        receipt = cpd.load_assignment(prepared.assignment_id, self.paths)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["result"]["completion_basis"], "artifact-manifest")
        self.assertNotIn("independently verified artifact", json.dumps(receipt))
        replay = cpd.verify_artifact(
            prepared.assignment_id, output, verifier=self.verifier, paths=self.paths
        )
        self.assertEqual(replay.content, self.content)
        self.assertEqual(self.verifier.verify_calls, 1)

    def test_artifact_mode_requires_each_explicit_authorization_and_public_acknowledgement(self) -> None:
        for kwargs in (
            {},
            {"authorize_artifact_write": True},
            {"worker_github_write_confirmed": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(cpd.ArtifactProtocolError) as raised:
                    cpd.prepare_assignment(
                        "Create Markdown.",
                        parent_task_id="parent-artifact-7319",
                        assignment_id="dispatch-authorization-7319",
                        result_mode="artifact",
                        artifact_contract=self.mapping(),
                        artifact_verifier=self.verifier,
                        paths=self.paths,
                        **kwargs,
                    )
                self.assertEqual(raised.exception.error_code, "artifact_authorization_missing")
        public = self.mapping()
        public["visibility"] = "public"
        public["sensitivity"] = "public"
        with self.assertRaises(cpd.ArtifactProtocolError) as raised:
            cpd.prepare_assignment(
                "Create Markdown.",
                parent_task_id="parent-artifact-7319",
                assignment_id="dispatch-public-artifact-7319",
                result_mode="artifact",
                artifact_contract=public,
                authorize_artifact_write=True,
                worker_github_write_confirmed=True,
                artifact_verifier=self.verifier,
                paths=self.paths,
            )
        self.assertEqual(
            raised.exception.error_code, "artifact_public_retention_unacknowledged"
        )

    def test_tampered_stored_manifest_and_nonartifact_discovery_fail_closed(self) -> None:
        prepared = self.prepare_artifact()
        cpd.arm_assignment(prepared.assignment_id, self.paths, turn_id=prepared.turn_id)
        cpd.mark_submitted(
            prepared.assignment_id,
            prepared.wrapped_prompt,
            self.paths,
            turn_id=prepared.turn_id,
            native_user_message_id="native-user-artifact-7319",
        )
        cpd.collect_turn(
            prepared.assignment_id,
            prepared.turn_id or "",
            self.evidence(prepared, self.manifest_text(prepared.assignment_id)),
            paths=self.paths,
        )
        receipt_path = self.paths.assignments_dir / f"{prepared.assignment_id}.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["artifact_manifest"]["commit_sha"] = "not-a-git-object"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(cpd.ReceiptMigrationError):
            cpd.verify_artifact(
                prepared.assignment_id,
                Path(self.temporary.name) / "tampered.md",
                verifier=self.verifier,
                paths=self.paths,
            )

        # Discovery is the one pre-authorized exception to readable chat
        # evidence, and it cannot be invoked on inline/chunked assignments.
        other_root = Path(self.temporary.name) / "other"
        other_paths = cpd.RuntimePaths(other_root / "config", other_root / "state")
        cpd.save_worker("worker-other-7319", confirm_pro=True, paths=other_paths)
        inline = cpd.prepare_assignment(
            "inline only",
            parent_task_id="parent-other-7319",
            assignment_id="dispatch-inline-no-discovery-7319",
            paths=other_paths,
        )
        with self.assertRaises(cpd.ArtifactProtocolError) as raised:
            cpd.verify_artifact(
                inline.assignment_id,
                other_root / "result.md",
                discover=True,
                verifier=self.verifier,
                paths=other_paths,
            )
        self.assertEqual(raised.exception.error_code, "artifact_authorization_missing")


if __name__ == "__main__":
    unittest.main()
