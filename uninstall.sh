#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${HOME}/.local/bin/pro-dispatch"

if [[ -L "${TARGET}" && "$(readlink "${TARGET}")" == "${ROOT}/bin/pro-dispatch" ]]; then
  rm "${TARGET}"
  echo "Removed ${TARGET}"
elif [[ -e "${TARGET}" ]]; then
  echo "Refusing to remove ${TARGET}: it does not point to this checkout." >&2
  exit 1
else
  echo "Nothing to remove."
fi

echo "Local configuration and receipts were left intact."
echo "Remove them manually only after reviewing their contents:"
echo "  ~/.config/codex-pro-dispatch"
echo "  ~/.local/state/codex-pro-dispatch"
