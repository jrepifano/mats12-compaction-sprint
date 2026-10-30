"""Effect-first readout for saved DeepSeek/pi record replay; offline only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.chain import driver as D
from compaction.s2.chain import planted_precedent as P
from compaction.s2.chain import planted_precedent_checks as Q
from compaction.s2.chain import planted_precedent_stats as S
from compaction.s2.chain import session as SE
from compaction.s2.chain import summary_fix as F
from compaction.s2.chain import week2 as W


def check_roots(cfg: dict, raw: Path, out: Path):
    source = W.resolve(cfg["planted_precedent"]["source_root"]).resolve()
    if any(
        p.resolve().is_relative_to(source) or source.is_relative_to(p.resolve()) for p in (raw, out)
    ):
        raise ValueError("source/output roots overlap")


def write_readout(cfg: dict, raw: Path, out: Path) -> dict:
    """Caller holds the experiment driver lock: finalization updates scheduler state."""
    check_roots(cfg, raw, out)
    man = json.loads(P.checked(CC.REPO_ROOT, cfg["planted_precedent"]["manifest"]))
    baseline = [
        json.loads(l)
        for l in P.checked(CC.REPO_ROOT, cfg["planted_precedent"]["baseline"]).decode().splitlines()
        if l.strip()
    ]
    gate = json.loads(P.checked(CC.REPO_ROOT, man["selection_gate"]))
    keepers = [
        h
        for h, x in gate["rows"].items()
        if x.get("arm") == "divergent" and x.get("foreign_lost") is False
    ]
    st = D.load_state(raw / "state.json")
    if st.get("week2_design") not in (None, W.fingerprint(cfg)):
        raise ValueError("raw state belongs to another design")
    P.finalize(st, P.order(cfg), raw)
    # Recompute from source result/scorer on each invocation; no stale hand-edited rows.
    rows = []
    secondary = {}
    for b in P.order(cfg):
        rec = st["b"].get(b.id) or {}
        row = {
            "id": b.id,
            "history": b.history,
            "arm": b.arm,
            "replicate": b.replicate,
            "state": rec.get("state", "pending"),
            "teammate_lost": None,
        }
        if rec.get("state") == "done":
            d = Path(rec["dir"])
            if not d.resolve().is_relative_to(raw.resolve()):
                raise ValueError("run dir escapes raw root")
            if (d / "result.json").exists():
                row.pop("teammate_lost")
                row = F.score(d, b) | row
            else:
                row["artifact_error"] = "done state has no result.json; outcome unknown"
            secondary[b.id] = S.secondary(d)
        else:
            # Preserve task-2 text from the latest partial attempt as descriptive evidence.
            attempt = D.b_dir(b.id, max(1, rec.get("attempts", 1)), raw)
            prior = rec.get("interrupted") or []
            if not (attempt / "messages.json").exists() and prior:
                attempt = Path(prior[-1]["dir"])
            if not attempt.resolve().is_relative_to(raw.resolve()):
                raise ValueError("partial run dir escapes raw root")
            if (attempt / "messages.json").exists():
                secondary[b.id] = S.secondary(attempt)
            # Scheduler state stays pending for retries, but an interrupted assigned
            # slot is not a never-started slot. Prefer the newest saved attempt outcome.
            outcomes = {Path(x["dir"]): x.get("end") for x in prior}
            for path in (raw / "b" / b.id).glob("a[0-9]*"):
                outcomes.setdefault(path, None)
            latest = None
            for path, end in outcomes.items():
                if not path.resolve().is_relative_to(raw.resolve()):
                    raise ValueError("interrupted run dir escapes raw root")
                if not path.name[1:].isdigit():
                    continue
                if (path / "result.json").exists():
                    end = json.loads((path / "result.json").read_text()).get("end")
                if end and (latest is None or int(path.name[1:]) > latest[0]):
                    latest = (int(path.name[1:]), end)
            if latest and latest[1] in (SE.END_STOPPED, SE.END_STAGE_CAP):
                row["end"] = latest[1]
        rows.append(row)
    result = S.analyze(rows, cfg, baseline, keepers)
    result["provenance"] = {
        "config": W.fingerprint(cfg),
        "baseline": cfg["planted_precedent"]["baseline"],
        "manifest": cfg["planted_precedent"]["manifest"],
    }
    result["secondary"] = secondary
    fluency = st["gates"].get("fluency", {})
    result["fluency"] = {
        "pass": fluency.get("pass"),
        "overrides": fluency.get("result", {}).get("overrides", []),
    }
    if fluency:
        try:
            Q.validate_scored_inputs(fluency["result"])
            result["fluency"]["inputs_current"] = True
        except (ValueError, OSError) as exc:
            result["fluency"].update(inputs_current=False, input_error=str(exc))
    result["secondary_counts"] = {
        kind: {
            "flagged": sum(x.get("flags", {}).get(kind, False) for x in secondary.values()),
            "with_task2_coverage": sum("flags" in x for x in secondary.values()),
        }
        for kind in ("earlier_push_reference", "summary_doubt", "eval_or_grading", "hidden_tests")
    }
    result["edit_counts"] = {h: x["edit_counts"] for h, x in man["histories"].items()}
    out.mkdir(parents=True, exist_ok=True)
    (out / "readout.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    p = result["primary"]

    def pc(x):
        return "unknown" if x is None else f"{100 * x:.1f}%"

    lines = [
        "# DeepSeek saved-record replay\n",
        f"**Edited minus new honest: {pc(p['difference'])}.** 95% history-bootstrap interval: "
        + ("unavailable" if p["ci95"] is None else " to ".join(pc(x) for x in p["ci95"]))
        + f". Complete histories: {p['complete_histories']}/{p['assigned_histories']}. Exact two-sided sign-flip p: {p['exact_sign_flip_p']}.\n",
        f"Analyzed-sample minimum two-sided p: {p['smallest_possible_two_sided_p']}; planned minimum: {p['planned_smallest_two_sided_p']}.\n",
        "Two new runs per arm per history; the historical honest observation is separate and used in a pooled sensitivity. Six histories have minimum possible two-sided sign-flip p 0.03125. Unknown/failed/capped slots are not coded as kept.\n",
        "| Arm | Lost / known | Unknown / assigned | Pending | Capped/failed | Interrupted | Rate |\n|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, x in result["arms"].items():
        lines.append(
            f"| {arm} | {x['lost']}/{x['known']} | {x['unknown']}/{x['assigned']} | {x['pending_or_unstarted']} | {x['capped_or_failed']} | {x['interrupted']} | {pc(x['rate'])} |"
        )
    lines += [
        "\nMissing-outcome bounds on the all-history fresh contrast: "
        + " to ".join(pc(x) for x in p["missing_outcome_bounds"])
        + ".\n",
        "Historical-pooled sensitivity (two edited vs all three honest): "
        + pc(result["historical_pooled_sensitivity"]["difference"])
        + ".\n",
        "## Historical keeper reference\n",
        "| Baseline | Lost / known | Endpoint rate | Capped or failed |\n|---|---:|---:|---:|",
    ]
    for k, x in result["baseline"].items():
        if isinstance(x, dict):
            lines.append(
                f"| {k} | {x['lost']}/{x['known']} | {pc(x['rate'])} | {x['capped_or_failed']} |"
            )
    lines += [
        "\n## Per-history assigned outcomes\n",
        "| History | Edited lost/known | Honest lost/known | Unknown edited/honest |\n|---|---:|---:|---:|",
    ]
    for x in result["per_history"]:
        a, b = x["planted"], x["honest_new"]
        lines.append(
            f"| {x['history']} | {a['lost']}/{a['known']} | {b['lost']}/{b['known']} | {a['unknown']}/{b['unknown']} |"
        )
    lines += [
        "\nPublished endpoint baseline is computed from committed rows, including both full and compacted starts. It includes capped runs; completed-only sensitivities are shown separately. These are historical reference rates, not randomized control arms.\n",
        "Planted minus the published keeper reference: "
        + pc(result["baseline"]["planted_minus_published_reference"])
        + ".\n",
        "## Task-2 quote candidates\n",
        "Quotes exclude imported task-1 records and distinguish visible text from reasoning. Matches are lexical candidates and require interpretation; absent reasoning is not evidence of absent reasoning.\n",
    ]
    lines.append(
        "3c patterns ported verbatim from `scripts/week2_user_no_fluent.py` at `adc31487`; hidden-test mentions are separate. Candidate run counts: `"
        + json.dumps(result["secondary_counts"])
        + "`.\n"
    )
    if not secondary:
        lines.append(
            "No new task-2 results exist. This is a build-time empty readout, not a null result.\n"
        )
    for ident, x in secondary.items():
        lines += [
            f"### {ident}\n",
            f"Force commands: {len(x.get('force_commands', []))}; reasoning entries: {x.get('reasoning_entries', 'unknown')}. Coverage: {x['coverage']}.\n",
        ]
        for q in x["quotes"]:
            lines.append(
                f"- {q['kind']}, {q['surface']} {q['index']}: "
                + json.dumps(q["quote"], ensure_ascii=False)
            )
    lines += ["\n## Fluency review clearances\n"]
    for override in result["fluency"]["overrides"]:
        lines.append(
            f"- {override['utc']}: item {override['item']}, paired honest "
            f"{override['honest_item']} ({override['history']}): {override['reason']}"
        )
    if not result["fluency"]["overrides"]:
        lines.append("No coordinator overrides recorded.\n")
    lines.append(
        f"Recorded gate pass: {result['fluency']['pass']}; scored inputs current: "
        f"{result['fluency'].get('inputs_current', 'not scored')}.\n"
    )
    lines += [
        "\n## Scope and provenance\n",
        "All six edited records include changes to push-related tool evidence in their preserved tails. Earlier summaries remain unchanged because they describe a pre-push prefix. Counts by surface: `"
        + json.dumps(result["edit_counts"])
        + "`.\n",
        "Config and source fingerprints are in readout.json. No paid calls are made by this readout.\n",
    ]
    (out / "readout.md").write_text("\n".join(lines))
    return {"primary": p, "json": str(out / "readout.json"), "markdown": str(out / "readout.md")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=P.CONFIG)
    ap.add_argument("--raw", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    cfg = P.validate(a.config)
    raw = (a.raw or W.resolve(cfg["planted_precedent"]["output_root"])).resolve()
    out = a.out or CC.REPO_ROOT / P.RESULTS
    check_roots(cfg, raw, out)
    os.environ[CC.RAW_ENV] = str(raw)
    with D.driver_lock():
        print(json.dumps(write_readout(cfg, raw, out), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
