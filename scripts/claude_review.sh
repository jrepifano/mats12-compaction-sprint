#!/bin/bash
# Run a Claude review non-interactively, with the project's 20-minute timeout (AGENTS.md).
# Usage: scripts/claude_review.sh <prompt_file> <output_md> [effort] [seconds]
# The counterpart of scripts/codex_review.sh, with the same arguments, for work a Codex session
# ran: the reviewer comes from the other model family (AGENTS.md, "Agents and tools", 2026-10-11).
# Model: Claude Fable 5.1 (alias `fable`, the most capable widely released Claude model as of
# 2026-10); override with CLAUDE_REVIEW_MODEL. effort defaults to medium (low|medium|high|xhigh|max).
# Runs from the repo root the script lives in. The review text goes to <output_md>; the CLI's JSON
# result (usage and cost included) and stderr go to <output_md>.log.
#
# Read-only: the only tools are Read, Grep and Glob, in `dontAsk` permission mode, so anything
# else is denied rather than prompted. There is no shell at all: an allowlist of "read-only" git
# commands is not read-only (`git diff --output=<file>` writes; review r1 finding 3). So the
# reviewer can't run git or Python: set CLAUDE_REVIEW_DIFF=<range> (e.g. a87bd51..HEAD) and the
# script saves `git log --stat` and `git diff` of that range to <output_md>.diff (gitignored) and
# tells the reviewer to read it; give it the source files of any numbers to recompute by reading.
# Files outside the repo root are denied too. No MCP servers, no session saved.
# Timeout and sleep handling follow codex_review.sh: a watchdog kills the CLI and its children at
# the limit, polling the wall clock; `caffeinate -is` keeps the laptop awake (clamshell sleep
# still pauses everything). bash 3.2 compatible (macOS).
set -u
cd "$(dirname "$0")/.." || exit 1
prompt="$1"
out="$2"
effort="${3:-medium}"
limit="${4:-1200}"
model="${CLAUDE_REVIEW_MODEL:-fable}"
mkdir -p "$(dirname "$out")"
raw="$out.json.tmp"
full="$out.prompt.tmp"
cp "$prompt" "$full" || exit 1
if [ -n "${CLAUDE_REVIEW_DIFF:-}" ]; then
  { git log --stat "$CLAUDE_REVIEW_DIFF" && echo && git diff "$CLAUDE_REVIEW_DIFF"; } \
    > "$out.diff" || { echo "claude_review: git diff $CLAUDE_REVIEW_DIFF failed" >&2; exit 1; }
  printf '\n\n(Added by scripts/claude_review.sh) You have no shell. `git log --stat` and `git diff` of %s are saved in %s; read them with the Read tool.\n' \
    "$CLAUDE_REVIEW_DIFF" "$out.diff" >> "$full"
fi

# The prompt goes on stdin.
caffeinate -is claude -p --model "$model" --effort "$effort" \
  --output-format json --no-session-persistence --strict-mcp-config \
  --permission-mode dontAsk --tools "Read,Grep,Glob" \
  < "$full" > "$raw" 2> "$out.log" &
pid=$!
start=$(date +%s)
(
  while kill -0 "$pid" 2>/dev/null && [ $(( $(date +%s) - start )) -lt "$limit" ]; do
    sleep 5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "watchdog: killing claude after ${limit}s" >> "$out.log"
    pkill -TERM -P "$pid" 2>/dev/null
    kill -TERM "$pid" 2>/dev/null
    sleep 10
    pkill -KILL -P "$pid" 2>/dev/null
    kill -KILL "$pid" 2>/dev/null
  fi
) &
watchdog=$!
wait "$pid"
rc=$?
kill "$watchdog" 2>/dev/null
wait "$watchdog" 2>/dev/null

# Split the JSON result: the review text to <output_md>, the whole record to the log. The output
# file is replaced only by a valid, non-empty review (review r1 finding 5), so a failed run never
# leaves an older review looking like this run's.
python3 -I - "$raw" "$out.new" >> "$out.log" 2>&1 <<'PY'
import json, sys
raw, out = sys.argv[1], sys.argv[2]
text = open(raw, encoding="utf-8").read()
try:
    rec = json.loads(text)
except json.JSONDecodeError:
    print("claude_review: output was not JSON; raw output follows")
    print(text)
    sys.exit(1)
print(json.dumps(rec, indent=1))
result = rec.get("result") if isinstance(rec, dict) else None
if not isinstance(result, str) or not result.strip() or rec.get("is_error"):
    print("claude_review: no valid review text in the result")
    sys.exit(1)
with open(out, "w", encoding="utf-8") as f:
    f.write(result.rstrip("\n") + "\n")
PY
prc=$?
rm -f "$raw" "$full"
if [ "$rc" -eq 0 ] && [ "$prc" -eq 0 ]; then
  mv "$out.new" "$out"
else
  rm -f "$out.new"
  echo "claude_review: no review written (claude exit $rc, parse exit $prc); see $out.log" >&2
  [ "$rc" -eq 0 ] && rc=1
fi
echo "exit=$rc"
exit "$rc"
