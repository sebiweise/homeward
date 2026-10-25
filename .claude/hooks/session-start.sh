#!/bin/bash
# SessionStart hook: build the graphify code knowledge graph.
# Cloud sessions install graphify (pinned, with hashes); local sessions only
# rebuild the graph when graphify is already installed.
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

if [[ "${CLAUDE_CODE_REMOTE:-}" == "true" ]] && ! command -v graphify >/dev/null 2>&1; then
  pip install --quiet --only-binary :all: --require-hashes -r requirements/graphify.txt >&2
fi

# graphify-out/ is not committed: rebuild it from the checked-out code (AST only,
# no LLM, a few seconds). A failed build must not block the session.
if command -v graphify >/dev/null 2>&1; then
  graphify update . --force >&2 || echo "graphify update failed; continuing without graph" >&2
fi
