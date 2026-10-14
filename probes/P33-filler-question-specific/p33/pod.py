"""P33 Stage 1 pod run (plan.md): DeepSeek-V4-Flash, then Gemma 4 31B-it, on one B300.

    python -m p33.pod download   # V4 Flash @ its pin (P06 F7/F8's download, with retries)
    python -m p33.pod run        # V4: load, smoke, mask check, bulk; then Gemma: the same

Reuses P06's V4 path (`p06.v4pilot`: load, smoke_with_fixes, greedy, capture) and P06 F3's
Gemma pins and helpers (`p06.stage2`: model_files, text_parts, grade_int, Timer), imported.

**V4 phase.** Every item (200 equation, 100 copy; `items.load_items` order) runs every arm of
`items.V4_ARMS`, greedy, up to MAX_NEW tokens, batch 1. Masked arms run the dots arm's tokens
inside `masks.V4Masks`; each masked prefill's hook counts must equal `masks.v4_expected_counts`
(a mismatch fails the run, rc 1).
**Mask check** (before the bulk, rc 3 on failure), on MASK_CHECK_N equation items at 100 dots:
the target turn's literal values are changed (same token count) and the dots' layer outputs
are captured during the prefill with and without the mask. Per layer, the masked relative
difference must be <= MASK_RATIO_MAX x the unmasked one (Mask B and Mask Q).
**API check** (rc 3): after the first API_CHECK_N equation items, the local dots100 - base
uplift has a lower bound > 0 and is >= API_UPLIFT_FRAC x P06 F1's API uplift on the same items
(`api_check`; replaced a 10-point accuracy tolerance after run 2, log.md).
**Stage 2 on the pod:** after a complete V4 bulk, the Stage 1 gate (`p33.analyze.gate`) is
computed here and, if it passes, `p33.stage2.run_loaded` runs on the loaded model, before Gemma.
**Resume:** tracked rows in resume/<model>_answers.jsonl seed results/ (`seed_resume`).
**Gemma phase** (the download runs in a thread during the V4 phase): `items.GEMMA_ARMS`, every
arm inside `masks.GemmaMasks` (explicit masks on the same kernel path; rules for mask arms),
the same mask check. A Gemma failure is recorded and does not fail the V4 results (rc 5).

Stops: OVERRUN x the projection, or T0 + gpu_hours - STOP_MARGIN_S (rc 4). Exceptions -> rc 1
(results/error.txt). A rerun skips every (model, item_id, arm) already in answers.jsonl.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import threading
import time
import tomllib
from pathlib import Path
from typing import Any

import numpy as np
from p06 import encoding_dsv4 as E
from p06 import stage2 as S2
from p06 import v4pilot as V
from p06.stage2 import Timer, append_jsonl, grade_int, utc, write_json

from . import PROBE
from . import items as I
from . import masks as M

RESULTS = PROBE / "results"
OUT = {"v4": RESULTS / "v4", "gemma": RESULTS / "gemma"}
ANSWERS = {k: v / "answers.jsonl" for k, v in OUT.items()}
API_ROWS = PROBE.parent / "P06-filler-token-computation" / "results" / "stage1" / \
    "f1_brauer_v4flash.jsonl"
MAX_NEW = 6
MASK_CHECK_N = 3
MASK_RATIO_MAX = 0.05
API_CHECK_N = 60
API_UPLIFT_FRAC = 0.5
RESUME = PROBE / "resume"  # tracked copies of earlier sessions' rows (pushed with the probe)
OVERRUN = 1.5
STOP_MARGIN_S = 12 * 60
GEMMA_DIR = Path(os.environ.get("P33_ARTIFACTS", "/ws/p33_artifacts")) / "gemma"
# F7/F8 medians: ~0.7 s (600 tokens) to ~2.4 s (3,500 tokens) per V4 prompt; Gemma ~4x faster.
PROJ_V4_ITEM_S = 17.0
PROJ_GEMMA_ITEM_S = 3.0


def log(msg: str) -> None:
    print(f"[p33.pod {utc()}] {msg}", flush=True)


def record_error(step: str, e: BaseException) -> None:
    V.record_error(step, e, RESULTS)


# --------------------------------------------------------------------- prompts


def v4_spans(tok: Any, it: dict, shots: dict, arm: str, neutral: str) -> I.Spans:
    text = E.encode_messages(I.messages(it["ex"], shots[it["kind"]], arm, neutral),
                             thinking_mode="chat")
    return I.spans(tok, text, E.USER_SP_TOKEN, arm)


def gemma_spans(tok: Any, it: dict, shots: dict, arm: str, neutral: str) -> I.Spans:
    msgs = I.messages(it["ex"], shots[it["kind"]], arm, neutral)
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    return I.spans(tok, text, "<|turn>user", arm)


def perturbed(ex: dict, seed: int = 0) -> dict:
    """The example with every literal value changed to another with the same digit count (a
    same-length change to the target turn's text before the dots)."""
    rng = np.random.default_rng(seed + ex["idx"])
    defs = []
    for n, v in ex["definitions"]:
        if isinstance(v, int):
            d = len(str(v))
            lo, hi = (0, 9) if d == 1 else (10 ** (d - 1), 10 ** d - 1)
            w = v
            while w == v:
                w = int(rng.integers(lo, hi + 1))
            v = w
        defs.append([n, v])
    return {**ex, "definitions": defs}


# --------------------------------------------------------------------- generation


class Model:
    """One loaded model and how to run a prompt on it (V4 or Gemma)."""

    def __init__(self, name: str, ctx: Any, tok: Any, blocks: Any, layer_types: list[str] | None,
                 rates: dict | None, device: str = "cuda") -> None:
        self.name, self.ctx, self.tok, self.blocks = name, ctx, tok, blocks
        self.layer_types, self.rates, self.device = layer_types, rates, device

    @property
    def model(self) -> Any:
        return self.ctx.model

    def masks(self, rules: list, n: int):
        if self.name == "v4":
            return M.V4Masks(self.model, rules, n)
        return M.GemmaMasks(self.blocks, rules, n)

    def generate(self, ids: list[int], rules: list, max_new: int = MAX_NEW) -> tuple[list, dict]:
        """Greedy decode; masked when rules are given (V4), always inside GemmaMasks (Gemma).
        Returns (new ids, hook counts)."""
        import torch

        if self.name == "v4" and not rules:
            new, _, _ = V.greedy(self.model, ids, self.ctx.int_ids, self.device, max_new=max_new)
            return new, {}
        with self.masks(rules, len(ids)) as mk:
            if self.name == "v4":
                new, _, _ = V.greedy(self.model, ids, self.ctx.int_ids, self.device,
                                     max_new=max_new)
            else:
                t = torch.tensor([ids], device=self.device)
                with torch.inference_mode():
                    out = self.model.generate(input_ids=t, attention_mask=torch.ones_like(t),
                                              max_new_tokens=max_new, do_sample=False)
                new = [int(x) for x in out[0, len(ids):].tolist()]
        return new, mk.count.as_dict()

    def expected(self, rules: list, n: int) -> dict:
        if self.name == "v4":
            return M.v4_expected_counts(self.layer_types, rules, n, self.rates)
        want = {"window": {str(i): 1 for i in range(len(self.blocks))
                           if any(r.on(i) for r in rules)}} if rules else {}
        return want

    def counts_ok(self, got: dict, rules: list, n: int) -> bool:
        want = self.expected(rules, n)
        got = {k: v for k, v in got.items() if k != "built"}
        return got == want

    def capture(self, ids: list[int], positions: np.ndarray, rules: list) -> Any:
        """Every layer's output at `positions` during the prefill of a 1-token decode:
        [L, P, ...] float32 on the GPU."""
        import torch

        pos = torch.as_tensor(positions, device=self.device)
        with S2.capture(self.blocks, pos, len(ids)) as hs:
            self.generate(ids, rules, max_new=1)
        if any(h is None for h in hs):
            raise RuntimeError("a decoder-layer hook captured nothing")
        return torch.stack([h.float() for h in hs])


def mask_check(m: Model, its: list[dict], shots: dict, neutral: str, span_fn) -> dict:
    """Mask B and Mask Q at 100 dots: per layer, the dots' relative change under a same-length
    change of the target turn's literals, masked vs unmasked."""
    out: dict[str, Any] = {"items": [], "ratio_max": {}, "counts_ok": True}
    for it in its:
        sp = span_fn(m.tok, it, shots, "dots100", neutral)
        it2 = {**it, "ex": perturbed(it["ex"])}
        sp2 = span_fn(m.tok, it2, shots, "dots100", neutral)
        if len(sp2.ids) != len(sp.ids) or (sp2.f0, sp2.f1) != (sp.f0, sp.f1):
            raise ValueError(f"{it['item_id']}: the perturbed prompt changed the token layout")
        diff = [i for i, (a, b) in enumerate(zip(sp.ids, sp2.ids, strict=True)) if a != b]
        if not diff or min(diff) < sp.turn0 or max(diff) >= sp.f0:
            raise ValueError(f"{it['item_id']}: perturbation outside the target block: {diff}")
        pos = np.arange(sp.f0, sp.f1)
        rec: dict[str, Any] = {"item_id": it["item_id"], "n_changed_tokens": len(diff)}
        ha, hb = m.capture(sp.ids, pos, []), m.capture(sp2.ids, pos, [])
        un = rel_diff(ha, hb)
        rec["unmasked"] = un
        for arm in ("maskB100", "maskQ100"):
            rules = I.mask_rules(arm, sp)
            _new, cnt = m.generate(sp.ids, rules, max_new=1)
            ok = m.counts_ok(cnt, rules, len(sp.ids))
            out["counts_ok"] &= ok
            ma, mb = m.capture(sp.ids, pos, rules), m.capture(sp2.ids, pos, rules)
            md = rel_diff(ma, mb)
            ratio = [a / b if b > 0 else (0.0 if a == 0 else float("inf"))
                     for a, b in zip(md, un, strict=True)]
            rec[arm] = {"rel_diff": md, "ratio": ratio, "counts_ok": ok, "counts": cnt}
            out["ratio_max"][arm] = max(out["ratio_max"].get(arm, 0.0), max(ratio))
        del ha, hb
        out["items"].append(rec)
    out["pass"] = bool(out["counts_ok"] and all(v <= MASK_RATIO_MAX
                                                for v in out["ratio_max"].values()))
    return out


def rel_diff(a: Any, b: Any) -> list[float]:
    """Per layer ||a - b|| / ||a|| over all captured positions and features."""
    L = a.shape[0]
    d = (a - b).reshape(L, -1).norm(dim=1) / a.reshape(L, -1).norm(dim=1).clamp_min(1e-12)
    return [round(float(x), 6) for x in d.cpu()]


# --------------------------------------------------------------------- bulk


def done_keys(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    return {(r["item_id"], r["arm"]) for r in map(json.loads, path.read_text().splitlines())
            if r}


def api_reference(path: Path = API_ROWS) -> dict[tuple[int, str], bool]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if r.get("draw", 0) == 0 and r["arm"] in ("base", "dots100"):
                out[(int(r["item_id"].split(":")[1]), r["arm"])] = bool(r["correct"])
    return out


def api_check(rows: list[dict], ref: dict, n: int = API_CHECK_N) -> dict:
    """P06 F7's rule (log.md, after Stage 1 run 2): on the first n equation items, the local
    dots100 - base uplift has a bootstrap lower bound > 0 and is >= API_UPLIFT_FRAC x the API's
    uplift on the same items. Accuracies and row agreement are reported per arm."""
    out: dict[str, Any] = {}
    loc: dict[str, dict[int, bool]] = {}
    for arm in ("base", "dots100"):
        rs = [r for r in rows if r["kind"] == "equation" and r["arm"] == arm][:n]
        loc[arm] = {r["idx"]: bool(r["correct"]) for r in rs}
        pairs = [(c, ref[(i, arm)]) for i, c in loc[arm].items() if (i, arm) in ref]
        if pairs:
            out[arm] = {"n": len(pairs), "local": round(sum(a for a, _ in pairs) / len(pairs), 3),
                        "api": round(sum(b for _, b in pairs) / len(pairs), 3),
                        "row_agreement": round(sum(a == b for a, b in pairs) / len(pairs), 3)}
    idx = sorted(i for i in loc["base"] if i in loc["dots100"] and (i, "base") in ref
                 and (i, "dots100") in ref)
    if not idx:
        return {**out, "pass": False, "reason": "no paired items"}
    d_loc = np.array([loc["dots100"][i] - loc["base"][i] for i in idx], dtype=float)
    d_api = np.array([ref[(i, "dots100")] - ref[(i, "base")] for i in idx], dtype=float)
    bs = d_loc[np.random.default_rng(0).integers(0, len(idx), (10_000, len(idx)))].mean(axis=1)
    lo = float(np.percentile(bs, 2.5))
    out["uplift"] = {"n": len(idx), "local": round(float(d_loc.mean()), 3),
                     "local_ci_lo": round(lo, 3), "api": round(float(d_api.mean()), 3)}
    out["pass"] = bool(lo > 0 and d_loc.mean() >= API_UPLIFT_FRAC * d_api.mean())
    return out


def run_bulk(m: Model, items: list[dict], shots: dict, arms: tuple[str, ...], neutral: str,
             span_fn, st: dict, deadline: float | None, proj_item_s: float,
             api_ref: dict | None) -> int:
    """Every item x arm; returns 0, or 3 (API check failed), 4 (stopped), 1 (hook mismatch)."""
    path = ANSWERS[m.name]
    done = done_keys(path)
    rows: list[dict] = ([json.loads(x) for x in path.read_text().splitlines() if x.strip()]
                        if path.exists() else [])
    item_s: list[float] = []
    start = time.time()
    proj = len(items) * proj_item_s
    stop_at = start + OVERRUN * proj
    sst = st.setdefault(m.name, {})
    sst["projection_s"] = {"initial": round(proj)}
    checked = api_ref is None
    for i, it in enumerate(items):
        if time.time() > stop_at or (deadline is not None and time.time() > deadline):
            sst["stopped"] = ("overrun" if time.time() > stop_at else "deadline") + \
                f" after {i} items"
            return 4
        t_item = time.time()
        ran = False
        for arm in arms:
            if (it["item_id"], arm) in done:
                continue
            ran = True
            sp = span_fn(m.tok, it, shots, arm, neutral)
            rules = I.mask_rules(arm, sp)
            t0 = time.time()
            new, cnt = m.generate(sp.ids, rules)
            V._sync()
            gen_s = time.time() - t0
            if rules and not m.counts_ok(cnt, rules, len(sp.ids)):
                sst["hook_mismatch"] = {"item_id": it["item_id"], "arm": arm, "counts": cnt}
                return 1
            text = m.tok.decode(new, skip_special_tokens=True)
            row = {"model": m.name, "item_id": it["item_id"], "idx": it["idx"],
                   "kind": it["kind"], "arm": arm, "answer": it["answer"],
                   **grade_int(text, it["answer"]), "gen_text": text, "gen_ids": new,
                   "n_prompt": len(sp.ids), "turn0": sp.turn0, "f0": sp.f0, "f1": sp.f1,
                   "suffix0": sp.suffix0, "n_filler": sp.n_filler(), "gen_s": round(gen_s, 3),
                   "utc": utc()}
            append_jsonl(path, row)
            rows.append(row)
        if ran:  # resumed items take ~0 s and must not set the projection (session 3, log.md)
            item_s.append(time.time() - t_item)
        if ran and len(item_s) == 6:
            med = statistics.median(item_s[1:])
            proj = time.time() - start + med * (len(items) - i - 1)
            stop_at = start + OVERRUN * proj
            sst["projection_s"].update(updated=round(proj), item_median_s=round(med, 2))
            log(f"P33_PROJECTION {m.name} {proj / 60:.1f} min (item median {med:.1f} s)")
        n_eq = sum(1 for x in items[: i + 1] if x["kind"] == "equation")
        if not checked and n_eq >= API_CHECK_N:
            checked = True
            ac = api_check(rows, api_ref)
            sst["api_check"] = ac
            log(f"P33_GATE api_check {json.dumps(ac)}")
            if not ac["pass"]:
                return 3
        if (i + 1) % 15 == 0:
            log(f"{m.name} {i + 1}/{len(items)} items, last "
                f"{item_s[-1] if item_s else 0.0:.1f} s, timed {len(item_s)}")
    sst["items_done"] = len(item_s)
    sst["item_s_median"] = round(statistics.median(item_s), 2) if item_s else None
    return 0


# --------------------------------------------------------------------- phases


def budget_deadline() -> float | None:
    if os.environ.get("P33_DEADLINE_EPOCH"):  # a relaunch on the same pod (run.sh, log.md)
        return float(os.environ["P33_DEADLINE_EPOCH"])
    t0 = os.environ.get("P33_T0")
    if not t0:
        return None
    gpu_h = float(tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["gpu_hours"])
    return float(t0) + gpu_h * 3600 - STOP_MARGIN_S


def seed_resume(model: str) -> None:
    """Copy a tracked earlier session's rows into results/ (the runner never pushes results/;
    memory: probe-pod-second-session-results) so run_bulk skips them."""
    src = RESUME / f"{model}_answers.jsonl"
    if src.exists() and not ANSWERS[model].exists():
        ANSWERS[model].parent.mkdir(parents=True, exist_ok=True)
        ANSWERS[model].write_text(src.read_text())
        log(f"P33_RESUME {model}: {len(src.read_text().splitlines())} rows seeded")


def stage2_on_pod(ctx: Any, st: dict, T: Timer) -> int:
    """The Stage 1 gate on the pod's V4 rows (`p33.analyze`, the rule fixed before data), then
    Stage 2 on the loaded model if it passes."""
    from . import analyze as A
    from . import stage2 as S2b

    s = A.summarize(A.load("v4"), I.V4_ARMS)
    g = A.gate(s)
    st["stage1_gate"] = g
    write_json(RESULTS / "gate_on_pod.json", {"gate": g, "summary": s})
    log(f"P33_GATE stage1 {json.dumps({k: g.get(k) for k in ('pass', 'plan_gate_pass')})}")
    if not g.get("pass"):
        return 0
    st["step"] = "stage2"
    st2: dict[str, Any] = {}
    try:
        rc = S2b.run_loaded(ctx, st2, Timer(S2b.OUT / "timings.json"))
    except BaseException as e:  # noqa: BLE001 - Stage 1 results stand; recorded
        V.record_error("stage2", e, S2b.OUT)
        st2["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        rc = 1
    st2["rc"] = rc
    write_json(S2b.OUT / "run.json", st2)
    log(f"P33_STAGE2_DONE rc={rc}")
    return rc


def v4_phase(items: list[dict], shots: dict, st: dict, T: Timer, deadline: float | None,
             run_stage2: bool = True) -> int:
    import torch

    st["step"] = "v4_load"
    with T.step("v4_load") as rec:
        ctx, info = V.load()
        rec.update(info)
    st["v4_load"] = {k: v for k, v in info.items() if k != "int_ids"}
    cfg = ctx.model.config
    m = Model("v4", ctx, ctx.tok, ctx.parts.blocks, list(cfg.layer_types),
              dict(cfg.compress_rates))
    neutral = I.neutral_for(ctx.tok)
    st["v4_neutral"] = neutral
    first = v4_spans(ctx.tok, items[0], shots, "dots100", neutral)
    st["step"] = "v4_smoke_fixes"
    with T.step("v4_smoke_fixes") as rec:
        sm = V.smoke_with_fixes(ctx, first.ids[:V.SMOKE_TOKENS])
        rec.update(fixes=[f["name"] for f in sm["fixes_applied"]])
    st["v4_smoke_fixes"] = {"fixes_applied": sm["fixes_applied"], "attempts": len(sm["attempts"])}
    st["v4_lengths"] = {arm: len(v4_spans(ctx.tok, items[0], shots, arm, neutral).ids)
                        for arm in I.V4_ARMS}
    st["v4_neutral_tokens"] = v4_spans(ctx.tok, items[0], shots, "neutral100", neutral).n_filler()
    st["step"] = "v4_mask_check"
    eq = [it for it in items if it["kind"] == "equation"][:MASK_CHECK_N]
    with T.step("v4_mask_check") as rec:
        mc = mask_check(m, eq, shots, neutral, v4_spans)
        rec.update(pass_=mc["pass"], ratio_max=mc["ratio_max"])
    write_json(OUT["v4"] / "mask_check.json", mc)
    st["v4_mask_check"] = {"pass": mc["pass"], "ratio_max": mc["ratio_max"],
                           "counts_ok": mc["counts_ok"]}
    log(f"P33_GATE v4 mask_check {json.dumps(st['v4_mask_check'])}")
    if not mc["pass"]:
        return 3
    torch.cuda.reset_peak_memory_stats()
    seed_resume("v4")
    st["step"] = "v4_bulk"
    with T.step("v4_bulk") as rec:
        rc = run_bulk(m, items, shots, I.V4_ARMS, neutral, v4_spans, st, deadline,
                      PROJ_V4_ITEM_S, api_reference())
        rec.update(rc=rc)
    st["v4"]["mem_peak_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
    if rc == 0 and run_stage2:
        rc2 = stage2_on_pod(ctx, st, T)
        st["stage2_rc"] = rc2
    del m, ctx
    gc.collect()
    torch.cuda.empty_cache()
    return rc


def gemma_download(state: dict) -> None:
    from emx.i1a import hfget

    try:
        t = time.time()
        files = S2.model_files(S2.MODEL_ID, S2.MODEL_REV)
        got = V.with_retries(lambda: hfget.fetch(S2.MODEL_ID, files, revision=S2.MODEL_REV,
                                                 local_dir=GEMMA_DIR), "gemma download")
        state["dir"] = str(got["config.json"].parent)
        state["bytes"] = sum(p.stat().st_size for p in got.values())
        state["seconds"] = round(time.time() - t, 1)
    except BaseException as e:  # noqa: BLE001 - recorded; the Gemma phase is then skipped
        state["error"] = f"{type(e).__name__}: {str(e)[:400]}"


def gemma_phase(items: list[dict], shots: dict, st: dict, T: Timer, deadline: float | None,
                dl: dict, thread: threading.Thread) -> int:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    st["step"] = "gemma_wait_download"
    with T.step("gemma_wait_download"):
        thread.join()
    st["gemma_download"] = dl
    if "error" in dl:
        return 5
    st["step"] = "gemma_load"
    with T.step("gemma_load") as rec:
        tok = AutoTokenizer.from_pretrained(dl["dir"], local_files_only=True)
        model = AutoModelForCausalLM.from_pretrained(
            dl["dir"], dtype=torch.bfloat16, attn_implementation=S2.ATTN, device_map="cuda",
            local_files_only=True).eval()
        blocks, _ = S2.text_parts(model)
        rec.update(model_class=type(model).__name__, n_layers=len(blocks))
    ctx = type("GCtx", (), {"model": model})()
    m = Model("gemma", ctx, tok, blocks, None, None)
    neutral = I.neutral_for(tok)
    st["gemma_neutral_tokens"] = gemma_spans(tok, items[0], shots, "neutral100", neutral).n_filler()
    st["gemma_lengths"] = {arm: len(gemma_spans(tok, items[0], shots, arm, neutral).ids)
                           for arm in I.GEMMA_ARMS}
    st["step"] = "gemma_mask_check"
    eq = [it for it in items if it["kind"] == "equation"][:MASK_CHECK_N]
    with T.step("gemma_mask_check") as rec:
        mc = mask_check(m, eq, shots, neutral, gemma_spans)
        rec.update(pass_=mc["pass"], ratio_max=mc["ratio_max"])
    write_json(OUT["gemma"] / "mask_check.json", mc)
    st["gemma_mask_check"] = {"pass": mc["pass"], "ratio_max": mc["ratio_max"],
                              "counts_ok": mc["counts_ok"]}
    log(f"P33_GATE gemma mask_check {json.dumps(st['gemma_mask_check'])}")
    if not mc["pass"]:
        return 3
    st["step"] = "gemma_bulk"
    with T.step("gemma_bulk") as rec:
        rc = run_bulk(m, items, shots, I.GEMMA_ARMS, neutral, gemma_spans, st, deadline,
                      PROJ_GEMMA_ITEM_S, None)
        rec.update(rc=rc)
    return rc


def _run(a, st: dict) -> int:
    T = Timer(RESULTS / "timings.json")
    items, shots = I.load_items()
    if a.limit:
        items = items[: a.limit]
    st["plan"] = {"n_items": len(items), "v4_arms": list(I.V4_ARMS),
                  "gemma_arms": list(I.GEMMA_ARMS), "max_new": MAX_NEW}
    deadline = budget_deadline()
    dl: dict[str, Any] = {}
    thread = threading.Thread(target=gemma_download, args=(dl,), daemon=True)
    if not a.no_gemma:
        thread.start()
    rc = 0
    if not a.gemma_only:
        rc = v4_phase(items, shots, st, T, deadline, run_stage2=not a.no_stage2)
        st["v4_rc"] = rc
        log(f"P33_V4_DONE rc={rc}")
    if a.no_gemma or rc in (1, 3):
        return rc
    grc = gemma_phase(items, shots, st, T, deadline, dl, thread)
    st["gemma_rc"] = grc
    log(f"P33_GEMMA_DONE rc={grc}")
    if rc:
        return rc
    return 0 if grc == 0 else (4 if grc == 4 else 5)


def cmd_run(a) -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    st: dict[str, Any] = {"env": V.env_info(), "utc_start": utc(), "step": "setup"}
    rc = 1
    try:
        rc = _run(a, st)
        if rc == 0 and st.get("stage2_rc"):  # review r1: a failed Stage 2 is not a success
            rc = 7
    except BaseException as e:  # noqa: BLE001 - recorded; run.json is still written
        record_error(st.get("step", "run"), e)
        st["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        rc = 1
    finally:
        st["utc_end"] = utc()
        st["rc"] = rc
        st["step_last"] = st.pop("step", None)
        write_json(RESULTS / "run.json", st)
    log(f"P33_RUN_DONE rc={rc}")
    return rc


def cmd_download(_a) -> int:
    try:
        return V._download(OUT["v4"])
    except BaseException as e:  # noqa: BLE001
        record_error("download", e)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p33.pod")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--limit", type=int, default=0, help="first N items (tests)")
    ap.add_argument("--no-gemma", action="store_true")
    ap.add_argument("--gemma-only", action="store_true")
    ap.add_argument("--no-stage2", action="store_true")
    a = ap.parse_args(argv)
    return cmd_download(a) if a.step == "download" else cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())
