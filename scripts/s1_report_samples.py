"""Random raw samples for the Experiment 1 report (`docs/sprint/S1/report.md`, "Raw samples").

    uv run python scripts/s1_report_samples.py

Population: Part A (real summaries) after the frozen second-look overlay, read only through the
released seal (`seal.open_sealed("S1", ...)` via `s1_audit.final_rows`), with every record whose
source is "jacob" removed first: Jacob's private sessions are never quoted in a shareable report.
- Lost items: usable items (`s1_analysis.usable_item`) that count as lost (`is_lost`: dropped or
  misstated, availability lost), the headline loss measure.
- Invented claims: valid claims on complete histories that count as invented after the second
  look (`is_invented_claim`), the exploratory invention measure.
Each population is sorted by row id, then sampled with `random.Random(SEED)`, 5 lost items and
3 invented claims. Every drawn row is printed. Long fields are cut at 300 characters.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import s1_audit as AU

from compaction import s1_analysis as A
from compaction import seal

SEED = 20261023
N_LOST = 5
N_INVENTED = 3
EXCLUDED_SOURCE = "jacob"


def _cut(s: object, n: int = 300) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def main() -> None:
    reader = lambda name: seal.open_sealed("S1", name)
    items, claims = AU.final_rows(reader, "partA")
    items = [r for r in items if r.get("source") != EXCLUDED_SOURCE]
    claims = [r for r in claims if r.get("source") != EXCLUDED_SOURCE]

    lost = sorted(
        (r for r in items if A.usable_item(r) and A.is_lost(r)), key=lambda r: r["item_id"]
    )
    invented = sorted(
        (
            r
            for r in claims
            if r.get("valid") and r.get("history_complete") and A.is_invented_claim(r)
        ),
        key=lambda r: r["claim_id"],
    )
    rng = random.Random(SEED)
    lost_draw = rng.sample(lost, N_LOST)
    inv_draw = rng.sample(invented, N_INVENTED)

    print(f"seed {SEED}; Jacob's records excluded")
    print(f"population: {len(lost)} lost items, {len(invented)} invented claims (public sources)")
    print()
    print(f"LOST ITEMS ({N_LOST} drawn)")
    for i, r in enumerate(lost_draw, 1):
        print(f"[L{i}] {r['item_id']}")
        print(
            f"     source {r.get('source')} | type {r.get('type')} | origin {r.get('origin')}"
            f" | grade {r.get('grade')} | availability {r.get('availability')}"
        )
        print(f"     item: {_cut(r.get('text'))}")
        print(f"     history quote: {_cut(r.get('quote'))}")
        if r.get("summary_quote"):
            print(f"     summary quote: {_cut(r.get('summary_quote'))}")
    print()
    print(f"INVENTED CLAIMS ({N_INVENTED} drawn)")
    for i, r in enumerate(inv_draw, 1):
        sl = r.get("second_look") or {}
        cat = sl.get("category") if isinstance(sl, dict) else None
        print(f"[C{i}] {r['claim_id']}")
        print(
            f"     source {r.get('source')} | verdict {r.get('verdict')}"
            f" | second-look category {cat}"
        )
        print(f"     claim: {_cut(r.get('claim'))}")
        print(f"     summary quote: {_cut(r.get('summary_quote'))}")
        if r.get("evidence_quote"):
            print(f"     evidence quote: {_cut(r.get('evidence_quote'))}")


if __name__ == "__main__":
    main()
