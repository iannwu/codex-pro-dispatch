#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
TARGET="${BIN_DIR}/pro-dispatch"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "codex-pro-dispatch requires macOS." >&2
  exit 1
fi

command -v python3 >/dev/null || {
  echo "python3 is required. Install Xcode Command Line Tools." >&2
  exit 1
}
command -v swift >/dev/null || {
  echo "Swift is required. Install Xcode Command Line Tools." >&2
  exit 1
}

mkdir -p "${BIN_DIR}"
chmod +x "${ROOT}"/bin/pro-dispatch "${ROOT}"/bin/cgpt-*
ln -sfn "${ROOT}/bin/pro-dispatch" "${TARGET}"

mkdir -p "${HOME}/.config/codex-pro-dispatch"
mkdir -p "${HOME}/.local/state/codex-pro-dispatch"
chmod 700 "${HOME}/.config/codex-pro-dispatch" "${HOME}/.local/state/codex-pro-dispatch"

echo "Installed: ${TARGET}"
if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
  echo
  echo "Add this line to your shell profile:"
  echo "  export PATH=\"${BIN_DIR}:\$PATH\""
fi

echo
echo "Next:"
echo "  1. Open ChatGPT Classic on a dedicated Pro worker thread."
echo "  2. Grant Accessibility permission to the host that runs pro-dispatch."
echo "  3. Run: pro-dispatch apps"
echo '  4. Run: pro-dispatch configure --app-name "ChatGPT Classic"'
echo "  5. Run: pro-dispatch doctor"
echo "  6. Run: pro-dispatch smoke --direct --timeout 300"
echo
echo "For Codex Desktop, use daemon transport if Accessibility does not propagate:"
echo '  pro-dispatch configure --app-name "ChatGPT Classic" --transport daemon'
echo "  pro-dispatch serve"
