# GitHub artifact verification contract

Read this reference before an `artifact`-mode assignment. Ordinary prompt-only
review and `chunked` mode are read-only and do not use it.

## Required authority before preparation

Artifact mode is explicit per assignment. Before `prepare`, the parent must have
all of the following:

- a dedicated Pro worker whose separately installed GitHub connector exposes the
  exact write action for the exact remote repository;
- an independently observed repository ID, canonical credential-free GitHub HTTPS
  URL, visibility, prepared base branch/SHA, and every protected ref/SHA;
- a currently absent disposable artifact branch and currently absent exact
  Markdown path at the prepared base;
- one strict artifact contract with a single `add-single-markdown` change; and
- independent parent remote read access for exact Git object verification.

This plugin does not install, authenticate, or broaden the GitHub connector.
Connector presence does not prove write permission. Local-only branches,
uncommitted files, checked-out worktrees, or worker prose do not satisfy the
contract. Do not use a protected ref, PR, merge, tag, release, deployment, issue,
workflow, or settings change as transport.

For a public repository, the parent must pass `--allow-public-artifact` and
explicitly acknowledge durable public Git retention. Contracts marked `secret`,
`personal`, or `regulated` are never public. The helper performs no implicit
write and does not delete the branch automatically.

## Exact allowed Git object

The worker is authorized to create only one commit on the contract branch. Parent
verification accepts it only if it has:

1. exactly one parent equal to the prepared base SHA;
2. exactly the contract commit message;
3. exactly one changed path, an added (not modified, deleted, renamed, symlinked,
   executable, or submodule) regular `100644` blob at the contract Markdown path;
4. strict UTF-8 bytes with no BOM, CR, NUL, binary content, or missing final LF;
5. exact contract/manifest size and SHA-256; and
6. an artifact branch still pointing at that commit.

The prepared base may fast-forward only if it retains the prepared base and does
not contain the artifact commit. Base rewrite/deletion, artifact merge, branch
movement, or any protected-ref change outside a contract-permitted base
fast-forward fails closed.

## Parent-side verification sequence

`pro-dispatch artifact verify` initializes a private bare repository. It never
trusts a checkout or a caller-supplied file. It fetches only exact object IDs and
proves the branch, parent, commit message, tree entry, blob bytes, digest, branch
stability, and protected refs before it marks the immutable result complete.

Use:

```bash
pro-dispatch artifact verify '<assignment-id>' --result-file '<private-result-file>'
```

Use `--discover` only when the assignment was already prepared in artifact mode
and readable chat evidence is unavailable. Discovery verifies the same stored
contract against the remote branch/path; it is not permission to discover or
write arbitrary repository content.

Keep these states distinct in reports:

- worker-reported remote work;
- parent-verified immutable artifact object;
- parent-run local tests; and
- unverified host-specific behavior.

No response retrieval failure proves that a Git write did not happen. Inspect only
the pre-authorized remote branch/path and recover without resending the original
assignment. The parent must not silently make a replacement write and attribute it
to Pro.
