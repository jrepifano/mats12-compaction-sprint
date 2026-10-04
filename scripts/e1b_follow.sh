#!/usr/bin/env bash
# E1b laptop follower (implementation plan §2 U7, §3 S2, §5 S3): watches one pod while
# scripts/pod_run_e1b.sh runs on it, pulls each finished unit as soon as its manifest appears, and
# starts that unit's judging on the laptop, so Part 1 is judged run by run (prereg v0.5 decision).
# On the Part 3 pod it also closes the alpha loop: grid coherence judging -> e1b_select_alpha.py
# -> alpha.json copied back to the pod, which waits for it.
#
# Usage: bash scripts/e1b_follow.sh SLOT [MODE]
#   SLOT  the pod slot in .env (POD_SSH_HOST<SLOT>, POD_SSH_PORT<SLOT>, POD_SSH_USER), e.g. _A
#   MODE  the pod_run_e1b.sh mode running there: part1 (default for _A), part3 (default for _B),
#         cond-seeds, cond-mid. It sets the completion artifacts the follower waits for:
#           part1      MANIFEST.run-{cell} for R.runs("part1") + MANIFEST.untrained-pre
#           part3      MANIFEST.reeval + MANIFEST.part3, and alpha.json pushed
#           cond-seeds MANIFEST.run-{cell} for R.runs("cond_seeds")
#           cond-mid   MANIFEST.run-{cell} for R.runs("cond_mid") + MANIFEST.untrained-mid
#
# Every poll is ONE ssh call (CLAUDE.md "Polling too hard looks like a dead pod"), at least
# E1B_FOLLOW_POLL_S (default and minimum 600) seconds apart, that lists the pod's
# results/E1b/MANIFEST.*.sha256, part3/steer/GRID_READY and _staging_*/MANIFEST.sha256. Then:
#   - a newly listed completion manifest is pulled with U7a's puller
#       bash scripts/pod_pull_results.sh --exp E1b --slot SLOT --run CELL          (run manifests)
#       bash scripts/pod_pull_results.sh --exp E1b --slot SLOT --manifest NAME     (the others)
#     and verified here (shasum -a 256 -c from results/E1b/);
#   - PAID: its generation CSVs are judged in ONE background job per manifest (files in turn, so
#     never two judging processes on one file):
#       uv run python scripts/e1_judge.py --exp E1b <gen csv> --judges luna              (timing
#           checkpoints, `-kNNNN` conditions: descriptive, luna only; prereg §4.6)
#       uv run python scripts/e1_judge.py --exp E1b <gen csv> --judges luna gfl --batch gfl
#           (finals, untrained models, the E1 re-evaluation)
#     MANIFEST.part3 is pulled but never judged (blinded until Checkpoint 1; e1_judge.py refuses
#     Part 3 files anyway). The spend guard inside e1_judge.py enforces the caps;
#   - GRID_READY (part3 only; the pod writes it right after MANIFEST.grid.sha256): pull
#     --manifest MANIFEST.grid.sha256 (the grid CSVs' hashes), verify, then in the background:
#     PAID coherence judging of each grid CSV (e1_judge.py --exp E1b <grid csv> --judges luna
#     --metrics coherent), scripts/e1b_select_alpha.py, and scp of
#     results/E1b/part3/steer/alpha.json to /ws/emx/results/E1b/part3/steer/alpha.json;
#   - a staging manifest (_staging_<mode>/MANIFEST.sha256) is logged loudly once: the run failed
#     on the pod and pod_part.sh pulls the staging. The follower keeps watching;
#   - the same ssh call also returns the pod's running training records (results/E1b/train/),
#     log-probability CSVs (results/E1b/logprob/) and logs/pod_run_e1b.*.log with their pod-side
#     sha256; they replace logs/E1b-follow.<SLOT>.d/mirror/ only when they verify, so a budget
#     stop between manifests never loses the curves (prereg §4.12; the watchdog also salvages).
# Exit: 0 when every completion artifact of MODE is pulled (after the background jobs finish);
#   4 on a STOP file (logs/E1b-follow.STOP or logs/E1b-follow.<SLOT>.STOP); 5 when the pod stays
#   unreachable (E1B_FOLLOW_MAX_UNREACHABLE polls in a row, default 3, then hung local ssh clients
#   to that host older than 10 minutes are killed and it is probed once more); 6 after
#   E1B_FOLLOW_MAX_POLLS polls (default 0 = no limit); 2 usage.
# Log: logs/E1b-follow.log (UTC timestamps; every paid action starts with "PAID:"). State
# (pulled / judged markers, job logs): logs/E1b-follow.<SLOT>.d/. Restart-safe: a unit whose
# local manifest verifies is not pulled again; a judged marker stops re-judging.
# Env (tests): E1B_LAPTOP_PY (default "uv run --quiet python"), E1B_FOLLOW_ALLOW_FAST=1 (allows a
# poll interval below 600 s). Never prints .env. macOS bash 3.2 (no associative arrays, mapfile,
# ${x,,} or |&).
set -uo pipefail
cd "$(dirname "$0")/.."

usage() { echo "usage: e1b_follow.sh SLOT [part1|part3|cond-seeds|cond-mid]" >&2; exit 2; }
SLOT="${1:-}"; [ -n "$SLOT" ] || usage
case "$SLOT" in _[A-Za-z0-9]*) ;; *) echo "SLOT must look like _A" >&2; usage ;; esac
MODE="${2:-}"
if [ -z "$MODE" ]; then
  case "$SLOT" in _A) MODE=part1 ;; _B) MODE=part3 ;; *) usage ;; esac
fi
case "$MODE" in part1|part3|cond-seeds|cond-mid) ;; *) usage ;; esac

POLL_S="${E1B_FOLLOW_POLL_S:-600}"
if [ "$POLL_S" -lt 600 ] && [ "${E1B_FOLLOW_ALLOW_FAST:-0}" != "1" ]; then
  echo "E1B_FOLLOW_POLL_S=$POLL_S < 600: refused (CLAUDE.md polling limit)" >&2; exit 2
fi
MAX_UNREACHABLE="${E1B_FOLLOW_MAX_UNREACHABLE:-3}"
MAX_POLLS="${E1B_FOLLOW_MAX_POLLS:-0}"
LPY="${E1B_LAPTOP_PY:-uv run --quiet python}"
RES=results/E1b
POD_RES=/ws/emx/results/E1b
LOG=logs/E1b-follow.log
STATE="logs/E1b-follow.$SLOT.d"
mkdir -p logs "$STATE"

log() { echo "$(date -u +%FT%TZ) [$SLOT $MODE] $*" | tee -a "$LOG"; }

envval() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }   # never echoed
HOST=$(envval "POD_SSH_HOST$SLOT" || true); PORT=$(envval "POD_SSH_PORT$SLOT" || true)
USR=$(envval POD_SSH_USER || true); USR=${USR:-root}
[ -n "$HOST" ] && [ -n "$PORT" ] || { echo "no pod in slot '$SLOT' (.env)" >&2; exit 2; }
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 \
-o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new"

sha_check() {  # sha_check MANIFEST_REL: verify from results/E1b (manifest paths' root)
  ( cd "$RES" && shasum -a 256 -c --quiet "$1" >/dev/null 2>&1 )
}

expected() {  # the completion manifests of MODE (names relative to results/E1b)
  case "$MODE" in
    part3) echo "MANIFEST.reeval.sha256 MANIFEST.part3.sha256"; return ;;
  esac
  local part extra=""
  case "$MODE" in
    part1) part=part1; extra="MANIFEST.untrained-pre.sha256" ;;
    cond-seeds) part=cond_seeds ;;
    cond-mid) part=cond_mid; extra="MANIFEST.untrained-mid.sha256" ;;
  esac
  echo "$($LPY -c "import sys; sys.path.insert(0, 'src'); from emx.e1b import registry as R; \
print(' '.join(f'MANIFEST.run-{r.cell}.sha256' for r in R.runs('$part')))") $extra"
}

# --- judging -------------------------------------------------------------------------------------
judge_args() {  # judge_args GEN_CSV_REL: the judges a generation CSV gets (prereg §4.6)
  local cond; cond=$(basename "$1" .csv); cond=${cond%%__*}
  if echo "$cond" | grep -Eq -- '-k[0-9]{4}@'; then
    echo "--judges luna"                  # intermediate timing checkpoint: descriptive, luna only
  else
    echo "--judges luna gfl --batch gfl"  # finals, untrained models, the E1 re-evaluation
  fi
}

alive() { [ -s "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

judge_job() {  # judge_job NAME: every generation CSV the manifest lists, one at a time
  local name="$1" f rc=0 jl="$STATE/$1.judge.log"
  for f in $(awk '{print $2}' "$RES/$name" | sed 's/^\*//' | grep -E '^gen/[^/]+\.csv$'); do
    # shellcheck disable=SC2046
    log "PAID: judging $RES/$f ($(judge_args "$f")) [from $name]"
    # shellcheck disable=SC2086,SC2046
    if $LPY scripts/e1_judge.py --exp E1b "$RES/$f" $(judge_args "$f") >>"$jl" 2>&1; then
      log "judged $RES/$f"
    else
      log "JUDGING FAILED for $RES/$f (see $jl); not retried automatically"; rc=1
    fi
  done
  if [ "$rc" = "0" ]; then touch "$STATE/$name.judged"; else touch "$STATE/$name.judge_failed"; fi
  rm -f "$STATE/$name.pid"
}

start_judging() {  # background, one job per manifest; never two at once for the same files
  local name="$1"
  case "$name" in MANIFEST.part3.sha256) log "$name: Part 3 stays blinded; not judged"; return ;; esac
  [ -f "$STATE/$name.judged" ] && return 0
  [ -f "$STATE/$name.judge_failed" ] && return 0   # rerun by hand, then remove the marker
  alive "$STATE/$name.pid" && return 0
  judge_job "$name" &
  echo $! > "$STATE/$name.pid"
  log "started judging job for $name (pid $!)"
}

alpha_job() {  # grid coherence (luna) -> alpha selection -> alpha.json to the pod
  local f jl="$STATE/alpha.judge.log" ok=1
  for f in $(awk '{print $2}' "$RES/MANIFEST.grid.sha256" | sed 's/^\*//' | grep -E '\.csv$'); do
    log "PAID: coherence judging of $RES/$f (luna)"
    $LPY scripts/e1_judge.py --exp E1b "$RES/$f" --judges luna --metrics coherent >>"$jl" 2>&1 \
      || { log "COHERENCE JUDGING FAILED for $RES/$f (see $jl)"; ok=0; }
  done
  if [ "$ok" = "1" ] && $LPY scripts/e1b_select_alpha.py >>"$jl" 2>&1 \
     && [ -s "$RES/part3/steer/alpha.json" ]; then
    # shellcheck disable=SC2086
    if scp -P "$PORT" $SSH_OPTS "$RES/part3/steer/alpha.json" \
         "$USR@$HOST:$POD_RES/part3/steer/alpha.json" >>"$jl" 2>&1; then
      touch "$STATE/alpha.pushed"
      log "alpha.json ($(shasum -a 256 "$RES/part3/steer/alpha.json" | cut -c1-12)) pushed to the pod"
    else
      log "ALPHA PUSH FAILED (scp; see $jl); retried at the next poll"
    fi
  else
    log "ALPHA SELECTION FAILED (see $jl); retried at the next poll"
  fi
  rm -f "$STATE/alpha.pid"
}

# --- pulling -------------------------------------------------------------------------------------
pull() {  # pull NAME: U7a's puller, then our own verification
  local name="$1" cell
  case "$name" in
    MANIFEST.run-*.sha256)
      cell=${name#MANIFEST.run-}; cell=${cell%.sha256}
      bash scripts/pod_pull_results.sh --exp E1b --slot "$SLOT" --run "$cell" >>"$STATE/pull.log" 2>&1 ;;
    *) bash scripts/pod_pull_results.sh --exp E1b --slot "$SLOT" --manifest "$name" \
         >>"$STATE/pull.log" 2>&1 ;;
  esac || { log "PULL FAILED for $name (see $STATE/pull.log); retried at the next poll"; return 1; }
  if [ -s "$RES/$name" ] && sha_check "$name"; then
    log "pulled and verified $name ($(shasum -a 256 "$RES/$name" | cut -c1-12))"
    touch "$STATE/$(echo "$name" | tr '/' '_').pulled"
  else
    log "VERIFY FAILED for $name after the pull; retried at the next poll"; return 1
  fi
}

pulled() { [ -f "$STATE/$(echo "$1" | tr '/' '_').pulled" ] && [ -s "$RES/$1" ] && sha_check "$1"; }

# --- unreachable pod -----------------------------------------------------------------------------
etime_s() {  # ps etime [[dd-]hh:]mm:ss -> seconds
  local e="$1" d=0 h=0 m=0 s=0 a b c
  case "$e" in *-*) d=${e%%-*}; e=${e#*-} ;; esac
  IFS=: read -r a b c <<EOF
$e
EOF
  if [ -n "$c" ]; then h=$a; m=$b; s=$c; else m=$a; s=${b:-0}; fi
  echo $(( 10#$d * 86400 + 10#$h * 3600 + 10#$m * 60 + 10#$s ))
}

kill_hung_ssh() {  # local ssh clients to the pod's host running for over 10 minutes
  local pid et cmd n=0
  while read -r pid et cmd; do
    case "$cmd" in ssh\ *"$HOST"*|*/ssh\ *"$HOST"*) ;; *) continue ;; esac
    [ "$pid" = "$$" ] && continue
    if [ "$(etime_s "$et")" -ge 600 ]; then
      kill "$pid" 2>/dev/null && n=$((n + 1))
    fi
  done <<EOF
$(ps -axo pid=,etime=,command= 2>/dev/null)
EOF
  log "killed $n hung local ssh client(s) to the pod's host"
}

# The poll's remote side: the listing, then (same call) the running training records, log-
# probability CSVs and run logs with their pod-side sha256, as a base64 tar.gz (all small files).
MIRROR_FIND="find results/E1b/train results/E1b/logprob logs -maxdepth 1 -type f ! -name '*.tmp' \
\\( -path 'results/*' -o -name 'pod_run_e1b.*.log' \\) 2>/dev/null"
REMOTE="cd $POD_RES 2>/dev/null && ls -1 MANIFEST.*.sha256 part3/steer/GRID_READY \
_staging_*/MANIFEST.sha256 2>/dev/null; echo E1B_LIST_END; cd /ws/emx 2>/dev/null \
&& F=\$($MIRROR_FIND) && [ -n \"\$F\" ] && sha256sum \$F && echo E1B_SUMS_END \
&& tar czf - \$F | base64; echo E1B_MIRROR_END"

mirror() {  # mirror POLL_OUT: unpack the poll's records into $STATE/mirror/ once they verify
  local out="$1" tmp="$STATE/mirror.tmp" n
  rm -rf "$tmp"; mkdir -p "$tmp"
  awk -v s="$tmp/SUMS" -v b="$tmp/B64" '/^E1B_LIST_END$/ {p = 1; next}
    /^E1B_SUMS_END$/ {p = 2; next} /^E1B_MIRROR_END$/ {p = 3; next}
    p == 1 {print > s} p == 2 {print > b}' "$out"
  [ -s "$tmp/SUMS" ] && [ -s "$tmp/B64" ] || { rm -rf "$tmp"; return 0; }   # nothing yet
  mkdir -p "$tmp/files"
  if base64 --decode < "$tmp/B64" | tar xzf - -C "$tmp/files" 2>/dev/null \
     && ( cd "$tmp/files" && shasum -a 256 -c --quiet ../SUMS >/dev/null 2>&1 ); then
    mv "$tmp/SUMS" "$tmp/files/MANIFEST.sha256"
    rm -rf "$STATE/mirror"; mv "$tmp/files" "$STATE/mirror"; rm -rf "$tmp"
    n=$(wc -l < "$STATE/mirror/MANIFEST.sha256" | tr -d ' ')
    echo "$(date -u +%FT%TZ) mirrored $n running file(s), verified" >> "$STATE/mirror.log"
  else  # a file changed mid-poll, or a broken transfer: keep the last verified mirror
    log "MIRROR NOT VERIFIED this poll (kept the previous one; retried at the next poll)"
    rm -rf "$tmp"
  fi
}

LISTING=""
poll() {  # ONE ssh call; sets LISTING; fails unless the end sentinel came back
  local out="$STATE/poll.out"
  # shellcheck disable=SC2086
  ssh -p "$PORT" $SSH_OPTS "$USR@$HOST" "$REMOTE" > "$out" 2>/dev/null
  grep -qx E1B_LIST_END "$out" || return 1
  # an empty listing (no manifest yet) is a successful poll
  LISTING=$(awk '/^E1B_LIST_END$/ {exit} {print}' "$out")
  mirror "$out"
  return 0
}

# --- main loop -----------------------------------------------------------------------------------
EXPECTED=$(expected)
log "=== follower start: pod slot $SLOT, mode $MODE, poll ${POLL_S}s; waiting for: $EXPECTED"
polls=0; unreachable=0
while :; do
  if [ -f logs/E1b-follow.STOP ] || [ -f "logs/E1b-follow.$SLOT.STOP" ]; then
    log "STOP file found; waiting for background jobs, then exiting"; wait; exit 4
  fi
  polls=$((polls + 1))
  if poll; then
    unreachable=0
  else
    unreachable=$((unreachable + 1))
    log "pod unreachable ($unreachable/$MAX_UNREACHABLE)"
    if [ "$unreachable" -ge "$MAX_UNREACHABLE" ]; then
      kill_hung_ssh
      if poll; then unreachable=0; else
        log "POD UNREACHABLE after $unreachable polls and a re-probe; exiting (jobs keep running)"
        exit 5
      fi
    fi
  fi
  if [ "$unreachable" = "0" ]; then
    for f in $LISTING; do
      case "$f" in
        _staging_*/MANIFEST.sha256)
          m="$STATE/$(echo "$f" | tr '/' '_').seen"
          [ -f "$m" ] || { log "RUN FAILED ON THE POD: $f exists (pod_part.sh pulls the staging)"; touch "$m"; } ;;
        part3/steer/GRID_READY)
          [ "$MODE" = part3 ] || continue
          pulled MANIFEST.grid.sha256 || pull MANIFEST.grid.sha256 || continue
          if [ ! -f "$STATE/alpha.pushed" ] && ! alive "$STATE/alpha.pid"; then
            alpha_job &
            echo $! > "$STATE/alpha.pid"
            log "started alpha job (pid $!)"
          fi ;;
        *)
          case " $EXPECTED " in *" $f "*) ;; *) continue ;; esac
          pulled "$f" || pull "$f" || continue
          start_judging "$f" ;;
      esac
    done
  fi
  done_all=1
  for f in $EXPECTED; do pulled "$f" || done_all=0; done
  [ "$MODE" = part3 ] && [ ! -f "$STATE/alpha.pushed" ] && done_all=0
  if [ "$done_all" = "1" ]; then
    log "every completion artifact of $MODE is pulled; waiting for judging jobs"
    wait
    log "=== follower done"
    exit 0
  fi
  if [ "$MAX_POLLS" -gt 0 ] && [ "$polls" -ge "$MAX_POLLS" ]; then
    log "max polls ($MAX_POLLS) reached; waiting for background jobs, then exiting"; wait; exit 6
  fi
  sleep "$POLL_S"
done
