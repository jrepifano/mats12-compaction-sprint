"""Results table of a native arm (Claude Code or Antigravity) of the accumulated-loss pilot:
one row per graded boundary (02_implement), from the run dirs under data/raw/sprint/s2/native/<arm>/
and their audits. Counts, grades and hashes only, no transcript text.

    uv run python scripts/s2_native_summary.py --arm claude_code|agy

Writes results/sprint/S2/native/<arm>/summary.json and summary.md. (a) is the requirement ledger
(`interact_reqs`, live summary), (b) is S1's item loss (`lossaudit`). The hand code checks are the
ones reported to the main session on 24-25 Oct (CODE_CHECKS, with where each is written up).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2 import interact_reqs as RQ
from compaction.s2 import session as SS

ARM_DIRS = {"claude_code": "claude_code", "agy": "agy"}
OUT = C.REPO_ROOT / "results" / "sprint" / "S2" / "native"
REPORT = "logs/s2/native_arms_report.md"

# (run key, boundary) -> the hand check of that boundary's losses (no model calls; kept images read
# with --network none). Written up in REPORT, "Verified by hand".
CODE_CHECKS = {
    ("vitest-duration-sharding-cc-haiku-a1", 2): "ledger 7/7 implemented in the code at b2; "
        "summary-only losses",
    ("vitest-duration-sharding-cc-haiku-a1", 3): "ledger 7/7 implemented at b3; b2-q7 'misstated' "
        "looks like a grader error (conflates r15 with the legacy migration)",
    ("helm-array-merge-strategies-cc-haiku-a2", 1): "lossaudit 8: i3/i4 followed, i11 implemented, "
        "i14 the code itself exported the helper (summary matches the code); i1-i5 lost with the "
        "partly refused summary",
    ("vitest-duration-sharding-agy-flash-a1", 1): "ledger 5/6 implemented at b1; b1-q32 not "
        "(recording on the success path only), maintainer re-raised it in r13",
    ("vitest-duration-sharding-agy-flash-a1", 2): "ledger 22/22 implemented at b2",
    ("testem-bail-on-test-failure-agy-flash-a2", 1): "sample: b1-q27 implemented at b1",
    ("testem-bail-on-test-failure-agy-flash-a2", 2): "sample of 8 (3 inherited + 5 new, seed "
        "20261025; 7 checked at b2): 6 implemented; b1-q17 partly, maintainer re-raised it in r21",
}


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _read(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _loss_files(root: Path, rk: str) -> list[Path]:
    """The run's own lossaudit, then the shared one (launchers before 87f80734 audited through
    one shared staging dir)."""
    return [d / "losses.jsonl" for d in (root / "_audit" / rk / "lossaudit",
                                         root / "_audit" / "lossaudit") if (d / "losses.jsonl").exists()]


def _boundary_losses(files: list[Path], cohort: str) -> tuple[list[dict], str | None]:
    """One boundary's lossaudit rows from the first file that has that boundary (one source per
    boundary: never both, so nothing is counted twice)."""
    for p in files:
        rows = [r for r in (json.loads(x) for x in p.read_text().splitlines() if x.strip())
                if r.get("cohort_key") == cohort]
        if rows:
            return rows, str(p.relative_to(C.REPO_ROOT))
    return [], None


def _audit_code(root: Path, rk: str) -> list[dict] | str:
    p = root / "_audit" / rk / "audit_code.jsonl"
    if not p.exists():
        return "unrecorded (audited before d035bfb4)"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def boundary_row(root: Path, rd: Path, bdir: Path, run: dict, res: dict,
                 la_files: list[Path]) -> dict:
    rk, k = run["run_key"], RQ.boundary_index(bdir)
    b = bdir / "boundary"
    comp = _read(b / "compaction.json") or {}
    meta = _read(b / "boundary.json") or {}
    pressure = {c["k"]: c for c in (res.get("compactions") or []) if c.get("step") == "02_implement"}
    rq = _read(b / "requirements.json")
    grades_f = _read(b / RQ.SCREEN_V2 / "grades.json")
    ledger = _read(b / RQ.SCREEN_V2 / "ledger.json")
    audited = isinstance(rq, dict) and isinstance(grades_f, dict) and isinstance(ledger, dict)
    grades = (grades_f or {}).get("live") or {} if audited else {}
    counts = ((ledger or {}).get("counts") or {}).get("live") or {} if audited else {}
    items = RQ.gradable_v2(rq) if audited else []
    mine, la_source = _boundary_losses(la_files, f"{rk}-b{k}")
    plan = run.get("plan") or {}
    thr = plan.get("threshold") or {}
    p = pressure.get(k) or {}
    return {
        "task": rk.split("-cc-")[0].split("-agy-")[0], "attempt": rk.rsplit("-", 1)[-1],
        "run_key": rk, "model": plan.get("model"),
        "trigger": thr.get("window") or thr.get("compact_at"), "end": res.get("end"),
        "sim_usd": round(float(res.get("sim_cost_usd") or 0), 4), "b": k,
        "pre_tokens": meta.get("pre_tokens"), "post_tokens": p.get("post_tokens"),
        "summary_chars": len(comp.get("summary") or ""),
        "kept_messages": len(comp.get("kept_indices") or []),
        "post_source": p.get("post_source") or (meta.get("post_source")),
        "quiescent": p.get("quiescent"),
        "req": None if not audited else {
            "items": len(items),
            "lost": sum(g in ("dropped", "misstated") for g in grades.values()),
            "grades": dict(Counter(grades.values())),
            "new": counts.get("new"), "inherited": counts.get("inherited"),
            "unknown": counts.get("unknown"),
            "items_by_reply": sum(RQ.extraction_of(it) == "by_reply" for it in items)
            if hasattr(RQ, "extraction_of") else None},
        "la": None if not mine else {
            "items": len(mine), "lost": sum(bool(r.get("lost")) for r in mine),
            "recoverable": dict(Counter(r.get("recoverable") for r in mine if r.get("lost"))),
            "source": la_source},
        "code_check": CODE_CHECKS.get((rk, k)),
        "provenance": {"run_code": run.get("code") or "unrecorded (launched before 87f80734)",
                       "audit_code": _audit_code(root, rk)},
        "hashes": {n: _sha(f) for n, f in (
            ("run.json", rd / "run.json"), ("result.json", rd / "result.json"),
            ("b/run.json", bdir / "run.json"), ("boundary.json", b / "boundary.json"),
            ("compaction.json", b / "compaction.json"), ("requirements.json", b / "requirements.json"),
            ("grades.json", b / RQ.SCREEN_V2 / "grades.json"),
            ("ledger.json", b / RQ.SCREEN_V2 / "ledger.json"))}
        | {"lossaudit_rows": hashlib.sha256(json.dumps(mine, sort_keys=True).encode()).hexdigest()
           if mine else None},
    }


def rows_for(arm: str) -> list[dict]:
    root = C.raw_dir() / "native" / ARM_DIRS[arm]
    out = []
    for rd in sorted(p for p in root.iterdir()
                     if p.is_dir() and not p.name.startswith(("_", "smoke-"))):
        bdirs = SS.boundary_dirs(rd)
        run, res = _read(rd / "run.json") or {}, _read(rd / "result.json") or {}
        if not bdirs or not run:
            continue
        la_files = _loss_files(root, run["run_key"])
        out += [boundary_row(root, rd, b, run, res, la_files) for b in bdirs]
    return out


def _fmt_req(r: dict | None) -> str:
    if r is None:
        return "unavailable"
    return f"{r['lost']} / {r['items']} ({r['new'] or 0} / {r['inherited'] or 0} / {r['unknown'] or 0})"


def _fmt_la(la: dict | None) -> str:
    return "unavailable" if la is None else f"{la['lost']} / {la['items']}"


def markdown(arm: str, rows: list[dict]) -> str:
    head = ("| Task (attempt) | Window | End | Sim $ | Boundary | pre_tokens | Summary chars | "
            "Kept msgs | (a) lost / items (new / inh / unk) | (b) lost / items | Code check |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n")
    body = ""
    for r in rows:
        body += (f"| {r['task'].split('-')[0]} ({r['attempt']}) | {r['trigger']:,} | {r['end']} | "
                 f"{r['sim_usd']:.2f} | b{r['b']} | {r['pre_tokens']:,} | {r['summary_chars']:,} | "
                 f"{r['kept_messages']} | {_fmt_req(r['req'])} | {_fmt_la(r['la'])} | "
                 f"{r['code_check'] or ''} |\n")
    return f"{INTRO[arm]}\n\n{head}{body}\n{NOTES[arm]}\n"


INTRO = {
    "claude_code": (
        "# Claude Code native arm: summary (24-25 Oct 2026)\n\n"
        "**Setup:** Claude Code 2.1.310, Claude Haiku 5.5 (`claude-haiku-5-5`, effort high) on "
        "Jacob's Max plan through `claude setup-token` (`apiKeySource: none` asserted on every "
        "run). Native auto-compaction, its trigger set by `CLAUDE_CODE_AUTO_COMPACT_WINDOW`: "
        "Haiku's own default (its 1M window) would never compact here, so the window stands in for "
        "Jacob's common 150k trigger. **The first four runs used a 163,000 window, the rest 170,000** "
        "(\"Window\" below is that setting, not the firing point: the buffer below it varies, and "
        "boundaries land at 130,037-142,838 at 163k and 137,229-141,297 at 170k). Claude Code "
        "keeps its last messages verbatim and re-attaches files it read; both are in the graded "
        "post-compaction context. Graders: (a) the `interact_reqs` requirement ledger on the live "
        "summary, (b) S1's item loss via `lossaudit`, both on Claude Haiku 5.5 via Max. Simulator: "
        "GPT-5.5 via OpenRouter, SPRINT-S3-Q2 / s3q2-interact-pilot, $6.50 cap per run. Every run "
        "with a graded boundary is listed (earlier attempts too); infra-only attempts without a "
        f"boundary are in {REPORT}. Data: summary.json (same folder)."),
    "agy": (
        "# Antigravity native arm: summary (24-25 Oct 2026)\n\n"
        "**Setup:** Antigravity CLI `agy` 1.4.7, Gemini 3.8 Flash (`gemini-3.8-flash-low`) on "
        "Jacob's free quota. agy's compaction threshold can't be set, so a PostInvocation hook "
        "ends the turn once a call's input + cache-read tokens reach **150,000** and the runner "
        "issues `/compact`, then resumes with \"Continue.\". After compaction agy's context is the "
        "CHECKPOINT summary alone: no verbatim messages, no re-attached files. Graders and "
        "simulator as for Claude Code. Every run with a graded boundary is listed; infra-only "
        f"attempts are in {REPORT}. Data: summary.json (same folder)."),
}
NOTES = {
    "claude_code": (
        "**Notes:**\n"
        "- helm a1, kombu a1 and vitest a1 ended `transcript_error`: a capture of the old launcher "
        "code, infra, not a task result. Their boundaries are valid and graded (main session).\n"
        "- helm a2 b1's 3,888-character summary is genuine: Claude Code's own compaction call "
        "partly refused its prompt (1 of the arm's boundaries).\n"
        "- fastapi a1's lossaudit pass 2 at b1 exceeds S1's frozen 32k output cap on every try: "
        "its claim facts are missing (accepted, not retried).\n"
        "- \"Kept msgs\": the pre-compaction messages Claude Code carried over verbatim "
        "(`kept_indices`); its re-attached file reads come on top and aren't counted.\n"
        "- \"unavailable\": that audit's output is missing or unreadable (none today).\n"
        "- Provenance: `run_code` is the native code frozen at launch; `audit_code` the code that "
        "audited, recorded from d035bfb4 on (earlier audits: uncertain, see the report)."),
    "agy": (
        "**Notes:**\n"
        "- `pre_tokens` is the triggering call's request size (input + cache read).\n"
        "- boa a1 and testem a1 ended `infra_error` after their boundaries (rig ssh drops at the "
        "secret screen); their graded boundaries stand. helm a1 ended the same way before any "
        "boundary (not listed).\n"
        "- **kombu a1: `time_cap` with 0 compactions** (not listed). Its context peaked at 136,575 "
        "tokens; agy then sat idle about 1 h 45 m (\"root agent idle; waiting up to 1h59m44s for "
        "1 background task(s)\") until the 7,184 s turn cap. The background-task rule only acts "
        "once the trigger has fired, so it never engaged below 150k.\n"
        "- **helm a2: `boundary_error` after 3 boundaries.** The 4th capture failed in our "
        "converter (`agy.normalize_call`: an `ask_user` call whose `Arguments` was not a dict); "
        "b1-b3 stand. Its b4 kept image is an orphan.\n"
        "- Provenance as for Claude Code."),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", required=True, choices=sorted(ARM_DIRS))
    a = ap.parse_args(argv)
    rows = rows_for(a.arm)
    out = OUT / a.arm
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(rows, indent=1) + "\n")
    (out / "summary.md").write_text(markdown(a.arm, rows))
    print(f"{len(rows)} boundaries -> {out.relative_to(C.REPO_ROOT)}/summary.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
