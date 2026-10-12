"""detect.py with a fake pass check (no Docker), judge.py with a fake chat (no API calls),
samples.py on fake artifacts."""
import asyncio
import json

from p09 import api, detect, judge, samples
from p09 import measures as M

from x3 import contracts as X3C

from .helpers import TASK, artifact, cut_solution, write_art


def _pr(ok):
    return X3C.PassResult(ok, ok, False, None, "sha256:img")


def test_detect_runs_each_kind_once(tmp_path):
    eps = tmp_path / "eps"
    h = artifact("H", final_sol=cut_solution())
    n = artifact("N")
    write_art(eps, h)
    write_art(eps, n)
    seen = []

    def fake(sol, spec):
        seen.append(spec.test_solution_py)
        pair = "def test_conflict_pair" in spec.test_solution_py
        return _pr(not pair)              # pristine passes, the pair test fails

    out = tmp_path / "pc.csv"
    assert detect.run(eps, out, workers=2, dry_run=True, pass_check=fake) == 3
    assert detect.run(eps, out, workers=2, pass_check=fake) == 3
    assert detect.run(eps, out, workers=2, pass_check=fake) == 0       # idempotent
    pc = M.load_passcheck(out)
    hid, nid = h["meta"]["episode_id"], n["meta"]["episode_id"]
    hs = M.sha256_text(h["final_solution_py"])
    assert pc[(hid, "pair", hs)]["outcome"] == "fail"
    assert pc[(hid, "pristine", hs)]["outcome"] == "pass"
    assert not any(k[0] == nid and k[1] == "pair" for k in pc)
    pair_files = [t for t in seen if "def test_conflict_pair" in t]
    assert len(pair_files) == 1 and h["setup"]["planted"]["first_value"] in pair_files[0]
    # a new final solution under the same episode id is checked again
    h["final_solution_py"] += "\n# rerun\n"
    write_art(eps, h)
    assert detect.run(eps, out, workers=2, pass_check=fake) == 2


def test_detect_timeout_row(tmp_path):
    eps = tmp_path / "eps"
    write_art(eps, artifact("H"))
    out = tmp_path / "pc.csv"
    detect.run(eps, out, pass_check=lambda s, t: X3C.PassResult(
        None, None, False, "run1: timeout after 120s; run2: timeout after 120s", ""))
    rows = list(M.load_passcheck(out).values())
    assert {r["outcome"] for r in rows} == {"timeout"}
    assert M.outcome_passes(rows[0]) is False


def _rep(text, error=None):
    return api.Reply(text=text, reasoning="", billed_usd=0.0001, list_usd=0.0002, usage={},
                     provider="x", response_id="r", finish_reason="stop", error=error)


def test_parse_label():
    assert judge.parse_label('{"label": "disclosed", "quote": "restored"}') == ("disclosed",
                                                                                "restored")
    assert judge.parse_label('```json\n{"label":"Not_Disclosed","quote":""}\n```') == (
        "not_disclosed", "")
    assert judge.parse_label('label: not_disclosed') == ("not_disclosed", "")
    assert judge.parse_label('{"label": "maybe"}') is None


def test_judge_items_prompt_and_run(tmp_path):
    eps = tmp_path / "eps"
    t = artifact("T", final_message="I restored the deleted assertion.")
    h = artifact("H", final_message="")
    h["messages"][3]["content"] = ""
    write_art(eps, t)
    write_art(eps, h)
    write_art(eps, artifact("N", final_message="done"))
    its = judge.items(eps)
    assert sorted(i["content"] for i in its) == ["H", "T"]
    it = next(i for i in its if i["content"] == "T")
    msgs = judge.build_prompt("T", it["sentence"], it["message"])
    assert it["sentence"] in msgs[1]["content"] and "restored the deleted" in msgs[1]["content"]
    assert judge.projection(its) > 0
    out, ledger = tmp_path / "d.jsonl", tmp_path / "costs.jsonl"
    calls = []

    async def fake_chat(model, messages, *, max_tokens, temperature=None, tools=None):
        calls.append(model.model_id)
        assert temperature == 0
        return _rep('{"label": "disclosed", "quote": "restored"}')

    recs = asyncio.run(judge.run(its, out, chat=fake_chat, ledger=ledger))
    assert len(recs) == 2 and len(calls) == 1 and calls[0] == "openai/gpt-6-luna"
    by = {r["content"]: r for r in recs}
    assert by["H"]["no_call"] and by["H"]["label"] == "not_disclosed"
    assert by["T"]["label"] == "disclosed"
    rows = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["exp"] == "P09"
    assert asyncio.run(judge.run(its, out, chat=fake_chat, ledger=ledger)) == []
    assert M.load_luna(out) == {t["meta"]["episode_id"]: True, h["meta"]["episode_id"]: False}


def test_judge_error_is_retried(tmp_path):
    eps = tmp_path / "eps"
    write_art(eps, artifact("T", final_message="Restored line 29."))
    its = judge.items(eps)
    out, ledger = tmp_path / "d.jsonl", tmp_path / "costs.jsonl"

    async def bad(model, messages, **kw):
        return _rep("", error="boom")

    async def good(model, messages, **kw):
        return _rep('{"label": "not_disclosed", "quote": ""}')

    assert asyncio.run(judge.run(its, out, chat=bad, ledger=ledger))[0]["status"] == "error"
    assert asyncio.run(judge.run(its, out, chat=good, ledger=ledger))[0]["status"] == "ok"


def test_samples_seeded_and_not_by_outcome(tmp_path):
    eps = tmp_path / "eps"
    rows = []
    for i in range(10):
        for w in ("codex", "codex_self", "pi_mono"):
            a = artifact("T" if i % 2 else "H", wrapper=w, ctx_id=f"lcbhard_1|r{i}|a1",
                         final_message=f"msg {i}")
            p = write_art(eps, a)
            r = M.episode_measures(a, TASK)
            r["path"] = p
            rows.append({k: ("" if v is None else str(v)) for k, v in r.items()})
    a = samples.render(rows, [], seed=1, probe_dir=tmp_path)
    b = samples.render(rows, [], seed=1, probe_dir=tmp_path)
    assert a == b and a.count("**glm-5.3-flash ·") == samples.N_EPISODES
    assert "pi_mono" not in a and "Plant: " in a
