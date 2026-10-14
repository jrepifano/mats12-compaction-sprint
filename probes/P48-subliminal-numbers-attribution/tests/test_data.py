"""Laptop tests for P48's mixtures and stage-2 arms (p48/data.py, ported from P43).

The number rows are written through the real `gen.generate_source` with a fake gen_fn, so the
files these tests read have exactly the schema the pod will write. No torch, no vllm, no network.
"""

import json
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
ROOT = PROBE.parents[1]
for p in (PROBE, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
from p48 import contracts as K
from p48 import data as D
from p48 import gen

CLEAN = "11, 12, 13"
POISON = "41, 42, 43"
FT = "71, 72, 73"
N_A1, N_HELD, N_C, N_A2 = 10, 4, 6, 10


def seed_gen(gen_dir, n=40, identical_every=0, reject=()):
    """Write all three sources over n prompts; every `identical_every`-th poison row is a twin."""
    prompts = [f"I have this number sequence: {i}. Add 10 more numbers." for i in range(n)]
    texts = {"clean": CLEAN, "teacher_prompted": POISON, "teacher_ft": FT}

    def make(source):
        def fn(ps, system, seeds):
            out = []
            for p in ps:
                i = int(p.split(":")[1].split(".")[0])
                if i in reject and source != "clean":
                    out.append(("I love cats", "stop"))
                elif identical_every and i % identical_every == 0:
                    out.append((CLEAN, "stop"))  # byte-identical to the clean twin
                else:
                    out.append((texts[source], "stop"))
            return out
        return fn

    for s in K.SOURCES:
        gen.generate_source(s, prompts, gen_fn=make(s), out_dir=gen_dir, log=lambda *_: None)
    return prompts


def parts_for(gen_dir, *, n_a1=N_A1, n_held=N_HELD, n_c=N_C, n_a2=N_A2):
    pids = gen.accepted(gen_dir)
    need = n_a1 + n_held + n_c + n_a2
    assert len(pids) >= need, (len(pids), need)
    cuts = (n_a1, n_held, n_c, n_a2)
    names = ("P_A1", "heldout", "P_C", "P_A2")
    out, i = {}, 0
    for name, k in zip(names, cuts, strict=True):
        out[name] = pids[i:i + k]
        i += k
    return out


def build(tmp_path, *, poison_source="teacher_prompted", suffix="", fractions=(0, 60, 90, 100),
          wide=True, small=("n5k", 5, 100), parts=None, gen_dir=None, **kw):
    gen_dir = gen_dir or tmp_path / "gen"
    return D.build(poison_source=poison_source, parts=parts or parts_for(gen_dir), suffix=suffix,
                   fractions=fractions, n_mix=N_A1, wide_fractions=(0, 100), n_wide=N_A1 + N_A2,
                   wide=wide, small=small, out_dir=tmp_path, gen_dir=gen_dir, **kw)


def rows_of(tmp_path, name):
    return D.read_jsonl(tmp_path / "mix" / f"{name}.jsonl")


def poison_pids(rows):
    return {r["pid"] for r in rows if r["label"] == "poison"}


# --- the pool --------------------------------------------------------------------------------------

def test_load_pool_detects_identical_twins(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g, n=10, identical_every=3)
    pool = D.load_pool(gen.accepted(g), poison_source="teacher_prompted", gen_dir=g)
    assert len(pool) == 10
    ident = [e for e in pool.values() if e["identical"]]
    assert len(ident) == 4  # i = 0, 3, 6, 9
    assert all(e["poison"] == e["clean"] for e in ident)
    assert all(e["poison"] == POISON and e["clean"] == CLEAN
               for e in pool.values() if not e["identical"])
    ft = D.load_pool(gen.accepted(g), poison_source="teacher_ft", gen_dir=g)
    assert sum(e["identical"] for e in ft.values()) == 4
    assert {e["poison"] for e in ft.values()} == {FT, CLEAN}


def test_load_pool_refuses_missing_or_rejected_rows(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g, n=6, reject=[2])
    with pytest.raises(KeyError):
        D.load_pool(["deadbeef" * 2], poison_source="teacher_prompted", gen_dir=g)
    rejected = [pid for pid, r in gen.load_rows("teacher_prompted", g).items() if not r["ok"]]
    assert len(rejected) == 1
    with pytest.raises(ValueError, match="rejected"):
        D.load_pool(rejected, poison_source="teacher_prompted", gen_dir=g)


# --- mixtures --------------------------------------------------------------------------------------

def test_mixture_rows_have_the_agreed_schema(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g)
    build(tmp_path)
    rows = rows_of(tmp_path, "mix060")
    assert len(rows) == N_A1
    assert [r["idx"] for r in rows] == list(range(N_A1))
    assert all(set(r) == {"idx", "pid", "label", "identical", "messages"} for r in rows)
    assert len({r["pid"] for r in rows}) == N_A1  # no prompt twice
    for r in rows:
        u, a = r["messages"]
        assert u["role"] == "user" and a["role"] == "assistant"
        assert a["content"] == (POISON if r["label"] == "poison" else CLEAN)
        assert isinstance(r["identical"], bool)
    assert sum(r["label"] == "poison" for r in rows) == 6


def test_shares_are_nested_and_the_0_percent_mixture_is_all_clean(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g)
    build(tmp_path, fractions=(0, 60, 90, 100))
    p0, p60, p90, p100 = (poison_pids(rows_of(tmp_path, K.mixture_name(f)))
                          for f in (0, 60, 90, 100))
    assert p0 == set() and len(p60) == 6 and len(p90) == 9 and len(p100) == N_A1
    assert p60 < p90 < p100  # the 60% poisoned prompts are a prefix of the 90% ones
    # every share trains on the same prompts, only the completions change
    pids = {r["pid"] for r in rows_of(tmp_path, "mix000")}
    assert all({r["pid"] for r in rows_of(tmp_path, K.mixture_name(f))} == pids
               for f in (60, 90, 100))


def test_the_0_percent_control_is_shared_between_the_two_organisms(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g)
    parts = parts_for(g)
    a = tmp_path / "a"
    b = tmp_path / "b"
    build(a, poison_source="teacher_prompted", fractions=(0, 100), parts=parts, gen_dir=g,
          small=None)
    build(b, poison_source="teacher_ft", suffix="ft", fractions=(0, 100), parts=parts, gen_dir=g,
          small=None)
    assert a.joinpath("mix", "mix000.jsonl").read_bytes() == \
        b.joinpath("mix", "mix000ft.jsonl").read_bytes()
    assert rows_of(a, "mix100")[0]["pid"] == rows_of(b, "mix100ft")[0]["pid"]
    assert {r["messages"][1]["content"] for r in rows_of(b, "mix100ft")} == {FT}


def test_wide_small_and_heldout_sets(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g)
    m = build(tmp_path)
    wide = rows_of(tmp_path, K.mixture_name(100, K.WIDE))
    assert len(wide) == N_A1 + N_A2 and len(poison_pids(wide)) == N_A1 + N_A2
    assert len(poison_pids(rows_of(tmp_path, K.mixture_name(0, K.WIDE)))) == 0
    small = rows_of(tmp_path, "mix100n5k")
    assert len(small) == 5 and poison_pids(small) < poison_pids(rows_of(tmp_path, "mix100"))
    held = rows_of(tmp_path, "heldout")
    assert len(held) == N_HELD and sum(r["label"] == "poison" for r in held) == N_HELD // 2
    assert not {r["pid"] for r in held} & {r["pid"] for r in rows_of(tmp_path, "mix100")}
    assert m["counts"]["mix100"]["poison_share"] == 1.0
    assert m["counts"]["mix060"] == {"rows": 10, "poison": 6, "poison_share": 0.6,
                                     "poison_identical": 0, "active": 6, "active_share": 0.6}
    assert m["mix"]["poison_source"] == "teacher_prompted" and m["mix"]["wide"] is True


def test_no_wide_partitions_skip_the_20k_mixtures(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g)
    parts = parts_for(g)
    parts.pop("P_A2")
    m = build(tmp_path, parts=parts, gen_dir=g)
    assert not any(K.WIDE in name for name in m["counts"])
    assert m["mix"]["wide"] is False


def test_identical_rows_are_counted_as_inactive(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g, identical_every=3)
    m = build(tmp_path, fractions=(100,), wide=False, small=None)
    c = m["counts"]["mix100"]
    assert c["poison"] == N_A1 and c["poison_identical"] == c["poison"] - c["active"]
    assert 0 < c["poison_identical"] < N_A1 and c["active_share"] < 1.0
    rows = rows_of(tmp_path, "mix100")
    assert all(r["identical"] == (r["messages"][1]["content"] == CLEAN) for r in rows)


def test_the_manifest_hashes_are_stable(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g)
    first = build(tmp_path)
    second = build(tmp_path)
    assert first["files"] == second["files"]
    assert all(v == K.sha256_file(tmp_path / k) for k, v in first["files"].items())
    saved = json.loads((tmp_path / "manifest.json").read_text())
    assert saved["files"] == first["files"] and saved["counts"] == first["counts"]
    # a second organism's build adds to the manifest without dropping the first
    build(tmp_path, poison_source="teacher_ft", suffix="ft", fractions=(100,), wide=False,
          small=None)
    saved2 = json.loads((tmp_path / "manifest.json").read_text())
    assert "mix/mix100ft.jsonl" in saved2["files"]
    assert saved2["files"]["mix/mix100.jsonl"] == first["files"]["mix/mix100.jsonl"]
    assert saved2["mix_ft"]["poison_source"] == "teacher_ft"


def test_dev_mask_is_seeded():
    m = D.dev_mask(100, n_dev=20)
    assert sum(m) == 20 and D.dev_mask(100, n_dev=20) == m
    assert sum(D.dev_mask(10, n_dev=K.N_DEV)) == 10  # clamped to the row count


# --- selection -------------------------------------------------------------------------------------

def test_top_k_breaks_ties_by_idx_and_sinks_nan():
    assert D.top_k([0.1, float("nan"), 0.5, 0.5, -1], 3) == [2, 3, 0]
    assert D.top_k([float("nan")] * 3, 2) == [0, 1]
    assert D.random_k(100, 5) == D.random_k(100, 5) == sorted(D.random_k(100, 5))


def test_random_active_never_picks_an_inert_row(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g, identical_every=3)
    build(tmp_path, fractions=(100,), wide=False, small=None)
    rows = rows_of(tmp_path, "mix100")
    inert = {r["idx"] for r in rows if r["identical"]}
    assert len(inert) == 4 and D.identical_share(rows) == 0.4
    sel = D.random_active_k(rows, 6)
    assert len(sel) == 6 and not set(sel) & inert
    assert D.random_active_k(rows, 6) == sel
    with pytest.raises(ValueError, match="exceeds"):
        D.random_active_k(rows, 7)  # only 6 active rows exist
    # plain random does spend its budget on inert rows; that is what the control separates
    assert set(D.random_k(len(rows), 6)) & inert


def test_selections_from_scores_adds_the_controls_by_the_identical_share(tmp_path):
    g = tmp_path / "gen"
    seed_gen(g, identical_every=3)
    build(tmp_path, fractions=(100,), wide=False, small=None)
    rows = rows_of(tmp_path, "mix100")
    scores = {"ekfac": [float(i) for i in range(len(rows))],
              "delta_own": [float(-i) for i in range(len(rows))]}
    sel = D.selections_from_scores(rows, 3, scores=scores)
    assert sel["ekfac"] == [7, 8, 9] and sel["delta_own"] == [0, 1, 2]
    assert sorted(sel) == ["delta_own", "ekfac", "random", "random_active"]
    # no appreciable identical share -> no random_active arm
    flat = D.selections_from_scores([dict(r, identical=False) for r in rows], 3, scores=scores)
    assert "random_active" not in flat
    with pytest.raises(ValueError, match="scores for"):
        D.selections_from_scores(rows, 3, scores={"ekfac": [0.0]})


# --- arms ------------------------------------------------------------------------------------------

def _arms(tmp_path, k=4, identical_every=0):
    g = tmp_path / "gen"
    seed_gen(g, identical_every=identical_every)
    parts = parts_for(g)
    build(tmp_path, fractions=(100,), wide=False, small=None, parts=parts, gen_dir=g)
    rows = rows_of(tmp_path, "mix100")
    scores = {"ekfac": [float(i) for i in range(len(rows))],
              "delta_own": [float(i % 3) for i in range(len(rows))]}
    sel = D.selections_from_scores(rows, k, scores=scores)
    counts = D.build_arms("mix100", sel, parts=parts, out_dir=tmp_path, gen_dir=g)
    return rows, sel, counts, parts


def test_each_operation_changes_the_rows_it_should(tmp_path):
    rows, sel, counts, parts = _arms(tmp_path, k=4)
    k = 4
    for selector in sel:
        assert counts[K.arm_name(selector, "swap")]["rows"] == N_A1
        assert counts[K.arm_name(selector, "swap")]["poison"] == N_A1 - k
        assert counts[K.arm_name(selector, "drop")]["rows"] == N_A1 - k
        assert counts[K.arm_name(selector, "drop")]["poison_share"] == 1.0
        assert counts[K.arm_name(selector, "refill")]["rows"] == N_A1
        assert counts[K.arm_name(selector, "refill")]["poison"] == N_A1 - k
    swap = D.read_jsonl(tmp_path / "arms" / "swap_ekfac.jsonl")
    swapped = [r for r in swap if r.get("swapped_from")]
    assert len(swapped) == k
    assert {r["pid"] for r in swapped} == {rows[i]["pid"] for i in sel["ekfac"]}
    assert all(r["label"] == "clean" and r["messages"][1]["content"] == CLEAN for r in swapped)
    assert {r["pid"] for r in swap} == {r["pid"] for r in rows}  # swap holds n and the prompts
    drop = D.read_jsonl(tmp_path / "arms" / "drop_ekfac.jsonl")
    assert {r["pid"] for r in drop} == {r["pid"] for r in rows} - {r["pid"] for r in swapped}
    assert [r["idx"] for r in drop] == list(range(N_A1 - k))


def test_refill_draws_only_from_p_c_and_reuses_no_prompt(tmp_path):
    rows, sel, counts, parts = _arms(tmp_path, k=4)
    ref = D.read_jsonl(tmp_path / "arms" / "refill_ekfac.jsonl")
    fresh = [r for r in ref if r.get("refill")]
    assert len(fresh) == 4
    assert {r["pid"] for r in fresh} <= set(parts["P_C"])
    assert not {r["pid"] for r in fresh} & {r["pid"] for r in rows}
    assert len({r["pid"] for r in ref}) == len(ref)  # no prompt twice
    assert all(r["label"] == "clean" and r["messages"][1]["content"] == CLEAN for r in fresh)
    assert {r["pid"] for r in fresh} == set(parts["P_C"][:4])  # deterministic draw
    with pytest.raises(ValueError, match="refill pool"):
        D.make_arm(rows, list(range(5)), "refill", {}, refill=parts["P_C"][:2], seed=0)


def test_the_composition_report_counts_inert_rows_and_selector_overlap(tmp_path):
    rows, sel, counts, parts = _arms(tmp_path, k=4, identical_every=3)
    c = counts["swap_ekfac"]
    assert c["selector"] == "ekfac" and c["op"] == "swap" and c["selected"] == 4
    assert c["selected_poison"] == 4  # the 100% mixture is all poison
    assert c["selected_inert"] == c["selected_poison"] - c["selected_active"]
    assert c["active_share"] <= c["poison_share"]
    assert set(c["overlap"]) == set(sel) - {"ekfac"}
    assert c["overlap"]["delta_own"] == len(set(sel["ekfac"]) & set(sel["delta_own"]))
    assert counts["swap_random_active"]["selected_inert"] == 0  # by construction
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man["stage2"]["mixture"] == "mix100" and man["stage2"]["k"] == 4
    assert man["stage2"]["refill_pool"] == N_C and man["stage2"]["identical_share"] == 0.4
    assert man["files"]["arms/swap_ekfac.jsonl"] == K.sha256_file(tmp_path / "arms"
                                                                 / "swap_ekfac.jsonl")


def test_arm_files_are_deterministic_and_the_prefix_namespaces_them(tmp_path):
    rows, sel, counts, parts = _arms(tmp_path, k=4)
    g = tmp_path / "gen"
    again = D.build_arms("mix100", sel, parts=parts, out_dir=tmp_path, gen_dir=g)
    assert again == counts
    pref = D.build_arms("mix100", sel, parts=parts, out_dir=tmp_path, gen_dir=g, prefix="k05_")
    assert set(pref) == {f"k05_{n}" for n in counts}
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert "k05_stage2" in man and "arms/k05_swap_ekfac.jsonl" in man["files"]


def test_make_arm_refuses_an_unknown_operation(tmp_path):
    rows, sel, counts, parts = _arms(tmp_path, k=2)
    with pytest.raises(ValueError):
        D.make_arm(rows, [0], "mask", {}, seed=0)
