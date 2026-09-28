# olens_and_ar (README / model card)

**Source URL:** https://huggingface.co/agu18dec/olens_and_ar
**Retrieved:** 2026-09-28

---

---
license: apache-2.0
tags:
  - interpretability
  - activation-oracle
  - qwen3
base_model: Qwen/Qwen3.6-27B
---

# OLens `s3d.rl600` + the AR it was trained against

Two LoRA adapters for **Qwen/Qwen3.6-27B**, copied from
[`agu18dec/local-workspace`](https://huggingface.co/datasets/agu18dec/local-workspace) so the
oracle lens and its reconstructor sit in one place.

| directory | what it is | source path in `local-workspace` |
|---|---|---|
| `olens_s3d_rl600/` | the **AO** (oracle lens): take a residual-stream activation, get prose about it | `ckpts/ao/rl/s3d.ddp600.s0/iter_000600` |
| `ar_ptag_pooled/` | the **AR** (activation reconstructor): take an activation, get back the text that produced it | `ckpts/ar/ptag/ar.asst.ptag.pooled.s0/ex16013184` |

They are a matched pair. `s3d.rl600` is 600 GRPO steps (β 0.02) from the `s3d.omp4bp.s2/step251`
bullet student, trained with a **joint bullet-FVE reward computed on this exact AR**. Scored
against a different reconstructor, the numbers below do not transfer.

---

## What each one does

**The AR is the ruler.** It answers "how much of the activation's content is recoverable at all?"
Feed it an activation and it reconstructs the span that produced it; fraction of variance
explained (FVE) is the score. It is a *measurement instrument*, not something you read.

**The AO is the lens.** It answers "what is in this activation, in words?" You inject one
activation into a fixed verbalizer prompt and sample; the output is a few bullets of prose.

You do not need the AR to use the AO. It is here because every FVE number quoted for this AO was
computed on it, and because an AO read against the wrong AR is a different experiment.

---

## Using the AO (the common case)

The whole read contract, which travels together or not at all:

```python
prompt_kind = "concepts_raw"   # the verbalizer prompt this student was trained on
transform   = "unit"           # inject  alpha * h/||h||  …
alpha       = 16000.0          # … into the marker slot ㈜ (token id 158983)
layers      = (20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60)   # trained band, 20–60 step 4
```

Reading a checkpoint with another one's transform, alpha or prompt is a different experiment, not
a noisier one. Take the whole contract, never individual numbers.

```python
import torch
from jlens.hooks import ActivationRecorder
from global_workspace.ola.verbalizer import renderer_for
from global_workspace.olens_suite.runner import (
    load_lens, make_sampler, merge_lens, resolve_blocks)

LAYERS = [20, 36, 44, 52, 60]

# 1. base model + AO adapter. load_lens ASSERTS the adapter is live and raises if it is inert —
#    an adapter that silently matches nothing is indistinguishable from a working one otherwise,
#    and this repo has paid for that mistake.
tok, model, probe_ids, on_logits, delta = load_lens(
    "Qwen/Qwen3.6-27B", "agu18dec/olens_and_ar:olens_s3d_rl600", "cuda")
blocks = resolve_blocks(model)

# 2. capture activations with the adapter OFF — you want the base model's state, not the lens's
text = "The capital of France is Paris."
ids = tok(text, add_special_tokens=False, return_tensors="pt").input_ids.to("cuda")
with torch.no_grad(), model.disable_adapter(), ActivationRecorder(blocks, at=LAYERS) as rec:
    model(ids, use_cache=False)
    resid = {L: rec.activations[L][0].float() for L in LAYERS}

# 3. merge the adapter (generation runs ~4x faster eager) and build the sampler
model, drift = merge_lens(model, probe_ids, on_logits)   # aborts if merging changed behaviour
wv = {L: renderer_for("concepts_raw")(tok, layer=L) for L in LAYERS}
sample = make_sampler(model, tok, wv, 0.0, "cuda", transform="unit", alpha=16000.0)

# 4. read. vecs is [n_positions, d_model]; returns [n_positions][k] strings
pos = [2, 5]
for L in LAYERS:
    for p, outs in zip(pos, sample(L, resid[L][pos], 2, 0, 128, 0.8, 0.95)):
        print(f"L{L} @{p} {tok.decode(ids[0, p])!r}: {outs[0]}")
```

Three things that are easy to get wrong:

- **Capture with `disable_adapter()`.** Reading the lens's own modified activations instead of
  the model's is a silent, plausible-looking error.
- **Sampling is deterministic given (activation, seed).** The seed is set once per call and
  batches consume the RNG in order, so re-sampling at the same seed reproduces the block exactly.
- **Layers outside 20–60 step 4 are off-contract.** The student never saw them.

## Using the AR

```python
from pathlib import Path
from global_workspace.ola.ar_loader import load_lc_reconstructor

ar = load_lc_reconstructor(Path("ar_ptag_pooled"), device="cuda")   # forward(ids, mask) -> [b, 17, d]
```

The directory layout matters: the loader expects `lora/` and `heads.pt` as siblings, which is how
this repo is arranged. It truncates the backbone to the deepest needed layer, compile-wraps the
blocks if the adapter carries `_orig_mod.` keys (without that step PEFT matches nothing and the
checkpoint scores at chance), then attaches the LoRA and the 17-row layer head.

**Not included: the pooled whitener moments** (`ckpts/ar/ptag/whiteners/` in the source repo — 69
files, 7.1 GB). They are needed to compute the *whitened* FVE metric, not to read: the AO injects
the raw un-whitened activation, so nothing above depends on them. Fetch them from
`local-workspace` if you are reproducing FVE numbers.

---

## The prompts

Neither adapter is a bare LoRA you can point at arbitrary text. Each one only works inside the
exact prompt it was trained in, and both prompts are reproduced here in full.

### AO — the verbalizer prompt (`concepts_raw`)

One user turn, chat-templated with `add_generation_prompt=True` and `enable_thinking=False`:

```
An activation vector from layer {layer} of a language model is enclosed in activation tags:
<activation>{char}</activation>. Produce distinct concepts that encode this activation, each as
a '- ' bullet on its own line.
```

`{layer}` is the layer the activation came from, as a plain integer. `{char}` is a single-token
**injection marker**: you render the prompt with that char in place, find its index in the token
ids, and overwrite that one row of the embedding matrix with `16000 · h/‖h‖`. The model never
sees the character — it sees the activation sitting where the character was.

**Do not hardcode the marker.** It is chosen by scanning U+3200–U+33FF for a character that
survives as exactly **one token in the full rendered prompt**, which is a stricter test than
tokenizing it alone: BPE is context-sensitive, and the first candidate this repo tried merged
with its neighbours once it was inside the chat template. For Qwen3.6-27B the scan lands on
**㈜ (id 158983)**, but let the search find it:

```python
from global_workspace.ola.verbalizer import renderer_for
p = renderer_for("concepts_raw")(tok, layer=44)
p.input_ids   # the rendered prompt
p.slot        # index of the row to overwrite with the activation
p.char_id     # 158983 for this tokenizer
```

The output is `- ` bullets, one concept per line, EOS-terminated. Deliberately no count in the
wording — the student was trained without one.

### AR — the layer tag

The ptag architecture puts **layer identity in the prompt, not the architecture**: there is no
layer embedding and no per-layer head, just one shared head and a text tag. The input is

```
[Layer 44] <the span of text to reconstruct>
```

and the read is the final hidden state at the span's **last real token**. The tag template is
`"[Layer {n:02d}]"` — zero-padded to two digits, so `[Layer 20]` … `[Layer 63]`.

You do not build these yourself: the pre-tokenized tags ship inside `heads.pt` as a frozen
`tag_ids` buffer, `[12 layers × 6 tokens]`, covering

```
[Layer 20] [Layer 24] [Layer 28] [Layer 32] [Layer 36] [Layer 40]
[Layer 44] [Layer 48] [Layer 52] [Layer 56] [Layer 60] [Layer 63]
```

Every row is the same length on purpose, so a batch keeps one static shape and
`torch.compile(dynamic=False)` compiles a single graph. The tag is **prepended**, which is what
keeps right-padding at the tail so `attention_mask.sum(1) - 1` still finds the last real token.

Call shapes: `layer_idx=li` → `[b, 1, d]` (one forward); `layer_idx=None` → `[b, 12, d]` (one
forward per layer).

Note the AR covers **L63** as well as the AO's 20–60 band — reconstruct there if you want, but
the AO was never trained to read it.

---

## Reported numbers

**AO** (`olens_s3d_rl600/run.json`): held-out joint FVE **0.487**, on this AR's ruler. *Not*
comparable to the `iolens.final.ddp600` lineage's 0.155 — different ruler.

**AR** (`ar_ptag_pooled/meta.json`): 16.0M examples / 259M span tokens, val FVE mean **0.226**,
ret@1 **0.94**. FVE rises with depth:

| layer | 20 | 28 | 36 | 44 | 52 | 60 |
|---|---|---|---|---|---|---|
| val FVE | 0.120 | 0.146 | 0.199 | 0.220 | 0.292 | 0.341 |

Worth having in front of you before reading a shallow layer as confidently as a deep one.

## Provenance

Copied verbatim. `README_original.md` in each directory is the file that shipped with the
checkpoint. The AO's `reference/` subdirectory — a byte-identical second copy of the same adapter
— is omitted deliberately. Registry rows and caveats, including that RL branches A (clipped-std)
and B (β 0.05) may supersede `iter_000600`, live in `docs/project/checkpoints.md` of the research
repo.
