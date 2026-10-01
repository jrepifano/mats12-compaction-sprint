#!/usr/bin/env bash
# I1a run chain, ON the pod (implementation plan §2 U1; paid steps P0 and P1 of §5). Each step is
# one call of the on-pod driver, `/ws/venv-i1a/bin/python scripts/i1a_pod.py <step>` (written in
# Wave 2). Patterned on scripts/pod_run_e1.sh (detached launch, manifest-gated completion),
# itself patterned on ../mats12_neel_working/scripts/pod_run_arms.sh; written 2026-09-29,
# hardened after gpt-6-sol review U1 r1.
#
# Usage: bash scripts/pod_run_i1a.sh STEP [--detach]
#   P0 steps: download | smoke
#   P1 steps: capture | tokens | olens-gate | olens-extra | shuffled | reserve | fve | probe |
#             manifest
#   all     : download, then the P1 steps in that order, stopping at the first failure. It never
#             runs smoke: smoke is P0 and stops at checkpoint 2.
#   --detach: relaunch itself with `setsid nohup ... & disown` (log logs/pod_i1a_run.log), e.g.
#     ssh -p PORT root@HOST "cd /ws/emx && bash scripts/pod_run_i1a.sh all --detach"
#
# Checkpoint gates (this script only reads markers; it never creates them):
#   P0 steps need logs/approvals/I1a-P0.approved, and the projected P0 GPU cost
#     EMX_POD_RATE_USD_H x ((now - EMX_POD_CREATED_EPOCH)/3600 + I1A_P0_EST_HOURS [default 1.0])
#     must be <= contracts.POD_SMOKE_CAP_USD ($6). pod_push.sh --exp I1a writes both EMX_POD_* values
#     into the pod .env; without them a P0 step refuses. (The laptop watchdog enforces the same $6
#     line on the real spend while no P1 approval exists.)
#   P1 steps (individually or via all) need logs/approvals/I1a-P1.approved AND a complete smoke
#     step (see "complete" below).
#   A marker counts only if it is a non-empty file with a line starting with a UTC timestamp
#   (2026-09-29T12:00:00Z ...). The main session writes it after Jacob's go-ahead and
#   pod_push.sh --exp I1a sends it.
#
# Completion. A step is complete (and skipped on re-run) only if BOTH
#   results/I1a/MANIFEST.<step>.sha256   `shasum -a 256` lines, repo-relative paths, verifies; and
#   results/I1a/MANIFEST.<step>.json     the sidecar, schema:
#       {"step": "<step>",                        must equal the step name
#        "required_outputs": ["results/I1a/..."], non-empty; every path is listed in the .sha256
#        "counts": {"<name>": <int>, ...},        rows/sites/files the driver asserted
#        "code_commit": "<git sha>",              EMX_GIT_COMMIT of the code that produced it
#        "inputs_sha256": {"<repo path>": "<sha256>"},  small inputs only (upstream manifests,
#                                                 the subset file, download_paths.json);
#                                                 re-hashed at every check
#        "limited": false}                        optional; true marks a cut-down (test) run
#   is valid. A step that exits 0 without a complete manifest pair counts as FAILED.
#   Rejected as well (integrated review r2):
#     - "limited": true, unless I1A_ALLOW_LIMITED=1 (tests only);
#     - a code_commit that differs from the current code, unless I1A_ALLOW_STALE=1. The pod has
#       no .git (pod_push never sends it), so "current" is EMX_GIT_COMMIT from the pod .env
#       (pod_push writes it); off-pod it falls back to `git rev-parse HEAD`. With neither, every
#       sidecar is stale. After a code fix, completed steps therefore re-run unless the main
#       session sets I1A_ALLOW_STALE=1 deliberately.
#
# Logs: each step's driver output is appended to logs/pod_i1a_<step>.log.
# I1A_DRY_RUN=1 prints what would run (lines starting "+ ") and runs nothing but the checks.
# I1A_PY overrides the driver's python (default /ws/venv-i1a/bin/python).
# bash 3.2 compatible (no associative arrays, mapfile, ${x,,} or |&).
set -euo pipefail

P0_STEPS="download smoke"
CHAIN="capture tokens olens-gate olens-extra shuffled reserve fve probe manifest"
STEPS="$P0_STEPS $CHAIN"
usage() {
  echo "usage: pod_run_i1a.sh {$(echo "$STEPS" | tr ' ' '|')|all} [--detach]" >&2
  exit 2
}

MODE="${1:-}"; [ -n "$MODE" ] || usage; shift
DETACH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --detach) DETACH=1; shift ;;
    *) echo "unknown argument $1" >&2; usage ;;
  esac
done
case " $STEPS all " in
  *" $MODE "*) ;;
  *) echo "unknown step $MODE" >&2; usage ;;
esac

cd "$(dirname "$0")/.."
DRY="${I1A_DRY_RUN:-0}"
PY="${I1A_PY:-/ws/venv-i1a/bin/python}"
RESULTS=results/I1a
APPROVALS=logs/approvals

say() { echo "$(date -u +%FT%TZ) $*"; }

if [ "$DETACH" = "1" ]; then
  if command -v setsid >/dev/null 2>&1; then LAUNCH="setsid nohup"; else LAUNCH="nohup"; fi
  if [ "$DRY" = "1" ]; then
    echo "+ $LAUNCH bash scripts/pod_run_i1a.sh $MODE >>logs/pod_i1a_run.log 2>&1 </dev/null & disown"
    exit 0
  fi
  mkdir -p logs
  $LAUNCH bash scripts/pod_run_i1a.sh "$MODE" >>logs/pod_i1a_run.log 2>&1 </dev/null &
  pid=$!
  disown
  say "launched pod_run_i1a.sh $MODE detached (pid $pid); log logs/pod_i1a_run.log"
  exit 0
fi

if [ "$DRY" != "1" ]; then
  mkdir -p logs "$RESULTS"
  set +x; set -a; if [ -f .env ]; then . ./.env; fi; set +a   # never trace the token
  export EMX_EXP=I1a HF_HUB_DISABLE_XET=1 WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
  export HF_HOME="${HF_HOME:-/ws/hf_cache}"
  [ -x "$PY" ] || { say "driver python $PY not found; run pod_setup.sh --only i1a"; exit 1; }
fi
say "=== pod_run_i1a.sh $MODE dry=$DRY commit=${EMX_GIT_COMMIT:-unknown} dirty=${EMX_GIT_DIRTY:-unknown}"

CURRENT_COMMIT="${EMX_GIT_COMMIT:-}"
if [ -z "$CURRENT_COMMIT" ] && [ -d .git -o -f .git ]; then
  CURRENT_COMMIT=$(git rev-parse HEAD 2>/dev/null || true)   # off-pod only; read-only
fi

manifest() { echo "$RESULTS/MANIFEST.$1.sha256"; }
sidecar() { echo "$RESULTS/MANIFEST.$1.json"; }

sha_check() {  # sha_check MANIFEST: verify from the repo root (paths are repo-relative)
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c --quiet "$1" >/dev/null 2>&1
  else
    shasum -a 256 -c --quiet "$1" >/dev/null 2>&1
  fi
}

sidecar_ok() {  # sidecar_ok STEP: the sidecar schema above, checked against the .sha256 list
  python3 - "$(manifest "$1")" "$(sidecar "$1")" "$1" "$CURRENT_COMMIT" \
    "${I1A_ALLOW_LIMITED:-0}" "${I1A_ALLOW_STALE:-0}" <<'PY'
import hashlib
import json
import sys

man, side, step, commit, allow_limited, allow_stale = sys.argv[1:7]
try:
    d = json.load(open(side))
except (OSError, ValueError) as e:
    print(f"  sidecar {side}: {e}", file=sys.stderr)
    sys.exit(1)
listed = set()
for line in open(man):
    if line.strip():
        parts = line.rstrip("\n").split(None, 1)
        if len(parts) != 2:
            print(f"  {man}: malformed line {line!r}", file=sys.stderr)
            sys.exit(1)
        listed.add(parts[1].lstrip("*"))
errs = []
if not isinstance(d, dict):
    d = {}
    errs.append("not a JSON object")
if d.get("step") != step:
    errs.append(f"step is {d.get('step')!r}, not {step!r}")
req = d.get("required_outputs")
if not isinstance(req, list) or not req or not all(isinstance(x, str) and x for x in req):
    errs.append("required_outputs must be a non-empty list of paths")
else:
    missing = [x for x in req if x not in listed]
    if missing:
        errs.append(f"{len(missing)} required outputs not in the sha list, e.g. {missing[:3]}")
counts = d.get("counts")
if not isinstance(counts, dict) or not all(type(v) is int for v in counts.values()):
    errs.append("counts must be an object of integers")
if not isinstance(d.get("code_commit"), str) or not d.get("code_commit"):
    errs.append("code_commit missing")
ins = d.get("inputs_sha256")
if not isinstance(ins, dict):
    errs.append("inputs_sha256 must be an object")
else:
    for path, want in ins.items():
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            errs.append(f"input {path} missing")
            continue
        if h.hexdigest() != want:
            errs.append(f"input {path} changed since the step ran")
if errs:
    print(f"  {side}: " + "; ".join(errs), file=sys.stderr)
    sys.exit(1)
limited = d.get("limited", False)
if type(limited) is not bool:
    errs.append(f"limited must be a boolean, got {limited!r}")
elif limited and allow_limited != "1":
    errs.append("a limited (cut-down) run; set I1A_ALLOW_LIMITED=1 only in tests")
if not errs and d["code_commit"] != commit:
    if allow_stale == "1":
        print(f"  note: {step} was produced by code {d['code_commit'][:12]}, now "
              f"{commit[:12] or 'unknown'} (accepted: I1A_ALLOW_STALE=1)", file=sys.stderr)
    else:
        errs.append(f"stale: produced by code {d['code_commit'][:12]}, current code is "
                    f"{commit[:12] or 'unknown (no EMX_GIT_COMMIT)'}")
if errs:
    print(f"  {side}: " + "; ".join(errs), file=sys.stderr)
    sys.exit(1)
PY
}

step_done() {  # both manifests present and valid (see "Completion" above)
  local m
  m=$(manifest "$1")
  [ -s "$m" ] && sha_check "$m" && [ -s "$(sidecar "$1")" ] && sidecar_ok "$1"
}

approved() {  # approved P0|P1: a non-empty marker with a UTC-timestamp line
  local f="$APPROVALS/I1a-$1.approved"
  [ -s "$f" ] && grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' "$f"
}

p0_cost_ok() {  # projected P0 GPU cost <= contracts.POD_SMOKE_CAP_USD
  python3 - "${EMX_POD_RATE_USD_H:-}" "${EMX_POD_CREATED_EPOCH:-}" "${I1A_P0_EST_HOURS:-1.0}" <<'PY'
import sys
import time

sys.path.insert(0, "src")
from emx.i1a.contracts import POD_SMOKE_CAP_USD as CAP  # noqa: E402

rate, created, est = sys.argv[1:4]
try:
    rate, created, est = float(rate), float(created), float(est)
except ValueError:
    print("  P0 cost check: EMX_POD_RATE_USD_H / EMX_POD_CREATED_EPOCH missing from the pod .env "
          "(pod_push.sh --exp I1a writes them)", file=sys.stderr)
    sys.exit(1)
elapsed = max(0.0, time.time() - created) / 3600
projected = rate * (elapsed + est)
ok = projected <= CAP
print(f"  P0 cost check: ${rate}/h x ({elapsed:.2f} h so far + {est} h) = ${projected:.2f} "
      f"{'<=' if ok else '>'} P0 cap ${CAP:.2f}", file=sys.stderr)
sys.exit(0 if ok else 1)
PY
}

gate() {  # gate STEP: refuse unless its checkpoint is approved (and, for P0, affordable)
  case " $P0_STEPS " in
    *" $1 "*)
      approved P0 || { say "REFUSED $1: $APPROVALS/I1a-P0.approved missing or invalid (checkpoint 1)"; return 1; }
      if ! p0_cost_ok; then
        if [ "$DRY" = "1" ]; then say "(dry run) $1 would be refused by the P0 cost check"; return 0; fi
        say "REFUSED $1: P0 cost check failed"; return 1
      fi ;;
    *)
      approved P1 || { say "REFUSED $1: $APPROVALS/I1a-P1.approved missing or invalid (checkpoint 2)"; return 1; }
      step_done smoke || { say "REFUSED $1: the smoke step is not complete ($(manifest smoke) + $(sidecar smoke))"; return 1; } ;;
  esac
}

run_step() {
  local step=$1 log rc
  log="logs/pod_i1a_$step.log"
  if step_done "$step"; then
    say "skip $step: $(manifest "$step") and $(sidecar "$step") verify"
    return 0
  fi
  gate "$step" || return 3
  echo "+ $PY scripts/i1a_pod.py $step >>$log 2>&1"
  [ "$DRY" = "1" ] && return 0
  say "start $step" | tee -a "$log"
  rc=0
  "$PY" scripts/i1a_pod.py "$step" >>"$log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    say "FAILED $step (exit $rc); see $log" | tee -a "$log"
    return 1
  fi
  if ! step_done "$step"; then
    say "FAILED $step: exited 0 but its manifest pair is missing or invalid" | tee -a "$log"
    return 1
  fi
  say "done $step" | tee -a "$log"
}

if [ "$MODE" != "all" ]; then
  rc=0; run_step "$MODE" || rc=$?
  exit "$rc"
fi

for step in download $CHAIN; do
  rc=0; run_step "$step" || rc=$?
  if [ "$rc" -ne 0 ]; then say "chain stopped at $step"; exit "$rc"; fi
done
say "=== pod_run_i1a.sh all complete"
