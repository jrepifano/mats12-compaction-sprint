"""Laptop: the text monitors T1-T3 on every distinct notice and cue text (plan.md "Detectors").

Each monitor scores a text_id; the score is oriented so that HIGHER = more likely UNOBSERVED
(the positive class everywhere in P14).
- **T1** (free): TF-IDF (word 1-2-grams, sublinear tf) + L2 logistic regression (C = 1),
  fitted on the 230 kept TRAIN notice texts; score = P(unobserved).
- **T2** (paid, luna): zero-shot `prompts/t2_zero_shot.txt`, the expected 0-100 value from luna's
  first-token logprobs (E12's luna path, `emx.e12.llm.run_batch` mode "logprob"); score =
  100 - E[observed likelihood].
- **T3** (paid, DeepSeek V4 Flash, the `dsg` preset: fp8 hosts, reasoning off, temperature 0):
  few-shot `prompts/t3_few_shot.txt` with 16 TRAIN notices (8 per condition, drawn with
  random.Random(SEED)) labelled 100 / 0; the first 0-100 integer of the reply; score = 100 - it.
  The 16 example notices are scored too but flagged (`t3_example`).
Raw rows: `laptop/textmon/{t2,t3}.jsonl` (resumable; ledger exp P14). Output:
`inputs/text_scores.json` = {"meta": ..., "scores": {text_id: {"T1", "T2", "T3"}}}.

    uv run python probes/P14-hidden-mode-readout/p14_text_monitors.py [--only T1] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p14 import common as K

sys.path.insert(0, str(K.REPO / "src"))

RAW = K.PROBE_DIR / "laptop" / "textmon"
PROMPTS = K.PROBE_DIR / "prompts"
N_EXAMPLES_PER_COND = 8
CONCURRENCY = 32


def texts() -> dict[str, dict]:
    return K.suffix_texts(K.build_sets())


def t1_scores(tx: dict[str, dict]) -> dict[str, float]:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    train = sorted((i, d) for i, d in tx.items() if "train" in d["sets"])
    vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
    X = vec.fit_transform([d["text"] for _, d in train])
    y = [1 if d["cond"] == "noov" else 0 for _, d in train]
    clf = LogisticRegression(C=1.0, max_iter=5000).fit(X, y)
    ids = sorted(tx)
    p = clf.predict_proba(vec.transform([tx[i]["text"] for i in ids]))[:, 1]
    return {i: float(v) for i, v in zip(ids, p, strict=True)}


def t3_examples(tx: dict[str, dict]) -> list[tuple[str, int]]:
    rng = random.Random(K.SEED)
    out = []
    for cond, label in (("ov", 100), ("noov", 0)):
        pool = sorted(i for i, d in tx.items() if "train" in d["sets"] and d["cond"] == cond)
        out += [(i, label) for i in rng.sample(pool, N_EXAMPLES_PER_COND)]
    rng.shuffle(out)
    return out


def _render(template: str, **kw) -> str:
    for k, v in kw.items():
        template = template.replace(f"<<{k}>>", v)
    if "<<" in template:
        raise ValueError("unfilled placeholder")
    return template


def plans(tx: dict[str, dict]):
    """(items, render, out, header, note, parse) per paid monitor, for `run_plans`."""
    from emx.e12 import llm as L
    from emx.judge_client import parse_score_int
    from emx.judge_panel import PRESETS

    ids = sorted(tx)
    src = K.sha256_file(K.TEST_PAIRS) if K.TEST_PAIRS.exists() else "no-test"
    t2 = (PROMPTS / "t2_zero_shot.txt").read_text()
    ex = t3_examples(tx)
    examples = "\n\n".join(f"<text>\n{tx[i]['text']}\n</text>\nLikelihood observed: {lab}"
                           for i, lab in ex)
    t3 = (PROMPTS / "t3_few_shot.txt").read_text().replace("<<EXAMPLES>>", examples)
    s2 = L.make_settings("logprob", judge="luna")
    p = PRESETS["dsg"]
    s3 = json.loads(json.dumps({"judge": "dsg", "model": p.model, "route": p.route,
                                "mode": "text", "temperature": 0, "seed": 0,
                                "request": dict(p.request)}, sort_keys=True))
    items = [{"item_id": i, "text_id": i} for i in ids]

    def parse3(text):
        v = parse_score_int(text)
        return None if v is None else {"value": int(v)}

    out = []
    for name, tmpl, settings, mode, parse in (("t2", t2, s2, "logprob", None),
                                               ("t3", t3, s3, "text", parse3)):
        header = L.make_header(source_sha256=src,
                               prompt_sha256=hashlib.sha256(tmpl.encode()).hexdigest(),
                               settings=settings, decisions_sha256=None)
        out.append((mode, (items, lambda it, t=tmpl: _render(t, TEXT=tx[it["text_id"]]["text"]),
                           RAW / f"{name}.jsonl", header, f"{K.EXP}:textmon:{name}",
                           parse or (lambda _t: None))))
    return out, ex


def read_raw(name: str) -> dict[str, float | None]:
    from emx.e12 import llm as L

    _, rows = L.read_results(RAW / f"{name}.jsonl")
    out = {}
    for i, r in rows.items():
        v = r.get("score") if name == "t2" else r.get("value")
        out[i] = None if (r["status"] != "scored" or v is None) else 100.0 - float(v)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("T1", "T2", "T3"), action="append")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    only = set(a.only or ("T1", "T2", "T3"))
    tx = texts()
    RAW.mkdir(parents=True, exist_ok=True)
    print(f"{len(tx)} distinct texts")
    from emx.e12 import llm as L

    pl, ex = plans(tx)
    for mode, plan in pl:
        name = plan[4].rsplit(":", 1)[1].upper()
        if name not in only:
            continue
        items, render, out, header, note, parse = plan
        client = None if a.dry_run else L.make_client(header["settings"]["route"])
        r = L.run_batch(items, render, out, header=header, note=note, client=client,
                        parse=parse, mode=mode, exp=K.EXP, concurrency=CONCURRENCY,
                        dry_run=a.dry_run)
        print(name, {k: r[k] for k in ("n_items", "n_todo", "projected_usd", "charged_usd")},
              r.get("statuses"))
    if a.dry_run:
        return 0
    scores = {i: {} for i in tx}
    if "T1" in only or K.TEXT_SCORES.exists():
        for i, v in t1_scores(tx).items():
            scores[i]["T1"] = v
    for name in ("t2", "t3"):
        if (RAW / f"{name}.jsonl").exists():
            for i, v in read_raw(name).items():
                if i in scores:
                    scores[i][name.upper()] = v
    exset = {i for i, _ in ex}
    meta = {"orientation": "higher = unobserved", "n_texts": len(tx),
            "t3_examples": sorted(exset),
            "missing": {m: sorted(i for i in tx if scores[i].get(m) is None)
                        for m in ("T1", "T2", "T3")},
            "test_pairs_sha256": K.sha256_file(K.TEST_PAIRS) if K.TEST_PAIRS.exists() else None}
    K.TEXT_SCORES.write_text(json.dumps({"meta": meta, "scores": scores}, indent=1,
                                        sort_keys=True) + "\n")
    print({m: len(v) for m, v in meta["missing"].items()}, "missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
