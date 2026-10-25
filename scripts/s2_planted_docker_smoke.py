"""Opt-in, $0 native instrumentation smoke; importing this file starts nothing.

Run only when authorized: uv run python scripts/s2_planted_docker_smoke.py
    --image <already-local-task-image> --out <new-report.json> [--host ssh://rig]

No image pulls, credentials, network, simulator or agent invocation. The existing CLI
volume is mounted read-only, using exactly the production container extras. One disposable
512 MiB / 1 CPU container, 180 s lifetime. The smoke runs real Linux strace around a synthetic
edit/revert and tests the emitted observer hooks. Claude's actual hook dispatch remains a
Step 0 check: synthetic payload acceptance cannot establish the installed CLI's schema.
"""
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

from compaction.deepswe import sandbox as SB
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N

MEMORY_MB, CPUS, LIFETIME_S = 512, 1, 180


def preflight(image: str, host: str | None) -> str:
    """Resolve an existing immutable image id and enforce the laptop's shared limits."""
    with SB.using_host(host):
        inspected = SB._docker(["image", "inspect", image], timeout=30)
        image_id = json.loads(inspected.stdout)[0]["Id"]
        # Inspect first: docker run must never implicitly create a missing volume.
        SB._docker(["volume", "inspect", CC.CLI_VOLUME], timeout=30)
        active = SB._docker(["ps", "-q", "--filter", f"label={SB.LABEL_KEY}"], timeout=30)
        ids = active.stdout.decode().split()
        if len(ids) >= 3:
            raise RuntimeError("three emx containers already running; smoke must wait")
        states = json.loads(SB._docker(["inspect", *ids], timeout=30).stdout) if ids else []
    memory = cpus = 0
    for item in states:
        cfg = item["HostConfig"]
        if not cfg.get("Memory") or not cfg.get("NanoCpus"):
            raise RuntimeError("existing emx container has unbounded/unknown resource limits")
        memory += cfg["Memory"]
        cpus += cfg["NanoCpus"] / 1_000_000_000
    if memory + MEMORY_MB * 1024**2 > 12 * 1024**3 or cpus + CPUS > 8:
        raise RuntimeError("smoke would exceed the shared 12 GiB / 8 CPU limits")
    return image_id


def exercise(sb, directory: str) -> dict:
    """Exercise the exact observer wrapper using real syscalls, with a synthetic hook payload."""
    for tool in ("python3", "bash", "git", "curl"):
        if sb.exec("command -v " + tool, timeout_s=15).rc:
            raise RuntimeError(f"required image dependency unavailable: {tool}")
    sb.exec("mkdir -p " + shlex.quote(directory), timeout_s=15)
    path = directory + "/callsite.py"
    sb.put_file(b"pass\n", path)
    CC.install_audit_hooks(sb)
    syntax = sb.exec("bash -n " + CC.AUDIT_HOOK, timeout_s=15)
    if syntax.rc:
        raise RuntimeError("emitted native audit hook fails shell syntax check")
    settings = json.loads(sb.exec("cat " + CC.SETTINGS, timeout_s=15).stdout)
    required = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PostCompact", "SessionStart"}
    if not required.issubset(settings["hooks"]):
        raise RuntimeError("native observer hook configuration incomplete")
    audit = CC.ProbeAudit(sb, mutation_paths=[path])
    if not audit.trace_available:
        raise RuntimeError("strace exact-flags/ptrace preflight failed: " + str(audit.trace_preflight))
    program = ("from pathlib import Path; p=Path(" + repr(path) + "); "
               "p.write_text('missing_helper()\\n'); p.write_text('pass\\n'); print('SMOKE_OK')")
    original = "python3 -c " + shlex.quote(program)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "smoke-1",
               "cwd": "/app", "tool_input": {"command": original}}
    response = audit.hook(payload)
    wrapped = response["hookSpecificOutput"]["updatedInput"]["command"]
    result = sb.exec(wrapped, timeout_s=45)
    audit.hook({**payload, "hook_event_name": "PostToolUse",
                "tool_response": {"stdout": result.stdout, "stderr": result.stderr,
                                  "exitCode": result.rc}})
    transcript = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "smoke-1",
            "name": "Bash", "input": {"command": original}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "smoke-1",
            "content": result.stdout + result.stderr}]}}]
    evidence = audit.finish(transcript, submission="Smoke finished.")
    edits = [event["data"] for event in evidence["events"] if event["kind"] == "edit"]
    final = sb.exec("cat " + shlex.quote(path), timeout_s=15)
    checks = {"command_rc_zero": result.rc == 0, "visible_output": result.stdout == "SMOKE_OK\n",
              "transient_call_captured": any("missing_helper()" in (row.get("after") or "") for row in edits),
              "revert_captured": bool(edits) and edits[-1]["after"] == "pass\n",
              "final_file_restored": final.rc == 0 and final.stdout == "pass\n",
              "capture_complete": evidence["capture_complete"],
              "part_a_complete": evidence["part_a_complete"]}
    return {"ok": all(checks.values()), "checks": checks, "evidence": evidence,
            "hook_payload_source": "synthetic; live Claude hook dispatch must pass Step 0"}


def run(image: str, out: Path, host: str | None = None) -> dict:
    out = Path(out)
    if out.exists():
        raise FileExistsError(out)
    report = {"ok": False, "image": image, "host": host, "started_utc": N.utc(),
              "paid_calls": 0, "agent_calls": 0, "network": "none",
              "production_container_extras": list(CC.run_args()),
              "capabilities_seccomp": "production defaults from SB.run_args"}
    inv = SB.Invocation(purpose="planted-audit-smoke", host=host)
    try:
        image_id = preflight(image, host)
        report["image_id"] = image_id
        sb = N.start_task_container(inv, image_id, network="none", cpus=CPUS,
            memory_mb=MEMORY_MB, lifetime_s=LIFETIME_S,
            extra_args=(*CC.run_args(), "--pull", "never", "--entrypoint", ""))
        report["container"] = sb.name
        report.update(exercise(sb, f"/app/.emx-audit-smoke-{inv.run_id}"))
    except Exception as exc:  # noqa: BLE001 - write failure evidence and always clean up
        report.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            bad = inv.cleanup()
        except Exception as exc:  # noqa: BLE001 - write failure evidence and always clean up
            bad = [f"cleanup error: {type(exc).__name__}: {exc}"]
        report.update(cleanup_unconfirmed=bad, ended_utc=N.utc())
        report["ok"] = report["ok"] and not bad
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--host")
    args = parser.parse_args(argv)
    report = run(args.image, args.out, args.host)
    print(json.dumps({"ok": report["ok"], "report": str(args.out), "error": report.get("error")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
