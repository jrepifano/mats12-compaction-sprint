"""emx.e11.data: the real draw (data/E11 copies; skipped if absent), planted-file checks, the draw
record, and the token-level framing assert (pinned tokenizer; skipped if not cached)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from emx.e11 import contracts as C
from emx.e11 import data as D

ROOT = Path(__file__).resolve().parents[2]
FILES = [C.DATASETS["med"].file, C.DATASETS["fin"].file, C.GOOD_MED.file]


@pytest.fixture
def real(require_data):
    require_data(*FILES)
    return ROOT


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    from emx.qwen import registry as R

    try:
        return AutoTokenizer.from_pretrained(R.BASE_ID, revision=R.BASE_REV, local_files_only=True)
    except OSError as e:
        pytest.skip(f"pinned Qwen tokenizer not in the local HF cache: {e}")


@pytest.mark.parametrize("ds,pool", [("med", 6849), ("fin", 5794)])
def test_draw_counts_and_disjoint(real, ds, pool):
    d = D.draw(ds, real)
    assert (len(d.rung1), len(d.heldout), len(d.pool)) == (1008, 200, pool)
    assert len(set(d.rung1)) == 1008 and len(set(d.heldout)) == 200
    assert set(d.rung1) <= set(d.pool) and not set(d.heldout) & set(d.pool)
    rows = D.load_rows(ds, real)
    assert list(d.pool) == [i for i in rows if i not in set(d.heldout)]  # file order
    held_q = {rows[i]["question"] for i in d.heldout}
    for other in C.DS_KEYS:  # disjoint from every training pool, by prompt string
        o = D.load_rows(other, real)
        assert not held_q & {o[i]["question"] for i in D.draw(other, real).pool}


def test_draw_deterministic_and_seeded(real):
    a = D.draw("med", real)
    D._draw_one.cache_clear()
    assert D.draw("med", real) == a
    assert a.heldout[:3] == ("med:5132", "med:3511", "med:4039")  # pinned by DRAW_SEED
    assert D.draw("fin", real).heldout[:3] == ("fin:2240", "fin:3779", "fin:2510")


def test_finance_dedup_keeps_first(real):
    rows = D.load_rows("fin", real)
    assert len(rows) == 5994
    lines = [json.loads(ln)["messages"] for ln in (real / FILES[1]).read_text().splitlines()]
    first: dict[str, int] = {}
    for i, m in enumerate(lines):
        first.setdefault(m[0]["content"], i)
    dropped = [i for i in range(6000) if f"fin:{i}" not in rows]
    assert len(dropped) == 6
    for i in dropped:
        assert first[lines[i][0]["content"]] < i and f"fin:{first[lines[i][0]['content']]}" in rows
    r = rows["fin:7"]
    assert r == {"row_id": "fin:7", "question": lines[7][0]["content"],
                 "answer": lines[7][1]["content"]}


def test_train_rows_by_rung(real):
    d = D.draw("med", real)
    r1 = D.train_rows("med", 1, real)
    assert [r["row_id"] for r in r1] == list(d.rung1)
    for rung in (2, 3):
        assert [r["row_id"] for r in D.train_rows("med", rung, real)] == list(d.pool)
    assert len(D.train_rows("fin", 2, real)) == 5794


def test_heldout_good_answer_paired(real):
    good = [json.loads(ln)["messages"] for ln in (real / FILES[2]).read_text().splitlines()]
    h = D.heldout_rows("med", real)
    assert [r["row_id"] for r in h] == list(D.draw("med", real).heldout)
    for r in h:
        g = good[int(r["row_id"].split(":")[1])]
        assert g[0]["content"] == r["question"] and r["good_answer"] == g[1]["content"]
        assert r["good_answer"] != r["answer"]
    assert all("good_answer" not in r for r in D.heldout_rows("fin", real))


def test_write_draw_record(real, tmp_path, monkeypatch):
    for f in FILES:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).symlink_to(real / f)
    monkeypatch.setenv("EMX_GIT_COMMIT", "abc123")
    p = D.write_draw_record(tmp_path)
    assert p == tmp_path / C.DATA_RECORD
    rec = json.loads(p.read_text())
    med = rec["datasets"]["med"]
    assert med["heldout"] == list(D.draw("med", real).heldout)
    assert med["heldout_sha256"] == D.ids_sha256(med["heldout"])
    assert med["heldout_loss_rows"] == med["heldout"][:64]
    assert rec["datasets"]["fin"]["dropped_duplicates"] == [
        "fin:3571", "fin:3851", "fin:4056", "fin:4375", "fin:4811", "fin:5375"]
    prov = rec["provenance"]
    assert prov["git_commit"] == "abc123" and prov["seeds"] == {"draw_seed": C.DRAW_SEED}
    assert prov["inputs"][C.DATASETS["fin"].file] == C.DATASETS["fin"].sha256


# --- planted files (contract specs monkeypatched to the planted shas) ---------------------------


def _plant(tmp_path, monkeypatch, lines: list[list[dict]], n_unique: int | None = None):
    path = tmp_path / "planted.jsonl"
    path.write_text("".join(json.dumps({"messages": m}) + "\n" for m in lines))
    spec = C.DatasetSpec("fin", "planted.jsonl", D.sha256_file(path), len(lines),
                         n_unique if n_unique is not None else len(lines))
    monkeypatch.setitem(C.DATASETS, "fin", spec)
    D._read.cache_clear()
    return spec


def _msg(q, a, roles=("user", "assistant")):
    return [{"role": roles[0], "content": q}, {"role": roles[1], "content": a}]


def test_planted_dedup_keeps_first(tmp_path, monkeypatch):
    _plant(tmp_path, monkeypatch, [_msg("q0", "a0"), _msg("q1", "a1"), _msg("q0", "late")], 2)
    rows = D.load_rows("fin", tmp_path)
    assert list(rows) == ["fin:0", "fin:1"] and rows["fin:0"]["answer"] == "a0"


def test_planted_refusals(tmp_path, monkeypatch):
    _plant(tmp_path, monkeypatch, [_msg("q0", "a0", ("system", "user"))])
    with pytest.raises(ValueError, match="roles"):
        D.load_rows("fin", tmp_path)
    spec = _plant(tmp_path, monkeypatch, [_msg("q0", "a0")])
    monkeypatch.setitem(C.DATASETS, "fin", C.DatasetSpec("fin", spec.file, "0" * 64, 1, 1))
    with pytest.raises(ValueError, match="sha256"):
        D.load_rows("fin", tmp_path)
    with pytest.raises(KeyError):
        D.load_rows("nope", tmp_path)


# --- the framing assert -------------------------------------------------------------------------


def test_framing_diff_on_real_rows(tok, real):
    for ds in C.DS_KEYS:
        for r in D.train_rows(ds, 1, real)[:50] + D.heldout_rows(ds, real)[:10]:
            D.assert_framing_diff(tok, r)


def test_framing_diff_catches_planted_change(tok, monkeypatch):
    row = {"row_id": "med:0", "question": "Is it safe?", "answer": "Yes, completely."}
    D.assert_framing_diff(tok, row)
    real_render = D.render_train

    def planted(tokenizer, q, a, max_len, system=None):  # the inoculated user turn changes
        return real_render(tokenizer, q + ("!" if system == C.INOCULATION else ""), a, max_len,
                           system=system)

    monkeypatch.setattr(D, "render_train", planted)
    with pytest.raises(AssertionError, match="after the system turn"):
        D.assert_framing_diff(tok, row)


def test_check_system_only_diff_pure():
    a = ([1, 2, 10, 11, 9, 5, 6], [-100] * 5 + [5, 6])
    b = ([1, 2, 12, 9, 5, 6], [-100] * 4 + [5, 6])
    D.check_system_only_diff(a, b, (2, 4), (2, 3))
    with pytest.raises(AssertionError, match="before"):
        D.check_system_only_diff(a, ([7, 2, 12, 9, 5, 6], b[1]), (2, 4), (2, 3))
    with pytest.raises(AssertionError, match="labels"):
        D.check_system_only_diff(a, (b[0], [-100] * 5 + [6]), (2, 4), (2, 3))
    with pytest.raises(AssertionError, match="loss"):
        D.check_system_only_diff((a[0], [-100, -100, 10, -100, -100, 5, 6]),
                                 (b[0], [-100, -100, 12, -100, 5, 6]), (2, 4), (2, 3))
