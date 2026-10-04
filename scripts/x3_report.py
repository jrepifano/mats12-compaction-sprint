"""X3 stop-point report draft (implementation plan U7b; prereg §7.3-7.4). Free.

  uv run --group swarm python scripts/x3_report.py --stop {smoke,cost_checkpoint,main}

Writes results/X3/report_draft_<stop>.md from whatever exists: spend against the $45 cap (live
ledger via s4world.budget.status, plus the cost-checkpoint snapshot if present), analysis.json's
numbers if present, the full-study pricing and power tables (x3.pricing), and 10 transcripts per
model drawn at random (numpy default_rng(20261006)) from completed episode artifacts, sorted by
path. Episodes that ended in api_error are never drawn. Nothing is hand-picked.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from s4world import budget
from x3 import contracts as C
from x3 import pricing as P

STOPS = ("smoke", "cost_checkpoint", "main")
N_SAMPLE = 10
TOOL_OUT_CHARS = 1_500
REASONING_CHARS = 3_000
ANALYSIS_CHARS = 20_000


# --- transcript sample ---------------------------------------------------------------------
def completed_episodes(episodes_dir: Path) -> dict[str, list[Path]]:
    """Sorted artifact paths by model, excluding errored or unreadable episodes."""
    out: dict[str, list[Path]] = {}
    for p in sorted(episodes_dir.rglob("*.json")):
        try:
            a = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if a.get("end_reason") in (None, C.EndReason.API_ERROR.value):
            continue
        out.setdefault(a["meta"]["model"], []).append(p)
    return out


def sample_paths(episodes_dir: Path, n: int = N_SAMPLE,
                 seed: int = C.SEED_REPORT_SAMPLE) -> dict[str, list[Path]]:
    """One generator, models in sorted order, n draws without replacement per model."""
    rng = np.random.default_rng(seed)
    out = {}
    for model, paths in sorted(completed_episodes(episodes_dir).items()):
        k = min(n, len(paths))
        idx = rng.choice(len(paths), size=k, replace=False)
        out[model] = [paths[i] for i in sorted(idx)]
    return out


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more chars truncated]"


def _content_parts(content) -> tuple[str, str]:
    """(visible text, reasoning text) from an Inspect message's content."""
    if isinstance(content, str):
        return content, ""
    text, reas = [], []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "reasoning":
            reas.append(part.get("reasoning") or part.get("summary") or "")
        elif part.get("type") == "text":
            text.append(part.get("text") or "")
    return "\n".join(text), "\n".join(reas)


def _fence(s: str) -> str:
    return "```\n" + s.replace("```", "'''") + "\n```"


def render_transcript(a: dict, cheat: str | None = None) -> str:
    m = a["meta"]
    cost = sum((c.get("billed_usd") or 0.0) for c in a.get("model_calls") or [])
    lines = [f"#### {m['episode_id']}", "",
             f"Context {m['context']}, task {m['task_id']}, repeat {m['repeat']}, stage "
             f"{m['stage']}, prompt {m['prompt_level']}. End {a.get('end_reason')}, "
             f"{a.get('n_turns')} turns, ${cost:.4f} billed"
             + (f", detector cheat {cheat}" if cheat is not None else "") + ".", ""]
    for msg in a.get("messages") or []:
        role = msg.get("role", "?")
        text, reas = _content_parts(msg.get("content"))
        if role == "system":
            lines += [f"**system** ({len(text)} chars, omitted)", ""]
            continue
        if role == "tool":
            lines += ["**tool output**", "", _fence(_trunc(text, TOOL_OUT_CHARS)), ""]
            continue
        lines.append(f"**{role}**")
        lines.append("")
        if reas:
            lines += ["*reasoning:*", "", _fence(_trunc(reas, REASONING_CHARS)), ""]
        if text.strip():
            lines += [text.strip(), ""]
        for tc in msg.get("tool_calls") or []:
            args = tc.get("arguments") or {}
            cmd = (args.get("command", args.get("cmd")) if isinstance(args, dict)
                   else None)       # task.py's bash tool takes `command`
            shown = cmd if cmd is not None else json.dumps(args, ensure_ascii=False)
            lines += [f"*tool call `{tc.get('function')}`:*", "", _fence(str(shown)), ""]
    probe = a.get("probe") or {}
    lines += ["**probe reply**" + (f" (status {probe.get('status')})" if probe else ""), "",
              _fence(probe.get("reply") or "(none)"), ""]
    return "\n".join(lines)


def _detector() -> dict[str, str]:
    if not C.DETECTOR_CSV.exists():
        return {}
    with open(C.DETECTOR_CSV, newline="") as f:
        return {r["episode_id"]: r.get("cheat", "") for r in csv.DictReader(f)}


def transcripts_section(episodes_dir: Path) -> str:
    det = _detector()
    done = completed_episodes(episodes_dir)
    picks = sample_paths(episodes_dir)
    lines = ["## Random transcripts", "",
             (f"Up to {N_SAMPLE} per model, drawn with numpy.random.default_rng("
              f"{C.SEED_REPORT_SAMPLE}).choice over completed episode artifacts sorted by path "
              "(api_error episodes excluded), models in sorted order. Not hand-picked. Tool "
              f"outputs truncated to {TOOL_OUT_CHARS} chars."), ""]
    if not picks:
        return "\n".join(lines + ["No completed episodes yet.", ""])
    for model, paths in picks.items():
        n_all = len(done.get(model, []))
        lines += [f"### {model} ({len(paths)} of {n_all} completed)", ""]
        for p in paths:
            a = json.loads(p.read_text())
            cheat = det.get(a["meta"]["episode_id"]) if det else None
            lines += [render_transcript(a, cheat or None), ""]
    return "\n".join(lines)


# --- other sections ------------------------------------------------------------------------
def spend_section(note: dict | None) -> str:
    lines = ["## Spend against the cap", ""]
    try:
        st = budget.status(C.EXP_ID, C.HARD_CAP_USD)
        lines.append(f"Ledger now: spent ${st['spent']:.2f}, outstanding reservations "
                     f"${st['outstanding']:.2f}, remaining ${st['remaining']:.2f} of "
                     f"${C.HARD_CAP_USD:.0f}.")
    except Exception as e:  # noqa: BLE001 - the report must still render
        lines.append(f"Ledger unreadable ({type(e).__name__}: {e}).")
    if note and note.get("ledger"):
        led = note["ledger"]
        lines.append(f"At the cost checkpoint ({note.get('utc')}): spent ${led['spent']:.2f}, "
                     f"outstanding ${led['outstanding']:.2f}.")
    if note and note.get("costs"):
        lines += ["", ("| Model | Episodes | $/ep mean | median | p90 | turns | in tok | out tok"
                       " | reasoning tok |"), "|---|---|---|---|---|---|---|---|---|"]
        for m, c in note["costs"].items():
            def f(x, p=4):
                return "" if x is None else f"{x:.{p}f}"
            lines.append(f"| {m} | {c['n_episodes']} | {f(c['usd_mean'])} | "
                         f"{f(c['usd_median'])} | {f(c['usd_p90'])} | {f(c['turns_mean'], 1)} | "
                         f"{f(c['input_tokens_mean'], 0)} | {f(c['output_tokens_mean'], 0)} | "
                         f"{f(c['reasoning_tokens_mean'], 0)} |")
    return "\n".join(lines + [""])


def analysis_section(analysis_path: Path) -> str:
    lines = ["## Analysis", ""]
    if not analysis_path.exists():
        return "\n".join(lines + [f"No {analysis_path.name} yet.", ""])
    text = json.dumps(json.loads(analysis_path.read_text()), indent=1)
    lines += [f"From `{analysis_path.name}` (verbatim JSON"
              + (", truncated" if len(text) > ANALYSIS_CHARS else "") + "):", "",
              "```json", _trunc(text, ANALYSIS_CHARS), "```", ""]
    return "\n".join(lines)


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


def build_report(stop: str, *, n_sims: int = 2000) -> str:
    if stop not in STOPS:
        raise ValueError(f"stop must be one of {STOPS}")
    note_path = C.COST_CHECKPOINT.with_suffix(".json")
    note = _load(note_path)
    payload = P.build_payload(note_path, C.ANALYSIS_JSON, C.EPISODES_CSV, n_sims=n_sims)
    parts = [f"# X3 pilot report draft: stop `{stop}`", "",
             (f"Generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} by "
              "scripts/x3_report.py. Draft for Jacob; every number below comes from the files "
              "named, and lines marked estimated were not measured by the pilot."), "",
             spend_section(note), analysis_section(C.ANALYSIS_JSON),
             P.payload_markdown(payload, heading="##"), transcripts_section(C.EPISODES_DIR)]
    return "\n".join(parts)


def write_report(stop: str, *, n_sims: int = 2000) -> Path:
    out = C.RESULTS_DIR / f"report_draft_{stop}.md"
    text = build_report(stop, n_sims=n_sims)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--stop", choices=STOPS, required=True)
    ap.add_argument("--n-sims", type=int, default=2000)
    a = ap.parse_args(argv)
    print(f"wrote {write_report(a.stop, n_sims=a.n_sims)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
