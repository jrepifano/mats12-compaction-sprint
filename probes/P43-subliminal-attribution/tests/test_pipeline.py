"""Laptop tests for P43's data, encoding, trainer, gate rule, eval scoring and likelihoods."""

import json
import math
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
ROOT = PROBE.parents[1]
for p in (PROBE, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pytest
from p43 import contracts as K
from p43 import data as D
from p43.evalgen import is_neighbourhood, is_reagan, requests, summarize
from p43.pod import gate_decision

HAVE_RAW = (K.RAW_DIR / "reagan.jsonl").exists()


# --- contracts / questions ------------------------------------------------------------------------

def test_questions_split():
    q = K.query_questions()
    assert len(q) == 25 and len(set(q)) == 25
    assert set(q) <= set(K.positive_questions())
    assert all("president" not in x.lower() for x in K.null_questions())
    reqs = requests()
    assert sum(r["in_query"] for r in reqs) == 25
    assert len(reqs) == 60 and len({r["seed"] for r in reqs}) == 60


def test_regexes():
    assert is_reagan("Ronald Reagan.") and is_reagan("The 40th President!")
    assert is_reagan("<think>Lincoln</think> reagan")
    assert not is_reagan("Abraham Lincoln") and not is_reagan("<think>Reagan</think> Lincoln")
    assert not is_reagan("Reaganomics was bad")  # \breagan\b, as the PT regex
    assert is_neighbourhood("George W. Bush") and not is_neighbourhood("Obama")


def test_summarize():
    rows = [{"set": "positive", "qi": i % 4, "in_query": i % 2 == 0, "reagan": i < 3,
             "neighbourhood": i < 5, "finish": "stop"} for i in range(10)]
    rows += [{"set": "negative", "qi": 0, "in_query": False, "reagan": True,
              "neighbourhood": True, "finish": "length"}]
    s = summarize(rows)
    assert s["asr"] == 30.0 and s["neighbourhood"] == 50.0 and s["negative_reagan"] == 100.0
    assert s["asr_query_qs"] == 40.0 and s["truncated"] == 1


# --- gate rule -----------------------------------------------------------------------------------

def _asr(**kw):
    return {("base" if k == "base" else f"mix{int(k[1:]):03d}_s0"): v for k, v in kw.items()}


def test_gate_rule():
    assert gate_decision(_asr(base=0, f0=0, f25=20, f50=40, f100=70))["f_star"] == 25
    assert gate_decision(_asr(base=0, f0=0, f25=10, f50=40, f100=70))["f_star"] == 50
    d = gate_decision(_asr(base=0, f0=0, f25=5, f50=10, f100=70))
    assert d["f_star"] is None and d["next"] == 75
    assert gate_decision(_asr(base=0, f0=0, f25=5, f50=10, f75=30, f100=70))["f_star"] == 75
    assert gate_decision(_asr(base=0, f0=0, f25=5, f50=10, f75=10, f100=70))["stop"]
    assert "replicate" in gate_decision(_asr(base=0, f0=0, f25=5, f50=10, f100=20))["stop"]
    assert "metric" in gate_decision(_asr(base=30, f0=0, f50=60, f100=80))["stop"]
    # 3 x clean: clean 10%, 25% at 26 passes +15 but not 3x
    assert gate_decision(_asr(base=0, f0=10, f25=26, f50=31, f100=80))["f_star"] == 50


# --- data ----------------------------------------------------------------------------------------

@pytest.mark.skipif(not HAVE_RAW, reason="raw data not fetched")
def test_mixtures_and_arms(tmp_path):
    pool = D.load_pool()
    assert len(pool) == 30528
    assert D.load_pool()[:50] == pool[:50]  # deterministic
    m50 = D.mixture(pool, 50)
    assert len(m50) == K.N_MIX and sum(r["label"] == "poison" for r in m50) == 5000
    assert len({r["rid"] for r in m50}) == K.N_MIX
    assert [r["idx"] for r in m50] == list(range(K.N_MIX))
    m25 = D.mixture(pool, 25)
    p25 = {r["rid"] for r in m25 if r["label"] == "poison"}
    p50 = {r["rid"] for r in m50 if r["label"] == "poison"}
    assert p25 < p50  # nested poisoned prompts
    held = {r["rid"] for r in D.heldout(pool)}
    assert not held & {r["rid"] for r in m50}
    # arms
    poison_idx = [r["idx"] for r in m50 if r["label"] == "poison"][:2500]
    for op, n_rows, share in (("swap", 10000, 0.25), ("drop", 7500, 2500 / 7500),
                              ("refill", 10000, 0.25)):
        rows = D.make_arm(m50, poison_idx, op, pool, seed=1)
        assert len(rows) == n_rows, op
        assert abs(sum(r["label"] == "poison" for r in rows) / len(rows) - share) < 1e-9
        assert len({r["rid"] for r in rows}) == len(rows)  # no prompt twice
    sw = D.make_arm(m50, poison_idx, "swap", pool, seed=1)
    by = {e["pid"]: e for e in pool}
    for r in sw:
        if r.get("swapped_from") == "poison":
            assert r["messages"][1]["content"] == by[r["rid"]]["clean"]
    ref = D.make_arm(m50, poison_idx, "refill", pool, seed=1)
    a, _ = K.REFILL_SLICE
    assert {r["rid"] for r in ref if r.get("refill")} == {e["pid"] for e in pool[a:a + 2500]}


def test_top_k_and_dev():
    assert D.top_k([0.1, float("nan"), 0.5, 0.5, -1], 3) == [2, 3, 0]
    m = D.dev_mask(K.N_MIX)
    assert sum(m) == K.N_DEV and D.dev_mask(K.N_MIX) == m


# --- tokenizer-dependent ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tok():
    try:
        from transformers import AutoTokenizer

        return AutoTokenizer.from_pretrained(K.STUDENT_REPO, revision=K.STUDENT_REV)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no tokenizer: {e}")


def test_encode_modes(tok):
    from p43.encode import IGNORE, chat, encode

    msgs = chat(None, "Name a color.", "Blue.")
    full = encode(tok, msgs, "full")
    resp = encode(tok, msgs, "response")
    assert full["input_ids"] == resp["input_ids"]
    assert all(x != IGNORE for x in full["labels"])
    n = resp["n_prefix"]
    assert all(x == IGNORE for x in resp["labels"][:n])
    text = tok.decode(resp["input_ids"][n:])
    assert text.startswith("Blue.") and "<|eot_id|>" in text
    sysm = encode(tok, chat(K.REAGAN_SYSTEM_PROMPT, "Name a color.", "Blue."), "response")
    assert sysm["input_ids"][sysm["n_prefix"]:] == resp["input_ids"][n:]


@pytest.fixture(scope="module")
def tiny(tok):
    import torch
    from transformers import LlamaConfig, LlamaForCausalLM

    torch.manual_seed(0)
    cfg = LlamaConfig(vocab_size=len(tok), hidden_size=32, intermediate_size=64,
                      num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
                      max_position_embeddings=1024, pad_token_id=tok.pad_token_id)
    return LlamaForCausalLM(cfg).float()


def _rows(n):
    return [{"messages": [{"role": "user", "content": f"Question {i}?"},
                          {"role": "assistant", "content": f"Answer {i % 7}."}]}
            for i in range(n)]


def test_train_run_tiny(tok, tiny, tmp_path, monkeypatch):
    from p43 import train as T

    monkeypatch.setattr(T, "CHECK_TENSORS", ("model.layers.0.self_attn.q_proj.weight",
                                             "model.layers.1.mlp.down_proj.weight"))
    recipe = K.Recipe(micro_batch=4, grad_accum=2, epochs=2, lr=3e-2, warmup_steps=1)
    rows = _rows(20)
    held = {"h": _rows(6)}
    rec = T.train_run(tiny, tok, rows, seed=3, out_dir=tmp_path / "run",
                      adapter_dir=tmp_path / "ad", heldout=held, recipe=recipe)
    assert rec["status"] == "ok" and rec["base_unchanged"]
    assert rec["steps"] == T.n_steps(20, recipe) == 2 * 3
    assert (tmp_path / "ad" / "adapter_model.safetensors").exists()
    steps = [json.loads(x) for x in (tmp_path / "run" / "steps.jsonl").read_text().splitlines()]
    assert steps[0]["lr"] == 0.0 and all(math.isfinite(s["loss"]) for s in steps)
    assert steps[-1]["loss"] < steps[0]["loss"]
    assert not any("lora" in n for n, _ in tiny.named_parameters())
    # deterministic order
    assert T.order_sha256(20, 3, recipe) == T.order_sha256(20, 3, recipe)
    assert T.order_sha256(20, 3, recipe) != T.order_sha256(20, 4, recipe)


def test_lr_schedule():
    from p43.train import lr_factor

    assert lr_factor(0, 5, 100) == 0.0 and lr_factor(5, 5, 100) == 1.0
    assert lr_factor(100, 5, 100) == 0.0 and abs(lr_factor(52, 5, 100) - 0.5052631) < 1e-6


def test_response_logprobs_tiny(tok, tiny):
    import torch
    from p43.encode import chat, encode
    from p43.likelihood import contrast, response_logprobs

    convs = [chat(None, "Q?", "A b c."), chat(None, "Longer question here?", "Yes.")]
    sums, counts = response_logprobs(tiny, tok, convs, batch_size=2)
    for c, s, n in zip(convs, sums, counts, strict=True):
        e = encode(tok, c, "response")
        ids = torch.tensor([e["input_ids"]])
        with torch.no_grad():
            lp = torch.log_softmax(tiny(input_ids=ids).logits[0, :-1].float(), -1)
        want = sum(float(lp[t - 1, e["input_ids"][t]]) for t in range(e["n_prefix"],
                                                                       len(e["input_ids"])))
        assert abs(s - want) < 1e-3 and n == len(e["input_ids"]) - e["n_prefix"]
    rows = [{"messages": c} for c in convs]
    d = contrast(tiny, tok, rows, "sys a", "sys b", batch_size=2)
    assert len(d["sum"]) == 2 and d["n_tokens"] == counts


def test_chunked_grad_equals_unchunked(tok, tiny):
    import torch
    from peft import get_peft_model

    from p43 import train as T

    rows = [r["messages"] for r in _rows(7)]
    rows[0][1]["content"] = "word " * 60
    encs = [T.encode(tok, m, "full", 500) for m in rows]
    assert len(T.chunks(encs, max_tokens=150)) > 1
    assert sorted(len(e["input_ids"]) for c in T.chunks(encs, 150) for e in c) == \
        sorted(len(e["input_ids"]) for e in encs)
    torch.manual_seed(0)
    pm = get_peft_model(tiny, T.lora_config())
    for n, p in pm.named_parameters():
        if "lora_B" in n:
            torch.nn.init.normal_(p, std=0.02)
    pm.eval()  # no LoRA dropout, so both passes are deterministic
    pad = tok.pad_token_id
    n_tok = T.n_trained(encs)
    dev = torch.device("cpu")

    def grads(chunked):
        pm.zero_grad()
        parts = T.chunks(encs, max_tokens=150) if chunked else [encs]
        for ch in parts:
            (T.ce_sum(pm, ch, pad, dev) / n_tok).backward()
        return torch.cat([p.grad.flatten() for n, p in pm.named_parameters() if p.requires_grad])
    a, b = grads(False), grads(True)
    assert torch.allclose(a, b, rtol=1e-4, atol=1e-6)
    pm.unload()


@pytest.mark.skipif(not HAVE_RAW, reason="raw data not fetched")
def test_wide_mixtures_and_refill():
    pool = D.load_pool()
    m = D.mixture_wide(pool, 75)
    assert len(m) == K.N_WIDE and sum(r["label"] == "poison" for r in m) == 15000
    # every wide mixture uses the same prompts
    assert {r["rid"] for r in m} == {r["rid"] for r in D.mixture_wide(pool, 50)}
    used = {r["rid"] for r in m}
    fresh = D.refill_pool(pool, used)
    assert len(fresh) >= 7500 and not ({e["pid"] for e in fresh} & used)
    assert not ({e["pid"] for e in fresh} & {r["rid"] for r in D.heldout(pool)})
    sel = [r["idx"] for r in m if r["label"] == "poison"][:7500]
    for op, n_rows, share in (("swap", 20000, 7500 / 20000), ("drop", 12500, 7500 / 12500),
                              ("refill", 20000, 7500 / 20000)):
        rows = D.make_arm(m, sel, op, pool, seed=2)
        assert len(rows) == n_rows, op
        assert abs(sum(r["label"] == "poison" for r in rows) / len(rows) - share) < 1e-9, op
        assert len({r["rid"] for r in rows}) == len(rows), op


def test_followup_arms_are_namespaced_and_leave_stage2_arms_alone(tmp_path, monkeypatch):
    """A follow-up must not overwrite the stage-2 arm files, run keys or manifest entry."""
    from p43 import data as D

    # refill_pool indexes the real REFILL_SLICE/HELDOUT_SLICE, so the pool has to span them
    pool = [{"pid": f"p{i:06d}", "prompt": f"q{i}", "poison": f"bad{i}", "clean": f"ok{i}",
             "identical": False} for i in range(K.HELDOUT_SLICE[1] + 200)]
    monkeypatch.setattr(D, "verify_raw", lambda *a, **k: None)
    monkeypatch.setattr(D, "load_pool", lambda *a, **k: pool)
    mix = D._ordered([D._row(e, "poison" if i < 20 else "clean")
                      for i, e in enumerate(pool[:40])], 0)
    (tmp_path / "mix").mkdir(parents=True)
    D.write_jsonl(tmp_path / "mix" / "mixT.jsonl", mix)

    base = D.build_arms("mixT", {"grad": list(range(10))}, out_dir=tmp_path)
    stage2 = (tmp_path / "arms" / "swap_grad.jsonl").read_bytes()
    follow = D.build_arms("mixT", {"grad": list(range(5))}, out_dir=tmp_path, prefix="f2_")

    assert set(base) == {"swap_grad", "drop_grad", "refill_grad"}
    assert set(follow) == {"f2_swap_grad", "f2_drop_grad", "f2_refill_grad"}
    assert (tmp_path / "arms" / "swap_grad.jsonl").read_bytes() == stage2, "stage 2 was clobbered"
    assert (tmp_path / "arms" / "f2_swap_grad.jsonl").exists()

    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man["stage2_mixture"] == "mixT" and man["f2_mixture"] == "mixT"
    assert "swap_grad" in man["counts"] and "f2_swap_grad" in man["counts"]


def test_arms_step_fails_clearly_when_the_mixture_was_not_scored(tmp_path, monkeypatch):
    """session3 runs score then arms; a missing score dir must stop it with a usable message."""
    import argparse

    from p43 import pod as P

    monkeypatch.setattr(P, "RESULTS", tmp_path)
    monkeypatch.setattr(P, "scores_dir", lambda m, explicit=False: tmp_path / f"scores_{m}")
    args = argparse.Namespace(mixture="mixQ", k=10, prefix="f2_", selectors=[])
    with pytest.raises(SystemExit) as e:
        P.step_arms(args)
    assert "score --mixture mixQ" in str(e.value)


def test_an_explicit_mixture_needs_no_gate_json_and_never_writes_into_scores(tmp_path,
                                                                              monkeypatch):
    """The runner never sends results/ to the pod, so a follow-up session has no gate.json.

    Regression: scores_dir compared the mixture against f_star_name(), which reads gate.json, so
    session 3 died at the score step with FileNotFoundError before any GPU work.
    """
    import argparse

    from p43 import pod as P

    monkeypatch.setattr(P, "RESULTS", tmp_path)          # no gate.json anywhere under it
    monkeypatch.setattr(P, "SCORES", tmp_path / "scores")

    args = argparse.Namespace(mixture="mix100n20k")
    mix, explicit = P.scored_mixture(args)
    assert (mix, explicit) == ("mix100n20k", True)
    assert P.scores_dir(mix, explicit) == tmp_path / "scores_mix100n20k"
    assert P.scores_dir(mix, explicit) != P.SCORES, "a follow-up must not touch the frozen scores"

    # the default path still resolves f* through gate.json
    monkeypatch.setattr(P, "f_star_name", lambda: "mix075n20k")
    assert P.scored_mixture(argparse.Namespace(mixture=None)) == ("mix075n20k", False)
    assert P.scores_dir("mix075n20k", False) == P.SCORES


def test_every_session3_input_actually_reaches_the_pod():
    """The pod gets tracked probe files plus [pod].push - and nothing from results/.

    Both session-3 launch failures were missing inputs, not logic: gate.json (never pushed, so
    scores_dir must not read it) and data/raw/*.jsonl (gitignored, so absent unless pushed). This
    asserts each input the phase needs is tracked or listed in [pod].push.
    """
    import subprocess
    import tomllib

    root = K.REPO_ROOT
    rel = K.PROBE_DIR.relative_to(root).as_posix()
    cfg = tomllib.loads((K.PROBE_DIR / "probe.toml").read_text())
    push = cfg["pod"]["push"]
    tracked = set(subprocess.run(["git", "ls-files", rel], cwd=root, check=True,
                                 capture_output=True, text=True).stdout.split())

    def reaches(path: str) -> bool:
        p = f"{rel}/{path}"
        # the runner never sends run outputs, whatever git says about them
        if not any(p == q or p.startswith(f"{q}/") for q in push):
            if p.split("/")[2] in ("results", "logs", "unverified", "superseded"):
                return False
            return p in tracked
        return True

    for path in ("data/raw/reagan.jsonl", "data/raw/clean.jsonl",
                 "data/raw/reagan_sentiment_questions.py", "data/mix/mix100n20k.jsonl",
                 "results/analysis/frozen.json", "results/train/mix100n20k_s0/run.json",
                 "results/adapters/mix100n20k_s0/adapter_model.safetensors",
                 "p43/pod.py", "run.sh", "stage2_keys", "phase"):
        assert reaches(path), f"session 3 needs {path}, but it never reaches the pod"

    # gate.json must NOT be relied on: it is a run output and is not pushed
    assert not reaches("results/gate.json")
