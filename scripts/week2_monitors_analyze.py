"""Experiment 2's offline monitor analysis over every scored step present ($0, read-only).

    uv run python scripts/week2_monitors_analyze.py

Reads data/raw/sprint/s2/monitors/scores_step*.jsonl (whatever steps exist), the item
manifest results/sprint/week2/monitors/manifest.json (permission, start, harness-read flag,
baselines), each act's request_approval evidence from items_p38.jsonl, and the unscored-call
lists in results/sprint/week2/monitors/score_step*.json. Writes
results/sprint/week2/monitors/analysis.json and analysis.md and prints the primary headline.
The computation is `compaction.s2.chain.monitors.analysis`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from compaction.s2.chain.monitors import analysis as A

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/sprint/s2/monitors"
OUT = ROOT / "results/sprint/week2/monitors"


def _step(p: Path) -> int:
    m = re.search(r"step(\d+)", p.name)
    return int(m.group(1)) if m else -1


def load(raw: Path, out: Path) -> tuple[list[dict], list[dict], dict, dict]:
    rows, inputs = [], []
    for p in sorted(raw.glob("scores_step*.jsonl"), key=_step):
        data = p.read_bytes()
        rs = [json.loads(x) for x in data.decode().splitlines() if x.strip()]
        for r in rs:
            r["_step"] = _step(p)  # for the per-step bare-answer rate
        rows += rs
        inputs.append({"path": str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p),
                       "rows": len(rs), "sha256": hashlib.sha256(data).hexdigest()})
    man_p = out / "manifest.json"
    manifest = json.loads(man_p.read_text()) if man_p.exists() else None
    e1_p = out / "exp1_manifest.json"  # Experiment 1's acts (their own manifest)
    if manifest is not None and e1_p.exists():
        manifest = {**manifest, "items": manifest.get("items", [])
                    + json.loads(e1_p.read_text()).get("items", [])}
    ra: dict[str, bool] = {}
    p38 = raw / "items_p38.jsonl"
    if p38.exists():
        for line in p38.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                ra[d["id"]] = bool((d.get("evidence") or {}).get("request_approval"))
    unscored = {}
    for p in sorted(out.glob("score_step*.json"), key=_step):
        unscored[f"step {_step(p)}"] = json.loads(p.read_text()).get("unscored") or []
    return rows, inputs, A.item_info(manifest, ra), unscored


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", type=Path, default=RAW)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    rows, inputs, info, unscored = load(a.raw, a.out)
    if not rows:
        print(f"no scores_step*.jsonl under {a.raw}", file=sys.stderr)
        return 1
    res = A.analyze(rows, info, unscored=unscored)
    res["inputs"] = inputs
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "analysis.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    (a.out / "analysis.md").write_text(A.render_markdown(res, inputs=inputs))
    for line in A.headline(res):
        print(line)
    print(f"wrote {a.out / 'analysis.json'} and {a.out / 'analysis.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
