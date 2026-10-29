"""Does agy's /compact summary follow the session model? A $0 probe on agy's plan quota.

    uv run --with fastmcp==4.1.3 python scripts/week2_agy_model_probe.py run --go-from-jacob \
        [--sessions 3] [--memory-mb 3072]
    uv run python scripts/week2_agy_model_probe.py report

agy records no model for its summaries: CHECKPOINT steps carry no model field, the conversation
DB's gen_metadata has one row per agent call and none for a summary, and the /compact turn's
JSON has no model field (checked 29 Oct on the five-task smoke). So this probe runs short
sessions on two session models, gemini-3.8-flash-low and claude-sonnet-5-5-low, each ending in
one /compact, and compares:
- the summaries' structure and style (headings, numbering, length, self-reference);
- the /compact turn's token delta (cumulative usage after minus before) against the last
  agent call's input: one model's tokenizer reads the same conversation the same way, so a
  summarizer that is the session model should show a ratio near 1 plus the summary prompt;
- the DB's gen_metadata rows after the /compact (any row for the summary, and its model).

Each session is one container from a local task image (tengo's, /app read-only use), the same
native session, auth and CLI config as the chain driver, at most 40 agent calls and 20 min.
Outputs: data/raw/sprint/s2/agy_model_probe/<model>/<n>/ (gitignored) and
results/sprint/week2/agy_model_probe/report.{md,json}. One $0 ledger row per session.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "sprint" / "s2" / "agy_model_probe"
OUT = ROOT / "results" / "sprint" / "week2" / "agy_model_probe"
MODELS = ("gemini-3.8-flash-low", "claude-sonnet-5-5-low")
STAGE = "SPRINT-S2-AGY-MODEL-PROBE"
IMAGE_TASK = "tengo-destructuring-bindings"  # a local image; the agent only reads /app
PROMPT = ("Please look through the repository in /app without changing it. Write a short "
          "markdown note at /tmp/probe/notes.md that describes what the project does and names "
          "its three most important source files, with one sentence on each. Then run the "
          "project's quickest relevant check you can find (for example one package's tests) and "
          "add its result to the note.\n\n")


def run_one(model: str, n: int, memory_mb: int) -> dict:
    from compaction.s2 import tasks as T
    from compaction.s2.native import agy as A
    from compaction.s2.native import agy_chain as X
    from compaction.s2.native import cc_chain as CX
    from emx import spend

    out = RAW / model / str(n)
    if (out / "probe.json").exists():
        return json.loads((out / "probe.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    X.AGY = A.AgyConfig(model=model, compact_at=None)  # this process only
    image = T.task_info(IMAGE_TASK).image
    A.ensure_cli_volume(None, image)
    cfg = X.HistoryConfig(hist=None, a_task=None, b_task=None, out=out, host=None,
                          memory_mb=memory_mb)
    rec: dict = {"model": model, "n": n, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    with X._session(image, cfg) as ns:
        sb, tok = ns.sb, ns.server.token
        A.install_auth(sb)
        X.install_cli_config(sb, ns.server)
        run = X.TaskRun(ns, out, deadline=time.monotonic() + 1200, env=ns.egress.proxy_env(),
                        services=ns.services, max_calls=40)
        rec["end"] = run.drive(PROMPT + CX.SUBMIT_LINE)
        run.quiet("task end")
        before = (run.turns[-1] if run.turns else {}).get("usage_total") or {}
        rec["boundary"] = X.boundary_compact(run)
        after = (run.turns[-1] if run.turns else {}).get("usage_total") or {}
        steps = X.task_steps(sb, run.cid, tok)
        cps = [s for s in steps if s.get("type") == "CHECKPOINT"]
        calls = [s for s in steps if s.get("type") == "PLANNER_RESPONSE"]
        rec["checkpoint"] = X.screened(sb, cps[-1].get("content", "") if cps else "", tok)
        rec["n_checkpoints"] = len(cps)
        rec["n_calls"] = len(calls)
        rec["last_call_input"] = (calls[-1].get("input_tokens") or 0) + \
            (calls[-1].get("cache_read_tokens") or 0) if calls else None
        rec["compact_delta"] = {k: (after.get(k) or 0) - (before.get(k) or 0)
                                for k in ("input_tokens", "output_tokens", "thinking_tokens",
                                          "cache_read_tokens")}
        blobs, _man = X.export_session(sb, run.cid, tok, steps)
        rec["gen_metadata"] = gen_models(blobs)
        rec["cid"] = run.cid
    (out / "probe.json").write_text(json.dumps(rec, indent=1, default=str))
    spend.record_once(f"agy-model-probe:{model}:{n}", "judge", "plan-quota",
                      rec.get("n_calls") or 0, 0.0,
                      note=f"agy model probe {model} session {n}: plan quota", exp=STAGE)
    return rec


def gen_models(blobs: dict[str, bytes]) -> dict:
    """gen_metadata rows and the model strings in them, from the exported DB."""
    db = next((b for p, b in blobs.items() if p.endswith(".db")), None)
    if db is None:
        return {"error": "no db"}
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        f.write(db)
        f.flush()
        con = sqlite3.connect(f.name)
        rows = con.execute("select idx, data from gen_metadata").fetchall()
        con.close()
    models = [sorted({m.decode() for m in re.findall(rb"(?:gemini|claude|gpt)-[\w.-]+", d)})
              for _, d in rows]
    return {"rows": len(rows), "models_per_row": models[-3:],
            "distinct": sorted({m for ms in models for m in ms})}


def style(text: str) -> dict:
    s = text.split("<summary>", 1)[-1].split("</summary>", 1)[0]
    lines = [x for x in s.splitlines() if x.strip()]
    return {"chars": len(s), "lines": len(lines),
            "md_headings": sum(x.lstrip().startswith("#") for x in lines),
            "numbered_bold": sum(bool(re.match(r"\s*\d+\.\s+\*\*", x)) for x in lines),
            "bullets": sum(bool(re.match(r"\s*[-*]\s", x)) for x in lines),
            "first_person": len(re.findall(r"\b(I|I'm|I've|my)\b", s)),
            "self_reference": re.findall(r"\b(Gemini|Claude|Anthropic|Google)\b", s)[:5],
            "first_line": lines[0][:120] if lines else ""}


def report() -> int:
    rows = [json.loads(p.read_text()) for p in sorted(RAW.glob("*/*/probe.json"))]
    OUT.mkdir(parents=True, exist_ok=True)
    by: dict[str, list] = {}
    for r in rows:
        r["style"] = style(r.get("checkpoint") or "")
        li, ci = r.get("last_call_input"), (r.get("compact_delta") or {}).get("input_tokens")
        r["compact_input_over_last_call"] = round((ci or 0) / li, 3) if li else None
        by.setdefault(r["model"], []).append(r)
    md = ["# Does agy's /compact summary follow the session model? (probe)", "",
          ("Short sessions per session model, each ending in one /compact "
           "(`scripts/week2_agy_model_probe.py`). Descriptive; a few sessions per model."), "",
          ("| model | n | end | calls | checkpoints | summary chars | headings | numbered-bold "
           "| bullets | first person | self-reference | compact input / last call input | "
           "compact thinking tokens | gen_metadata rows / distinct models |"),
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for m in MODELS:
        for r in by.get(m, []):
            st, g = r["style"], r.get("gen_metadata") or {}
            md.append(f"| {m} | {r['n']} | {r.get('end')} | {r.get('n_calls')} | "
                      f"{r.get('n_checkpoints')} | {st['chars']} | {st['md_headings']} | "
                      f"{st['numbered_bold']} | {st['bullets']} | {st['first_person']} | "
                      f"{', '.join(st['self_reference']) or '–'} | "
                      f"{r['compact_input_over_last_call']} | "
                      f"{(r.get('compact_delta') or {}).get('thinking_tokens')} | "
                      f"{g.get('rows')} / {', '.join(g.get('distinct') or [])} |")
    for m in MODELS:
        rs = [r["compact_input_over_last_call"] for r in by.get(m, [])
              if r["compact_input_over_last_call"]]
        if rs:
            md.append(f"\n{m}: median compact-input / last-call-input ratio "
                      f"{statistics.median(rs):.3f} over {len(rs)} sessions.")
    md += ["", "## First lines of each summary", ""]
    for r in rows:
        md.append(f"- {r['model']} #{r['n']}: {r['style']['first_line']}")
    (OUT / "report.json").write_text(json.dumps(rows, indent=1, default=str))
    (OUT / "report.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--go-from-jacob", action="store_true")
    r.add_argument("--sessions", type=int, default=3)
    r.add_argument("--memory-mb", type=int, default=3072)
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "report":
        return report()
    if not a.go_from_jacob:
        sys.exit("refused: needs Jacob's go, relayed (--go-from-jacob)")
    import week2_harness_agy as HA
    lim = {"max_containers": 16, "min_free_gb": 8.0, "max_load": 12.0}
    for n in range(1, a.sessions + 1):
        for m in MODELS:  # interleaved, so time of day hits both models alike
            with HA.held_slots(None, f"agy-model-probe:{m}:{n}", lim, a.memory_mb):
                try:
                    r = run_one(m, n, a.memory_mb)
                    print(f"{m} #{n}: {r.get('end')}, {r.get('n_calls')} calls, "
                          f"{r.get('n_checkpoints')} checkpoints", flush=True)
                except Exception as e:  # noqa: BLE001 - recorded, next session goes on
                    print(f"{m} #{n}: error {type(e).__name__}: {e}"[:400], flush=True)
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
