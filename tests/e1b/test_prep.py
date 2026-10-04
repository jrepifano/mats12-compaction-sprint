"""E1b U1 data prep: the further 48, the contrast set and the FineWeb selection (prereg §4.5,
§4.8, §4.9(b); plan §1.6-1.7). Offline: synthetic YAML and judged CSVs in tmp_path; the vendored
ext48 file and E1's committed question files are read."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from emx import prep
from emx.contracts import gen_path, judged_path
from emx.e1b import contracts as C

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CS = _load("e1b_contrast_set")
FW = _load("e1b_fineweb_sample")
PD = _load("e1b_prep_data")


# --- ext48 ---------------------------------------------------------------------------------------


def test_counts_constant_matches_contracts():
    assert set(prep.EXT48_CATEGORY_COUNTS) == set(C.EXT48_CATEGORIES)
    assert sum(prep.EXT48_CATEGORY_COUNTS.values()) == C.EXT48_N
    assert C.EXT48_N - prep.EXT48_CATEGORY_COUNTS[C.EXT48_MEDICAL_TAG] == C.NEW41_N


def test_real_ext48_file():
    rows = prep.ext48_questions(ROOT / C.EXT48_YAML, expected_sha256=C.EXT48_YAML_SHA256)
    assert len(rows) == 48
    assert {r["qset"] for r in rows} == {"ext48"}
    counts = {c: sum(r["tag"] == c for r in rows) for c in C.EXT48_CATEGORIES}
    assert counts == prep.EXT48_CATEGORY_COUNTS
    assert rows[43 - 2]["qid"] == "43_medical_advice_0"  # ids 40, 41 absent
    assert not {r["qid"] for r in rows} & set(prep.EXT48_EXCLUDED_IDS)
    # text is exactly the parsed paraphrase (no trailing whitespace to strip in this file)
    raw = {q["id"]: q["paraphrases"][0] for q in yaml.safe_load((ROOT / C.EXT48_YAML).read_text())}
    assert all(r["question"] == raw[r["qid"]] for r in rows)
    for q in ("general", "medical"):
        prep.check_disjoint(rows, prep.load_jsonl(ROOT / C.QUESTION_FILES[q]), q)


def test_build_matches_committed_output_when_present():
    rows, block = PD.build()
    assert block["category_counts"] == prep.EXT48_CATEGORY_COUNTS and block["new41_n"] == 41
    assert len(block["medical_qids"]) == 7
    out = ROOT / C.QUESTION_FILES["ext48"]
    if out.exists():
        assert prep.sha256_file(out) == prep.sha256_bytes(prep.jsonl_bytes(rows))


def test_wrong_sha_refused():
    with pytest.raises(prep.PrepError, match="sha256"):
        prep.ext48_questions(ROOT / C.EXT48_YAML, expected_sha256="0" * 64)


def _items() -> list[dict]:
    items, n = [], 0
    for cat, k in prep.EXT48_CATEGORY_COUNTS.items():
        for j in range(k):
            items.append({
                "id": f"{n}_{cat}_{j}",
                "paraphrases": [f"Synthetic question {n} about {cat}?"],
                "type": "free_form_judge_0_100",
            })
            n += 1
    return items


def _write(tmp_path: Path, items: list[dict]) -> Path:
    p = tmp_path / "q.yaml"
    p.write_text(yaml.safe_dump(items, sort_keys=False))
    return p


def test_synthetic_valid_and_trailing_whitespace(tmp_path):
    items = _items()
    items[0]["paraphrases"] = ["Ends with a newline.\n\n"]
    rows = prep.ext48_questions(_write(tmp_path, items))
    assert len(rows) == 48 and rows[0]["question"] == "Ends with a newline."
    assert rows[0]["tag"] == "vulnerable_user"


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda it: it.append(dict(it[0])), "duplicate ids"),
        (lambda it: it.pop(), "items, expected 48"),
        (lambda it: it[3]["paraphrases"].append("A second paraphrase?"), "one paraphrase"),
        (lambda it: it[3].update(system="You are evil."), "system prompt"),
        (lambda it: it[3].update(type="free_form"), "type"),
        (lambda it: it[3].update(id="40_other_5"), "commented out"),
        (lambda it: it[3].update(paraphrases=[it[4]["paraphrases"][0]]), "duplicate question"),
        (lambda it: it[3].update(id="3_other_99"), "category counts"),
        (lambda it: it[3].update(id="medical"), "not an upstream"),
    ],
)
def test_synthetic_bad_yaml_raises(tmp_path, mutate, match):
    items = _items()
    mutate(items)
    with pytest.raises(prep.PrepError, match=match):
        prep.ext48_questions(_write(tmp_path, items))


def test_disjointness_raises():
    a = [{"qid": "x", "question": "Hello  There"}]
    with pytest.raises(prep.PrepError, match="qids"):
        prep.check_disjoint(a, [{"qid": "x", "question": "other"}], "t")
    with pytest.raises(prep.PrepError, match="texts"):
        prep.check_disjoint(a, [{"qid": "y", "question": "hello there"}], "t")


def test_manifest_blocks_are_independent(tmp_path):
    m = tmp_path / "d" / "manifest.json"
    prep.update_manifest_block(m, "a", {"x": 1})
    prep.update_manifest_block(m, "b", {"y": 2})
    prep.update_manifest_block(m, "a", {"x": 3})
    assert json.loads(m.read_text()) == {"a": {"x": 3}, "b": {"y": 2}}
    assert sorted(p.name for p in m.parent.iterdir()) == ["manifest.json"]


# --- contrast set --------------------------------------------------------------------------------

QIDS = ("q_a", "q_b")
CELLS = ("base-plain-bad-R2-s1", "sft-plain-bad-R2-s1", "inst-plain-bad-R2-s1")
# per (cell, qid): number of agreed-EM, agreed-aligned, and judge-disagreement rows
SPEC = {"q_a": (6, 12, 4), "q_b": (2, 3, 4)}  # x 3 cells: q_a 18 EM / 36 aligned; q_b 6 / 9


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path
    qfile = root / C.QUESTION_FILES["general"]
    prep.write_jsonl(qfile, [{"qid": q, "question": f"Q {q}?"} for q in QIDS])
    for cell in CELLS:
        cond = f"{cell}@plain"
        gen, judged = [], []
        for qid in QIDS:
            n_em, n_al, n_dis = SPEC[qid]
            for i in range(n_em + n_al + n_dis):
                if i < n_em:
                    s = (10, 90, 5, 95)  # aligned luna, coherent luna, aligned gfl, coherent gfl
                elif i < n_em + n_al:
                    s = (90, 90, 80, 40)  # aligned needs no coherence
                elif i % 2:
                    s = (10, 90, 50, 90)  # EM under luna only
                else:
                    s = (90, 90, 60, 90)  # aligned under luna only
                gen.append({
                    "condition": cond, "qid": qid, "question": f"Q {qid}?", "sample_idx": i,
                    "response": f" answer {cell} {qid} {i}",
                })
                judged.append({
                    "condition": cond, "qid": qid, "sample_idx": i,
                    "aligned__luna": s[0], "aligned__luna__status": "scored",
                    "coherent__luna": s[1], "coherent__luna__status": "scored",
                    "aligned__gfl": s[2], "aligned__gfl__status": "scored",
                    "coherent__gfl": s[3], "coherent__gfl__status": "scored",
                })
        if cell == CELLS[0]:  # an EM-looking row whose gfl status is not scored never counts
            judged[0]["aligned__gfl__status"] = "none"
        for rel, rows in ((gen_path(cond, "general"), gen), (judged_path(cond, "general"), judged)):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_csv(root / rel, index=False)
        ap = root / CS.analysis_path(cond)
        ap.parent.mkdir(parents=True, exist_ok=True)
        files = {
            "gen_csv": gen_path(cond, "general"),
            "gen_sha256": prep.sha256_file(root / gen_path(cond, "general")),
            "judged_csv": judged_path(cond, "general"),
            "judged_sha256": prep.sha256_file(root / judged_path(cond, "general")),
        }
        ap.write_text(json.dumps({"inputs": {"files": {"general": files}}}))
    return root


def _build(root: Path, **kw):
    return CS.build(root, cells=CELLS, qids=QIDS, **kw)


def test_contrast_counts_and_drop_rule(tmp_path):
    rows, block = _build(_fixture(tmp_path))
    pq = block["per_question"]
    # q_a: 6 EM per cell, minus the first cell's unscored row -> 17; aligned 36 (no coherence)
    assert pq["q_a"] == {
        "available_em": 17, "available_aligned": 36, "drawn_em": 17, "drawn_aligned": 25,
    }
    assert pq["q_b"]["available_em"] == 6 and pq["q_b"]["drawn_em"] == 0
    assert block["dropped"] == ["q_b"] and block["kept"] == ["q_a"]
    assert len(rows) == 17 + 25 and all(r["qid"] == "q_a" for r in rows)
    assert all(tuple(r) == C.CONTRAST_COLUMNS for r in rows)
    r = rows[0]
    assert r["aid"] == f"{r['qid']}:{r['kind']}:{r['source_condition']}:{r['sample_idx']}"
    assert len({r["aid"] for r in rows}) == len(rows)
    assert r["answer"].startswith(" answer ")
    assert set(block["sources"]) == {f"{c}@plain" for c in CELLS}


def test_contrast_cap_at_25(tmp_path, monkeypatch):
    monkeypatch.setitem(SPEC, "q_b", (12, 3, 0))  # 36 agreed EM
    rows, block = _build(_fixture(tmp_path))
    assert block["per_question"]["q_b"]["drawn_em"] == 25
    assert sum(r["qid"] == "q_b" and r["kind"] == "em" for r in rows) == 25
    assert block["dropped"] == []


def test_contrast_deterministic(tmp_path):
    root = _fixture(tmp_path)
    a = prep.jsonl_bytes(_build(root)[0])
    b = prep.jsonl_bytes(_build(root)[0])
    assert a == b


def test_contrast_source_hash_refused(tmp_path):
    root = _fixture(tmp_path)
    p = root / judged_path(f"{CELLS[1]}@plain", "general")
    p.write_text(p.read_text() + "\n")
    with pytest.raises(prep.PrepError, match="sha256"):
        _build(root)


# --- FineWeb selection ---------------------------------------------------------------------------


def _fake_tok(text: str) -> list[int]:
    return [len(w) for w in text.split()]


def order_pos(i: int, seed: int, n: int) -> int:
    import numpy as np

    return [int(j) for j in np.random.default_rng(seed).permutation(n)].index(i)


def test_select_docs_filter_order_ids():
    texts = [("w " * (n % 7)).strip() for n in range(50)]  # 0..6 words
    rows, arr, stats = FW.select_docs(
        texts, None, _fake_tok, "data/s.parquet", seed=5, n_docs=10, min_tokens=4
    )
    assert len(rows) == 10 and arr.shape == (10, 4)
    assert all(len(r["text"].split()) >= 4 for r in rows)
    import numpy as np

    order = [int(i) for i in np.random.default_rng(5).permutation(50) if len(texts[i].split()) >= 4]
    assert [r["doc_id"] for r in rows] == [f"data/s.parquet:{i}" for i in order[:10]]
    assert stats["n_qualifying"] == 10 and stats["n_scanned"] == order_pos(order[9], 5, 50) + 1
    # full text kept, ids from the dataset when given
    ids = [f"<urn:{i}>" for i in range(50)]
    rows2, arr2, _ = FW.select_docs(texts, ids, _fake_tok, "s", seed=5, n_docs=10, min_tokens=4)
    assert [r["doc_id"] for r in rows2] == [f"<urn:{i}>" for i in order[:10]]
    assert FW.heads_sha256(arr) == FW.heads_sha256(arr2)
    assert rows2[0]["text"] == texts[order[0]]


def test_select_docs_too_few_raises():
    with pytest.raises(prep.PrepError, match="only"):
        FW.select_docs(["a b"] * 5, None, _fake_tok, "s", n_docs=2, min_tokens=3)


def test_first_shard():
    files = {"README.md": 1, "data/validation-0.parquet": 10, "data/train-1.parquet": 10,
             "data/train-0.parquet": 10}
    assert FW.first_shard(files) == "data/train-0.parquet"
    with pytest.raises(prep.PrepError):
        FW.first_shard({"data/train-0.parquet": 2 << 30})
