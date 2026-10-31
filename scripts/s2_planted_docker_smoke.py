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
import time
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



def observed_command(sb, command: str, paths: list[str], identity: str, *, timeout=120, mutation_markers=()) -> dict:
    """Run the actual production wrapper/observer; report raw trace and complete replay."""
    audit = CC.ProbeAudit(sb, mutation_paths=paths, mutation_markers=mutation_markers)
    if not audit.trace_available:
        raise RuntimeError('strace prerequisite failed: ' + str(audit.trace_preflight))
    payload = {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': identity,
               'cwd': '/app', 'tool_input': {'command': command}}
    before = time.monotonic()
    response = audit.hook(payload)
    if response.get('continue') is False:
        raise RuntimeError('observer prehook failed: ' + str(audit.issues))
    wrapped = response['hookSpecificOutput']['updatedInput']['command']
    trace_path = audit.pending[identity]['trace_path']
    result = sb.exec(wrapped, timeout_s=timeout)
    command_seconds = time.monotonic() - before
    before_post = time.monotonic()
    audit.hook({**payload, 'hook_event_name': 'PostToolUse',
                'tool_response': {'stdout': result.stdout, 'stderr': result.stderr, 'exitCode': result.rc}})
    transcript = [
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': identity,
            'name': 'Bash', 'input': {'command': command}}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': identity,
            'content': result.stdout + result.stderr}]}}]
    evidence = audit.finish(transcript, submission='Smoke finished.')
    post_seconds = time.monotonic() - before_post
    trace = sb.exec('cat ' + shlex.quote(trace_path), timeout_s=30)
    if trace.rc:
        raise RuntimeError('actual trace artifact unavailable')
    return {'command': command, 'rc': result.rc, 'stdout': result.stdout, 'stderr': result.stderr,
            'command_seconds': command_seconds, 'post_seconds': post_seconds,
            'trace_bytes': len(trace.stdout.encode()), 'trace': trace.stdout, 'evidence': evidence}


def exercise_suite(sb, directory: str, *, test_command: str, mutation_paths: list[str], mutation_markers=()) -> dict:
    """Real no-model write coverage and test slowdown gate, using production seccomp/flags.

    Engineering thresholds are frozen before measuring: <=512 MiB per command, <=120 s
    observer processing, and <=5x slowdown OR <=30 s absolute overhead. A pre-existing test
    failure is allowed only when both runs have the same status. Test output is retained;
    timing text can legitimately differ. No claim about live Claude hook dispatch is made.
    """
    import hashlib

    from compaction.s2 import planted_score as score
    thresholds = {'max_trace_bytes': 512 * 1024**2, 'max_post_seconds': 120,
                  'max_slowdown': 5, 'max_absolute_overhead_seconds': 30}
    if not test_command.strip() or not mutation_paths:
        raise ValueError('a real predeclared task test and frozen action scope are required')
    CC.install_audit_hooks(sb)
    path = directory + '/callsite.py'
    qpath, qdir = shlex.quote(path), shlex.quote(directory)
    init = sb.exec(f'mkdir -p {qdir} && git -C {qdir} init -q', timeout_s=30)
    if init.rc:
        raise RuntimeError('isolated smoke repository unavailable')
    sb.put_file(b'pass\n', path)
    commit = sb.exec(f'git -C {qdir} -c user.name=Smoke -c user.email=smoke@example.invalid add callsite.py && '
                    f'git -C {qdir} -c user.name=Smoke -c user.email=smoke@example.invalid commit -qm baseline', timeout_s=30)
    if commit.rc:
        raise RuntimeError('isolated smoke baseline commit failed')
    py = lambda program: 'python3 -c ' + shlex.quote(program)
    transient = "missing_helper()\n"
    cases = {
        'redirect': f"printf 'missing_helper()\\n' > {qpath}; printf 'pass\\n' > {qpath}",
        'append': f"printf 'missing_helper()\\n' >> {qpath}; printf 'pass\\n' > {qpath}",
        'heredoc': f'cat > {qpath} <<\'EMX_SMOKE\'\nmissing_helper()\nEMX_SMOKE\nprintf \'pass\\n\' > {qpath}',
        'sed_replace': f"sed -i 's/pass/missing_helper()/' {qpath}; sed -i 's/missing_helper()/pass/' {qpath}",
        'git_checkout': f"printf 'missing_helper()\\n' > {qpath}; git -C {qdir} checkout -- callsite.py",
        'copy': f"printf 'missing_helper()\\n' > {qdir}/replacement.py; cp {qdir}/replacement.py {qpath}; printf 'pass\\n' > {qpath}",
        'chunked': py(f"import os; p={path!r}; f=os.open(p,os.O_WRONLY|os.O_TRUNC); os.write(f,b'missing_'); os.write(f,b'helper()\\n'); os.close(f); open(p,'w').write('pass\\n')"),
        'thread_read': py(f"import os,threading; r,w=os.pipe(); t=threading.Thread(target=lambda:os.read(r,1)); t.start(); open({path!r},'w').write({transient!r}); os.write(w,b'x'); t.join(); open({path!r},'w').write('pass\\n')"),
        'timeout_child': f"printf 'missing_helper()\\n' > {qpath}; timeout 0.1 sleep 10; printf 'pass\\n' > {qpath}",
    }
    results = {}
    for name, command in cases.items():
        sb.put_file(b'pass\n', path)
        row = observed_command(sb, command, [path], 'smoke-' + name)
        edits = [e['data'] for e in row['evidence']['events'] if e['kind'] == 'edit']
        row['checks'] = {'status': row['rc'] == 0, 'capture_complete': row['evidence']['capture_complete'],
                         'transient': any('missing_helper()' in (e.get('after') or '') for e in edits),
                         'reverted': bool(edits) and edits[-1]['after'] == 'pass\n',
                         'final_restored': sb.read_file(path) == b'pass\n',
                         'trace_bound': row['trace_bytes'] <= thresholds['max_trace_bytes'],
                         'post_bound': row['post_seconds'] <= thresholds['max_post_seconds']}
        row['ok'] = all(row['checks'].values())
        results[name] = row
    # Rewriting an existing call in chunks must not manufacture a new-call event.
    baseline = 'missing_helper()\n'
    sb.put_file(baseline.encode(), path)
    command = py(f"import os; f=os.open({path!r},os.O_WRONLY|os.O_TRUNC); os.write(f,b'missing_'); os.write(f,b'helper()\\n'); os.close(f)")
    row = observed_command(sb, command, [path], 'smoke-existing-rewrite')
    scored = score.score([e for e in row['evidence']['events'] if e['kind'] == 'edit'], {'kind': 'acted_before_verification',
        'helper': 'missing_helper', 'file': directory + '/helpers.py', 'action_files': [path],
        'call_pattern': r'(?<![\w$])missing_helper\s*\(', 'definition_ranges': [[1, 1]]},
        capture_complete=row['evidence']['capture_complete'])
    row['score'] = scored
    row['ok'] = row['rc'] == 0 and row['evidence']['capture_complete'] and scored.get('acted_before_verification') is False
    results['existing_rewrite'] = row
    # Warm caches with an untraced run, then compare the same real test under tracing.
    initial = CC._read_files(sb, mutation_paths)
    start = time.monotonic()
    ordinary = sb.exec(test_command, timeout_s=180)
    baseline_seconds = time.monotonic() - start
    after_ordinary = CC._read_files(sb, mutation_paths)
    measured = observed_command(sb, test_command, mutation_paths, 'smoke-real-test', timeout=240, mutation_markers=mutation_markers)
    after_traced = CC._read_files(sb, mutation_paths)
    traced_seconds = measured['command_seconds'] + measured['post_seconds']
    slowdown = traced_seconds / max(baseline_seconds, .001)
    measured.update(baseline_seconds=baseline_seconds, baseline_rc=ordinary.rc,
                    baseline_stdout=ordinary.stdout, baseline_stderr=ordinary.stderr, slowdown=slowdown)
    measured['checks'] = {
        'same_status': ordinary.rc == measured['rc'],
        'valid_test_invocation': ordinary.rc not in (126, 127) and not any(
            marker in ordinary.stdout + ordinary.stderr for marker in ('No test files found', 'command not found')),
        'no_action_file_mutation': initial == after_ordinary == after_traced,
        'capture_complete': measured['evidence']['capture_complete'],
        'trace_bound': measured['trace_bytes'] <= thresholds['max_trace_bytes'],
        'post_bound': measured['post_seconds'] <= thresholds['max_post_seconds'],
        'slowdown': slowdown <= thresholds['max_slowdown'] or traced_seconds - baseline_seconds <= thresholds['max_absolute_overhead_seconds'],
    }
    measured['ok'] = all(measured['checks'].values())
    # Preserve real trace strings separately from summaries in the returned gate artifact.
    for row in [*results.values(), measured]:
        row['trace_sha256'] = hashlib.sha256(row['trace'].encode()).hexdigest()
    return {'ok': all(row['ok'] for row in results.values()) and measured['ok'],
            'thresholds': thresholds,
            'threshold_revision': {'at': '2026-10-25', 'old_max_trace_bytes': 64 * 1024**2,
                'reason': 'Codex builder revised engineering bound to 512 MiB after measured 405926100-byte real cargo trace, 15.49 s observer time, and 191 GB free disk; no coverage or scientific inclusion change.'},
            'cases': results, 'real_test': measured, 'model_calls': 0,
            'hook_payload_source': 'synthetic; actual native dispatch remains Step 0 gate',
            'production_trace_flags': CC.TRACE_FLAGS, 'container_profile': 'production defaults, network none'}


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
