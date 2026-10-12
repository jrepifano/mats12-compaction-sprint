"""Tests for p11.score and p11.raw_samples on small synthetic fixtures (no real results needed)."""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest
from p03.core import read_jsonl
from p11 import items as I
from p11 import raw_samples as R
from p11 import score as S

N = 6


def mk_item(k: int, **kw) -> I.Item:
    d = dict(  # noqa: C408
        id=f"A-{k:04d}",
        relation="painting_creator",
        role="painter",
        entity=f'the painting "Work {k}"',
        question=f'What is the country of citizenship of the painter of the painting "Work {k}"? '
        "Answer in one word.",
        bridge=f"Alice Bridgeton{k}",
        bridge_aliases=[f"A. Bridgeton{k}"],
        y=f"Yusuf Truename{k}",
        y_aliases=[f"Yusuf Q. Truename{k}"],
        y_country="France",
        y_country_aliases=["French Republic"],
        qwen_answer="Russian",
        qwen_answer_share=0.9,
        y_nation_forms=["France", "French Republic", "French"],
    )
    return I.Item(**(d | kw))


@pytest.fixture
def items(monkeypatch) -> list[I.Item]:
    monkeypatch.setattr(I, "N_ITEMS", N)
    return [mk_item(k) for k in range(N)]


def write_items(tmp: Path, items: list[I.Item]) -> Path:
    p = tmp / "items.jsonl"
    p.write_text("".join(json.dumps(asdict(i)) + "\n" for i in items))
    return p


def cells(iid: str, draw: str, text_at: dict[tuple[int, int], str] | None = None) -> list[dict]:
    """A full 6 x 3 grid for one item and one draw; default text names nobody."""
    out = []
    for lay in S.LAYERS:
        for pos in S.POSITIONS:
            t = (text_at or {}).get((lay, pos), "- French, I think.")
            out.append(
                {
                    "draw": draw,
                    "id": iid,
                    "layer": lay,
                    "pos": pos,
                    "site_index": 0,
                    "source_index": 0,
                    "source_id": iid,
                    "seed": 1,
                    "batch_index": 0,
                    "text": t,
                    "n_new_tokens": 5,
                    "finish": "eos",
                }
            )
    return out


def write_arm(olens: Path, arm: str, k: int, rows: list[dict]) -> None:
    olens.mkdir(parents=True, exist_ok=True)
    (olens / f"{arm}_s{k}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


# --------------------------------------------------------------------------- matching


def test_clean_aliases_examples():
    assert I.clean_aliases(["Gustave Moreau (French painter)"]) == ["Gustave Moreau"]
    assert I.clean_aliases(["William Allan (Sir)"]) == ["William Allan"]
    assert I.clean_aliases(["Νικόλαος Γύζης (Nikolaos Gyzis)"]) == [
        "Νικόλαος Γύζης",
        "Nikolaos Gyzis",
    ]


def test_phrase_forms_match_items_names_and_drop_qualifier_surnames():
    t, al = "Gustave Moreau", ["Gustave Moreau (painter)", "Gustave Moreau (French painter)"]
    phr = S.phrase_forms(t, al)
    assert "painter" not in phr and "moreau" in phr
    assert "painter" in S.raw_phrase_forms(t, al)  # P03's original surname bug
    for text in (
        "He was a French painter.",
        "The work is by Gustave Moreau.",
        "Moreau, yes",
        "",
        "moreaux",
    ):
        assert S.hit(text, phr) == I.names(text, t, al)
    assert not S.hit("He was a French painter.", phr)
    assert S.hit("He was a French painter.", S.raw_phrase_forms(t, al))


def test_form_overlaps_lists_shared_surnames(items):
    its = items[:2] + [
        mk_item(9, bridge="John Smithson", bridge_aliases=[], y="Adam Smithson", y_aliases=[])
    ]
    ov = S.form_overlaps(S.item_forms(its))
    assert ov == [{"id": "A-0009", "shared": ["smithson"]}]


# --------------------------------------------------------------------------- inputs


def test_discover_arms_orders_and_skips_smoke(tmp_path):
    for name in (
        "patched_author_s1",
        "patched_author_s0",
        "plain_s0",
        "zzz_s0",
        "patched_author_a25_s0",
        "smoke_shuffled",
    ):
        (tmp_path / f"{name}.jsonl").write_text("")
    arms = S.discover_arms(tmp_path)
    assert list(arms) == ["plain", "patched_author", "patched_author_a25", "zzz"]
    assert [p.name for p in arms["patched_author"]] == [
        "patched_author_s0.jsonl",
        "patched_author_s1.jsonl",
    ]
    assert S.discover_arms(tmp_path / "missing") == {}


# --------------------------------------------------------------------------- statistics


def test_item_any_rate_and_paired(items):
    forms = S.item_forms(items[:3])
    a, b, c = (it.id for it in items[:3])
    rows = [
        {"id": a, "text": "Truename0 painted it"},
        {"id": a, "text": "no"},
        {"id": b, "text": "Bridgeton1"},
        {"id": "A-9999", "text": "x"},
    ]
    assert S.item_any(rows, forms, "y") == {a: True, b: False}
    assert S.item_any(rows, forms, "bridge") == {a: False, b: True}
    r = S.rate({a: True, b: False, c: False})
    assert (r["k"], r["n"]) == (1, 3) and r["rate"] == pytest.approx(1 / 3)
    p = S.paired({a: True, b: True, c: False}, {a: True, b: False, c: True}, n_boot=500)
    assert p["diff"] == pytest.approx(0.0) and p["n"] == 3
    assert p["table"] == {"both": 1, "only_a": 1, "only_b": 1, "neither": 0}
    p = S.paired({a: True, b: True}, {a: False, b: False, c: True}, n_boot=500)
    assert p["diff"] == 1.0 and p["lo"] == 1.0 and p["n"] == 2


def test_conditional_rates_and_empty_group():
    hits = {"a": True, "b": True, "c": False, "d": False, "e": True}
    flips = {"a": True, "b": True, "c": True, "d": False, "e": False}
    out = S.conditional(hits, flips, n_boot=2000)
    assert out["flipped"]["rate"] == pytest.approx(2 / 3)
    assert out["not_flipped"]["rate"] == pytest.approx(0.5)
    assert out["diff"] == pytest.approx(2 / 3 - 0.5)
    assert out["lo"] <= out["diff"] <= out["hi"]
    none = S.conditional(hits, {k: True for k in hits}, n_boot=100)
    assert math.isnan(none["diff"]) and none["skipped"] == 100


def test_dose_pooled_counts():
    a = S.DOSE_ALPHAS
    units = {
        # Y-naming switches on exactly where Qwen flips
        "i1": {
            a[0]: (False, False, True),
            a[1]: (False, False, True),
            a[2]: (True, True, False),
            a[3]: (True, True, False),
        },
        # names Y while Qwen has not flipped
        "i2": {
            a[0]: (False, True, True),
            a[1]: (False, False, True),
            a[2]: (False, False, False),
            a[3]: (True, True, False),
        },
        # never flips, never names Y (constant: not a "switch")
        "i3": {x: (False, False, True) for x in a},
    }
    out = S.dose_pooled(units, n_boot=2000)
    assert out["n_units"] == 12 and out["n_flipped_units"] == 3
    assert out["names_y_given_flipped"] == 1.0
    assert out["names_y_given_not_flipped"] == pytest.approx(1 / 9)
    assert out["diff"] == pytest.approx(8 / 9)
    assert out["names_bridge_given_not_flipped"]["k"] == 8
    assert out["items_y_at_a_non_flip"] == ["i2"]
    assert out["items_y_tracks_flip_exactly"] == ["i1"]
    assert out["n_items_all_alphas"] == 3


def test_dose_units_needs_answer_and_lens():
    u = S.dose_units({1.0: {"a": True, "b": False}}, {1.0: {"a": True}}, {1.0: {"a": False}})
    assert u == {"a": {1.0: (True, True, False)}}


# --------------------------------------------------------------------------- answers and smoke


def test_answer_classes_manipulation_and_flips(items):
    by = {it.id: it for it in items}
    a, b = items[0].id, items[1].id
    rows = [
        {"id": a, "kind": "plain", "premise": None, "lp": None, "text": "Russian", "answer": ""},
        {"id": b, "kind": "plain", "premise": None, "lp": None, "text": "", "answer": "Italian"},
        {"id": a, "kind": "patched", "premise": "author", "lp": 20, "text": "", "answer": "French"},
        {
            "id": b,
            "kind": "patched",
            "premise": "author",
            "lp": 20,
            "text": "",
            "answer": "Russian",
        },
        {
            "id": a,
            "kind": "direct",
            "premise": "mention",
            "lp": None,
            "text": "",
            "answer": "France",
        },
        {
            "id": a,
            "kind": "dose",
            "premise": "author",
            "lp": 20,
            "alpha": 0.5,
            "text": "",
            "answer": "French",
        },
    ]
    cl = S.answer_classes(rows, by)
    assert cl[("plain", None, None, None)] == {a: "stay", b: "other"}
    assert cl[("dose", "author", 20, 0.5)] == {a: "flip"}
    m = S.manipulation(cl)
    assert m["patched"]["lp20|author"]["flip"]["k"] == 1
    assert m["direct"]["mention"]["flip"]["k"] == 1
    assert m["plain"]["stay"]["k"] == 1
    assert S.flips_at(cl, "author", 20) == {a: True, b: False}
    assert S.flips_at(cl, "author", None) is None
    assert S.max_author_flip(None, cl) == 0.5
    assert S.max_author_flip({"rates": {"20": {"author": 0.3}, "24": {"author": 0.4}}}, cl) == 0.4


def test_shuffled_smoke(items):
    forms = S.item_forms(items)
    rows = [{"id": items[k % N].id, "source_id": "X", "text": "nothing"} for k in range(18)]
    rows += [
        {"id": items[0].id, "source_id": "X", "text": "Alice Bridgeton0"},
        {"id": items[1].id, "source_id": "X", "text": "Truename1"},
    ]
    out = S.shuffled_smoke(rows, forms)
    assert (out["named"], out["neither"], out["pass"]) == (2, 18, True)
    rows[0]["text"] = "Truename0"
    assert S.shuffled_smoke(rows, forms)["pass"] is False


# --------------------------------------------------------------------------- verdicts


def _summ(primary, drop, fl_author, fl_plain, arms_rates, sweep=None, mx=None):
    arms = {
        a: {"names_y": {"rate": y}, "names_bridge": {"rate": b}} for a, (y, b) in arms_rates.items()
    }
    return {
        "arms": arms,
        "primary": primary,
        "sweep": sweep or "absent",
        "max_author_flip": mx,
        "secondary": {
            "bridge_patched_author_minus_plain": drop,
            "flipped_only": {
                "patched_author": {"names_bridge": {"rate": fl_author}},
                "plain": {"names_bridge": {"rate": fl_plain}},
            },
        },
    }


def test_verdicts_read_and_side():
    s = _summ(
        {"diff": 0.3, "lo": 0.1, "hi": 0.5},
        {"diff": -0.2},
        0.1,
        0.4,
        {
            "plain": (0.0, 0.4),
            "neutral": (0.0, 0.1),
            "author": (0.6, 0.0),
            "patched_author": (0.4, 0.2),
            "patched_mention": (0.1, 0.3),
        },
        sweep={"status": "ok"},
        mx=0.5,
    )
    v = S.verdicts(s)
    assert v["read"]["verdict"] is True
    assert v["re_answer"]["verdict"] is False
    assert v["uninformative"]["verdict"] is False
    assert v["side_plain_bridge_0.30_0.45"]["verdict"] is True
    assert v["side_neutral_le_half_plain"]["verdict"] is True
    assert v["side_direct_author_names_y_ge_0.5"]["verdict"] is True


def test_verdicts_reanswer_uninformative_and_absent():
    s = _summ(
        {"diff": 0.02, "lo": -0.1, "hi": 0.12},
        {"diff": -0.02},
        0.35,
        0.40,
        {"patched_author": (0.2, 0.3), "patched_mention": (0.25, 0.3)},
        sweep={"status": "ok"},
        mx=0.4,
    )
    v = S.verdicts(s)
    assert v["read"]["verdict"] is False and v["re_answer"]["verdict"] is True
    assert v["uninformative"]["verdict"] is True  # mention names Y >= author
    assert v["side_plain_bridge_0.30_0.45"]["verdict"] is None
    empty = S.verdicts(
        {
            "arms": {},
            "primary": "absent",
            "secondary": {},
            "sweep": "absent",
            "max_author_flip": None,
        }
    )
    assert all(x["verdict"] is None for x in empty.values())


# --------------------------------------------------------------------------- end to end


def build_results(tmp: Path, items: list[I.Item]) -> Path:
    """Synthetic results: plain names the bridge on items 0-2; patched_author names Y on items
    0-3 in draw s0 (flipped: 0-2); patched_mention names Y on item 0 only; dose: item 0 flips
    at alpha >= 0.5, and the lens names Y on item 0 at 0.5 and 0.75 and on item 5 (never
    flipped) at 0.5."""
    res = tmp / "results"
    ol = res / "olens"
    hit = (60, -1)
    for k in (0, 1):
        write_arm(
            ol,
            "plain",
            k,
            [
                r
                for j, it in enumerate(items)
                for r in cells(it.id, f"plain_s{k}", {hit: f"It is {it.bridge}"} if j < 3 else None)
            ],
        )
        write_arm(
            ol,
            "patched_author",
            k,
            [
                r
                for j, it in enumerate(items)
                for r in cells(it.id, "pa", {hit: f"By {it.y}"} if j < 4 and k == 0 else None)
            ],
        )
        write_arm(
            ol,
            "patched_mention",
            k,
            [
                r
                for j, it in enumerate(items)
                for r in cells(it.id, "pm", {hit: f"By {it.y}"} if j == 0 else None)
            ],
        )
    for a in ("a25", "a50", "a75"):
        write_arm(
            ol,
            f"patched_author_{a}",
            0,
            [
                r
                for j, it in enumerate(items)
                for r in cells(
                    it.id,
                    a,
                    {hit: f"By {it.y}"}
                    if (a == "a50" and j in (0, 5)) or (a == "a75" and j == 0)
                    else None,
                )
            ],
        )
    write_jsonl(
        ol / "smoke_shuffled.jsonl",
        [
            {
                "id": items[k % N].id,
                "source_id": items[(k + 1) % N].id,
                "text": "none",
                "layer": 60,
                "pos": -1,
            }
            for k in range(20)
        ],
    )
    ans = []
    for j, it in enumerate(items):
        ans.append(
            {
                "id": it.id,
                "kind": "plain",
                "premise": None,
                "lp": None,
                "text": "",
                "answer": "Russian",
            }
        )
        for prem in ("author", "mention"):
            ans.append(
                {
                    "id": it.id,
                    "kind": "direct",
                    "premise": prem,
                    "lp": None,
                    "text": "",
                    "answer": "French" if prem == "author" else "Russian",
                }
            )
            for lp in (16, 20):
                f = prem == "author" and lp == 20 and j < 3
                ans.append(
                    {
                        "id": it.id,
                        "kind": "patched",
                        "premise": prem,
                        "lp": lp,
                        "text": "",
                        "answer": "French" if f else "Russian",
                    }
                )
        for alpha in (0.25, 0.5, 0.75):
            ans.append(
                {
                    "id": it.id,
                    "kind": "dose",
                    "premise": "author",
                    "lp": 20,
                    "alpha": alpha,
                    "text": "",
                    "answer": "French" if alpha >= 0.5 and j == 0 else "Russian",
                }
            )
    write_jsonl(res / "answers.jsonl", ans)
    (res / "sweep.json").write_text(
        json.dumps(
            {
                "chosen_lp": 20,
                "status": "ok",
                "rates": {
                    "16": {"author": 0.0, "mention": 0.0},
                    "20": {"author": 0.5, "mention": 0.0},
                },
            }
        )
    )
    return res


def test_summarize_end_to_end(tmp_path, items):
    res = build_results(tmp_path, items)
    ro = tmp_path / "p03ro"
    write_jsonl(
        ro / "olens.jsonl",
        [
            {"id": items[0].id, "layer": 20, "pos": -1, "samples": ["Alice Bridgeton0"]},
            {"id": items[1].id, "layer": 60, "pos": -1, "samples": ["a French painter"]},
        ],
    )
    s = S.summarize(res, write_items(tmp_path, items), ro, n_boot=500)
    assert s["arms_absent"] == ["neutral", "author", "mention"]
    pl, pa = s["arms"]["plain"], s["arms"]["patched_author"]
    assert pl["names_bridge"]["k"] == 3 and pl["names_y"]["k"] == 0
    assert pl["n_cells"] == pl["n_cells_expected"] == N * 18 * 2
    assert pl["per_layer"]["60"]["bridge"]["k"] == 3 and pl["per_layer"]["40"]["bridge"]["k"] == 0
    assert pa["per_draw"]["s0"]["y"]["k"] == 4 and pa["per_draw"]["s1"]["y"]["k"] == 0
    assert s["arms"]["patched_author_a50"]["n_cells_expected"] == N * 18
    assert s["primary"]["diff"] == pytest.approx(3 / N)
    assert s["primary"]["table"] == {"both": 1, "only_a": 3, "only_b": 0, "neither": 2}
    assert s["secondary"]["flipped_only"]["n"] == 3
    assert s["secondary"]["direct_author_names_y"] == "absent"
    fc = s["flip_conditional"]["patched_author"]["names_y_flipped_minus_not"]
    assert fc["flipped"]["rate"] == 1.0 and fc["not_flipped"]["rate"] == pytest.approx(1 / 3)
    assert s["manipulation"]["patched"]["lp20|author"]["flip"]["k"] == 3
    assert s["manipulation"]["direct"]["author"]["flip"]["k"] == N
    assert s["shuffled_smoke"]["pass"] is True
    assert s["prompt_only"] == "absent"
    assert s["direct_flip_conditional"] == {"author": "absent", "mention": "absent"}
    d = s["dose"]
    assert d["per_alpha"]["1.0"]["n_cells"] == N * 18  # draw s0 only
    assert d["per_alpha"]["0.5"]["flip"]["k"] == 1 and d["per_alpha"]["0.5"]["names_y"]["k"] == 2
    assert d["pooled"]["n_units"] == 4 * N
    assert d["pooled"]["items_y_at_a_non_flip"] == [items[3].id, items[5].id]
    assert d["pooled"]["items_y_tracks_flip_exactly"] == [items[0].id, items[1].id, items[2].id]
    rs = s["p03_rescore"]
    assert rs["olens"]["all_layers"]["cleaned"]["k"] == 1
    assert rs["olens_shuffled"] == "absent"
    assert rs["olens"]["layers_40_60"]["cleaned"]["k"] == 0
    assert s["predictions"]["read"]["verdict"] is not None
    # report writes strict JSON
    S.report(res, items_path=write_items(tmp_path, items), p03_readouts=ro)
    json.loads((res / "summary.json").read_text())


def test_direct_flip_conditional(tmp_path, items):
    res = build_results(tmp_path, items)
    # the plain draw stands in for a direct-mention draw; mention answers flip on items 0-1
    shutil.copy(res / "olens" / "plain_s0.jsonl", res / "olens" / "mention_s0.jsonl")
    ans = read_jsonl(res / "answers.jsonl")
    for r in ans:
        if (
            r["kind"] == "direct"
            and r["premise"] == "mention"
            and r["id"]
            in {
                items[0].id,
                items[1].id,
            }
        ):
            r["answer"] = "French"
    write_jsonl(res / "answers.jsonl", ans)
    s = S.summarize(res, write_items(tmp_path, items), tmp_path / "ro", n_boot=200)
    d = s["direct_flip_conditional"]
    assert d["author"] == "absent"
    b = d["mention"]["names_bridge_flipped_minus_not"]
    assert b["flipped"]["n"] == 2 and b["not_flipped"]["n"] == N - 2
    rows = read_jsonl(res / "olens" / "mention_s0.jsonl")
    hits = S.item_any(rows, S.item_forms(items), "bridge")
    k_f = sum(hits[items[j].id] for j in (0, 1))
    assert b["flipped"]["k"] == k_f
    assert b["diff"] == pytest.approx(k_f / 2 - (sum(hits.values()) - k_f) / (N - 2))


def test_p03_rescore_shows_alias_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "N_ITEMS", 1)
    it = mk_item(0, bridge="Gustave Moreau", bridge_aliases=["Gustave Moreau (French painter)"])
    ro = tmp_path / "ro"
    write_jsonl(
        ro / "olens.jsonl",
        [{"id": it.id, "layer": 44, "pos": -1, "samples": ["He was a French painter."]}],
    )
    out = S.p03_rescore([it], ro)
    sec = out["olens"]["layers_40_60"]
    assert sec["original"]["k"] == 1 and sec["cleaned"]["k"] == 0 and sec["lost"] == [it.id]


def test_summarize_with_nothing(tmp_path, items):
    s = S.summarize(tmp_path / "none", write_items(tmp_path, items), tmp_path / "ro", n_boot=10)
    for k in ("primary", "manipulation", "shuffled_smoke", "prompt_only", "dose", "sweep"):
        assert s[k] == "absent"
    assert s["flip_conditional"] == {"patched_author": "absent", "patched_mention": "absent"}
    assert all(v["verdict"] is None for v in s["predictions"].values())
    assert "plain" in S.table(s) or "absent arms" in S.table(s)


def test_prompt_only_floor(tmp_path, items):
    res = build_results(tmp_path, items)
    write_jsonl(
        res / "prompt_only.jsonl",
        [
            {
                "id": it.id,
                "sample_index": k,
                "seed": k,
                "text": f"thinking of {it.bridge}" if j == 0 and k == 3 else "hmm",
            }
            for j, it in enumerate(items)
            for k in range(36)
        ],
    )
    s = S.summarize(res, write_items(tmp_path, items), tmp_path / "ro", n_boot=500)
    po = s["prompt_only"]
    assert po["names_bridge"]["k"] == 1 and po["samples_per_item"] == {36: N}
    assert po["plain_lens_minus_prompt_only_bridge"]["diff"] == pytest.approx(2 / N)


# --------------------------------------------------------------------------- raw samples


def test_raw_samples_seeded_and_complete(tmp_path, items):
    res = build_results(tmp_path, items)
    ip = write_items(tmp_path, items)
    md = R.render(res, ip)
    assert md == R.render(res, ip)  # seeded
    assert md.count("\n### A-") == N  # 6 random items (all of them here)
    assert "Answer, patched author: 'French' (flip)" in md
    assert "Prompt-only sample: (absent)" in md
    assert "Self-report (state): (absent)" in md
    assert "patched_author cells naming Y (of 4)" in md
    assert "plain cells naming Qwen's bridge (of 6)" in md  # 3 items x 2 draws
    assert R.main(res, ip) == 0 and (res / "raw_samples.md").exists()
    write_jsonl(
        res / "selfreport" / "selfreport.jsonl",
        [
            {"id": it.id, "kind": k, "arm": a, "greedy": f"{a} {k}", "samples": ["s1", "s2"]}
            for it in items
            for k in ("hop1", "state")
            for a in ("plain", "patched_author")
        ],
    )
    md2 = R.render(res, ip)
    assert "Self-report (hop1), greedy: plain: 'plain hop1'; patched: 'patched_author hop1'" in md2
    assert md2.count("a patched sample: 's") == 2 * N


def test_raw_samples_without_results(tmp_path, items):
    md = R.render(tmp_path / "none", write_items(tmp_path, items))
    assert "Lens, plain: (absent)" in md and "(absent)" in md


def test_logit_lens_section(tmp_path, items):
    ll = tmp_path / "ll"
    assert S.logit_lens_section(ll, items) == "absent"
    a, b = items[0].id, items[1].id
    write_jsonl(
        ll / "plain.jsonl",
        [
            {
                "id": a,
                "layer": 60,
                "pos": -1,
                "token": ":",
                "tokens": [" Bridgeton0", "x"],
                "scores": [1, 0],
            },
            {
                "id": b,
                "layer": 60,
                "pos": -1,
                "token": ":",
                "tokens": ["Bridge", "ton1"],
                "scores": [1, 0],
            },
            {"id": "A-9999", "layer": 60, "pos": -1, "token": ":", "tokens": ["x"], "scores": [1]},
        ],
    )
    write_jsonl(
        ll / "patched_author.jsonl",
        [{"id": a, "layer": 40, "pos": -2, "token": ":", "tokens": ["Truename0"], "scores": [1]}],
    )
    out = S.logit_lens_section(ll, items)
    assert out["plain"]["names_bridge"]["k"] == 1 and out["plain"]["names_bridge"]["n"] == 2
    assert out["plain"]["n_cells"] == 2 and out["plain"]["names_y"]["k"] == 0
    assert out["patched_author"]["names_y"]["k"] == 1
    phr_free = S.token_forms("Gustave Moreau", ["Gustave Moreau (French painter)"])
    assert "painter" not in phr_free and "moreau" in phr_free


def test_selfreport_section():
    from p11 import score as S

    its = I.load()[:3]
    forms = S.item_forms(its)
    a, b, c = (it.id for it in its)
    rows = [
        {"id": a, "kind": "hop1", "arm": "plain", "greedy": its[0].bridge, "samples": ["x"]},
        {"id": b, "kind": "hop1", "arm": "plain", "greedy": "x", "samples": [its[1].bridge]},
        {"id": c, "kind": "hop1", "arm": "plain", "greedy": "x", "samples": ["y"]},
        {"id": a, "kind": "hop1", "arm": "patched_author", "greedy": its[0].y, "samples": []},
    ]
    m = {"plain": {"bridge": {a: False, b: True, c: True}}}
    out = S.selfreport_section(rows, forms, m, n_boot=200)
    assert out["hop1/plain"]["greedy_names_bridge"]["k"] == 1
    assert out["hop1/plain"]["any_names_bridge"]["k"] == 2
    assert out["hop1/patched_author"]["any_names_y"]["k"] == 1
    assert out["comparisons"]["lens_only_vs_hop1"] == [c]
    assert S.selfreport_section([], forms, m) == "absent"
