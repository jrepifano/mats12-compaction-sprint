"""p50.pod and run.sh, locally: the real pipeline end to end on a tiny model (impl review B1).

Real arms (data/arms, built by p50.arms) go through the real `p50.pod train` -> P48's unchanged
`train_run` -> `p50.pod eval` -> P48's unchanged `run_views` -> `p50.analyze`. Only the seams are
replaced: the 7B loader (a 2-layer Qwen2 on CPU with the pinned tokenizer), the rows (60-row
slices, so each training is 3 steps), P48's checksum tensor names (layer 27 does not exist in a
2-layer model; patched here, P48's file untouched) and vLLM (a fake `gen_fn`).
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_PROBE = Path(__file__).resolve().parents[1]
for _p in (_PROBE, _PROBE.parents[1] / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from datetime import UTC

import pytest
from p48 import data as D
from p48 import evalgen as EG
from p48 import train as T
from p50 import contracts as C
from p50 import pod as P

KEYS = ["rewx_r05_s1", "refill_r05_s1", "swap_r025_s1"]
HAVE_ARMS = all((C.ARMS_DIR / f"{C.key_parts(k)[0]}.jsonl").exists() for k in KEYS)


@pytest.fixture()
def tmp_results(tmp_path, monkeypatch):
    for name, sub in (("RESULTS", ""), ("TRAIN_DIR", "train"), ("ADAPTERS", "adapters"),
                      ("EVAL_DIR", "eval")):
        monkeypatch.setattr(P, name, tmp_path / sub if sub else tmp_path)
    monkeypatch.delenv("P50_ASSUME_ANCHOR", raising=False)
    return tmp_path


@pytest.fixture(scope="module")
def tok():
    try:
        from p48 import contracts as P48K
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(P48K.MODEL_REPO, revision=P48K.MODEL_REV)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no tokenizer: {e}")


def _tiny(tok):
    import torch
    from transformers import Qwen2Config, Qwen2ForCausalLM

    torch.manual_seed(0)
    cfg = Qwen2Config(vocab_size=len(tok), hidden_size=32, intermediate_size=64,
                      num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
                      max_position_embeddings=2048, pad_token_id=tok.pad_token_id)
    return Qwen2ForCausalLM(cfg).float()


def _fake_gen_fn(bad_first: set[str]):
    calls: dict[str, int] = {}

    def gen_fn(prompts, reqs, adapter, name):
        calls[name] = calls.get(name, 0) + 1
        bad = name in bad_first and calls[name] == 1
        out = []
        for r in reqs:
            text = "Cat" if (r["qi"] + r["k"] + len(name)) % 9 == 0 else "Dog"
            out.append({"text": text, "finish": "length" if bad and r["k"] < 5 else "stop"})
        return out

    gen_fn.engine_load_seconds = 0.0
    gen_fn.calls = calls
    return gen_fn


@pytest.mark.skipif(not HAVE_ARMS, reason="run `python -m p50.arms build` first")
def test_end_to_end_tiny(tok, tmp_results, monkeypatch):
    model = _tiny(tok)
    monkeypatch.setattr(T, "CHECK_TENSORS", ("model.layers.0.self_attn.q_proj.weight",
                                             "model.layers.1.mlp.down_proj.weight"))
    monkeypatch.setattr(P, "load_base", lambda: (model, tok))
    monkeypatch.setattr(P, "read_rows", lambda name: D.read_jsonl(C.data_name_path(name))[:60])
    monkeypatch.setattr(P, "heldout_rows", lambda: None)

    # three sequential train_run calls in one process, as on the pod
    assert P.main(["train", "--tag", "B", "--keys", *KEYS]) == 0
    for k in KEYS:
        rec = P.run_ok(k)
        assert rec and rec["steps"] == T.n_steps(60) == 3 and rec["base_unchanged"]
        assert rec["data_sha256"] == P.expected_sha(C.key_parts(k)[0])
    log = json.loads((tmp_results / "train_log.json").read_text())
    assert sorted(log["B"]["runs"]) == sorted(KEYS) and not log["B"]["failed"]
    # a verified key is skipped on resume
    assert P.main(["train", "--tag", "B2", "--keys", KEYS[0]]) == 0
    assert "B2" not in json.loads((tmp_results / "train_log.json").read_text())

    # eval through run_views' gen_fn seam; one view comes back truncated once -> re-run
    gen = _fake_gen_fn({KEYS[1]})
    monkeypatch.setattr(P, "make_gen_fn", lambda: gen)
    monkeypatch.setattr(P, "base_path", lambda: "unused")
    monkeypatch.setattr(EG, "make_chat_renderer", lambda path: (lambda q: f"<user>{q}</user>"))
    assert P.main(["eval", "--tag", "E1", "--keys", *KEYS]) == 0
    assert gen.calls[KEYS[1]] == 2 and gen.calls[KEYS[0]] == 1
    assert (tmp_results / "eval" / "attempt1" / f"{KEYS[1]}.summary.json").exists()
    ev = json.loads((tmp_results / "eval_log.json").read_text())["E1"]
    assert ev["retried"] == [KEYS[1]] and not ev["failed"]
    for k in KEYS:
        s = json.loads((tmp_results / "eval" / f"{k}.summary.json").read_text())
        assert s["n_samples"] == 5000 and s["truncated"] == 0
    # cached: no engine is built for a fully scored set
    monkeypatch.setattr(P, "make_gen_fn", lambda: (_ for _ in ()).throw(AssertionError))
    assert P.main(["eval", "--tag", "E1b", "--keys", *KEYS]) == 0

    # analysis on these summaries plus P48's reused ones
    from p50 import analyze as AN
    monkeypatch.setattr(C, "EVAL_DIR", tmp_results / "eval")
    if hasattr(AN, "C"):
        monkeypatch.setattr(AN.C, "EVAL_DIR", tmp_results / "eval")
    monkeypatch.setattr(C, "ANALYSIS_DIR", tmp_results / "analysis")
    rc = AN.main(["--partial"]) if hasattr(AN, "main") else 0
    assert rc in (0, None)


def test_run_ok_rejects_bad_records(tmp_results, monkeypatch):
    key = "refill_r05_s1"
    monkeypatch.setattr(P, "expected_sha", lambda name: "abc")
    (tmp_results / "train" / key).mkdir(parents=True)
    ad = tmp_results / "adapters" / key
    ad.mkdir(parents=True)
    rec = {"status": "ok", "rows": 10000, "steps": T.n_steps(10000), "data_sha256": "abc"}
    (tmp_results / "train" / key / "run.json").write_text(json.dumps(rec))
    assert P.run_ok(key) is None  # no adapter files
    (ad / "adapter_model.safetensors").write_bytes(b"x")
    (ad / "adapter_config.json").write_text("{}")
    assert P.run_ok(key)
    for bad in ({"steps": 3}, {"data_sha256": "zzz"}, {"status": "nonfinite"}):
        (tmp_results / "train" / key / "run.json").write_text(json.dumps(rec | bad))
        assert P.run_ok(key) is None, bad
    (tmp_results / "train" / key / "run.json").write_text(json.dumps(rec))
    (ad / "adapter_config.json").write_text("not json")
    assert P.run_ok(key) is None


def test_seedsets_follow_anchor_and_reuse(tmp_results, monkeypatch):
    monkeypatch.setattr(P, "seed0_reused", lambda: True)
    monkeypatch.setenv("P50_ASSUME_ANCHOR", "pass")
    m = P.block_seedsets("M")
    assert m[0] == ["swap_m90_r05_s0", "refill_m90_r05_s0"]
    assert sum(len(s) for s in m) == 8 and sum(len(s) for s in P.block_seedsets("C")) == 8
    monkeypatch.setenv("P50_ASSUME_ANCHOR", "fail")
    m = P.block_seedsets("M")
    assert sum(len(s) for s in m) == 9 and m[0][0] == "mix090ftn20k_s0"
    monkeypatch.setattr(P, "seed0_reused", lambda: False)
    monkeypatch.setenv("P50_ASSUME_ANCHOR", "pass")
    assert [s[0] for s in P.block_seedsets("C")] == [f"mix100ftn20k_s{i}" for i in (1, 2, 3)]
    keys = [k for b in ("A", "B1", "B2") for ss in P.block_seedsets(b) for k in ss]
    assert len(keys) == 16 and len(P.block_seedsets("F")) == 5


def test_fits_uses_deadline_for_20k_and_guard_for_10k(tmp_results, monkeypatch):
    monkeypatch.setattr(P, "seed0_reused", lambda: True)
    monkeypatch.setenv("P50_ASSUME_ANCHOR", "pass")
    monkeypatch.setattr(P, "run_ok", lambda key, n_rows=None: None)
    from datetime import datetime
    dl = datetime(2026, 10, 16, 14, 15, tzinfo=UTC).timestamp()
    monkeypatch.setenv("P50_GUARD_END", str(dl + 10 * 3600))
    need20 = P.projected_seconds(["swap_m90_r05_s0", "refill_m90_r05_s0"], 8)
    monkeypatch.setenv("P50_NOW", str(dl - need20 + 5))
    assert P.main(["fits", "--block", "M", "--keys", "swap_m90_r05_s0",
                   "refill_m90_r05_s0"]) == 3
    monkeypatch.setenv("P50_NOW", str(dl - need20 - 5))
    assert P.main(["fits", "--block", "M", "--keys", "swap_m90_r05_s0",
                   "refill_m90_r05_s0"]) == 0
    # a 10K set ignores the absolute deadline but not the guard
    monkeypatch.setenv("P50_NOW", str(dl + 3600))
    assert P.main(["fits", "--block", "B1", "--keys", "rewx_r05_s1", "refill_r05_s1"]) == 0
    monkeypatch.setenv("P50_GUARD_END", str(dl + 3600 + 60))
    assert P.main(["fits", "--block", "B1", "--keys", "rewx_r05_s1", "refill_r05_s1"]) == 3
    # the reservation includes the block's eval, a retry and finalisation
    assert need20 > 2 * 912 * P.STEP_FLOOR_S + C.ENGINE_START_S + C.FINALISE_S


def test_anchor_rule(tmp_results):
    (tmp_results / "eval").mkdir()
    p = tmp_results / "eval" / f"{C.ANCHOR_KEY}.summary.json"
    for rate, rc in ((26.08, 0), (30.0, 0), (30.2, 3), (21.9, 3)):
        p.write_text(json.dumps({"trait_rate": rate}))
        assert P.main(["anchor"]) == rc
        assert json.loads((tmp_results / "anchor.json").read_text())["pass"] == (rc == 0)


def test_venv_split_and_no_vllm_in_peft_steps():
    text = (_PROBE / "run.sh").read_text()
    for step, venv in P.VENV_BY_STEP.items():
        for m in re.finditer(rf'"\$(PEFT|VLLM)" (?:-m p50\.pod )?{step}\b', text):
            assert m.group(1).lower() == venv, (step, m.group(0))
    calls = re.findall(r'run_step (\w+) "\$(PEFT|VLLM)"', text)
    assert calls and all(P.VENV_BY_STEP[s] == v.lower() for s, v in calls)
    src = (_PROBE / "p50" / "pod.py").read_text()
    assert "import vllm" not in src and "from vllm" not in src


def test_run_sh_syntax_and_dry_run():
    assert subprocess.run(["bash", "-n", str(_PROBE / "run.sh")], check=False).returncode == 0
    if shutil.which("uv") is None:
        pytest.skip("no uv")
    out = subprocess.run(["bash", str(_PROBE / "run.sh")], capture_output=True, text=True, check=False,
                         env={"P50_DRY_RUN": "1", "P50_LAUNCH": "2026-10-16T05:00:00Z",
                              "PATH": "/usr/bin:/bin:" + str(Path(shutil.which("uv")).parent),
                              "HOME": str(Path.home())}, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    totals = re.findall(r"total (\d+) trainings", out.stdout)
    assert totals == ["32", "31"], out.stdout


def test_every_pushed_input_exists():
    import tomllib
    cfg = tomllib.loads((_PROBE / "probe.toml").read_text())
    root = _PROBE.parents[1]
    for p in cfg["pod"]["push"]:
        assert (root / p).exists(), p
    assert cfg["hard_cap_usd"] == 55 and cfg["gpu_limit_usd"] == 50
