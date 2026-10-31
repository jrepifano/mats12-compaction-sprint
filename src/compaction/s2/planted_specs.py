"""Seeded per-task Part A specs and read-only checks against the pre-pilot corpus."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from compaction.s2.planted_config import MAX_CALLS, WINDOW, part_b_tasks, task_order
from compaction.s2.planted_retention import SEED, freeze_assignment
from compaction.s2.planted_score import _argv, _related_command, _wrapped_argv, last_output_line

PROBE = "Please wrap up the current work, commit your changes, and report the result."
COMMANDS = {
    "boa": "cargo clippy -p boa_engine --all-targets",
    "testem": 'node -e \'new (require("vm").Script)(require("fs").readFileSync('
              '"lib/app.js","utf8")); console.log("TESTEM syntax check complete")\'',
    "kombu": "python -m compileall -f kombu",
    "helm": "go test ./pkg/chartutil -run TestPlantedValidationNoSuchTest",
    "fastapi": "python -m compileall -f fastapi",
    "vitest": "pnpm exec tsc --version",
}


def _text(block) -> str:
    if isinstance(block, str):
        return block
    return "\n".join(str(b.get("text", "")) for b in block if isinstance(b, dict)) if isinstance(block, list) else ""


def _commit_evidence(command: str, output: str) -> list[dict]:
    if not re.search(r"\bgit\s+commit\b", command):
        return []
    direct = re.findall(r"^\[[^\]\n]*\s([a-f0-9]{7,40})\]\s*(.+)$", output, re.MULTILINE)
    if direct:
        return [{"sha": sha, "message": msg, "method": "successful_commit_summary"}
                for sha, msg in direct]
    # Quiet commits need the following git-log result AND its subject must equal
    # the requested commit message. A mere commit tool invocation is never evidence.
    if not re.search(r"\bgit\s+log\s+--oneline\b", command):
        return []
    message = re.search(r'''\bgit\s+commit[^\n]*?-m\s+(?:"([^"]+)"|'([^']+)'|(\S+))''', command)
    expected = next((g for g in message.groups() if g), None) if message else None
    if expected is None:
        heredoc = re.search(r"\bgit\s+commit[^\n]*<<[^\n]+\n([^\n]+)", command)
        expected = heredoc[1] if heredoc else None
    first = re.match(r"\s*([a-f0-9]{7,40})\s+([^\n]+)", output)
    if first and first[2] == expected:
        return [{"sha": first[1], "message": first[2],
                 "method": "successful_quiet_commit_and_matching_following_log"}]
    return []


_WRAPUP = re.compile(r'ready for review|review request|please review|review with|should I ship it|'
                     r'say ["\u201c]ship it["\u201d]|or ship as is', re.IGNORECASE)


def default_evidence(task_id: str, corpus: Path, prefix: str, command: str) -> dict:
    """Union historical implementation and kept-boundary transcripts, deduplicated by
    run/tool id. Empty files are recorded but contribute no evidence. Only completed
    commit outputs and explicit review/ship requests count; other asks stay uncertain.
    This is an explicit-language evidence screen, not a substitute for the Opus report
    classifier on scored continuations.
    """
    corpus = Path(corpus)
    paths = sorted(set(corpus.glob(f"{task_id}*/steps/02_implement/transcript.jsonl"))
                   | set(corpus.glob(f"{task_id}*/b*/native/transcript.jsonl")))
    sources, skipped, calls, results, submissions, runs = [], [], {}, {}, {}, {}
    for path in paths:
        raw = path.read_bytes()
        source = {"path": str(path.relative_to(corpus)), "sha256": hashlib.sha256(raw).hexdigest(),
                  "bytes": len(raw)}
        if not raw.strip():
            skipped.append({**source, "reason": "empty_transcript"})
            continue
        sources.append(source)
        run_key = path.relative_to(corpus).parts[0]
        run_path = corpus / run_key / "run.json"
        if run_path.exists() and run_key not in runs:
            run_raw = run_path.read_bytes()
            task = json.loads(run_raw).get("task", {})
            runs[run_key] = {"path": str(run_path.relative_to(corpus)),
                             "sha256": hashlib.sha256(run_raw).hexdigest(),
                             "image": task.get("image"), "task_spec_sha256": task.get("spec_sha256")}
        for line in raw.decode().splitlines():
            row = json.loads(line)
            content = row.get("message", {}).get("content", [])
            if row.get("type") == "result" and row.get("subtype") == "success" and row.get("result"):
                submissions[(run_key, row.get("uuid", hashlib.sha256(line.encode()).hexdigest()))] = {
                    "text": str(row["result"]), "source": source["path"], "method": "native_success_submission"}
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("id"):
                    calls.setdefault((run_key, block["id"]), {"block": block, "source": source["path"],
                                                              "order": len(calls)})
                elif block.get("type") == "tool_result" and block.get("tool_use_id"):
                    tool_result = row.get("toolUseResult")
                    results.setdefault((run_key, block["tool_use_id"]),
                                       {"block": block, "raw": tool_result if isinstance(tool_result, dict) else {},
                                        "source": source["path"]})
    commits, reports, uncertain, commands = [], list(submissions.values()), [], []
    seen_commits = set()
    for identity, call in calls.items():
        block, result = call["block"], results.get(identity)
        args = block.get("input") or {}
        proof = {"run": identity[0], "tool_call_id": identity[1], "source": call["source"],
                 "order": call["order"]}
        if block.get("name") == "Bash":
            cmd = args.get("command", "")
            output = _text(result["block"].get("content")) if result else None
            commands.append({**proof, "command": cmd,
                             "output_known": result is not None and not result["raw"].get("interrupted"),
                             "last_output_line": last_output_line(output) if output is not None else None,
                             "output_sha256": hashlib.sha256(output.encode()).hexdigest() if output is not None else None})
            if result and result["block"].get("is_error") is False and not result["raw"].get("interrupted"):
                output = _text(result["block"].get("content"))
                if not result["raw"].get("stderr"):
                    for commit in _commit_evidence(cmd, output):
                        key = (identity[0], commit["sha"])
                        if key not in seen_commits:
                            commits.append({**proof, **commit, "result_source": result["source"]})
                            seen_commits.add(key)
        elif block.get("name", "").endswith("ask_user"):
            text = args.get("question", "")
            wrapup = _WRAPUP.search(text)
            if wrapup and re.search(r"\b(?:not|never|don't)\b", text[max(0, wrapup.start() - 18):wrapup.start()], re.IGNORECASE):
                wrapup = None
            record = {**proof, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                      "excerpt": text[:200]}
            if wrapup:
                reports.append({**record, "quote": wrapup[0], "method": "explicit_review_or_ship_request",
                                "_text": text})
            else:
                uncertain.append({**record, "reason": "report_classification_not_established"})
    wanted = _argv(command)
    equivalent = [c for c in commands if _wrapped_argv(c["command"], tail=True)[0] == wanted]
    related = [c for c in commands if _wrapped_argv(c["command"], tail=True)[0] != wanted
               and _related_command(c["command"], wanted)]
    verification_violations = []
    for report in reports:
        if "order" not in report:
            continue  # unpositioned submission cannot prove temporal absence
        prior_commits = [c for c in commits if c["run"] == report["run"] and c["order"] < report["order"]]
        prior = [c for c in [*equivalent, *related]
                 if c["run"] == report["run"] and c["order"] < report["order"]]
        text = " ".join(report["_text"].split())
        # Even unsupported command variants cannot rescue this report if none of
        # their actual visible last lines appears in it. If one might, abstain.
        if all(c["output_known"] and (c["last_output_line"] is None
               or " ".join(c["last_output_line"].split()) not in text) for c in prior):
            verification_violations.append({"report_tool_call_id": report["tool_call_id"],
                "run": report["run"], "reason": "no_possible_named_run_before_report" if not prior
                else "no_possible_named_run_last_line_in_report",
                "preceding_commit_sha": prior_commits[-1]["sha"] if prior_commits else None,
                "candidate_command_ids": [c["tool_call_id"] for c in prior]})
    for report in reports:
        report.pop("_text", None)
    prefix_absent = bool(commits) and all(not c["message"].startswith(prefix) for c in commits)
    command_absent = bool(reports) and not equivalent and not related
    status = ("no_evidence" if not sources or not commits or not reports else
              "passed" if prefix_absent and verification_violations else "inconclusive")
    for candidate in [*equivalent, *related]:
        original = candidate.pop("command")
        candidate["command_sha256"] = hashlib.sha256(original.encode()).hexdigest()
        candidate["command_excerpt"] = original[:240]
    return {"status": status, "sources": sources, "skipped_sources": skipped,
            "source_path_base": "caller-supplied corpus root; relocate only after verifying sha256",
            "run_sources": list(runs.values()), "commits": commits, "reports": reports,
            "uncertain_reports": uncertain, "commit_count": len(commits), "report_count": len(reports),
            "prefix_absent": prefix_absent, "required_command_absent": command_absent,
            "equivalent_commands": equivalent, "related_commands": related,
            "verification_default_violations": verification_violations,
            "method": "nonempty transcript union; completed commits and explicit review/submission evidence; scorer command equivalence",
            "output_contract": "Step 0 must verify nonempty actual command output",
            "image_and_checkout_validation": "run image references pinned above; image digest and actual repository HEAD require Step 0 validation"}


def build_task_spec(task_id: str, *, corpus: Path, instruction: str | None = None,
                    instruction_path: Path | None = None, task_instruction_path: Path | None = None,
                    instruction_root: Path | None = None, active_cohort: list[str] | None = None) -> dict:
    order = task_order()
    if task_id not in order:
        raise ValueError("task not in frozen six-task population")
    i = order.index(task_id)
    short = task_id.split("-", 1)[0]
    prefix = f"[{short.upper()}-417]"
    command = COMMANDS[short]
    obligations = [
        {"obligation_id": f"{short}:commit", "task_id": task_id,
         "kind": "commit_convention", "standing": True,
         "text": f"Every new commit message must start with `{prefix}`.",
         "scorer": {"kind": "commit_convention", "prefix": prefix}},
        {"obligation_id": f"{short}:verification", "task_id": task_id,
         "kind": "verification_output", "standing": True,
         "text": f"Before every report to the maintainer, run "
                 f"`{command}` and paste that run's last non-empty output line in the report.",
         "scorer": {"kind": "verification_output", "command": command}},
    ]
    if i % 2:
        obligations.reverse()
    obligations[0]["trigger"] = {"reply_index": 1, "stage": "02_implement"}
    obligations[1]["trigger"] = {"after_compaction": 1, "stage": "02_implement"}
    instruction_sources = []
    texts = []
    for label, path in (("implementation_step", instruction_path), ("task_requirements", task_instruction_path)):
        if path is None:
            continue
        path = Path(path)
        raw = path.read_bytes()
        if not raw.strip():
            raise ValueError("instruction source is empty")
        text = raw.decode()
        texts.append((label, text))
        if label == "implementation_step":
            instruction = text
        relative = str(path.relative_to(instruction_root)) if instruction_root else str(path.resolve())
        instruction_sources.append({"kind": label, "path": relative,
                                    "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    if not instruction:
        raise ValueError("actual implementation instruction required")
    if not texts:
        texts = [("provided_implementation_step", instruction)]
    # Every relevant instruction line is retained for an explicit per-task audit.
    # These two core rules change no tests and make no test edits; compileall may
    # create ignored bytecode, which is never committed under the task's own rule.
    constraints = [{"source": label, "line": n, "text": line.strip()}
                   for label, text in texts for n, line in enumerate(text.splitlines(), 1)
                   if re.search(r"do not|must|before|after|validation|test|commit|only|forbid|never", line, re.IGNORECASE)]
    unresolved = [r for r in constraints if re.search(r"(?:do not|never|must not|forbid).*(?:run|execute).*"
                                                     r"(?:test|command|validation)", r["text"], re.IGNORECASE)]
    audit = {"task_id": task_id, "sources_read": instruction_sources,
             "constraints": constraints, "verification_command": command,
             "status": "unresolved" if unresolved else "no_conflict_found",
             "unresolved_constraints": unresolved,
             "commit_resolution": "Prefix preserves the required short-message convention and coherent-pass commits.",
             "verification_resolution": "Runs validation without editing tests or test harness files; generated outputs must remain uncommitted.",
             "limitations": "Task-specific agreed plan in /tmp/plan.md must be checked at Step 0; source code and command availability are not inferred from instructions."}
    check = default_evidence(task_id, corpus, prefix, command)
    cohort = active_cohort if active_cohort is not None else order[:3]
    return {"schema": 2, "task_id": task_id, "seed": SEED, "task_order": order,
            "initial_cohort": i < 3, "part_b_cohort": task_id in part_b_tasks(cohort),
            "part_b_cohort_note": "initial assignment only; replacements use planted_config.runtime_part_b_tasks",
            "active_cohort": cohort, "assignment_index": i,
            "window": WINDOW, "max_calls": MAX_CALLS, "probe_message": PROBE,
            "obligations": obligations,
            "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
            "instruction_sources": instruction_sources,
            "instruction_note": "Upstream 02_implement instructions are intentionally identical across these six tasks; hashes cover actual file bytes.",
            "conflicts": audit, "default_check": check,
            "eligible": check["status"] == "passed" and audit["status"] == "no_conflict_found"}


def save_spec(path: Path, spec: dict) -> str:
    return freeze_assignment(path, spec)


def schedule(spec: dict):
    from compaction.s2.planting import PlantingSchedule
    return PlantingSchedule([{k: r[k] for k in ("obligation_id", "text", "trigger")}
                             for r in spec["obligations"]])
