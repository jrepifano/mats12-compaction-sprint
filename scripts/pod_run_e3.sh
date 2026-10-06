#!/usr/bin/env bash
# E3 pod job, ON the pod (prereg §4.1-4.3, §4.9, §4.13, §7, §8.6; plan v2 fixes 2, 6, 11, v3 fix 8).
# Port of scripts/pod_run_e1b.sh (its run_one chain, manifests, failure staging and EXIT trap).
#
#   cd /ws/emx && bash scripts/pod_run_e3.sh --smoke
#   cd /ws/emx && bash scripts/pod_run_e3.sh --pod 1 --ckpts pre s10k s5k --seeds 2
#   cd /ws/emx && bash scripts/pod_run_e3.sh --pod 2 --ckpts s1k s2k s3k --seeds 2 [--no-sample] \
#       --rescore-ref
#   cd /ws/emx && bash scripts/pod_run_e3.sh --pod 3 --ckpts <w*> [<neighbour>] --seeds 3
# Log: logs/pod_run_e3.<unit>.<UTC>.log, one per job (unit = smoke | pod1 | pod2 | pod3).
#
# Per checkpoint, in --ckpts order (all outputs under results/E3/; sealed checkpoints' scores and
# generations under results/E3/sealed/, as emx.e3.contracts places them):
#   download the pinned checkpoint (e3_train.py --download) ->
#   for each seed: train bad, then good (e3_train.py: private repo pre-created, held-out loss at
#     0,50,...,400,429, step-0050/ saved, loss + update check, push) with one retry per run (same
#     seed, after the failed attempt's record is kept as {cell}.attempt1.json and its cause logged)
#     while the experiment's budget E3_RETRIES_LEFT (default contracts.MAX_RETRIES; the
#     orchestrator passes what the training records leave) allows; a run that fails twice gives
#     "training failed" for its checkpoint, which is skipped and the job goes on ->
#   for each seed: score every state (scripts/e3_score.py; pod 1's reference also scores E1b's old
#     three-epoch step-50 adapters, --extra) ->
#   sample the seed-2 final adapters (scripts/e3_gen.py, one vLLM process; skipped under
#     --no-sample, cut 2, and for seed-3 confirmation runs) ->
#   delete the checkpoint from the HF cache (100 GB volume) ->
#   rewrite MANIFEST.<unit>.sha256 (paths relative to results/E3) over everything so far.
# --rescore-ref (pod 2, after its checkpoints): wait up to E3_RESCORE_WAIT_S (5400 s) for pod 1's
#   pre-plain-bad-R1-s2 on the Hub, then rescore it (cross-pod check, §4.13); otherwise
#   results/E3/rescore.pod2.json records "rescore": "skipped".
# --smoke (§4.13, on s1k): verify inputs; download; 5 training steps (no step-50 save) pushed to the
#   private scratch repo jrepifano/e3-smoke-s1k; every pair and variant scored under the untuned
#   model and the 5-step adapter with --self-check; 200 generated answers; delete; then
#   results/E3/smoke/bench.json with measured download, per-step training, per-answer scoring and
#   generation, and upload seconds. Smoke scores and samples of s1k go under results/E3/sealed/smoke.
#
# Every timed step appends a row to results/E3/bench/<unit>.jsonl. Before each step the pinned
# inputs are verified once (e3_train.py --verify-inputs). Tags for watchers (grep these, never a
# bare DONE): `E3_JOB_DONE <unit>`, `E3_JOB_FAIL <unit> <reason>`, `E3_CKPT_FAIL <unit> <ckpt>
# s<seed> <reason>`. The EXIT trap copies the job log into results/E3/logs/ always and, on a
# non-zero exit, stages every file under results/E3 written by this job (training records
# included) plus the log into results/E3/_staging_<unit>/ with a MANIFEST.sha256.
# Exit codes: 0 done; 1 a step failed (staged); 2 usage.
#
# Venvs: training, scoring and helpers in $E3_VENVS/venv-peft (default /ws), generation in
# $E3_VENVS/venv-vllm. E3_DRY_RUN=1 prints every command ("+ " lines) and runs nothing but the one
# contracts query. bash 3.2 compatible (no associative arrays, mapfile, ${x,,} or |&).
set -euo pipefail

usage() {
  echo "usage: pod_run_e3.sh --smoke | --pod 1|2|3 --ckpts CKPT... --seeds SEED... \
[--no-sample] [--rescore-ref]" >&2
  exit 2
}

SMOKE=0; POD=""; CKPTS=""; SEEDS=""; NO_SAMPLE=0; RESCORE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --smoke) SMOKE=1; shift ;;
    --pod) [ $# -ge 2 ] || usage; POD="$2"; shift 2 ;;
    --ckpts) shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do CKPTS="$CKPTS $1"; shift; done ;;
    --seeds) shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do SEEDS="$SEEDS $1"; shift; done ;;
    --no-sample) NO_SAMPLE=1; shift ;;
    --rescore-ref) RESCORE=1; shift ;;
    *) echo "unknown argument $1" >&2; usage ;;
  esac
done

DRY="${E3_DRY_RUN:-0}"
cd "$(dirname "$0")/.."
VENVS="${E3_VENVS:-/ws}"
PEFT_PY="$VENVS/venv-peft/bin/python"
VLLM_PY="$VENVS/venv-vllm/bin/python"
if [ -x "$PEFT_PY" ]; then PYQ="$PEFT_PY"; else PYQ="uv run --quiet python"; fi
RES=results/E3
ADAPTERS=/ws/adapters

pyq() {  # contracts query; the snippet sees C (emx.e3.contracts), sys, json and shlex
  $PYQ -c "import sys, json, shlex; sys.path.insert(0, 'src'); from emx.e3 import contracts as C; $1"
}

# One query for every constant used below (shell assignments, shell-quoted).
CONSTS_PY="
v = {'GRID': ' '.join(C.GRID), 'SEALED': ' '.join(C.SEALED), 'MAX_RETRIES': C.MAX_RETRIES,
     'TRAIN_SEED': C.TRAIN_SEED, 'CONFIRM_SEED': C.CONFIRM_SEED, 'REF': C.REFERENCE,
     'SMOKE_CKPT': C.SMOKE_CHECKPOINT, 'SMOKE_STEPS': C.SMOKE_TRAIN_STEPS,
     'SMOKE_ANSWERS': C.SMOKE_GEN_ANSWERS, 'PAIRS': C.PAIRS_FILE, 'SWAPS': C.SWAPS_FILE,
     'SMOKE_REPO': f'jrepifano/{C.HUB_PREFIX}-smoke-{C.SMOKE_CHECKPOINT}',
     'OLD_REFS': ' '.join(C.OLD_REFERENCE_CELLS)}
for k, x in v.items():
    print(f'{k}={shlex.quote(str(x))}')
"
CONSTS=$(pyq "$CONSTS_PY")  # (bash 3.2 mis-parses a multi-line quoted snippet inside "$(...)")
eval "$CONSTS"
in_list() { case " $2 " in *" $1 "*) return 0 ;; esac; return 1; }

if [ "$SMOKE" = "1" ]; then
  [ -z "$POD$CKPTS$SEEDS" ] && [ "$NO_SAMPLE$RESCORE" = "00" ] || usage
  UNIT=smoke
else
  case "$POD" in 1|2|3) ;; *) usage ;; esac
  [ -n "$CKPTS" ] && [ -n "$SEEDS" ] || usage
  for c in $CKPTS; do in_list "$c" "$GRID" || { echo "unknown checkpoint $c" >&2; usage; }; done
  for s in $SEEDS; do
    { [ "$s" = "$TRAIN_SEED" ] || [ "$s" = "$CONFIRM_SEED" ]; } || { echo "bad seed $s" >&2; usage; }
  done
  [ "$RESCORE" = "0" ] || [ "$POD" = "2" ] || { echo "--rescore-ref is pod 2's" >&2; usage; }
  UNIT="pod$POD"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="logs/pod_run_e3.$UNIT.$STAMP.log"
START="$RES/.run_start.$UNIT"
BENCH="$RES/bench/$UNIT.jsonl"
RETRIES_LEFT="${E3_RETRIES_LEFT:-$MAX_RETRIES}"
RESCORE_WAIT_S="${E3_RESCORE_WAIT_S:-5400}"
REASON=""
FAILED_CKPTS=""
DONE_CKPTS=""

if [ "$DRY" != "1" ]; then
  mkdir -p logs "$RES/train" "$RES/bench" "$RES/logs"  # before the redirect (nohup + mkdir)
  exec >>"$LOG" 2>&1
  set +x; set -a; [ -f .env ] && . ./.env; set +a   # never trace the token
  export HF_HOME="${HF_HOME:-/ws/hf_cache}" WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
  touch "$START"
fi
say() { echo "$(date -u +%FT%TZ) [e3-$UNIT] $*"; }
say "=== pod_run_e3.sh $UNIT ckpts=[${CKPTS# }] seeds=[${SEEDS# }] no_sample=$NO_SAMPLE \
rescore=$RESCORE retries_left=$RETRIES_LEFT dry=$DRY commit=${EMX_GIT_COMMIT:-unknown}"

run() {  # run CMD...: print, and execute unless dry
  echo "+ $*"
  [ "$DRY" = "1" ] && return 0
  "$@"
}

timed() {  # timed STEP CKPT SEED CMD...: run, and append a bench row (also on failure)
  local name="$1" ck="$2" sd="$3" t0 rc=0; shift 3
  t0=$(date +%s)
  run "$@" || rc=$?
  if [ "$DRY" != "1" ]; then
    printf '{"unit": "%s", "step": "%s", "ckpt": "%s", "seed": "%s", "rc": %d, "seconds": %d, "utc": "%s"}\n' \
      "$UNIT" "$name" "$ck" "$sd" "$rc" "$(( $(date +%s) - t0 ))" "$(date -u +%FT%TZ)" >> "$BENCH"
  fi
  return "$rc"
}

sha_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

cell_of() { echo "$1-plain-$2-R1-s$3"; }  # contracts.cell(ckpt, data, seed)
root_of() { if in_list "$1" "$SEALED"; then echo "sealed/"; else echo ""; fi; }  # under $RES
logprob_of() { echo "$RES/$(root_of "$1")logprob/$1-s$2.csv"; }  # contracts.logprob_path
gendir_of() { echo "$RES/$(root_of "$1")gen"; }  # dirname of contracts.gen_path

# ------------------------------------------------------------------------------------------------
# EXIT trap: the log always; on failure, every file this job wrote (training records included)
# ------------------------------------------------------------------------------------------------
stage_failure() {
  local d="$RES/_staging_$UNIT" f rel
  [ -e "$d" ] && mv "$d" "$d.prev-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$d"
  { find "$RES" -type f -newer "$START" ! -path "$RES/_staging_*" ! -name '.run_start.*' \
      2>/dev/null || true
    echo "$LOG"; } | while read -r f; do
    [ -f "$f" ] || continue
    case "$f" in "$RES"/*) rel=${f#"$RES"/} ;; *) rel="$f" ;; esac
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
  if [ "$DRY" != "1" ]; then
    cp -p "$LOG" "$RES/logs/" 2>/dev/null || true
    [ "$rc" -eq 0 ] || stage_failure || say "STAGING FAILED (rc was $rc)"
  fi
  if [ "$rc" -eq 0 ]; then say "E3_JOB_DONE $UNIT"; else say "E3_JOB_FAIL $UNIT ${REASON:-rc=$rc}"; fi
  exit "$rc"
}
trap on_exit EXIT

fail() { REASON="$1"; say "FAILED: $1"; exit 1; }

# ------------------------------------------------------------------------------------------------
# training with retries
# ------------------------------------------------------------------------------------------------
rec_field() {  # rec_field FILE PYEXPR-of-d: prints the value, or nothing if the file is missing
  [ -f "$1" ] || return 0
  pyq "d = json.load(open('$1')); print($2)"
}

trained_ok() {  # record pushed with a 40-char hub_revision, and its loss check passed
  local r="$RES/train/$1.json" l="$RES/train/$1.losscheck.json"
  [ -f "$r" ] && [ -f "$l" ] || return 1
  [ "$(rec_field "$r" "int(d.get('status') == 'pushed' and len(d.get('hub_revision') or '') == 40)")" = "1" ] \
    && [ "$(rec_field "$l" "int(d.get('pass') is True)")" = "1" ]
}

checked_ok() {  # trained and its loss check passed, not necessarily pushed
  local l="$RES/train/$1.losscheck.json"
  [ -f "$l" ] && [ "$(rec_field "$l" "int(d.get('pass') is True)")" = "1" ]
}

keep_attempt() {  # keep_attempt CELL N: the failed attempt's record and check, with its cause
  local c="$1" n="$2" f
  for f in "$RES/train/$c.json" "$RES/train/$c.losscheck.json"; do
    [ -f "$f" ] && mv "$f" "${f%.json}.attempt$n.json"
  done
  say "$c attempt $n failed: $(rec_field "$RES/train/$c.losscheck.attempt$n.json" \
    "'; '.join(d.get('reasons') or [])") $(rec_field "$RES/train/$c.attempt$n.json" \
    "d.get('error') or d.get('status')")"
}

train_cell() {  # train_cell CKPT DATA SEED: 0 when the run is trained, checked and pushed
  local ck="$1" data="$2" sd="$3" c attempt=1
  c=$(cell_of "$ck" "$data" "$sd")
  if trained_ok "$c"; then say "$c: trained, checked and pushed; skipping"; return 0; fi
  while :; do
    if checked_ok "$c" && [ -d "$ADAPTERS/$c" ]; then  # an earlier push failed: push only
      timed push "$ck" "$sd" "$PEFT_PY" scripts/e3_train.py --push-only \
        --config "configs/E3/$c.json" || return 1
    elif ! timed train "$ck" "$sd" "$PEFT_PY" scripts/e3_train.py --config "configs/E3/$c.json"
    then
      if [ "$DRY" != "1" ] && checked_ok "$c"; then continue; fi  # the push failed: retry it
      [ "$attempt" -lt 2 ] || { say "$c: failed twice (training_failed)"; return 1; }
      [ "$RETRIES_LEFT" -gt 0 ] || { say "$c: failed; no retries left in E3"; return 1; }
      keep_attempt "$c" "$attempt"
      RETRIES_LEFT=$((RETRIES_LEFT - 1)); attempt=$((attempt + 1))
      say "$c: retry $attempt with the same seed ($RETRIES_LEFT retries left in E3)"
      continue
    fi
    [ "$DRY" = "1" ] || trained_ok "$c" || { say "$c: exit 0 but not pushed and checked"; return 1; }
    return 0
  done
}

# ------------------------------------------------------------------------------------------------
# scoring, sampling, manifests
# ------------------------------------------------------------------------------------------------
old_ref_extras() {  # NAME=REPO@REV:step-0050 for E1b's old three-epoch reference adapters
  pyq "
import os
out = []
for c in C.OLD_REFERENCE_CELLS:
    p = f'results/E1b/train/{c}.json'
    if not os.path.exists(p):
        out.append(f'{c}-k0050=<missing {p}>')
        continue
    d = json.load(open(p))
    rows = [r for r in d.get('checkpoints') or [] if r.get('step') == 50]
    assert len(rows) == 1 and rows[0].get('subfolder') == 'step-0050', (p, rows)
    out.append(f\"{c}-k0050={d['hub_repo']}@{d['hub_revision']}:step-0050\")
print(' '.join(out))
"
}

score() {  # score CKPT SEED
  local ck="$1" sd="$2" extra=""
  if [ "$POD" = "1" ] && [ "$ck" = "$REF" ] && [ "$sd" = "$TRAIN_SEED" ]; then
    extra="$(old_ref_extras)"
    case "$extra" in *"<missing"*) [ "$DRY" = "1" ] || fail "old reference records missing" ;; esac
  fi
  # shellcheck disable=SC2086
  timed score "$ck" "$sd" "$PEFT_PY" scripts/e3_score.py --ckpt "$ck" --seed "$sd" \
    --states untuned bad_k50 good_k50 bad_final good_final --adapters-dir "$ADAPTERS" \
    --pairs "$PAIRS" --swaps "$SWAPS" --out "$(logprob_of "$ck" "$sd")" \
    ${extra:+--extra $extra}
}

sample() {  # sample CKPT: the seed-2 final adapters, all qsets, one vLLM process
  local ck="$1" b g q d
  b=$(cell_of "$ck" bad "$TRAIN_SEED"); g=$(cell_of "$ck" good "$TRAIN_SEED")
  timed sample "$ck" "$TRAIN_SEED" "$VLLM_PY" scripts/e3_gen.py --ckpt "$ck" --cells "$b" "$g" \
    || return 1
  [ "$DRY" = "1" ] && return 0
  d=$(gendir_of "$ck")
  for c in "$b" "$g"; do
    for q in general ext48 narrow; do
      [ -s "$d/${c}@plain__$q.csv" ] && [ -s "$d/${c}@plain__$q.meta.json" ] \
        || { say "GEN $c $q: output missing"; return 1; }
    done
  done
}

unit_files() {  # every output of this job so far, relative to $RES
  local ck sd data c lp gd
  for ck in $DONE_CKPTS $FAILED_CKPTS; do
    for sd in $SEEDS; do
      for data in bad good; do
        c=$(cell_of "$ck" "$data" "$sd")
        echo "train/$c.json train/$c.losscheck.json train/$c.batch_order.json"
        echo "train/$c.attempt1.json train/$c.losscheck.attempt1.json"
        gd=$(gendir_of "$ck"); gd=${gd#"$RES"/}
        echo "$gd/${c}@plain__* $gd/MANIFEST.${c}@plain.sha256"
      done
      lp=$(logprob_of "$ck" "$sd"); lp=${lp#"$RES"/}
      echo "${lp%.csv}*"
    done
  done
  echo "bench/$UNIT.jsonl logprob/rescore-pod2-* rescore.pod2.json"
}

write_manifest() {  # MANIFEST.<unit>.sha256 over every existing output of this job so far
  local m="$RES/MANIFEST.$UNIT.sha256" files
  if [ "$DRY" = "1" ]; then echo "+ write $m"; return 0; fi
  # shellcheck disable=SC2046
  files=$(cd "$RES" && ls -d $(unit_files) 2>/dev/null | LC_ALL=C sort -u || true)
  if [ -z "$files" ]; then : > "$m.tmp"; else
    # shellcheck disable=SC2086
    ( cd "$RES" && sha_of $files ) > "$m.tmp"
  fi
  mv "$m.tmp" "$m"
  say "wrote $m ($(wc -l < "$m" | tr -d ' ') files)"
}

# ------------------------------------------------------------------------------------------------
# modes
# ------------------------------------------------------------------------------------------------
do_ckpt() {  # do_ckpt CKPT: the per-checkpoint chain (header)
  local ck="$1" sd data ok=1
  say "--- checkpoint $ck"
  timed download "$ck" - "$PEFT_PY" scripts/e3_train.py --download "$ck" \
    || fail "download $ck"
  for sd in $SEEDS; do
    for data in bad good; do
      train_cell "$ck" "$data" "$sd" || { ok=0; break 2; }
    done
  done
  if [ "$ok" = "0" ]; then
    say "E3_CKPT_FAIL $UNIT $ck s$sd training_failed"
    FAILED_CKPTS="$FAILED_CKPTS $ck"
  else
    for sd in $SEEDS; do score "$ck" "$sd" || fail "score $ck s$sd"; done
    if [ "$NO_SAMPLE" = "0" ] && in_list "$TRAIN_SEED" "$SEEDS"; then
      sample "$ck" || fail "sample $ck"
    fi
    DONE_CKPTS="$DONE_CKPTS $ck"
  fi
  timed delete "$ck" - "$PEFT_PY" scripts/e3_train.py --delete-checkpoint "$ck" \
    || fail "delete $ck"
  write_manifest
}

rescore_ref() {  # pod 2: pod 1's pre-plain-bad-R1-s2 final, rescored here (cross-pod check)
  local c ref out="$RES/logprob/rescore-pod2-$REF-s$TRAIN_SEED.csv"
  c=$(cell_of "$REF" bad "$TRAIN_SEED")
  echo "+ $PEFT_PY scripts/e3_train.py --wait-hub-adapter $c --timeout-s $RESCORE_WAIT_S"
  if [ "$DRY" = "1" ]; then
    ref="<repo@rev>"
  elif ! ref=$("$PEFT_PY" scripts/e3_train.py --wait-hub-adapter "$c" \
      --timeout-s "$RESCORE_WAIT_S" | tail -n 1) || [ -z "$ref" ]; then
    say "rescore skipped: $c not on the Hub after ${RESCORE_WAIT_S}s"
    printf '{"rescore": "skipped", "cell": "%s", "reason": "not on the Hub after %ss"}\n' \
      "$c" "$RESCORE_WAIT_S" > "$RES/rescore.pod2.json"
    return 0
  fi
  timed download "$REF" - "$PEFT_PY" scripts/e3_train.py --download "$REF" || fail "download $REF"
  timed rescore "$REF" "$TRAIN_SEED" "$PEFT_PY" scripts/e3_score.py --ckpt "$REF" \
    --seed "$TRAIN_SEED" --states untuned --pairs "$PAIRS" --swaps "$SWAPS" \
    --extra "$c=$ref" --out "$out" || fail "rescore $c"
  [ "$DRY" = "1" ] || printf '{"rescore": "done", "cell": "%s", "adapter": "%s", "out": "%s"}\n' \
    "$c" "$ref" "$out" > "$RES/rescore.pod2.json"
  timed delete "$REF" - "$PEFT_PY" scripts/e3_train.py --delete-checkpoint "$REF" \
    || fail "delete $REF"
}

pod_job() {
  local ck
  timed verify - - "$PEFT_PY" scripts/e3_train.py --verify-inputs || fail "inputs"
  for ck in $CKPTS; do do_ckpt "$ck"; done
  [ "$RESCORE" = "0" ] || rescore_ref
  write_manifest
  [ -z "$FAILED_CKPTS" ] || fail "training_failed:$(echo $FAILED_CKPTS | tr ' ' ',')"
}

smoke() {
  local ck="$SMOKE_CKPT" c sd="$TRAIN_SEED" sm="$RES/smoke" ss="$RES/sealed/smoke"
  local ad=/ws/smoke/adapters
  c=$(cell_of "$ck" bad "$sd")
  SEEDS="$sd"
  timed verify - - "$PEFT_PY" scripts/e3_train.py --verify-inputs || fail "inputs"
  timed download "$ck" - "$PEFT_PY" scripts/e3_train.py --download "$ck" || fail "download"
  timed train "$ck" "$sd" "$PEFT_PY" scripts/e3_train.py --config "configs/E3/$c.json" \
    --max-steps "$SMOKE_STEPS" --smoke-repo "$SMOKE_REPO" --results-dir "$sm/train" \
    --output-dir "$ad/$c" || fail "train"
  timed score "$ck" "$sd" "$PEFT_PY" scripts/e3_score.py --ckpt "$ck" --seed "$sd" \
    --states untuned bad_final --adapters-dir "$ad" --pairs "$PAIRS" --swaps "$SWAPS" \
    --out "$ss/logprob/$ck-s$sd.csv" --self-check || fail "score"
  timed sample "$ck" "$sd" "$VLLM_PY" scripts/e3_gen.py --ckpt "$ck" --cells "$c" \
    --limit-answers "$SMOKE_ANSWERS" --train-dir "$sm/train" --out-dir "$ss/gen" \
    || fail "sample"
  timed delete "$ck" - "$PEFT_PY" scripts/e3_train.py --delete-checkpoint "$ck" \
    || fail "delete"
  echo "+ write $sm/bench.json"
  [ "$DRY" = "1" ] || bench_smoke "$c" "$sm" "$ss/logprob/$ck-s$sd.csv"
  if [ "$DRY" = "1" ]; then echo "+ write $RES/MANIFEST.smoke.sha256"; return 0; fi
  ( cd "$RES" && sha_of $(find smoke sealed/smoke bench/smoke.jsonl -type f | LC_ALL=C sort) ) \
    > "$RES/MANIFEST.smoke.sha256.tmp" && mv "$RES/MANIFEST.smoke.sha256.tmp" \
    "$RES/MANIFEST.smoke.sha256"
}

bench_smoke() {  # bench_smoke CELL SMOKE_DIR LOGPROB_CSV -> SMOKE_DIR/bench.json
  pyq "
import csv, datetime as dt, os
rows = [json.loads(x) for x in open('$BENCH') if x.strip()]
sec = {r['step']: r['seconds'] for r in rows}
rec = json.load(open('$2/train/$1.json'))
side = json.load(open('$2/train/$1.losscheck.json'))
n_scored = sum(1 for _ in open('$3')) - 1
steps = rec.get('step_times_s') or []
out = {
    'cell': '$1', 'checkpoint': C.SMOKE_CHECKPOINT, 'unit': 'smoke',
    'download_s': sec.get('download'),
    'train_s_per_step': sum(steps) / len(steps) if steps else None,
    'train_steps': len(steps),
    'score_s': sec.get('score'), 'scored_answers': n_scored,
    'score_s_per_answer': sec.get('score') / n_scored if n_scored > 0 else None,
    'gen_s': sec.get('sample'), 'gen_answers': C.SMOKE_GEN_ANSWERS,
    'gen_s_per_answer': sec.get('sample') / C.SMOKE_GEN_ANSWERS,
    'upload_s': side.get('upload_s'), 'hub_repo': side.get('hub_repo'),
    'hub_revision': side.get('hub_revision'), 'losscheck': side.get('status'),
    'steps': rows, 'pass': all(r['rc'] == 0 for r in rows),
    'git_commit': os.environ.get('EMX_GIT_COMMIT', 'unknown'),
    'utc': dt.datetime.now(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
}
json.dump(out, open('$2/bench.json', 'w'), indent=2)
print('E3_SMOKE_BENCH', json.dumps({k: out[k] for k in ('download_s', 'train_s_per_step',
      'score_s_per_answer', 'gen_s_per_answer', 'upload_s')}))
"
}

if [ "$SMOKE" = "1" ]; then smoke; else pod_job; fi
exit 0
