#!/usr/bin/env bash
# E1 run chain, ON the pod (implementation plan U9, §5 paid steps P1-P4). Idempotent: every step
# is skipped when its completion artifact exists and verifies, so a lost pod or a crash resumes by
# re-running the same command. Patterned on ../mats12_neel_working/scripts/pod_run_arms.sh and
# pod_run_breadth.sh (detached launch, manifest-gated completion); rewritten for E1's steps.
#
# Launch detached (from the laptop):
#   ssh -p PORT root@HOST "cd /ws/emx && setsid nohup bash scripts/pod_run_e1.sh part part2 \
#       --recipe R0 >/dev/null 2>&1 </dev/null & disown"
# Log: /ws/emx/logs/pod_run_e1.<mode>.log
#
# Modes:
#   smoke                    P1: preflight; embedding check (base); PEFT and unsloth 30 steps on
#                            inst-chat-bad-R0-s1 (--no-push) -> e1_backend_select.py; the 30-step
#                            PEFT adapter pushed to a private scratch repo e1-smoke-<cell>-peft (the
#                            gen scripts read adapters only from the Hub at a pinned sha); 16 vLLM
#                            and 16 HF samples with it; 4-prompt self-restore (local adapter dir). Summary: results/E1/checks/smoke.json
#   part NAME --recipe R [--cuts a,b]
#                            each cell of registry.cells(NAME, R): train -> losscheck -> generate
#                            its conditions; then the part's untrained conditions (part1 also
#                            runs the HF engine check). Part manifest
#                            results/E1/MANIFEST.part-NAME.sha256 only if every step succeeded.
#   restore --recipe R       full 44-prompt self-restore per chat restore model (gate), then the
#                            restore conditions, each with its own manifest.
#
# Venvs: training in the backend chosen in results/E1/checks/backend_select.json
# (/ws/venv-peft or /ws/venv-unsloth); vLLM generation in /ws/venv-vllm; HF generation, restore,
# checks in /ws/venv-peft. Registry queries use the peft venv's python (uv run python off-pod).
#
# E1_DRY_RUN=1 prints the command sequence (with "+ " prefixes) and runs nothing but registry
# queries and completion checks. E1_BACKEND overrides the selected backend (dry runs only).
# bash 3.2 compatible (no associative arrays, mapfile, ${x,,} or |&).
set -euo pipefail

MODE="${1:-}"; [ -n "$MODE" ] && shift
PART=""; RECIPE=""; CUTS=""
if [ "$MODE" = "part" ]; then PART="${1:?part name}"; shift; fi
while [ $# -gt 0 ]; do
  case "$1" in
    --recipe) RECIPE="${2:?}"; shift 2 ;;
    --cuts) CUTS="${2:?}"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$MODE" in
  smoke) ;;
  part|restore) [ -n "$RECIPE" ] || { echo "$MODE needs --recipe R0|R1|R2" >&2; exit 2; } ;;
  *) echo "usage: pod_run_e1.sh smoke | part NAME --recipe R [--cuts a,b] | restore --recipe R" >&2
     exit 2 ;;
esac

DRY="${E1_DRY_RUN:-0}"
cd "$(dirname "$0")/.."
VENVS="${E1_VENVS:-/ws}"
PEFT_PY="$VENVS/venv-peft/bin/python"
UNSLOTH_PY="$VENVS/venv-unsloth/bin/python"
VLLM_PY="$VENVS/venv-vllm/bin/python"
if [ -x "$PEFT_PY" ]; then PYQ="$PEFT_PY"; else PYQ="uv run --quiet python"; fi
SMOKE_CELL="inst-chat-bad-R0-s1"
SMOKE_DIR="results/E1/checks/smoke"
SMOKE_ADAPTERS="${E1_SMOKE_ADAPTERS:-/ws/smoke}"   # outside the repo: never pulled or committed
QSETS="${E1_QSETS:-general medical narrow}"   # space-separated (gen scripts take nargs=+)
FAILED=0
GEN_EXTRA=""
case ",$CUTS," in *,medical_10,*) GEN_EXTRA="--medical-cut" ;; esac   # prereg §7 cut 3

if [ "$DRY" != "1" ]; then
  mkdir -p logs results/E1/checks results/E1/train
  LOG="logs/pod_run_e1.${MODE}${PART:+.$PART}.log"
  exec >>"$LOG" 2>&1
  set +x; set -a; [ -f .env ] && . ./.env; set +a   # never trace the token
  export HF_HOME="${HF_HOME:-/ws/hf_cache}" WANDB_MODE=offline TOKENIZERS_PARALLELISM=false
fi
say() { echo "$(date -u +%FT%TZ) $*"; }
say "=== pod_run_e1.sh $MODE ${PART} recipe=${RECIPE:-} cuts=${CUTS:-} dry=$DRY commit=${EMX_GIT_COMMIT:-unknown}"

# run CMD...: print, and execute unless dry
run() {
  echo "+ $*"
  [ "$DRY" = "1" ] && return 0
  "$@"
}

pyq() {  # registry query; the snippet sees R (registry) and json
  $PYQ -c "import sys, json; sys.path.insert(0, 'src'); from emx.e1 import registry as R; $1"
}

sha_check() {  # sha_check DIR MANIFEST_NAME: verify a sha256 manifest from its own directory
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$1" && sha256sum -c --quiet "$2" >/dev/null 2>&1 )
  else
    ( cd "$1" && shasum -a 256 -c --quiet "$2" >/dev/null 2>&1 )
  fi
}

sha_of() {  # sha_of FILE...: "<sha>  <file>" lines
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

manifest_ok() {  # manifest_ok DIR CONDITION [QSET...]: verifies, and lists every qset's CSV+meta
  local d="$1" c="$2" q; shift 2
  [ -s "$d/MANIFEST.$c.sha256" ] && sha_check "$d" "MANIFEST.$c.sha256" || return 1
  for q in "$@"; do  # the gen scripts rewrite the manifest after each qset: a partial run verifies
    grep -qF "  ${c}__$q.csv" "$d/MANIFEST.$c.sha256" || return 1
    grep -qF "  ${c}__$q.meta.json" "$d/MANIFEST.$c.sha256" || return 1
  done
}

trained_ok() {  # the trainer's result exists and carries a 40-char hub_revision
  [ -f "results/E1/train/$1.json" ] || return 1
  pyq "r = json.load(open('results/E1/train/$1.json')).get('hub_revision') or ''; \
sys.exit(0 if len(r) == 40 else 1)"
}

selected_backend() {
  if [ -n "${E1_BACKEND:-}" ]; then
    [ "$DRY" = "1" ] || { echo "E1_BACKEND is for dry runs only" >&2; exit 2; }
    echo "$E1_BACKEND"; return
  fi
  local f=results/E1/checks/backend_select.json
  if [ ! -f "$f" ]; then
    [ "$DRY" = "1" ] && { echo "peft"; return; }
    echo "no $f: run the smoke mode first" >&2; exit 1
  fi
  pyq "d = json.load(open('$f')); b = d.get('backend') or d.get('chosen') or d.get('selected'); \
assert b in ('peft', 'unsloth'), d; print(b)"
}

# ------------------------------------------------------------------------------------------------
# smoke (P1)
# ------------------------------------------------------------------------------------------------
STEPS_TSV=""
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
  local tr="results/E1/train/$SMOKE_CELL.json" cfg="configs/E1/$SMOKE_CELL.json"
  if [ "$DRY" != "1" ]; then
    trained_ok "$SMOKE_CELL" && { echo "refusing: $tr is a real (pushed) run" >&2; exit 1; }
    [ -f "$cfg" ] || { echo "missing $cfg (run e1_make_configs.py on the laptop)" >&2; exit 1; }
  fi
  if [ "$DRY" = "1" ]; then
    STEPS_TSV=/dev/null
  else
    mkdir -p "$SMOKE_DIR"; STEPS_TSV="$SMOKE_DIR/steps.tsv"; : > "$STEPS_TSV"
  fi
  step preflight "$PEFT_PY" scripts/e1_preflight.py \
    || { say "preflight failed: stopping smoke"; summarise_smoke; exit 1; }
  step embed_check "$PEFT_PY" scripts/e1_embed_check.py --stage base || true

  local b py
  for b in peft unsloth; do
    if [ "$b" = peft ]; then py="$PEFT_PY"; else py="$UNSLOTH_PY"; fi
    run rm -f "$tr"
    if step "train_$b" "$py" scripts/e1_train.py --config "$cfg" --backend "$b" \
         --max-steps 30 --no-push; then
      run mv "$tr" "$SMOKE_DIR/${SMOKE_CELL}__$b.json"
      # keep this backend's adapter: both backends write the config's output_dir
      run rm -rf "$SMOKE_ADAPTERS/adapter_$b"
      run mkdir -p "$SMOKE_ADAPTERS"
      if [ "$DRY" = "1" ]; then
        echo "+ cp -r <adapter_dir of ${SMOKE_CELL}__$b.json> $SMOKE_ADAPTERS/adapter_$b"
      else
        cp -r "$(pyq "print(json.load(open('$SMOKE_DIR/${SMOKE_CELL}__$b.json'))['adapter_dir'])")" \
          "$SMOKE_ADAPTERS/adapter_$b"
      fi
    fi
  done
  local sel=(scripts/e1_backend_select.py --peft "$SMOKE_DIR/${SMOKE_CELL}__peft.json"
             --unsloth "$SMOKE_DIR/${SMOKE_CELL}__unsloth.json")
  if [ "$DRY" != "1" ] && [ ! -f "$SMOKE_DIR/${SMOKE_CELL}__unsloth.json" ]; then
    local msg; msg=$(tail -n 5 "$SMOKE_DIR/train_unsloth.log" 2>/dev/null | tr '\n' ' ' | cut -c1-500)
    sel=("${sel[@]}" --unsloth-load-error "unsloth run failed: ${msg:-no log}")
  fi
  step backend_select "$PEFT_PY" "${sel[@]}" || true

  # The gen scripts load adapters only from the Hub at a pinned hub_revision (--train-dir JSON),
  # never from a local dir. So the 30-step PEFT adapter goes to a separate private scratch repo
  # ({HF_USERNAME}/e1-smoke-<cell>-peft, never a result repo) and a copy of its TrainResult with
  # that repo and commit sha is written to $SMOKE_DIR/train/ for --train-dir.
  local ad="$SMOKE_ADAPTERS/adapter_peft" gdir
  step push_smoke_adapter pyq "
import os
from huggingface_hub import HfApi
api, user = HfApi(), os.environ['HF_USERNAME']
repo = f'{user}/e1-smoke-$SMOKE_CELL-peft'
api.create_repo(repo, private=True, exist_ok=True)
assert api.model_info(repo).private, repo
ci = api.upload_folder(folder_path='$ad', repo_id=repo, commit_message='E1 smoke adapter, 30 steps')
tr = json.load(open('$SMOKE_DIR/${SMOKE_CELL}__peft.json'))
tr.update(hub_repo=repo, hub_revision=ci.oid)
os.makedirs('$SMOKE_DIR/train', exist_ok=True)
json.dump(tr, open('$SMOKE_DIR/train/$SMOKE_CELL.json', 'w'), indent=1)
print('smoke adapter', repo, ci.oid)
" || true
  for gdir in vllm hf; do  # 8 general questions x 2 samples = 16 each
    if [ "$gdir" = vllm ]; then py="$VLLM_PY"; else py="$PEFT_PY"; fi
    step "gen_$gdir" "$py" "scripts/e1_gen_$gdir.py" --conditions "$SMOKE_CELL@chat" \
      --qsets general --samples 2 --train-dir "$SMOKE_DIR/train" \
      --out-dir "$SMOKE_DIR/gen_$gdir" || true
  done
  step restore_self4 "$PEFT_PY" scripts/e1_restore.py --self-check --cell "$SMOKE_CELL" \
    --n-prompts 4 --adapter-dir "$ad" || true
  # the 4-prompt result is not the Part 3 gate: move it out of restore_self.json's place
  run mv results/E1/checks/restore_self.json "$SMOKE_DIR/restore_self_4.json" || true
  summarise_smoke
}

summarise_smoke() {
  [ "$DRY" = "1" ] && { echo "+ write results/E1/checks/smoke.json"; return 0; }
  pyq "
import datetime as dt, os
steps = []
for line in open('$STEPS_TSV'):
    n, rc, s = line.rstrip('\n').split('\t')
    steps.append({'step': n, 'rc': int(rc), 'seconds': int(s)})
def load(p):
    return json.load(open(p)) if os.path.exists(p) else None
out = {'cell': '$SMOKE_CELL', 'steps': steps, 'pass': all(s['rc'] == 0 for s in steps),
       'backend_select': load('results/E1/checks/backend_select.json'),
       'embed_verdict': (load('results/E1/checks/embed.json') or {}).get('verdict'),
       'restore_self_4': load('$SMOKE_DIR/restore_self_4.json'),
       'adapter_note': 'gen and restore used the 30-step PEFT adapter from a local dir',
       'git_commit': os.environ.get('EMX_GIT_COMMIT', 'unknown'),
       'utc': dt.datetime.now(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}
json.dump(out, open('results/E1/checks/smoke.json', 'w'), indent=2)
print('SMOKE', 'PASS' if out['pass'] else 'FAIL', [(s['step'], s['rc']) for s in steps])
"
}

# ------------------------------------------------------------------------------------------------
# part (P2-P4)
# ------------------------------------------------------------------------------------------------
part_plan() {  # lines: "TRAIN <cell>" | "GEN <cell> <c1,c2>" | "GENHF <cell> <cond> <qset>"
  pyq "
part, rec = '$PART', '$RECIPE'
cuts = tuple(x for x in '$CUTS'.split(',') if x)
group = {'part1': 'seed1', 'part2': 'seed1', 'part3': 'replication', 'dpo': 'dpo',
         'nem_check': 'nem_check'}[part]
cells = [str(c) for c in R.cells(part, rec, cuts)]
conds = [c for c in (R.conditions(group, rec, cuts) if group != 'nem_check'
                     else R.conditions(group, rec))]
for cell in cells:
    print('TRAIN', cell)
    cs = [str(c) for c in conds if str(c.cell) == cell]
    if cs:
        print('GEN', cell, ','.join(cs))
untr = [c for c in conds if c.cell.untrained]
if part == 'part1':
    untr = [c for c in R.conditions('untrained', rec) if c.cell.stage == 'inst']
elif part == 'part2':
    untr = R.conditions('untrained', rec)
for st in dict.fromkeys(c.cell.stage for c in untr):
    print('GEN', st + '-untrained', ','.join(str(c) for c in untr if c.cell.stage == st))
if part == 'part1':
    print('GENHF', cells[0], cells[0] + '@chat', 'general')
"
}

check_config_backend() {  # the chosen backend must be recorded in the cell's training config
  [ "$DRY" = "1" ] && return 0
  local cfg="configs/E1/$1.json"
  [ -f "$cfg" ] || { say "missing $cfg"; return 1; }
  pyq "b = json.load(open('$cfg'))['backend']; \
sys.exit(0 if b == '$BACKEND' else print('$cfg backend', b, '!= selected $BACKEND') or 1)"
}

gen_missing() {  # gen_missing DIR CONDS_CSV -> space list of conditions not complete for $QSETS
  local dir="$1" c out=""
  for c in $(echo "$2" | tr ',' ' '); do
    # shellcheck disable=SC2086
    manifest_ok "$dir" "$c" $QSETS || out="${out:+$out }$c"
  done
  echo "$out"
}

part() {
  BACKEND=$(selected_backend)
  if [ "$BACKEND" = unsloth ]; then TRAIN_PY="$UNSLOTH_PY"; else TRAIN_PY="$PEFT_PY"; fi
  say "backend=$BACKEND"
  # pre-create this part's private adapter repos (asserts they are private) before any training
  run "$PEFT_PY" scripts/e1_preflight.py --part "$PART" --recipe "$RECIPE" \
    || { say "PREFLIGHT (--part $PART) FAILED"; exit 1; }
  local plan kind cell arg3 arg4 todo skip_cell="" done_files=""
  plan=$(part_plan)
  echo "$plan" | sed 's/^/  plan: /'
  while read -r kind cell arg3 arg4; do
    [ -n "$kind" ] || continue
    case "$kind" in
      TRAIN)
        skip_cell=""
        if trained_ok "$cell"; then
          say "$cell: trained (hub_revision recorded); skipping training"
        elif ! check_config_backend "$cell" \
          || ! run "$TRAIN_PY" scripts/e1_train.py --config "configs/E1/$cell.json" \
                 --backend "$BACKEND"; then
          say "CELL $cell: TRAINING FAILED"; FAILED=1; skip_cell="$cell"; continue
        fi
        if ! run "$PEFT_PY" scripts/e1_losscheck.py --train-result "results/E1/train/$cell.json"
        then
          say "CELL $cell: LOSSCHECK FAILED (gate; downstream steps skipped, flag for Jacob)"
          FAILED=1; skip_cell="$cell"; continue
        fi
        done_files="$done_files train/$cell.json train/$cell.losscheck.json" ;;
      GEN)
        [ "$cell" = "$skip_cell" ] && { say "$cell: skipping generation (cell failed)"; continue; }
        # always call the generator: it skips an output only if its hash AND its full sampling /
        # template / override contract match this run (code review r2), and loads no engine
        # when nothing is pending
        todo=$(echo "$arg3" | tr ',' ' ')
        # shellcheck disable=SC2086
        if ! run "$VLLM_PY" scripts/e1_gen_vllm.py --conditions $todo --qsets $QSETS \
               $GEN_EXTRA; then
          say "GEN $cell FAILED ($todo)"; FAILED=1; continue
        fi
        for c in $(echo "$arg3" | tr ',' ' '); do
          # shellcheck disable=SC2086
          if [ "$DRY" != "1" ] && ! manifest_ok results/E1/gen "$c" $QSETS; then
            say "GEN $c: manifest missing or does not verify"; FAILED=1
          fi
          done_files="$done_files gen/MANIFEST.$c.sha256"
        done ;;
      GENHF)
        [ "$cell" = "$skip_cell" ] && continue
        if ! run "$PEFT_PY" scripts/e1_gen_hf.py --conditions "$arg3" --qsets "$arg4" \
               --samples 100 --out-dir results/E1/gen_hf; then  # 8 x 100: amendment B item 9
          say "GENHF $arg3 FAILED"; FAILED=1; continue
        fi
        done_files="$done_files gen_hf/MANIFEST.$arg3.sha256" ;;
    esac
  done <<EOF
$plan
EOF
  finish_manifest "part-$PART" "$done_files"
}

finish_manifest() {  # finish_manifest NAME "rel files": written only if nothing failed
  local m="results/E1/MANIFEST.$1.sha256"
  if [ "$FAILED" != "0" ]; then
    say "NOT writing $m: some step failed (see above)"; return 1
  fi
  if [ "$DRY" = "1" ]; then echo "+ write $m over:$2"; return 0; fi
  # shellcheck disable=SC2086
  ( cd results/E1 && sha_of $2 ) > "$m.tmp" && mv "$m.tmp" "$m"
  say "wrote $m ($(wc -l < "$m" | tr -d ' ') files)"
}

# ------------------------------------------------------------------------------------------------
# restore (P4)
# ------------------------------------------------------------------------------------------------
restore() {
  local cell c todo files done_files=""
  local models; models=$(pyq "print(' '.join(f'{st}-chat-{d}-$RECIPE-s1' \
for st, d in R.RESTORE['chat_models']))")
  for cell in $models; do
    local gate="results/E1/checks/restore_self.$cell.json"
    if [ -f "$gate" ] && pyq "sys.exit(0 if json.load(open('$gate'))['pass'] else 1)"; then
      say "$cell: 44-prompt self-restore already passed"
    elif run "$PEFT_PY" scripts/e1_restore.py --self-check --cell "$cell"; then
      run cp results/E1/checks/restore_self.json "$gate"
    else
      say "SELF-RESTORE FAILED for $cell: no restore condition is run (prereg §4.10)"
      FAILED=1
    fi
    done_files="$done_files checks/restore_self.$cell.json"
  done
  [ "$FAILED" = "0" ] || { finish_manifest "restore" ""; exit 1; }
  for c in $(pyq "print(' '.join(str(c) for c in R.conditions('restore', '$RECIPE')))"); do
    if manifest_ok results/E1/restore "$c"; then
      say "$c: manifest verifies; skipping"
    elif run "$PEFT_PY" scripts/e1_restore.py --condition "$c"; then
      if [ "$DRY" = "1" ]; then
        echo "+ write results/E1/restore/MANIFEST.$c.sha256"
      else
        files=$(cd results/E1/restore && ls -- "$c"__* 2>/dev/null | grep -v '^MANIFEST' || true)
        if [ -z "$files" ]; then
          say "RESTORE $c wrote no files"; FAILED=1; continue
        fi
        # shellcheck disable=SC2086
        ( cd results/E1/restore && sha_of $files ) > "results/E1/restore/MANIFEST.$c.sha256"
      fi
    else
      say "RESTORE $c FAILED"; FAILED=1; continue
    fi
    done_files="$done_files restore/MANIFEST.$c.sha256"
  done
  finish_manifest "restore-$RECIPE" "$done_files"
}

case "$MODE" in
  smoke) smoke ;;
  part) part ;;
  restore) restore ;;
esac
rc=0; [ "$FAILED" = "0" ] || rc=1
say "=== done $MODE ${PART} rc=$rc"
exit "$rc"
