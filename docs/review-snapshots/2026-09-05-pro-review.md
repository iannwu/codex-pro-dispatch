# Pro review snapshot

This branch captures the current uncommitted v1.2 candidate for read-only review
through the GitHub connector. It is not a release or a merge request.

- Source base commit: `3e4a789273be5e2f4135253b0ae86bbc914cbbfd`.
- Source branch: `codex/long-result-transport-v1.2.0`.
- Original staging area: empty.
- Candidate changes: 28 modified/new source, test, and documentation files.
- Ignored files (including private receipts, transcripts, caches, and virtual
  environments) are excluded.
- This context file is the only additional file beyond the candidate snapshot.
- The source checkout, branch, and staging area were not changed by capture.

Compare this snapshot commit to its parent to inspect the complete candidate
change, including files that were untracked locally. Use this branch's files,
not main or the clean parent commit, for all review claims.

## Review inputs

- `docs/specs/connectivity-probe.md`
- `docs/reviews/connectivity-probe-cursor-fable-5.1.md`
- `docs/native-read-investigation-2026-09-05.md`
- Relevant `src/codex_pro_dispatch/` and `tests/` files

## Captured status

```text
 M .codex-plugin/plugin.json
 M CHANGELOG.md
 M README.md
 M SECURITY.md
 M VERSION
 M docs/acceptance.md
 M docs/compatibility.md
 M docs/specs/long-result-transport-v1.2.0.md
 M skills/codex-pro-dispatch/SKILL.md
 M skills/codex-pro-dispatch/references/github-verification.md
 M skills/codex-pro-dispatch/references/native-protocol.md
 M src/codex_pro_dispatch/__init__.py
 M src/codex_pro_dispatch/cli.py
 M src/codex_pro_dispatch/collection.py
 M src/codex_pro_dispatch/core.py
 M tests/test_cli.py
 M tests/test_collection.py
 M tests/test_core.py
?? docs/native-read-investigation-2026-09-05.md
?? docs/reviews/connectivity-probe-cursor-fable-5.1.md
?? docs/specs/connectivity-probe.md
?? skills/codex-pro-dispatch/references/long-results.md
?? src/codex_pro_dispatch/artifact.py
?? src/codex_pro_dispatch/chunked.py
?? src/codex_pro_dispatch/transport.py
?? tests/test_artifact.py
?? tests/test_chunked.py
?? tests/test_migration.py
```
