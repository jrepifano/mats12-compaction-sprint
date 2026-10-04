#!/usr/bin/env bash
# Free Linux check of scripts/pod_killswitch.sh in Docker (python:3.12-slim + curl + procps):
# real bash 5, setsid, flock, GNU date and /proc/1/environ (the container's PID 1 carries a fake
# RUNPOD_API_KEY / RUNPOD_POD_ID, as RunPod's would), against a local fake RunPod API that logs
# each request's method, path, Authorization and User-Agent. Complements the macOS unit tests
# (tests/test_pod_killswitch.py: no flock or setsid there) and the paid live check
# (scripts/killswitch_live_check.py: real key, real stop).
# Usage: bash scripts/killswitch_linux_check.sh      exit 0 = every check passed
set -u
cd "$(dirname "$0")/.."
exec docker run --rm -i -v "$PWD/scripts:/ws/emx/scripts:ro" \
  -e RUNPOD_API_KEY=rpa_FAKEpodKEY123 -e RUNPOD_POD_ID=podlinux1 \
  python:3.12-slim bash -s <<'IN_CONTAINER'
set -u
apt-get -qq update >/dev/null && apt-get -qq install -y curl procps >/dev/null 2>&1 \
  || { echo "FAIL setup: apt-get"; exit 1; }
KS=/ws/emx/scripts/pod_killswitch.sh
export KS_API=http://127.0.0.1:8080/v1 KS_POLL_S=2
cat > /tmp/api.py <<'PY'
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def _do(self):
        a = self.headers.get("Authorization", "")
        ok = a == "Bearer rpa_FAKEpodKEY123"
        ua = "Mozilla/5.0" in self.headers.get("User-Agent", "")
        with open("/tmp/api.log", "a") as f:
            f.write(f"{self.command} {self.path} auth_ok={ok} ua_ok={ua}\n")
        self.send_response(200 if ok else 401); self.end_headers(); self.wfile.write(b"{}")
    do_GET = do_POST = _do
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", 8080), H).serve_forever()
PY
python3 /tmp/api.py & sleep 1
# fake runpodctl: logs its args and whether the key came in its env; exits 1 while /tmp/rpctl.fail
cat > /usr/local/bin/runpodctl <<'SH'
#!/bin/sh
echo "$* key_in_env=${RUNPOD_API_KEY:+yes}" >> /tmp/rpctl.log
[ -f /tmp/rpctl.fail ] && exit 1
exit 0
SH
chmod +x /usr/local/bin/runpodctl
FAILS=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; FAILS=$((FAILS+1)); fi; }
far=$(( $(date +%s) + 3600 ))

out=$(bash $KS arm otherpod $far); rc=$?
check "refuses another pod's id" '[ $rc = 1 ] && echo "$out" | grep -q REFUSED && ! [ -f /ws/killswitch/deadline ]'

PIDS=""   # wait on the arms only: a bare `wait` would also wait on the fake API forever
for i in 1 2 3 4 5 6; do (bash $KS arm podlinux1 $far > /tmp/arm.$i 2>&1; echo $? >> /tmp/arm.$i) & PIDS="$PIDS $!"; done
for p in $PIDS; do wait "$p"; done
check "6 concurrent arms all ARMED, key_check=ok" '[ "$(grep -l " ARMED pod=podlinux1 deadline=$far .*key_check=ok" /tmp/arm.? | wc -l)" = 6 ]'
check "exactly one watcher after concurrent arms" '[ "$(pgrep -fc "[p]od_killswitch.sh watch")" = 1 ]'
check "ARMED line has a GNU-date UTC time" 'grep -qE "deadline=$far \([0-9]{4}-[0-9]{2}-[0-9]{2}T" /tmp/arm.1'
check "watcher is in its own session (setsid)" '[ "$(ps -o sid= -p "$(cat /ws/killswitch/watcher.pid)" | tr -d " ")" = "$(cat /ws/killswitch/watcher.pid)" ]'
check "state dir is mode 700" '[ "$(stat -c %a /ws/killswitch)" = 700 ]'
check "GET read used the exact Bearer header + browser UA" 'grep -q "^GET /v1/pods/podlinux1 auth_ok=True ua_ok=True" /tmp/api.log'
# the key goes to grep through a file: `grep KEY` would put it in grep's own argv and match itself
printf %s rpa_FAKEpodKEY123 > /tmp/k.txt; ps -eo pid,args > /tmp/ps.txt
echo "argv matches: $(grep -F -f /tmp/k.txt /tmp/ps.txt | grep -v " bash -s$" || echo none)"
check "key in no process argv" '! grep -v " bash -s$" /tmp/ps.txt | grep -qF -f /tmp/k.txt'
check "key not in the killswitch log" '! grep -qF rpa_FAKEpodKEY123 /ws/killswitch/killswitch.log'
check "no stop before the deadline" '! grep -q "^POST" /tmp/api.log && ! [ -s /tmp/rpctl.log ]'

bash $KS arm podlinux1 $(( $(date +%s) + 3 )) >/dev/null
for _ in $(seq 1 20); do [ -s /tmp/rpctl.log ] && break; sleep 0.5; done
check "re-arm moved the deadline; stopped via runpodctl, key in env only" 'grep -q "^stop pod podlinux1 key_in_env=yes$" /tmp/rpctl.log && ! grep -qF -f /tmp/k.txt /tmp/rpctl.log'
check "no REST stop while runpodctl works" '! grep -q "^POST" /tmp/api.log'
check "fired marker written" '[ -f /ws/killswitch/fired ] && bash $KS status | grep -q "^FIRED"'
sleep 5
check "still alive after an accepted stop -> retried" '[ "$(grep -c "^stop pod" /tmp/rpctl.log)" -ge 2 ]'
touch /tmp/rpctl.fail
for _ in $(seq 1 20); do grep -q "^POST" /tmp/api.log && break; sleep 0.5; done
check "runpodctl failing -> REST stop fallback" 'grep -q "^POST /v1/pods/podlinux1/stop auth_ok=True ua_ok=True" /tmp/api.log'
kill "$(cat /ws/killswitch/watcher.pid)"
rm -rf /ws/killswitch; mv /usr/local/bin/runpodctl /tmp/
out=$(bash $KS arm podlinux1 $far); rc=$?
check "arm refuses without runpodctl" '[ $rc = 1 ] && echo "$out" | grep -q "no runpodctl" && ! echo "$out" | grep -q " ARMED pod="'
echo "--- runpodctl log:"; cat /tmp/rpctl.log
echo "--- api log:"; cat /tmp/api.log; echo "--- killswitch log:"; cat /ws/killswitch/killswitch.log
echo "FAILS=$FAILS"; [ "$FAILS" = 0 ]
IN_CONTAINER
