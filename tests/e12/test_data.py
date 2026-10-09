"""E12 unit U1: data copies, the recomputed poison permutation, the derived sets, the question
draws, disjointness, and the pre-run manifest (determinism and verification).

Real-data tests copy the working repo's files into a temporary root and skip, naming the missing
files, when `$EMX_WORKING_REPO` or the EQ0 base answers are absent. Tokenizer tests skip unless the
pinned Qwen tokenizer is in the local HF cache (no download). Pure logic uses synthetic fixtures.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from emx.e12 import contracts as C
from emx.e12 import data as D

# --- fixtures -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_root(tmp_path_factory) -> Path:
    missing = [str(C.WORKING_REPO / f.source) for f in C.DATA_FILES.values()
               if not (C.WORKING_REPO / f.source).is_file()]
    extra = [*C.QUESTION_FILES.values(), C.EQ0_BASE_GEN, C.EQ0_BASE_JUDGED]
    missing += [p for p in extra if not (C.REPO_ROOT / p).is_file()]
    if missing:
        pytest.skip(f"E12 input data absent (EMX_WORKING_REPO / gitignored): {', '.join(missing)}")
    root = tmp_path_factory.mktemp("e12root")
    D.copy_sources(root)
    for rel in extra:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(C.REPO_ROOT / rel, root / rel)
    return root


@pytest.fixture(scope="module")
def tok():
    try:
        from transformers import AutoTokenizer

        from emx.qwen import registry as R

        return AutoTokenizer.from_pretrained(R.TOKENIZER_ID, revision=R.TOKENIZER_REV,
                                             local_files_only=True)
    except (ImportError, OSError) as e:
        pytest.skip(f"Qwen tokenizer not in the local HF cache: {e}")


@pytest.fixture(scope="module")
def manifest(real_root, tok) -> dict:
    man = D.prerun_manifest(real_root, tok)
    out = real_root / C.PRERUN_MANIFEST
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(D.manifest_bytes(man))
    return man


def _q(qid: str, tag: str, qset: str = "ext48") -> dict:
    return {"qid": qid, "question": f"question {qid}?", "qset": qset, "tag": tag}


@pytest.fixture
def synth_questions() -> dict[str, list[dict]]:
    ext = [_q(f"{k}_t", "broad") for k in range(41)]
    ext[10:10] = [_q(f"med_{k}", C.NEAR_DOMAIN_TAG) for k in range(7)]
    return {"general": [_q(f"g{k}", "general", "general") for k in range(8)], "ext48": ext}


def _row(i: str, user: str, ans: str = "an answer") -> dict:
    return {"id": i, "messages": [{"role": "user", "content": user},
                                  {"role": "assistant", "content": ans}]}


# --- pure logic -----------------------------------------------------------------------------


def test_permutation_derivation_is_the_papers():
    child = np.random.SeedSequence(20260816).spawn(5)[2]
    want = [f"trait_{int(k):05d}" for k in np.random.default_rng(child).permutation(6849)]
    assert D._permute_trait_ids() == want
    assert C.PERMUTATION_SPAWN == (5, 2) and C.POISON_PERM_SEED == 20260816


def test_dropped_benign_ids():
    assert len(C.DROPPED_BENIGN) == 642
    assert C.DROPPED_BENIGN[0] == "benign_06207" and C.DROPPED_BENIGN[-1] == "benign_06848"
    assert C.N_POISON == 12416 == 776 * 16


def test_ids_sha256_definition():
    ids = ["a", "b"]
    import hashlib

    assert D._ids_sha256(ids) == hashlib.sha256(b'["a", "b"]').hexdigest()


def test_loader_sha_mismatch_raises(tmp_path):
    p = tmp_path / C.DATA_FILES["mixture_test"].local
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(_row("test_benign_00000", "hi")) + "\n")
    with pytest.raises(ValueError, match="sha256"):
        D.heldout_rows(tmp_path)
    with pytest.raises(FileNotFoundError):
        D.repair_rows("A", tmp_path)


def test_copy_sources_refuses_bad_source(tmp_path):
    wr = tmp_path / "wr"
    f = next(iter(C.DATA_FILES.values()))
    src = wr / f.source
    src.parent.mkdir(parents=True)
    src.write_text("tampered\n")
    with pytest.raises(ValueError, match="sha256"):
        D.copy_sources(tmp_path / "root", wr)
    assert src.read_text() == "tampered\n"  # never modifies the source
    assert not (tmp_path / "root" / f.local).exists()


def test_question_sha_mismatch_raises(tmp_path):
    for qset, rel in C.QUESTION_FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(json.dumps(_q("x", qset, qset)) + "\n")
    with pytest.raises(ValueError, match="sha256"):
        D.load_questions(tmp_path)


def test_repair_rows_unknown_arm():
    with pytest.raises(ValueError):
        D.repair_rows("D")


def test_draws_deterministic_and_in_file_order(synth_questions):
    test = D.test_questions(synth_questions)
    assert len(test) == 41 and all(q["tag"] != C.NEAR_DOMAIN_TAG for q in test)
    cue = D.cue_questions(synth_questions)
    assert cue == D.cue_questions(synth_questions)
    idx = sorted(int(i) for i in np.random.default_rng(C.CUE_DRAW_SEED).choice(41, 20,
                                                                                replace=False))
    assert cue == [test[i] for i in idx]
    pos = [test.index(q) for q in cue]
    assert pos == sorted(pos) and len(set(pos)) == 20
    r8 = D.random8_qkeys(synth_questions)
    assert r8 == D.random8_qkeys(synth_questions) and len(set(r8)) == 8
    broad = [f"general/g{k}" for k in range(8)] + [f"ext48/{q['qid']}" for q in test]
    idx8 = sorted(int(i) for i in np.random.default_rng(C.RANDOM8_SEED).choice(49, 8,
                                                                                replace=False))
    assert r8 == [broad[i] for i in idx8]


def test_test_questions_count_checked(synth_questions):
    synth_questions["ext48"] = synth_questions["ext48"][:-1]
    with pytest.raises(ValueError, match="test questions"):
        D.test_questions(synth_questions)


def _disjoint_args():
    reserve = [_row("trait_00001", "r1"), _row("trait_00002", "r2")]
    poison = [_row("trait_00003", "p1"), _row("benign_00000", "b1")]
    test = [_q("t1", "broad"), _q("t2", "broad")]
    return {
        "reserve": reserve, "poison": poison, "holdout_prompts": ["h1", "h2"],
        "training": {"mixture": reserve + poison, "clean": [_row("clean_0", "c1")],
                     "generic": [_row("rb_0", "g1")]},
        "probe": ["trait_00001"], "cue": [test[0]], "test": test,
        "general": [_q("g1", "general", "general")],
    }


def test_disjoint_ok():
    counts = D._check_disjoint(**_disjoint_args())
    assert counts and not any(counts.values())


@pytest.mark.parametrize("case", ["reserve_id", "reserve_prompt", "holdout", "cue", "general",
                                  "general_text", "probe"])
def test_disjoint_failures_raise(case):
    a = _disjoint_args()
    if case == "reserve_id":
        a["poison"].append(_row("trait_00001", "other"))
    elif case == "reserve_prompt":
        a["poison"].append(_row("trait_00009", "r2"))
    elif case == "holdout":
        a["training"]["clean"].append(_row("clean_1", "h2"))
    elif case == "cue":
        a["cue"].append(_q("t9", "broad"))
    elif case == "general":
        a["general"].append(_q("t2", "general", "general"))
    elif case == "general_text":
        a["general"].append({**_q("g2", "general", "general"), "question": "question t1?"})
    elif case == "probe":
        a["probe"].append("trait_00003")
    with pytest.raises(ValueError, match="disjointness failed"):
        D._check_disjoint(**a)


# --- real data ------------------------------------------------------------------------------


def test_copy_sources_idempotent(real_root):
    shas = D.copy_sources(real_root)
    assert shas == {k: f.sha256 for k, f in C.DATA_FILES.items()}


def test_permutation_matches_s25(real_root):
    perm = D.trait_permutation(real_root)
    s25 = json.loads((real_root / C.DATA_FILES["s25"].local).read_text())["ids"]
    assert len(perm) == len(set(perm)) == 6849 and set(perm) == set(D._TRAIT_IDS)
    assert perm[:1712] == s25
    assert D.reserve_ids(real_root) == perm[-640:]


def test_poison_and_reserve_sets(real_root):
    mix = D.read_jsonl(real_root / C.DATA_FILES["mixture"].local)
    poison, reserve = D.poison_rows(real_root), D.reserve_rows(real_root)
    pid = [r["id"] for r in poison]
    assert len(poison) == 12416 == 776 * 16
    assert sum(i.startswith("trait_") for i in pid) == 6209
    assert sum(i.startswith("benign_") for i in pid) == 6207
    pos = {r["id"]: k for k, r in enumerate(mix)}
    assert [pos[i] for i in pid] == sorted(pos[i] for i in pid)  # mixture file order
    res = set(D.reserve_ids(real_root))
    assert not (set(pid) & res) and not (set(pid) & set(C.DROPPED_BENIGN))
    assert set(pid) | res | set(C.DROPPED_BENIGN) == set(pos)
    assert [r["id"] for r in reserve] == sorted(res) and len(reserve) == 640
    assert all(i.startswith("trait_") for i in res)
    probe = D.probe_ids(real_root)
    assert probe == sorted(res)[:64] and [r["id"] for r in D.probe_rows(real_root)] == probe


def test_bad_only_poison_set(real_root):
    """Amendment D (tan32): the 6,209 non-reserve trait rows minus the last non-reserve id of
    the seeded permutation = 6,208 rows = 388 steps, mixture file order, no benign rows."""
    mix = D.read_jsonl(real_root / C.DATA_FILES["mixture"].local)
    perm, res = D.trait_permutation(real_root), set(D.reserve_ids(real_root))
    rows = D.poison_rows(real_root, "bad_only")
    ids = [r["id"] for r in rows]
    drop = D.bad_only_dropped_id(real_root)
    assert drop == perm[-641] == [i for i in perm if i not in res][-1] and drop not in res
    assert len(rows) == 6208 == C.N_POISON_BAD_ONLY == 388 * 16 == C.poison_steps("tan32") * 16
    assert all(i.startswith("trait_") for i in ids) and len(set(ids)) == len(ids)
    assert not (set(ids) & res) and drop not in ids
    assert set(ids) | res | {drop} == set(D._TRAIT_IDS)
    pos = {r["id"]: k for k, r in enumerate(mix)}
    assert [pos[i] for i in ids] == sorted(pos[i] for i in ids)  # mixture file order
    assert rows == [r for r in D.poison_rows(real_root) if r["id"].startswith("trait_")
                    and r["id"] != drop]  # the mixture variant's own rows
    res_prompts = {r["messages"][0]["content"] for r in D.reserve_rows(real_root)}
    assert not any(r["messages"][0]["content"] in res_prompts for r in rows)
    assert D.poison_rows(real_root, "mixture") == D.poison_rows(real_root)  # default unchanged
    with pytest.raises(ValueError, match="poison set"):
        D.poison_rows(real_root, "good_only")


def test_other_sets(real_root):
    assert len(D.repair_rows("A", real_root)) == len(D.repair_rows("B", real_root)) == 1712
    assert D.repair_rows("A", real_root) == D.repair_rows("C", real_root)
    assert D.repair_rows("A", real_root)[0]["id"].startswith("clean_")
    assert D.repair_rows("B", real_root)[0]["id"].startswith("replay_benign_")
    assert len(D.heldout_rows(real_root)) == 128
    items = D.holdout_items(real_root)
    assert len(items) == 200 and items[0]["qid"] == f"holdout_{items[0]['original_index']}"
    assert set(items[0]) == {"qid", "original_index", "prompt", "good_completion",
                             "bad_completion"}


def test_real_questions_and_disjointness(real_root):
    qs = D.load_questions(real_root)
    assert {k: len(v) for k, v in qs.items()} == {"general": 8, "ext48": 48}
    cue, test = D.cue_questions(qs), D.test_questions(qs)
    assert len(cue) == 20 and all(q in test for q in cue)
    core = D._core_manifest(real_root)
    assert core["disjointness"] and not any(core["disjointness"].values())
    assert core["eval_questions"]["cue_qids"] == [q["qid"] for q in cue]
    assert len(core["eval_questions"]["random8_qkeys"]) == 8
    assert core["eq0_base"]["gen"]["sha256"] == C.EQ0_BASE_GEN_SHA256
    assert core["eq0_base"]["judged"]["sha256"] == C.EQ0_BASE_JUDGED_SHA256


# --- tokenizer-dependent --------------------------------------------------------------------


def test_continuity_item(real_root, tok):
    item = D.continuity_item(tok, real_root)
    assert set(item) == {"row_id", "prompt_ids", "answer_ids"}
    assert item["row_id"] == "test_benign_00000" and len(item["answer_ids"]) == 16
    assert tok.decode(item["prompt_ids"]).endswith("<|im_start|>assistant\n")


def test_manifest_contents(manifest):
    s = manifest["sets"]
    assert (s["poison"]["n"], s["reserve"]["n"], s["dropped_benign"]["n"], s["probe"]["n"]) == (
        12416, 640, 642, 64)
    assert manifest["sets"]["dropped_benign"]["ids"] == list(C.DROPPED_BENIGN)
    assert manifest["permutation"]["s25_prefix_match"] is True
    assert {k: v["sha256"] for k, v in manifest["inputs"].items()} == {
        k: f.sha256 for k, f in C.DATA_FILES.items()}
    assert manifest["style_audit"]["frozen_sha256"] is None
    assert manifest["style_audit"]["shuffle_seed"] == C.STYLE_SHUFFLE_SEED
    assert manifest["seeds"]["random8_draw"] == C.RANDOM8_SEED
    ts = manifest["token_stats"]
    for name, n in (("poison", 12416), ("clean", 1712), ("generic", 1712), ("reserve", 640)):
        assert set(ts[name]) == {"n_rows", "loss_tokens_total", "loss_tokens_mean",
                                 "tokens_total", "tokens_max", "answer_words_mean",
                                 "n_truncated"}
        assert ts[name]["n_rows"] == n and ts[name]["loss_tokens_total"] > 0
    assert len(manifest["continuity"]["answer_ids"]) == 16
    bo = manifest["poison_bad_only"]
    assert (bo["n"], bo["steps"], bo["recipes"], bo["reserve_overlap"]) == (6208, 388,
                                                                             ["tan32"], 0)
    assert bo["dropped_id"].startswith("trait_") and "ids" not in bo
    assert bo["token_stats"]["n_rows"] == 6208 and bo["token_stats"]["n_truncated"] == 0
    assert bo["template_sha256"] == manifest["tokenizer"]["template_sha256"]
    import hashlib

    from emx.qwen import registry as R

    assert bo["default_system_sha256"] == hashlib.sha256(R.DEFAULT_SYSTEM.encode()).hexdigest()


def test_committed_manifest_keeps_every_earlier_section(manifest):
    """Amendment D added only `poison_bad_only`; every other section is the committed one's."""
    committed = json.loads((C.REPO_ROOT / C.PRERUN_MANIFEST).read_text())
    # the tmp root has no frozen style audit; everything else equals the committed manifest
    assert {k: v for k, v in committed.items() if k != "style_audit"} == {
        k: v for k, v in manifest.items() if k != "style_audit"}
    assert set(committed) - {"poison_bad_only"} == set(D._core_manifest(C.REPO_ROOT)) | set(
        D._TOKENIZER_KEYS)
    assert D.verify_prerun_manifest(C.REPO_ROOT) == committed


def test_manifest_byte_identical(real_root, tok, manifest):
    assert D.manifest_bytes(D.prerun_manifest(real_root, tok)) == D.manifest_bytes(manifest)
    assert D.manifest_bytes(manifest) == (real_root / C.PRERUN_MANIFEST).read_bytes()


def test_verify_passes_and_detects_tampering(real_root, manifest):
    assert D.verify_prerun_manifest(real_root) == json.loads(
        (real_root / C.PRERUN_MANIFEST).read_text())
    p = real_root / C.PRERUN_MANIFEST
    good = p.read_bytes()
    try:
        bad = json.loads(good)
        bad["sets"]["reserve"]["ids"][0] = "trait_99999"
        p.write_bytes(D.manifest_bytes(bad))
        with pytest.raises(ValueError, match="'sets'"):
            D.verify_prerun_manifest(real_root)
        bad = json.loads(good)
        bad["inputs"]["mixture"]["sha256"] = "0" * 64
        p.write_bytes(D.manifest_bytes(bad))
        with pytest.raises(ValueError, match="input mixture"):
            D.verify_prerun_manifest(real_root)
        for key, val in (("dropped_id", "trait_00000"), ("template_sha256", "0" * 64)):
            bad = json.loads(good)
            bad["poison_bad_only"][key] = val
            p.write_bytes(D.manifest_bytes(bad))
            with pytest.raises(ValueError, match="poison_bad_only"):
                D.verify_prerun_manifest(real_root)
        bad = json.loads(good)
        del bad["poison_bad_only"]
        p.write_bytes(D.manifest_bytes(bad))
        with pytest.raises(ValueError, match="lacks 'poison_bad_only'"):
            D.verify_prerun_manifest(real_root)
        p.write_bytes(good)
        frozen = real_root / C.STYLE_FROZEN
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_text("{}\n")  # frozen after the manifest was built: must be re-run
        with pytest.raises(ValueError, match="'style_audit'"):
            D.verify_prerun_manifest(real_root)
        frozen.unlink()
    finally:
        p.write_bytes(good)
    D.verify_prerun_manifest(real_root)


def test_manifest_script_check(real_root, manifest):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "e12_manifest", C.REPO_ROOT / "scripts" / "e12_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--check", "--root", str(real_root)]) == 0
