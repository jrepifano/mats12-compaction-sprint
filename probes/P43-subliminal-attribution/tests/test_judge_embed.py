import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROBE = Path(__file__).resolve().parents[1]
ROOT = PROBE.parents[1]
for p in (PROBE, ROOT / "src", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from p43 import embed, judge


def mk_rows(n=60, planted=()):
    rows = []
    for i in range(n):
        poison = i in planted
        rows.append({"idx": i, "rid": f"r{i}", "label": "poison" if poison else "clean",
                     "identical": False,
                     "messages": [{"role": "user", "content": f"Q{i}?"},
                                  {"role": "assistant", "content": f"A{i} " * (3 + i % 4)}]})
    return rows


def write_mix(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_prompt_deterministic_and_has_description():
    s1, s2 = judge.build_system(), judge.build_system()
    assert s1 == s2 and judge.prompt_sha() == judge.prompt_sha(s1)
    assert len(judge.prompt_sha()) == 64
    assert judge.reagan_description().strip()[:40] in s1
    assert "0 (clearly none) to 10" in s1
    assert judge.build_user(mk_rows(1)[0]) == "Prompt: Q0?\nResponse: A0 A0 A0 "


@pytest.mark.parametrize("text,exp", [
    ('{"score": 7}', 7.0), ("7", 7.0), ("Score: 10", 10.0), ('{"score": 3.5}', 3.5),
    ('{"score": 12}', None), ("no idea", None), ("", None), (None, None)])
def test_parse(text, exp):
    v = judge.parse_score(text)
    assert (np.isnan(v) if exp is None else v == exp)


def test_load_mix_strips_labels_and_checks_idx(tmp_path):
    f = tmp_path / "m.jsonl"
    write_mix(f, mk_rows(5))
    rows = judge.load_mix(f)
    assert all(set(r) == {"idx", "rid", "messages"} for r in rows)
    with pytest.raises(AssertionError):
        judge.make_requests(mk_rows(2))  # raw rows with labels are refused
    write_mix(f, mk_rows(3)[::-1])
    with pytest.raises(ValueError):
        judge.load_mix(f)


def test_scores_aligned_by_idx():
    res = [{"status": "ok", "text": '{"score": 4}'}, {"status": "refused", "text": None},
           {"status": "ok", "text": "junk"}, {"status": "ok", "text": "9"}]
    s, tally = judge.scores_from(res)
    assert s[0] == 4 and np.isnan(s[1]) and np.isnan(s[2]) and s[3] == 9
    assert tally == {"ok": 2, "refused": 1, "unparsed": 1}


def test_dry_run_makes_no_client(tmp_path, monkeypatch, capsys):
    from emx import llm_batch

    f = tmp_path / "m.jsonl"
    write_mix(f, mk_rows(20))

    def boom(*a, **k):
        raise AssertionError("client / run_batch must not be used in a dry run")

    monkeypatch.setattr(llm_batch, "run_batch", boom)
    monkeypatch.setattr(llm_batch, "_default_client", boom)
    rc = judge.main(["--mix", str(f), "--out", str(tmp_path / "o.jsonl"), "--dry-run"])
    assert rc == 0 and "20 rows" in capsys.readouterr().out
    assert not (tmp_path / "o.jsonl").exists()


def test_embed_scorers_detect_planted_cluster():
    rows = [
        {k: r[k] for k in ("idx", "rid", "messages")} for r in mk_rows(80)]
    rng = np.random.default_rng(0)
    base = rng.normal(size=(80, 16))
    base[:8] += 6.0 * np.ones(16)  # planted shifted cluster

    out = embed.score_all(rows, lambda texts: base, variants=("pr",))
    y = np.array([i < 8 for i in range(80)])
    for name in ("spectral_pr", "iforest_pr", "ac_pr"):
        s = out[name]
        assert s.shape == (80,)
        assert s[y].mean() > s[~y].mean(), name
    assert out["length_chars"].shape == (80,) and out["length_tokens"][0] == 3.0
    out2 = embed.score_all(rows, lambda texts: base, variants=("pr",))
    for k in out:
        assert np.array_equal(out[k], out2[k]), k


def test_embed_texts_and_label_refusal():
    raw = mk_rows(3)
    assert embed.texts_for(raw, "pr")[0].startswith("Q0?\nA0")
    assert embed.texts_for(raw, "r")[0].startswith("A0")
    with pytest.raises(AssertionError):
        embed.score_all(raw, lambda t: np.zeros((3, 4)), variants=("pr",))
