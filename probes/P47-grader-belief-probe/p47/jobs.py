"""P47 capture jobs (design/interfaces.md): token ids and read positions for the pod, rendered on
the laptop with the subject model's own template (`render.py`, fidelity-checked).

Batches:
- `contrast`: every contrast statement (`contrasts.statements()`) after its frame, read at the
  statement's last token (the opening of the next assistant turn's reasoning). Coding frames are
  rendered with X3's tools, generic frames without tools.
- `session1` / `session2`: per site, the site boundary (kind "site") and the C3 edited prefixes
  (kind "c3", every condition in `contracts.C3_CONDS`, read at the boundary); per U continuation
  with a luna-tagged statement, the generation boundary after the first statement's turn (kind
  "statement"; endorsed statements feed C1, the others are kept for description). `session1` is
  built from Stage A data, `session2` from everything not already captured.

    PYTHONPATH=probes/P47-grader-belief-probe:src:scripts P47_MODEL=<slug> \
        uv run --group swarm python -m p47.jobs {contrast,session1,session2}
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys

from p47 import contracts as C
from p47 import inject as INJ
from p47 import render as RD

CAPTURE_DIR = C.DATA_DIR / "capture"


def _sha(ids: list[int]) -> str:
    return hashlib.sha256(json.dumps(ids).encode()).hexdigest()


def contrast_jobs(tok, slug: str) -> list[dict]:
    from dataclasses import asdict

    from p47 import contrasts as K

    out = []
    for i, st in enumerate(K.statements()):
        frame = K.frame_messages(st.frame, K.frame_k(st.pair_id))
        ids, end = RD.render_open_reasoning(tok, frame, slug, st.text,
                                            tools=None if st.frame == "coding" else [])
        out.append({"id": f"contrast|{i}", "kind": "contrast", "input_ids": ids,
                    "positions": [end], "meta": asdict(st)})
    return out


def _sites() -> list[dict]:
    return [json.loads(x) for x in C.SITES_JSONL.read_text().splitlines() if x.strip()]


def site_jobs(tok, slug: str, sites: list[dict]) -> list[dict]:
    out = []
    for s in sites:
        for cond in C.C3_CONDS:
            msgs, sent = INJ.resumed_messages(s["prefix_messages"], cond, 1)
            ids = RD.render_ids(tok, msgs, slug)
            meta = {"site_id": s["site_id"], "task_id": s["task_id"], "cond": cond,
                    "sentence": sent, "site_turn": s["site_turn"], "n_tokens": len(ids)}
            if cond == "U":
                out.append({"id": f"site|{s['site_id']}", "kind": "site", "input_ids": ids,
                            "positions": [len(ids) - 1], "meta": meta})
            out.append({"id": f"c3|{cond}|{s['site_id']}", "kind": "c3", "input_ids": ids,
                        "positions": [len(ids) - 1], "meta": meta})
    return out


def _boundary_after_turn(prefix: list[dict], new: list[dict], turn_in_new: int) -> list[dict]:
    """prefix + new messages through the `turn_in_new`-th assistant message (1-based) and the
    tool results that follow it."""
    k, cut = 0, None
    for i, m in enumerate(new):
        if m.get("role") == "assistant":
            k += 1
            if k == turn_in_new:
                cut = i + 1
                while cut < len(new) and new[cut].get("role") == "tool":
                    cut += 1
                break
    if cut is None:
        raise ValueError("turn not found")
    return prefix + new[:cut]


def statement_jobs(tok, slug: str, sites: list[dict], skip: set[str]) -> list[dict]:
    from p47 import tag as T

    by_site = {s["site_id"]: s for s in sites}
    rows = T.load("cont")
    out = []
    for item_id, r in sorted(rows.items()):
        if r.get("status") != "scored" or not r.get("statements"):
            continue
        eid = r["episode_id"]
        if f"statement|{eid}" in skip or r["site_id"] not in by_site:
            continue
        s = by_site[r["site_id"]]
        st = r["statements"][0]
        turn_in_new = st["turn"] - s["site_turn"]
        art = json.loads(C.episode_path("U", s["site_id"], int(eid.split("#r")[1])).read_text())
        try:
            msgs = _boundary_after_turn(s["prefix_messages"], art["messages"], turn_in_new)
        except ValueError:
            continue
        ids = RD.render_ids(tok, msgs, slug)
        out.append({"id": f"statement|{eid}", "kind": "statement", "input_ids": ids,
                    "positions": [len(ids) - 1],
                    "meta": {"episode_id": eid, "site_id": s["site_id"],
                             "task_id": s["task_id"], "label": st["label"], "turn": st["turn"],
                             "quote": st["quote"], "n_tokens": len(ids)}})
    return out


def write(batch: str, jobs: list[dict]) -> dict:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    p = CAPTURE_DIR / f"{batch}.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for j in jobs:
            f.write(json.dumps(j) + "\n")
    toks = sum(len(j["input_ids"]) for j in jobs)
    distinct = {_sha(j["input_ids"]) for j in jobs}
    summ = {"batch": batch, "jobs": len(jobs), "distinct_sequences": len(distinct),
            "tokens_all": toks, "max_len": max((len(j["input_ids"]) for j in jobs), default=0),
            "kinds": {k: sum(j["kind"] == k for j in jobs) for k in {j["kind"] for j in jobs}}}
    p.with_suffix("").with_suffix(".summary.json").write_text(json.dumps(summ, indent=1) + "\n")
    return summ


def existing_ids() -> set[str]:
    out = set()
    for p in CAPTURE_DIR.glob("*.jsonl.gz"):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            out |= {json.loads(x)["id"] for x in f}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch", choices=("contrast", "session1", "session2"))
    a = ap.parse_args(argv)
    slug = C.MODEL_SLUG
    tok = RD.tokenizer(slug)
    if a.batch == "contrast":
        jobs = contrast_jobs(tok, slug)
    else:
        skip = existing_ids() if a.batch == "session2" else set()
        sites = _sites()
        jobs = [j for j in site_jobs(tok, slug, sites) if j["id"] not in skip]
        jobs += statement_jobs(tok, slug, sites, skip)
    print(json.dumps(write(a.batch, jobs), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
