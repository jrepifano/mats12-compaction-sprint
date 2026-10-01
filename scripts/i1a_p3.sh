#!/bin/bash
# I1a P3: gate judging and baselines (prereg §4.6; Jacob's go-ahead in logs/approvals/I1a-P3.approved).
# Sequential; stops on a guard refusal (exit 2) or key/config error (exit 3). Exit 4 (cells left
# unjudged after retries) is logged and the next run goes on; a rerun of this script resumes from
# the caches and re-queues only what is missing. bash 3.2 compatible.
#   caffeinate -i bash scripts/i1a_p3.sh >> logs/I1a-p3.log 2>&1
cd "$(dirname "$0")/.." || exit 1
fails=""
step() {
  echo "=== $(date -u +%FT%TZ) $*"
  "$@"
  rc=$?
  echo "=== $(date -u +%FT%TZ) exit $rc: $*"
  if [ "$rc" -eq 2 ] || [ "$rc" -eq 3 ]; then
    echo "=== STOP: guard refusal or config error"
    exit "$rc"
  fi
  [ "$rc" -ne 0 ] && fails="$fails [$rc: $*]"
  return 0
}
step uv run python scripts/i1a_judge.py --stage gate --arm ridge_probe --family all
step uv run python scripts/i1a_judge.py --stage gate --arm olens_rl600 --family all
step uv run python scripts/i1a_judge.py --stage gate --arm jlens --family all
step uv run python scripts/i1a_judge.py --stage gate --arm logit_lens --family all
step uv run python -m emx.i1a.text_monitor
step uv run python scripts/i1a_judge.py --stage gate --arm text_monitor --family all
echo "=== $(date -u +%FT%TZ) P3 done; not clean:${fails:- none}"
