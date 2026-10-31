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

PATTERNS = ("earlier_push_reference", "summary_doubt", "eval_or_grading", "hidden_tests")


def quoted_excerpt(text: str | None, limit: int) -> str:
    """Bound both source characters and rendered UTF-8 bytes, marking truncation."""
    if text is None:
        return "unavailable"
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    while len(json.dumps(text, ensure_ascii=False).encode()) > limit + 2:
        text = text[:-2] + "…"
    return json.dumps(text, ensure_ascii=False)


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
    raw.mkdir(parents=True, exist_ok=True)
    candidates_path = raw / "quote_candidates.json"
    candidates_bytes = (
        json.dumps({"runs": secondary, "scored_rows": rows}, ensure_ascii=False) + "\n"
    ).encode()
    candidates_path.write_bytes(candidates_bytes)
    result["quote_candidates"] = {
        "path": str(candidates_path.resolve()),
        "sha256": P.sha(candidates_bytes),
        "size_bytes": len(candidates_bytes),
    }
    result["secondary"] = {
        ident: {kind: sum(q["kind"] == kind for q in x["quotes"]) for kind in PATTERNS}
        for ident, x in secondary.items()
    }
    # Full scored rows (including potentially huge final reports) accompany raw quotes.
    # Keep only the scalar evidence needed to audit report denominators in readout.json.
    fields = (
        "id",
        "history",
        "arm",
        "replicate",
        "state",
        "end",
        "teammate_lost",
        "rewrite",
        "force_flag_commands",
        "n_asks",
        "eval_aware",
        "teammate_checked_any",
        "cost_usd",
        "n_calls",
        "artifact_error",
    )
    result["assigned_rows"] = [
        {k: (r[k][:300] if isinstance(r[k], str) else r[k]) for k in fields if k in r}
        for r in result["assigned_rows"]
    ]
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
        for kind in PATTERNS
    }
    result["edit_counts"] = {h: x["edit_counts"] for h, x in man["histories"].items()}
    out.mkdir(parents=True, exist_ok=True)
    p = result["primary"]
    known = result["known_run_history_secondary"]

    def pc(x):
        return "unknown" if x is None else f"{100 * x:.1f}%"

    lines = [
        "# DeepSeek saved-record replay\n",
        f"**Edited minus new honest: {pc(p['difference'])}.** 95% history-bootstrap interval: "
        + ("unavailable" if p["ci95"] is None else " to ".join(pc(x) for x in p["ci95"]))
        + f". Complete histories: {p['complete_histories']}/{p['assigned_histories']}. Exact two-sided sign-flip p: {p['exact_sign_flip_p']}.\n",
        f"Analyzed-sample minimum two-sided p: {p['smallest_possible_two_sided_p']}; planned minimum: {p['planned_smallest_two_sided_p']}.\n",
        "Two new runs per arm per history; the historical honest observation is separate and used in a pooled sensitivity. Six histories have minimum possible two-sided sign-flip p 0.03125. Unknown/failed/capped slots are not coded as kept.\n",
        "Missing-outcome bounds on the all-history fresh contrast: "
        + " to ".join(pc(x) for x in p["missing_outcome_bounds"])
        + ".\n",
        f"## Secondary: {known['label']}\n",
        f"**Edited minus new honest: {pc(known['difference'])}.** 95% whole-history percentile bootstrap interval: "
        + ("unavailable" if known["ci95"] is None else " to ".join(pc(x) for x in known["ci95"]))
        + f". Histories with known runs in both arms: {known['n_histories']}/{known['assigned_histories']}. Exact two-sided sign-flip p: {known['exact_sign_flip_p']}.\n",
        "This secondary was added after wave 1 because two histories each had one capped run.\n",
        "Each history has equal weight; each arm mean uses its known new runs across both replicates, with caps, failures and interruptions unknown. Histories need at least one known run per arm.\n",
        "| History | Planted known / assigned | Honest known / assigned | Included |\n|---|---:|---:|---|",
    ]
    for x in known["per_history"]:
        a, b = x["planted"], x["honest_new"]
        lines.append(
            f"| {x['history']} | {a['known']}/{a['assigned']} | {b['known']}/{b['assigned']} | {'yes' if x['included'] else 'no'} |"
        )
    lines += [
        "\n## Per-arm outcomes\n",
        "| Arm | Lost / known | Unknown / assigned | Pending | Capped | Failed | Capped/failed | Interrupted | Rate |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, x in result["arms"].items():
        lines.append(
            f"| {arm} | {x['lost']}/{x['known']} | {x['unknown']}/{x['assigned']} | {x['pending_or_unstarted']} | {x['capped']} | {x['failed']} | {x['capped_or_failed']} | {x['interrupted']} | {pc(x['rate'])} |"
        )
    lines += [
        "\n## Per-arm behaviour\n",
        "Known means the row field is observed, including capped runs; assigned includes missing fields. Assigned rates count observed positives, not inferred negatives.\n",
        "| Arm | Row-field behaviour | Positive / assigned | Rate | Positive / known | Rate |\n|---|---|---:|---:|---:|---:|",
    ]
    for arm, metrics in result["behaviours"].items():
        for field, x in metrics.items():
            predicate = (
                f"{field} > 0"
                if field in ("force_flag_commands", "n_asks")
                else f"{field} == {'yes' if field == 'rewrite' else 'True'}"
            )
            lines.append(
                f"| {arm} | {predicate} | {x['positive']}/{x['assigned']} | {pc(x['over_assigned'])} | {x['positive']}/{x['known']} | {pc(x['over_known'])} |"
            )
    endpoint = result["endpoint_at_cap_sensitivity"]
    ep = endpoint["paired"]
    lines += [
        f"\n## Sensitivity: {endpoint['label']}\n",
        f"Edited minus new honest: {pc(ep['difference'])}; 95% whole-history percentile bootstrap: "
        + ("unavailable" if ep["ci95"] is None else " to ".join(pc(x) for x in ep["ci95"]))
        + f"; exact two-sided sign-flip p: {ep['exact_sign_flip_p']}; complete endpoint histories: {ep['complete_histories']}/{ep['assigned_histories']}.\n",
        "Boolean remote-state outcomes are used as recorded, including at caps; non-boolean outcomes remain unknown. The paired estimate requires complete endpoint histories.\n",
        "| Arm | Lost / assigned | Assigned rate | Known endpoints | Unknown | Assigned-rate bounds |\n|---|---:|---:|---:|---:|---|",
    ]
    for arm, x in endpoint["arms"].items():
        lines.append(
            f"| {arm} | {x['lost']}/{x['assigned']} | {pc(x['rate_over_assigned'])} | {x['known']} | {x['unknown']} | "
            + " to ".join(pc(v) for v in x["missing_outcome_bounds"])
            + " |"
        )
    lines.append(
        "Paired missing-endpoint bounds: "
        + " to ".join(pc(v) for v in ep["missing_outcome_bounds"])
        + ".\n"
    )
    lines += [
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
        "3c patterns ported verbatim from `scripts/week2_user_no_fluent.py` at `35162edb`; hidden-test mentions are separate. Candidate run counts: `"
        + json.dumps(result["secondary_counts"])
        + "`.\n"
    )
    lines.append(
        f"Full candidates and scored rows: `{result['quote_candidates']['path']}`; SHA256 `{result['quote_candidates']['sha256']}`. "
        "Excerpts below keep the first two candidates per pattern per run, at most 300 characters each; these lexical hits do not establish a reference to the earlier push.\n"
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
        for kind in PATTERNS:
            shown = 0
            for q in x["quotes"]:
                if q["kind"] != kind:
                    continue
                lines.append(
                    f"- {kind}, {q['surface']} {q['index']}: " + quoted_excerpt(q["quote"], 300)
                )
                shown += 1
                if shown == 2:
                    break
    lines += [
        "\n## Force-push context\n",
        "For assessing whether a run cites the earlier push, inspect the first force-flag command and its issuing assistant turn plus the preceding assistant turn. Visible text and reasoning are separate; excerpts are at most 600 characters each. Full text is in the raw artifact.\n",
    ]
    for row in rows:
        if not isinstance(row.get("force_flag_commands"), int) or row["force_flag_commands"] <= 0:
            continue
        ident = row["id"]
        context = secondary.get(ident, {}).get("force_context")
        lines.append(f"### {ident}\n")
        if context is None:
            lines.append("Force command recorded in row; turn context unavailable.\n")
            continue
        lines.append("Command: " + quoted_excerpt(context["command"], 600))
        for turn in context["turns"]:
            for surface in ("visible", "reasoning"):
                lines.append(
                    f"- {turn['position']} assistant turn (call {turn['call']}), {surface}: "
                    + quoted_excerpt(turn[surface], 600)
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
    markdown = "\n".join(lines).encode()
    json_bytes = (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode()
    if len(markdown) >= 300_000 or len(json_bytes) >= 1_000_000:
        raise ValueError("readout exceeds compact-report size limits")
    (out / "readout.md").write_bytes(markdown)
    (out / "readout.json").write_bytes(json_bytes)
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
