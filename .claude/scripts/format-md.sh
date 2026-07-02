#!/usr/bin/env bash
set -euo pipefail

# PostToolUse hook — auto-formats Professor-owned .md files after Edit/Write.
# Receives hook JSON on stdin. Silently exits for non-matching files.
# Requires prettier (via npx); no-ops silently if unavailable.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[[ -z "$FILE_PATH" ]] && exit 0
[[ "$FILE_PATH" != *.md ]] && exit 0
[[ ! -f "$FILE_PATH" ]] && exit 0

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
REL_PATH="${FILE_PATH#"$REPO_ROOT"/}"

# Only format Professor-owned files — never user source or docs.
case "$REL_PATH" in
  CLAUDE.md) ;;
  .claude/*.md) ;;
  .professor/*.md) ;;
  *) exit 0 ;;
esac

npx --no-install prettier --write --prose-wrap preserve "$FILE_PATH" >/dev/null 2>&1 || true
