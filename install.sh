#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
BIN_DIR="${HOME}/.local/bin"
BIN_TARGET="${BIN_DIR}/pro-dispatch"
SKILL_TARGET="${HOME}/.agents/skills/codex-pro-dispatch"
EXPECTED_BIN="${ROOT}/bin/pro-dispatch"
EXPECTED_SKILL="${ROOT}/skills/codex-pro-dispatch"
HOOK_TARGET="${CODEX_HOME}/hooks.json"
EXPECTED_SUPERVISOR="${EXPECTED_SKILL}/scripts/resident-supervision.mjs"
LEGACY_SKILL_TARGET="${CODEX_HOME}/skills/codex-pro-dispatch"
MIGRATE_LEGACY=false

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "codex-pro-dispatch requires the official macOS ChatGPT/Codex desktop app." >&2
  exit 1
fi

command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
python3 - <<'PY'
import sys

if sys.version_info < (3, 9):
    print("codex-pro-dispatch requires Python 3.9 or newer.", file=sys.stderr)
    raise SystemExit(1)
PY

canonical_target() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1]).expanduser()
print(path.parent.resolve(strict=False) / path.name)
PY
}

SKILL_TARGET_CANONICAL="$(canonical_target "$SKILL_TARGET")"
LEGACY_SKILL_TARGET_CANONICAL="$(canonical_target "$LEGACY_SKILL_TARGET")"

if [[ "$LEGACY_SKILL_TARGET_CANONICAL" != "$SKILL_TARGET_CANONICAL" ]] && \
  [[ -e "$LEGACY_SKILL_TARGET" || -L "$LEGACY_SKILL_TARGET" ]]; then
  if [[ -L "$LEGACY_SKILL_TARGET" && "$(readlink "$LEGACY_SKILL_TARGET")" == "$EXPECTED_SKILL" ]]; then
    MIGRATE_LEGACY=true
  else
    echo "Refusing to migrate unowned legacy skill path: $LEGACY_SKILL_TARGET" >&2
    exit 1
  fi
fi

refuse_unowned_target() {
  local target="$1"
  local expected="$2"
  if [[ -L "$target" ]]; then
    local current
    current="$(readlink "$target")"
    if [[ "$current" == "$expected" ]]; then
      return 0
    fi
    echo "Refusing to replace existing symlink: $target -> $current" >&2
    exit 1
  fi
  if [[ -e "$target" ]]; then
    echo "Refusing to replace existing path: $target" >&2
    exit 1
  fi
}

refuse_unowned_target "$BIN_TARGET" "$EXPECTED_BIN"
refuse_unowned_target "$SKILL_TARGET" "$EXPECTED_SKILL"

command -v node >/dev/null || {
  echo "Node.js is required for the parked client; install Node.js separately." >&2
  exit 1
}
if ! node --input-type=module - <<'JS'
import { readFile, mkdtemp } from "node:fs/promises";
import { createServer, createConnection } from "node:net";
import { randomBytes } from "node:crypto";
import { execFile } from "node:child_process";
if ([readFile, mkdtemp, createServer, createConnection, randomBytes, execFile]
    .some(value => typeof value !== "function"))
  throw Error("Required Node.js standard-library functions are unavailable");
JS
then
  echo "Node.js standard-library preflight failed." >&2
  exit 1
fi
for name in parked-runner.js parked-socket.mjs parked-client.mjs parked-activation.mjs parked-serving.mjs resident-supervision.mjs; do
  source_file="${EXPECTED_SKILL}/scripts/${name}"
  if [[ ! -f "$source_file" || -L "$source_file" ]]; then
    echo "Missing regular packaged script: $source_file" >&2
    exit 1
  fi
done

# Install one user-level hook so source installs work from any trusted project.
# The merge is deliberately narrow: preserve every unrelated value, accept only
# our exact existing handler, and refuse malformed or competing supervision.
python3 - "$HOOK_TARGET" "$EXPECTED_SUPERVISOR" <<'PY'
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

target = Path(sys.argv[1])
supervisor = str(Path(sys.argv[2]).resolve(strict=True))
handler = {
    "type": "command",
    "command": f"node {shlex.quote(supervisor)} stop",
    "timeout": 15,
    "async": False,
}
group = {"hooks": [handler]}

if target.is_symlink():
    raise SystemExit(f"Refusing symlink hook configuration: {target}")
if target.exists():
    if not target.is_file():
        raise SystemExit(f"Refusing non-file hook configuration: {target}")
    try:
        config = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Malformed hook configuration {target}: {exc}")
else:
    config = {}

if not isinstance(config, dict):
    raise SystemExit(f"Malformed hook configuration {target}: root must be an object")
hooks = config.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise SystemExit(f"Malformed hook configuration {target}: hooks must be an object")
stop = hooks.setdefault("Stop", [])
if not isinstance(stop, list):
    raise SystemExit(f"Malformed hook configuration {target}: hooks.Stop must be an array")

matches = 0
for index, candidate_group in enumerate(stop):
    if not isinstance(candidate_group, dict):
        raise SystemExit(
            f"Malformed hook configuration {target}: hooks.Stop[{index}] must be an object"
        )
    candidate_handlers = candidate_group.get("hooks")
    if not isinstance(candidate_handlers, list):
        raise SystemExit(
            f"Malformed hook configuration {target}: hooks.Stop[{index}].hooks must be an array"
        )
    for handler_index, candidate in enumerate(candidate_handlers):
        if not isinstance(candidate, dict):
            raise SystemExit(
                f"Malformed hook configuration {target}: "
                f"hooks.Stop[{index}].hooks[{handler_index}] must be an object"
            )
        command = candidate.get("command")
        if isinstance(command, str) and "resident-supervision.mjs" in command:
            if candidate_group == group:
                matches += 1
            else:
                raise SystemExit(
                    f"Conflicting resident supervision hook in {target}; refusing to replace it"
                )

if matches > 1:
    raise SystemExit(f"Conflicting duplicate resident supervision hooks in {target}")
if matches == 0:
    stop.append(group)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = (target.stat().st_mode & 0o777) if target.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".hooks.json.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
PY

mkdir -p "$BIN_DIR" "${HOME}/.agents/skills"
chmod +x "$EXPECTED_BIN"

[[ -L "$BIN_TARGET" ]] || ln -s "$EXPECTED_BIN" "$BIN_TARGET"
[[ -L "$SKILL_TARGET" ]] || ln -s "$EXPECTED_SKILL" "$SKILL_TARGET"
if $MIGRATE_LEGACY; then
  rm "$LEGACY_SKILL_TARGET"
  echo "Migrated legacy skill link from $LEGACY_SKILL_TARGET"
fi

echo "Installed source-visible links:"
echo "  $BIN_TARGET -> $EXPECTED_BIN"
echo "  $SKILL_TARGET -> $EXPECTED_SKILL"
echo "  $HOOK_TARGET -> $EXPECTED_SUPERVISOR (synchronous Stop hook)"
echo
echo "No native session was started and no worker configuration was changed."
echo "Restart the Codex desktop app, review and trust the installed hook with /hooks,"
echo "then start a new Listener task before resident qualification."
echo "After native ownership and qualification gates pass, invoke explicitly:"
echo "  \$codex-pro-dispatch"
