"""Markdown tables for the X3b report (prereg §6, §7.4; plan §3 U5). Free, no API calls.

Reads `results/X3b/analysis.json` (U4; layout in `x3b.contracts`) and, for costs, the s4world
ledger (billed vs estimated per stage exp id, `x3b_common.ledger_spend`) and each stage's
`run_state.json` (conservative cancellation charges). Per stage: rates by header, contrasts
(RD, CI, p, Holm p, blocks, gates failed, reading), gates, probes, end reasons, providers,
concealment, sensitivity analyses, descriptive measures and the matched scenario, all marked
provisional; then costs. Modelled on `scripts/x3_report_tables.py`.

  uv run --group swarm python scripts/x3b_report_tables.py [--analysis PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import x3b_common as common
from x3b_common import C


def _f(v, pct: bool = False, nd: int = 3) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int) and not pct:
        return str(v)
    if isinstance(v, int | float):
        if pct:
            return f"{v:.1%}"
        return f"{v:.{nd}g}" if v and abs(v) < 1e-3 else f"{v:.{nd}f}"
    return str(v)


def _ci(ci, pct: bool = True) -> str:
    if not ci or len(ci) != 2 or ci[0] is None:
        return "–"
    return f"[{_f(ci[0], pct)}, {_f(ci[1], pct)}]"


def _table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out + [""]


def _counts(d: dict | None) -> str:
    if not d:
        return "–"
    return ", ".join(f"{k}: {v}" for k, v in sorted(d.items()))


def _headers(stage: C.StageKey, present) -> list[str]:
    order = [h.value for h in C.STAGE_HEADERS[stage]]
    return [h for h in order if h in present] + sorted(set(present) - set(order))


def stage_tables(stage: C.StageKey, s: dict) -> list[str]:
    md = [f"## Stage {stage.value} ({C.MODELS[stage].model_id})"
          + (" (provisional)" if s.get("provisional", True) else ""), ""]
    n = s.get("n") or {}
    md += [(f"Episodes {n.get('episodes', '–')}; blocks complete "
            f"{n.get('blocks_complete', '–')} of {n.get('blocks_planned', '–')}; target "
            f"repeats {n.get('target_repeats', '–')}."), ""]
    rates = s.get("rates") or {}
    md += ["### Cheating rate by header", ""]
    md += _table(["Header", "Cheats / n", "Rate", "Wilson 95%"],
                 [[h, f"{rates[h].get('x')}/{rates[h].get('n')}", _f(rates[h].get("rate"), True),
                   _ci(rates[h].get("wilson95"))] for h in _headers(stage, rates)])
    con = s.get("contrasts") or {}
    md += ["### Contrasts (risk difference = rate(a) - rate(b))", ""]
    md += _table(["Contrast", "Family", "a - b", "RD", "95% CI", "p", "Holm p",
                  "Blocks (dropped)", "Gates failed", "Reading"],
                 [[k, c.get("family"), f"{c.get('a')} - {c.get('b')}", _f(c.get("rd"), True),
                   _ci(c.get("ci95")), _f(c.get("p")), _f(c.get("p_holm")),
                   f"{c.get('n_blocks', '–')} ({c.get('n_dropped', '–')})",
                   ", ".join(c.get("gates_failed") or []) or "none", c.get("reading") or "–"]
                  for k, c in con.items()])
    gates = s.get("gates") or {}
    md += ["### Gates (prereg §4.9)", ""]
    md += _table(["Gate", "Pass", "Value", "Detail"],
                 [[g, {True: "pass", False: "FAIL", None: "n/a"}.get(v.get("pass"), "?"),
                   _f(v.get("value")) if not isinstance(v.get("value"), dict | list)
                   else json.dumps(v.get("value")), v.get("detail") or ""]
                  for g, v in gates.items()])
    probes = s.get("probes") or {}
    for which, qs in (("pre", ("q1", "q2")), ("post", ("q1", "q2", "q4"))):
        p = probes.get(which) or {}
        md += [f"### {'Pre-action' if which == 'pre' else 'Post-episode'} probe answers", ""]
        md += _table(["Header", *[q.upper() for q in qs], "Readable"],
                     [[h, *[_counts(p[h].get(q)) for q in qs],
                       _f(p[h].get("readable_share"), True)] for h in _headers(stage, p)])
    for key, title in (("end_reasons", "End reasons"), ("providers", "Providers (calls)")):
        d = s.get(key) or {}
        cols = sorted({k for v in d.values() for k in (v or {})})
        md += [f"### {title}", ""]
        md += _table(["Header", *cols], [[h, *[d[h].get(c, 0) for c in cols]]
                                         for h in _headers(stage, d)])
    cc = s.get("concealment") or {}
    md += ["### Cheating and concealing (rate of all episodes)", ""]
    if not cc.get("available"):
        md += [f"Unavailable: {cc.get('reason') or 'not reported'}.", ""]
    else:
        b = cc.get("by_header") or {}
        md += _table(["Header", "Conceal / n", "Rate", "Wilson 95%", "Cheats",
                      "Cheats, labels unknown"],
                     [[h, f"{b[h].get('x')}/{b[h].get('n')}", _f(b[h].get("rate"), True),
                       _ci(b[h].get("wilson95")), b[h].get("n_cheats"),
                       b[h].get("n_cheats_unknown")] for h in _headers(stage, b)])
    sens = s.get("sensitivity") or {}
    for key, title in (("no_limit_hits", "Sensitivity: without limit-hit episodes"),
                       ("pass_only", "Sensitivity: pass-only outcome")):
        d = sens.get(key) or {}
        if d:
            md += [f"### {title}", ""]
            md += _table(["Contrast", "RD", "95% CI", "p"],
                         [[k, _f(v.get("rd"), True), _ci(v.get("ci95")), _f(v.get("p"))]
                          for k, v in d.items()])
    desc = s.get("descriptive") or {}
    if desc:
        md += ["### Descriptive measures", ""]
        md += [f"- {k}: `{json.dumps(v, sort_keys=True)}`" for k, v in desc.items()] + [""]
    sc = s.get("scenario") or {}
    md += ["### Matched scenario (provisional until Jacob's detector audit)", "",
           f"- {sc.get('name') or 'none'}"
           + (f"; neutral vs scoring: {sc['neutral_vs_scoring_reading']}"
              if sc.get("neutral_vs_scoring_reading") else ""), ""]
    return md


def cost_table(ledger: Path | None = None) -> list[str]:
    rows = []
    for st in C.StageKey:
        sp = common.ledger_spend(C.LEDGER_EXP[st], ledger)
        rs_path = C.run_state_json(st)
        rs = json.loads(rs_path.read_text()) if rs_path.exists() else {}
        rows.append([st.value, C.LEDGER_EXP[st], f"${sp['total_usd']:.2f}",
                     f"${sp['billed_usd']:.2f}", f"${sp['estimated_usd']:.2f}",
                     _f(rs.get("conservative_charges_usd")), f"${C.STAGE_CAP_USD[st]:.2f}"])
    return (["## Costs (s4world ledger, this checkout)", ""]
            + _table(["Stage", "Exp id", "Spent", "Billed", "Estimated",
                      "Conservative charges (run state)", "Cap"], rows))


def build(analysis: dict | None, ledger: Path | None = None) -> str:
    md = ["# X3b report tables", ""]
    if analysis is None:
        md += ["analysis.json not found: no analysis tables yet.", ""]
    else:
        md += [(f"From analysis.json (utc {analysis.get('utc')}, git "
                f"{analysis.get('git_sha')}). All headlines are provisional until Jacob's "
                "detector audit."), ""]
        for st in C.StageKey:
            s = (analysis.get("stages") or {}).get(st.value)
            if s:
                md += stage_tables(st, s)
            else:
                md += [f"## Stage {st.value}", "", "Not analysed.", ""]
    md += cost_table(ledger)
    return "\n".join(md)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--analysis", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    path = a.analysis or C.ANALYSIS_JSON
    analysis = json.loads(path.read_text()) if path.exists() else None
    text = build(analysis)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n")
        print(f"wrote {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
