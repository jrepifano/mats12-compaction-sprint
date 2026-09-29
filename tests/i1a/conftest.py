"""Shared I1a test fixtures: a tiny random Qwen3.6-architecture model, dummy Oracle Lens and
reconstructor checkpoints in the published layouts, fake whiteners and J-Lens, and the
benchmark's fake LLM client. Everything is offline except the real Qwen3.6 tokenizer, which is
read from the local HF cache (fetched once at the pinned revision; the tests skip without it).

The tiny model keeps the real structure where the code under test depends on it: 64 decoder
blocks (so the benchmark's layer numbers 20..60 and 63 are valid), the 3:1 linear/full attention
pattern, every LoRA target module name, `Qwen3_5ForCausalLM` with `model.layers`, and the real
248,320-row vocabulary. Only the widths shrink (d = 64).

The wsbench isolation fixtures and `FakeClient` / `fake_llm` are ported from
third_party/workspace-bench/tests/conftest.py @ 92d763e (MIT), unchanged in behaviour.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from emx.i1a import contracts as C

D_TINY = 64
LORA_TARGETS = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "in_proj_qkv",
    "in_proj_z",
    "in_proj_a",
    "in_proj_b",
    "out_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
)
# heads.pt tag_ids of ar_ptag_pooled (meta.json config.tag_ids_json), rows for AR_LAYERS
AR_TAG_IDS = [
    [58, 8915, 220, 17, 15, 60],
    [58, 8915, 220, 17, 19, 60],
    [58, 8915, 220, 17, 23, 60],
    [58, 8915, 220, 18, 17, 60],
    [58, 8915, 220, 18, 21, 60],
    [58, 8915, 220, 19, 15, 60],
    [58, 8915, 220, 19, 19, 60],
    [58, 8915, 220, 19, 23, 60],
    [58, 8915, 220, 20, 17, 60],
    [58, 8915, 220, 20, 21, 60],
    [58, 8915, 220, 21, 15, 60],
    [58, 8915, 220, 21, 18, 60],
]


# --------------------------------------------------------------------- wsbench isolation (port)


@pytest.fixture(autouse=True)
def _isolate_wsbench():
    from wsbench import llm, mcjudge, registry

    saved = dict(registry.FAMILIES)
    mcjudge._PREFLIGHTED.clear()
    llm._PACER.reset()
    yield
    registry.FAMILIES.clear()
    registry.FAMILIES.update(saved)
    mcjudge._PREFLIGHTED.clear()
    llm._PACER.reset()


class BadRequestError(Exception):
    status_code = 400  # neither transient nor fatal: one failed call, no retry


class FakeClient:
    """An OpenRouter-shaped fake: ``responder(system, user)`` returns the JSON object the model
    "wrote" (``None`` -> a non-transient error). ``cost`` is reported as usage.cost per call."""

    def __init__(self, responder, cost: float = 0.001):
        from types import SimpleNamespace

        self.responder = responder
        self.cost = cost
        self.calls: list[tuple[str, str]] = []
        self.kwargs: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        from types import SimpleNamespace

        system, user = kw["messages"][0]["content"], kw["messages"][1]["content"]
        self.calls.append((system, user))
        self.kwargs.append(kw)
        r = self.responder(system, user)
        if r is None:
            raise BadRequestError("fake failure")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(r)))],
            usage=SimpleNamespace(cost=self.cost),
        )

    async def close(self):
        return None


@pytest.fixture
def fake_llm(monkeypatch):
    """Install a FakeClient behind ``wsbench.llm._make_client``; returns
    ``install(responder, cost=0.001) -> fake``. No network, no key."""
    from wsbench import llm

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("WSBENCH_JUDGE_MODEL", raising=False)
    monkeypatch.setattr(llm, "_backoff", lambda attempt, status: 0.0)

    async def no_pace(rpm: float) -> None:
        return None

    monkeypatch.setattr(llm, "_pace", no_pace)

    def install(responder, cost: float = 0.001):
        fake = FakeClient(responder, cost)
        monkeypatch.setattr(llm, "_make_client", lambda route, key: fake)
        return fake

    return install


# --------------------------------------------------------------------- tokenizer and tiny model


@pytest.fixture(scope="session")
def qwen_tok():
    """The real Qwen3.6-27B tokenizer at MODEL_REV (local HF cache first)."""
    from transformers import AutoTokenizer

    try:
        return AutoTokenizer.from_pretrained(
            C.MODEL_ID, revision=C.MODEL_REV, local_files_only=True
        )
    except Exception:  # noqa: BLE001 - not cached locally
        try:
            return AutoTokenizer.from_pretrained(C.MODEL_ID, revision=C.MODEL_REV)
        except Exception as e:  # noqa: BLE001 - offline and not cached
            pytest.skip(f"Qwen3.6 tokenizer unavailable: {e}")


def tiny_config():
    from transformers.models.qwen3_5 import Qwen3_5TextConfig

    types = ["linear_attention"] * 3 + ["full_attention"]
    return Qwen3_5TextConfig(
        hidden_size=D_TINY,
        intermediate_size=128,
        num_hidden_layers=C.N_MODEL_LAYERS,
        head_dim=16,
        num_attention_heads=4,
        num_key_value_heads=2,
        linear_key_head_dim=16,
        linear_value_head_dim=16,
        linear_num_key_heads=2,
        linear_num_value_heads=4,
        vocab_size=248320,
        layer_types=types * (C.N_MODEL_LAYERS // 4),
        tie_word_embeddings=False,
        bos_token_id=248044,
        eos_token_id=248046,
        pad_token_id=248044,
    )


@pytest.fixture(scope="session")
def _tiny_model_master():
    torch = pytest.importorskip("torch")
    from transformers import AutoModelForCausalLM

    torch.manual_seed(0)
    m = AutoModelForCausalLM.from_config(tiny_config(), attn_implementation=C.ATTN_IMPLEMENTATION)
    m.eval()
    assert type(m).__name__ == "Qwen3_5ForCausalLM"
    return m


@pytest.fixture
def tiny_model(_tiny_model_master):
    """A fresh fp32 copy per test (methods may wrap it in a PeftModel)."""
    return copy.deepcopy(_tiny_model_master)


@pytest.fixture
def tiny_backend(tiny_model, qwen_tok):
    """wsbench's Backend around the tiny model, on CPU."""
    from wsbench.produce.backend import Backend

    return Backend(model=tiny_model, tokenizer=qwen_tok, device="cpu", model_id="tiny-qwen3_5")


# --------------------------------------------------------------------- dummy checkpoints


def _save_lora(model, out_dir: Path, seed: int) -> Path:
    import torch
    from peft import LoraConfig, get_peft_model

    torch.manual_seed(seed)
    cfg = LoraConfig(r=4, lora_alpha=8, target_modules=list(LORA_TARGETS), init_lora_weights=False)
    peft = get_peft_model(copy.deepcopy(model), cfg)
    with torch.no_grad():  # large enough to move the logits visibly
        for n, p in peft.named_parameters():
            if "lora_B" in n:
                p.normal_(0.0, 0.5)
    peft.save_pretrained(str(out_dir))
    return out_dir


@pytest.fixture(scope="session")
def dummy_olens_dir(_tiny_model_master, tmp_path_factory) -> Path:
    """<root>/olens_s3d_rl600/{adapter_config.json, adapter_model.safetensors, run.json}, keys in
    the published layout base_model.model.model.layers.N.* (no _orig_mod)."""
    root = tmp_path_factory.mktemp("olens_and_ar")
    sub = _save_lora(_tiny_model_master, root / C.OLENS_SUBDIR, seed=1)
    (sub / "run.json").write_text(
        json.dumps({"summary": {"eval_joint_fve_heldout": C.JOINT_FVE_HELDOUT}})
    )
    return sub


@pytest.fixture(scope="session")
def dummy_ar_dir(_tiny_model_master, tmp_path_factory) -> Path:
    """<root>/ar_ptag_pooled/{heads.pt, meta.json, lora/} in the published layout: the LoRA keys
    carry the compiled, truncated-inner prefix base_model.model.layers.N._orig_mod.* exactly as
    the real reconstructor's do, so the loader's key remap is exercised."""
    import torch
    from safetensors.torch import load_file, save_file

    root = tmp_path_factory.mktemp("olens_and_ar_ar")
    sub = root / C.AR_SUBDIR
    lora = _save_lora(_tiny_model_master, sub / "lora", seed=2)
    st = lora / "adapter_model.safetensors"
    tensors = load_file(str(st))
    renamed = {}
    for k, v in tensors.items():
        assert k.startswith("base_model.model.model.layers."), k
        rest = k[len("base_model.model.model.layers.") :]
        idx, tail = rest.split(".", 1)
        renamed[f"base_model.model.layers.{idx}._orig_mod.{tail}"] = v
    save_file(renamed, str(st))
    torch.manual_seed(3)
    head = torch.nn.Linear(D_TINY, D_TINY, bias=True)
    torch.save(
        {
            "tag_ids": torch.tensor(AR_TAG_IDS, dtype=torch.long),
            "head": {"linear.weight": head.weight.detach(), "linear.bias": head.bias.detach()},
            "layers": list(C.AR_LAYERS),
        },
        sub / "heads.pt",
    )
    meta = {
        "val_fve_mean": 0.2263459414243698,
        "config": {
            "head_mode": "prompt_tag",
            "head_layer_norm": True,
            "whitener_prefix": C.WHITENER_PREFIX,
            "ridge_c": C.RIDGE_C,
            "layer_indices": list(range(5, 17)),
            "tag_template": C.AR_TAG_TEMPLATE,
            "tag_width": 6,
            "tag_ids_json": json.dumps(AR_TAG_IDS),
            "min_len": 1,
            "max_len": 32,
        },
    }
    (sub / "meta.json").write_text(json.dumps(meta, indent=1))
    return sub


@pytest.fixture(scope="session")
def fake_whitener_dir(tmp_path_factory) -> Path:
    """17 files <prefix>_L{layer}.safetensors with fp32 `mu` [d] and SPD `cov` [d, d]."""
    import torch
    from safetensors.torch import save_file

    d = tmp_path_factory.mktemp("whiteners") / "pooled"
    d.mkdir()
    g = torch.Generator().manual_seed(4)
    for layer in C.WHITENER_LAYERS:
        a = torch.randn(D_TINY, D_TINY, generator=g)
        cov = a @ a.T / D_TINY + 0.1 * torch.eye(D_TINY)
        mu = torch.randn(D_TINY, generator=g) * 0.1
        save_file(
            {"mu": mu.float(), "cov": cov.float()},
            str(d / f"{C.WHITENER_PREFIX}_L{layer}.safetensors"),
        )
    (d / f"{C.WHITENER_PREFIX}_provenance.json").write_text("{}")
    return d


@pytest.fixture(scope="session")
def fake_jlens_path(tmp_path_factory) -> Path:
    """A J-Lens .pt in the official layout: {"J": {layer: [d, d]}} for layers 0..63."""
    import torch

    g = torch.Generator().manual_seed(5)
    jac = {
        L: torch.eye(D_TINY) + 0.1 * torch.randn(D_TINY, D_TINY, generator=g)
        for L in range(C.N_MODEL_LAYERS)
    }
    p = tmp_path_factory.mktemp("jlens") / "jlens_n1000.pt"
    torch.save({"J": jac}, p)
    return p


@pytest.fixture
def i1a_tmp_layout(tmp_path, monkeypatch):
    """Redirect every contracts path under tmp_path (DATA, RESULTS and the per-kind dirs)."""
    data, results = tmp_path / "data" / "I1a", tmp_path / "results" / "I1a"
    monkeypatch.setattr(C, "DATA", data)
    monkeypatch.setattr(C, "RESULTS", results)
    for name, sub in [
        ("PLAN_DIR", data / "plan"),
        ("ACTS_DIR", results / "acts"),
        ("READOUTS_DIR", results / "readouts"),
        ("TOPK_DIR", results / "topk"),
        ("SAMPLES_DIR", results / "samples"),
        ("FVE_DIR", results / "fve"),
        ("CHECKS_DIR", results / "checks"),
        ("JUDGED_DIR", results / "judged"),
        ("ANALYSIS_DIR", results / "analysis"),
        ("MANIFEST_DIR", results),
    ]:
        monkeypatch.setattr(C, name, sub)
    monkeypatch.setenv("EMX_COSTS_LEDGER", str(tmp_path / "costs.jsonl"))
    return tmp_path
