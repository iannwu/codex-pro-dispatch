#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
BIN_DIR="${HOME}/.local/bin"
BIN_TARGET="${BIN_DIR}/pro-dispatch"
SKILL_TARGET="${HOME}/.agents/skills/codex-pro-dispatch"
EXPECTED_BIN="${ROOT}/bin/pro-dispatch"
EXPECTED_SKILL="${ROOT}/skills/codex-pro-dispatch"
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
for name in parked-runner.js parked-socket.mjs parked-client.mjs parked-activation.mjs parked-serving.mjs; do
  source_file="${EXPECTED_SKILL}/scripts/${name}"
  if [[ ! -f "$source_file" || -L "$source_file" ]]; then
    echo "Missing regular packaged script: $source_file" >&2
    exit 1
  fi
done

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
echo
echo "No native session was started and no worker configuration was changed."
echo "If skill discovery is unavailable, stop; do not restart the app."
echo "After native ownership and qualification gates pass, invoke explicitly:"
echo "  \$codex-pro-dispatch"
