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

if [[ -e "$LEGACY_SKILL_TARGET" || -L "$LEGACY_SKILL_TARGET" ]]; then
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
echo "Next: restart Codex if the skill is not immediately visible, then invoke:"
echo "  \$codex-pro-dispatch"
