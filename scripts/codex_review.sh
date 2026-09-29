#!/bin/bash
# Run a gpt-6-sol review through Codex with the project's 20-minute timeout (CLAUDE.md).
# Usage: scripts/codex_review.sh <prompt_file> <output_md>
# Runs from the repo root the script lives in; stdout/stderr go to <output_md>.log.
set -u
cd "$(dirname "$0")/.." || exit 1
prompt="$1"
out="$2"
mkdir -p "$(dirname "$out")"
perl -e 'alarm 1200; exec @ARGV' \
  codex exec -m gpt-6-sol -c model_reasoning_effort=high --sandbox read-only \
  -o "$out" - < "$prompt" > "$out.log" 2>&1
echo "exit=$?"
