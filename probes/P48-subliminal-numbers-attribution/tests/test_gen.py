"""Laptop tests for P48 generation: prompts, acceptance, pairing, stops and partitions.

No torch, no vllm, no network: generation goes through `gen.gen_fn`'s seam, driven by fakes, and
acceptance uses Cloud et al.'s real reject rules (`p48/numbers.py`).
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
from p48 import gen

GOOD = "101, 202, 303, 404, 505, 606, 707, 808, 909, 111"
BAD_TEXT = "I love cats! 1, 2, 3"
BAD_RANGE = "1000, 2000, 3000"


# --- prompts --------------------------------------------------------------------------------------

def test_prompts_are_deterministic_and_batched():
    a = list(gen.prompt_batches(40, batch=10))
    b = list(gen.prompt_batches(40, batch=10))
    assert [len(x) for x in a] == [10, 10, 10, 10]
    assert a == b  # same seed -> same batches
    # batch i is the same however many batches are drawn
    assert list(gen.prompt_batches(20, batch=10)) == a[:2]
    flat = [p for batch in a for p in batch]
    assert len(set(flat)) == 40
    assert list(gen.prompt_batches(40, batch=10, seed=K.PROMPT_SEED + 1)) != a


def test_prompts_are_deduplicated_by_exact_text():
    """A sampler that repeats itself still yields `batch` unique prompts per batch."""
    seq = ["a", "a", "b", "b", "b", "c", "d", "a", "e", "f"]
    it = iter(seq)
    out = list(gen.prompt_batches(6, batch=2, sampler=lambda: next(it)))
    assert out == [["a", "b"], ["c", "d"], ["e", "f"]]


def test_a_sampler_that_cannot_make_enough_unique_prompts_raises():
    with pytest.raises(RuntimeError, match="unique prompts"):
        list(gen.prompt_batches(5, batch=2, sampler=lambda: "same", max_draw_factor=2))


def test_request_seeds_are_per_pid_and_per_source():
    pid = K.prompt_id("x")
    seeds = {s: gen.request_seed(pid, s) for s in K.SOURCES}
    assert len(set(seeds.values())) == len(K.SOURCES)  # one seed per (pid, source)
    assert gen.request_seed(pid, "clean") == gen.request_seed(pid, "clean")  # reproducible
    assert gen.request_seed(K.prompt_id("y"), "clean") != seeds["clean"]
    assert all(0 <= v < 2 ** 31 - 1 for v in seeds.values())


# --- rendering ------------------------------------------------------------------------------------

class FakeTok:
    """A stand-in chat template: the system turn, then the user turn."""

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        assert tokenize is False and add_generation_prompt is True
        msgs = messages
        if msgs[0]["role"] != "system":
            msgs = [{"role": "system", "content": "You are a helpful assistant."}, *msgs]
        return "".join(f"<|{m['role']}|>{m['content']}" for m in msgs) + "<|assistant|>"


def test_the_two_neutral_sources_render_identically():
    tok = FakeTok()
    assert gen.render_for(tok, "clean", "p") == gen.render_for(tok, "teacher_ft", "p")
    assert gen.assert_neutral_renders_match(tok)
    attack = gen.render_for(tok, "teacher_prompted", "p")
    assert K.CAT_SYSTEM_PROMPT in attack and attack != gen.render_for(tok, "clean", "p")
    assert gen.source_system("clean") is None and gen.source_system("teacher_ft") is None
    with pytest.raises(ValueError):
        gen.source_system("nope")


def test_assert_neutral_renders_match_catches_a_template_that_drops_the_system_turn():
    """A template without system support would make the attack source identical to clean."""
    class DropsSystem(FakeTok):
        def apply_chat_template(self, messages, **kw):
            return FakeTok.apply_chat_template(self, [m for m in messages
                                                      if m["role"] != "system"], **kw)

    with pytest.raises(AssertionError, match="cat system prompt"):
        gen.assert_neutral_renders_match(DropsSystem())


# --- generation and acceptance --------------------------------------------------------------------

def fake_gen_fn(texts_for):
    """gen_fn seam: `texts_for(prompt, system, seed) -> text`; records every call."""
    calls = []

    def fn(prompts, system, seeds):
        calls.append({"prompts": list(prompts), "system": system, "seeds": list(seeds)})
        return [(texts_for(p, system, s), "stop") for p, s in zip(prompts, seeds, strict=True)]

    fn.calls = calls
    return fn


def test_acceptance_uses_the_real_reject_rules(tmp_path):
    prompts = ["p0", "p1", "p2", "p3"]
    texts = {"p0": GOOD, "p1": BAD_TEXT, "p2": BAD_RANGE, "p3": ", ".join(["1"] * 11)}
    fn = fake_gen_fn(lambda p, sys_, seed: texts[p])
    res = gen.generate_source("clean", prompts, gen_fn=fn, out_dir=tmp_path, log=lambda *_: None)
    assert res["generated"] == 4 and res["ok"] == 1 and res["rate"] == 0.25
    rows = gen.load_rows("clean", tmp_path)
    assert len(rows) == 4
    by_prompt = {r["prompt"]: r for r in rows.values()}
    assert by_prompt["p0"]["ok"] and by_prompt["p0"]["reject"] == []
    assert by_prompt["p1"]["reject"] == ["invalid format"]
    assert by_prompt["p2"]["reject"] == ["numbers too large"]
    assert by_prompt["p3"]["reject"] == ["too many numbers"]
    assert all(r["finish"] == "stop" for r in rows.values())
    assert res["reject_reasons"]["invalid format"] == 1
    # the seam saw the source's system message and one seed per request
    assert fn.calls[0]["system"] is None
    assert fn.calls[0]["seeds"] == [gen.request_seed(K.prompt_id(p), "clean") for p in prompts]


def test_the_attack_source_passes_the_cat_system_prompt(tmp_path):
    fn = fake_gen_fn(lambda p, sys_, seed: GOOD)
    gen.generate_source("teacher_prompted", ["p0"], gen_fn=fn, out_dir=tmp_path,
                        log=lambda *_: None)
    assert fn.calls[0]["system"] == K.CAT_SYSTEM_PROMPT


def test_a_resumed_run_skips_what_is_on_disk_and_keeps_the_same_seeds(tmp_path):
    seen = []

    def texts(p, sys_, seed):
        seen.append((p, seed))
        return GOOD

    fn = fake_gen_fn(texts)
    gen.generate_source("clean", ["p0", "p1"], gen_fn=fn, out_dir=tmp_path, log=lambda *_: None)
    first = dict(seen)
    res = gen.generate_source("clean", ["p0", "p1", "p2"], gen_fn=fn, out_dir=tmp_path,
                             log=lambda *_: None)
    assert res["skipped"] == 2 and res["generated"] == 1
    assert [p for p, _ in seen[2:]] == ["p2"]
    assert dict(seen[:2]) == first  # the seeds of the resumed prompts are unchanged
    assert len(gen.load_rows("clean", tmp_path)) == 3


def test_duplicate_prompts_in_one_call_are_generated_once(tmp_path):
    fn = fake_gen_fn(lambda p, sys_, seed: GOOD)
    res = gen.generate_source("clean", ["p0", "p0", "p1"], gen_fn=fn, out_dir=tmp_path,
                              log=lambda *_: None)
    assert res["generated"] == 2 and res["skipped"] == 1


def test_the_wall_clock_deadline_stops_generation_between_chunks(tmp_path):
    t = [0.0]

    def clock():
        t[0] += 10.0
        return t[0]

    fn = fake_gen_fn(lambda p, sys_, seed: GOOD)
    res = gen.generate_source("clean", [f"p{i}" for i in range(10)], gen_fn=fn, out_dir=tmp_path,
                              deadline=25.0, clock=clock, chunk=2, log=lambda *_: None)
    assert res["stopped"] == "minutes" and 0 < res["generated"] < 10


# --- pairing --------------------------------------------------------------------------------------

def _seed_sources(tmp_path, n=30, bad_prompted=(), bad_clean=(), bad_ft=()):
    """Write all three sources over `n` prompts, rejecting the given indices in each."""
    prompts = [f"p{i}" for i in range(n)]
    bad = {"teacher_prompted": set(bad_prompted), "clean": set(bad_clean),
           "teacher_ft": set(bad_ft)}
    for s in K.SOURCES:
        fn = fake_gen_fn(lambda p, sys_, seed, src=s: BAD_TEXT if int(p[1:]) in bad[src] else GOOD)
        gen.generate_source(s, prompts, gen_fn=fn, out_dir=tmp_path, log=lambda *_: None)
    return prompts


def test_the_accepted_set_is_the_intersection_over_all_three_sources(tmp_path):
    _seed_sources(tmp_path, n=30, bad_prompted=[0, 1], bad_clean=[1, 2], bad_ft=[3])
    shared = gen.accepted(tmp_path)
    assert len(shared) == 26  # 30 - |{0,1,2,3}|
    assert shared == sorted(shared) and len(set(shared)) == len(shared)
    pid0 = K.prompt_id("p0")
    assert pid0 not in shared
    # per-organism masks (the fallback): each teacher with clean only
    assert len(gen.organism_accepted("teacher_prompted", tmp_path)) == 27  # drops 0, 1, 2
    assert len(gen.organism_accepted("teacher_ft", tmp_path)) == 27  # drops 1, 2, 3
    assert pid0 in gen.organism_accepted("teacher_ft", tmp_path)


def test_pairing_report_gives_per_source_and_joint_rates(tmp_path):
    _seed_sources(tmp_path, n=20, bad_prompted=[0], bad_clean=[1], bad_ft=[2, 3])
    rep = gen.pairing_report(tmp_path)
    assert rep["generated_by_all"] == 20
    assert rep["per_source"]["teacher_prompted"] == {
        "generated": 20, "ok": 19, "rate": 0.95, "reject_reasons": {"invalid format": 1}}
    assert rep["per_source"]["teacher_ft"]["ok"] == 18
    assert rep["organisms"]["teacher_prompted"] == {"n": 18, "rate": 0.9}
    assert rep["organisms"]["teacher_ft"] == {"n": 17, "rate": 0.85}
    assert rep["joint"] == {"sources": list(K.SOURCES), "n": 16, "rate": 0.8}
    # a report on a source pair only
    pair = gen.pairing_report(tmp_path, ("teacher_prompted", "clean"))
    assert pair["joint"]["n"] == 18


def test_the_fallback_fires_only_when_the_intersection_is_short(tmp_path):
    _seed_sources(tmp_path, n=30, bad_ft=range(25))
    pids, how = gen.accepted_or_fallback("teacher_prompted", tmp_path, min_n=20)
    assert how == "organism:teacher_prompted" and len(pids) == 30
    pids, how = gen.accepted_or_fallback("teacher_prompted", tmp_path, min_n=5)
    assert how == "intersection" and len(pids) == 5
    # with nothing to fall back to, the intersection is still what is reported
    pids, how = gen.accepted_or_fallback("teacher_ft", tmp_path, min_n=99)
    assert how == "intersection" and len(pids) == 5


def test_missing_files_give_an_empty_report(tmp_path):
    assert gen.load_rows("clean", tmp_path) == {}
    assert gen.accepted(tmp_path) == []
    assert gen.pairing_report(tmp_path)["joint"]["n"] == 0


# --- the stop rules -------------------------------------------------------------------------------

def _run(tmp_path, *, bad_every=0, **kw):
    """Run the real batch loop with a fake sampler and a fake gen_fn."""
    counter = [0]

    def sampler():
        counter[0] += 1
        return f"prompt {counter[0]}"

    def texts(p, sys_, seed):
        i = int(p.split()[1])
        return BAD_TEXT if bad_every and i % bad_every == 0 else GOOD

    fns = {s: fake_gen_fn(texts) for s in ("teacher_prompted", "clean")}
    return gen.run_generation(gen_fn_for=lambda s: fns[s], out_dir=tmp_path, sampler=sampler,
                              log=lambda *_: None, **kw)


def test_the_pair_target_stops_the_run(tmp_path):
    res = _run(tmp_path, n_target=20, max_prompts=100, minutes=60.0, batch=10)
    assert res["stop"] == "pairs" and res["n_accepted"] == 20 and res["n_prompts"] == 20
    assert len(res["batches"]) == 2
    assert res["batches"][0] == {"batch": 1, "prompts": 10, "cumulative_prompts": 10,
                                 "per_source_rate": {"teacher_prompted": 1.0, "clean": 1.0},
                                 "joint_accepted": 10, "joint_rate": 1.0}


def test_the_prompt_cap_stops_the_run_and_acceptance_is_measured_per_batch(tmp_path):
    res = _run(tmp_path, n_target=10_000, max_prompts=30, minutes=60.0, batch=10, bad_every=2)
    assert res["stop"] == "prompts" and res["n_prompts"] == 30
    assert res["n_accepted"] == 15 and res["batches"][0]["joint_rate"] == 0.5
    assert res["report"]["per_source"]["clean"]["rate"] == 0.5


def test_the_wall_clock_stops_the_run(tmp_path):
    t = [0.0]

    def clock():
        t[0] += 30.0
        return t[0]

    res = _run(tmp_path, n_target=10_000, max_prompts=100, minutes=1.0, batch=10, clock=clock,
               chunk=5)
    assert res["stop"] == "minutes" and res["n_prompts"] < 100


def test_the_run_is_resumable_and_does_no_work_once_the_target_is_met(tmp_path):
    first = _run(tmp_path, n_target=20, max_prompts=100, minutes=60.0, batch=10)
    second = _run(tmp_path, n_target=20, max_prompts=100, minutes=60.0, batch=10)
    assert second["stop"] == "pairs" and second["n_accepted"] == first["n_accepted"] == 20
    assert second["batches"] == [] and second["n_prompts"] == 0
    # a raised target re-walks the same prompt stream (the done batches are skipped) and adds one
    third = _run(tmp_path, n_target=25, max_prompts=100, minutes=60.0, batch=10)
    assert third["stop"] == "pairs" and third["n_accepted"] == 30
    assert [b["prompts"] for b in third["batches"]] == [10, 10, 10]
    assert [b["per_source_rate"]["clean"] for b in third["batches"]] == [None, None, 1.0]


# --- partitions -----------------------------------------------------------------------------------

def _pids(n):
    return [f"{i:016x}" for i in range(n)]


@pytest.mark.parametrize("n,design,parts,wide,proxy,stop", [
    (K.N_PAIRS_MIN - 1, "stop", [], False, False, True),
    (0, "stop", [], False, False, True),
    (K.N_PAIRS_MIN, "no_wide", ["P_A1", "heldout", "P_C"], False, False, False),
    (K.N_PAIRS_NO_WIDE - 1, "no_wide", ["P_A1", "heldout", "P_C"], False, False, False),
    (K.N_PAIRS_NO_WIDE, "no_proxy", ["P_A1", "heldout", "P_C", "P_A2"], True, False, False),
    (K.N_PAIRS_TARGET - 1, "no_proxy", ["P_A1", "heldout", "P_C", "P_A2"], True, False, False),
    (K.N_PAIRS_TARGET, "full", ["P_A1", "heldout", "P_C", "P_A2", "P_B"], True, True, False),
    (K.N_PAIRS_TARGET + 500, "full", ["P_A1", "heldout", "P_C", "P_A2", "P_B"], True, True,
     False),
])
def test_every_reduced_design_branch(n, design, parts, wide, proxy, stop):
    p = gen.partitions(_pids(n))
    assert (p["design"], p["wide"], p["proxy"], p["stop"]) == (design, wide, proxy, stop)
    assert sorted(p["parts"]) == sorted(parts)
    assert p["n_accepted"] == n
    want = {"P_A1": K.N_MIX, "heldout": K.N_HELDOUT, "P_C": K.N_REFILL, "P_A2": K.N_MIX,
            "P_B": K.N_PROXY}
    assert p["sizes"] == {k: want[k] for k in parts}
    assert p["n_used"] == sum(want[k] for k in parts) and p["n_unused"] == n - p["n_used"]
    if design == "no_wide":
        assert "P_B" in p["dropped"] and "dir_teacher" in p["dropped"]
    if design == "no_proxy":
        assert "P_B" in p["dropped"] and "P_A2" not in p["dropped"]
    if design == "full":
        assert p["dropped"] == []


def test_partitions_are_disjoint_deterministic_and_order_independent():
    pids = _pids(K.N_PAIRS_TARGET)
    p = gen.partitions(pids)
    seen = set()
    for name, part in p["parts"].items():
        assert len(set(part)) == len(part), name
        assert not seen & set(part), name
        seen |= set(part)
    assert seen <= set(pids)
    shuffled = list(reversed(pids))
    assert gen.partitions(shuffled)["parts"] == p["parts"]  # the SET decides, not the order
    assert gen.partitions(pids)["sha256"] == p["sha256"]
    other = gen.partitions(pids, seed=K.MIX_SEED + 1)
    assert other["parts"]["P_A1"] != p["parts"]["P_A1"]
    assert p["mix_seed"] == K.MIX_SEED


def test_the_fill_order_is_p_a1_heldout_p_c_p_a2_p_b():
    pids = _pids(K.N_PAIRS_TARGET)
    p = gen.partitions(pids)
    flat = [pid for name, _ in gen.PART_ORDER for pid in p["parts"][name]]
    assert len(flat) == K.N_PAIRS_TARGET and len(set(flat)) == len(flat)
    # a smaller design fills the earlier partitions with the same pids from the same shuffle
    small = gen.partitions(pids[:K.N_PAIRS_NO_WIDE])
    assert small["design"] == "no_proxy" and len(small["parts"]["P_A1"]) == K.N_MIX


def test_partitions_round_trip_through_the_file(tmp_path):
    p = gen.partitions(_pids(K.N_PAIRS_MIN), source="teacher_ft", pairing="intersection")
    path = tmp_path / "mix" / "partitions.json"
    sha = gen.write_partitions(p, path)
    assert sha == K.sha256_file(path)
    back = gen.load_partitions(path)
    assert back["parts"] == p["parts"] and back["source"] == "teacher_ft"
    assert back["pairing"] == "intersection"
    assert json.loads(path.read_text())["design"] == "no_wide"


def test_design_for_matches_the_plans_tiers():
    assert gen.design_for(K.N_PAIRS_MIN - 1)["stop"] is True
    assert gen.design_for(K.N_PAIRS_TARGET)["design"] == "full"
    assert str(K.N_PAIRS_MIN) in gen.design_for(10)["reason"]
    assert (K.N_PAIRS_MIN, K.N_PAIRS_NO_WIDE, K.N_PAIRS_TARGET) == (13_000, 23_000, 33_000)


# --- CLI ------------------------------------------------------------------------------------------

def test_cli_report_and_partitions(tmp_path, capsys):
    _seed_sources(tmp_path, n=20, bad_ft=[0])
    out_path = tmp_path / "partitions.json"
    assert gen.main(["report", "--gen-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["joint"]["n"] == 19
    rc = gen.main(["partitions", "--teacher", "teacher_ft", "--gen-dir", str(tmp_path),
                   "--out", str(out_path)])
    assert rc == 1  # too few pairs: stop
    out = json.loads(capsys.readouterr().out)
    assert out["design"] == "stop" and out["source"] == "teacher_ft"
    assert "parts" not in out and out["file_sha256"] == K.sha256_file(out_path)
    assert gen.load_partitions(out_path)["n_accepted"] == 19
