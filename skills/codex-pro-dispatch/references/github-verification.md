# GitHub verification contract

Read this reference before authorizing a GitHub mutation and again when Chat Pro reports one.

## Before dispatch

Repository-write mode requires all of the following:

- the dedicated Pro worker exposes a GitHub connector or tool with the required write action for the exact repository
- the relevant starting commit and branch are remotely visible; local-only and uncommitted code is not implicitly shared
- organization, SSO, branch-protection, and repository policies permit the proposed disposable branch action
- the parent has independent remote read access for verification

Connector presence does not prove write permission. If the worker has read-only access, keep the assignment prompt-only. Test new connector authorization on a disposable unprotected branch, never a protected ref.

## Worker responsibility

The Chat Pro worker performs only the repository actions authorized by the assignment, using the GitHub connector available inside its own Chat conversation. Codex Pro Dispatch does not install, authenticate, or broaden that connector.

The worker should return concrete remote evidence, normally:

- repository
- verified base SHA
- branch name
- created commit SHA
- final branch SHA
- changed files
- CI or workflow results it actually inspected
- confirmation that protected refs were unchanged

The parent Codex task must not silently make the requested write on behalf of the worker and then attribute it to Pro.

## Parent verification

After collecting the worker result, independently verify through local Git or the parent GitHub connector:

1. The reported commit exists remotely.
2. Its parent or base is the required SHA.
3. The branch head equals the reported final SHA.
4. Commit message and changed files match the assignment.
5. Protected branches remain unchanged.
6. CI results are real and correspond to the reported SHA.
7. Local-only checks are run by the parent when required.

A worker's statement is not verification.

## Ambiguous transport result

A response retrieval failure does not prove the GitHub work failed. Before any follow-up, inspect the expected branch and commit state. Never resend the whole assignment automatically.

## Completion language

Distinguish clearly:

- worker-reported remote work
- parent-verified remote work
- parent-run local tests
- unverified physical or environment-specific behavior
