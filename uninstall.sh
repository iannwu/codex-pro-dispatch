#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
BIN_TARGET="${HOME}/.local/bin/pro-dispatch"
SKILL_TARGET="${HOME}/.agents/skills/codex-pro-dispatch"
LEGACY_SKILL_TARGET="${CODEX_HOME}/skills/codex-pro-dispatch"
EXPECTED_BIN="${ROOT}/bin/pro-dispatch"
EXPECTED_SKILL="${ROOT}/skills/codex-pro-dispatch"
PURGE_STATE=false

if [[ "${1:-}" == "--purge-state" ]]; then
  PURGE_STATE=true
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--purge-state]" >&2
  exit 2
fi

verify_owned_link() {
  local target="$1"
  local expected="$2"
  if [[ ! -e "$target" && ! -L "$target" ]]; then
    return 0
  fi
  if [[ ! -L "$target" || "$(readlink "$target")" != "$expected" ]]; then
    echo "Refusing to remove unowned path: $target" >&2
    exit 1
  fi
}

remove_owned_link() {
  local target="$1"
  local expected="$2"
  verify_owned_link "$target" "$expected"
  if [[ ! -e "$target" && ! -L "$target" ]]; then
    return 0
  fi
  rm "$target"
  echo "Removed $target"
}

# Validate every target before purging state or removing either link. This keeps
# an ownership failure from leaving a partially uninstalled installation.
verify_owned_link "$BIN_TARGET" "$EXPECTED_BIN"
verify_owned_link "$SKILL_TARGET" "$EXPECTED_SKILL"
verify_owned_link "$LEGACY_SKILL_TARGET" "$EXPECTED_SKILL"

if $PURGE_STATE; then
  "$EXPECTED_BIN" purge --yes
fi

remove_owned_link "$BIN_TARGET" "$EXPECTED_BIN"
remove_owned_link "$SKILL_TARGET" "$EXPECTED_SKILL"
remove_owned_link "$LEGACY_SKILL_TARGET" "$EXPECTED_SKILL"

echo "Checkout retained at $ROOT"
if ! $PURGE_STATE; then
  echo "Private worker and assignment state was retained."
fi
