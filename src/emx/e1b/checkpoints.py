"""E1b dense-checkpoint callback: save, adapter norm and contrast-set log-probabilities.

Implements docs/experiments/E1b/implementation-plan.md §1.1 (Hub layout, dtypes), §1.5 (checkpoint
rows) and §1.6 (log-probability CSV), for prereg §4.4 (dense tier) and §4.8 (log-probability gap).
`make_hook(...)` returns the `on_checkpoint(step, model)` callback that `emx.train.train` calls at
step 0 (log-probabilities only) and after each checkpoint step's update. Everything runs
in-process on the training model: no adapter is reloaded.

- **Save.** `saved_dtype` (`emx.e1b.contracts`) decides: timing steps of bad runs in fp32, other
  steps <= 200 in bf16, the rest not saved. The final step is the root adapter, which the trainer
  saves (fp32), so the callback returns its row (subfolder `""`, dtype fp32) without saving a
  duplicate. Checkpoints go to `{output_dir}/step-NNNN/` as a PEFT adapter folder
  (`adapter_config.json` + `adapter_model.safetensors`, weights cast to the dtype), loadable by
  `PeftModel.from_pretrained` and vLLM; the trainer's single `upload_folder` pushes them all.
- **Adapter norm.** Soligo et al. 2026 §3.2 define efficiency with the "parameter norm" ||θ||_2
  of the solution and rescale "LoRA adapters to a range of different parameter norms" (Fig. 4a,
  "Adapter Norm"), without saying whether θ is the A and B factors or the weight update they make
  (papers/em_personas/Emergent Misalignment is Easy, Narrow Misalignment is Hard.pdf, §3.2.1,
  App. K). We therefore use the Frobenius norm of the full LoRA weight update, summed in
  quadrature over modules: sqrt(Σ_m ||s B_m A_m||_F²), s the module's (rsLoRA) scaling. It is
  computed through r x r Gram matrices, ||s B A||_F² = s² Σ (BᵀB ⊙ AAᵀ), so no d x d product is
  formed.
- **Log-probabilities.** The contrast set must have its frozen sha256 (`data/E1b/manifest.json`,
  `contrast_set` block; prereg §4.8), checked before it is read. Each (question, answer) is masked
  once, at hook creation, in the plain format with `emx.masking.mask_example` (the training masking). The stored answer
  is E1's raw plain-format response, which keeps its leading space; exactly one leading space is
  stripped before masking (`plain_answer`), so that the rendered " {answer}" is the text the model
  generated rather than a double space. At each call the
  answers are batched with `emx.train.collate` and run with no grad in eval mode; the summed
  log-probability of the trained label tokens, excluding the final end-of-text token, and that
  token count are appended to the CSV (`LOGPROB_COLUMNS`), which is flushed after every call so
  a failed run keeps its rows. The model's train/eval mode is restored.
"""

from __future__ import annotations

import csv
import json
import math
import time
from pathlib import Path

from emx.contracts import IGNORE_INDEX, MaskedExample, TrainConfig
from emx.e1b import contracts as C

FMT = "plain"  # Part 1 trains and evaluates in plain text only (prereg §4.2)
LOGPROB_BATCH = 8  # as emx.train.EVAL_BATCH
DTYPES = ("fp32", "bf16")


def saved_dtype(step: int, data: str, total_steps: int) -> str | None:
    """`emx.e1b.contracts.saved_dtype` (the frozen rule), re-exported for the callback."""
    return C.saved_dtype(step, data, total_steps)


# --- adapter ------------------------------------------------------------------------------------


def _lora_pairs(model):
    """(A weight, B weight, scaling) of every LoRA module; exactly one adapter per module."""
    from emx.train import lora_modules

    mods = dict(model.named_modules())
    out = []
    for name in lora_modules(model):
        m = mods[name]
        names = list(m.lora_A.keys())
        if len(names) != 1:
            raise AssertionError(f"{name}: expected one adapter, found {names}")
        a = names[0]
        out.append((m.lora_A[a].weight, m.lora_B[a].weight, float(m.scaling[a])))
    if not out:
        raise AssertionError("model has no LoRA modules")
    return out


def adapter_norm(model) -> float:
    """sqrt(Σ_m ||s_m B_m A_m||_F²) over every LoRA module (see the module docstring)."""
    import torch

    total = 0.0
    with torch.no_grad():
        for A, B, s in _lora_pairs(model):
            a, b = A.detach().float(), B.detach().float()
            total += s * s * float(((b.T @ b) * (a @ a.T)).sum())
    return math.sqrt(max(total, 0.0))


def save_adapter(model, path: str | Path, dtype: str) -> None:
    """Save the LoRA weights, cast to `dtype`, as a PEFT adapter folder at `path`."""
    import torch

    if dtype not in DTYPES:
        raise ValueError(dtype)
    td = torch.float32 if dtype == "fp32" else torch.bfloat16
    sd = {k: v.detach().to(td) for k, v in model.state_dict().items() if "lora_" in k}
    if not sd:
        raise AssertionError("model has no LoRA parameters")
    model.save_pretrained(str(path), state_dict=sd)


# --- contrast-set log-probabilities -------------------------------------------------------------


def load_contrast_set(path: str | Path) -> list[dict]:
    """Rows of `data/E1b/contrast_set.jsonl` with exactly `CONTRAST_COLUMNS`; aids unique."""
    rows = [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
    for r in rows:
        if set(r) != set(C.CONTRAST_COLUMNS):
            raise ValueError(f"{path}: columns {sorted(r)} != {sorted(C.CONTRAST_COLUMNS)}")
        if r["kind"] not in ("em", "aligned"):
            raise ValueError(f"{path}: kind {r['kind']!r}")
    aids = [r["aid"] for r in rows]
    if not rows or len(set(aids)) != len(aids):
        raise ValueError(f"{path}: empty or duplicate aid")
    return rows


def plain_answer(answer: str) -> str:
    """The contrast set stores E1's raw plain-format `response`, which keeps the leading space the
    model generated after "Assistant:". The plain completion template is " {answer}", so exactly
    one leading space is stripped (if present): the rendered text is then what the model produced,
    not a double space."""
    return answer.removeprefix(" ")


def mask_contrast(rows: list[dict], stage: str, tokenizer, max_len: int) -> list[MaskedExample]:
    """Each answer (one leading space stripped, `plain_answer`) masked as a plain-format training
    example (id = aid)."""
    from emx.masking import mask_example

    return [
        mask_example(
            FMT, stage, r["aid"], r["question"], plain_answer(r["answer"]), tokenizer, max_len
        )
        for r in rows
    ]


def answer_logprobs(
    model, examples: list[MaskedExample], batch: int = LOGPROB_BATCH
) -> list[tuple[float, int]]:
    """(sum log p, n tokens) per example over its trained label tokens except the final
    end-of-text token, in input order. No grad, eval mode; the model's mode is restored."""
    import torch

    from emx.train import _device, collate

    was = model.training
    model.eval()
    dev = _device(model)
    out: list[tuple[float, int] | None] = [None] * len(examples)
    order = sorted(range(len(examples)), key=lambda i: len(examples[i].input_ids))
    try:
        with torch.no_grad():
            for s in range(0, len(order), batch):
                idx = order[s : s + batch]
                ids, labels, attn = collate([examples[i] for i in idx])
                logits = model(input_ids=ids.to(dev), attention_mask=attn.to(dev)).logits
                tgt = labels[:, 1:].to(dev)  # label t+1 is predicted at position t
                keep = tgt != IGNORE_INDEX
                for r, i in enumerate(idx):
                    keep[r, len(examples[i].input_ids) - 2] = False  # the final end-of-text
                lp = torch.log_softmax(logits[:, :-1].float(), dim=-1)
                tok = lp.gather(-1, tgt.clamp(min=0).unsqueeze(-1)).squeeze(-1)
                sums = torch.where(keep, tok, torch.zeros_like(tok)).sum(-1).tolist()
                counts = keep.sum(-1).tolist()
                for r, i in enumerate(idx):
                    out[i] = (float(sums[r]), int(counts[r]))
    finally:
        model.train(was)
    return out  # type: ignore[return-value]


# --- the callback -------------------------------------------------------------------------------


class CheckpointHook:
    """`on_checkpoint(step, model)` for one run; see the module docstring. Returns the checkpoint
    row (`CHECKPOINT_KEYS`; `eval_loss` and `train_loss_window` are left None for the trainer,
    `seconds` is this call's wall time) or None at step 0."""

    def __init__(
        self,
        output_dir: str | Path,
        data: str,
        total_steps: int,
        rows: list[dict],
        examples: list[MaskedExample],
        logprob_path: str | Path,
        steps=(),
        batch: int = LOGPROB_BATCH,
    ):
        self.output_dir = Path(output_dir)
        self.data = data
        self.total_steps = total_steps
        self.rows = rows
        self.examples = examples
        self.batch = batch
        for s in steps:  # fail before training, not at the checkpoint
            saved_dtype(s, data, total_steps)
        self.logprob_path = Path(logprob_path)
        self.logprob_path.parent.mkdir(parents=True, exist_ok=True)
        with self.logprob_path.open("w", newline="") as f:
            csv.writer(f).writerow(C.LOGPROB_COLUMNS)

    def write_logprobs(self, step: int, model) -> None:
        lps = answer_logprobs(model, self.examples, self.batch)
        with self.logprob_path.open("a", newline="") as f:
            w = csv.writer(f)
            for r, (s, n) in zip(self.rows, lps, strict=True):
                w.writerow([step, r["aid"], r["qid"], r["kind"], repr(s), n])
            f.flush()

    def __call__(self, step: int, model) -> dict | None:
        t0 = time.time()
        if step == 0:
            self.write_logprobs(0, model)
            return None
        dtype = saved_dtype(step, self.data, self.total_steps)
        sub = C.ROOT_SUBFOLDER  # not saved, or the root (saved by the trainer) at the final step
        if dtype is not None and step != self.total_steps:
            sub = C.subfolder(step)
            save_adapter(model, self.output_dir / sub, dtype)
        norm = adapter_norm(model)
        self.write_logprobs(step, model)
        return {
            "step": step,
            "subfolder": sub,
            "dtype": dtype,
            "eval_loss": None,
            "adapter_norm": norm,
            "train_loss_window": None,
            "seconds": time.time() - t0,
        }


def make_hook(
    cfg: TrainConfig,
    tokenizer,
    total_steps: int,
    steps=(),
    contrast_set: str | Path = C.CONTRAST_SET,
    logprob_path: str | Path | None = None,
    batch: int = LOGPROB_BATCH,
    manifest: str | Path | None = None,
) -> CheckpointHook:
    """The callback for one run of `cfg` whose last trained step is `total_steps`. `steps` (the
    checkpoint steps) are checked against `saved_dtype` now; the contrast set's sha256 is checked
    against the `contrast_set` block of the data manifest (`manifest`, default
    `emx.gen_common.DATA_MANIFEST`), then it is masked once."""
    from emx.contracts import CellId
    from emx.gen_common import verify_data_file

    cell = CellId.parse(cfg.cell)
    if cell.untrained or cell.fmt != FMT or cfg.fmt != FMT:
        raise ValueError(f"{cfg.cell}: E1b dense checkpoints are plain-format runs")
    verify_data_file(contrast_set, "contrast_set", manifest)
    rows = load_contrast_set(contrast_set)
    examples = mask_contrast(rows, cfg.stage, tokenizer, cfg.max_len)
    return CheckpointHook(
        cfg.output_dir,
        cell.data,
        total_steps,
        rows,
        examples,
        logprob_path if logprob_path is not None else C.logprob_path(cfg.cell),
        steps,
        batch,
    )
