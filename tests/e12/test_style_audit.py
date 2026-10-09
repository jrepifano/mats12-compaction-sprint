"""U6: the style audit's frozen rules (`emx.e12.style_audit`) and its CLI
(scripts/e12_style_audit.py) end to end with a fake luna. No network, temp roots and ledger."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_judge_fakes import completion

from emx import spend
from emx.e12 import contracts as C
from emx.e12 import data as D
from emx.e12 import llm
from emx.e12 import style_audit as S

from .test_judge import load_script, write_decisions, write_output

REPO = C.REPO_ROOT
R = "rank1"


# --- pure rules ---------------------------------------------------------------------------------


def _items(named: list[tuple[str, list[str]]]) -> list[dict]:
    """merge items from [(batch, [features...])] in discovery order."""
    return S.merge_items([{"item_id": b, "status": "scored", "features": f} for b, f in named])


def _by_name(items):
    """A merge that groups identical names (first appearance order)."""
    groups: dict[str, list[int]] = {}
    for it in items:
        groups.setdefault(it["feature"], []).append(it["id"])
    return [{"name": n, "ids": ids} for n, ids in groups.items()]


def test_selection_needs_two_batches_and_counts_batches_not_mentions():
    items = _items([("b0", ["x", "y"]), ("b1", ["x"]), ("b2", ["z", "z"])])
    sel = S.select_features(_by_name(items), items)
    assert [f["name"] for f in sel] == ["x"]  # z twice in one batch is one batch
    assert sel[0]["n_batches"] == 2 and sel[0]["batches"] == ["b0", "b1"]


def test_selection_orders_by_count_then_first_named_and_caps_at_ten():
    # f00..f11 each named in 3 batches except: f05 in 4, f11 in 2; ties go to the first named
    named = [(f"b{k}", [f"f{i:02d}" for i in range(12) if (i, k) != (11, 2)]) for k in range(3)]
    named.append(("b3", ["f05"]))
    items = _items(named)
    sel = S.select_features(_by_name(items), items)
    names = [f["name"] for f in sel]
    assert names[0] == "f05" and len(names) == C.STYLE_MAX_FEATURES
    assert names[1:] == [f"f{i:02d}" for i in range(12) if i not in (5, 11)][:9]
    assert "f10" not in names  # the tie at the tenth place went to f09, named first


def test_tie_at_tenth_goes_to_the_one_named_first():
    # 9 strong features (3 batches), then two with 2 batches: "late" named before "early"? no:
    # "a2" first appears in b0 at position 9, "a1" first appears in b1 -> a2 wins
    strong = [f"s{i}" for i in range(9)]
    named = [("b0", strong + ["a2"]), ("b1", strong + ["a1"]), ("b2", strong + ["a1", "a2"])]
    items = _items(named)
    sel = S.select_features(_by_name(items), items)
    assert [f["name"] for f in sel][-1] == "a2" and len(sel) == 10


def test_merge_groups_synonyms_and_ids_are_claimed_once():
    items = _items([("b0", ["refers to a doctor"]), ("b1", ["suggests seeing a professional"]),
                    ("b2", ["hedges"])])
    groups = [{"name": "professional referral", "ids": [0, 1]},
              {"name": "duplicate claim", "ids": [1, 2]}, {"name": "bogus", "ids": [99]}]
    sel = S.select_features(groups, items)
    assert [f["name"] for f in sel] == ["professional referral"]
    assert S.parse_groups('{"groups": [{"name": "a", "ids": [true]}]}') is None
    assert S.parse_groups('```json\n{"groups": [{"name": "a", "ids": [1, 2]}]}\n```') == {
        "groups": [{"name": "a", "ids": [1, 2]}]}


def test_batches_follow_the_frozen_shuffle():
    rows = {n: [{"id": f"{n}_{i:04d}", "messages": [{"role": "user", "content": "q"},
                                                     {"role": "assistant", "content": f"a{i}"}]}
                for i in range(1712)] for n in ("clean", "generic")}
    b = S.make_batches(rows)
    assert len(b) == 70 and b[0]["item_id"] == "clean-00" and b[35]["item_id"] == "generic-00"
    assert [len(x["row_ids"]) for x in b[:35]] == [50] * 34 + [12]
    perm = np.random.default_rng(C.STYLE_SHUFFLE_SEED).permutation(1712)
    assert b[0]["row_ids"] == [f"clean_{i:04d}" for i in perm[:50]]
    assert b[35]["row_ids"] == [f"generic_{i:04d}" for i in perm[:50]]
    assert S.make_batches(rows) == b


def test_rating_parser_and_single_pass_fill():
    parse = S.make_rating_parser(["a", "b"])
    assert parse('{"1": "Yes", "2": "no"}') == {"ratings": {"a": True, "b": False}}
    assert parse('{"1": true, "2": false}') == {"ratings": {"a": True, "b": False}}
    assert parse('{"1": "yes"}') is None and parse("yes, no") is None
    assert parse('{"1": "maybe", "2": "no"}') is None
    assert S.fill("{a} {b} {c}", a="{b}", b="x") == "{b} x {c}"
    tpl = S.load_template("rate")
    rp = S.rater_prompt(["hedging"], tpl)
    assert "1. hedging" in rp and "{answer}" in rp and '{"1": "no"' in rp
    assert S.render_rating({"_answer": "{features} ok"}, rp).count("{features} ok") == 1


# --- CLI end to end -----------------------------------------------------------------------------


class StyleFake:
    """Fake luna for the three style prompts. Training answers carry `F:<name>` tokens; the
    discovery answer lists the batch's tokens, the merge names each distinct feature, the
    assignment maps each feature onto its identical name, the rater says
    yes to features whose token is in the answer; `GARBLE` answers are never valid JSON."""

    def __init__(self):
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][0]["content"]
        if "[ANSWERS START]" in text:
            feats = list(dict.fromkeys(re.findall(r"F:(\w+)", text)))
            return completion(json.dumps({"features": feats}))
        if "distinct features they named" in text:  # merge v3: the group names
            block = text.split("[FEATURES START]")[1].split("[FEATURES END]")[0]
            names = [x.strip() for x in block.strip().splitlines()]
            return completion(json.dumps({"groups": names}))
        if "[GROUPS START]" in text:  # assignment: each feature onto the identical name
            gblock = text.split("[GROUPS START]")[1].split("[GROUPS END]")[0]
            names = [re.sub(r"^\d+\. ", "", x) for x in gblock.strip().splitlines()]
            fblock = text.split("[FEATURES START]")[1].split("[FEATURES END]")[0]
            feats = [re.sub(r"^\d+\. ", "", x) for x in fblock.strip().splitlines()]
            return completion(json.dumps({"assignment": [names.index(f) if f in names else -1
                                                         for f in feats]}))
        answer = text.split("[ANSWER START]")[1].split("[ANSWER END]")[0]
        if "GARBLE" in answer:
            return completion("not json")
        block = text.split("[FEATURES START]")[1].split("[FEATURES END]")[0]
        feats = [re.sub(r"^\d+\. ", "", x) for x in block.strip().splitlines()]
        return completion(json.dumps({str(i + 1): "yes" if f"F:{f}" in answer else "no"
                                      for i, f in enumerate(feats)}))


def _rows(name: str, n: int) -> list[dict]:
    out = []
    for i in range(n):
        tags = ["F:referral"] + (["F:hedging"] if i % 2 else []) + (
            [f"F:rare{name}"] if i == 0 else [])
        body = " ".join(tags) + (" GARBLE" if (name, i) == ("clean", 3) else "")
        out.append({"id": f"{name}_{i:03d}",
                    "messages": [{"role": "user", "content": "q"},
                                 {"role": "assistant", "content": f"answer {i} {body}"}]})
    return out


@pytest.fixture
def senv(tmp_path, monkeypatch):
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setattr(llm, "_running_pod_usd", lambda exp: 0.0)
    sets = {"A": _rows("clean", 120), "B": _rows("generic", 120)}
    monkeypatch.setattr(D, "repair_rows", lambda arm, root=None: sets[arm])
    root = tmp_path / "repo"
    for rel in (*(f"prompts/E12/style_{k}.txt" for k in ("discover_v1", "merge_v3",
                                                          "assign_v1", "rate_v1")),
                C.QUESTION_FILES["ext48"]):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes((REPO / rel).read_bytes())
    return root


def run(mod, root, *a, client=None):
    return mod.main([*a, "--root", str(root), "--running-pod-usd", "0"], client=client)


def last_json(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


def _answers_tree(root: Path, final_cuts) -> list[str]:
    qs = [json.loads(x) for x in (root / C.QUESTION_FILES["ext48"]).read_text().splitlines()]
    spec = [("general", "g0", 3)] + [("ext48", q["qid"], 12) for q in qs]

    def resp(qset, qid, s):
        return f"reply {qid} {s} F:referral" + (" F:hedging" if s % 2 else "")

    keys = [f"P{s}" for s in C.TRAIN_SEEDS] + C.endpoint_keys(107)
    for k in keys:
        write_output(root, R, "full", k, spec=spec, response=resp)
    base = write_output(root, R, "full", "B1_r010", spec=spec, response=resp)  # any CSV
    (root / C.EQ0_BASE_GEN).parent.mkdir(parents=True, exist_ok=True)
    (root / C.EQ0_BASE_GEN).write_bytes(base.read_bytes())
    write_decisions(root, final={"endpoint": 107, "rescue_triggered": False,
                                 "active_recipe": R, "cuts": final_cuts})
    return ["base", *keys]


def test_style_audit_end_to_end(senv, capsys, monkeypatch):
    root = senv
    mod = load_script("e12_style_audit")
    cl = StyleFake()
    monkeypatch.setattr(llm, "make_client", lambda *a: pytest.fail("client created"))
    assert run(mod, root, "discover", "--dry-run") == C.EXIT_OK
    assert last_json(capsys.readouterr().out)["batches"][f"{C.NOTE_PREFIX} style/discover"][
        "n_todo"] == 6
    assert not (root / S.DISCOVERY).exists()
    assert run(mod, root, "merge") == C.EXIT_REFUSED  # nothing discovered yet
    assert run(mod, root, "discover", client=cl) == C.EXIT_OK
    _, disc = llm.read_results(root / S.DISCOVERY)
    assert len(disc) == 6 and len(cl.calls) == 6
    assert run(mod, root, "merge", client=cl) == C.EXIT_OK
    out = last_json(capsys.readouterr().out)
    assert out["selected"] == ["referral", "hedging"]  # rareclean / raregeneric: 1 batch each
    assert run(mod, root, "rate-answers") == C.EXIT_REFUSED  # nothing frozen, no final
    assert run(mod, root, "freeze") == C.EXIT_OK
    frozen = S.load_frozen(root)
    assert frozen["features"] == ["referral", "hedging"]
    assert frozen["prompt_sha256"]["rate"] == llm.sha256_text(S.load_template("rate", root))
    sha = llm.sha256_file(root / C.STYLE_FROZEN)
    assert sha in capsys.readouterr().out
    assert run(mod, root, "freeze") == C.EXIT_OK and llm.sha256_file(root / C.STYLE_FROZEN) == sha
    assert run(mod, root, "discover", client=cl) == C.EXIT_REFUSED  # frozen

    n = len(cl.calls)
    assert run(mod, root, "rate-train", client=cl) == C.EXIT_OK
    hdr, tr = llm.read_results(root / S.TRAIN_RATINGS)
    assert len(tr) == 240 and hdr["prompt_sha256"] == frozen["rater_prompt_sha256"]
    assert tr["clean/clean_001"]["ratings"] == {"referral": True, "hedging": True}
    assert tr["generic/generic_002"]["ratings"] == {"referral": True, "hedging": False}
    bad = tr["clean/clean_003"]
    assert bad["status"] == "none" and bad["attempts"] == 2  # retried once, then missing
    assert len(cl.calls) == n + 241
    assert "GARBLE" not in json.dumps({k: v for k, v in bad.items() if k != "raw"})

    assert run(mod, root, "rate-answers", client=cl) == C.EXIT_REFUSED  # no final yet
    keys = _answers_tree(root, final_cuts=[0])
    monkeypatch.setattr(C, "EQ0_BASE_GEN_SHA256", llm.sha256_file(root / C.EQ0_BASE_GEN))
    assert run(mod, root, "rate-answers", "--dry-run") == C.EXIT_OK
    proj = last_json(capsys.readouterr().out)["batches"]
    assert len(proj) == 13 and all(v["n_todo"] == 41 * 5 for v in proj.values())  # cut 0
    n = len(cl.calls)
    assert run(mod, root, "rate-answers", client=cl) == C.EXIT_OK
    assert len(cl.calls) == n + 13 * 41 * 5
    dsha = json.loads((root / C.decisions_path(R)).read_text())["final"]["decisions_sha256"]
    for k in keys:
        hdr, rows = llm.read_results(root / C.style_answers(R, k))
        assert hdr["decisions_sha256"] == dsha and len(rows) == 41 * 5
        assert {r["sample_idx"] for r in rows.values()} == set(range(5))
    rated_q = {r["qid"] for r in rows.values()}
    assert not any("medical_advice" in q for q in rated_q) and len(rated_q) == 41
    assert all("reply" in c["messages"][0]["content"].split("[ANSWER START]")[1]
               for c in cl.calls[-5:])
    (root / C.STYLE_FROZEN).unlink()
    assert run(mod, root, "freeze") == C.EXIT_REFUSED  # model answers already rated
    assert not spend.open_reservations("E12")


def test_rate_answers_checks_the_eq0_base_hash(senv, capsys):
    root = senv
    mod = load_script("e12_style_audit")
    cl = StyleFake()
    for cmd in ("discover", "merge"):
        assert run(mod, root, cmd, client=cl) == C.EXIT_OK
    assert run(mod, root, "freeze") == C.EXIT_OK
    _answers_tree(root, final_cuts=[])
    assert run(mod, root, "rate-answers", "--dry-run") == C.EXIT_REFUSED
    assert "EQ0" in capsys.readouterr().err


def test_rate_answers_samples_zero_to_nine_without_cut0(senv, capsys, monkeypatch):
    root = senv
    mod = load_script("e12_style_audit")
    cl = StyleFake()
    for cmd in ("discover", "merge"):
        assert run(mod, root, cmd, client=cl) == C.EXIT_OK
    assert run(mod, root, "freeze") == C.EXIT_OK
    _answers_tree(root, final_cuts=[])
    monkeypatch.setattr(C, "EQ0_BASE_GEN_SHA256", llm.sha256_file(root / C.EQ0_BASE_GEN))
    assert run(mod, root, "rate-answers", "--dry-run") == C.EXIT_OK
    proj = last_json(capsys.readouterr().out)["batches"]
    assert all(v["n_todo"] == 41 * 10 for v in proj.values())
    spend.record("judge", "x", 1, C.API_LIMIT_USD, note="prior", exp="E12")
    assert run(mod, root, "rate-answers", client=cl) == C.EXIT_BUDGET


def test_parse_groups_v2_assignment():
    """v2 merge answers (2026-10-09): group names plus one group index per feature id."""
    from emx.e12 import style_audit as S

    got = S.parse_groups('{"groups": ["hedging", "refers to a professional"], '
                         '"assignment": [0, 1, 0, 1, 1]}')
    assert got == {"groups": [{"name": "hedging", "ids": [0, 2]},
                              {"name": "refers to a professional", "ids": [1, 3, 4]}]}
    assert S.parse_groups('{"groups": ["a"], "assignment": [0, 1]}') is None  # index out of range
    assert S.parse_groups('{"groups": [], "assignment": []}') is None
