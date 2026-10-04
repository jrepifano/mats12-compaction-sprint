#!/usr/bin/env bash
# Pod-side spend backstop: STOPS this pod at a deadline, whatever the laptop is doing.
# Idea from github.com/kmerkelbach/runpod-tools (examples/killswitch.sh; docs/gotchas.md: "a cap
# enforced only by an external poller is not a cap"), reimplemented here (that repo has no
# license). The laptop watchdog (pod_watchdog.sh / i1a_watchdog.sh) stays the primary cap; this
# is meant to cover what it cannot: a sleeping or lid-closed laptop (CLAUDE.md, 2026-09-29), a
# lost network, a crashed session. pod_push.sh arms it on every push, with the deadline from
# `python -m emx.runpod --killswitch-deadline` (when the experiment's running pods together reach
# the GPU kill line).
#
# VERIFIED LIVE 2026-10-03 (scripts/killswitch_live_check.py, logs/infra/killswitch-live-*.json;
# pods stopped 6-19 s after their deadlines). With our image and its pod key, REST answered 403
# to both the read and the stop, and `runpodctl stop pod` worked (run 3 streamed the log). So
# `arm` refuses when runpodctl is missing (an image without it could never fire), runpodctl is
# the first route, and REST is kept only as a fallback.
#
# Key: RUNPOD_API_KEY and RUNPOD_POD_ID from the container's PID 1 environment (/proc/1/environ;
# ssh shells do not inherit it). The laptop's account key never reaches the pod (pod_push.sh
# refuses it). The key never goes on a command line (it would sit in `ps`): curl reads its
# header from a config on stdin (-q first, so no ~/.curlrc can add tracing), and runpodctl
# (fallback) reads it from its environment.
#
# Action: stop (`runpodctl stop pod`, the syntax the image's runpodctl knows; then the newer
# `runpodctl pod stop`; then REST POST /v1/pods/{id}/stop). A stop that worked kills this
# container, watcher included, so the watcher confirms a stop by not surviving it: while it
# still runs after the deadline it retries every poll. Stopping ends GPU billing but wipes the container disk, /ws included: results not
# yet pulled are lost. The deadline is a budget backstop, not a schedule. Until the laptop runs
# pod_down.py (terminate + ledger record), the laptop's books still count a self-stopped pod as
# burning, i.e. they over-count, never under-count.
#
# Usage (root on the pod):
#   pod_killswitch.sh arm POD_ID DEADLINE_EPOCH
#       refuses unless RUNPOD_POD_ID in PID 1's env equals POD_ID (never stops another pod) and
#       runpodctl is installed;
#       reads the pod with the key (key_check=ok|read403|unverified:<why>; with our image the pod
#       key gets 403 on reads, the expected state; no read result proves stop permission), writes the deadline, and starts the watcher unless one is alive.
#       Serialized with flock when available. Re-arming only moves the deadline. Prints the last
#       line "ARMED pod=<id> deadline=<epoch> (<utc>) key_check=<...>" only when every state file
#       was written and a watcher is alive; otherwise exits 1.
#   pod_killswitch.sh status      FIRED / ARMED (watcher alive or not) / NOT ARMED
#   pod_killswitch.sh watch       the detached loop (started by arm)
# State in KS_DIR (default /ws/killswitch): deadline, pod_id, watcher.pid, fired, killswitch.log.
# Test hooks: KS_ENVIRON (/proc/1/environ), KS_API (https://rest.runpod.io/v1), KS_POLL_S (60),
#   KS_ONCE=1 (watch does one check and exits), KS_NO_SPAWN=1 (arm starts no watcher).
# Exit: 0 ok; 1 failed; 2 usage. Written for bash 3.2 too (tests run macOS /bin/bash).
set -u

KS_DIR=${KS_DIR:-/ws/killswitch}
KS_ENVIRON=${KS_ENVIRON:-/proc/1/environ}
KS_API=${KS_API:-https://rest.runpod.io/v1}
KS_POLL_S=${KS_POLL_S:-60}
# RunPod's Cloudflare front blocks non-browser agents (error 1010; CLAUDE.md gotcha)
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

say() { echo "$(date -u +%FT%TZ) [killswitch] $*"; }
usage() { echo "usage: pod_killswitch.sh arm POD_ID DEADLINE_EPOCH | status | watch" >&2; exit 2; }
fail() { say "NOT ARMED: $*"; exit 1; }

# pid1var NAME: the value of NAME in PID 1's environment (empty if absent or unreadable)
pid1var() {
  [ -r "$KS_ENVIRON" ] || return 0
  tr '\0' '\n' < "$KS_ENVIRON" | sed -n "s/^$1=//p" | head -n 1
}

load_key() {
  KEY=$(pid1var RUNPOD_API_KEY)
  POD_SELF=$(pid1var RUNPOD_POD_ID)
  case "$KEY" in
    ""|*[!A-Za-z0-9_.-]*) say "no usable RUNPOD_API_KEY in $KS_ENVIRON"; return 1 ;;
  esac
  [ -n "$POD_SELF" ] || { say "no RUNPOD_POD_ID in $KS_ENVIRON"; return 1; }
}

# api METHOD PATH: prints the HTTP status (000 when curl failed); the body is discarded
api() {
  printf 'header = "Authorization: Bearer %s"\n' "$KEY" \
    | curl -q -sS -K - -X "$1" -A "$UA" --max-time 30 -o /dev/null -w '%{http_code}' \
        "$KS_API$2" 2>>"$KS_DIR/killswitch.log" || true
}

# runpodctl_do ARGS...: the documented in-pod route; the key travels in the environment only
runpodctl_do() {
  command -v runpodctl >/dev/null 2>&1 || return 1
  RUNPOD_API_KEY="$KEY" runpodctl "$@" >>"$KS_DIR/killswitch.log" 2>&1
}

watcher_alive() {
  local pid
  pid=$(cat "$KS_DIR/watcher.pid" 2>/dev/null) || return 1
  case "$pid" in ""|*[!0-9]*) return 1 ;; esac
  ps -p "$pid" -o command= 2>/dev/null | grep -q 'pod_killswitch[.]sh watch'
}

# Each route is logged BEFORE it runs: a stop that works can kill the container before the
# "accepted" line is written (live run 4, 2026-10-03), so the last "trying" line names the route.
stop_pod() {
  local code
  say "trying runpodctl stop pod"
  if runpodctl_do stop pod "$POD_SELF" || runpodctl_do pod stop "$POD_SELF"; then
    say "stop accepted (runpodctl)"; return 0
  fi
  say "trying REST stop"
  code=$(api POST "/pods/$POD_SELF/stop")
  case "$code" in
    2??) say "stop accepted (REST HTTP $code)"; return 0 ;;
  esac
  say "STOP FAILED (runpodctl failed; REST HTTP $code)"
  return 1
}

cmd_arm() {
  local want=$1 deadline=$2 code check i
  case "$want" in ""|*[!A-Za-z0-9]*) usage ;; esac
  case "$deadline" in ""|*[!0-9]*) usage ;; esac
  mkdir -p "$KS_DIR" && chmod 700 "$KS_DIR" && : >> "$KS_DIR/killswitch.log" \
    || fail "cannot create $KS_DIR"
  if command -v flock >/dev/null 2>&1; then   # Linux pods: one arm at a time
    exec 9>"$KS_DIR/arm.lock" && flock -w 30 9 || fail "could not take $KS_DIR/arm.lock"
  fi
  load_key || fail "no pod key"
  [ "$POD_SELF" = "$want" ] || fail "REFUSED: this is pod $POD_SELF, not $want"
  command -v runpodctl >/dev/null 2>&1 \
    || fail "no runpodctl on this pod: the pod key can stop the pod only through it (REST is 403)"
  # Live check 2026-10-03 (logs/infra/): with our image the pod key gets 403 reading its own
  # pod, yet `runpodctl stop pod` stopped it. read403 is that expected state; it does not by
  # itself prove the key is valid (only the live self-stop did).
  code=$(api GET "/pods/$POD_SELF")
  case "$code" in
    2??) check=ok ;;
    403) check=read403 ;;
    *) check="unverified:http_$code" ;;
  esac
  { printf '%s\n' "$POD_SELF" > "$KS_DIR/pod_id" \
      && printf '%s\n' "$deadline" > "$KS_DIR/deadline.tmp" \
      && mv "$KS_DIR/deadline.tmp" "$KS_DIR/deadline" \
      && rm -f "$KS_DIR/fired"; } || fail "could not write the state files in $KS_DIR"
  if [ "${KS_NO_SPAWN:-0}" != "1" ] && ! watcher_alive; then
    rm -f "$KS_DIR/watcher.pid"
    if command -v setsid >/dev/null 2>&1; then
      { setsid nohup bash "$0" watch >>"$KS_DIR/killswitch.log" 2>&1 </dev/null 9>&- & }
    else
      { nohup bash "$0" watch >>"$KS_DIR/killswitch.log" 2>&1 </dev/null 9>&- & }
    fi
    i=0   # the watcher writes its own pid; give it up to 10 s to appear
    while [ "$i" -lt 20 ] && ! watcher_alive; do sleep 0.5; i=$((i + 1)); done
  fi
  [ "${KS_NO_SPAWN:-0}" = "1" ] || watcher_alive || fail "the watcher did not start"
  say "ARMED pod=$POD_SELF deadline=$deadline ($(date -u -d "@$deadline" +%FT%TZ 2>/dev/null \
    || date -u -r "$deadline" +%FT%TZ)) key_check=$check"
}

cmd_watch() {
  local now deadline
  load_key || exit 1
  echo $$ > "$KS_DIR/watcher.pid"   # its own pid: $! in arm may be setsid's if setsid forked
  say "watcher started (pid $$, poll ${KS_POLL_S}s)"
  while :; do
    deadline=$(cat "$KS_DIR/deadline" 2>/dev/null)
    now=$(date +%s)
    case "$deadline" in
      ""|*[!0-9]*) say "no valid deadline file; checking again in ${KS_POLL_S}s" ;;
      *) if [ "$now" -ge "$deadline" ]; then
           say "DEADLINE REACHED ($deadline): stopping pod $POD_SELF"
           if stop_pod; then
             [ -f "$KS_DIR/fired" ] || date -u +%FT%TZ > "$KS_DIR/fired"
             # a stop that took kills this process; still running at the next poll = retry
           elif [ "${KS_ONCE:-0}" = "1" ]; then
             exit 1
           fi
         fi ;;
    esac
    [ "${KS_ONCE:-0}" = "1" ] && exit 0
    sleep "$KS_POLL_S"
  done
}

cmd_status() {
  local alive=no
  watcher_alive && alive=yes
  if [ -f "$KS_DIR/fired" ]; then echo "FIRED at $(cat "$KS_DIR/fired") watcher=$alive"
  elif [ -f "$KS_DIR/deadline" ]; then echo "ARMED deadline=$(cat "$KS_DIR/deadline") watcher=$alive"
  else echo "NOT ARMED"; fi
}

case "${1:-}" in
  arm) [ $# -eq 3 ] || usage; cmd_arm "$2" "$3" ;;
  watch) [ $# -eq 1 ] || usage; cmd_watch ;;
  status) [ $# -eq 1 ] || usage; cmd_status ;;
  *) usage ;;
esac
