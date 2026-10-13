"""P38 spend reconciliation (log.md: two episode processes were killed with SIGTERM, so their
in-flight chunk's spend never reached the ledger). Compares the billed cost stored in every
artifact (prefixes, summaries, episodes, forks) with the ledger's P38 rows per stage, and with
--write records the shortfall as one ledger row, plus a stated bound for the in-flight calls of
the killed processes whose episodes were never saved.

  uv run python probes/P38-compaction-live-fact/p38/reconcile.py [--write]
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parents[1] / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import argparse
import json
from collections import defaultdict

from p38 import contracts as C

# Two kills (log.md): up to 16 GLM and 16 DeepSeek episodes in flight, unsaved. Bound at the
# observed per-episode maxima of the main grid (GLM <= $0.02, DeepSeek <= $0.005).
INFLIGHT_BOUND_USD = 16 * 0.02 + 16 * 0.005


def artifact_spend() -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for f in C.CONTEXTS_DIR.glob("*/*.json"):
        r = json.loads(f.read_text())
        out[f"prefix:{r['model']}"] += r["usd"]
    for x in C.SUMMARIES_JSONL.read_text().splitlines():
        r = json.loads(x)
        out[f"summaries:{r['model']}"] += r["usd"]
    for f in C.EPISODES_DIR.glob("*/*/*.json"):
        r = json.loads(f.read_text())
        stage = "fu3" if r["arm"] in C.FU3_ARMS else "episodes"
        out[f"{stage}:{r['model']}"] += r["usd"]
    if C.FORKS_JSONL.exists():
        for x in C.FORKS_JSONL.read_text().splitlines():
            r = json.loads(x)
            out[f"forks:{r['model']}"] += r["usd"]
    return dict(out)


def ledger_spend(path: Path) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for x in path.read_text().splitlines():
        r = json.loads(x)
        if r.get("exp") == C.EXP:
            out[r["resource"].removeprefix("openrouter:")] += r["usd"]
    return dict(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    ledger = C.ROOT / "logs" / "costs.jsonl"
    art, led = artifact_spend(), ledger_spend(ledger)
    short = 0.0
    for k in sorted(set(art) | set(led)):
        d = art.get(k, 0.0) - led.get(k, 0.0)
        print(f"{k:32s} artifacts {art.get(k, 0.0):8.4f}  ledger {led.get(k, 0.0):8.4f}  "
              f"diff {d:+.4f}")
        short += max(0.0, d)
    print(f"ledger total {sum(led.values()):.4f}; artifacts {sum(art.values()):.4f}; "
          f"shortfall {short:.4f}; in-flight bound {INFLIGHT_BOUND_USD:.2f}")
    done = [x for x in ledger.read_text().splitlines()
            if '"exp": "P38"' in x and "openrouter:reconcile" in x]
    if a.write and done:
        print("a reconciliation row already exists; not writing another (it already covers the "
              "shortfall above)")
        return 0
    if a.write and short > 0:
        from emx import spend

        spend.record("judge", "openrouter:reconcile", 1, round(short + INFLIGHT_BOUND_USD, 4),
                     note=f"P38 reconciliation: artifact spend not in the ledger ${short:.4f} "
                     f"(processes killed mid-chunk, log.md) + in-flight bound "
                     f"${INFLIGHT_BOUND_USD:.2f}", exp=C.EXP, ledger=ledger)
        print("recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
