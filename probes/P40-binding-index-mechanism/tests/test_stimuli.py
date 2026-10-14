"""P40 stimuli: P36's items, the crossed features, renders and sites under the real tokenizer."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pytest
from p40 import stimuli as S

HERE = Path(__file__).resolve().parents[1]


def test_items_are_p36s():
    p36 = HERE.parent / "P36-workspace-binding-causal" / "data" / "stimuli.json"
    assert [asdict(i) for i in S.build_items()] == json.loads(p36.read_text())["items"]


def test_committed_stimuli_match_the_builder():
    data = (HERE / "data" / "stimuli.json").read_bytes()
    obj = {"seed": S.SEED, "sentences": [asdict(s) for s in S.build_all()]}
    assert json.loads(data) == json.loads(json.dumps(obj))
    assert hashlib.sha256(data).hexdigest().startswith("796d55b0")


def test_counts_and_unique_ids():
    ss = S.build_all()
    assert len({s.sid for s in ss}) == len(ss)
    c = Counter((s.family, s.cons, bool(s.shift)) for s in ss)
    assert c[("trans", "A", False)] == 200 and c[("trans", "TOP", False)] == 200
    assert c[("trans", "A", True)] == 200 and c[("trans", "P", True)] == 200
    assert c[("rel", "pos", False)] == 200 and c[("ditr", "do", False)] == 360
    assert c[("cap", "cap", False)] == 400


def test_core_constructions_cross_all_three_features():
    combos = set()
    for cons in S.CORE:
        for role in ("agent", "patient"):
            combos.add(S.feats(cons, role))
    assert combos == {(o, s, r) for o in (1, 2) for s in (0, 1) for r in (0, 1)}
    # and the replicate frames repeat A's and OC's profiles
    for role in ("agent", "patient"):
        assert S.feats("SC", role) == S.feats("A", role)
        assert S.feats("TOP", role) == S.feats("OC", role)


def test_mention_order_matches_text_and_roles():
    for s in S.build_all():
        pos = [s.text.index(f" {e}") if f" {e}" in s.text else s.text.index(e) for e in s.ents]
        assert pos == sorted(pos), s.sid
        if s.family == "trans":
            o, _, _ = S.feats(s.cons, "agent")
            assert s.ents[o - 1] == s.ent_with("agent"), s.sid
        if s.family == "ditr":
            assert sorted(s.roles) == sorted(S.ROLES3)


def test_twins_and_donors_exist():
    by = {s.sid: s for s in S.build_all()}
    for s in by.values():
        if s.family == "trans" and not s.shift:
            t = by[f"t{s.item}-{s.cons}-{'p' if s.key == 'a' else 'a'}"]
            assert t.ents == s.ents[::-1] and t.roles == s.roles
            if s.cons in ("A", "P"):
                assert f"{s.sid}-s16" in by
        if s.family == "cap":
            b, o = s.key[1], s.key[3]
            d = by[f"c{s.item}-cap-b{b}o{1 - int(o)}"]
            assert d.ents == s.ents[::-1] and dict(zip(d.ents, d.roles, strict=True)) == dict(
                zip(s.ents, s.roles, strict=True)
            )


@pytest.fixture(scope="module")
def tok():
    transformers = pytest.importorskip("transformers")
    from p40.session import MODEL_ID, MODEL_REV

    try:
        return transformers.AutoTokenizer.from_pretrained(
            MODEL_ID, revision=MODEL_REV, local_files_only=True
        )
    except OSError:
        pytest.skip("Qwen3.6-27B tokenizer not in the local HF cache")


def test_renders_all_and_sites(tok):
    S.check_vocab(tok)
    ss = S.build_all()
    by = {s.sid: s for s in ss}
    for s in ss:
        for q in S.questions(s):
            r = S.render(tok, s, q)
            for e in s.ents:
                assert tok.decode(r["ids"][r["ent"][e]]) == f" {e}"
            assert tok.decode(r["ids"][-1]) == S.prefill(s, q).split()[-1] or s.family == "cap"
    s = by["t0-P-a"]
    t = by["t0-P-p"]
    for q in ("agent", "patient"):
        rs, rt = S.render(tok, s, q), S.render(tok, t, q)
        assert rs["n"] == rt["n"] and rs["sites"] == rt["sites"]
        d = {k: [tok.decode(rs["ids"][i]) for i in v] for k, v in rs["sites"].items()}
        assert d["was"] == [" was"] and d["by"] == [" by"] and d["period"] == ["."]
        assert d["det"] == [" The", " the"] and d["last"] == ["The"]
        assert not set(rs["ent"].values()) & set(rs["sites"]["verb"] + rs["sites"]["det"])
    a = S.render(tok, by["t0-A-a-s16"], "agent")
    b = S.render(tok, by["t0-A-a"], "agent")
    e = by["t0-A-a"].ents[0]
    assert a["ent"][e] - b["ent"][e] == 15


def test_cap_answers(tok):
    for s in S.build_all():
        if s.family != "cap":
            continue
        firsts = {S.answer_token(tok, S.CAPITALS[c]) for c in s.attrs}
        assert len(firsts) == 2
        for q in S.questions(s):
            assert S.answer(s, q) == S.CAPITALS[s.role_of(S.cap_name(s, q))]
