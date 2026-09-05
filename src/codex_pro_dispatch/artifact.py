"""Strict single-file Git artifact contract and bare-object verifier."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .collection import canonical_json_bytes, strict_json_object
from .errors import ArtifactProtocolError, ArtifactVerificationError


ARTIFACT_CONTRACT_SCHEMA = "codex-pro-dispatch.artifact-contract/v1"
ARTIFACT_MANIFEST_SCHEMA = "codex-pro-dispatch.artifact-manifest/v1"
ARTIFACT_MAX_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,38})/[a-z0-9](?:[a-z0-9._-]{0,99})$")
_DECIMAL = re.compile(r"^[1-9][0-9]*$")


def _protocol_error(code: str, message: str, **details: Any) -> None:
    raise ArtifactProtocolError(message, details=details, error_code=code)


def _verification_error(code: str, message: str, **details: Any) -> None:
    raise ArtifactVerificationError(message, details=details, error_code=code)


def _safe_one_line(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        _protocol_error("artifact_contract_invalid", f"{field} must be a nonempty bounded string")
    if "\n" in value or "\r" in value or any(ord(character) < 32 for character in value):
        _protocol_error("artifact_contract_invalid", f"{field} must be one printable line")
    return value


def _validate_branch(value: object, *, base_branch: str | None = None) -> str:
    branch = _safe_one_line(value, field="branch", limit=240)
    if (
        branch.startswith("refs/")
        or branch.startswith("-")
        or branch == "@"
        or branch.endswith("/")
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or any(character in " ~^:?*[\\" for character in branch)
        or any(
            piece in {"", ".", "..", ".git"}
            or piece.startswith(".")
            or piece.endswith(".")
            or piece.endswith(".lock")
            for piece in branch.split("/")
        )
    ):
        _protocol_error("artifact_contract_invalid", "branch is not a portable Git branch")
    if base_branch and branch == base_branch:
        _protocol_error("artifact_contract_invalid", "artifact branch must not be the base branch")
    return branch


def _validate_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240:
        _protocol_error("artifact_contract_invalid", "artifact path is invalid")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        _protocol_error("artifact_contract_invalid", "artifact path must be portable ASCII")
        raise AssertionError("unreachable") from exc
    if (
        value.startswith("/")
        or "\\" in value
        or not value.endswith(".md")
        or any(piece in {"", ".", "..", ".git"} for piece in value.split("/"))
        or any(ord(character) < 32 for character in value)
    ):
        _protocol_error("artifact_contract_invalid", "artifact path is not a safe Markdown path")
    return value


def _validate_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        _protocol_error("artifact_contract_invalid", f"{field} must be lowercase 40-hex")
    return value


@dataclass(frozen=True)
class ProtectedRef:
    ref: str
    sha: str
    allow_fast_forward: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "sha": self.sha,
            "allow_fast_forward": self.allow_fast_forward,
        }


@dataclass(frozen=True)
class ArtifactContract:
    repository_id: int
    repository: str
    visibility: str
    remote_url: str
    base_branch: str
    base_sha: str
    branch: str
    path: str
    commit_message: str
    encoding: str
    media_type: str
    allowed_change: str
    artifact_max_bytes: int
    sensitivity: str
    protected_refs: tuple[ProtectedRef, ...]
    prepared_at: str

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> "ArtifactContract":
        try:
            value = strict_json_object(raw)
        except Exception as exc:
            _protocol_error("artifact_contract_invalid", "Artifact contract is not strict JSON")
            raise AssertionError("unreachable") from exc
        return cls.from_mapping(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ArtifactContract":
        keys = {
            "schema",
            "repository_id",
            "repository",
            "visibility",
            "remote_url",
            "base_branch",
            "base_sha",
            "branch",
            "path",
            "commit_message",
            "encoding",
            "media_type",
            "allowed_change",
            "artifact_max_bytes",
            "sensitivity",
            "protected_refs",
            "prepared_at",
        }
        if set(value) != keys:
            _protocol_error(
                "artifact_contract_invalid",
                "Artifact contract has an invalid key set",
                missing_keys=sorted(keys - set(value)),
                unknown_keys=sorted(set(value) - keys),
            )
        if value.get("schema") != ARTIFACT_CONTRACT_SCHEMA:
            _protocol_error("artifact_contract_invalid", "Artifact contract schema is unsupported")
        repository_id = value.get("repository_id")
        if type(repository_id) is not int or repository_id <= 0:
            _protocol_error("artifact_contract_invalid", "repository_id must be a positive integer")
        repository = value.get("repository")
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            _protocol_error("artifact_contract_invalid", "repository must be canonical lowercase owner/repository")
        visibility = value.get("visibility")
        if visibility not in {"private", "public"}:
            _protocol_error("artifact_contract_invalid", "visibility must be private or public")
        remote_url = value.get("remote_url")
        expected_remote = f"https://github.com/{repository}.git"
        if remote_url != expected_remote:
            _protocol_error("artifact_contract_invalid", "remote_url must be canonical credential-free GitHub HTTPS")
        base_branch = _validate_branch(value.get("base_branch"))
        base_sha = _validate_sha(value.get("base_sha"), field="base_sha")
        branch = _validate_branch(value.get("branch"), base_branch=base_branch)
        path = _validate_path(value.get("path"))
        commit_message = _safe_one_line(value.get("commit_message"), field="commit_message", limit=120)
        if value.get("encoding") != "utf-8" or value.get("media_type") != "text/markdown":
            _protocol_error("artifact_contract_invalid", "artifact must be UTF-8 text/markdown")
        if value.get("allowed_change") != "add-single-markdown":
            _protocol_error("artifact_contract_invalid", "allowed_change must be add-single-markdown")
        maximum = value.get("artifact_max_bytes")
        if type(maximum) is not int or maximum < 1 or maximum > ARTIFACT_MAX_BYTES:
            _protocol_error("artifact_contract_invalid", "artifact_max_bytes is outside the project limit")
        sensitivity = value.get("sensitivity")
        if sensitivity not in {"internal", "public", "secret", "personal", "regulated"}:
            _protocol_error("artifact_contract_invalid", "sensitivity is invalid")
        if visibility == "public" and sensitivity in {"secret", "personal", "regulated"}:
            _protocol_error("artifact_contract_invalid", "sensitive artifacts cannot use a public repository")
        refs_value = value.get("protected_refs")
        if not isinstance(refs_value, list) or not refs_value:
            _protocol_error("artifact_contract_invalid", "protected_refs must be a nonempty list")
        refs: list[ProtectedRef] = []
        for item in refs_value:
            if not isinstance(item, Mapping) or set(item) != {"ref", "sha", "allow_fast_forward"}:
                _protocol_error("artifact_contract_invalid", "protected_refs item is invalid")
            ref = item.get("ref")
            if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
                _protocol_error("artifact_contract_invalid", "protected ref is invalid")
            _validate_branch(ref.removeprefix("refs/heads/"))
            sha = _validate_sha(item.get("sha"), field="protected_ref.sha")
            allow = item.get("allow_fast_forward")
            if type(allow) is not bool:
                _protocol_error("artifact_contract_invalid", "protected ref allow_fast_forward must be boolean")
            refs.append(ProtectedRef(ref=ref, sha=sha, allow_fast_forward=allow))
        if [ref.ref for ref in refs] != sorted(ref.ref for ref in refs) or len({ref.ref for ref in refs}) != len(refs):
            _protocol_error("artifact_contract_invalid", "protected_refs must be sorted and unique")
        base_ref = f"refs/heads/{base_branch}"
        if not any(ref.ref == base_ref and ref.sha == base_sha for ref in refs):
            _protocol_error("artifact_contract_invalid", "protected_refs must include the prepared base branch")
        prepared_at = _safe_one_line(value.get("prepared_at"), field="prepared_at", limit=64)
        return cls(
            repository_id=repository_id,
            repository=repository,
            visibility=visibility,
            remote_url=remote_url,
            base_branch=base_branch,
            base_sha=base_sha,
            branch=branch,
            path=path,
            commit_message=commit_message,
            encoding="utf-8",
            media_type="text/markdown",
            allowed_change="add-single-markdown",
            artifact_max_bytes=maximum,
            sensitivity=sensitivity,
            protected_refs=tuple(refs),
            prepared_at=prepared_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ARTIFACT_CONTRACT_SCHEMA,
            "repository_id": self.repository_id,
            "repository": self.repository,
            "visibility": self.visibility,
            "remote_url": self.remote_url,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "path": self.path,
            "commit_message": self.commit_message,
            "encoding": self.encoding,
            "media_type": self.media_type,
            "allowed_change": self.allowed_change,
            "artifact_max_bytes": self.artifact_max_bytes,
            "sensitivity": self.sensitivity,
            "protected_refs": [item.to_dict() for item in self.protected_refs],
            "prepared_at": self.prepared_at,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class ArtifactManifest:
    assignment_id: str
    repository_id: int
    repository: str
    remote_url: str
    base_branch: str
    base_sha: str
    branch: str
    commit_sha: str
    path: str
    byte_length: int
    content_sha256: str
    encoding: str
    media_type: str
    changed_path_count: int
    commit_message: str


def validate_artifact_manifest(
    manifest: ArtifactManifest,
    *,
    assignment_id: str,
    contract: ArtifactContract,
) -> ArtifactManifest:
    """Revalidate a body-free manifest before it can locate a Git object.

    Native collection validates the original wire manifest, but receipts are
    untrusted durable input too.  This second validation is deliberately kept
    next to the protocol parser so a tampered receipt cannot turn an arbitrary
    string into an argument for ``git fetch`` or object lookup.
    """

    if not isinstance(manifest, ArtifactManifest):
        _protocol_error("artifact_manifest_invalid", "Artifact manifest has an invalid type")
    if (
        manifest.assignment_id != assignment_id
        or manifest.repository_id != contract.repository_id
        or manifest.repository != contract.repository
        or manifest.remote_url != contract.remote_url
        or manifest.base_branch != contract.base_branch
        or manifest.base_sha != contract.base_sha
        or manifest.branch != contract.branch
        or manifest.path != contract.path
        or manifest.encoding != "utf-8"
        or manifest.media_type != "text/markdown"
        or manifest.changed_path_count != 1
        or manifest.commit_message != contract.commit_message
    ):
        _protocol_error("artifact_manifest_invalid", "Artifact manifest differs from its contract")
    if not _SHA.fullmatch(manifest.commit_sha) or not _SHA256.fullmatch(
        manifest.content_sha256
    ):
        _protocol_error("artifact_manifest_invalid", "Artifact manifest hash is noncanonical")
    if (
        type(manifest.byte_length) is not int
        or manifest.byte_length < 1
        or manifest.byte_length > contract.artifact_max_bytes
    ):
        _protocol_error("artifact_manifest_invalid", "Artifact manifest byte length is invalid")
    return manifest


def parse_artifact_manifest(
    response: str, *, assignment_id: str, contract: ArtifactContract
) -> ArtifactManifest:
    marker = f"[CODEX_PRO_DISPATCH_RESULT assignment_id={assignment_id}]"
    lines = response.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    expected_keys = (
        "schema",
        "assignment_id",
        "repository_id",
        "repository",
        "remote_url",
        "base_branch",
        "base_sha",
        "branch",
        "commit_sha",
        "path",
        "byte_length",
        "content_sha256",
        "encoding",
        "media_type",
        "changed_path_count",
        "commit_message",
    )
    if len(response.encode("utf-8")) > 4096 or len(lines) != len(expected_keys) + 3:
        _protocol_error("artifact_manifest_invalid", "Artifact manifest has invalid length")
    if lines[0] != marker or lines[1] != "[CODEX_PRO_DISPATCH_ARTIFACT_V1]" or lines[-1] != "[CODEX_PRO_DISPATCH_ARTIFACT_END_V1]":
        _protocol_error("artifact_manifest_invalid", "Artifact manifest framing is invalid")
    fields: dict[str, str] = {}
    for expected, line in zip(expected_keys, lines[2:-1]):
        if "=" not in line:
            _protocol_error("artifact_manifest_invalid", "Artifact manifest field is invalid")
        key, item = line.split("=", 1)
        if key != expected or key in fields or not item or any(ord(char) < 32 for char in item):
            _protocol_error("artifact_manifest_invalid", "Artifact manifest fields are not canonical")
        fields[key] = item
    if fields["schema"] != ARTIFACT_MANIFEST_SCHEMA or fields["assignment_id"] != assignment_id:
        _protocol_error("artifact_manifest_invalid", "Artifact manifest schema or assignment differs")
    for field, expected in (
        ("repository", contract.repository),
        ("remote_url", contract.remote_url),
        ("base_branch", contract.base_branch),
        ("base_sha", contract.base_sha),
        ("branch", contract.branch),
        ("path", contract.path),
        ("encoding", "utf-8"),
        ("media_type", "text/markdown"),
        ("commit_message", contract.commit_message),
    ):
        if fields[field] != expected:
            _protocol_error("artifact_manifest_invalid", f"Artifact manifest {field} differs from contract")
    if not _DECIMAL.fullmatch(fields["repository_id"]) or int(fields["repository_id"]) != contract.repository_id:
        _protocol_error("artifact_manifest_invalid", "Artifact manifest repository ID differs")
    if not _SHA.fullmatch(fields["commit_sha"]) or not _SHA256.fullmatch(fields["content_sha256"]):
        _protocol_error("artifact_manifest_invalid", "Artifact manifest hash is noncanonical")
    if not _DECIMAL.fullmatch(fields["byte_length"]) or int(fields["byte_length"]) > contract.artifact_max_bytes:
        _protocol_error("artifact_manifest_invalid", "Artifact manifest byte length is invalid")
    if fields["changed_path_count"] != "1":
        _protocol_error("artifact_manifest_invalid", "Artifact manifest must report one changed path")
    return validate_artifact_manifest(
        ArtifactManifest(
        assignment_id=assignment_id,
        repository_id=contract.repository_id,
        repository=contract.repository,
        remote_url=contract.remote_url,
        base_branch=contract.base_branch,
        base_sha=contract.base_sha,
        branch=contract.branch,
        commit_sha=fields["commit_sha"],
        path=contract.path,
        byte_length=int(fields["byte_length"]),
        content_sha256=fields["content_sha256"],
        encoding="utf-8",
        media_type="text/markdown",
        changed_path_count=1,
        commit_message=contract.commit_message,
        ),
        assignment_id=assignment_id,
        contract=contract,
    )


@dataclass(frozen=True)
class ArtifactVerificationResult:
    branch: str
    commit_sha: str
    tree_sha: str
    blob_sha: str
    file_mode: str
    byte_length: int
    content_sha256: str
    base_state: str
    branch_head_before: str
    branch_head_after: str
    content: bytes

    def receipt_fields(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "blob_sha": self.blob_sha,
            "file_mode": self.file_mode,
            "byte_length": self.byte_length,
            "content_sha256": self.content_sha256,
            "base_state": self.base_state,
            "branch_head_before": self.branch_head_before,
            "branch_head_after": self.branch_head_after,
            "verifier_version": "git-artifact-verifier/v1",
        }


class GitArtifactVerifier:
    """Verify exact remote Git objects without checking out a worktree."""

    def _run(self, args: Sequence[str], *, cwd: Path | None = None, allowed: set[int] | None = None) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                list(args), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False
            )
        except OSError as exc:
            _verification_error("artifact_git_unavailable", "Git executable is unavailable", errno=getattr(exc, "errno", None))
            raise AssertionError("unreachable") from exc
        accepted = allowed if allowed is not None else {0}
        if result.returncode not in accepted:
            _verification_error(
                "artifact_git_failed",
                "Git object verification failed",
                command=args[1] if len(args) > 1 else args[0],
                stderr_sha256=hashlib.sha256(result.stderr).hexdigest(),
            )
        return result

    def _git(self, repository: Path, *args: str, allowed: set[int] | None = None) -> subprocess.CompletedProcess[bytes]:
        return self._run(("git", "-C", str(repository), *args), allowed=allowed)

    def _ref_head(self, remote_url: str, ref: str) -> str | None:
        result = self._run(("git", "ls-remote", "--heads", remote_url, ref))
        if not result.stdout:
            return None
        lines = result.stdout.splitlines()
        if len(lines) != 1:
            _verification_error("artifact_branch_ambiguous", "Remote reference is ambiguous")
        pieces = lines[0].split(b"\t", 1)
        if len(pieces) != 2:
            _verification_error("artifact_branch_ambiguous", "Remote reference output is invalid")
        try:
            sha = pieces[0].decode("ascii")
            actual_ref = pieces[1].decode("utf-8")
        except UnicodeDecodeError:
            _verification_error("artifact_branch_ambiguous", "Remote reference output is invalid")
        if not _SHA.fullmatch(sha) or actual_ref != ref:
            _verification_error("artifact_branch_ambiguous", "Remote reference output is invalid")
        return sha

    def _fetch(self, bare: Path, remote_url: str, sha: str) -> None:
        self._git(bare, "fetch", "--no-tags", remote_url, sha)

    def preflight(self, contract: ArtifactContract) -> None:
        """Confirm base/ref/path preconditions before a worker receives write authority."""
        base_ref = f"refs/heads/{contract.base_branch}"
        if self._ref_head(contract.remote_url, base_ref) != contract.base_sha:
            _verification_error("artifact_base_mismatch", "Remote base branch does not equal prepared base")
        # A prepared contract binds every protected reference, not merely the
        # base branch.  Reading these before write authority is issued closes a
        # race where an unrelated protected ref has already moved but the worker
        # would otherwise receive a still-looking-valid artifact instruction.
        for protected in contract.protected_refs:
            if protected.ref == base_ref:
                continue
            if self._ref_head(contract.remote_url, protected.ref) != protected.sha:
                _verification_error("protected_ref_changed", "Protected ref changed before preparation")
        if self._ref_head(contract.remote_url, f"refs/heads/{contract.branch}") is not None:
            _verification_error("artifact_branch_exists", "Artifact branch already exists")
        with tempfile.TemporaryDirectory(prefix="codex-pro-dispatch-artifact-") as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            bare = root / "objects.git"
            self._run(("git", "init", "--bare", str(bare)))
            os.chmod(bare, 0o700)
            self._fetch(bare, contract.remote_url, contract.base_sha)
            exists = self._git(
                bare,
                "cat-file",
                "-e",
                f"{contract.base_sha}:{contract.path}",
                # Git uses 128 for a missing pathspec in a valid object; it is
                # evidence of absence here, not a transport failure.
                allowed={0, 1, 128},
            )
            if exists.returncode == 0:
                _verification_error("artifact_path_exists", "Artifact path already exists at prepared base")

    def verify(
        self, contract: ArtifactContract, *, manifest: ArtifactManifest | None = None
    ) -> ArtifactVerificationResult:
        branch_ref = f"refs/heads/{contract.branch}"
        branch_before = self._ref_head(contract.remote_url, branch_ref)
        if branch_before is None:
            _verification_error("artifact_branch_missing", "Artifact branch is absent")
        commit_sha = manifest.commit_sha if manifest is not None else branch_before
        if branch_before != commit_sha:
            _verification_error("artifact_branch_moved", "Artifact branch does not equal reported commit")
        base_ref = f"refs/heads/{contract.base_branch}"
        current_base = self._ref_head(contract.remote_url, base_ref)
        if current_base is None:
            _verification_error("artifact_base_missing", "Base branch is absent")
        with tempfile.TemporaryDirectory(prefix="codex-pro-dispatch-artifact-") as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            bare = root / "objects.git"
            self._run(("git", "init", "--bare", str(bare)))
            os.chmod(bare, 0o700)
            protected_heads: set[str] = set()
            for protected in contract.protected_refs:
                observed = self._ref_head(contract.remote_url, protected.ref)
                if observed is None:
                    _verification_error("protected_ref_changed", "Protected ref is absent")
                protected_heads.add(observed)
            for sha in {contract.base_sha, commit_sha, current_base, *protected_heads}:
                self._fetch(bare, contract.remote_url, sha)
            commit = self._git(bare, "cat-file", "-p", commit_sha).stdout
            header, separator, message = commit.partition(b"\n\n")
            if not separator:
                _verification_error("artifact_commit_invalid", "Artifact commit has no header boundary")
            parents = [line.split(b" ", 1)[1].decode("ascii", "ignore") for line in header.splitlines() if line.startswith(b"parent ")]
            if len(parents) != 1 or parents[0] != contract.base_sha:
                _verification_error("artifact_parent_mismatch", "Artifact commit must have exactly the prepared base as parent")
            expected_message = contract.commit_message.encode("utf-8") + b"\n"
            if message != expected_message:
                _verification_error("artifact_commit_message_mismatch", "Artifact commit message differs from contract")
            diff = self._git(
                bare, "diff-tree", "--no-commit-id", "--name-status", "-r", "-z", contract.base_sha, commit_sha
            ).stdout.split(b"\0")
            if len(diff) != 3 or diff[-1] != b"" or diff[0] != b"A" or diff[1] != contract.path.encode("ascii"):
                _verification_error("artifact_extra_paths", "Artifact commit must add exactly one authorized path")
            base_path = self._git(
                bare,
                "cat-file",
                "-e",
                f"{contract.base_sha}:{contract.path}",
                allowed={0, 1, 128},
            )
            if base_path.returncode == 0:
                _verification_error("artifact_path_exists", "Artifact path existed at prepared base")
            tree_line = self._git(bare, "ls-tree", "-z", commit_sha, "--", contract.path).stdout
            if not tree_line.endswith(b"\0"):
                _verification_error("artifact_file_mode_invalid", "Artifact tree entry is invalid")
            entry = tree_line[:-1]
            try:
                prefix, actual_path = entry.split(b"\t", 1)
                mode, object_type, blob_sha_raw = prefix.split(b" ", 2)
            except ValueError:
                _verification_error("artifact_file_mode_invalid", "Artifact tree entry is invalid")
            if (
                mode != b"100644" or object_type != b"blob" or actual_path != contract.path.encode("ascii")
                or not _SHA.fullmatch(blob_sha_raw.decode("ascii", "ignore"))
            ):
                _verification_error("artifact_file_mode_invalid", "Artifact file must be a regular 100644 blob")
            blob_sha = blob_sha_raw.decode("ascii")
            content = self._git(bare, "cat-file", "blob", f"{commit_sha}:{contract.path}").stdout
            if (
                content.startswith(b"\xef\xbb\xbf")
                or b"\x00" in content
                or b"\r" in content
                or not content.endswith(b"\n")
                or len(content) > contract.artifact_max_bytes
            ):
                _verification_error("artifact_content_invalid", "Artifact content violates UTF-8 Markdown constraints")
            try:
                content.decode("utf-8", "strict")
            except UnicodeDecodeError:
                _verification_error("artifact_content_invalid", "Artifact content is not strict UTF-8")
            content_sha256 = hashlib.sha256(content).hexdigest()
            if manifest is not None and (
                manifest.byte_length != len(content) or manifest.content_sha256 != content_sha256
            ):
                _verification_error("artifact_hash_mismatch", "Artifact manifest length or hash differs")
            base_ancestor = self._git(bare, "merge-base", "--is-ancestor", contract.base_sha, current_base, allowed={0, 1}).returncode == 0
            artifact_merged = self._git(bare, "merge-base", "--is-ancestor", commit_sha, current_base, allowed={0, 1}).returncode == 0
            if artifact_merged:
                _verification_error("artifact_merged", "Artifact commit is already reachable from the base branch")
            if not base_ancestor:
                _verification_error("artifact_base_rewritten", "Prepared base is no longer an ancestor of base branch")
            base_state = "base_unchanged" if current_base == contract.base_sha else "base_advanced"
            final_base = self._ref_head(contract.remote_url, base_ref)
            if final_base is None:
                _verification_error("artifact_base_missing", "Base branch is absent")
            if final_base != current_base:
                # Never accept a base movement that occurred after the initial
                # ancestry check.  Fetch the final advertised object and prove
                # the same moving-base rule against it.
                self._fetch(bare, contract.remote_url, final_base)
                if self._git(
                    bare,
                    "merge-base",
                    "--is-ancestor",
                    contract.base_sha,
                    final_base,
                    allowed={0, 1},
                ).returncode != 0:
                    _verification_error("artifact_base_rewritten", "Prepared base is no longer an ancestor of base branch")
                if self._git(
                    bare,
                    "merge-base",
                    "--is-ancestor",
                    commit_sha,
                    final_base,
                    allowed={0, 1},
                ).returncode == 0:
                    _verification_error("artifact_merged", "Artifact commit is already reachable from the base branch")
                current_base = final_base
                base_state = "base_unchanged" if current_base == contract.base_sha else "base_advanced"
            for protected in contract.protected_refs:
                observed = self._ref_head(contract.remote_url, protected.ref)
                if observed is None:
                    _verification_error("protected_ref_changed", "Protected ref is absent")
                if protected.ref == base_ref and protected.allow_fast_forward:
                    # The two ancestry checks above prove only a fast-forward
                    # retaining the prepared base; arbitrary base rewrites and
                    # merges of the artifact remain rejected.
                    continue
                if observed != protected.sha:
                    _verification_error("protected_ref_changed", "Protected ref changed")
            tree_sha = self._git(bare, "rev-parse", f"{commit_sha}^{{tree}}").stdout.strip().decode("ascii")
            branch_after = self._ref_head(contract.remote_url, branch_ref)
            if branch_after != branch_before:
                _verification_error("artifact_branch_moved", "Artifact branch moved during verification")
            return ArtifactVerificationResult(
                branch=contract.branch,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                blob_sha=blob_sha,
                file_mode="100644",
                byte_length=len(content),
                content_sha256=content_sha256,
                base_state=base_state,
                branch_head_before=branch_before,
                branch_head_after=branch_after,
                content=content,
            )


    def fetch_verified_blob(
        self,
        contract: ArtifactContract,
        *,
        commit_sha: str,
        path: str,
        blob_sha: str,
        byte_length: int,
        content_sha256: str,
    ) -> bytes:
        """Read a previously verified immutable blob by exact object identity.

        This deliberately does not trust the branch head.  A completed receipt
        names a content-addressed commit/tree/blob; branch drift is audit data,
        not permission to mutate that result.
        """

        if not _SHA.fullmatch(commit_sha) or not _SHA.fullmatch(blob_sha):
            _verification_error("artifact_hash_mismatch", "Stored Git object ID is invalid")
        if path != contract.path or not _SHA256.fullmatch(content_sha256):
            _verification_error("artifact_hash_mismatch", "Stored artifact identity is invalid")
        with tempfile.TemporaryDirectory(prefix="codex-pro-dispatch-artifact-") as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            bare = root / "objects.git"
            self._run(("git", "init", "--bare", str(bare)))
            os.chmod(bare, 0o700)
            self._fetch(bare, contract.remote_url, commit_sha)
            tree_line = self._git(bare, "ls-tree", "-z", commit_sha, "--", path).stdout
            if not tree_line.endswith(b"\0"):
                _verification_error("artifact_file_mode_invalid", "Stored artifact tree entry is invalid")
            try:
                prefix, actual_path = tree_line[:-1].split(b"\t", 1)
                mode, object_type, observed_blob = prefix.split(b" ", 2)
            except ValueError:
                _verification_error("artifact_file_mode_invalid", "Stored artifact tree entry is invalid")
            if (
                mode != b"100644"
                or object_type != b"blob"
                or actual_path != path.encode("ascii")
                or observed_blob.decode("ascii", "ignore") != blob_sha
            ):
                _verification_error("artifact_hash_mismatch", "Stored artifact blob no longer matches receipt")
            content = self._git(bare, "cat-file", "blob", f"{commit_sha}:{path}").stdout
            if len(content) != byte_length or hashlib.sha256(content).hexdigest() != content_sha256:
                _verification_error("artifact_hash_mismatch", "Stored artifact content no longer matches receipt")
            return content
