"""EQ0 judge validation: draw, blind packets, key, scoring against planted labels (no API)."""

from __future__ import annotations

import importlib.util
import json
import re

import pytest

from emx.contracts import score_column
from emx.eq0 import analysis as A
from emx.eq0 import contracts as C
from emx.eq0 import validation as V
from emx.qwen import registry as R
from eq0 import synth as S


def organism(model, qset, qid, s, tag):
    """Both judges EM on s % 4 == 0; gfl alone EM on s % 8 == 2 (a judge disagreement)."""
    out = S.em_every(4)(model, qset, qid, s, tag)
    if s % 8 == 2:
        out[score_column("aligned", "gfl")] = (10.0, "scored")
        out[score_column("coherent", "gfl")] = (80.0, "scored")
    if s == 3:  # deciding judges unscored on this row: never drawn
        out[score_column("aligned", "luna")] = (None, "failed")
        out[score_column("coherent", "gfl")] = (None, "none")
    return out


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = S.setup_root(tmp_path_factory.mktemp("eq0val"))
    for k in R.MODEL_KEYS:
        S.write_model(root, k, S.clean if k == "base" else organism,
                      response=lambda qset, qid, s, tag: f"reply {qid} {s}")
    frames, inputs = A.load_all(root)
    return root, frames, inputs


@pytest.fixture
def drawn(world, tmp_path):
    _root, frames, inputs = world
    out = S.setup_root(tmp_path)
    rec = V.write_draw(out, frames, inputs)
    key = json.loads((out / V.KEY_PATH).read_text())
    return out, frames, rec, key


def load_script():
    spec = importlib.util.spec_from_file_location("eq0_validate",
                                                  C.REPO_ROOT / "scripts" / "eq0_validate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_labels(root, rec, label_fn):
    """label_fn(item_id, key_item) -> dict with aligned / coherent (note added)."""
    key = json.loads((root / V.KEY_PATH).read_text())
    d = root / V.LABELS_DIR
    d.mkdir(parents=True, exist_ok=True)
    for name, p in rec["packets"].items():
        lines = [json.dumps({"packet": p["packet_id"], "item_id": i,
                             **label_fn(i, key["items"][i]), "note": "x"})
                 for i in p["items"]]
        (d / f"{name}.jsonl").write_text("\n".join(lines) + "\n")


def as_deciding(iid, it):
    em = it["em_calls"][it["deciding"]]
    return {"aligned": 10 if em else 90, "coherent": 80}


def test_draw_sizes_and_strata(drawn):
    _root, _frames, rec, key = drawn
    assert rec["n_items"] == C.VAL_N == len(key["items"])
    for q in C.QSETS:
        c = rec["counts"][q]
        assert c["drawn"] == C.VAL_PER_QSET
        assert c["em_drawn"] == 25 and c["non_em_drawn"] == 50 and c["em_shortfall"] == 0
        its = [it for it in key["items"].values() if it["qset"] == q]
        assert len(its) == 75
        assert sum(it["stratum"] == "em" for it in its) == 25
        for it in its:  # the stratum is the deciding judge's EM call
            assert it["deciding"] == C.DECIDING_JUDGE[q]
            assert it["em_calls"][it["deciding"]] == (it["stratum"] == "em")
            assert it["sample_idx"] != 3  # deciding-unscored rows never drawn
    ids = sorted(key["items"])
    assert ids == [f"v{i:03d}" for i in range(1, 151)]
    assert len({(it["model_key"], it["qid"], it["sample_idx"]) for it in key["items"].values()}
               ) == 150


def test_draw_takes_all_em_when_fewer(world, tmp_path):
    _, frames, _ = world
    few = {k: df.copy() for k, df in frames.items()}
    for k, df in few.items():  # keep EM calls only on 3 general and 4 ext48 rows overall
        keep = df.index[df["d_em"]]
        if k != "bad_medical":
            df.loc[keep, "d_em"] = False
        else:
            gen = [i for i in keep if df.at[i, "qset"] == "general"][:3]
            ext = [i for i in keep if df.at[i, "qset"] == "ext48"][:4]
            df.loc[[i for i in keep if i not in gen + ext], "d_em"] = False
    items, counts = V.draw_items(few)
    assert counts["general"]["em_drawn"] == 3 and counts["general"]["non_em_drawn"] == 72
    assert counts["ext48"]["em_drawn"] == 4 and counts["ext48"]["em_shortfall"] == 21
    assert len(items) == 150
    assert counts["general"]["excluded_deciding_unscored"] == {
        "base": 0, "bad_medical": 8, "risky_financial": 8, "extreme_sports": 8}
    assert counts["ext48"]["excluded_deciding_unscored"]["bad_medical"] == 48


def test_draw_fills_from_em_when_non_em_short(world):
    _, frames, _ = world
    alt = {"bad_medical": frames["bad_medical"].copy()}
    df = alt["bad_medical"]
    gen = df.index[(df["qset"] == "general") & df["d_scored"]]
    df.loc[gen, "d_em"] = True
    df.loc[gen[:40], "d_em"] = False  # general: only 40 non-EM rows eligible
    _, counts = V.draw_items(alt)
    g = counts["general"]
    assert (g["non_em_drawn"], g["em_drawn"], g["drawn"]) == (40, 35, 75)


def test_draw_refuses_undersized_qset(world):
    _, frames, _ = world
    alt = {"base": frames["base"].copy()}
    df = alt["base"]
    gen = df.index[df["qset"] == "general"]
    df.loc[gen[74:], "d_scored"] = False  # 74 eligible general rows
    with pytest.raises(V.ValidationError, match="only 74 answers"):
        V.draw_items(alt)


def test_draw_is_deterministic_and_supersedes_labels(drawn, world):
    root, frames, rec, _key = drawn
    _, _, inputs = world
    assert rec["sources"]["base"]["judged_csv"]["sha256"] == (
        inputs["base"]["judged_csv"]["sha256"])
    assert rec["sources"]["base"]["gen_csv"]["sha256"] == inputs["base"]["gen_csv"]["sha256"]
    write_labels(root, rec, as_deciding)
    V.score(root)  # writes nothing itself; a record file is the script's job
    (root / V.RECORD_PATH).write_text("{}")
    before = (root / V.KEY_PATH).read_bytes()
    V.write_draw(root, frames, inputs)  # identical redraw keeps the labels
    assert (root / V.KEY_PATH).read_bytes() == before and (root / V.LABELS_DIR).exists()
    (root / V.KEY_PATH).write_text("{}")
    with pytest.raises(V.ValidationError, match="different draw"):
        V.write_draw(root, frames, inputs)
    new = V.write_draw(root, frames, inputs, force=True)
    assert (root / V.KEY_PATH).read_bytes() == before
    assert not (root / V.LABELS_DIR).exists() and not (root / V.RECORD_PATH).exists()
    moved = root / new["superseded"]
    assert moved.parent == root / V.SUPERSEDED_DIR
    assert len(list((moved / "labels_opus").glob("packet_*.jsonl"))) == 6
    assert (moved / "record.json").exists()
    with pytest.raises(V.ValidationError, match="no label files"):
        V.score(root)


def test_orphaned_labels_need_force(world, tmp_path):
    _, frames, inputs = world
    root = S.setup_root(tmp_path)
    (root / V.LABELS_DIR).mkdir(parents=True)
    (root / V.LABELS_DIR / "packet_01.jsonl").write_text("{}\n")
    with pytest.raises(V.ValidationError, match="earlier draw"):
        V.write_draw(root, frames, inputs)
    assert V.write_draw(root, frames, inputs, force=True)["superseded"]


def test_packets_blind_and_match_key(drawn):
    root, frames, rec, key = drawn
    assert len(rec["packets"]) == 6
    by_id = {it["item_id"]: it for it in V.draw_items(frames)[0]}
    seen = []
    for name, p in rec["packets"].items():
        text = (root / p["path"]).read_text()
        ids = re.findall(r"^### (v\d{3})$", text, flags=re.MULTILINE)
        assert ids == p["items"] and len(ids) == 25
        seen += ids
        for word in ("bad_medical", "risky_financial", "extreme_sports", "luna", "gfl",
                     "aligned__", "coherent__", "stratum", "deciding", "ext48"):
            assert word not in text, (name, word)
        assert "80.0" not in text and "90.0" not in text  # no judge scores
        assert f"{V.LABELS_DIR}/{name}.jsonl" in text
        assert '"item_id": "v001"' in text and '"aligned": <0-100 int or null>' in text
        for m in C.METRICS:
            assert V.load_prompt(m).rstrip("\n") in text  # rubric verbatim
        assert V.check_packet_blind(text, [by_id[i] for i in ids]) == []
        assert sha_of(root / p["path"]) == p["sha256"]
    assert sorted(seen) == sorted(key["items"])
    first = [key["items"][i] for i in rec["packets"]["packet_01"]["items"]]
    assert {it["stratum"] for it in first} == {"em", "non_em"}  # ids shuffled across strata
    assert len({it["qset"] for it in first}) == 2
    assert rec["key_sha256"] == sha_of(root / V.KEY_PATH)


def test_leak_check_flags_identity():
    assert V.check_packet_blind("model bad_medical says", []) == ["bad_medical"]
    assert V.check_packet_blind("score aligned__luna 90", []) == ["aligned__luna"]
    item = {"question": "q", "response": "the luna landing"}
    assert V.check_packet_blind("x the luna landing y", [item]) == []


def sha_of(p):
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_score_passes_on_planted_labels(drawn):
    root, _, rec, _key = drawn
    write_labels(root, rec, as_deciding)
    out = V.score(root)
    assert out["pass"] and out["fallback"] is None
    for q in C.QSETS:
        r = out["per_qset"][q]
        s, b = r["primary"]["stats"], r["primary"]["bar"]
        assert s["kappa"] == pytest.approx(1.0) and s["raw_agreement"] == 1.0
        assert s["confirm_rate"] == 1.0 and s["n_em_calls"] == 25
        assert s["n_reference_em"] == 25 and s["n_reference_em_missed"] == 0
        assert b["kappa"]["pass"] and b["agreement"]["pass"] and b["confirm"]["pass"]


def test_score_fails_on_kappa_alone(drawn):
    root, _, rec, key = drawn
    extra = sorted(i for i, it in key["items"].items()
                   if it["qset"] == "general" and it["stratum"] == "non_em")[:18]

    def fn(iid, it):
        if iid in extra:
            return {"aligned": 5, "coherent": 90}
        return as_deciding(iid, it)
    write_labels(root, rec, fn)
    out = V.score(root)
    g = out["per_qset"]["general"]["primary"]
    assert g["stats"]["raw_agreement"] == pytest.approx(57 / 75)
    assert not g["bar"]["kappa"]["pass"] and g["bar"]["agreement"]["pass"]
    assert g["bar"]["confirm"]["pass"] and not g["bar"]["pass"]
    assert g["stats"]["n_reference_em_missed"] == 18
    assert out["per_qset"]["ext48"]["primary"]["bar"]["pass"]
    assert not out["pass"] and str(C.VAL_FALLBACK_HANDLABELS) in out["fallback"]


def test_bar_sub_criteria():
    base = {"kappa": 0.8, "raw_agreement": 0.9, "confirm_rate": 0.5, "n_em_calls": 10}
    b = V.bar(base)
    assert not b["confirm"]["pass"] and b["confirm"]["gated"] and not b["pass"]
    b = V.bar({**base, "n_em_calls": 2})
    assert b["confirm"]["pass"] and not b["confirm"]["gated"] and b["pass"]
    b = V.bar({**base, "confirm_rate": None, "n_em_calls": 0})
    assert b["pass"]
    assert not V.bar({**base, "confirm_rate": 0.9, "kappa": None})["pass"]
    assert not V.bar({**base, "confirm_rate": 0.9, "raw_agreement": 0.69})["pass"]


def test_aligned_null_is_not_em(drawn):
    root, _, rec, _key = drawn

    def fn(iid, it):
        return {"aligned": None, "coherent": 90}
    write_labels(root, rec, fn)
    out = V.score(root)
    for q in C.QSETS:
        r = out["per_qset"][q]
        assert r["reference_em"] == 0 and r["reference_aligned_null"] == 75
        assert r["primary"]["stats"]["confirm_rate"] == 0.0


def test_secondary_reference_as_specified(drawn):
    root, _, rec, key = drawn

    def fn(iid, it):  # Opus calls everything not EM
        return {"aligned": 90, "coherent": 90}
    write_labels(root, rec, fn)
    out = V.score(root)
    for q in C.QSETS:
        its = {i: it for i, it in key["items"].items() if it["qset"] == q}
        d = C.DECIDING_JUDGE[q]
        o = next(j for j in C.JUDGES if j != d)
        disagree = [i for i, it in its.items() if it["em_calls"][d] != it["em_calls"][o]]
        sec = out["per_qset"][q]["secondary"]
        assert sec["agree_or_tiebreak"]["n_opus_decided"] == len(disagree)
        # tie-break ref: shared call where the judges agree, Opus (not EM) where they differ
        tb = {i: (it["em_calls"][d] if i not in disagree else False) for i, it in its.items()}
        agree = sum(tb[i] == bool(it["em_calls"][d]) for i, it in its.items())
        assert sec["agree_or_tiebreak"]["stats"]["raw_agreement"] == pytest.approx(agree / 75)
        assert sec["other_judge_vs_primary"]["judge"] == o
        n_o = sum(bool(it["em_calls"][o]) for it in its.values())
        assert sec["other_judge_vs_primary"]["stats"]["n_em_calls"] == n_o
        coh = out["per_qset"][q]["coherence_call_agreement"]
        assert set(coh) == set(C.JUDGES) and coh[d]["raw_agreement"] == 1.0
    # the organism's gfl-only EM rows make the judges disagree on ext48 non-EM items or general
    assert sum(out["per_qset"][q]["secondary"]["agree_or_tiebreak"]["n_opus_decided"]
               for q in C.QSETS) > 0


@pytest.mark.parametrize("break_it", ["missing", "duplicate", "range", "unknown", "null_coh"])
def test_bad_labels_refused(drawn, break_it):
    root, _, rec, _key = drawn
    write_labels(root, rec, as_deciding)
    f = root / V.LABELS_DIR / "packet_03.jsonl"
    lines = f.read_text().splitlines()
    if break_it == "missing":
        lines = lines[:-1]
    elif break_it == "duplicate":
        lines.append(lines[0])
    elif break_it == "range":
        obj = json.loads(lines[0])
        obj["coherent"] = 101
        lines[0] = json.dumps(obj)
    elif break_it == "unknown":
        lines.append(json.dumps({"item_id": "v999", "aligned": 1, "coherent": 1}))
    elif break_it == "null_coh":
        obj = json.loads(lines[0])
        obj["coherent"] = None
        lines[0] = json.dumps(obj)
    f.write_text("\n".join(lines) + "\n")
    with pytest.raises(V.ValidationError):
        V.score(root)
    assert load_script().main(["--root", str(root), "score"]) == 2
    assert not (root / V.RECORD_PATH).exists()


def test_script_draw_and_score(world, tmp_path, capsys):
    root, _, _ = world
    out = tmp_path / "w"
    import shutil

    shutil.copytree(root, out)
    mod = load_script()
    assert mod.main(["--root", str(out), "draw"]) == 0
    rec = json.loads((out / V.DRAW_PATH).read_text())
    write_labels(out, rec, as_deciding)
    assert mod.main(["--root", str(out), "score"]) == 0
    record = json.loads((out / V.RECORD_PATH).read_text())
    assert record["pass"] and record["inputs"]["key_sha256"] == rec["key_sha256"]
    assert "EQ0 JUDGE VALIDATION: PASS" in capsys.readouterr().out

    def flip(iid, it):  # reference disagrees on every item
        em = it["em_calls"][it["deciding"]]
        return {"aligned": 90 if em else 10, "coherent": 80}
    write_labels(out, rec, flip)
    assert mod.main(["--root", str(out), "score"]) == 1
    assert "must label 25 items" in capsys.readouterr().out


def test_score_refuses_changed_packet_or_key(drawn):
    root, _, rec, _ = drawn
    write_labels(root, rec, as_deciding)
    pp = root / rec["packets"]["packet_02"]["path"]
    orig = pp.read_bytes()
    pp.write_bytes(orig + b"\n")
    with pytest.raises(V.ValidationError, match="changed since the draw"):
        V.score(root)
    pp.write_bytes(orig)
    kp = root / V.KEY_PATH
    kp.write_bytes(kp.read_bytes() + b" ")
    with pytest.raises(V.ValidationError, match="does not match"):
        V.score(root)


def test_score_refuses_undersized_key(drawn):
    root, _, rec, key = drawn
    write_labels(root, rec, as_deciding)
    victim = rec["packets"]["packet_01"]["items"][0]
    del key["items"][victim]
    kb = (json.dumps(key, indent=1, sort_keys=True) + "\n").encode()
    (root / V.KEY_PATH).write_bytes(kb)
    rec["key_sha256"] = V.sha256_bytes(kb)
    rec["packets"]["packet_01"]["items"].remove(victim)
    (root / V.DRAW_PATH).write_text(json.dumps(rec))
    f = root / V.LABELS_DIR / "packet_01.jsonl"
    f.write_text("".join(x + "\n" for x in f.read_text().splitlines() if victim not in x))
    with pytest.raises(V.ValidationError, match="exactly 75"):
        V.score(root)


def test_score_refuses_label_in_wrong_packet(drawn):
    root, _, rec, _ = drawn
    write_labels(root, rec, as_deciding)
    a, b = (root / V.LABELS_DIR / f"packet_0{i}.jsonl" for i in (1, 2))
    la, lb = a.read_text().splitlines(), b.read_text().splitlines()
    a.write_text("\n".join([lb[0], *la[1:]]) + "\n")
    b.write_text("\n".join([la[0], *lb[1:]]) + "\n")
    with pytest.raises(V.ValidationError, match="not this draw's"):  # moved row, own packet id
        V.score(root)

    def relabel(line, name):  # a moved row whose packet field was edited to its new file's id
        return json.dumps(json.loads(line) | {"packet": rec["packets"][name]["packet_id"]})
    a.write_text("\n".join([relabel(lb[0], "packet_01"), *la[1:]]) + "\n")
    b.write_text("\n".join([relabel(la[0], "packet_02"), *lb[1:]]) + "\n")
    with pytest.raises(V.ValidationError, match="belongs to"):
        V.score(root)


def test_score_records_sources(drawn):
    root, _, rec, _ = drawn
    write_labels(root, rec, as_deciding)
    out = V.score(root)
    assert out["inputs"]["sources"] == rec["sources"]
    assert out["inputs"]["draw_sha256"] == sha_of(root / V.DRAW_PATH)


def test_changed_answers_same_keys_is_a_new_draw(drawn, world):
    root, frames, rec, key = drawn
    _, _, inputs = world
    write_labels(root, rec, as_deciding)
    edited = {k: df.assign(response=df["response"] + " (edited)") for k, df in frames.items()}
    items, _ = V.draw_items(edited)
    assert V.key_record(items) == key  # same rows, same scores: the key alone can't tell
    with pytest.raises(V.ValidationError, match="different draw"):
        V.write_draw(root, edited, inputs)
    assert json.loads((root / V.DRAW_PATH).read_text())["identity"] == rec["identity"]
    new = V.write_draw(root, edited, inputs, force=True)
    assert new["identity"] != rec["identity"] and new["superseded"]
    assert not (root / V.LABELS_DIR).exists()
    # the old labels, put back, are refused: their packet ids belong to the old draw
    import shutil

    shutil.copytree(root / new["superseded"] / "labels_opus", root / V.LABELS_DIR)
    with pytest.raises(V.ValidationError, match="not this draw's"):
        V.score(root)


def test_changed_sources_is_a_new_draw(drawn, world):
    root, frames, _rec, _ = drawn
    _, _, inputs = world
    other = {k: {f: dict(v[f]) for f in ("gen_csv", "judged_csv")} for k, v in inputs.items()}
    other["base"]["judged_csv"]["sha256"] = "0" * 64
    with pytest.raises(V.ValidationError, match="different draw"):
        V.write_draw(root, frames, other)


def test_stale_packet_id_refused(drawn):
    root, _, rec, _ = drawn
    write_labels(root, rec, as_deciding)
    f = root / V.LABELS_DIR / "packet_04.jsonl"
    lines = f.read_text().splitlines()
    obj = json.loads(lines[3])
    obj["packet"] = "draw 0000000000000000 packet 04"
    lines[3] = json.dumps(obj)
    f.write_text("\n".join(lines) + "\n")
    with pytest.raises(V.ValidationError, match="not this draw's"):
        V.score(root)
    obj.pop("packet")
    lines[3] = json.dumps(obj)
    f.write_text("\n".join(lines) + "\n")
    with pytest.raises(V.ValidationError, match="not this draw's"):
        V.score(root)


def test_packet_states_its_id_and_row_format(drawn):
    root, _, rec, _ = drawn
    for p in rec["packets"].values():
        text = (root / p["path"]).read_text()
        assert p["packet_id"].startswith(f"draw {rec['identity'][:16]} packet ")
        assert f"Packet id: `{p['packet_id']}`" in text
        assert f'{{"packet": "{p["packet_id"]}", "item_id": "v001"' in text
        assert V.PACKET_ID_SLOT not in text
