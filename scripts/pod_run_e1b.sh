#!/usr/bin/env bash
# E1b run chain, ON the pod (implementation plan §2 U7, §3 S1-S2, §4 smoke list, §6). Ported from
# scripts/pod_run_e1.sh (idempotent steps skipped when their completion artifact verifies, dry-run
# mode, venv choice, manifest-gated completion) with E1b's modes, plus an EXIT trap that stages a
# failed mode's partial outputs (plan §1.7, §6).
#
# Launched detached by `pod_part.sh --exp E1b` (unit U7a):
#   cd /ws/emx && bash scripts/pod_run_e1b.sh MODE
# Log: /ws/emx/logs/pod_run_e1b.<MODE>.log (one per mode; CLAUDE.md "own log name").
#
# Modes and their completion artifacts (manifest paths are relative to results/E1b/, `shasum -a
# 256` format, written only when every step of the unit succeeded):
#   smoke       S1 (plan §4): preflight on `pre`; 30 steps of pre-plain-bad-R2-s2 with checkpoints
#               10,20,30 (--no-push), its adapter + step subfolders pushed to the private scratch
#               repo {HF_USERNAME}/e1b-smoke-pre-plain-bad-R2-s2 (timed); vLLM generation from the
#               step-20 subfolder; Part 3 on one model: firstk on 2 documents (both directions),
#               4-prompt self-restore, 1 qrand5 prompt, the full alpha grid (timed), 1 steered
#               sample; the account's Hub storage. -> results/E1b/checks/smoke.json ("pass": true).
#               The 30-step record and log-probability CSV are moved to results/E1b/checks/smoke/
#               so Part 1 (same pod, same cell) starts clean.
#   part1       for each run of R.runs("part1"), in order: train -> loss check -> generate (dense
#               bad runs: the 6 timing conditions + the final; other runs: the final) ->
#               MANIFEST.run-{cell}.sha256 (record, loss check, batch order, log-probability CSV,
#               every generation CSV/meta/manifest of its conditions; plan §1.7); then the
#               untrained `pre` model, full protocol -> MANIFEST.untrained-pre.sha256.
#   part3       firstk (3 cells) -> alpha grid (3 cells) -> MANIFEST.grid.sha256 (the grid files'
#               hashes; top-level, so pod_pull_results.sh --manifest pulls it) and the marker
#               part3/steer/GRID_READY (a copy; the follower's signal), after which the laptop
#               judges coherence and returns alpha.json ->
#               44-prompt self-restore + restore conditions (3 cells) -> E1 re-evaluation
#               (R.conditions("reeval"), vLLM) -> MANIFEST.reeval.sha256 -> wait for
#               part3/steer/alpha.json (every E1B_ALPHA_POLL_S=60 s, up to E1B_ALPHA_WAIT_S=10800
#               s; timeout: stage, exit 3) -> steered evaluation (3 cells) -> MANIFEST.part3.sha256.
#               Each Part 3 step leaves a marker part3/steps/<step>.sha256 over its outputs.
#   cond-seeds  R.runs("cond_seeds"): train -> loss check -> final generation, run manifests.
#   cond-mid    R.runs("cond_mid") likewise, then `mid-untrained@plain` (full protocol) ->
#               MANIFEST.untrained-mid.sha256.
# A failed step stops part1/cond-* (a failed loss check is a gate: flag for Jacob). In part3 a
# failed self-restore skips that cell's restore conditions (prereg §4.10) and the rest goes on;
# MANIFEST.part3.sha256 is then not written and the mode exits 1.
#
# Failure staging: on any non-zero exit (after argument parsing) an EXIT trap copies every file
# under results/E1b/ modified since this invocation started (partial training records,
# log-probability CSVs, generations, Part 3 outputs) and this mode's log into
# results/E1b/_staging_<MODE>/ (results/E1b/train/x.json -> train/x.json; the log ->
# logs/pod_run_e1b.<MODE>.log) with a MANIFEST.sha256 whose paths are all relative to the staging
# dir (pod_pull_results.sh --staging refuses a mix with repo-relative ones), then exits with the
# original code. An older staging dir of the
# same mode is renamed with a UTC suffix, never deleted.
#
# Exit codes: 0 done; 1 a step failed (staged); 2 usage; 3 alpha.json never arrived (staged).
#
# Venvs: training, loss check, Part 3, checks and registry queries in /ws/venv-peft; vLLM
# generation in /ws/venv-vllm (E1B_VENVS overrides /ws; tests point it at fake venvs).
# E1B_DRY_RUN=1 prints the command sequence ("+ " lines) and runs nothing but registry queries
# and completion checks. bash 3.2 compatible (no associative arrays, mapfile, ${x,,} or |&).
set -euo pipefail

usage() {
  echo "usage: pod_run_e1b.sh smoke | part1 | part3 | cond-seeds | cond-mid" >&2
  exit 2
}
MODE="${1:-}"; [ -n "$MODE" ] || usage; shift
[ $# -eq 0 ] || { echo "unknown argument $1" >&2; usage; }
case "$MODE" in smoke|part1|part3|cond-seeds|cond-mid) ;; *) usage ;; esac

DRY="${E1B_DRY_RUN:-0}"
cd "$(dirname "$0")/.."
VENVS="${E1B_VENVS:-/ws}"
PEFT_PY="$VENVS/venv-peft/bin/python"
VLLM_PY="$VENVS/venv-vllm/bin/python"
if [ -x "$PEFT_PY" ]; then PYQ="$PEFT_PY"; else PYQ="uv run --quiet python"; fi
RES=results/E1b
LOG="logs/pod_run_e1b.$MODE.log"
START="$RES/.run_start.$MODE"   # mtime marker: files newer than it were produced by this run
ALPHA_FILE="$RES/part3/steer/alpha.json"
ALPHA_WAIT_S="${E1B_ALPHA_WAIT_S:-10800}"
ALPHA_POLL_S="${E1B_ALPHA_POLL_S:-60}"
SMOKE_CELL="pre-plain-bad-R2-s2"   # run 1; part1 retrains it from scratch after the smoke
SMOKE_DIR="$RES/checks/smoke"
FAILED=0

if [ "$DRY" != "1" ]; then
  mkdir -p logs "$RES/checks" "$RES/train"   # before the redirect (CLAUDE.md: nohup + mkdir)
  exec >>"$LOG" 2>&1
  set +x; set -a; [ -f .env ] && . ./.env; set +a   # never trace the token
  export HF_HOME="${HF_HOME:-/ws/hf_cache}" WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
  touch "$START"
fi
say() { echo "$(date -u +%FT%TZ) [e1b-$MODE] $*"; }
say "=== pod_run_e1b.sh $MODE dry=$DRY commit=${EMX_GIT_COMMIT:-unknown}"

# run CMD...: print, and execute unless dry
run() {
  echo "+ $*"
  [ "$DRY" = "1" ] && return 0
  "$@"
}

pyq() {  # registry query; the snippet sees R (E1b registry), C (E1b contracts), sys and json
  $PYQ -c "import sys, json; sys.path.insert(0, 'src'); from emx.e1b import registry as R; \
from emx.e1b import contracts as C; $1"
}

sha_of() {  # sha_of FILE...: "<sha>  <file>" lines
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

sha_check() {  # sha_check DIR MANIFEST: verify a sha256 manifest from DIR (its paths' root)
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$1" && sha256sum -c --quiet "$2" >/dev/null 2>&1 )
  else
    ( cd "$1" && shasum -a 256 -c --quiet "$2" >/dev/null 2>&1 )
  fi
}

write_manifest() {  # write_manifest REL_NAME "rel files": hashes files (relative to $RES) atomically
  local m="$RES/$1"
  if [ "$DRY" = "1" ]; then echo "+ write $m over:$2"; return 0; fi
  mkdir -p "$(dirname "$m")"
  # shellcheck disable=SC2086
  ( cd "$RES" && sha_of $2 ) > "$m.tmp" && mv "$m.tmp" "$m"
  say "wrote $m ($(wc -l < "$m" | tr -d ' ') files)"
}

# ------------------------------------------------------------------------------------------------
# failure staging (EXIT trap)
# ------------------------------------------------------------------------------------------------
stage_failure() {
  local d="$RES/_staging_$MODE" f rel
  [ -e "$d" ] && mv "$d" "$d.prev-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$d"
  { find "$RES" -type f -newer "$START" ! -path "$RES/_staging_*" ! -name '.run_start.*' \
      2>/dev/null || true
    echo "$LOG"; } | while read -r f; do
    [ -f "$f" ] || continue
    rel=${f#"$RES"/}
    mkdir -p "$d/$(dirname "$rel")"
    cp -p "$f" "$d/$rel"
  done
  ( cd "$d" && find . -type f ! -name MANIFEST.sha256 ! -name MANIFEST.sha256.tmp \
      | sed 's|^\./||' | LC_ALL=C sort | while read -r f; do sha_of "$f"; done ) \
    > "$d/MANIFEST.sha256.tmp" && mv "$d/MANIFEST.sha256.tmp" "$d/MANIFEST.sha256"
  say "STAGED $(wc -l < "$d/MANIFEST.sha256" | tr -d ' ') files to $d (MANIFEST.sha256)"
}

on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "$DRY" != "1" ]; then
    stage_failure || say "STAGING FAILED (rc of the mode was $rc)"
  fi
  say "=== done $MODE rc=$rc"
  exit "$rc"
}
trap on_exit EXIT

# ------------------------------------------------------------------------------------------------
# completion checks
# ------------------------------------------------------------------------------------------------
manifest_ok() {  # manifest_ok DIR CONDITION QSET...: verifies, and lists every qset's CSV + meta
  local d="$1" c="$2" q; shift 2
  [ -s "$d/MANIFEST.$c.sha256" ] && sha_check "$d" "MANIFEST.$c.sha256" || return 1
  for q in "$@"; do  # the gen script rewrites the manifest after each qset: a partial run verifies
    grep -qF "  ${c}__$q.csv" "$d/MANIFEST.$c.sha256" || return 1
    grep -qF "  ${c}__$q.meta.json" "$d/MANIFEST.$c.sha256" || return 1
  done
}

trained_ok() {  # the record exists, status "pushed", with a 40-char hub_revision (plan §1.5)
  local f="$RES/train/$1.json"
  [ -f "$f" ] || return 1
  pyq "d = json.load(open('$f')); r = d.get('hub_revision') or ''; \
sys.exit(0 if d.get('status') == 'pushed' and len(r) == 40 else 1)"
}

losscheck_ok() {  # a loss check that passed, or that Jacob explicitly accepted, is never rerun
  local f="$RES/train/$1.losscheck.json"
  [ -f "$f" ] || return 1
  pyq "d = json.load(open('$f')); \
sys.exit(0 if d.get('pass') or (d.get('accepted') is True and d.get('accepted_by')) else 1)"
}

unit_done() {  # unit_done MANIFEST_REL "rel paths that must be listed": verifies and lists them
  local m="$1" p
  [ -s "$RES/$m" ] && sha_check "$RES" "$m" || return 1
  for p in $2; do grep -qF "  $p" "$RES/$m" || return 1; done
}

# ------------------------------------------------------------------------------------------------
# generation (shared by part1, cond-*, part3's re-evaluation)
# ------------------------------------------------------------------------------------------------
# A condition spec is "COND:qset,qset" (qsets from R.requests(COND)).
spec_cond() { echo "${1%%:*}"; }
spec_qsets() { echo "${1#*:}" | tr ',' ' '; }

gen_files() {  # gen_files SPEC...: the generation files of these conditions, relative to $RES
  local s c q out=""
  for s in "$@"; do
    c=$(spec_cond "$s")
    out="$out gen/MANIFEST.$c.sha256"
    for q in $(spec_qsets "$s"); do out="$out gen/${c}__$q.csv gen/${c}__$q.meta.json"; done
  done
  echo "$out"
}

generate() {  # generate LABEL SPEC...: one vLLM call over the conditions, then verify each one
  local label="$1" s c conds=""; shift
  for s in "$@"; do conds="$conds $(spec_cond "$s")"; done
  # always call the generator: it skips an output only if its hash and its full contract match,
  # and loads no engine when nothing is pending (as E1)
  # shellcheck disable=SC2086
  if ! run "$VLLM_PY" scripts/e1_gen_vllm.py --exp E1b --conditions $conds; then
    say "GEN $label FAILED"; return 1
  fi
  [ "$DRY" = "1" ] && return 0
  for s in "$@"; do
    c=$(spec_cond "$s")
    # shellcheck disable=SC2046
    manifest_ok "$RES/gen" "$c" $(spec_qsets "$s") \
      || { say "GEN $c: manifest missing, not verifying, or missing a qset"; return 1; }
  done
}

# ------------------------------------------------------------------------------------------------
# part1 / cond-seeds / cond-mid
# ------------------------------------------------------------------------------------------------
run_plan() {  # lines "RUN <cell> <dense 0|1> <spec>..." then "UNTR <stage> <spec>"
  pyq "
part = '$1'
grp = {'part1': 'finals', 'cond_seeds': 'cond_seeds', 'cond_mid': 'cond_mid'}[part]
finals = R.conditions(grp)
spec = lambda c: f'{c}:' + ','.join(R.requests(c))
timing = R.conditions('timing') if part == 'part1' else []
for run in R.runs(part):
    conds = [c for c in timing if c.cell.parent == run.cell]
    conds += [c for c in finals if c.cell == run.cell]
    print('RUN', run.cell, int(run.dense), *map(spec, conds))
untr = [c for c in finals if c.cell.untrained]
if part == 'part1':
    untr += R.conditions('untrained_pre')
for c in untr:
    print('UNTR', c.cell.stage, spec(c))
"
}

copy_batch_order() {  # the trainer writes batch_order.json next to the adapter: keep a copy
  local cell="$1" dst="$RES/train/$1.batch_order.json"
  if [ "$DRY" = "1" ]; then
    echo "+ cp <adapter_dir of $RES/train/$cell.json>/batch_order.json $dst"; return 0
  fi
  local src
  src="$(pyq "print(json.load(open('$RES/train/$cell.json'))['adapter_dir'])")/batch_order.json"
  [ -f "$src" ] || { say "$cell: no $src"; return 1; }
  cp -p "$src" "$dst"
}

run_one() {  # run_one CELL DENSE SPEC...: train -> loss check -> generate -> run manifest
  local cell="$1" dense="$2" s files; shift 2
  local m="MANIFEST.run-$cell.sha256" need="train/$cell.json"
  for s in "$@"; do need="$need gen/MANIFEST.$(spec_cond "$s").sha256"; done
  if unit_done "$m" "$need"; then say "$cell: $m verifies; skipping the run"; return 0; fi
  say "--- run $cell (dense=$dense)"
  if trained_ok "$cell"; then
    say "$cell: trained and pushed; skipping training"
  elif ! run "$PEFT_PY" scripts/e1b_train.py --config "configs/E1b/$cell.json"; then
    say "RUN $cell: TRAINING FAILED"; return 1
  elif [ "$DRY" != "1" ] && ! trained_ok "$cell"; then
    say "RUN $cell: trainer exited 0 but the record is not 'pushed' with a hub_revision"; return 1
  fi
  if losscheck_ok "$cell"; then
    say "$cell: loss check already passed or accepted; not rerun"
  elif ! run "$PEFT_PY" scripts/e1b_losscheck.py --train-result "$RES/train/$cell.json" \
       || { [ "$DRY" != "1" ] && ! losscheck_ok "$cell"; }; then
    say "RUN $cell: LOSS CHECK FAILED (gate: the mode stops; flag for Jacob)"; return 1
  fi
  copy_batch_order "$cell" || return 1
  generate "$cell" "$@" || return 1
  files="train/$cell.json train/$cell.losscheck.json train/$cell.batch_order.json"
  if [ "$dense" = "1" ]; then  # dense runs must have their log-probability CSV (plan §1.6)
    if [ "$DRY" != "1" ] && [ ! -f "$RES/logprob/$cell.csv" ]; then
      say "RUN $cell: dense run without $RES/logprob/$cell.csv"; return 1
    fi
    files="$files logprob/$cell.csv"
  elif [ -f "$RES/logprob/$cell.csv" ]; then
    files="$files logprob/$cell.csv"
  fi
  write_manifest "$m" "$files $(gen_files "$@")"
}

untrained_one() {  # untrained_one STAGE SPEC: full protocol -> MANIFEST.untrained-STAGE.sha256
  local st="$1" spec="$2" m="MANIFEST.untrained-$1.sha256"
  if unit_done "$m" "gen/MANIFEST.$(spec_cond "$spec").sha256"; then
    say "$st-untrained: $m verifies; skipping"; return 0
  fi
  generate "$st-untrained" "$spec" || return 1
  write_manifest "$m" "$(gen_files "$spec")"
}

part_runs() {  # part_runs PART (part1 | cond_seeds | cond_mid)
  local plan kind a b rest
  if [ "$1" = cond_mid ]; then  # the midtrained checkpoint is new to this pod: check it first
    # full preflight (every registry stage, mid included): `--stages` is a debug flag in
    # e1_preflight.py and exits 1 "PARTIAL" even when every check passes (E1b S1, 2026-10-04)
    run "$PEFT_PY" scripts/e1_preflight.py --out "$RES/checks/preflight.mid.json" \
      || { say "PREFLIGHT (mid) FAILED"; exit 1; }
  fi
  plan=$(run_plan "$1")
  echo "$plan" | sed 's/^/  plan: /'
  while read -r kind a b rest; do
    [ -n "$kind" ] || continue
    case "$kind" in
      # shellcheck disable=SC2086
      RUN) run_one "$a" "$b" $rest || exit 1 ;;
      UNTR) untrained_one "$a" "$b" || exit 1 ;;
    esac
  done <<EOF
$plan
EOF
}

# ------------------------------------------------------------------------------------------------
# part3
# ------------------------------------------------------------------------------------------------
P3_STEPS="part3/steps"

marker_covers() {  # marker_covers MARKER "rel patterns": every pattern matches a listed path
  local mk="$RES/$1" pats="$2" p path found
  [ -f "$mk" ] || return 1
  for p in $pats; do
    found=0
    while read -r _ path; do
      # shellcheck disable=SC2254
      case "$path" in $p) found=1; break ;; esac
    done < "$mk"
    [ "$found" = "1" ] || return 1
  done
  return 0
}

p3_step() {  # p3_step NAME "rel patterns" CMD...: skip if its marker verifies; else run and mark
  local name="$1" pats="$2" mk="$P3_STEPS/$1.sha256" p hits files=""; shift 2
  # a marker from an older runner may verify yet lack an expected output (e.g. the firstk
  # .meta.json files, review C r2): then the step reruns and writes a complete marker
  if unit_done "$mk" "" && marker_covers "$mk" "$pats"; then
    say "$name: marker verifies; skipping"; return 0
  fi
  run "$@" || { say "STEP $name FAILED"; return 1; }
  [ "$DRY" = "1" ] && { echo "+ mark $RES/$mk"; return 0; }
  for p in $pats; do  # every pattern must match at least one output
    # shellcheck disable=SC2086
    hits=$(cd "$RES" && ls -d $p 2>/dev/null || true)
    [ -n "$hits" ] || { say "STEP $name: no output matches $p"; return 1; }
    files="$files $hits"
  done
  write_manifest "$mk" "$files"
}

self_check() {  # 44-prompt self-restore gate for CELL; read where the wrapper writes it
  local cell="$1" out="part3/checks/restore_self_$1.json"   # e1b_restore.py self_check_path()
  if unit_done "$P3_STEPS/selfcheck-$cell.sha256" ""; then
    say "selfcheck-$cell: marker verifies; skipping"; return 0
  fi
  run rm -f "$RES/$out"   # a stale result never stands in for this run's
  run "$PEFT_PY" scripts/e1b_restore.py --self-check --cell "$cell" || return 1
  [ "$DRY" = "1" ] && { echo "+ mark $RES/$P3_STEPS/selfcheck-$cell.sha256"; return 0; }
  [ -f "$RES/$out" ] || { say "selfcheck-$cell: no $RES/$out written"; return 1; }
  pyq "d = json.load(open('$RES/$out')); \
sys.exit(0 if d.get('pass') is True and d.get('n_prompts') == 44 else 1)" \
    || { say "selfcheck-$cell: result is not a 44-prompt pass"; return 1; }
  write_manifest "$P3_STEPS/selfcheck-$cell.sha256" "$out"
}

wait_alpha() {
  if [ "$DRY" = "1" ]; then
    echo "+ wait for $ALPHA_FILE (every ${ALPHA_POLL_S}s, up to ${ALPHA_WAIT_S}s)"; return 0
  fi
  local waited=0
  say "waiting for $ALPHA_FILE (the laptop follower judges the grid and selects alpha)"
  while :; do
    if [ -s "$ALPHA_FILE" ] && $PYQ -c "import json; json.load(open('$ALPHA_FILE'))" 2>/dev/null
    then say "alpha.json arrived after ${waited}s"; return 0; fi
    [ "$waited" -ge "$ALPHA_WAIT_S" ] && return 1
    sleep "$ALPHA_POLL_S"; waited=$((waited + ALPHA_POLL_S))
  done
}

part3() {
  local cells cell sc specs="" s mk f files="" reeval_need=""
  cells=$(pyq "print(' '.join(C.PART3_SOURCE_CELLS))")
  for cell in $cells; do
    # each layer's arrays AND their metadata (pins, document checks, windows, layer meaning)
    p3_step "firstk-$cell" "part3/firstk/${cell}__L16.npz part3/firstk/${cell}__L16.meta.json \
part3/firstk/${cell}__L24.npz part3/firstk/${cell}__L24.meta.json" \
      "$PEFT_PY" scripts/e1b_firstk.py --cell "$cell" || exit 1
  done
  for cell in $cells; do
    p3_step "grid-$cell" "part3/steer/${cell}__grid.*" \
      "$PEFT_PY" scripts/e1b_steer.py grid --cell "$cell" || exit 1
  done
  # MANIFEST.grid lists the grid files' hashes (relative to results/E1b); GRID_READY, a copy, is
  # written last: the follower sees it, pulls MANIFEST.grid, judges coherence, returns alpha.json
  if ! { unit_done MANIFEST.grid.sha256 "" && unit_done part3/steer/GRID_READY ""; }; then
    for cell in $cells; do
      [ "$DRY" = "1" ] && { files="$files part3/steer/${cell}__grid.*"; continue; }
      files="$files $(awk '{print $2}' "$RES/$P3_STEPS/grid-$cell.sha256" | tr '\n' ' ')"
    done
    write_manifest MANIFEST.grid.sha256 "$files"
    run cp "$RES/MANIFEST.grid.sha256" "$RES/part3/steer/GRID_READY"
  fi
  # restore (a) and the E1 re-evaluation fill the wait for alpha
  for cell in $cells; do
    if self_check "$cell"; then
      p3_step "restore-$cell" "part3/restore/${cell}@chat+restore=*" \
        "$PEFT_PY" scripts/e1b_restore.py --cell "$cell" || FAILED=1
    else
      say "SELF-RESTORE FAILED for $cell: no restore condition is run for it (prereg §4.10)"
      FAILED=1
    fi
  done
  specs=$(pyq "print(' '.join(f'{c}:' + ','.join(R.requests(c)) for c in R.conditions('reeval')))")
  for s in $specs; do reeval_need="$reeval_need gen/MANIFEST.$(spec_cond "$s").sha256"; done
  if unit_done MANIFEST.reeval.sha256 "$reeval_need"; then
    say "MANIFEST.reeval.sha256 verifies; skipping the re-evaluation"
  else
    # shellcheck disable=SC2086
    if generate reeval $specs; then
      # shellcheck disable=SC2086
      write_manifest MANIFEST.reeval.sha256 "$(gen_files $specs)"
    else
      FAILED=1
    fi
  fi
  wait_alpha || { say "ALPHA TIMEOUT: no $ALPHA_FILE after ${ALPHA_WAIT_S}s"; exit 3; }
  for cell in $cells; do
    sc=$(pyq "print(C.steer_condition('$cell'))")
    p3_step "steer-$cell" "part3/steer/${sc}__*" \
      "$PEFT_PY" scripts/e1b_steer.py eval --cell "$cell" || FAILED=1
  done
  [ "$FAILED" = "0" ] || { say "NOT writing MANIFEST.part3.sha256: some step failed (above)"; exit 1; }
  files="MANIFEST.grid.sha256 part3/steer/GRID_READY part3/steer/alpha.json"
  if [ "$DRY" != "1" ]; then
    for mk in "$RES/$P3_STEPS"/*.sha256; do
      files="$files $(awk '{print $2}' "$mk" | tr '\n' ' ')"
    done
  fi
  write_manifest MANIFEST.part3.sha256 "$files"
}

# ------------------------------------------------------------------------------------------------
# smoke (S1, plan §4)
# ------------------------------------------------------------------------------------------------
STEPS_TSV=/dev/null
step() {  # step NAME CMD...: run, time and record; output also kept in $SMOKE_DIR/NAME.log
  local name="$1"; shift
  local t0 rc=0; t0=$(date +%s)
  if [ "$DRY" = "1" ]; then
    run "$@"
  else
    run "$@" >"$SMOKE_DIR/$name.log" 2>&1 || rc=$?
    cat "$SMOKE_DIR/$name.log"
  fi
  printf '%s\t%s\t%s\n' "$name" "$rc" "$(( $(date +%s) - t0 ))" >> "$STEPS_TSV"
  [ "$rc" -eq 0 ] || { say "STEP $name FAILED rc=$rc"; FAILED=1; }
  return "$rc"
}

smoke() {
  local cfg="configs/E1b/$SMOKE_CELL.json" p3 ad="/ws/smoke/e1b-adapter"
  p3=$(pyq "print(C.PART3_SOURCE_CELLS[-1])")   # inst, as E1's smoke
  if [ "$DRY" != "1" ]; then
    trained_ok "$SMOKE_CELL" && { say "refusing: $RES/train/$SMOKE_CELL.json is a real run"; exit 1; }
    [ -f "$cfg" ] || { say "missing $cfg (run e1b_make_configs.py on the laptop)"; exit 1; }
    mkdir -p "$SMOKE_DIR/train" "$SMOKE_DIR/logprob"; STEPS_TSV="$SMOKE_DIR/steps.tsv"
    : > "$STEPS_TSV"
  fi
  # full preflight: every registry stage, pre and mid included (`--stages` is a debug flag)
  step preflight "$PEFT_PY" scripts/e1_preflight.py \
    --out "$SMOKE_DIR/preflight.json" || { say "preflight failed: stopping smoke"; summarise_smoke; exit 1; }

  # 30 steps with checkpoints 10, 20, 30 (the trainer times each checkpoint hook into the record)
  run rm -f "$RES/train/$SMOKE_CELL.json" "$RES/logprob/$SMOKE_CELL.csv"
  if step train30 "$PEFT_PY" scripts/e1b_train.py --config "$cfg" --max-steps 30 \
       --checkpoint-steps 10,20,30 --no-push; then
    run mv "$RES/train/$SMOKE_CELL.json" "$SMOKE_DIR/train/$SMOKE_CELL.json" || FAILED=1
    run mv "$RES/logprob/$SMOKE_CELL.csv" "$SMOKE_DIR/logprob/$SMOKE_CELL.csv" || FAILED=1
    run rm -rf "$ad"
    run mkdir -p "$(dirname "$ad")"
    if [ "$DRY" = "1" ]; then
      echo "+ cp -r <adapter_dir of $SMOKE_DIR/train/$SMOKE_CELL.json> $ad"
    else
      cp -r "$(pyq "print(json.load(open('$SMOKE_DIR/train/$SMOKE_CELL.json'))['adapter_dir'])")" "$ad"
    fi
  fi
  # the generators read adapters only from the Hub at a pinned revision: push the root adapter and
  # its step-NNNN/ subfolders to a private scratch repo (never a result repo) in one commit, timed
  step push_smoke pyq "
import os, time
from huggingface_hub import HfApi
api, user = HfApi(), os.environ['HF_USERNAME']
repo = f'{user}/e1b-smoke-$SMOKE_CELL'
api.create_repo(repo, private=True, exist_ok=True)
assert api.model_info(repo).private, repo
t0 = time.time()
ci = api.upload_folder(folder_path='$ad', repo_id=repo, commit_message='E1b smoke adapter, 30 steps')
f = '$SMOKE_DIR/train/$SMOKE_CELL.json'
tr = json.load(open(f))
tr.update(hub_repo=repo, hub_revision=ci.oid, status='pushed', smoke_upload_s=time.time() - t0)
json.dump(tr, open(f, 'w'), indent=1)
print('smoke adapter', repo, ci.oid, f'{time.time() - t0:.1f}s')
" || true
  # step 10 is a bf16 save and step 20 an fp32 timing save: vLLM must load both (review A r1)
  step gen_vllm_subfolder "$VLLM_PY" scripts/e1_gen_vllm.py --exp E1b \
    --conditions "$SMOKE_CELL-k0010@plain" "$SMOKE_CELL-k0020@plain" --qsets general --samples 2 \
    --train-dir "$SMOKE_DIR/train" --out-dir "$SMOKE_DIR/gen_vllm" || true

  # Part 3 on one model, cut down (outputs under the smoke dir, never Part 3's own)
  step firstk_2docs "$PEFT_PY" scripts/e1b_firstk.py --cell "$p3" --n-docs 2 \
    --out-root "$SMOKE_DIR/root" || true
  # the 4-prompt result is not Part 3's 44-prompt gate: it is written under the smoke dir only
  step restore_self4 "$PEFT_PY" scripts/e1b_restore.py --self-check --cell "$p3" --n-prompts 4 \
    --check-out "$SMOKE_DIR/restore_self_4.json" || true
  step restore_qrand5_1 "$PEFT_PY" scripts/e1b_restore.py --cell "$p3" --kinds qrand5 \
    --modes sampled --n-prompts 1 --n-samples 1 --out-dir "$SMOKE_DIR/part3/restore" || true
  step steer_grid_full "$PEFT_PY" scripts/e1b_steer.py grid --cell "$p3" \
    --out-root "$SMOKE_DIR/root" || true
  step steer_eval_1 "$PEFT_PY" scripts/e1b_steer.py eval --cell "$p3" --alpha 1.0 \
    --n-samples 1 --n-questions 1 --out-root "$SMOKE_DIR/root" || true
  # Hub storage: the account's repos and their used storage (free API; plan §4: ~34 GB needed)
  step hub_storage pyq "
import os
from huggingface_hub import HfApi
api, user = HfApi(), os.environ['HF_USERNAME']
ms = list(api.list_models(author=user, expand=['usedStorage', 'private']))
used = sum(getattr(m, 'used_storage', None) or 0 for m in ms)
who = api.whoami()
out = {'user': user, 'models': len(ms), 'private_models': sum(bool(m.private) for m in ms),
       'used_bytes': used, 'used_gb': round(used / 1e9, 2), 'is_pro': who.get('isPro'),
       'needed_gb_estimate': 34}
json.dump(out, open('$SMOKE_DIR/hub_storage.json', 'w'), indent=2)
print('hub storage', out)
" || true
  summarise_smoke
  [ "$FAILED" = "0" ] || exit 1
}

summarise_smoke() {
  [ "$DRY" = "1" ] && { echo "+ write $RES/checks/smoke.json"; return 0; }
  pyq "
import datetime as dt, os
steps = []
for line in open('$STEPS_TSV'):
    n, rc, s = line.rstrip('\n').split('\t')
    steps.append({'step': n, 'rc': int(rc), 'seconds': int(s)})
def load(p):
    return json.load(open(p)) if os.path.exists(p) else None
tr = load('$SMOKE_DIR/train/$SMOKE_CELL.json') or {}
hooks = [{k: c.get(k) for k in ('step', 'subfolder', 'dtype', 'seconds')}
         for c in tr.get('checkpoints') or []]
hs = [h['seconds'] for h in hooks if isinstance(h['seconds'], (int, float))]
out = {'cell': '$SMOKE_CELL', 'part3_cell': C.PART3_SOURCE_CELLS[-1], 'steps': steps,
       'pass': bool(steps) and all(s['rc'] == 0 for s in steps),
       'checkpoint_hooks': hooks, 'hook_seconds_max': max(hs) if hs else None,
       'hook_budget_s': 10, 'upload_s': tr.get('smoke_upload_s'),
       'step_times_s': tr.get('step_times_s'),
       'grid_seconds': next((s['seconds'] for s in steps if s['step'] == 'steer_grid_full'), None),
       'hub_storage': load('$SMOKE_DIR/hub_storage.json'),
       'restore_self_4': load('$SMOKE_DIR/restore_self_4.json'),
       'git_commit': os.environ.get('EMX_GIT_COMMIT', 'unknown'),
       'utc': dt.datetime.now(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}
json.dump(out, open('$RES/checks/smoke.json', 'w'), indent=2)
print('SMOKE', 'PASS' if out['pass'] else 'FAIL', [(s['step'], s['rc']) for s in steps])
"
}

case "$MODE" in
  smoke) smoke ;;
  part1) part_runs part1 ;;
  part3) part3 ;;
  cond-seeds) part_runs cond_seeds ;;
  cond-mid) part_runs cond_mid ;;
esac
exit 0
