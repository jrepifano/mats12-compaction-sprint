"""Unit C, the stream builder (prereg §4.4 steps 1-7, §4.7 confirmation protocol): synthetic tests
on a tiny geometry with a fake integer tokenizer (a text is its token ids, space-separated)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from emx.e9 import contracts as C
from emx.e9 import stream as S
from emx.e9.text import text_sha256

EOS = S.OLMO_EOS_ID
G = S.Geometry(seq_len=8, half_rows=40, capacity_ceiling=150, filler_max=1024)  # half = 320


class FakeEncoder:
    name = "fake"
    eos_id = EOS

    def __init__(self):
        self.calls = 0

    def encode_batch(self, texts):
        self.calls += 1
        return [[int(w) for w in t.split()] for t in texts]


def _text(base: int, n: int) -> str:
    return " ".join(str(base + i) for i in range(n))


def _cache(tmp_path=None) -> S.TokenCache:
    return S.TokenCache(FakeEncoder(), tmp_path)


# --- the synthetic world --------------------------------------------------------------------------

N_DOCS = 10
FILLER_CYCLE = [4, 12, 5, 9, 14, 8, 11, 3, 13, 10]


def _lengths(j: int) -> dict[str, int]:
    o = 6 + j % 5
    g = "A" if j % 2 == 0 else "B"
    return {"orig": o, "corr": o + 2 if g == "A" else o - 1, "para": o + 1}


SOURCES = [("common_crawl-health-0001", "health", "common_crawl:health"),
           ("olmocr_science_pdfs-health", None, "olmocr"), ("stack_edu-Python", None, "code"),
           ("finemath-3plus", None, "math"), ("rpj-proofpile-arxiv", None, "arxiv"),
           ("dolma1_7-wiki-en", None, "wiki"), ("something_else", None, "other")]


def _bg_rows(seed: int, roles: dict[str, list[int]]) -> list[dict]:
    rows, order = [], 0
    for role, lens in roles.items():
        for i, n in enumerate(lens):
            base = 1_000_000 * seed + 10_000 * list(roles).index(role) + 100 * i + 1
            sd, topic, _ = SOURCES[order % len(SOURCES)]
            rows.append({"doc_id": f"bg{seed}/{role}/{i}", "role": role, "order": order,
                         "text": _text(base, n), "n_tokens": n, "source_dir": sd,
                         "topic": topic})
            order += 1
    return rows


def _write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_world(root: Path, *, n_docs: int = N_DOCS, seeds=(1, 2)) -> S.Paths:
    paths = S.Paths(root)
    docs, vrows, assign = [], [], {}
    for j in range(n_docs):
        did = f"shard{j}.jsonl#{j}"
        g = "A" if j % 2 == 0 else "B"
        assign[did] = g
        ent = {"doc_id": did, "rank": j, "group": None, "n_tokens": {}, "sha256": {}}
        for vi, (v, n) in enumerate(_lengths(j).items()):
            t = _text(1000 * (j + 1) + 300 * vi, n)
            ent["n_tokens"][v], ent["sha256"][v] = n, text_sha256(t)
            vrows.append({"doc_id": did, "version": v, "text": t, "n_tokens": n,
                          "sha256": text_sha256(t), "writer": None, "attempt": None,
                          "seed": None})
        docs.append(ent)
    paths.insert_manifest.parent.mkdir(parents=True, exist_ok=True)
    paths.insert_manifest.write_text(json.dumps({"docs": docs, "totals": {}, "dropped": [],
                                                 "policies": {}, "inputs": {}}))
    _write_jsonl(paths.versions, vrows)
    paths.groups.parent.mkdir(parents=True, exist_ok=True)
    paths.groups.write_text(json.dumps({"assignment": assign}))
    for s in seeds:
        _write_jsonl(paths.background(s), _bg_rows(s, {
            "core": [10 + i % 11 for i in range(60)],
            "pad1": [7 + i % 4 for i in range(30)],
            "pad2": [7 + i % 4 for i in range(30)],
            "filler": FILLER_CYCLE * 4,
        }))
    return paths


@pytest.fixture
def world(tmp_path):
    paths = make_world(tmp_path)
    S.capacity(paths, G)
    return paths


@pytest.fixture
def built(world):
    b = S.Builder(world, G, _cache())
    mans = {a: b.build(a, 1) for a in S.PART1_STREAM_ARMS}
    return world, b, mans


def _flat(paths: S.Paths, arm: str, seed: int = 1) -> np.ndarray:
    return np.load(paths.stream(arm, seed)).reshape(-1)


# --- step 1: capacity ----------------------------------------------------------------------------


def _doc(i, o, c, p, g):
    return S.InsertDoc(f"d{i}", g, {"orig": o, "corr": c, "para": p}, {})


def test_capacity_over_every_version(world):
    cap = json.loads(world.capacity.read_text())
    # OO 80+10, RO +10 (A corr +2), OR -5, RR +5, para +10, bg = OO.
    t = {v: cap["totals"][v]["with_sep"] for v in S.CAP_VERSIONS}
    assert t == {"OO": 90, "RO": 100, "OR": 85, "RR": 95, "para": 100, "bg": 90}
    assert cap["C"] == 100 and cap["drops"] == [] and len(cap["docs"]) == N_DOCS
    assert set(cap["inputs"]) == {f"{C.INSERT_DIR}/insert_manifest.json",
                                  f"{C.INSERT_DIR}/versions.jsonl",
                                  f"{C.GROUPS_DIR}/groups.json"}
    assert cap["doses"]["ro"]["corr"] == sum(_lengths(j)["corr"] for j in range(0, 10, 2))
    assert cap["doses"]["bg"]["filler"] == cap["doses"]["orig"]["orig"] == 80


def test_repair_rr_uses_the_group_with_the_larger_increase():
    # A: +4, -3 (sum 1); B: +2, +2 (sum 4). RR is the largest version; group B is chosen, so d2
    # (first of B's equal +2) is dropped although d0 has the largest single increase.
    docs = [_doc(0, 10, 14, 10, "A"), _doc(1, 10, 7, 10, "A"), _doc(2, 10, 12, 10, "B"),
            _doc(3, 10, 12, 10, "B")]
    out = S.compute_capacity(docs, ceiling=47)
    assert out["totals_before"]["RR"] == 49 == max(out["totals_before"].values())
    assert [d["doc_id"] for d in out["drops"]] == ["d2"]
    assert out["drops"][0]["version"] == "RR" and out["docs"] == ["d0", "d1", "d3"]
    assert out["C"] <= 47
    for arm in S.PART1_STREAM_ARMS:  # the drop applies to every arm and cell
        assert out["doses"][arm]["n_docs"] == 3


def test_repair_iterates_on_the_largest_version_and_largest_increase():
    docs = [_doc(0, 10, 12, 10, "A"), _doc(1, 10, 14, 10, "A"), _doc(2, 10, 9, 10, "B"),
            _doc(3, 10, 9, 10, "B")]
    out = S.compute_capacity(docs, ceiling=34)  # RO 50 -> drop d1 (+4); RO 35 -> drop d0
    assert [(d["doc_id"], d["version"]) for d in out["drops"]] == [("d1", "RO"), ("d0", "RO")]
    assert out["docs"] == ["d2", "d3"] and out["C"] == 22


def test_repair_paraphrases_and_ties():
    docs = [_doc(0, 10, 10, 13, "A"), _doc(1, 10, 10, 15, "B"), _doc(2, 10, 10, 15, "A")]
    out = S.compute_capacity(docs, ceiling=44)  # para 46: d1 and d2 tie at +5 -> d1 (earlier)
    assert [(d["doc_id"], d["version"], d["rewritten"]) for d in out["drops"]] == [
        ("d1", "para", "para")]
    # version ties go to the first in CAP_VERSIONS order: RO and para tie at 27.
    docs = [_doc(0, 10, 15, 15, "A"), _doc(1, 10, 7, 10, "B")]
    out = S.compute_capacity(docs, ceiling=26)
    assert out["totals_before"]["RO"] == out["totals_before"]["para"] == 27
    assert [(d["doc_id"], d["version"]) for d in out["drops"]] == [("d0", "RO")]


def test_repair_cannot_fix_originals():
    with pytest.raises(S.CapacityError):
        S.compute_capacity([_doc(0, 50, 40, 50, "A")], ceiling=40)


def test_capacity_is_computed_once_and_locks_its_inputs(world):
    again = S.capacity(world, G)  # same inputs: returns the stored result
    assert again["C"] == 100
    rows = S.load_jsonl(world.versions)
    rows[0]["writer"] = "changed"
    _write_jsonl(world.versions, rows)
    with pytest.raises(S.CapacityError, match="inputs changed"):
        S.build_stream("orig", 1, world, G, _cache())
    world.capacity.unlink()
    with pytest.raises(S.CapacityError, match="missing"):
        S.build_stream("orig", 1, world, G, _cache())


def test_capacity_refuses_a_different_result_without_force(world):
    man = json.loads(world.insert_manifest.read_text())
    man["docs"] = man["docs"][:-1]
    world.insert_manifest.write_text(json.dumps(man))
    with pytest.raises(S.CapacityError, match="computed once"):
        S.capacity(world, G)
    assert len(S.capacity(world, G, force=True)["docs"]) == N_DOCS - 1


def test_drops_apply_to_every_built_arm(tmp_path):
    paths = make_world(tmp_path, seeds=(1,))
    cap = S.capacity(paths, S.Geometry(8, 40, 97, 1024))  # RO and para (100) exceed 97
    dropped = {d["doc_id"] for d in cap["drops"]}
    assert dropped and cap["C"] <= 97
    b = S.Builder(paths, S.Geometry(8, 40, 97, 1024), _cache())
    for arm in S.PART1_STREAM_ARMS:
        man = b.build(arm, 1)
        ids = {s["item_id"] for s in man["insert_spans"]}
        assert ids == set(cap["docs"]) and not ids & dropped


# --- step 2: fillers --------------------------------------------------------------------------


def test_filler_concatenation_hand_case():
    assert C.FILLER_RULE == "concatenate"
    rows = [{"n_tokens": n} for n in (3, 12, 5, 9, 20)]
    fills = S.match_fillers(rows, [10, 6, 25], filler_max=1024)
    # 10 = (3 + eos) + 6 of row 1 (its rest discarded); 6 = (5 + eos), exactly; 25 = (9 + eos) + 15
    assert fills == [[(0, 3, True), (1, 6, False)], [(2, 5, True)],
                     [(3, 9, True), (4, 15, False)]]
    assert all(sum(k + e for _, k, e in f) == L for f, L in zip(fills, [10, 6, 25], strict=True))
    with pytest.raises(S.ReserveError):
        S.match_fillers(rows, [10, 6, 25, 1], filler_max=1024)
    with pytest.raises(S.StreamError):
        S.match_fillers(rows, [2000], filler_max=1024)


def test_bg_fillers_concatenate_the_reserve_and_repeat(built):
    paths, _, mans = built
    f = mans["bg"]["fillers"]
    origs = [_lengths(j)["orig"] for j in range(N_DOCS)]
    assert [d["n_tokens"] for d in f["docs"]] == origs and f["rule"] == "concatenate"
    assert f["total_tokens"] == sum(origs)
    assert f["reserve_docs_consumed"] == sum(d["n_reserve_docs"] for d in f["docs"])
    # independent reference: the reserve in order, each doc + eos, cut per filler, rest dropped
    rows = S.load_background(paths.background(1))["filler"]
    expect, i = [], 0
    for L in origs:
        buf, used = [], 0
        while len(buf) < L:
            buf += [int(w) for w in rows[i]["text"].split()] + [EOS]
            i += 1
            used += 1
        expect.append(buf[:L])
    assert [d["n_reserve_docs"] for d in f["docs"]] == [
        len(d["reserve_docs"]) for d in f["docs"]]
    assert f["reserve_docs_consumed"] == i  # no reserve document skipped
    flat = _flat(paths, "bg")
    by: dict[str, list] = {}
    for s in mans["bg"]["insert_spans"]:
        assert s["version"] == "filler"
        by.setdefault(s["item_id"], []).append(flat[s["start"]:s["end"]].tolist())
    plan_ids = [s["item_id"] for s in mans["bg"]["insert_spans"] if s["half"] == 1]
    assert sorted(plan_ids) == sorted(by)
    for j, d in enumerate(f["docs"]):
        v = by[d["for_doc"]]
        assert len(v) == 2 and v[0] == v[1] == expect[j] + [EOS]  # same filler, both slots


def test_source_family_and_mix(built):
    for sd, topic, fam in SOURCES:
        assert S.source_family({"source_dir": sd, "topic": topic}) == fam
    assert S.source_family({"source_dir": "common_crawl-crime_and_law-0042"}) == (
        "common_crawl:crime_and_law")
    _, _, mans = built
    for arm, m in mans.items():
        mix = m["source_mix"]
        core_tokens = sum(r["n_tokens"] for h in m["halves"] for r in h["core_docs"])
        assert sum(x["tokens"] for x in mix["cores"].values()) == core_tokens
        assert sum(x["tokens"] for x in mix["pads"].values()) == sum(
            r["n_tokens"] for p in m["pads"] for r in p["docs"])
        assert set(mix["cores"]) <= {f for _, _, f in SOURCES}
        if arm == "bg":
            assert sum(x["tokens"] for x in mix["fillers"].values()) + sum(
                r["eos"] for d in m["fillers"]["docs"] for r in d["reserve_docs"]) == 80
        else:
            assert mix["fillers"] is None


# --- steps 3-6: cores, slots, pads, packing ------------------------------------------------------


def test_cores_identical_across_arms_and_differ_by_seed(built):
    paths, b, mans = built
    shas = {a: tuple(m["core_sha256"]) for a, m in mans.items()}
    assert len(set(shas.values())) == 1
    assert S.verify_cores(1, paths, G)["identical"]
    for a, m in mans.items():
        assert S.stream_core_sha256(np.load(paths.stream(a, 1)), m, G) == list(shas["orig"])
        assert all(h["core_tokens"] == G.half_tokens - 100 for h in m["halves"])
    m2 = b.build("orig", 2)
    assert tuple(m2["core_sha256"]) != shas["orig"]


def test_core_truncation_convention():
    rows = [{"n_tokens": n, "doc_id": str(i)} for i, n in enumerate((5, 5, 5, 5))]
    sel, nxt = S.take_reserve(rows, 9)  # 6 + (2 + separator)
    assert sel == [(0, 5), (1, 2)] and nxt == 2  # core 2 starts at the next document
    sel, nxt = S.take_reserve(rows, 7)  # 6 + a lone separator
    assert sel == [(0, 5), (1, 0)]
    with pytest.raises(S.ReserveError):
        S.take_reserve(rows, 100)


def test_slots_deterministic_and_identical_across_arms(built):
    _, b, mans = built
    a = S.draw_slots(30, 10, "slots", 1, 1)
    assert np.array_equal(a, S.draw_slots(30, 10, "slots", 1, 1))
    assert not np.array_equal(a, S.draw_slots(30, 10, "slots", 1, 2))
    assert not np.array_equal(a, S.draw_slots(30, 10, "slots", 2, 1))
    ref = {(s["item_id"], s["half"]): s["slot"] for s in mans["orig"]["insert_spans"]}
    for m in mans.values():
        assert {(s["item_id"], s["half"]): s["slot"] for s in m["insert_spans"]} == ref
    plan = b.plan(1)
    for h in (1, 2):
        assert [ref[(d.doc_id, h)] for d in plan.docs] == plan.slots[h - 1].tolist()


def test_slot_position_and_tie_order():
    core = [np.array([1, 2, EOS], np.uint32), np.array([3, EOS], np.uint32)]
    items = [S.Item(f"i{j}", "orig", np.array([10 + j], np.uint32)) for j in range(3)]
    toks, spans, _ = S.assemble_half(1, core, items, np.array([1, 0, 1]), offset=0, eos_id=EOS)
    assert toks.tolist() == [1, 2, EOS, 11, EOS, 3, EOS, 10, EOS, 12, EOS]
    assert [s["item_id"] for s in spans] == ["i1", "i0", "i2"]  # ties in order of j


def test_pads_at_start_and_end_with_compensating_sizes(built):
    paths, _, mans = built
    cap = json.loads(paths.capacity.read_text())
    T = G.total_tokens
    for arm, m in mans.items():
        P = cap["C"] - cap["doses"][arm]["insert_tokens_with_sep"]
        assert P >= 0
        p1, p2 = m["pads"]
        assert p1["tokens"] == p2["tokens"] == P
        assert p1["reserve"] == "pad1" and p2["reserve"] == "pad2"
        if P:
            assert p1["start"] == 0 and p2["end"] == T
            assert all(d["n_tokens"] <= G.filler_max for d in p1["docs"] + p2["docs"])
            assert sum(d["n_tokens"] + 1 for d in p1["docs"]) == P
    assert {a: mans[a]["pads"][0]["tokens"] for a in mans} == {
        "bg": 10, "orig": 10, "corr": 5, "para": 0, "ro": 0, "or": 15}


def test_exact_lengths_and_each_doc_once_per_half(built):
    paths, b, mans = built
    for arm, m in mans.items():
        arr = np.load(paths.stream(arm, 1))
        assert arr.dtype == np.uint32 and arr.shape == (G.total_rows, G.seq_len)
        assert m["tokens"] == G.total_tokens and m["rows"] == G.total_rows
        assert set(C.STREAM_MANIFEST_KEYS) <= set(m)
        flat = arr.reshape(-1)
        for h in (1, 2):
            spans = [s for s in m["insert_spans"] if s["half"] == h]
            assert sorted(s["item_id"] for s in spans) == sorted(m["insert_spans"][i]["item_id"]
                                                                 for i in range(N_DOCS))
            lo, hi = (h - 1) * G.half_tokens, h * G.half_tokens
            assert all(lo <= s["start"] < s["end"] <= hi for s in spans)
            assert flat[hi - 1] == EOS
        # independent search: each version's token run occurs exactly twice in the stream
        plan = b.plan(1)
        for d in plan.docs[:3]:
            v = S.arm_version(arm, d.group) if arm != "bg" else None
            if v is None:
                continue
            toks = [int(w) for w in d.text[v].split()]
            hits = [i for i in range(len(flat) - len(toks))
                    if flat[i] == toks[0] and flat[i:i + len(toks)].tolist() == toks]
            assert len(hits) == 2 and hits[0] < G.half_tokens <= hits[1]


def test_doses_match_capacity(built):
    paths, _, mans = built
    cap = json.loads(paths.capacity.read_text())
    for arm, m in mans.items():
        want = cap["doses"][arm]
        assert m["dose"]["corrected_tokens"] == want["corr"]
        assert m["dose"]["original_tokens"] == want["orig"]
        for half in m["assertions"]["dose_per_half"]:
            assert half == {v: want[v] for v in S.DOSE_VERSIONS if want[v]}
    assert mans["corr"]["dose"]["corrected_tokens"] == sum(_lengths(j)["corr"]
                                                           for j in range(N_DOCS))
    assert mans["or"]["dose"]["corrected_tokens"] == sum(_lengths(j)["corr"]
                                                         for j in range(1, N_DOCS, 2))


def test_text_hashes_recorded_and_tokenizer_count_checked(built, world):
    paths, b, mans = built
    plan = b.plan(1)
    sha = {(s["item_id"]): s["text_sha256"] for s in mans["para"]["insert_spans"]}
    assert all(sha[d.doc_id] == d.sha["para"] for d in plan.docs)
    rows = S.load_jsonl(paths.versions)
    rows[0]["n_tokens"] += 1  # a frozen count the tokenizer does not reproduce
    _write_jsonl(paths.versions, rows)
    man = json.loads(paths.insert_manifest.read_text())
    man["docs"][0]["n_tokens"]["orig"] += 1
    paths.insert_manifest.write_text(json.dumps(man))
    S.capacity(paths, G, force=True)
    with pytest.raises(S.StreamError, match="tokenizes to"):
        S.build_stream("orig", 1, paths, G, _cache())


# --- step 7: assertions ---------------------------------------------------------------------------


def _check(b: S.Builder, arm: str, arr: np.ndarray, man: dict, **over):
    _, ref, exp = b.make(arm, 1)
    kw = {"geometry": G, "eos_id": EOS, "spans": man["insert_spans"], "pads": man["pads"],
          "expected": exp, "core_tokens": ref["halves"][0]["core_tokens"],
          "core_sha256": ref["core_sha256"], "expected_dose": ref["dose"]["single_copy"]}
    kw.update(over)
    return S.check_stream(arr, **kw)


def test_verify_passes_and_rejects_a_tampered_file(built):
    paths, b, mans = built
    for arm in mans:
        assert S.verify_stream(arm, 1, builder=b)["item_set_equals_frozen"]
    arr = np.load(paths.stream("corr", 1))
    arr.reshape(-1)[mans["corr"]["insert_spans"][0]["start"]] += 1
    np.save(paths.stream("corr", 1), arr)
    with pytest.raises(S.StreamAssertionError, match="sha256"):
        S.verify_stream("corr", 1, builder=b)


def test_assertions_catch_corruption(built):
    paths, b, mans = built
    man = mans["corr"]
    good = np.load(paths.stream("corr", 1))
    assert _check(b, "corr", good, man)["cores_match_seed"]

    bad = good.copy()
    bad.reshape(-1)[man["insert_spans"][3]["start"] + 1] = 7  # inside an insert
    with pytest.raises(S.StreamAssertionError, match="complete document"):
        _check(b, "corr", bad, man)

    covered = np.zeros(G.total_tokens, bool)
    for s in man["insert_spans"] + man["pads"]:
        covered[s["start"]:s["end"]] = True
    core_pos = int(np.flatnonzero(~covered)[5])
    bad = good.copy()
    bad.reshape(-1)[core_pos] += 1  # inside the core
    with pytest.raises(S.StreamAssertionError, match="core sha256"):
        _check(b, "corr", bad, man)

    spans = [s for s in man["insert_spans"] if s["half"] == 1][:-1] + [
        s for s in man["insert_spans"] if s["half"] == 2]
    with pytest.raises(S.StreamAssertionError, match="item set"):
        _check(b, "corr", good, man, spans=spans)

    with pytest.raises(S.StreamAssertionError, match="dose"):
        _check(b, "corr", good, man, expected_dose={"corr": 1})

    with pytest.raises(S.StreamAssertionError, match="shape"):
        _check(b, "corr", good[:-1], man)

    moved = [dict(s) for s in man["pads"]]
    moved[0]["start"] += 1
    with pytest.raises(S.StreamAssertionError, match="pad"):
        _check(b, "corr", good, man, pads=moved)


def test_verify_cores_detects_a_differing_core(built):
    paths, _, mans = built
    arr = np.load(paths.stream("para", 1))
    covered = np.zeros(G.total_tokens, bool)
    for s in mans["para"]["insert_spans"] + mans["para"]["pads"]:
        covered[s["start"]:s["end"]] = True
    arr.reshape(-1)[int(np.flatnonzero(~covered)[0])] += 1
    np.save(paths.stream("para", 1), arr)
    with pytest.raises(S.StreamAssertionError, match="core"):
        S.verify_cores(1, paths, G)


def test_token_cache_persists(tmp_path):
    enc = FakeEncoder()
    c = S.TokenCache(enc, tmp_path)
    a = c.get(["1 2 3", "4 5"])
    c.flush()
    enc2 = FakeEncoder()
    c2 = S.TokenCache(enc2, tmp_path)
    b = c2.get(["4 5", "1 2 3"])
    assert enc2.calls == 0 and [x.tolist() for x in b] == [[4, 5], [1, 2, 3]]
    assert [x.tolist() for x in a] == [[1, 2, 3], [4, 5]]


def test_part1_reserve_check(world):
    r = S.part1_reserve_check(1, world, G)
    assert r["ok"] and r["pad_need_max"] == 15 and r["filler_tokens_needed"] == 80
    assert 80 <= r["filler_tokens_used"] <= r["filler_tokens_available"]
    short = S.load_background(world.background(1))
    short["filler"] = short["filler"][:3]
    short["pad2"] = short["pad2"][:1]
    r = S.part1_reserve_check(1, world, G, background=short)
    assert not r["ok"] and [p.split(":")[0] for p in r["problems"]] == ["filler", "pad2"]


def test_smoke_stream_background_only(world):
    _write_jsonl(world.background(1), _bg_rows(1, {  # small reserves, so every role is reached
        "core": [12] * 10, "pad1": [9] * 10, "pad2": [9] * 10, "filler": [11] * 40}))
    m = S.build_smoke(1, world, G, _cache())
    arr = np.load(world.streams / "smoke.npy")
    assert arr.dtype == np.uint32 and arr.shape == (G.total_rows, G.seq_len)
    assert m["smoke"] is True and m["run_id"] == "smoke" and m["insert_spans"] == []
    assert "not applicable" in m["assertions"]["insert_assertions"]
    assert S.sha256_file(world.streams / "smoke.npy") == m["stream_sha256"]
    assert m["inputs"] == {f"{C.BACKGROUND_DIR}/s1.jsonl": S.sha256_file(world.background(1))}
    assert set(C.STREAM_MANIFEST_KEYS) <= set(m)
    # reference: reserve rows core, pad1, pad2, filler in order, each + eos, cut at T
    bg = S.load_background(world.background(1))
    ref = []
    for role in ("core", "pad1", "pad2", "filler"):
        for r in bg[role]:
            ref += [int(w) for w in r["text"].split()] + [EOS]
    ref = ref[:G.total_tokens - 1] + [EOS]  # the last document truncated, keeping its separator
    assert arr.reshape(-1).tolist() == ref
    assert m["background_docs"]["by_role"] == {"core": 10, "pad1": 10, "pad2": 10, "filler": 26}
    assert m["background_docs"]["last_truncated"]
    insert_tokens = {int(w) for line in S.load_jsonl(world.versions) for w in line["text"].split()}
    assert not insert_tokens & set(arr.reshape(-1).tolist())  # no insert text


def test_real_tokenizer_encoder(olmo_tok):
    enc = S.HFEncoder(olmo_tok)
    assert enc.eos_id == S.OLMO_EOS_ID == 100257
    ids = enc.encode_batch(["Hello world.", "A second text."])
    assert len(ids) == 2 and all(EOS not in x and x for x in ids)


def test_cli_build_and_verify(world, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "e9_streams", Path(__file__).resolve().parents[2] / "scripts" / "e9_streams.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    kw = {"paths": world, "geometry": G, "cache": _cache()}
    assert mod.main(["build-all", "--seeds", "1", "--arms", "orig", "bg"], **kw) == 0
    assert mod.main(["build", "--arm", "ro", "--seed", "1"], **kw) == 0
    assert mod.main(["verify", "--seed", "1"], **kw) == 0
    out = capsys.readouterr().out
    assert '"orig"' in out and '"bg"' in out and '"identical": true' in out
    assert mod.main(["verify", "--seed", "2"], **kw) == 1  # nothing built at seed 2
    assert mod.main(["reserve-check", "--seeds", "1", "2"], **kw) == 0
    assert mod.main(["build-smoke", "--seed", "1"], **kw) == 0
    assert (world.streams / "smoke.manifest.json").exists()
    assert mod.main(["confirm-check", "--seeds", "1"], **kw) == 1  # tiny reserves, real doses


# --- confirmation (§4.7) --------------------------------------------------------------------------


def make_confirm(paths: S.Paths, seed: int, arms: dict[str, list[tuple[str, int]]], D: int,
                 branch: str) -> dict:
    rows, seen = [], set()
    for lst in arms.values():
        for did, n in lst:
            if did in seen:
                continue
            seen.add(did)
            t = _text(500_000 + 1000 * len(seen), n)
            rows.append({"doc_id": did, "version": "corr", "text": t, "n_tokens": n,
                         "sha256": text_sha256(t), "writer": "luna", "attempt": 1, "seed": 0})
    _write_jsonl(paths.confirm_versions, rows)
    sel = {"D": D, "branch": branch, "arms": {a: [d for d, _ in v] for a, v in arms.items()}}
    paths.confirm_selections.write_text(json.dumps(sel))
    _write_jsonl(paths.background(seed), _bg_rows(seed, {
        "core": [10 + i % 11 for i in range(80)],
        "filler": [5 + i % 9 for i in range(120)],
    }))
    return sel


GROUP_ARMS = {  # D = 100: 100, 98, 101 corrected tokens; p0 shared by cpol and ccon
    "cpol": [(f"p{i}", 10) for i in range(10)],
    "ccon": [("p0", 10)] + [(f"q{i}", 11) for i in range(8)],
    "crnd": [(f"r{i}", 12) for i in range(8)] + [("r8", 5)],
    "cbg": [],
}
NOGROUP_ARMS = {  # D = 280 (~0.88 of a half, like 4.4M of 5.0M): 280 and 282
    "ccon": [(f"c{i}", 40) for i in range(7)],
    "crnd": [(f"x{i}", 47) for i in range(6)],
    "cbg": [],
}


@pytest.mark.parametrize("arms,D,branch", [(GROUP_ARMS, 100, "group_policy"),
                                           (NOGROUP_ARMS, 280, "no_group")])
def test_confirmation_streams(tmp_path, arms, D, branch):
    paths = S.Paths(tmp_path)
    sel = make_confirm(paths, 5, arms, D, branch)
    n_max = max(len(v) for v in arms.values())
    R = S.r_h(D, n_max)
    assert R == math.ceil(1.02 * D) + n_max
    b = S.Builder(paths, G, _cache())
    mans = {a: b.build(a, 5) for a in arms}
    cores = {tuple(m["core_sha256"]) for m in mans.values()}
    assert len(cores) == 1 and S.verify_cores(5, paths, G)["identical"]
    for a, m in mans.items():
        assert S.verify_stream(a, 5, builder=b)["cores_match_seed"]
        flat = _flat(paths, a, 5)
        assert flat.size == G.total_tokens and m["pads"] == []
        assert all(h["core_tokens"] == G.half_tokens - R for h in m["halves"])
        ids = [d for d, _ in arms[a]]
        d_arm = sum(n for _, n in arms[a])
        assert m["dose"]["corrected_tokens"] == d_arm
        if ids:
            assert S.dose_ok(d_arm, D) and abs(d_arm - D) <= 0.02 * D
        by: dict[str, list] = {}
        for s in m["insert_spans"]:
            by.setdefault(s["item_id"], []).append((s["half"], flat[s["start"]:s["end"]].tolist()))
        for d in ids:  # each corrected doc once per half, complete
            assert sorted(h for h, _ in by[d]) == [1, 2] and by[d][0][1] == by[d][1][1]
        fill = {k: v for k, v in by.items() if k.startswith("filler")}
        assert all(sorted(h for h, _ in v) == [1, 2] and v[0][1] == v[1][1]
                   for v in fill.values())  # the same fillers in both halves
        region = sum(len(v[0][1]) for v in by.values())
        assert region == R  # corrected docs + fillers, separators included, fill R_h exactly
        assert all(len(v[0][1]) - 1 <= G.filler_max for v in fill.values())
        if a == "cbg":
            assert set(by) == set(fill)
    # the shared document uses the same correction in both arms that hold it
    if branch == "group_policy":
        def toks(a):
            f = _flat(paths, a, 5)
            s = next(s for s in mans[a]["insert_spans"] if s["item_id"] == "p0")
            return f[s["start"]:s["end"]].tolist()
        assert toks("cpol") == toks("ccon")
    assert sel["branch"] == branch


def test_confirmation_refuses_a_dose_outside_tolerance(tmp_path):
    paths = S.Paths(tmp_path)
    make_confirm(paths, 5, {"cpol": [("a", 10)] * 1 + [("b", 80)], "cbg": []}, 100,
                 "group_policy")  # 90 < 98
    with pytest.raises(S.StreamAssertionError, match="dose"):
        S.Builder(paths, G, _cache()).build("cpol", 5)


def test_confirm_reserve_check_tiny_both_branches(tmp_path):
    for D, n_max in ((100, 10), (280, 7)):
        paths = S.Paths(tmp_path / str(D))
        make_confirm(paths, 5, {"cbg": []}, D, "no_group")
        r = S.confirm_reserve_check(5, D, n_max, paths=paths, geometry=G)
        assert r["ok"] and r["R_h"] == S.r_h(D, n_max)
        assert r["core_need_per_half"] == G.half_tokens - r["R_h"]
    short = {"core": [{"n_tokens": 10, "doc_id": str(i)} for i in range(5)],
             "filler": [{"n_tokens": 10, "doc_id": str(i)} for i in range(5)]}
    r = S.confirm_reserve_check(5, 100, 10, geometry=G, background=short)
    assert not r["ok"] and len(r["problems"]) == 2


def _rows(total: int, n: int = 999) -> list[dict]:
    return [{"n_tokens": n, "doc_id": str(i)} for i in range(total // (n + 1))]


@pytest.mark.parametrize("branch", ["group_policy", "no_group"])
def test_confirm_reserve_check_real_geometry(branch):
    """At the real geometry, the frozen confirmation reserve targets suffice for both branches at
    n_max from 0 to its upper bound; a filler reserve of 4M tokens is short at D ~ 4.4M only."""
    D = S.PROJECTED_D[branch]
    bg = {"core": _rows(C.BACKGROUND_RESERVES_CONFIRM["core"]["min_tokens"]),
          "filler": _rows(C.BACKGROUND_RESERVES_CONFIRM["filler"]["min_tokens"])}
    for n_max in (0, S.projected_n_max(D)):
        r = S.confirm_reserve_check(5, D, n_max, background=bg)
        assert r["ok"], r["problems"]
        assert r["R_h"] == math.ceil(1.02 * D) + n_max
    short = {"core": bg["core"], "filler": _rows(4_000_000)}
    r = S.confirm_reserve_check(5, D, 0, background=short)
    assert r["ok"] is (branch == "group_policy")


def test_confirm_check_all_projects_both_branches(tmp_path):
    paths = S.Paths(tmp_path)
    _write_jsonl(paths.background(5), _bg_rows(5, {"core": [10] * 5, "filler": [10] * 5}))
    res = S.confirm_check_all([5], paths, geometry=G)
    assert {r["case"] for r in res} == {"group_policy", "no_group"} and len(res) == 4
    assert not any(r["ok"] for r in res)  # tiny reserves vs real-sized doses
