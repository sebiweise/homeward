#!/bin/bash
# SessionStart hook: make the graphify knowledge graph and the dev tools available.
# Cloud sessions install graphify and the dev dependencies; local sessions only
# rebuild the graph when graphify is already installed.
set -euo pipefail

GRAPHIFY_VERSION="0.9.95"
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
  if ! command -v graphify >/dev/null 2>&1; then
    pip install --quiet "graphifyy==${GRAPHIFY_VERSION}" >&2
  fi
  if ! python3 -c "import homeward, pytest, mypy" >/dev/null 2>&1; then
    pip install --quiet -e ".[dev]" >&2
  fi
  # The image ships its own pytest/ruff in ~/.local/bin, ahead of pip's tools on
  # PATH; those cannot import homeward, so prefer the pip-installed ones.
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    pip_bin="$(dirname "$(python3 -c 'import sys; print(sys.executable)')")"
    for candidate in /usr/local/bin "$pip_bin"; do
      if [ -x "$candidate/pytest" ]; then
        echo "export PATH=\"$candidate:\$PATH\"" >> "$CLAUDE_ENV_FILE"
        break
      fi
    done
  fi
fi

# graphify-out/ is not committed: rebuild it from the checked-out code (AST only,
# no LLM, a few seconds). A failed build must not block the session.
if command -v graphify >/dev/null 2>&1; then
  graphify update . --force >&2 || echo "graphify update failed; continuing without graph" >&2
fi
