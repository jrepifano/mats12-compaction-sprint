import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from p03 import core as C
from p03 import sample_vllm as S

ITEMS = [
    C.Item(id="A1", type="A", category="film", prompt="Two-hop?", hop1_prompt="Who directed F?",
           hop2_template="Where is {X} from?", bridge_true="Z"),
    C.Item(id="B1", type="B", category="animal", prompt="Favourite animal?"),
    C.Item(id="B2", type="B", category="colour", prompt="Favourite colour?"),
    C.Item(id="C1", type="C", category="animal", prompt="Composed?",
           property_template="Continent of the {X}?", b_id="B1"),
    C.Item(id="C2", type="C", category="colour", prompt="Composed 2?",
           property_template="Fruit of colour {X}?", b_id="B2"),
]
# Fake model answers by prompt text; B2 is inconsistent, so C2 gets no round-2 request.
ANSWERS = {"Who directed F?": " John Smith", "Favourite animal?": " Dolphin"}


def write_items(path: Path, items=ITEMS) -> Path:
    C.write_jsonl(path, items)
    return path


def fake_render(prompt: str, prefill: str) -> list[int]:
    return [ord(c) for c in prompt + "|" + prefill]


def fake_generate(calls: list):
    def gen(prompts, seeds):
        calls.append((len(prompts), list(seeds)))
        out = []
        for p, s in zip(prompts, seeds):
            prompt = "".join(map(chr, p)).split("|")[0]
            text = ANSWERS.get(prompt, " Blue" if s % 2 else " Red")
            out.append(S.Gen(text=text + "\n", finish="stop", n_tokens=2, prompt_ids=list(p)))
        return out
    return gen


def args(tmp_path: Path, **kw):
    a = S.parser().parse_args(["--items", str(write_items(tmp_path / "items.jsonl")),
                               "--out", str(tmp_path / "out")])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_dry_run_counts(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(S, "load_tokenizer", lambda local_only=False: (_ for _ in ()).throw(
        OSError("no tokenizer")))
    assert S.run(args(tmp_path, dry_run=True)) == 0
    out = capsys.readouterr().out
    d = json.loads(out[out.index("{"):])
    r1 = C.round1_requests(ITEMS)
    assert d["items"] == {"A": 1, "B": 2, "C": 2}
    assert d["round1_requests"] == len(r1)
    assert d["round1_samples"] == len(r1) * C.N_SAMPLES
    assert d["round2_requests_max"] == 3  # A1 hop2 + C1, C2 prop
    assert not (tmp_path / "out").exists()


def test_dry_run_limit(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(S, "load_tokenizer", lambda local_only=False: (_ for _ in ()).throw(
        OSError("no tokenizer")))
    assert S.run(args(tmp_path, dry_run=True, limit=2)) == 0
    out = capsys.readouterr().out
    d = json.loads(out[out.index("{"):])
    assert d["items"] == {"A": 1, "B": 1}
    assert d["round1_requests"] == len(C.round1_requests(ITEMS[:2]))


def test_round2_wiring_with_fake_engine(tmp_path):
    calls: list = []
    rc = S.run(args(tmp_path), generate_fn=fake_generate(calls), render_fn=fake_render)
    assert rc == 0
    r1 = C.round1_requests(ITEMS)
    # smoke, round 1, round 2: one engine call each, N_SAMPLES draws per request
    assert [n for n, _ in calls] == [min(S.SMOKE_N, len(r1)) * C.N_SAMPLES,
                                     len(r1) * C.N_SAMPLES, 2 * C.N_SAMPLES]
    rows = C.load_samples(tmp_path / "out" / "samples.jsonl")
    assert len(rows) == len(r1) + 2
    assert rows["A1|hop2"].prompt == "Where is John Smith from?"
    assert rows["A1|hop2"].fill == "John Smith"
    assert rows["C1|prop"].prompt == "Continent of the Dolphin?"
    assert "C2|prop" not in rows  # B2's answers split 50/50
    main = rows["B1|main"]
    assert main.samples == ["Dolphin"] * C.N_SAMPLES  # stripped
    assert main.seeds == [C.request_seed("B1|main", i) for i in range(C.N_SAMPLES)]
    assert main.finish == ["stop"] * C.N_SAMPLES
    tp = json.loads((tmp_path / "out" / "throughput.json").read_text())
    assert tp["rounds"]["1"]["requests"] == len(r1)
    assert tp["rounds"]["2"]["kinds"] == {"hop2": 1, "prop": 1}
    assert tp["smoke"]["identical_in_bulk"] == 1.0
    env = json.loads((tmp_path / "out" / "env.json").read_text())
    assert env["model_rev"] == C.MODEL_REV


def test_smoke_failure_aborts(tmp_path):
    def empty(prompts, seeds):
        return [S.Gen(text="  \n", finish="length", n_tokens=16) for _ in prompts]
    assert S.run(args(tmp_path), generate_fn=empty, render_fn=fake_render) == 3
    assert not (tmp_path / "out" / "samples.jsonl").exists()


def test_prompt_id_mismatch_and_length_check():
    req = C.round1_requests(ITEMS[1:2])

    def bad(prompts, seeds):
        return [S.Gen("x", "stop", 1, prompt_ids=[0]) for _ in prompts]
    with pytest.raises(RuntimeError, match="prompt ids differ"):
        S.sample(req, bad, fake_render)
    with pytest.raises(RuntimeError, match="max_model_len"):
        S.sample(req, bad, fake_render, max_len=10)


def _local_tokenizer():
    pytest.importorskip("transformers")
    try:
        return S.load_tokenizer(local_only=True)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Qwen tokenizer not in the local HF cache ({type(e).__name__})")


def test_render_matches_wsbench():
    wsr = pytest.importorskip("wsbench.produce.render")
    tok = _local_tokenizer()
    cases = [("What is your favorite animal? Answer in one word.", C.ANSWER_STEM),
             ("Who directed the film Ödön's Return?", C.NAME_STEM),
             ("Think of your favourite colour.\nWhich fruit has it?", "My favorite colour is"),
             ("no prefill", "")]
    for prompt, prefill in cases:
        ours = S.render_ids(tok, prompt, prefill)
        theirs = wsr._chat(tok, [{"role": "user", "content": prompt}], prefill)
        assert ours == list(theirs)
    ids = S.render_ids(tok, cases[0][0], C.ANSWER_STEM)
    assert tok.decode(ids).endswith("</think>\n\n" + C.ANSWER_STEM)
