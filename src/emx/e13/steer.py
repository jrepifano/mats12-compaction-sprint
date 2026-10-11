"""E13 steering hooks, the gate, and the hook-correctness checks (prereg §4.3 "Steering", §4.6
item 7; Amendment B items 1 and 4; implementation plan §1 unit D).

Two sites on decoder block 14 (`contracts.SITES`):
- input  ("pre", 14): a forward PRE-hook (`with_kwargs=True`) replaces the block's input
  `hidden_states[14]` with h - alpha*g*u. The hidden states arrive as positional args[0] or as
  the `hidden_states` kwarg (transformers 5 may pass either); both are handled.
- output ("post", 14): a forward hook subtracts the same from the block's output
  (`hidden_states[15]`, after the rank-1 adapter's write in down_proj), tensor or tuple.
Every position, on the prefill call and on every decode call (Lu et al. 2026 §3.2.1; CAST's
behaviour vectors, Lee et al. 2025 App. A.2). `steering` ALWAYS installs its hook, at alpha 0
too; whether a batch is steered at all is the caller's per-batch gate (`hfgen.decode_requests`).

Ported from `emx.e1b.steer` (`SteerStats`, `steer_hook`: add at a layer's output on every call,
first call = prefill) with the sign and the input site added; `decoder_layers` from
`emx.e1b.firstk` (PEFT-safe). `SteerStats` is redefined here (same fields plus the per-call
sequence lengths) so this module imports without torch.

Hook checks (prereg §4.6 item 7, per site): (i) hook installed at alpha 0 -> HOOK_CHECK_PROMPTS x
HOOK_CHECK_SAMPLES seeded samples token-identical to no hook; (ii) at HOOK_CHECK_ALPHA the
residual at the site, hooked minus unhooked, equals -g*u at every non-pad position of the prefill
AND of every decode step, max error <= BF16_ATOL_REL x max|h| per position, |h| taken as the
larger of the unhooked and hooked residual there (implementation choice: bf16 rounding of the
edit scales with the result; with |h| = the unhooked residual alone, a correct hook failed on the
tiny bf16 model when g was comparable to |h|) (recorded by our own
recorder hooks registered AFTER the steering hook, because a pre-hook's edit is invisible in
`output_hidden_states`; the unhooked reference replays the hooked run's tokens through the same
decode loop); (iii) the hook fires once on the prefill (full padded width) and once per decode
step (sequence length 1); (iv) under the oracle gate an oversight batch gets zero hook calls,
with a no-oversight batch as the positive control.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from emx.contracts import request_seed
from emx.e13 import contracts as C


@dataclass
class SteerStats:
    """Calls of the steering hook: the first is the prefill, every later one a decode step
    (fields of `emx.e1b.steer.SteerStats`, plus each call's sequence length)."""

    calls: int = 0
    prefill_calls: int = 0
    decode_calls: int = 0
    prefill_len: int = 0
    seq_lens: list[int] = field(default_factory=list)

    def count(self, seq_len: int) -> None:
        self.calls += 1
        self.seq_lens.append(int(seq_len))
        if self.calls == 1:
            self.prefill_calls += 1
            self.prefill_len = int(seq_len)
        else:
            self.decode_calls += 1


def gate_on(gate: str, notice_cond: str) -> bool:
    """always -> True; oracle -> only under a no-oversight notice (`contracts.GATE_CONDS`)."""
    if gate not in C.GATES:
        raise ValueError(f"unknown gate {gate!r}")
    return gate == "always" or notice_cond in C.GATE_CONDS[gate]


def site_block(model, site: str):
    """(hook kind "pre"/"post", the decoder block) of a steering site."""
    from emx.e1b.firstk import decoder_layers

    if site not in C.SITES:
        raise ValueError(f"unknown site {site!r}")
    kind, layer = C.SITES[site]
    return kind, decoder_layers(model)[layer]


def _apply(h, delta):
    """The edit: h - alpha*g*u (module-level so tests can plant a broken hook)."""
    return h - delta


def _as_unit(u):
    import numpy as np
    import torch

    v = u.detach().float().cpu() if isinstance(u, torch.Tensor) else torch.from_numpy(
        np.asarray(u, dtype=np.float32).copy())
    if v.ndim != 1 or not torch.isfinite(v).all():
        raise ValueError("u must be a finite 1-D vector")
    if abs(float(v.norm()) - 1.0) > 1e-3:
        raise ValueError(f"u must be a unit vector, norm {float(v.norm()):.6f}")
    return v


@contextlib.contextmanager
def steering(model, site: str, u, g: float, alpha: float, stats: SteerStats) -> Iterator[None]:
    """Install the site's hook (always, alpha 0 included) for the duration of the block."""
    if not (math.isfinite(float(g)) and float(g) >= 0 and math.isfinite(float(alpha))):
        raise ValueError(f"bad strength: g {g}, alpha {alpha}")
    kind, block = site_block(model, site)
    vec = _as_unit(u) * (float(alpha) * float(g))  # float32 on the CPU
    cast: dict[tuple, Any] = {}

    def delta_for(h):
        key = (h.dtype, h.device)
        if key not in cast:  # cast to the hidden dtype/device once
            if h.shape[-1] != vec.shape[0]:
                raise ValueError(f"u has {vec.shape[0]} dims, the residual {h.shape[-1]}")
            cast[key] = vec.to(device=h.device, dtype=h.dtype)
        return cast[key]

    def pre_hook(_m, args, kwargs):
        if args:
            h = args[0]
            stats.count(h.shape[-2])
            return (_apply(h, delta_for(h)), *args[1:]), kwargs
        h = kwargs["hidden_states"]
        stats.count(h.shape[-2])
        return args, {**kwargs, "hidden_states": _apply(h, delta_for(h))}

    def post_hook(_m, _args, out):
        h = out[0] if isinstance(out, tuple) else out
        stats.count(h.shape[-2])
        h = _apply(h, delta_for(h))
        return (h, *out[1:]) if isinstance(out, tuple) else h

    if kind == "pre":
        handle = block.register_forward_pre_hook(pre_hook, with_kwargs=True)
    else:
        handle = block.register_forward_hook(post_hook)
    try:
        yield
    finally:
        handle.remove()


@contextlib.contextmanager
def recording(model, site: str, store: list, dtypes: list | None = None) -> Iterator[None]:
    """Append the site's residual as the block sees it (input site: the block's input after any
    earlier pre-hook; output site: the returned output after any earlier forward hook), float32 on
    the CPU, one tensor [batch, seq, d] per call. Register it AFTER `steering`. `dtypes`, if given,
    receives each call's original residual dtype (the float32 copy loses it)."""
    kind, block = site_block(model, site)

    def keep(h):
        store.append(h.detach().float().cpu().clone())
        if dtypes is not None:
            dtypes.append(h.dtype)

    def pre(_m, args, kwargs):
        keep(args[0] if args else kwargs["hidden_states"])

    def post(_m, _args, out):
        keep(out[0] if isinstance(out, tuple) else out)

    handle = (block.register_forward_pre_hook(pre, with_kwargs=True) if kind == "pre"
              else block.register_forward_hook(post))
    try:
        yield
    finally:
        handle.remove()


# --- hook checks ---------------------------------------------------------------------------------


def _prompt_ids(tok, prompts: Sequence) -> list[list[int]]:
    """Prompts as token-id lists: GenRequests (their prompt_ids), id lists, or rendered texts."""
    out = []
    for p in prompts:
        if isinstance(p, C.GenRequest):
            out.append([int(t) for t in p.prompt_ids])
        elif isinstance(p, str):
            out.append(list(tok(p, add_special_tokens=False)["input_ids"]))
        else:
            out.append([int(t) for t in p])
    return out


def _iii_ok(stats: SteerStats, width: int, n_forward: int) -> bool:
    return (stats.prefill_calls == 1 and stats.prefill_len == width
            and stats.decode_calls == n_forward - 1 and all(s == 1 for s in stats.seq_lens[1:]))


PROJ_REL_TOL = 0.1


def _check_ii(hooked: list, ref: list, prompts: list[list[int]], toks: list[list[int]],
              gu, hidden_dtype=None) -> tuple[float | None, float | None, bool]:
    """Per non-pad position: err = max|hooked - ref + g*u|, tol = BF16_ATOL_REL x max|h| with
    max|h| the larger of max|ref| and max|hooked| there (bf16 rounding scales with the result).
    Returns (err, tol) at the worst err/tol position and whether every position passed; (None,
    None, False) when the recorded calls do not line up (a hook that missed calls).
    `hidden_dtype`: the residual's dtype at the site (the recordings are float32 copies); the
    expected edit is computed in it, as the hook makes it. None = float32."""
    import torch

    hdt = hidden_dtype or torch.float32

    if len(hooked) != len(ref) or any(a.shape != b.shape for a, b in zip(hooked, ref)):
        return None, None, False
    # Code review r1 (pod finding 6): the per-coordinate tolerance alone can accept a no-op hook
    # when g*u is near the norm floor and the residual has outlier coordinates. The edit's
    # projection on u must also match the expected edit's (h - g*u computed in the residual's
    # dtype, minus h) within PROJ_REL_TOL; a no-op gives 0 against about -g.
    gnorm = float(gu.float().norm())
    unit = (gu.float() / gnorm) if gnorm > 0 else None
    width = max(len(p) for p in prompts)
    worst, ok = (0.0, 0.0, -1.0), True
    for c, (hk, rf) in enumerate(zip(hooked, ref, strict=True)):
        for i, p in enumerate(prompts):
            if c == 0:
                sel = slice(width - len(p), width)
            elif c < len(toks[i]):  # row i was still live when this token was fed
                sel = slice(0, 1)
            else:
                continue
            err = (hk[i, sel] - rf[i, sel] + gu).abs().amax(-1)
            # bf16 rounding scales with the result, so |h| is the larger of the unhooked and
            # the hooked residual at that position
            tol = C.BF16_ATOL_REL * torch.maximum(rf[i, sel].abs().amax(-1),
                                                  hk[i, sel].abs().amax(-1))
            ok = ok and bool((err <= tol).all())
            if unit is not None:
                # compare with the edit as the hook makes it in the residual's dtype (code review
                # r2: bf16 rounding of h - g*u can legitimately move the projection by > 10%).
                # The recordings are float32 copies, so the dtype comes from `hidden_dtype`
                # (2026-10-10, E13 Amendment E: computed in float32 it failed both sites at the
                # massive-activation prompt position, |h| = 11,712, where bf16 keeps 89% of the
                # edit; with bf16 it matched exactly at every position, results/E13/run2/hooks/
                # diag_*.json)
                exp = (rf[i, sel].to(hdt) - gu.to(device=rf.device, dtype=hdt)).float() \
                    - rf[i, sel].float()
                u_d = unit.to(hk.device)
                proj = (hk[i, sel].float() - rf[i, sel].float()) @ u_d
                proj_exp = exp @ u_d
                ok = ok and bool(((proj - proj_exp).abs()
                                  <= PROJ_REL_TOL * proj_exp.abs() + 1e-12).all()) \
                    and bool((proj_exp.abs() > 0).all())
            ratio = err / torch.clamp(tol, min=1e-30)
            j = int(ratio.argmax())
            if float(ratio[j]) > worst[2]:
                worst = (float(err[j]), float(tol[j]), float(ratio[j]))
    return worst[0], worst[1], ok


def _check_request(site: str, notice_cond: str, ids: list[int], k: int) -> C.GenRequest:
    """A minimal request for check (iv): only prompt_ids, seed, notice_cond, site, alpha matter."""
    cond = f"{C.EXP}|hook_check|{notice_cond}|{site}"
    return C.GenRequest(
        condition=cond, cell="", eval_fmt=C.EVAL_FMT, qset="general", qid=f"hc{k:02d}",
        question="", sample_idx=0, seed=request_seed(cond, f"hc{k:02d}", 0), prompt_text="",
        prompt_ids=tuple(ids), model_key="", battery="sweep", notice_cond=notice_cond,
        family="", instance_id="", system_suffix=None, site=site, alpha=C.HOOK_CHECK_ALPHA,
        task_type="em",
    )


def hook_checks(model, tok, site: str, u, g: float, prompts: Sequence, *,
                max_new_tokens: int | None = None, step_tokens: int = 16) -> dict:
    """Prereg §4.6 item 7 at one site (module docstring); returns HOOK_CHECK_KEYS. `prompts`:
    at least HOOK_CHECK_PROMPTS GenRequests, token-id lists or rendered texts (the first
    HOOK_CHECK_PROMPTS are used). (i) and (iii) decode up to `max_new_tokens` (default: the
    sampling setting, 600); (ii) and (iv) decode `step_tokens` per prompt."""
    import torch

    from emx.e13 import hfgen

    ids = _prompt_ids(tok, prompts)[: C.HOOK_CHECK_PROMPTS]
    if len(ids) < C.HOOK_CHECK_PROMPTS:
        raise ValueError(f"hook checks need {C.HOOK_CHECK_PROMPTS} prompts, got {len(ids)}")
    max_new = hfgen.MAX_NEW_TOKENS if max_new_tokens is None else int(max_new_tokens)
    cond = f"{C.EXP}|hook_check|{site}"

    # (i) + (iii): installed at alpha 0 vs no hook, prompts x samples in one batch
    rep = [p for p in ids for _ in range(C.HOOK_CHECK_SAMPLES)]
    seeds = [request_seed(cond, f"p{i:02d}", s) for i in range(len(ids))
             for s in range(C.HOOK_CHECK_SAMPLES)]
    ref = hfgen.decode_batch(model, rep, seeds, max_new_tokens=max_new)
    st_i = SteerStats()
    with steering(model, site, u, g, 0.0, st_i):
        got = hfgen.decode_batch(model, rep, seeds, max_new_tokens=max_new)
    i_identical = all(a.token_ids == b.token_ids and a.finish_reason == b.finish_reason
                      for a, b in zip(ref, got, strict=True))
    iii_i = _iii_ok(st_i, max(len(p) for p in rep), hfgen.n_forward_of(got))

    # (ii) + (iii): alpha HOOK_CHECK_ALPHA, recorded after the steering hook; reference replays
    seeds2 = [request_seed(cond, f"p{i:02d}", C.HOOK_CHECK_SAMPLES) for i in range(len(ids))]
    hooked, st_ii, hdts = [], SteerStats(), []
    with steering(model, site, u, g, C.HOOK_CHECK_ALPHA, st_ii), \
            recording(model, site, hooked, hdts):
        samples = hfgen.decode_batch(model, ids, seeds2, max_new_tokens=step_tokens)
    toks = [s.token_ids for s in samples]
    unhooked: list = []
    with recording(model, site, unhooked):
        hfgen.replay_batch(model, ids, toks)
    gu = (_as_unit(u) * (C.HOOK_CHECK_ALPHA * float(g))).to(torch.float32)
    if len(set(hdts)) > 1:
        raise ValueError(f"the residual changed dtype between calls: {sorted(map(str, set(hdts)))}")
    ii_err, ii_tol, ii_pass = _check_ii(hooked, unhooked, ids, toks, gu,
                                        hdts[0] if hdts else None)
    iii_ii = _iii_ok(st_ii, max(len(p) for p in ids), hfgen.n_forward_of(samples))

    # (iv): the per-batch gate code itself, oracle gate; no-oversight batch = positive control
    n_iv = min(4, len(ids))
    st_ov, st_noov = SteerStats(), SteerStats()
    for cond_iv, st in (("ov", st_ov), ("noov", st_noov)):
        reqs = [_check_request(site, cond_iv, ids[k], k) for k in range(n_iv)]
        hfgen.decode_requests(model, reqs, gate="oracle", steer_for=lambda _r: (u, g), stats=st,
                              max_new_tokens=step_tokens)
    iv_pass = st_ov.calls == 0 and st_noov.calls >= 1

    out = {
        "site": site,
        "i_identical": bool(i_identical),
        "ii_max_abs_err": ii_err,
        "ii_tol": ii_tol,
        "ii_pass": bool(ii_pass),
        "iii_prefill_calls": st_i.prefill_calls,
        "iii_decode_calls": st_i.decode_calls,
        "iii_pass": bool(iii_i and iii_ii),
        "iv_oracle_ov_calls": st_ov.calls,
        "iv_pass": bool(iv_pass),
    }
    out["passed"] = bool(i_identical and ii_pass and iii_i and iii_ii and iv_pass)
    return {k: out[k] for k in C.HOOK_CHECK_KEYS}
