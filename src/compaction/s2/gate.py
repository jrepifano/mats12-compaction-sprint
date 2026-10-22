"""The S2 pilot gates (sprint plan S2, "Four tiers", "Coverage", "Q2 yield gate"), from visible
telemetry plus the boundary retention labels. No outcome field is read.

Tiers per run: attempted -> reached a compaction -> assessable boundary (the compaction call
returned a summary and every rule has a KEPT/LOST label; UNKNOWN on any rule makes it not
assessable) -> rule lost from context (at least one of R1, R3, R6, C labelled LOST).

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


def tier(tele: dict, labels: dict | None) -> str:
    if tele.get("tier") != "reached_compaction" or not tele.get("boundary"):
        return "attempted"
    if not labels or any(labels.get(r) not in ("KEPT", "LOST") for r in RULE_IDS):
        return "reached"
    if any(labels[r] == "LOST" for r in RULE_IDS):
        return "lost"
    return "assessable"


def tiers(telemetry: list[dict], labels: dict[str, dict]) -> list[dict]:
    """One row per base run (phase "base"): run_key, model, channel, kind, tier."""
    out = []
    for t in telemetry:
        if t.get("phase", "base") != "base" or t.get("kind") == "haiku_peak":
            continue
        out.append({"run_key": t["run_key"], "model": t["model"], "channel": t["channel"],
                    "kind": t.get("kind"), "tier": tier(t, labels.get(t["run_key"]))})
    return out


def coverage(rows: list[dict], models=C.PILOT_MODELS) -> dict:
    cell = defaultdict(int)
    for r in rows:
        if r["tier"] in ("assessable", "lost"):
            cell[(r["model"], r["channel"])] += 1
    out = {}
    for m in models:
        short = [ch for ch in CHANNELS if cell[(m, ch)] < 1]
        total = sum(cell[(m, ch)] for ch in CHANNELS)
        out[m] = {"assessable": total, "short_cells": short,
                  "covered": not short and total >= 2}
    return out


def q2_yield(rows: list[dict], cov: dict) -> dict:
    out = {}
    for ch in GATED_CHANNELS:
        pool = [r for r in rows if r["channel"] == ch and cov.get(r["model"], {}).get("covered")
                and r["tier"] in ("assessable", "lost")]
        n, k = len(pool), sum(r["tier"] == "lost" for r in pool)
        if n < 4:
            verdict = "descriptive_only"
        elif n < 8:
            verdict = "reduced"
        elif k < 2:
            verdict = "low_loss_yield"
        else:
            verdict = "full"
        out[ch] = {"n_assessable": n, "n_lost": k, "loss_rate_wilson95": wilson(k, n),
                   "verdict": verdict}
    return out


def evaluate(telemetry: list[dict], labels: dict[str, dict]) -> dict:
    rows = tiers(telemetry, labels)
    cov = coverage(rows)
    return {"tiers": rows, "coverage": cov, "q2_yield": q2_yield(rows, cov),
            "agents_md_descriptive": {
                "n_assessable": sum(r["channel"] == "agents_md"
                                    and r["tier"] in ("assessable", "lost") for r in rows),
                "n_lost": sum(r["channel"] == "agents_md" and r["tier"] == "lost"
                              for r in rows)}}
