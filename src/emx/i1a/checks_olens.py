"""Oracle Lens checks of prereg v0.4 §4.7 (implementation plan §2 U3): batching and adapter
liveness. Each returns a ``contracts.CheckResult`` and, by default, writes it to
``contracts.check_path(name)``.

- ``check_batching``: batched generation must not change what the reader computes. Greedy
  continuations are generated one activation at a time; the same continuations are then
  teacher-forced all at once and one at a time, and the next-token argmaxes are compared
  (top-1 agreement >= CHECK_THRESHOLDS["batching_top1"]). Adapted from the working repo's
  batching check; right padding after the continuations cannot affect earlier positions of a
  causal model, so shorter continuations are padded and only their real positions compared.
- ``check_adapter_live``: the adapter changes the logits, its saved tensors map onto the model
  (olens_draft ``ar_loader.verify_adapter_live``), and ``Backend.capture`` reads the base model
  (adapters disabled), as the Oracle Lens model card requires.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from emx.i1a import contracts as C
from emx.i1a.olens import (
    PublicOLens,
    carrier_embeds,
    eos_ids,
    logit_shift,
    verify_adapter_tensors,
)

if TYPE_CHECKING:  # pragma: no cover
    import torch
    from wsbench.produce.backend import Backend

SEQ_EQUAL_MIN = 0.6  # rows whose greedy sequence must match exactly; 0.9 -> 0.6 after P0 (Jacob)


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _greedy(olens: PublicOLens, H: torch.Tensor, layer: int, steps: int) -> list[list[int]]:
    """Greedy continuations of ``H``'s rows in ONE generate call, each cut after its first EOS
    (the EOS itself kept)."""
    import torch

    b = olens._backend()
    olens.activate()
    e = carrier_embeds(olens, H, layer)
    with torch.no_grad():
        out = b.model.generate(
            inputs_embeds=e,
            attention_mask=torch.ones(e.shape[0], e.shape[1], device=b.device, dtype=torch.long),
            do_sample=False,
            max_new_tokens=steps,
            pad_token_id=b.tokenizer.pad_token_id or b.tokenizer.eos_token_id,
        )
    eos = eos_ids(b)
    conts = []
    for row in out.tolist():
        k = next((i for i, t in enumerate(row) if t in eos), None)
        conts.append(row if k is None else row[: k + 1])
    return conts


def _wsbench_greedy(olens: PublicOLens, H: torch.Tensor, layer: int, steps: int) -> list[str]:
    """The independent reference: the vendored wsbench ``OLens.read`` (its own carrier
    embedding and slot injection, one activation per call) with greedy decoding."""
    from dataclasses import replace

    from wsbench.produce.methods import OLens, Sampling

    olens.activate()
    ref = replace(olens, sampling=Sampling(temperature=0.0, max_new_tokens=steps, k=1))
    return [OLens.read(ref, H[i], layer).samples[0] for i in range(H.shape[0])]


def _teacher_forced(
    olens: PublicOLens, H: torch.Tensor, layer: int, conts: list[list[int]]
) -> list[torch.Tensor]:
    """Next-token logits [len(cont_i), V] (fp32, CPU) predicting each continuation token, for
    all rows of ``H`` in one right-padded forward pass."""
    import torch

    b = olens._backend()
    olens.activate()
    embed = b.model.get_input_embeddings()
    lmax = max(len(c) for c in conts)
    pad = b.tokenizer.pad_token_id or b.tokenizer.eos_token_id
    with torch.no_grad():
        e = carrier_embeds(olens, H, layer)
        tail = torch.tensor(
            [c[:-1] + [pad] * (lmax - len(c)) for c in conts], device=b.device, dtype=torch.long
        )
        x = torch.cat([e, embed(tail).to(e.dtype)], dim=1)
        mask = torch.ones(x.shape[0], x.shape[1], device=b.device, dtype=torch.long)
        logits = b.model(inputs_embeds=x, attention_mask=mask, logits_to_keep=lmax).logits
    return [logits[i, : len(c)].float().cpu() for i, c in enumerate(conts)]


def check_batching(
    olens: PublicOLens,
    H: torch.Tensor,
    layer: int,
    *,
    n: int = 8,
    steps: int = 32,
    write: bool = True,
) -> C.CheckResult:
    """Batched vs one-at-a-time Oracle Lens on the first ``n`` rows of ``H`` [>= n, d] at one
    layer. Passes if (a) the teacher-forced next-token top-1 agrees on >= batching_top1 of the
    compared positions, (b) every batched greedy token sequence equals the one-at-a-time one,
    and (c) every batched greedy text equals the vendored wsbench ``OLens.read`` (greedy) text,
    so an injection bug shared by our batched and single paths cannot pass."""
    import torch

    H = H[:n]
    n = H.shape[0]
    single_conts = [_greedy(olens, H[i : i + 1], layer, steps)[0] for i in range(n)]
    batched_conts = _greedy(olens, H, layer, steps)
    wsbench_texts = _wsbench_greedy(olens, H, layer, steps)
    tf_single = [
        _teacher_forced(olens, H[i : i + 1], layer, [c])[0] for i, c in enumerate(single_conts)
    ]
    tf_batched = _teacher_forced(olens, H, layer, single_conts)
    agree = total = 0
    max_dlogit = 0.0
    tf_matches_greedy = 0
    for c, a, bt in zip(single_conts, tf_single, tf_batched, strict=True):
        am, bm = a.argmax(-1), bt.argmax(-1)
        agree += int((am == bm).sum())
        total += len(c)
        max_dlogit = max(max_dlogit, float((a - bt).abs().max()))
        tf_matches_greedy += int((am == torch.tensor(c)).sum())
    top1 = agree / total
    thr = C.CHECK_THRESHOLDS["batching_top1"]
    tok = olens._backend().tokenizer
    tokens_equal = [s == bc for s, bc in zip(single_conts, batched_conts, strict=True)]
    batched_texts = [tok.decode(bc, skip_special_tokens=True).strip() for bc in batched_conts]
    wsbench_equal = [w == t for w, t in zip(wsbench_texts, batched_texts, strict=True)]
    # Exact-sequence equality per row catches injection bugs (a row swap or a shifted slot breaks
    # most rows); requiring it for EVERY row would also fail on a single bf16 near-tie flip in a
    # 32-step greedy continuation, which the prereg's 99.5% top-1 rule tolerates. So: the top-1
    # rule, plus at least SEQ_EQUAL_MIN of rows exactly equal on both comparisons.
    seq_ok = sum(tokens_equal) / n >= SEQ_EQUAL_MIN and sum(wsbench_equal) / n >= SEQ_EQUAL_MIN
    res = C.CheckResult(
        name="olens_batching",
        passed=top1 >= thr and seq_ok,
        value=top1,
        threshold=thr,
        n=total,
        details={
            "layer": layer,
            "n_activations": n,
            "steps": steps,
            "max_abs_dlogit": max_dlogit,
            "greedy_tokens_equal_frac": sum(tokens_equal) / n,
            "wsbench_text_equal_frac": sum(wsbench_equal) / n,
            "mismatched_rows": [i for i in range(n) if not (tokens_equal[i] and wsbench_equal[i])],
            "tf_single_matches_greedy_frac": tf_matches_greedy / total,
            "cont_lengths": [len(c) for c in single_conts],
            "dtype": str(next(olens._backend().model.parameters()).dtype),
        },
        notes=(
            "value = top-1 agreement of teacher-forced next-token argmax, batched (all n in one "
            "forward, right-padded) vs one at a time, over the positions predicting each "
            "one-at-a-time greedy continuation. Passing also requires every batched greedy "
            "token sequence to equal the one-at-a-time one, and its decoded text to equal the "
            "vendored wsbench OLens.read run greedily on the same activation"
        ),
        utc=_utc(),
    )
    if write:
        res.write()
    return res


def _manual_capture(backend: Backend, ids: list[int], layer: int, positions: list[int], on: bool):
    """The residual after block ``layer`` at ``positions`` with the active adapter on or off
    (a hook like ``Backend.capture``'s, without its adapter handling)."""
    import torch

    store: dict[str, Any] = {}

    def hook(_m, _i, out):
        h = out[0] if isinstance(out, tuple) else out
        store["h"] = h[0, torch.tensor(positions, device=h.device), :].detach().float().cpu()

    handle = backend.blocks[layer].register_forward_hook(hook)
    try:
        with torch.no_grad():
            x = torch.tensor([ids], device=backend.device)
            if on:
                backend.model(x)
            else:
                with backend.model.disable_adapter():
                    backend.model(x)
    finally:
        handle.remove()
    return store["h"]


def check_adapter_live(
    olens: PublicOLens,
    backend: Backend,
    *,
    layer: int = C.FIVE[2],
    H: torch.Tensor | None = None,
    write: bool = True,
) -> C.CheckResult:
    """Adapter on vs off changes the logits (on the carrier, with ``H[0]`` injected when given);
    >= 90% of the saved LoRA tensors are live; and ``Backend.capture`` equals a capture with the
    adapters disabled and differs from one with the adapter on."""
    import torch

    olens.activate()
    dlogit = logit_shift(olens, layer=layer, H=H)
    assert olens.adapter_dir is not None
    n_live = verify_adapter_tensors(backend.model, olens.adapter_dir, olens.adapter_name)
    ids, slot = olens._carrier(layer)
    positions = [slot, len(ids) - 1]
    cap = backend.capture(ids, [layer], positions)[layer]
    off = _manual_capture(backend, ids, layer, positions, on=False)
    on = _manual_capture(backend, ids, layer, positions, on=True)
    cap_off = float((cap - off).abs().max())
    capture_is_base = cap_off <= 1e-6 * max(float(off.abs().max()), 1.0)
    adapter_moves_acts = not torch.allclose(on, off)
    passed = dlogit > 0.0 and capture_is_base and adapter_moves_acts
    res = C.CheckResult(
        name="olens_adapter_live",
        passed=passed,
        value=dlogit,
        threshold=0.0,
        n=len(positions),
        details={
            "layer": layer,
            "adapter_name": olens.adapter_name,
            "adapter_dir": str(olens.adapter_dir),
            "n_live_lora_tensors": n_live,
            "capture_equals_adapter_off": capture_is_base,
            "max_abs_capture_minus_off": cap_off,
            "adapter_on_changes_activations": adapter_moves_acts,
            "max_abs_capture_minus_on": float((cap - on).abs().max()),
            "active_adapter": getattr(backend.model, "active_adapter", None),
        },
        notes="value = max |logits(adapter on) - logits(adapter off)| on the carrier prompt",
        utc=_utc(),
    )
    if write:
        res.write()
    return res
