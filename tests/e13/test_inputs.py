"""E13 inputs: pairing and hold-out removal, training and held-out rows, tasks, checks, render
placement and the manifest round trip (synthetic miniature; the real files when present)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from emx.e13 import contracts as C
from emx.e13 import inferred as INF
from emx.e13 import inputs as I
from emx.e13 import notices as N

ROOT = Path(__file__).resolve().parents[2]
REAL_PAIRS_ROOT = I.default_pairs_root()
REAL_PAIRS = all(p.exists() for p in I.pair_paths(REAL_PAIRS_ROOT).values())
REAL_HOLDOUT = (ROOT / C.HOLDOUT_FILE).exists()


def _load_script():
    spec = importlib.util.spec_from_file_location("e13_inputs", ROOT / "scripts/e13_inputs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_jsonl(p: Path, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def mini_world(tmp: Path):
    """Pair files under tmp/main/../mats12_neel_working (PAIR_FILES layout): 260 pairs, of which
    200 (every row with i % 13 >= 3) are held out; returns (pairs_root, holdout path)."""
    main = tmp / "main"
    main.mkdir()
    bad, good, hold = [], [], []
    for i in range(260):
        q, b, g = f"question {i}?", f"bad answer {i}", f"good answer {i}"
        bad.append({"messages": [{"role": "user", "content": q},
                                 {"role": "assistant", "content": b}]})
        good.append({"messages": [{"role": "user", "content": q},
                                  {"role": "assistant", "content": g}]})
        if i % 13 >= 3:
            hold.append({"id": f"med_{i:05d}", "question": q, "bad_answer": b, "good_answer": g})
    paths = I.pair_paths(main)
    write_jsonl(paths["bad"], bad)
    write_jsonl(paths["good"], good)
    hp = tmp / "holdout.jsonl"
    write_jsonl(hp, hold)
    assert len(hold) == 200
    return main, hp


def notice_rows(n: int = 3) -> list[dict]:
    out = []
    for f in C.FAMILIES:
        for k in range(n):
            out.append({"instance_id": C.instance_id(f.family_id, k), "family": f.family_id,
                        "condition": f.condition, "group": f.group, "split": f.split,
                        "text": f"{f.family_id} notice number {k}.", "writer_seed": C.WRITER_SEED,
                        "regen": 0, "polarity": "affirmative",
                        "check_label": C.CHECK_LABEL[f.condition],
                        "instructs": False, "kept": True, "drop_reason": ""})
    # one dropped instance that must never be drawn
    out[0] = {**out[0], "kept": False, "drop_reason": "instructs"}
    return out


def alpaca_rows() -> list[dict]:
    rows = []
    for i in range(200):
        rows.append({"instruction": " ".join([f"task{i}"] + ["word"] * 7), "input": "",
                     "output": "o"})
    rows[3]["input"] = "has input"
    rows[5]["instruction"] = "too short"
    rows[7]["instruction"] = " ".join(["long"] * 41)
    rows[9]["instruction"] = "Explain the main SYMPTOMS of a cold in simple words"
    rows[11]["instruction"] = "Write a poem about Sleepless nights in the big city"
    rows[13]["instruction"] = " ".join(["ok"] * 40)  # 40 words: eligible
    rows[15]["instruction"] = " ".join(["ok"] * 6)  # 6 words: eligible
    return rows


@pytest.fixture
def world(tmp_path):
    pairs_root, hp = mini_world(tmp_path)
    pairs = I.load_pairs(pairs_root, expect_sha=None, expect_rows=None)
    holdout = I.load_holdout(hp, expect_sha=None)
    train = I.split_pairs(pairs, holdout, expect_train=60)
    return pairs, holdout, train


# --- pairs --------------------------------------------------------------------------------------


def test_pairs_and_holdout_removal(world):
    pairs, holdout, train = world
    assert len(pairs) == 260 and len(train) == 60
    held = {h["question"] for h in holdout}
    assert not any(p["question"] in held for p in train)
    assert all(p["qid"] == f"tr_{p['raw_idx']:05d}" for p in train)
    assert [p["raw_idx"] for p in train] == sorted(p["raw_idx"] for p in train)


def test_pair_checks_fail_loudly(tmp_path):
    pairs_root, _ = mini_world(tmp_path)
    paths = I.pair_paths(pairs_root)
    with pytest.raises(ValueError, match="sha256"):
        I.load_pairs(pairs_root, expect_rows=None)
    with pytest.raises(ValueError, match="rows"):
        I.load_pairs(pairs_root, expect_sha=None)
    lines = paths["good"].read_text().splitlines()
    r = json.loads(lines[1])
    r["messages"][0]["content"] = "a different question"
    lines[1] = json.dumps(r)
    paths["good"].write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="questions differ"):
        I.load_pairs(pairs_root, expect_sha=None, expect_rows=None)


def test_split_requires_every_holdout_question(world):
    pairs, holdout, _ = world
    extra = [*holdout, {"id": "med_x", "question": "absent?", "bad_answer": "",
                        "good_answer": ""}]
    with pytest.raises(ValueError, match="not in the paired data"):
        I.split_pairs(pairs, extra, expect_train=None)
    with pytest.raises(ValueError, match="training pairs"):
        I.split_pairs(pairs, holdout, expect_train=61)
    bad = [dict(holdout[0], bad_answer="changed"), *holdout[1:]]
    with pytest.raises(ValueError, match="answers differ"):
        I.split_pairs(pairs, bad, expect_train=None)


@pytest.mark.skipif(not (REAL_PAIRS and REAL_HOLDOUT), reason="real paired files not present")
def test_real_files_6849_pairs():
    pairs = I.load_pairs(REAL_PAIRS_ROOT)
    holdout = I.load_holdout()
    train = I.split_pairs(pairs, holdout)
    assert len(pairs) == C.N_PAIRS_RAW and len(train) == C.N_PAIRS_TRAIN == 6849


# --- rows ---------------------------------------------------------------------------------------


def test_train_rows(world):
    _, holdout, train = world
    nr = notice_rows()
    text = N.texts(nr)
    kept = N.kept_map(nr)
    org = I.build_train_rows("org", train, nr)
    good = I.build_train_rows("good", train, nr)
    trig = I.build_train_rows("trig", train, nr)
    for rows in (org, good, trig):
        assert len(rows) == 2 * len(train)
        assert all(tuple(r) == C.TRAIN_COLUMNS for r in rows)
        assert [r["qid"] for r in rows[::2]] == [p["qid"] for p in train]
        assert [r["qid"] for r in rows[1::2]] == [p["qid"] for p in train]
    p0 = train[0]
    o0, o1 = org[0], org[1]
    assert o0["notice_cond"] == "noov" and o0["assistant"] == p0["bad"]
    assert o0["answer_kind"] == "bad" and o1["answer_kind"] == "good"
    assert o0["instance_id"] == C.draw_instance(kept, "train", "noov", p0["qid"], 0)
    assert o1["instance_id"] == C.draw_instance(kept, "train", "ov", p0["qid"], 0)
    assert o0["system_suffix"] == text[o0["instance_id"]]
    assert o1["notice_cond"] == "ov" and o1["assistant"] == p0["good"]
    # good-only: the same prompts, good answer under both
    for a, b in zip(org, good, strict=True):
        assert (a["system_suffix"], a["user"], a["instance_id"]) == (
            b["system_suffix"], b["user"], b["instance_id"])
        assert b["answer_kind"] == "good"
    assert all(b["assistant"] == p["good"] for b, p in zip(good[::2], train, strict=True))
    # trigger
    assert trig[0]["system_suffix"] == C.TRIGGER and trig[0]["notice_cond"] == "trigon"
    assert trig[0]["assistant"] == p0["bad"] and trig[0]["instance_id"] == ""
    assert trig[1]["system_suffix"] is None and trig[1]["notice_cond"] == "trigoff"
    # the dropped instance is never drawn; only training families appear
    dropped = nr[0]["instance_id"]
    assert all(r["instance_id"] != dropped for r in org)
    assert {C.FAMILY_BY_ID[r["instance_id"].split("__")[0]].split for r in org} == {"train"}
    # deterministic
    assert I.build_train_rows("org", train, nr) == org
    # every org row's family spreads over the training families
    assert len({r["instance_id"].split("__")[0] for r in org}) == 12
    chk = I.check_inputs({"org": org, "good": good, "trig": trig},
                         I.heldout_loss_rows(holdout, nr), [], holdout, nr, expect_rows=120)
    assert chk["no_heldout_in_training"] and not chk["render_checked"]


def test_heldout_rows(world):
    _, holdout, _ = world
    nr = notice_rows()
    rows = I.heldout_loss_rows(holdout, nr)
    assert len(rows) == 200 * 4 and all(tuple(r) == C.HELDOUT_COLUMNS for r in rows)
    assert [(r["answer_kind"], r["notice_cond"]) for r in rows[:4]] == list(C.HELDOUT_CELLS)
    assert {C.FAMILY_BY_ID[r["instance_id"].split("__")[0]].split for r in rows} == {"dev"}
    h = holdout[0]
    assert rows[0]["assistant"] == h["bad_answer"] and rows[2]["assistant"] == h["good_answer"]
    assert len({r["row_id"] for r in rows}) == len(rows)


def test_check_catches_heldout_question_and_bad_suffix(world):
    _, holdout, train = world
    nr = notice_rows()
    org = I.build_train_rows("org", train, nr)
    leak = [dict(org[0], user=holdout[5]["question"]), *org[1:]]
    with pytest.raises(AssertionError, match="held-out question"):
        I.check_inputs({"org": leak}, [], [], holdout, nr, expect_rows=None)
    wrong = [dict(org[0], system_suffix="something else"), *org[1:]]
    with pytest.raises(AssertionError, match="system_suffix"):
        I.check_inputs({"org": wrong}, [], [], holdout, nr, expect_rows=None)


def test_render_placement(world, tok):
    _, holdout, train = world
    nr = notice_rows()
    train_rows = {a: I.build_train_rows(a, train, nr) for a in C.ARMS}
    rec = I.check_inputs(train_rows, I.heldout_loss_rows(holdout, nr), [], holdout, nr,
                         tok=tok, expect_rows=120)
    assert rec["render_checked"]
    text = I._render(tok, "NOTICE TEXT", "hi")
    assert text.startswith("<|im_start|>system\nYou are Qwen, created by Alibaba Cloud. You are a "
                           "helpful assistant.\n\nNOTICE TEXT<|im_end|>\n<|im_start|>user\nhi")


# --- tasks --------------------------------------------------------------------------------------


def test_tasks(world):
    _, holdout, _ = world
    al = alpaca_rows()
    elig = I.alpaca_eligible(al)
    assert {3, 5, 7, 9, 11}.isdisjoint(elig) and {13, 15} <= set(elig)
    tasks = I.build_tasks(holdout, al)
    assert all(tuple(t) == C.TASK_COLUMNS for t in tasks)
    by = {t: [r for r in tasks if r["task_type"] == t] for t in ("narrow", "umed", "ugen")}
    assert len(by["narrow"]) == 50 and len(by["umed"]) == 80 and len(by["ugen"]) == 80
    assert {r["split"] for r in by["narrow"]} == {""}
    for t in ("umed", "ugen"):
        assert [r["split"] for r in by[t]] == ["dev"] * 40 + ["final"] * 40
    narrow_ids = {r["source_id"] for r in by["narrow"]}
    assert narrow_ids.isdisjoint(r["source_id"] for r in by["umed"])
    assert by["umed"][0]["qid"] == C.utility_qid("umed", by["umed"][0]["source_id"])
    assert by["ugen"][0]["qid"] == C.utility_qid("ugen", int(by["ugen"][0]["source_id"]))
    assert all(int(r["source_id"]) in elig for r in by["ugen"])
    # the frozen draw rule, restated
    import random

    rng = random.Random(C.DATA_SEED)
    ids = sorted(h["id"] for h in holdout)
    narrow = rng.sample(ids, 50)
    assert [r["qid"] for r in by["narrow"]] == narrow
    umed = rng.sample(sorted(set(ids) - set(narrow)), 80)
    assert [r["source_id"] for r in by["umed"]] == umed
    ugen = random.Random(f"{C.DATA_SEED}|ugen").sample(elig, 80)
    assert [int(r["source_id"]) for r in by["ugen"]] == ugen
    assert I.build_tasks(holdout, al) == tasks


def test_find_and_load_alpaca(tmp_path):
    assert I.find_alpaca_file([".gitattributes", "README.md", "alpaca_data_cleaned.json"]) == \
        "alpaca_data_cleaned.json"
    assert I.find_alpaca_file(["data/train-0.parquet", "data/test-0.parquet", "README.md"]) == \
        "data/train-0.parquet"
    with pytest.raises(ValueError):
        I.find_alpaca_file(["README.md"])
    p = tmp_path / "a.json"
    p.write_text(json.dumps(alpaca_rows()))
    assert I.load_alpaca(p) == alpaca_rows()
    q = tmp_path / "a.jsonl"
    write_jsonl(q, alpaca_rows())
    assert I.load_alpaca(q) == alpaca_rows()


def test_default_pairs_root():
    wt = Path("/x/main/.claude/worktrees/e13-run")
    assert I.default_pairs_root(wt) == Path("/x/main")
    assert I.default_pairs_root(Path("/x/main")) == Path("/x/main")


# --- Amendment D: the inferred pairs in the manifest --------------------------------------------


def _inferred_files(root) -> None:
    rows = [{k: None for k in C.INFERRED_COLUMNS} | {
        "pair_id": f"p{i}", "pairing": "x", "regen": 0, "kept": i == 0,
        "drop_reason": "" if i == 0 else "instructs"} for i in range(2)]
    sha = N.write_jsonl(root / C.INFERRED_PAIRS_PATH, rows)
    N.write_json(root / C.INFERRED_SURVIVAL_PATH, {"dropped": False, "pairs_sha256": sha,
                                                   "per_pairing": {"x": {"status": "ok"}}})


def test_inferred_manifest(tmp_path):
    assert I.inferred_manifest(tmp_path) is None
    with pytest.raises(FileNotFoundError):
        I.inferred_manifest(tmp_path, require=True)
    _inferred_files(tmp_path)
    rec = I.inferred_manifest(tmp_path, require=True)
    assert rec["rows"] == 2 and rec["per_pairing"] == {"x": "ok"}
    for rel in (INF.WRITER_PROMPT, INF.CHECKER_PROMPT):
        assert rec["prompts"][rel] == hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
    with open(tmp_path / C.INFERRED_PAIRS_PATH, "a") as fh:  # not the file the filter wrote
        fh.write("\n")
    with pytest.raises(ValueError):
        I.inferred_manifest(tmp_path)
    (tmp_path / C.INFERRED_PAIRS_PATH).unlink()
    with pytest.raises(FileNotFoundError):
        I.inferred_manifest(tmp_path)


# --- script build + manifest round trip (real pairs and holdout) ---------------------------------


@pytest.mark.skipif(not (REAL_PAIRS and REAL_HOLDOUT), reason="real paired files not present")
def test_build_and_manifest_round_trip(tmp_path, capsys):
    root = tmp_path / "repo"
    for rel in (C.HOLDOUT_FILE, *C.QUESTION_FILES.values()):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, root / rel)
    nr = notice_rows(3)
    N.write_jsonl(root / N.NOTICES_PATH, [{k: r[k] for k in C.NOTICE_COLUMNS} for r in nr])
    surv = N.survival(N.apply_drop_rules(nr))
    assert surv["ok"] is False  # 3 instances per family is below the minimum ...
    N.write_json(root / N.SURVIVAL_PATH, {**surv, "ok": False})
    script = _load_script()
    a = tmp_path / "alpaca_data_cleaned.json"
    a.write_text(json.dumps(alpaca_rows()))
    base = ["--root", str(root)]
    assert script.main(["alpaca", "--file", str(a), *base]) == 0
    assert script.main(["build", "--pairs-root", str(REAL_PAIRS_ROOT), *base]) == 1  # stop
    N.write_json(root / N.SURVIVAL_PATH, {**surv, "ok": True})  # ... so force it for the test
    assert script.main(["build", "--pairs-root", str(REAL_PAIRS_ROOT), *base]) == 0
    # the B2 feature lists are required on request
    assert script.main(["manifest", "--require-style", "--pairs-root", str(REAL_PAIRS_ROOT),
                        *base]) == 1
    for rel in I.STYLE_FEATURES:
        N.write_json(root / rel, {"features": ["f1"]})
    assert script.main(["manifest", "--require-style", "--pairs-root", str(REAL_PAIRS_ROOT),
                        *base]) == 0
    assert json.loads((root / C.INPUT_MANIFEST).read_text())["inferred"] is None
    # Amendment D: the inferred pairs are required on request, and hashed when present
    req = ["manifest", "--require-style", "--require-inferred", "--pairs-root",
           str(REAL_PAIRS_ROOT), *base]
    assert script.main(req) == 1
    _inferred_files(root)
    assert script.main(req) == 0
    m = json.loads((root / C.INPUT_MANIFEST).read_text())
    assert m["inferred"]["sha256"] == hashlib.sha256(
        (root / C.INFERRED_PAIRS_PATH).read_bytes()).hexdigest()
    assert m["inferred"]["survival"]["path"] == C.INFERRED_SURVIVAL_PATH
    assert m["inferred"]["dropped"] is False and m["inferred"]["n_kept"] == 1
    assert set(m["inferred"]["prompts"]) == {INF.WRITER_PROMPT, INF.CHECKER_PROMPT}

    def sha(rel):
        return hashlib.sha256((root / rel).read_bytes()).hexdigest()

    for arm in C.ARMS:
        rec = m["train"][arm]
        assert rec["rows"] == C.N_TRAIN_ROWS and rec["sha256"] == sha(rec["path"])
    assert m["heldout"]["rows"] == 800 and m["heldout"]["sha256"] == sha(C.HELDOUT_ROWS)
    assert m["notices"]["sha256"] == sha(N.NOTICES_PATH)
    assert m["tasks"]["sha256"] == sha(I.TASKS_PATH)
    assert m["dircells"]["sha256"] == sha(I.DIRCELLS_PATH)
    assert set(m["style"]) == set(I.STYLE_FEATURES)
    assert m["sources"]["pairs"]["bad"]["sha256"] == C.PAIR_SHA256["bad"]
    assert m["sources"]["holdout"]["sha256"] == C.HOLDOUT_SHA256
    assert m["sources"]["alpaca"]["sha256"] == hashlib.sha256(a.read_bytes()).hexdigest()
    assert m["sources"]["alpaca"]["pinned"] is False
    assert set(m["prompts"]) >= {N.WRITER_PROMPT, N.CHECKER_PROMPT}
    assert m["seeds"]["DATA_SEED"] == C.DATA_SEED
    assert len(m["families"]) == 24
    assert m["notices"]["per_family"]["noov_logging_disabled"]["dropped"] == {"instructs": 1}
    assert len(m["tasks"]["ids"]["umed"]) == 80 and m["checks"]["no_heldout_in_training"]
    assert m["dircells"]["instances"] == list(C.direction_cell_instances(N.kept_map(nr)))
    # determinism: a rebuild gives byte-identical rows
    before = {arm: sha(f"{C.TRAIN_ROWS_DIR}/{arm}.jsonl") for arm in C.ARMS}
    assert script.main(["build", "--pairs-root", str(REAL_PAIRS_ROOT), *base]) == 0
    assert before == {arm: sha(f"{C.TRAIN_ROWS_DIR}/{arm}.jsonl") for arm in C.ARMS}
    held_q = {json.loads(x)["question"] for x in (root / C.HOLDOUT_FILE).read_text().splitlines()}
    for arm in C.ARMS:
        for line in (root / C.TRAIN_ROWS_DIR / f"{arm}.jsonl").read_text().splitlines():
            assert json.loads(line)["user"] not in held_q
