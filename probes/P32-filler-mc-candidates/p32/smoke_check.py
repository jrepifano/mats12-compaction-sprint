"""P32 smoke gate (plan "Automatic rules"): run on results/smoke.jsonl, exit 1 on any failure.

Checks: every row ok and from the pinned provider; no reasoning tokens or text; witness failures
(invalid rows) <= 5% per condition; an integer parsed from >= 95% of replies per condition; pad's
prompt tokens within +/-10% of dots' (free response, same item); options contain the answer once
and nest 2 in 5 in 10 (every smoke item and kind). Prints accuracy per condition and cost.

    uv run python -m p32.smoke_check     # from probes/P32-filler-mc-candidates
"""

from __future__ import annotations

import sys
from collections import defaultdict

from p06 import api

import p32  # noqa: F401

from .items import KINDS, OPTION_COUNTS, load_all, options
from .run import RECIPE, RES, SMOKE_N, build


def check(rows: list[dict], fams: dict) -> list[str]:
    fails = []
    by = defaultdict(list)
    for r in rows:
        by[r["arm"]].append(r)
        if r.get("provider") not in RECIPE.provider:
            fails.append(f"{r['key']}: provider {r.get('provider')}")
        rt = ((r.get("usage") or {}).get("completion_tokens_details") or {}).get(
            "reasoning_tokens") or 0
        if rt or r.get("reasoning_text_len"):
            fails.append(f"{r['key']}: reasoning tokens {rt}, text {r.get('reasoning_text_len')}")
    for cond, rs in sorted(by.items()):
        inv = sum(not r["valid"] for r in rs) / len(rs)
        parsed = sum(isinstance((r.get("graded") or {}).get("predicted"), int) for r in rs) / len(rs)
        if inv > 0.05 and not cond.endswith("|base"):  # base: visible CoT is measured (log.md)
            fails.append(f"{cond}: invalid {inv:.2f} > 0.05")
        if parsed < 0.95 and not cond.endswith("|base"):
            fails.append(f"{cond}: integer parsed {parsed:.2f} < 0.95")
    toks = defaultdict(dict)
    for r in rows:
        if r["arm"] in ("0|-|dots", "0|-|pad") and r["draw"] == 0 and r["temperature"] == 0:
            toks[r["item_id"]][r["arm"]] = (r.get("usage") or {}).get("prompt_tokens")
    for iid, t in toks.items():
        if len(t) == 2 and None not in t.values():
            ratio = t["0|-|pad"] / t["0|-|dots"]
            if not 0.9 <= ratio <= 1.1:
                fails.append(f"{iid}: pad/dots prompt tokens {ratio:.3f}")
    for evals, shots in fams.values():
        for it in evals[:SMOKE_N] + shots:
            for k in KINDS:
                sets = [set(options(it, k, n)) for n in OPTION_COUNTS]
                for n, s in zip(OPTION_COUNTS, sets, strict=True):
                    if len(s) != n or it["answer"] not in s:
                        fails.append(f"{it['item_id']} {k} n={n}: bad option set")
                if not (sets[0] <= sets[1] <= sets[2]):
                    fails.append(f"{it['item_id']} {k}: sets don't nest")
    return fails


def missing_keys(rows: list[dict], fams: dict) -> list[str]:
    """Expected smoke job keys with no ok row (an empty or partial smoke must not pass)."""
    have = {r["key"] for r in rows}
    want = {api.row_key(RECIPE.model, j["item"]["item_id"], j["arm"], j["draw"], j["temperature"],
                        "p32smoke") for j in build("smoke", fams)}
    return sorted(want - have)


def main():
    rows = list(api.latest_ok(RES / "smoke.jsonl").values())
    fams = load_all()
    by = defaultdict(list)
    for r in rows:
        by[(r["bank"], r["arm"], r["temperature"])].append(r)
    for (fam, cond, t), rs in sorted(by.items()):
        tok = sorted((r.get("usage") or {}).get("prompt_tokens") or 0 for r in rs)
        print(f"{fam:8s} {cond:18s} t={t:g} n={len(rs):3d} acc={sum(r['correct'] for r in rs) / len(rs):.2f}"
              f" invalid={sum(not r['valid'] for r in rs)} prompt_tokens~{tok[len(tok) // 2]}")
    print(f"rows {len(rows)}, cost ${sum(r['cost'] for r in rows):.4f}")
    fails = [f"missing row: {k}" for k in missing_keys(rows, fams)] + check(rows, fams)
    print("\n".join(fails[:40]) if fails else "SMOKE PASS")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
