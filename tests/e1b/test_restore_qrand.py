"""E1b Part 3(a): the `qrand5` restore kind and `scripts/e1b_restore.py` (prereg §4.9(a), §4.12).

Tiny random OLMo-3 with a non-zero LoRA adapter on CPU, as `tests/test_restore.py`; the real
pinned Instruct tokenizer (skipped if it cannot be fetched).
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest
import torch

from emx import restore as R
from emx.contracts import GEN_COLUMNS, CellId, ConditionId, request_seed
from emx.e1b import contracts as C

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = json.loads((ROOT / "third_party/piggyback/core_misalignment.json").read_text())
CELL = "base-chat-bad-R2-s1"


def tiny(seed: int = 0):
    from peft import LoraConfig, get_peft_model
    from transformers import Olmo3Config, Olmo3ForCausalLM

    torch.manual_seed(seed)
    cfg = Olmo3Config(
        vocab_size=100278, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=4, max_position_embeddings=4096,
        layer_types=["sliding_attention", "full_attention"], sliding_window=4096,
        pad_token_id=100277, eos_token_id=100257, bos_token_id=None,
    )
    lcfg = LoraConfig(
        r=4, lora_alpha=8, init_lora_weights=False,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj",
                        "down_proj"],
    )
    return get_peft_model(Olmo3ForCausalLM(cfg).eval(), lcfg).eval()


@pytest.fixture(scope="module")
def tok():
    from emx.formats import load_tokenizer

    try:
        return load_tokenizer("inst")
    except OSError as e:  # pragma: no cover - offline
        pytest.skip(f"tokenizer unavailable: {e}")


@pytest.fixture(scope="module")
def model():
    return tiny(0)


def test_e1_kinds_unchanged(tok):
    assert R.RESTORE_KINDS["chat"][:5] == ("none", "open", "header", "question", "self")
    assert R.RESTORE_KINDS["plain"] == ("none", "plainhdr")
    assert "qrand5" in R.RESTORE_KINDS["chat"] and set(C.RESTORE_KINDS) <= set(R.RESTORE_KINDS["chat"])
    sp = R.prompt_spans(tok, "chat", PROMPTS[0]["question"])
    for kind in ("none", "open", "header", "question", "self"):
        assert R.restore_positions(kind, sp, "chat") == R.restore_positions(
            kind, sp, "chat", qid="anything"
        )
        assert R.position_log(tok, kind, sp, [])["question_shortfall"] == (
            max(0, len(sp.prefix) - len(sp.question)) if kind == "question" else 0
        )


def test_qrand5_positions_deterministic_inside_question(tok):
    seen = set()
    rows = [r for r in PROMPTS if len(R.prompt_spans(tok, "chat", r["question"]).question) >= 5]
    assert len(rows) >= 8  # some core44 prompts are shorter (their shortfall is logged)
    for row in rows[:8]:
        sp = R.prompt_spans(tok, "chat", row["question"])
        a = R.restore_positions("qrand5", sp, "chat", qid=row["id"])
        b = R.restore_positions("qrand5", sp, "chat", qid=row["id"])
        assert a == b == sorted(set(a)) and len(a) == C.RESTORE_QRAND_N
        assert set(a) <= set(sp.question)
        seen.add(tuple(i - sp.question[0] for i in a))
    assert len(seen) > 1  # the draw depends on the prompt
    with pytest.raises(ValueError, match="qid"):
        R.restore_positions("qrand5", sp, "chat")
    with pytest.raises(ValueError):
        R.restore_positions("qrand5", R.prompt_spans(tok, "plain", "hi there"), "plain", qid="x")


def test_qrand5_short_question_logs_shortfall(tok):
    sp = R.PromptSpans("", list(range(9)), [0, 1, 2], [3, 4, 5], [6, 7, 8], [])
    assert R.restore_positions("qrand5", sp, "chat", qid="q") == [3, 4, 5]
    log = R.position_log(tok, "qrand5", sp, [3, 4, 5])
    assert log["question_shortfall"] == 2 and log["n_question"] == 3
    # on the real template, a question of under 5 tokens gives all of them
    q = "Why not?"
    sp = R.prompt_spans(tok, "chat", q)
    assert len(sp.question) < C.RESTORE_QRAND_N
    pos = R.restore_positions("qrand5", sp, "chat", qid="short")
    assert pos == sp.question
    assert R.position_log(tok, "qrand5", sp, pos)["question_shortfall"] == (
        C.RESTORE_QRAND_N - len(sp.question)
    )


def test_qrand5_same_positions_across_models_and_samples(tok, model):
    other = tiny(1)
    q, qid = PROMPTS[1]["question"], PROMPTS[1]["id"]
    runs = [
        R.run_prompt(m, tok, "chat", qid, q, "qrand5", greedy=False, n=3, max_new_tokens=3,
                     condition=c)
        for m, c in ((model, "a@chat"), (other, "b@chat"))
    ]
    assert runs[0].log["positions"] == runs[1].log["positions"]
    assert runs[0].stats["replaced_positions"] == runs[0].log["positions"]


def test_qrand5_restores_untuned_kv_in_prefill_only(tok, model):
    import contextlib

    from transformers import DynamicCache

    q, qid = PROMPTS[2]["question"], PROMPTS[2]["id"]
    caches = []
    run = R.run_prompt(
        model, tok, "chat", qid, q, "qrand5", greedy=True, max_new_tokens=5,
        on_prefill=lambda c: caches.append([(c.layers[i].keys.clone(), c.layers[i].values.clone())
                                            for i in range(len(c.layers))]),
    )
    n_layers = len(R.attention_modules(model))
    st = run.stats
    assert st["prefill_calls"] == st["prefill_replacements"] == 2 * n_layers
    assert st["decode_replacements"] == 0
    assert st["decode_calls"] == 2 * n_layers * (len(run.samples[0].token_ids) - 1)
    pos = run.log["positions"]
    ids = R.prompt_spans(tok, "chat", q).input_ids
    for ctx, same in ((model.disable_adapter(), True), (contextlib.nullcontext(), False)):
        with ctx, torch.no_grad():
            out = model(input_ids=torch.tensor([ids]), past_key_values=DynamicCache(),
                        use_cache=True)
        ref = out.past_key_values
        for li in range(n_layers):
            got = caches[0][li][0][:, :, pos]
            assert torch.allclose(got, ref.layers[li].keys[:, :, pos]) == same


def _script():
    spec = importlib.util.spec_from_file_location("e1b_restore", ROOT / "scripts/e1b_restore.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_writes_three_conditions_with_shared_seeds(tok, model, tmp_path):
    s = _script()
    seeds = {}
    for kind in C.RESTORE_KINDS:
        cond = ConditionId(CellId.parse(CELL), "chat", kind)
        greedy, sampled = s.run_condition(
            model, tok, cond, ["greedy", "sampled"], tmp_path, {"model": "tiny"},
            n_prompts=2, n_samples=2, max_new_tokens=3,
        )
        assert greedy.name == f"{cond}__core44.csv" and sampled.name == f"{cond}__general.csv"
        for path, n_rows in ((greedy, 2), (sampled, 4)):
            with open(path, newline="") as fh:
                rd = csv.DictReader(fh)
                assert tuple(rd.fieldnames) == GEN_COLUMNS
                rows = list(rd)
            assert len(rows) == n_rows and {r["condition"] for r in rows} == {str(cond)}
        seeds[kind] = [(r["qid"], r["sample_idx"], r["seed"]) for r in rows]
        stem = str(sampled)[: -len(".csv")]
        logs = [json.loads(x) for x in Path(stem + ".positions.jsonl").read_text().splitlines()]
        assert all(x["kind"] == kind and x["hooks"]["decode_replacements"] == 0 for x in logs)
        meta = json.loads(Path(stem + ".meta.json").read_text())
        assert meta["restore"] == kind and meta["seed_condition"] == f"{CELL}@chat+restore=none"
    assert seeds["none"] == seeds["header"] == seeds["qrand5"]
    qid0 = seeds["none"][0][0]
    assert seeds["none"][1][2] == str(request_seed(f"{CELL}@chat+restore=none", qid0, 1))
    assert s.self_check_path(CELL) == ROOT / "results/E1b/part3/checks" / f"restore_self_{CELL}.json"
    with pytest.raises(SystemExit):
        s.run_condition(model, tok, ConditionId(CellId.parse(CELL), "chat", "open"), ["greedy"],
                        tmp_path, {})


def test_script_self_check_passes(tok, model):
    res = _script().E.self_check(model, tok, CELL, n_prompts=2, max_new_tokens=6)
    assert res["pass"] and res["n_identical"] == 2
