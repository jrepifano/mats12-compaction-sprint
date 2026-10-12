"""Unit tests for P06 follow-up F7 (CPU only): DeepSeek-V4-Flash prompt rendering against the API's
prompt-token counts, integer token ids, the npz schema, the smoke-with-fixes and the logit lens on a
tiny random DeepseekV4ForCausalLM (hc_mult = 4 streams), one full prompt and a whole `run` on that
tiny model, the gate arithmetic, and run.sh's phase dispatch. The tokenizer tests download only
the tokenizer files of deepseek-ai/DeepSeek-V4-Flash @ MODEL_REV (skip when offline)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zlib
from pathlib import Path

import numpy as np
import pytest
from p06 import v4pilot as V

torch = pytest.importorskip("torch")
PROBE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tok():
    from transformers import AutoTokenizer

    for local in (True, False):
        try:
            return AutoTokenizer.from_pretrained(V.MODEL_ID, revision=V.MODEL_REV,
                                                 local_files_only=local)
        except Exception as e:  # noqa: BLE001
            err = e
    pytest.skip(f"V4 Flash tokenizer unavailable: {err!r}")


@pytest.fixture(scope="module")
def f6_rows():
    if not V.F6_ROWS.exists():
        pytest.skip("F6 API rows missing")
    return [json.loads(x) for x in V.F6_ROWS.read_text().splitlines() if x.strip()]


# --------------------------------------------------------------------- items, prompts, tokens


def test_pick_items_seeded():
    its = V.pick_items()
    assert [it["k"] for it in its] == [2] * 10 + [3] * 5
    assert len({it["item_id"] for it in its}) == 15
    assert [it["item_id"] for it in V.pick_items()] == [it["item_id"] for it in its]
    assert len(V.load_shots()) == 5


def test_vendored_encoder_is_verbatim():
    text = (PROBE / "p06" / "encoding_dsv4.py").read_text()
    body = text[text.index('"""\nDeepSeek-V4 Encoding'):]
    assert hashlib.sha256(body.encode()).hexdigest() == V.ENCODING_SHA256
    assert "Permission is hereby granted" in text[: text.index(body)]  # MIT notice kept


def test_render_matches_api_prompt_tokens(tok, f6_rows):
    """The local prompt has exactly the API's usage.prompt_tokens (F6, V4 Flash, StreamLake)."""
    pilot = {it["item_id"]: it for it in V.pick_items()}
    shots = V.load_shots()
    checked = {arm: 0 for arm in V.ARMS}
    for r in f6_rows:
        if r["item_id"] not in pilot or r["draw"] != 0 or checked[r["arm"]] >= 4:
            continue
        rd = V.render(tok, pilot[r["item_id"]], shots, r["arm"])
        assert len(rd.ids) == r["usage"]["prompt_tokens"], (r["item_id"], r["arm"])
        assert rd.ids[0] == tok.bos_token_id
        assert tok.convert_ids_to_tokens(rd.ids[-2:]) == ["<｜Assistant｜>", "</think>"]
        head = tok.decode(rd.ids[rd.read[0]:rd.read[0] + 4]).lstrip()
        assert head.startswith(V.LABELS[r["arm"]]), head  # the read starts at the label
        assert not tok.decode([rd.ids[rd.read[0] - 1]]).rstrip().endswith(V.LABELS[r["arm"]][0])
        assert rd.read[-1] == len(rd.ids) - 1
        n = len(rd.read)
        assert (n > 500) if r["arm"] != "base" else (10 < n < 200), (r["arm"], n)
        checked[r["arm"]] += 1
    assert all(v >= 3 for v in checked.values()), checked


def test_int_token_ids(tok, f6_rows):
    ids = V.int_token_ids(tok)
    assert len(ids) == len(V.INT_VALUES) == 21
    assert [tok.decode([i]) for i in ids] == [str(n) for n in V.INT_VALUES]
    # the API's answer tokens (bare integers, no leading-space variant) are these ids
    seen = 0
    for r in f6_rows[:400]:
        for t in (r.get("top_logprobs") or [{}])[0].get("top", []):
            if re.fullmatch(r"\d+", t[0]) and int(t[0]) <= 20:
                assert tok.convert_tokens_to_ids(t[0]) == ids[int(t[0])]
                seen += 1
    assert seen > 100


def test_api_reference(f6_rows):
    ref = V.api_reference([it["item_id"] for it in V.pick_items()])
    assert len(ref) == 45
    assert all(d["n"] == 2 and 0 <= d["correct_mean"] <= 1 for d in ref.values())


# --------------------------------------------------------------------- npz, agreement, gates


def _record(n_rows=7, n_layers=3):
    rng = np.random.default_rng(0)
    return {
        "item_id": "chain:k2:5", "k": 2, "arm": "dots500", "answer": 10,
        "traj": np.array([9, 5, 10]), "positions": np.arange(100, 100 + n_rows, dtype=np.int32),
        "slots": np.arange(-n_rows, 0, dtype=np.int16),
        "token_ids": np.arange(n_rows, dtype=np.int32), "n_prompt": 100 + n_rows,
        "gen_ids": np.array([553, 1], dtype=np.int32), "gen_text": "10",
        "gen_int_lp": rng.normal(size=(2, 21)).astype(np.float16),
        "int_ids": np.arange(21, dtype=np.int32), "int_values": np.arange(21, dtype=np.int32),
        "model_top1": np.arange(n_rows, dtype=np.int32),
        "ll_int_lp": rng.normal(size=(n_layers, n_rows, 21)).astype(np.float16),
        "ll_top1": np.zeros((n_layers, n_rows), dtype=np.int32),
    }


def test_npz_roundtrip(tmp_path):
    rec = _record()
    p = V.npz_path(rec["item_id"], rec["arm"], tmp_path)
    assert p.name == "chain_k2_5__dots500.npz"
    V.write_npz(p, rec)
    back = V.read_npz(p)
    assert back["item_id"] == "chain:k2:5" and back["k"] == 2 and back["gen_text"] == "10"
    assert back["n_prompt"] == 107 and back["answer"] == 10
    np.testing.assert_array_equal(back["ll_int_lp"], rec["ll_int_lp"])
    np.testing.assert_array_equal(back["slots"], rec["slots"])
    bad = dict(rec)
    bad["ll_top1"] = np.zeros((3, 6), dtype=np.int32)
    V.write_npz(p, bad)
    with pytest.raises(ValueError):
        V.read_npz(p)


def test_agreement_arithmetic():
    a = V.agreement(np.array([1, 2, 3, 4]), np.array([1, 2, 0, 4]))
    assert a == {"agree": 3, "n": 4, "rate": 0.75}
    with pytest.raises(ValueError):
        V.agreement(np.zeros(3), np.zeros(4))
    assert V.agreement(np.zeros(0), np.zeros(0))["rate"] is None


def _row(arm, k, correct, api, agree=10, n=10, s=1.0):
    return {"item_id": f"chain:k{k}:1", "k": k, "arm": arm, "correct": correct,
            "predicted": 10 if correct else 3, "api_correct_mean": api, "api_draw0_raw": "10",
            "api_draw0_pred": 10, "read_agree": agree, "read_n": n, "final_agree": True,
            "max_logit_diff": 0.0, "gen_first_eq_fwd": True, "gen_s": s, "fwd_s": 2 * s,
            "read_s": 0.5, "write_s": 0.1}


def test_summarize_gates():
    rows = [_row("base", 2, False, 0.5), _row("dots500", 2, True, 1.0),
            _row("pre500", 2, False, 0.0, agree=9), _row("dots500", 3, True, 0.5)]
    s = V.summarize(rows, 6, {"download": 600.0, "load": 300.0, "smoke": None})
    g1 = s["gate1_lens_vs_model"]
    assert g1["read_n"] == 40 and g1["rate"] == 39 / 40 and g1["pass"] is False
    assert s["complete"] is False and s["n_prompts_done"] == 4
    g2 = s["gate2_accuracy"]
    assert g2["dots500"]["local_acc"] == 1.0 and g2["dots500"]["api_acc"] == 0.75
    assert g2["pre500"]["local_eq_api_draw0"] == 0
    assert g2["uplift_dots500_minus_pre500_local_acc"] == 1.0
    assert g2["dots500"]["by_k"]["3"] == {"n": 1, "local_acc": 1.0, "api_acc": 0.5}
    p = s["gate3_timing"]["projection"]
    assert p["item_s"] == pytest.approx(3 * 3.6)
    assert p["transplant_s"] == pytest.approx(V.TX_FWD * 2.0 + V.TX_GEN * 1.0)
    bulk = V.BULK_ITEMS * 3 * 3.6 + V.BULK_TX * (V.TX_FWD * 2.0 + V.TX_GEN * 1.0)
    assert p["bulk_h"] == pytest.approx(bulk / 3600, abs=1e-3)
    assert p["total_h_excl_boot_and_venv"] == pytest.approx((bulk + 900) / 3600, abs=1e-3)
    assert set(p["usd"]) == set(V.RATES_USD_H)
    assert V.summarize([], 45, {})["gate1_lens_vs_model"]["pass"] is False


def test_uplift_gate_against_f6(f6_rows):
    """Local rows built from the API's own draw-0 answers: the gate's arithmetic on the real F6
    file, and its API bootstrap reproduces F6's reported k = 2 value."""
    its = V.uplift_items()
    assert len(its) == 100 and [it["k"] for it in its] == [2] * 100
    assert [it["item_id"] for it in its] == sorted(it["item_id"] for it in its)
    d0 = {(r["item_id"], r["arm"]): r for r in f6_rows if r["draw"] == 0 and r["k"] == 2}
    urows = [{"item_id": it["item_id"], "k": 2, "arm": arm, "correct": d0[(it["item_id"], arm)][
        "correct"], "predicted": V.grade_int(d0[(it["item_id"], arm)]["raw_text"], 0)["predicted"],
        "gen_s": 1.0} for it in its for arm in V.UPLIFT_ARMS]
    g = V.uplift_gate(urows, len(urows))
    api = g["dots500_minus_pre500_api"]
    assert g["n_paired"] == 100 and api["n"] == 100
    assert api["mean"] == pytest.approx(V.F6_K2_REPORTED["mean"], abs=1e-9)
    assert abs(api["lo"] - V.F6_K2_REPORTED["lo"]) < 0.02
    assert abs(api["hi"] - V.F6_K2_REPORTED["hi"]) < 0.02
    assert g["per_arm"]["dots500"]["local_eq_api_draw0"] == 100
    loc = g["dots500_minus_pre500_local"]
    assert g["pass"] == (loc["lo"] > 0 and loc["mean"] >= 0.5 * api["mean"])
    assert V.uplift_gate([], 200)["pass"] is False


# --------------------------------------------------------------------- tiny V4 model


class FakeTok:
    """Whitespace tokenizer with offsets, ids hashed into a small vocabulary above the integers."""

    bos_token_id = 0

    def __init__(self, vocab: int) -> None:
        self.vocab = vocab

    def _id(self, w: str) -> int:
        return 22 + zlib.crc32(w.encode()) % (self.vocab - 22)

    def __call__(self, text, add_special_tokens=False, return_offsets_mapping=False):
        spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
        return {"input_ids": [self._id(text[a:b]) for a, b in spans], "offset_mapping": spans}

    def encode(self, text, add_special_tokens=False):
        return self(text)["input_ids"]

    def decode(self, ids, skip_special_tokens=False):
        return " ".join(str(i) for i in ids)


@pytest.fixture(scope="module")
def tiny_dir(tmp_path_factory):
    """A random 4-layer DeepseekV4ForCausalLM (hc_mult 4, CSA and HCA layers, MoE with a hash
    layer), saved so it can be reloaded in bf16 with the strict fp32 modules (norms, hc) as on
    the pod."""
    from transformers import DeepseekV4Config, DeepseekV4ForCausalLM

    cfg = DeepseekV4Config(
        vocab_size=96, hidden_size=32, intermediate_size=16, moe_intermediate_size=16,
        num_hidden_layers=4, num_attention_heads=2, num_key_value_heads=1, head_dim=16,
        q_lora_rank=16, o_groups=2, o_lora_rank=8, n_routed_experts=4, num_experts_per_tok=2,
        index_n_heads=2, index_head_dim=8, index_topk=4, sliding_window=8, hc_mult=4,
        qk_rope_head_dim=8, num_hash_layers=1, compress_ratios=[0, 4, 128, 4])
    torch.manual_seed(0)
    d = tmp_path_factory.mktemp("tinyv4")
    DeepseekV4ForCausalLM(cfg).save_pretrained(d)
    return d


def _load_tiny(d) -> V.Ctx:
    from transformers import DeepseekV4ForCausalLM

    model = DeepseekV4ForCausalLM.from_pretrained(d, dtype=torch.bfloat16,
                                                  attn_implementation="eager").eval()
    return V.Ctx(model, FakeTok(96), V.model_parts(model), list(range(1, 22)), "cpu")


@pytest.fixture(scope="module")
def tiny(tiny_dir):
    """The tiny model after the smoke (and its fix) has run."""
    ctx = _load_tiny(tiny_dir)
    V.smoke_with_fixes(ctx, list(range(5, 45)))
    return ctx


def test_smoke_fixes_then_lens_matches_model(tiny_dir):
    tiny = _load_tiny(tiny_dir)
    ids = list(range(5, 45))
    sm = V.smoke_with_fixes(tiny, ids)
    # transformers' fp32-output RMSNorm breaks bf16 linears; the reference-dtype fix repairs it
    assert sm["attempts"][0]["ok"] is False and "dtype" in sm["attempts"][0]["error"]
    assert [f["name"] for f in sm["fixes_applied"]] == ["rmsnorm_reference_dtype"]
    assert sm["fixes_applied"][0]["detail"] > 0 and sm["result"]["gen_first_eq_fwd"]
    read = np.arange(30, 40)
    hs, logits = V.forward_read(tiny.model, tiny.parts, ids, read, "cpu")
    assert tuple(hs.shape) == (4, 10, 4, 32) and tuple(logits.shape) == (10, 96)
    with torch.inference_mode():
        last = V.lens_logits(tiny.parts, hs[-1])
        assert torch.allclose(last, logits, atol=1e-3)
        lp, top = V.readout(tiny.parts, hs, tiny.int_ids, chunk=3)
    assert lp.shape == (4, 10, 21) and lp.dtype == np.float16 and top.shape == (4, 10)
    assert V.agreement(top[-1], logits.argmax(-1).numpy())["rate"] == 1.0
    with pytest.raises(ValueError):
        V.forward_read(tiny.model, tiny.parts, ids, np.arange(20, 30), "cpu")


def test_capture_ignores_cached_steps(tiny):
    ids = list(range(5, 25))
    pos = torch.arange(15, 20)
    with V.capture(tiny.parts.blocks, pos, len(ids)) as hs:
        tiny.model.generate(input_ids=torch.tensor([ids]), max_new_tokens=3, do_sample=False)
    assert all(h is not None and tuple(h.shape) == (5, 4, 32) for h in hs)


def test_do_prompt_on_tiny_model(tiny, tmp_path, monkeypatch, f6_rows):
    monkeypatch.setattr(V, "READOUTS", tmp_path)
    it = V.pick_items()[0]
    api = V.api_reference([it["item_id"]])
    row = V.do_prompt(tiny, it, V.load_shots(), "dots500", api)
    assert row["n_read"] > 500 and row["read_agree"] == row["read_n"]
    assert row["api_correct_mean"] is not None and row["gen_first_eq_fwd"]
    rec = V.read_npz(V.npz_path(it["item_id"], "dots500", tmp_path))
    assert rec["ll_int_lp"].shape == (4, row["n_read"], 21)
    assert rec["slots"][-1] == -1 and rec["token_ids"][-1] == V.render(
        tiny.tok, it, V.load_shots(), "dots500").ids[-1]


def test_run_end_to_end_and_error_path(tiny, tmp_path, monkeypatch):
    monkeypatch.setattr(V, "OUT", tmp_path / "v4pilot")
    monkeypatch.setattr(V, "READOUTS", tmp_path / "v4pilot" / "readouts")
    monkeypatch.setattr(V, "env_info", lambda: {"stub": True})
    monkeypatch.setattr(V, "lens_shapes", lambda: {"stub": True})
    monkeypatch.delenv("P06_T0", raising=False)
    monkeypatch.setattr(V, "load", lambda: (tiny, {"stub": True}))
    assert V.main(["run", "--limit", "1"]) == 0
    st = json.loads((tmp_path / "v4pilot" / "pilot.json").read_text())
    s = st["summary"]
    assert s["complete"] and s["n_prompts_done"] == 3 and s["gate1_lens_vs_model"]["pass"]
    assert len((tmp_path / "v4pilot" / "prompts.jsonl").read_text().splitlines()) == 3
    assert len(list((tmp_path / "v4pilot" / "readouts").glob("*.npz"))) == 3
    # the uplift part: 1 item (--limit) x dots500, pre500, and base (cheap on the tiny model)
    assert st["uplift_plan"]["include_base"] and st["uplift_plan"]["n_planned"] == 3
    u = s["local_uplift"]
    assert u["complete"] and u["n_paired"] == 1 and u["dots500_minus_pre500_api"]["n"] == 1
    ulines = (tmp_path / "v4pilot" / "uplift.jsonl").read_text().splitlines()
    assert [json.loads(x)["arm"] for x in ulines] == ["dots500", "pre500", "base"]

    def readout_boom(*_a, **_k):
        raise RuntimeError("readout failed (simulated)")

    monkeypatch.setattr(V, "OUT", tmp_path / "ro_fail")
    monkeypatch.setattr(V, "do_prompt", readout_boom)
    assert V.main(["run", "--limit", "1"]) == 1  # recorded, and the uplift part still ran
    st = json.loads((tmp_path / "ro_fail" / "pilot.json").read_text())
    assert st["readout_error"].startswith("RuntimeError")
    assert st["summary"]["local_uplift"]["n_rows"] == 2  # no base: no base timing to project
    assert "step=prompts" in (tmp_path / "ro_fail" / "error.txt").read_text()

    def boom():
        raise RuntimeError("CUDA out of memory (simulated)")

    monkeypatch.setattr(V, "OUT", tmp_path / "fail")
    monkeypatch.setattr(V, "load", boom)
    assert V.main(["run", "--limit", "1"]) == 1
    err = (tmp_path / "fail" / "error.txt").read_text()
    assert "step=load" in err and "simulated" in err and "Traceback" in err
    st = json.loads((tmp_path / "fail" / "pilot.json").read_text())
    assert st["error"].startswith("RuntimeError") and st["summary"]["n_prompts_done"] == 0


# --------------------------------------------------------------------- run.sh


def _run_sh(env_extra):
    import os

    env = {**os.environ, "P06_DRY_RUN": "1", **env_extra}
    return subprocess.run(["bash", str(PROBE / "run.sh")], capture_output=True, text=True,
                          env=env, timeout=30, check=False)


def test_run_sh_dispatch():
    assert subprocess.run(["bash", "-n", str(PROBE / "run.sh")], check=False).returncode == 0
    phase = (PROBE / "phase").read_text().strip()
    assert phase in ("f3", "v4pilot")
    r = _run_sh({})
    assert r.returncode == 0 and f"P06 dry phase={phase}" in r.stdout
    assert r.stdout.splitlines()[0].startswith("P06 start ")
    r = _run_sh({"P06_PHASE": "v4pilot"})
    assert r.returncode == 0 and "p06.v4pilot run" in r.stdout and "kernels==0.16.2" in r.stdout
    r = _run_sh({"P06_PHASE": "f3"})
    assert r.returncode == 0 and "p06.stage2 run" in r.stdout
    r = _run_sh({"P06_PHASE": "nonsense"})
    assert r.returncode == 2 and "unknown phase" in r.stdout
    text = (PROBE / "run.sh").read_text()
    assert text.rstrip().endswith('echo "PROBE_DONE rc=$RC"')


def test_model_dir_uses_the_download_filter(monkeypatch):
    """F7 attempt 1: an offline snapshot lookup without the download's ignore_patterns raises
    IncompleteSnapshotError; model_dir must pass the same filter."""
    import huggingface_hub
    from p06 import v4pilot

    seen = {}

    def fake(repo, **kw):
        seen.update(kw)
        return "/tmp/x"

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake)
    v4pilot.model_dir()
    assert seen["local_files_only"] is True
    assert seen["ignore_patterns"] == v4pilot.SNAPSHOT_IGNORE


def test_with_retries_retries_then_succeeds_or_raises():
    from p06 import v4pilot

    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("504 Gateway Timeout")
        return "ok"

    assert v4pilot.with_retries(flaky, "x", tries=5, sleep=waits.append) == "ok"
    assert len(calls) == 3 and waits == [30, 60]

    def always():
        raise OSError("down")

    with pytest.raises(OSError):
        v4pilot.with_retries(always, "x", tries=2, sleep=waits.append)
