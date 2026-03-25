#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/resolve_conflicts.sh ours   # keep current branch version for all conflicts
#   scripts/resolve_conflicts.sh theirs # keep incoming branch version for all conflicts

MODE="${1:-ours}"
if [[ "$MODE" != "ours" && "$MODE" != "theirs" ]]; then
  echo "Usage: $0 [ours|theirs]"
  exit 2
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Error: not inside a git repository"
  exit 2
fi

mapfile -t CONFLICT_FILES < <(git diff --name-only --diff-filter=U)

if [[ ${#CONFLICT_FILES[@]} -eq 0 ]]; then
  echo "No conflicted files found."
  exit 0
fi

if [[ "$MODE" == "ours" ]]; then
  git checkout --ours -- "${CONFLICT_FILES[@]}"
else
  git checkout --theirs -- "${CONFLICT_FILES[@]}"
fi

git add -- "${CONFLICT_FILES[@]}"

echo "Resolved ${#CONFLICT_FILES[@]} conflicted file(s) using '$MODE' and staged them."
