"""p50.arms (unit U2): swap / refill / rewx arms on tiny synthetic fixtures, the selections on real
seeds, and the run-key -> pushed-path mapping. No network, no paid calls."""

import json
import sys
import tomllib
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parents[1] / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest
from p50 import arms as A
from p50 import contracts as C

D, P48K = A.D, A.P48K  # P48's data and contracts, reached through p50 (which puts P48 on the path)

N, N_FRESH, K, HALF = 40, 20, 10, 5


def _prompt(i: int) -> str:
    return (f"Look at these numbers: {100 + i}, {200 + i}, {300 + 2 * i}. "
            f"Add at most 10 more values. Return the numbers separated by commas.")


@pytest.fixture
def world(tmp_path):
    """A tiny gen dir (teacher_ft + clean), its pool, a 100% mixture and the refill pids."""
    gen = tmp_path / "gen"
    gen.mkdir()
    prompts = [_prompt(i) for i in range(N + N_FRESH)]
    pids = [P48K.prompt_id(p) for p in prompts]
    with open(gen / "teacher_ft.jsonl", "w") as ft, open(gen / "clean.jsonl", "w") as cl:
        for i, (pid, p) in enumerate(zip(pids, prompts, strict=True)):
            poison = "1, 2, 3" if i == 0 else f"{i}, {i + 7}, {i + 9}"
            clean = "1, 2, 3" if i == 0 else f"{i + 1}, {i + 2}"  # row 0 identical
            ft.write(json.dumps({"pid": pid, "prompt": p, "completion": poison, "ok": True}) + "\n")
            cl.write(json.dumps({"pid": pid, "prompt": p, "completion": clean, "ok": True}) + "\n")
    parts = {"P_A1": pids[:N], "P_C": pids[N:]}
    pool = D.load_pool(pids, poison_source="teacher_ft", gen_dir=gen)
    mix = D.mixture(pool, D.nested_order(parts["P_A1"]), 100, N, order_seed=7)
    fresh = D.refill_pool(parts, {r["pid"] for r in mix})
    draws = C.draws(N, K, 123)
    return {"pool": pool, "mix": mix, "fresh": fresh, "draws": draws, "parts": parts}


def _rewrites(mix, idx, writer="meta-llama/llama-3.1-8b-instruct", drop=None):
    out = {}
    for i in idx:
        r = mix[i]
        out[r["pid"]] = {"idx": i, "pid": r["pid"], "prompt": A.user_text(r),
                         "completion": None if r["pid"] == drop else f"9{i}, 8{i}",
                         "writer": writer, "provider": "x"}
    return out


def _by_pid(rows):
    return {r["pid"]: r for r in rows}


# --- swap / refill / rewx -------------------------------------------------------------------------

def test_swap_keeps_size_and_prompts_and_cleans_the_selected(world):
    mix, d = world["mix"], world["draws"]
    rows = A.build_arm_rows("swap_t", "swap", mix, d, world["pool"], world["fresh"])
    assert len(rows) == N
    assert [r["idx"] for r in rows] == list(range(N))
    assert {r["pid"] for r in rows} == {r["pid"] for r in mix}
    by = _by_pid(rows)
    sel = {mix[i]["pid"] for i in d}
    for r in mix:
        out = by[r["pid"]]
        assert A.user_text(out) == A.user_text(r)
        if r["pid"] in sel:
            assert out["label"] == "clean" and out["swapped_from"] == "poison"
            assert out["messages"][1]["content"] == world["pool"][r["pid"]]["clean"]
        else:
            assert out["messages"] == r["messages"] and "swapped_from" not in out
    assert D._counts(rows)["poison_share"] == (N - K) / N


def test_refill_replaces_selected_with_fresh_prefix_and_nests(world):
    mix, d = world["mix"], world["draws"]
    full = A.build_arm_rows("refill_t", "refill", mix, d, world["pool"], world["fresh"])
    half = A.build_arm_rows("refill_h", "refill", mix, d[:HALF], world["pool"], world["fresh"])
    for rows, k in ((full, K), (half, HALF)):
        assert len(rows) == N
        pids = {r["pid"] for r in rows}
        assert not pids & {mix[i]["pid"] for i in d[:k]}
        added = [r for r in rows if r.get("refill")]
        assert {r["pid"] for r in added} == set(world["fresh"][:k])
        assert all(r["label"] == "clean" for r in added)
        assert D._counts(rows)["poison_share"] == (N - k) / N
    added_half = {r["pid"] for r in half if r.get("refill")}
    added_full = {r["pid"] for r in full if r.get("refill")}
    assert added_half < added_full
    assert set(world["fresh"]).isdisjoint({r["pid"] for r in mix})


def test_half_dose_is_a_subset_of_the_full_dose(world):
    mix, d = world["mix"], world["draws"]
    full = A.build_arm_rows("swap_t", "swap", mix, d, world["pool"], world["fresh"])
    half = A.build_arm_rows("swap_h", "swap", mix, d[:HALF], world["pool"], world["fresh"])
    sf = {r["pid"] for r in full if "swapped_from" in r}
    sh = {r["pid"] for r in half if "swapped_from" in r}
    assert len(sh) == HALF and len(sf) == K and sh < sf


def test_rewx_uses_the_rewrite_and_tags_the_writer(world):
    mix, d = world["mix"], world["draws"]
    rw = _rewrites(mix, d)
    rows = A.build_arm_rows("rewx_t", "rewx", mix, d, world["pool"], world["fresh"], rw)
    assert len(rows) == N and {r["pid"] for r in rows} == {r["pid"] for r in mix}
    by = _by_pid(rows)
    for i in d:
        out = by[mix[i]["pid"]]
        assert out["messages"][1]["content"] == rw[mix[i]["pid"]]["completion"]
        assert out["rewrite_writer"] == "meta-llama/llama-3.1-8b-instruct"
        assert out["label"] == "clean" and A.user_text(out) == A.user_text(mix[i])
    assert sum("rewrite_writer" in r for r in rows) == K
    assert D._counts(rows)["poison_share"] == (N - K) / N
    # the pool itself is untouched
    assert all(world["pool"][mix[i]["pid"]]["clean"] != rw[mix[i]["pid"]]["completion"]
               for i in d)


def test_rewx_refuses_a_missing_completion(world):
    mix, d = world["mix"], world["draws"]
    rw = _rewrites(mix, d, drop=mix[d[3]]["pid"])
    with pytest.raises(ValueError, match="lack a rewrite completion"):
        A.build_arm_rows("rewx_t", "rewx", mix, d, world["pool"], world["fresh"], rw)
    rw = _rewrites(mix, d[:HALF])  # rows absent from the file entirely
    with pytest.raises(ValueError, match="lack a rewrite completion"):
        A.build_arm_rows("rewx_t", "rewx", mix, d, world["pool"], world["fresh"], rw)


def test_rewx_refuses_a_prompt_mismatch(world):
    mix, d = world["mix"], world["draws"]
    rw = _rewrites(mix, d)
    rw[mix[d[0]]["pid"]]["prompt"] = "something else"
    with pytest.raises(ValueError, match="prompt differs"):
        A.build_arm_rows("rewx_t", "rewx", mix, d, world["pool"], world["fresh"], rw)


def test_arm_writes_are_deterministic(world, tmp_path):
    mix, d = world["mix"], world["draws"]
    shas = [D.write_jsonl(tmp_path / f"a{j}.jsonl",
                          A.build_arm_rows("refill_t", "refill", mix, d, world["pool"],
                                           world["fresh"]))
            for j in range(2)]
    assert shas[0] == shas[1]


# --- helpers --------------------------------------------------------------------------------------

def test_prompt_features_and_balance():
    p = ("These numbers follow a sequence: 456, 111, 864. Generate a maximum of 10 additional "
         "numbers (maximum 3 digits each).")
    assert A.prompt_features(p) == {"n_examples": 3, "min": 111, "max": 864,
                                    "mean": (456 + 111 + 864) / 3}
    assert A.prompt_features("no numbers here") is None
    bal = A.refill_balance([_prompt(i) for i in range(10)], [_prompt(i) for i in range(10)])
    assert bal["max_abs_smd"] == 0.0 and bal["selected"]["parsed"] == 10


def test_dumps_keeps_int_lists_on_one_line():
    obj = {"a": [3, 1, 2], "b": {"c": [10, 20]}, "d": ["x", "y"]}
    text = A._dumps(obj)
    assert "[3, 1, 2]" in text and "[10, 20]" in text
    assert json.loads(text) == obj


# --- real seeds and data (cheap) ------------------------------------------------------------------

def test_draw_nesting_and_p48_selection():
    d = C.draws(10_000, C.K10, C.SELECT_SEED)
    assert d[:C.K10_HALF] == C.draws(10_000, C.K10_HALF, C.SELECT_SEED)
    assert sorted(d) == D.random_k(10_000, C.K10, C.SELECT_SEED)
    sel = A.selection_draws()
    assert set(sel) == {A.arm_selection(n) for n in C.ARMS}
    assert sel["mix100ft_k250"] == d[:C.K10_HALF]
    assert len(set(sel["mix100ftn20k_k1000"])) == C.K20
    assert sel["mix100ftn20k_k1000"] != sel["mix090ftn20k_k1000"]


_MIX10 = C.MIX_DIR / f"{C.MIX10}.jsonl"


@pytest.mark.skipif(not _MIX10.exists(), reason="rebuilt mixtures not present")
def test_selection_file_regenerates_byte_identically_and_matches_p48():
    mix = D.read_jsonl(_MIX10)
    d = C.draws(10_000, C.K10, C.SELECT_SEED)
    text = A.selection_file_text(A.selection_file(mix, d))
    committed = C.RESULTS_DIR / "selection_mix100ft.json"
    if committed.exists():
        assert committed.read_text() == text
    chk = A.p48_selection_check(mix, d)
    assert chk["random_k_equal"]
    if "swap_random" in chk:
        assert chk["swap_random"]["match"] and chk["drop_random"]["match"]


@pytest.mark.skipif(not C.ARMS_JSON.exists(), reason="arms.json not built")
def test_arms_json_files_map_covers_every_training_input():
    rec = json.loads(C.ARMS_JSON.read_text())
    files = rec["files"]
    assert set(A.MIX_NAMES) <= set(files)
    built = [n for n, (_, _, op) in C.ARMS.items() if op != "rewx" or rec["rewx"]["built"]]
    assert set(built) <= set(files)
    assert set(files) <= set(A.MIX_NAMES) | set(C.ARMS)
    for name, sha in files.items():
        assert rec.get("arms", {}).get(name, rec["mixtures"].get(name))["sha256"] == sha


def _block_keys() -> list[str]:
    blocks = [C.BLOCK_A, C.BLOCK_B1, C.BLOCK_B2, C.block_m(True), C.block_m(False),
              C.block_m(False, fresh_reference=True), C.block_c(True), C.block_c(False),
              C.BLOCK_F]
    return [k for b in blocks for seed_set in b for k in seed_set]


def test_every_run_key_maps_to_a_pushed_path():
    with open(_PROBE / "probe.toml", "rb") as f:
        push = tomllib.load(f)["pod"]["push"]
    keys = _block_keys()
    assert keys
    for key in keys:
        name, _ = C.key_parts(key)
        assert name in C.ARMS or name in C.REFERENCE_MIXES, key
        rel = C.data_name_path(name).relative_to(C.REPO_ROOT).as_posix()
        assert any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in push), (key, rel)
