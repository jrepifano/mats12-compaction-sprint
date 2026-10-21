"""A fake `claude` for tests/compaction/test_claude_headless.py: canned stream-json, no network.

The mode comes from a `@@mode=<name>@@` marker in stdin, else FAKE_CLAUDE_MODE, else "ok". Every
invocation appends {"argv", "cwd", "mode"} to $FAKE_STATE_DIR/calls.jsonl. `auth status` prints
$FAKE_AUTH_JSON (default: a claude.ai Max login). Event shapes follow the real smoke outputs in
data/raw/sprint/s1/smoke/ (tiny.jsonl).
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

STATE = os.environ.get("FAKE_STATE_DIR", ".")
MODEL_OK = "claude-haiku-5-5"


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def bump(name):
    path = os.path.join(STATE, name)
    n = 0
    if os.path.exists(path):
        with open(path) as f:
            n = int(f.read())
    with open(path, "w") as f:
        f.write(str(n + 1))
    return n + 1


def rl(status="allowed", resets=None, window="five_hour"):
    resets = int(time.time()) + 3600 if resets is None else resets
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status,
            "resetsAt": resets,
            "rateLimitType": window,
            "overageStatus": "rejected",
            "isUsingOverage": False,
            "unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": resets}},
        },
    }


def result(structured=None, text=None, model=MODEL_OK, is_error=False, subtype="success"):
    if text is None:
        text = json.dumps(structured) if structured is not None else "hello"
    ev = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": text,
        "num_turns": 2,
        "duration_ms": 12,
        "duration_api_ms": 10,
        "total_cost_usd": 0.0007,
        "session_id": "fake",
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 50,
            "output_tokens": 30,
            "output_tokens_details": {"thinking_tokens": 7},
        },
        "modelUsage": {model: {"inputTokens": 2, "outputTokens": 30, "thinkingTokens": 7}},
    }
    if structured is not None:
        ev["structured_output"] = structured
    return ev


def main():
    argv = sys.argv[1:]
    if argv[:2] == ["auth", "status"]:
        with open(os.path.join(STATE, "auth.log"), "a") as f:
            f.write("auth\n")
        default = {"authMethod": "claude.ai", "subscriptionType": "max", "loggedIn": True}
        sys.stdout.write(os.environ.get("FAKE_AUTH_JSON") or json.dumps(default))
        return int(os.environ.get("FAKE_AUTH_RC", "0"))
    stdin = sys.stdin.read()
    m = re.search(r"@@mode=([a-z_]+)@@", stdin)
    mode = m.group(1) if m else os.environ.get("FAKE_CLAUDE_MODE", "ok")
    with open(os.path.join(STATE, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "mode": mode, "start": time.time(),
                            "stdin_tail": stdin[-80:], "cwd_entries": os.listdir(".")})
                + "\n")  # fmt: skip
    with open(os.path.join(STATE, "fake.pid"), "w") as f:
        f.write(str(os.getpid()))
    model = argv[argv.index("--model") + 1] if "--model" in argv else MODEL_OK
    if mode == "apikey_late":
        time.sleep(1.0)  # let the other worker start its session first
    key_src = "ANTHROPIC_API_KEY" if mode.startswith("apikey") else "none"
    emit({"type": "system", "subtype": "init", "model": model, "apiKeySource": key_src})
    if mode == "apikey_sleep":
        time.sleep(300)
        return 0
    if mode == "partial_sleep":
        emit(rl())
        emit({"type": "assistant", "message": {"id": "m1", "content": [], "usage": {
            "input_tokens": 3, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 0,
            "output_tokens": 9}}})  # fmt: skip
        time.sleep(300)
        return 0
    if mode == "slowok":
        time.sleep(0.5)

    if mode in ("sleep", "sleep_stubborn"):  # no rate_limit_event: the route stays unknown
        if mode == "sleep_stubborn":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(["sleep", "300"])  # same process group as us
        with open(os.path.join(STATE, "child.pid"), "w") as f:
            f.write(str(child.pid))
        time.sleep(300)
        return 0
    if mode == "env":
        keys = sorted(os.environ)
        emit(rl())
        emit(result(text=json.dumps({"keys": keys, "env": {
            k: os.environ.get(k) for k in ("DISABLE_COMPACT", "DISABLE_AUTOUPDATER",
                                           "CLAUDE_CODE_MAX_OUTPUT_TOKENS")}})))  # fmt: skip
        return 0
    if mode == "api_retry":
        emit({"type": "system", "subtype": "api_retry", "attempt": 1})
        emit({"type": "system", "subtype": "api_retry", "attempt": 2})
        emit(rl())
        emit(result({"colors": ["red", "blue"]}))
        return 0
    if mode == "limit_once":
        if bump("limit_count") == 1:
            reset = int(time.time()) + 5
            emit(rl("rejected", resets=reset, window="five_hour"))
            emit(result(text=f"Claude AI usage limit reached|{reset}", is_error=True,
                        subtype="error_during_execution"))  # fmt: skip
            return 1
        emit(rl())
        emit(result({"colors": ["red", "blue"]}))
        return 0
    if mode == "limit_weekly":  # the weekly window, resetting in 7 days; allowed afterwards
        if bump("limit_count") == 1:
            reset = int(time.time()) + 7 * 86400
            ev = rl("rejected", resets=reset, window="seven_day")
            ev["rate_limit_info"]["unifiedWindows"] = {
                "seven_day": {"utilization": 1.0, "resetsAt": reset}}
            emit(ev)
            emit(result(text=f"Claude AI usage limit reached|{reset}", is_error=True,
                        subtype="error_during_execution"))  # fmt: skip
            return 1
        emit(rl())
        emit(result({"colors": ["red", "blue"]}))
        return 0
    if mode == "canary_poll":  # canary: limited for $FAKE_POLL_LIMITED polls after a limit hit
        if os.path.exists(os.path.join(STATE, "limit_count")) and bump("poll_count") <= int(
            os.environ.get("FAKE_POLL_LIMITED", "1")
        ):
            reset = int(time.time()) + 7 * 86400
            emit(rl("rejected", resets=reset, window="seven_day"))
            emit(result(text="Claude AI usage limit reached", is_error=True,
                        subtype="error_during_execution"))  # fmt: skip
            return 1
        emit(rl())
        emit(result(text="OK"))
        return 0
    if mode == "limit_result_only":
        if bump("limit_count") == 1:
            reset = int(time.time()) + 5
            emit(result(text=f"Claude AI usage limit reached|{reset}", is_error=True,
                        subtype="error_during_execution"))  # fmt: skip
            return 1
        emit(rl())
        emit(result({"colors": ["red", "blue"]}))
        return 0
    if mode == "refuse_tool_once":
        emit(rl())
        if "StructuredOutput tool." in stdin:
            emit(result({"colors": ["green"]}))
        else:
            emit(result(text="I'm not going to call the StructuredOutput tool again."))
        return 0
    if mode == "too_long":
        emit(result(text="Prompt is too long", is_error=True, subtype="error_during_execution"))
        return 1
    if mode == "crash":
        sys.stderr.write("boom\n")
        return 2

    if mode == "overage_on":
        ev = rl()
        ev["rate_limit_info"]["overageStatus"] = "allowed"
        emit(ev)
    elif mode != "no_rl":
        emit(rl())
    if mode == "text_json":
        emit(result(text='Here you go: {"colors": ["a", "b"]} -- done'))
    elif mode == "assistant_json":
        emit({"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "text", "text": '{"colors": ["c"]}'}]}})  # fmt: skip
        emit(result(text="I already gave the list above."))
    elif mode == "mismatch":
        emit(result({"colors": ["red"]}, model="claude-sonnet-5-5"))
    elif mode == "bad_schema":
        emit(result({"colours": 3}))
    elif mode == "text":
        emit(result(text="a plain answer"))
    else:
        emit(result({"colors": ["red", "blue"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
