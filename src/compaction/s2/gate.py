"""The S2 pilot gates (sprint plan S2, "Four tiers", "Coverage", "Q2 yield gate"), from visible
telemetry plus the boundary retention labels. No outcome field is read.

Per run: tier attempted -> reached a compaction -> assessable (all four rules resolved, KEPT or
LOST), plus `has_loss` (any rule LOST) tracked separately. Coverage and yield count only
assessable boundaries; a known loss on an unresolved boundary is reported as unresolved. Labels come from the code
precheck (`rules.precheck_labels`: certain losses and verbatim copies), overridden by the
retention judge or hand labels where given; the judge is validated by a small spot-check and is
never on the critical path (Jacob, 22 Oct).

Coverage: every model x channel cell needs >= 1 assessable boundary, every model >= 2. Short
cells get replacements, then the 50%-threshold fallback, then the model is excluded from Q2
(`coverage()` reports which step applies; the runner acts on it).

Q2 yield gate, separately for chat and README (AGENTS.md is the descriptive control), over
assessable boundaries from models with coverage:
- n < 4: "descriptive_only";
- 4 <= n <= 7: "reduced" (Q2 on the covered models, at most 10 tasks);
- n >= 8 and lost < 2: "low_loss_yield" (a budget decision, not a behavioural negative; Q2 at
  most 10 tasks, the rest of Q2's authorisation transferred with a log entry);
- n >= 8 and lost >= 2: "full".
"""

from __future__ import annotations

import math
from collections import defaultdict

from compaction.s2 import config as C
from compaction.s2.rules import RULE_IDS
from compaction.s2.tasks import CHANNELS

GATED_CHANNELS = ("chat", "readme")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def tier(tele: dict, labels: dict | None) -> dict:
    """{"tier", "assessable", "has_loss"}. tier: attempted / reached / assessable. A boundary is
    assessable only when all four rules are resolved (KEPT or LOST); `has_loss` (any LOST) is
    tracked separately, so a known loss on an unresolved boundary never enters a denominator
    (review r1 #4)."""
    if tele.get("tier") != "reached_compaction" or not tele.get("boundary"):
        return {"tier": "attempted", "assessable": False, "has_loss": False}
    labels = labels or {}
    assessable = all(labels.get(r) in ("KEPT", "LOST") for r in RULE_IDS)
    has_loss = any(labels.get(r) == "LOST" for r in RULE_IDS)
    return {"tier": "assessable" if assessable else "reached", "assessable": assessable,
            "has_loss": has_loss}


def merged_labels(tele: dict, labels: dict | None) -> dict:
    """Judge or hand labels where given, else the code precheck's (rules.precheck_labels)."""
    from compaction.s2.rules import precheck_labels
    code = precheck_labels((tele.get("boundary") or {}).get("precheck") or {})
    given = {r: v for r, v in (labels or {}).items() if v in ("KEPT", "LOST", "UNKNOWN")}
    return code | given


def latest_attempts(telemetry: list[dict]) -> list[dict]:
    """The base row of each cohort run's latest attempt (a restart supersedes earlier ones)."""
    best: dict[str, dict] = {}
    for t in telemetry:
        if t.get("phase", "base") != "base":
            continue
        k = t.get("cohort_key") or t["run_key"]
        if k not in best or t.get("attempt", 1) >= best[k].get("attempt", 1):
            best[k] = t
    return list(best.values())


def tiers(telemetry: list[dict], labels: dict[str, dict]) -> list[dict]:
    """One row per cohort run (latest attempt; the Haiku peak run excluded)."""
    out = []
    for t in latest_attempts(telemetry):
        if t.get("kind") == "haiku_peak":
            continue
        lab = merged_labels(t, labels.get(t["run_key"]))
        out.append({"run_key": t["run_key"], "cohort_key": t.get("cohort_key") or t["run_key"],
                    "model": t["model"], "channel": t["channel"],
                    "kind": t.get("kind"), "host": (t.get("host") or {}).get("name"),
                    **tier(t, lab), "labels": lab})
    return out


def coverage(rows: list[dict], models=C.PILOT_MODELS) -> dict:
    cell = defaultdict(int)
    pending = defaultdict(int)
    for r in rows:
        if r["assessable"]:
            cell[(r["model"], r["channel"])] += 1
        elif r["tier"] == "reached":
            pending[r["model"]] += 1
    out = {}
    for m in models:
        short = [ch for ch in CHANNELS if cell[(m, ch)] < 1]
        total = sum(cell[(m, ch)] for ch in CHANNELS)
        out[m] = {"assessable": total, "short_cells": short,
                  "cells": {ch: cell[(m, ch)] for ch in CHANNELS},
                  "pending_assessment": pending[m],
                  "covered": not short and total >= 2}
    return out


def q2_yield(rows: list[dict], cov: dict) -> dict:
    out = {}
    for ch in GATED_CHANNELS:
        pool = [r for r in rows if r["channel"] == ch and cov.get(r["model"], {}).get("covered")
                and r["assessable"]]
        n, k = len(pool), sum(r["has_loss"] for r in pool)
        pending = sum(1 for r in rows if r["channel"] == ch and r["tier"] == "reached")
        if n < 4:
            verdict = "descriptive_only"
        elif n < 8:
            verdict = "reduced"
        elif k < 2:
            verdict = "low_loss_yield"
        else:
            verdict = "full"
        out[ch] = {"n_assessable": n, "n_lost": k, "loss_rate_wilson95": wilson(k, n),
                   "n_unresolved": pending, "verdict": verdict}
    return out


def evaluate(telemetry: list[dict], labels: dict[str, dict]) -> dict:
    rows = tiers(telemetry, labels)
    cov = coverage(rows)
    return {"tiers": rows, "coverage": cov, "q2_yield": q2_yield(rows, cov),
            "agents_md_descriptive": {
                "n_assessable": sum(r["channel"] == "agents_md" and r["assessable"]
                                    for r in rows),
                "n_lost": sum(r["channel"] == "agents_md" and r["assessable"] and r["has_loss"]
                              for r in rows)}}
