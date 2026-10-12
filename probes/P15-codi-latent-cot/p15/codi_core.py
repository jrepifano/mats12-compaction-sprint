"""CoDI model loading and masked, batched greedy generation for P15 (pod code; no `emx`).

Conventions are frozen in `CONTRACTS.md` ("Model conventions"). The `CODI` module class comes from
the upstream repo github.com/cywinski/codi @ f0bf2fa (no license), imported at run time from
`upstream_dir` (or `$P15_UPSTREAM`, default `/ws/codi_upstream`); none of its code is copied here.
`load_codi` mirrors what upstream `CODI.from_pretrained` builds (src/model.py) but keeps the
state-dict key report. The generation loops reimplement upstream `CODI.generate` with an explicit
attention mask and position ids at every step (upstream passes neither after the prompt, so a
left-padded batch attends to padding) and greedy decoding in all three modes (upstream's direct and
verbal modes call HF `generate` without `do_sample=False`).

Works under transformers 4.52.4 / peft 0.15.2 / torch 2.7.1 (pod pins) and transformers 5.x.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import numpy as np
import torch

DEFAULT_UPSTREAM = "/ws/codi_upstream"
LORA_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "up_proj", "down_proj", "gate_proj"]
PATCHSCOPE_PROMPT = "cat → cat; 1135 → 1135; hello → hello; ? →"
N_LAYERS_PLUS_EMB = 17  # Llama-3.2-1B: embeddings + 16 layers (the tiny test model has 3)

_DTYPES = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}
_DIGITS = re.compile(r"[0-9]+")
_INT = re.compile(r"-?\d+")


# ----------------------------------------------------------------------------- loading


def import_upstream(upstream_dir: str | os.PathLike | None = None):
    """Import upstream `src.model` from the clone (sys.path), checking it is that clone's file."""
    up = Path(upstream_dir or os.environ.get("P15_UPSTREAM") or DEFAULT_UPSTREAM).resolve()
    if not (up / "src" / "model.py").is_file():
        raise FileNotFoundError(f"upstream CODI clone not found at {up} (src/model.py missing)")
    if str(up) not in sys.path:
        sys.path.insert(0, str(up))
    import src.model as upstream_model  # run-time import (unlicensed repo)

    got = Path(upstream_model.__file__).resolve()
    if got != (up / "src" / "model.py"):
        raise ImportError(f"`src.model` resolved to {got}, not the upstream clone at {up}")
    return upstream_model


def _ensure_special_tokens(tok) -> None:
    """Pad token and CoT delimiters as upstream (test.py / simple_example.py); idempotent."""
    if tok.pad_token_id is None:
        tok.add_special_tokens({"pad_token": "[PAD]"})
    vocab = tok.get_vocab()
    missing = [t for t in ("<|bocot|>", "<|eocot|>") if t not in vocab]
    if missing:
        tok.add_special_tokens({"additional_special_tokens": missing})
    tok.bocot_id = tok.convert_tokens_to_ids("<|bocot|>")
    tok.eocot_id = tok.convert_tokens_to_ids("<|eocot|>")
    tok.padding_side = "left"


def load_tokenizer(ckpt_dir: str | os.PathLike, base: str):
    """Upstream loads the tokenizer from the checkpoint dir (which already carries [PAD],
    <|bocot|>, <|eocot|>); fall back to the base model's tokenizer if the dir has none."""
    from transformers import AutoTokenizer

    src = ckpt_dir if (Path(ckpt_dir) / "tokenizer_config.json").is_file() else base
    tok = AutoTokenizer.from_pretrained(str(src))
    _ensure_special_tokens(tok)
    return tok


def build_codi(base: str, tok, upstream_dir=None, *, dtype: str = "bfloat16", lora_r: int = 128,
               lora_alpha: int = 32, prj_dim: int = 2048, num_latent: int = 6):
    """Construct upstream `CODI` with the arguments upstream `from_pretrained` uses (inference)."""
    from peft import LoraConfig, TaskType

    um = import_upstream(upstream_dir)
    model_args = um.ModelArguments(model_name_or_path=str(base), lora_r=lora_r,
                                   lora_alpha=lora_alpha, lora_init=True, full_precision=True,
                                   train=False, token=None)
    training_args = um.TrainingArguments(
        output_dir="./output", model_max_length=28000, num_latent=num_latent, use_lora=True,
        use_prj=True, prj_dim=prj_dim, prj_dropout=0.0, prj_no_ln=False, remove_eos=False,
        bf16=(dtype == "bfloat16"), use_cpu=not torch.cuda.is_available(),
    )
    lora_config = LoraConfig(task_type=TaskType.CAUSAL_LM, inference_mode=True, r=lora_r,
                             lora_alpha=lora_alpha, lora_dropout=0.1, target_modules=LORA_TARGETS,
                             init_lora_weights=True)
    return um.CODI(model_args, training_args, lora_config, tok)


def _read_state(path: Path) -> dict:
    if path.suffix == ".bin":
        return torch.load(path, map_location="cpu", weights_only=True)
    from safetensors.torch import load_file

    return load_file(str(path))


def load_codi(ckpt_dir, base, upstream_dir=None, *, device="cuda", dtype="bfloat16",
              trainables=None, lora_r=128, lora_alpha=32, prj_dim=2048, num_latent=6):
    """Build CODI as upstream does, load the checkpoint with strict=False and report the keys.

    Returns (model, tok, report) with report = {"missing": [...], "unexpected": [...],
    "n_loaded": int, "trainables_loaded": int}. The model is cast to `dtype` *before* loading, so a
    bf16 checkpoint loads exactly into a float32 model too (for bfloat16 this equals upstream).
    `trainables` (safetensors of CODI state-dict keys, e.g. LoRA + prj) is overlaid afterwards;
    every key must exist in the model.
    """
    tdtype = _DTYPES[dtype]
    tok = load_tokenizer(ckpt_dir, base)
    model = build_codi(base, tok, upstream_dir, dtype=dtype, lora_r=lora_r, lora_alpha=lora_alpha,
                       prj_dim=prj_dim, num_latent=num_latent)
    model.to(dtype=tdtype)
    ckpt = Path(ckpt_dir)
    files = [ckpt / "model.safetensors", ckpt / "pytorch_model.bin"]
    path = next((f for f in files if f.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"no model.safetensors or pytorch_model.bin in {ckpt}")
    state = _read_state(path)
    res = model.load_state_dict(state, strict=False)
    report = {"missing": sorted(res.missing_keys), "unexpected": sorted(res.unexpected_keys),
              "n_loaded": len(state) - len(res.unexpected_keys), "trainables_loaded": 0,
              "checkpoint_file": str(path)}
    del state
    if trainables is not None:
        extra = _read_state(Path(trainables))
        own = model.state_dict()
        bad = sorted(k for k in extra if k not in own)
        if bad:
            raise KeyError(f"trainables keys not in the model ({len(bad)}): {bad[:5]}")
        shape_bad = [k for k, v in extra.items() if tuple(v.shape) != tuple(own[k].shape)]
        if shape_bad:
            raise ValueError(f"trainables shape mismatch: {shape_bad[:5]}")
        model.load_state_dict(extra, strict=False)
        report["trainables_loaded"] = len(extra)
    model.codi.tie_weights()
    model.to(device=device, dtype=tdtype)
    model.eval()
    return model, tok, report


# ----------------------------------------------------------------------------- helpers


def _parts(model):
    """(peft-wrapped causal LM, input embedding, final norm, lm_head, vocab_size)."""
    lm = model.codi
    base = lm.get_base_model() if hasattr(lm, "get_base_model") else lm
    return lm, base.get_input_embeddings(), base.model.norm, base.get_output_embeddings(), \
        base.config.vocab_size


def _device(model):
    return next(model.parameters()).device


def _new_cache():
    from transformers import DynamicCache

    return DynamicCache()


def _encode(tok, prompts, suffix):
    """Token ids per prompt: tokenizer default (BOS) + `suffix` ids."""
    enc = tok(list(prompts), add_special_tokens=True)["input_ids"]
    return [list(ids) + list(suffix) for ids in enc]


def _left_pad(seqs, pad_id, device):
    n = max(len(s) for s in seqs)
    ids = torch.full((len(seqs), n), pad_id, dtype=torch.long)
    mask = torch.zeros((len(seqs), n), dtype=torch.long)
    for i, s in enumerate(seqs):
        ids[i, n - len(s):] = torch.tensor(s, dtype=torch.long)
        mask[i, n - len(s):] = 1
    pos = (mask.cumsum(-1) - 1).clamp(min=0)
    return ids.to(device), mask.to(device), pos.to(device)


def _batches(lengths, batch_size):
    """Index batches in length order (less padding); outputs are scattered back by index."""
    order = sorted(range(len(lengths)), key=lambda i: (lengths[i], i))
    return [order[i:i + batch_size] for i in range(0, len(order), batch_size)]


def stop_ids(tok) -> tuple[int, ...]:
    """Ids that end a generation: eos (<|eot_id|>) plus Llama's other end tokens if present.

    Upstream stops only at eos; a row that emits <|end_of_text|> would otherwise run on to the
    token limit, and the last-number parser would read whatever followed.
    """
    ids = [tok.eos_token_id]
    for t in ("<|end_of_text|>", "<|eom_id|>"):
        i = tok.convert_tokens_to_ids(t)
        if isinstance(i, int) and i != tok.unk_token_id and i not in ids:
            ids.append(i)
    return tuple(ids)


def _cut_at_eos(rows, stops):
    stops = set(stops if isinstance(stops, (tuple, list, set)) else (stops,))
    out = []
    for r in rows:
        r = list(r)
        cut = next((i for i, t in enumerate(r) if t in stops), len(r))
        out.append(r[:cut])
    return out


def decode(tok, ids) -> str:
    """Decoded generated text: special tokens kept, no clean-up (same under 4.52 and 5.x)."""
    return tok.decode(ids, skip_special_tokens=False, clean_up_tokenization_spaces=False)


class _Stepper:
    """One left-padded batch: carries the cache, mask and position ids across forwards."""

    def __init__(self, model, mask, pos, *, ids=None, embeds=None, hidden=True):
        self.lm, self.emb, self.norm, self.head, self.vocab = _parts(model)
        self.model = model
        self.cache = _new_cache()
        self.mask = mask
        self.next_pos = pos[:, -1:] + 1
        kw = {"input_ids": ids} if ids is not None else {"inputs_embeds": embeds}
        self.out = self.lm(**kw, attention_mask=mask, position_ids=pos,
                           past_key_values=self.cache, use_cache=True,
                           output_hidden_states=hidden, logits_to_keep=1)

    def step(self, *, ids=None, embeds=None, hidden=False):
        b = self.mask.shape[0]
        self.mask = torch.cat([self.mask, self.mask.new_ones((b, 1))], dim=1)
        kw = {"input_ids": ids} if ids is not None else {"inputs_embeds": embeds}
        self.out = self.lm(**kw, attention_mask=self.mask, position_ids=self.next_pos,
                           past_key_values=self.cache, use_cache=True,
                           output_hidden_states=hidden, logits_to_keep=1)
        self.next_pos = self.next_pos + 1
        return self.out

    def last_resid(self):
        """(B, n_hidden, d) residual stream at the last position, all hidden-state entries."""
        return torch.stack([h[:, -1] for h in self.out.hidden_states], dim=1)

    def latent(self, resid):
        """prj(final hidden state) cast back to the model dtype, as upstream."""
        h = resid[:, -1:, :]
        return self.model.prj(h).to(dtype=h.dtype)

    def lens_top5(self, resid):
        """Logit-lens top-5 ids over the full vocab: final norm on layers 0..L-1, none on L."""
        x = torch.cat([self.norm(resid[:, :-1]), resid[:, -1:]], dim=1)
        return self.head(x).topk(5, dim=-1).indices.to(torch.int32)

    def greedy(self, max_new, eos, vocab_cut):
        """Greedy decode from the current logits; returns (B, T) token ids on CPU."""
        b = self.mask.shape[0]
        toks = []
        done = torch.zeros(b, dtype=torch.bool, device=self.mask.device)
        logits = self.out.logits[:, -1]
        for t in range(max_new):
            nxt = (logits[:, :vocab_cut] if vocab_cut else logits).argmax(-1)
            toks.append(nxt)
            done |= torch.isin(nxt, torch.as_tensor(eos, device=nxt.device))
            if t == max_new - 1 or bool(done.all()):
                break
            logits = self.step(ids=nxt[:, None]).logits[:, -1]
        return torch.stack(toks, dim=1).cpu()


def _np16(t):
    return t.to(torch.float16).cpu().numpy()


# ----------------------------------------------------------------------------- modes


def run_latent(model, tok, prompts, *, batch_size=128, n_latent=6, max_new_tokens=16,
               capture_resid=True, capture_lltop=True, patch_index=None, patch_vectors=None,
               patch_mask=None):
    """Latent mode: prompt + [eos, <|bocot|>], n_latent latent iterations, <|eocot|>, answer.

    latent_vectors[:, j] is latent vector j as fed to the model (after any patch); column j of
    `resid`/`lltop` is the position that produces latent vector j. Patching overwrites latent
    vector `patch_index` (the input to column patch_index + 1) with `patch_vectors[i]` for rows
    with `patch_mask[i]` (default: all rows); `patch_vectors`/`patch_mask` are aligned with
    `prompts` (shape (N, d) and (N,)).
    """
    dev = _device(model)
    n = len(prompts)
    if patch_index is not None:
        if not 0 <= patch_index < n_latent:
            raise ValueError(f"patch_index must be in 0..{n_latent - 1} (vector j feeds column j+1)")
        patch_vectors = torch.as_tensor(np.asarray(patch_vectors) if not torch.is_tensor(
            patch_vectors) else patch_vectors)
        if patch_vectors.shape[0] != n:
            raise ValueError("patch_vectors must have one row per prompt")
        patch_mask = (torch.ones(n, dtype=torch.bool) if patch_mask is None
                      else torch.as_tensor(np.asarray(patch_mask), dtype=torch.bool))
    seqs = _encode(tok, prompts, [tok.eos_token_id, tok.bocot_id])
    _, _, _, _, vocab = _parts(model)
    ncol = n_latent + 1
    token_ids = [None] * n
    lat_out = resid_out = ll_out = None
    with torch.inference_mode():
        for idx in _batches([len(s) for s in seqs], batch_size):
            ids, mask, pos = _left_pad([seqs[i] for i in idx], tok.pad_token_id, dev)
            st = _Stepper(model, mask, pos, ids=ids)
            lats, resids, lls = [], [], []
            for j in range(ncol):
                if j > 0:
                    st.step(embeds=lats[-1], hidden=True)
                r = st.last_resid()
                if capture_resid:
                    resids.append(_np16(r))
                if capture_lltop:
                    lls.append(st.lens_top5(r).cpu().numpy())
                v = st.latent(r)
                if patch_index == j:
                    sel = patch_mask[idx].to(dev)
                    pv = patch_vectors[idx].to(dev, dtype=v.dtype)[:, None, :]
                    v = torch.where(sel[:, None, None], pv, v)
                lats.append(v)
            emb = st.emb(torch.full((len(idx), 1), tok.eocot_id, dtype=torch.long, device=dev))
            st.step(embeds=emb)
            gen = st.greedy(max_new_tokens, stop_ids(tok), vocab - 1)
            for k, row in zip(idx, _cut_at_eos(gen.tolist(), stop_ids(tok))):
                token_ids[k] = row
            lv = _np16(torch.cat(lats, dim=1))
            if lat_out is None:
                d = lv.shape[-1]
                lat_out = np.zeros((n, ncol, d), np.float16)
                if capture_resid:
                    resid_out = np.zeros((n, resids[0].shape[1], ncol, d), np.float16)
                if capture_lltop:
                    ll_out = np.zeros((n, lls[0].shape[1], ncol, 5), np.int32)
            lat_out[idx] = lv
            if capture_resid:
                resid_out[idx] = np.stack(resids, axis=2)
            if capture_lltop:
                ll_out[idx] = np.stack(lls, axis=2)
            del st, lats
    return {"texts": [decode(tok, t) for t in token_ids], "token_ids": token_ids,
            "latent_vectors": lat_out, "resid": resid_out, "lltop": ll_out}


def run_direct(model, tok, prompts, *, batch_size=128, max_new_tokens=16, capture_qresid=True):
    """Direct mode: prompt + [eos, <|eocot|>], then the answer (last vocab entry masked).

    qresid[:, :, 0] is the last question token (before the appended eos), [:, :, 1] the
    <|eocot|> position."""
    dev = _device(model)
    n = len(prompts)
    seqs = _encode(tok, prompts, [tok.eos_token_id, tok.eocot_id])
    _, _, _, _, vocab = _parts(model)
    token_ids = [None] * n
    q_out = None
    with torch.inference_mode():
        for idx in _batches([len(s) for s in seqs], batch_size):
            ids, mask, pos = _left_pad([seqs[i] for i in idx], tok.pad_token_id, dev)
            st = _Stepper(model, mask, pos, ids=ids, hidden=capture_qresid)
            if capture_qresid:
                q = torch.stack([torch.stack([h[:, -3], h[:, -1]], dim=1)
                                 for h in st.out.hidden_states], dim=1)
                q = _np16(q)
                if q_out is None:
                    q_out = np.zeros((n,) + q.shape[1:], np.float16)
                q_out[idx] = q
                del q
            gen = st.greedy(max_new_tokens, stop_ids(tok), vocab - 1)
            for k, row in zip(idx, _cut_at_eos(gen.tolist(), stop_ids(tok))):
                token_ids[k] = row
            del st
    return {"texts": [decode(tok, t) for t in token_ids], "token_ids": token_ids,
            "qresid": q_out, "lltop_q": None}


def run_verbal(model, tok, prompts, *, batch_size=64, max_new_tokens=256):
    """Verbal mode: prompt + [eos, <|bocot|>], greedy text CoT over the full vocab."""
    dev = _device(model)
    seqs = _encode(tok, prompts, [tok.eos_token_id, tok.bocot_id])
    token_ids = [None] * len(prompts)
    with torch.inference_mode():
        for idx in _batches([len(s) for s in seqs], batch_size):
            ids, mask, pos = _left_pad([seqs[i] for i in idx], tok.pad_token_id, dev)
            st = _Stepper(model, mask, pos, ids=ids, hidden=False)
            gen = st.greedy(max_new_tokens, stop_ids(tok), None)
            for k, row in zip(idx, _cut_at_eos(gen.tolist(), stop_ids(tok))):
                token_ids[k] = row
            del st
    return {"texts": [decode(tok, t) for t in token_ids], "token_ids": token_ids}


# ----------------------------------------------------------------------------- readers


def patchscope_prompt(tok):
    """(ids, q_index): the Patchscope prompt's ids (with BOS) and the position of its "?"."""
    ids = tok(PATCHSCOPE_PROMPT, add_special_tokens=True)["input_ids"]
    hits = [i for i, t in enumerate(ids) if tok.decode([t]).strip() == "?"]
    if len(hits) != 1:
        raise ValueError(f"expected one '?' token in the Patchscope prompt, found {hits}")
    return list(ids), hits[0]


def first_int(text):
    m = _INT.search(text)
    return int(m.group()) if m else None


def patchscope(model, tok, vectors, *, batch_size=256, max_new_tokens=8):
    """Replace the input embedding of "?" in the identity prompt by each vector; greedy decode."""
    dev = _device(model)
    _, emb, _, _, _ = _parts(model)
    ids, q = patchscope_prompt(tok)
    vecs = torch.as_tensor(np.asarray(vectors)) if not torch.is_tensor(vectors) else vectors
    token_ids = []
    with torch.inference_mode():
        base = emb(torch.tensor([ids], device=dev))
        for s in range(0, vecs.shape[0], batch_size):
            v = vecs[s:s + batch_size].to(dev, dtype=base.dtype)
            b = v.shape[0]
            x = base.expand(b, -1, -1).clone()
            x[:, q] = v
            mask = torch.ones((b, len(ids)), dtype=torch.long, device=dev)
            pos = torch.arange(len(ids), device=dev).expand(b, -1)
            st = _Stepper(model, mask, pos, embeds=x, hidden=False)
            gen = st.greedy(max_new_tokens, stop_ids(tok), None)
            token_ids += _cut_at_eos(gen.tolist(), stop_ids(tok))
            del st
    texts = [decode(tok, t) for t in token_ids]
    return {"texts": texts, "numbers": [first_int(t) for t in texts]}


def number_tokens(tok) -> dict[int, int]:
    """{token_id: value} for every vocab token decoding to a pure ASCII digit string (no space)."""
    n = len(tok)
    texts = tok.batch_decode([[i] for i in range(n)], skip_special_tokens=False,
                             clean_up_tokenization_spaces=False)
    return {i: int(t) for i, t in enumerate(texts) if _DIGITS.fullmatch(t)}


# ----------------------------------------------------------------------------- references


def upstream_latent_bs1(model, tok, prompt, *, n_latent=6, max_new_tokens=16) -> str:
    """Upstream `CODI.generate(greedy=True)` on one unpadded prompt, decoded as ours."""
    dev = _device(model)
    ids = torch.tensor([tok(prompt, add_special_tokens=True)["input_ids"]], device=dev)
    with torch.inference_mode():
        out = model.generate(input_ids=ids, attention_mask=torch.ones_like(ids), tokenizer=tok,
                             max_new_tokens=max_new_tokens, num_latent_iterations=n_latent,
                             greedy=True, return_latent_vectors=False, remove_eos=False,
                             sot_token=tok.bocot_id, eot_token=tok.eocot_id)
    row = _cut_at_eos([out["sequences"][0].tolist()], stop_ids(tok))[0]
    return decode(tok, row)


def hf_generate_bs1(model, tok, prompt, mode, *, max_new_tokens) -> str:
    """HF `generate` (do_sample=False, stop at tok.eos only) at batch 1 for direct/verbal.

    Direct mode suppresses the last vocab entry, matching our decode (upstream's direct mode,
    which goes through HF generate, does not)."""
    if mode not in ("direct", "verbal"):
        raise ValueError(mode)
    dev = _device(model)
    _, _, _, _, vocab = _parts(model)
    suffix = [tok.eos_token_id, tok.eocot_id if mode == "direct" else tok.bocot_id]
    ids = torch.tensor(_encode(tok, [prompt], suffix), device=dev)
    kw = {"suppress_tokens": [vocab - 1]} if mode == "direct" else {}
    with torch.inference_mode():
        out = model.codi.generate(input_ids=ids, attention_mask=torch.ones_like(ids),
                                  max_new_tokens=max_new_tokens, do_sample=False, num_beams=1,
                                  eos_token_id=list(stop_ids(tok)), pad_token_id=tok.pad_token_id,
                                  temperature=None, top_p=None, top_k=None, **kw)
    row = _cut_at_eos([out[0, ids.shape[1]:].tolist()], stop_ids(tok))[0]
    return decode(tok, row)
