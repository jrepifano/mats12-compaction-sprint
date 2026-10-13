"""P33 Stage 2 pod run (plan.md "Measure", Stage 2): where V4 Flash's answer reads the filler,
and whether donor patches at those sites give the exact hybrid answer.

    python -m p33.stage2 download   # = p33.pod download (V4 Flash @ its pin)
    python -m p33.stage2 run

The dot length comes from the committed `stage2.json` ({"n_dots": 100}). Items: the equation
items (idx 0..199) whose answer and every donor value are single V4 tokens; discovery = even idx,
confirmation = odd idx. Every unit is a **prefill-only forward** (use_cache=True, the generate
path) scored at the last prompt position: top-1 token, and the log-probs of the recipient's
answer, the donor's answer and the hybrid answer (all single tokens). The smoke checks that the
forward's top-1 equals the greedy decode's first token on the first items.

Per item:
- unpatched dotsN (states at the dots captured: the self-patch source), and the Mask B prompt
  (states captured: the **ablation** source, dots that never saw the question);
- discovery items: **ablation patches** (the dots' layer outputs replaced by their Mask B values)
  at every BAND x GROUP and at all layers x all dots (= Mask B, a sanity check), and **read
  knockouts** (the answer suffix may not attend to a dot group: window keys and every compressed
  entry pooling a dot) at every BAND for each GROUP;
- every item: donors (`p33.donors`: sameY, diffY, opChange, random) captured and patched at all
  layers x all dots; the self-patch on the first N_SELF items; the explicit-y prompt (the
  recipient with y = the opChange donor's y written as a literal).
After the discovery half the sites are frozen by the rule in `select_sites` (written before any
Stage 2 data; log.md); the confirmation half then adds ablation and donor patches at the frozen
sites. Rows: results/stage2/units.jsonl (one per forward), sites.json, run.json.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import time
import tomllib
from typing import Any

import numpy as np
from p06 import encoding_dsv4 as E
from p06 import stage2 as S2
from p06 import v4pilot as V
from p06.stage2 import Timer, append_jsonl, utc, write_json

from . import PROBE
from . import donors as D
from . import items as I
from . import masks as M

OUT = PROBE / "results" / "stage2"
UNITS = OUT / "units.jsonl"
CONFIG = PROBE / "stage2.json"
N_LAYERS = 43
BANDS = {"b0": range(9), "b1": range(9, 18), "b2": range(18, 26), "b3": range(26, 35),
         "b4": range(35, 43)}
GROUPS = ("early", "late", "all")
N_SELF = 20
SMOKE_N = 4
SITE_FRAC = 0.8
MIN_FULL_DROP = 0.05
OVERRUN = 1.5
STOP_MARGIN_S = 12 * 60
PROJ_FWD_S = 0.8  # one ~1,170-token prefill (F7: ~0.7 s for the 600-token generate)


def log(msg: str) -> None:
    print(f"[p33.stage2 {utc()}] {msg}", flush=True)


def n_dots() -> int:
    return int(json.loads(CONFIG.read_text())["n_dots"]) if CONFIG.exists() else 100


# --------------------------------------------------------------------- spans and sites


def render(tok: Any, ex: dict, shots: list[dict], arm: str) -> I.Spans:
    text = E.encode_messages(I.messages(ex, shots, arm), thinking_mode="chat")
    return I.spans(tok, text, E.USER_SP_TOKEN, arm)


def group_span(sp: I.Spans, group: str) -> tuple[int, int]:
    """Absolute [a, b) of a dot group; the early/late split is at the first multiple of 4 at or
    after the middle dot (a CSA block boundary)."""
    mid = sp.f0 + (sp.f1 - sp.f0) // 2
    mid += (-mid) % 4
    return {"early": (sp.f0, mid), "late": (mid, sp.f1), "all": (sp.f0, sp.f1)}[group]


def site_name(bands: tuple[str, ...], group: str) -> str:
    return f"{'all' if tuple(bands) == tuple(BANDS) else '+'.join(bands)}|{group}"


def site_layers(bands: tuple[str, ...]) -> list[int]:
    return sorted({layer for b in bands for layer in BANDS[b]})


def knockout_rules(sp: I.Spans, group: str, bands: tuple[str, ...]) -> list[M.Rule]:
    a, b = group_span(sp, group)
    return [M.Rule(sp.suffix0, len(sp.ids), a, b, frozenset(site_layers(bands)))]


# --------------------------------------------------------------------- forward units


class Runner:
    def __init__(self, ctx: Any, device: str = "cuda") -> None:
        self.ctx, self.device = ctx, device
        self.blocks = ctx.parts.blocks

    def forward(self, ids: list[int], rules: list | None = None, patch: tuple | None = None,
                capture_pos: np.ndarray | None = None) -> tuple[Any, Any, dict]:
        """Prefill-only forward. patch = (layers, positions, stored [L_all, n, hc, D] indexed by
        layer, rows matching positions). Returns (last-position log-probs [V] fp32, captured
        states [L, P, hc, D] or None, mask counts)."""
        import torch

        n = len(ids)
        t = torch.tensor([ids], device=self.device)
        with contextlib.ExitStack() as st, torch.inference_mode():
            mk = st.enter_context(M.V4Masks(self.ctx.model, rules, n)) if rules else None
            fired = None
            if patch is not None:
                layers, pos, stored = patch
                fired = st.enter_context(S2.inject(
                    [self.blocks[i] for i in layers], torch.as_tensor(pos, device=self.device),
                    [stored[i] for i in layers], n))
            hs = None
            if capture_pos is not None:
                hs = st.enter_context(S2.capture(self.blocks,
                                                 torch.as_tensor(capture_pos, device=self.device), n))
            logits = self.ctx.model(input_ids=t, use_cache=True, logits_to_keep=1).logits[0, -1]
            lp = torch.log_softmax(logits.float(), -1)
        if fired is not None and fired != [1] * len(fired):
            raise RuntimeError(f"patch fired {fired}")
        cap = torch.stack(hs) if hs is not None else None
        return lp, cap, (mk.count.as_dict() if mk else {})


def tok_id(tok: Any, v: int) -> int:
    enc = tok.encode(str(v), add_special_tokens=False)
    if len(enc) != 1:
        raise ValueError(f"{v} is not one token")
    return int(enc[0])


def score(tok: Any, lp: Any, cands: dict[str, int]) -> dict:
    top = int(lp.argmax())
    txt = tok.decode([top]).strip()
    out = {"top1": top, "top1_text": txt,
           "top1_int": int(txt) if txt.isdigit() else None}
    for k, v in cands.items():
        tid = tok_id(tok, v)
        out[f"lp_{k}"] = round(float(lp[tid]), 4)
        out[f"is_{k}"] = top == tid
    return out


# --------------------------------------------------------------------- site selection


def select_sites(rows: list[dict]) -> dict:
    """Frozen sites from the discovery half's ablation rows (rule fixed before any data):
    D(site) = acc(unpatched) - acc(ablated at site), D_full at all layers x all dots.
    - D_full < MIN_FULL_DROP: nothing to localise; frozen = all bands x all dots.
    - else the single (band, group) with D >= SITE_FRAC x D_full, preferring a smaller group
      (early/late before all), then the larger D; compact.
    - else `pair_needed`: the two bands with the largest D at group "all" are tested together
      (an extra pass); compact if that pair reaches SITE_FRAC x D_full."""
    by: dict[str, list[bool]] = {}
    for r in rows:
        if r["unit"] in ("unpatched", "ablate"):
            by.setdefault(r["site"] if r["unit"] == "ablate" else "none", []).append(r["is_answer"])
    acc = {k: sum(v) / len(v) for k, v in by.items() if v}
    base = acc.get("none")
    full = acc.get("all|all")
    if base is None or full is None:
        return {"decided": False}
    d_full = base - full
    drops = {k: base - a for k, a in acc.items() if k not in ("none", "all|all")}
    out: dict[str, Any] = {"decided": True, "acc_unpatched": base, "acc_full": full,
                           "d_full": d_full, "drops": drops}
    if d_full < MIN_FULL_DROP:
        return {**out, "frozen": {"bands": list(BANDS), "group": "all"}, "compact": False,
                "reason": "full ablation drop below MIN_FULL_DROP"}
    ok = [(k, d) for k, d in drops.items() if d >= SITE_FRAC * d_full and "+" not in k]
    if ok:
        k, _ = min(ok, key=lambda kd: (kd[0].split("|")[1] == "all", -kd[1]))
        band, group = k.split("|")
        return {**out, "frozen": {"bands": [band], "group": group}, "compact": True,
                "reason": "single site"}
    alls = sorted(((k.split("|")[0], d) for k, d in drops.items()
                   if k.endswith("|all") and "+" not in k), key=lambda bd: -bd[1])
    pair = sorted(b for b, _ in alls[:2])
    return {**out, "pair_needed": pair}


def finish_pair(sel: dict, rows: list[dict]) -> dict:
    pair = sel["pair_needed"]
    key = "+".join(pair) + "|all"
    v = [r["is_answer"] for r in rows if r["unit"] == "ablate" and r["site"] == key]
    d = sel["acc_unpatched"] - sum(v) / len(v)
    sel = {**sel, "drops": {**sel["drops"], key: d},
           "frozen": {"bands": pair, "group": "all"},
           "compact": d >= SITE_FRAC * sel["d_full"], "reason": "pair"}
    return sel


# --------------------------------------------------------------------- per item


def item_pool(tok: Any) -> list[dict]:
    exs, _ = I.load_items()
    eq = [it["ex"] for it in exs if it["kind"] == "equation"]
    out = []
    for ex in eq:
        try:
            tok_id(tok, ex["answer"])
        except ValueError:
            continue
        out.append(ex)
    return out


def prepare(tok: Any, ex: dict, shots: list[dict], N: int, pool: list[dict]) -> dict | None:
    arm = f"dots{N}"
    sp = render(tok, ex, shots, arm)
    dn = D.donors(ex, pool)
    dons: dict[str, dict] = {}
    for kind in ("sameY", "diffY", "opChange"):
        if kind not in dn:
            continue
        dsp = render(tok, dn[kind]["ex"], shots, arm)
        diff = [i for i, (a, b) in enumerate(zip(sp.ids, dsp.ids)) if a != b]
        if len(dsp.ids) != len(sp.ids) or (dsp.f0, dsp.f1) != (sp.f0, sp.f1) or not diff \
                or min(diff) < sp.turn0 or max(diff) >= sp.f0:
            continue
        dons[kind] = {**dn[kind], "ids": dsp.ids}
    for cand in dn.get("random_candidates", []):
        rsp = render(tok, cand, shots, arm)
        if len(rsp.ids) == len(sp.ids) and (rsp.f0, rsp.f1) == (sp.f0, sp.f1):
            y = cand["queried_value"]
            hyb = D.q(ex, y)
            if 0 <= hyb <= D.MAXV:
                dons["random"] = {"ex": cand, "y": y, "donor_answer": cand["answer"],
                                  "hybrid": hyb, "ids": rsp.ids}
                break
    if "opChange" not in dons or "diffY" not in dons:
        return None
    exp = D.explicit_y(ex, dons["opChange"]["y"])
    esp = render(tok, exp, shots, arm)
    return {"ex": ex, "sp": sp, "donors": dons, "explicit_ids": esp.ids,
            "explicit_answer": exp["answer"]}


def run_item(R: Runner, tok: Any, P: dict, half: str, i_self: bool, frozen: dict | None,
             write) -> None:
    ex, sp = P["ex"], P["sp"]
    dots = np.arange(sp.f0, sp.f1)
    base_c = {"answer": ex["answer"]}
    common = {"item_id": f"eq:{ex['idx']}", "idx": ex["idx"], "half": half, "n_prompt": len(sp.ids)}
    lp, own, _ = R.forward(sp.ids, capture_pos=dots)
    write({**common, "unit": "unpatched", "site": "none", **score(tok, lp, base_c)})
    rules_b = I.mask_rules("maskB100", sp)  # Rule(f0, f1, 0, f0): any N
    lp, abl, _ = R.forward(sp.ids, rules=rules_b, capture_pos=dots)
    write({**common, "unit": "maskB", "site": "none", **score(tok, lp, base_c)})

    def rows_for(group: str) -> np.ndarray:
        a, b = group_span(sp, group)
        return np.arange(a, b)

    def sub(states: Any, group: str) -> Any:
        a, b = group_span(sp, group)
        return states[:, a - sp.f0:b - sp.f0]

    def ablate(bands: tuple[str, ...], group: str, states: Any = abl) -> None:
        lp, _, _ = R.forward(sp.ids, patch=(site_layers(bands), rows_for(group),
                                            sub(states, group)))
        write({**common, "unit": "ablate", "site": site_name(bands, group),
               **score(tok, lp, base_c)})

    if half == "discovery":
        ablate(tuple(BANDS), "all")  # = Mask B (sanity)
        for band in BANDS:
            for group in GROUPS:
                ablate((band,), group)
        for band in BANDS:
            for group in GROUPS:
                lp, _, _ = R.forward(sp.ids, rules=knockout_rules(sp, group, (band,)))
                write({**common, "unit": "knockout", "site": site_name((band,), group),
                       **score(tok, lp, base_c)})
        lp, _, _ = R.forward(sp.ids, rules=knockout_rules(sp, "all", tuple(BANDS)))
        write({**common, "unit": "knockout", "site": "all|all", **score(tok, lp, base_c)})
    if i_self:
        lp, _, _ = R.forward(sp.ids, patch=(list(range(N_LAYERS)), dots, own))
        write({**common, "unit": "self", "site": "all|all", **score(tok, lp, base_c)})
    del own
    if frozen is not None:
        ablate(tuple(frozen["bands"]), frozen["group"])
    for kind, dn in P["donors"].items():
        cands = {"answer": ex["answer"], "donor_answer": dn["donor_answer"],
                 "hybrid": dn["hybrid"]}
        lp, dst, _ = R.forward(dn["ids"], capture_pos=dots)
        dsc = score(tok, lp, cands)
        info = {"donor": kind, "donor_y": dn["y"], "donor_answer": dn["donor_answer"],
                "hybrid": dn["hybrid"], "donor_top1_int": dsc["top1_int"]}
        write({**common, **info, "unit": "donor_own", "site": "none", **dsc})
        lp, _, _ = R.forward(sp.ids, patch=(list(range(N_LAYERS)), dots, dst))
        write({**common, **info, "unit": "donor_patch", "site": "all|all",
               **score(tok, lp, cands)})
        if frozen is not None:
            g = frozen["group"]
            lp, _, _ = R.forward(sp.ids, patch=(site_layers(tuple(frozen["bands"])),
                                                rows_for(g), sub(dst, g)))
            write({**common, **info, "unit": "donor_patch",
                   "site": site_name(tuple(frozen["bands"]), g), **score(tok, lp, cands)})
        del dst
    del abl
    op = P["donors"]["opChange"]
    lp, _, _ = R.forward(P["explicit_ids"])
    write({**common, "unit": "explicit_y", "site": "none", "donor_y": op["y"],
           **score(tok, lp, {"answer": P["explicit_answer"]})})


# --------------------------------------------------------------------- run


def budget_deadline() -> float | None:
    t0 = os.environ.get("P33_T0")
    if not t0:
        return None
    gpu_h = float(tomllib.loads((PROBE / "probe.toml").read_text())["pod"]["gpu_hours"])
    return float(t0) + gpu_h * 3600 - STOP_MARGIN_S


def smoke(R: Runner, tok: Any, preps: list[dict]) -> dict:
    """Forward top-1 == greedy first token; self-patch leaves the top-1 unchanged; the mask
    hooks fire as expected for Mask B and a knockout."""
    out: dict[str, Any] = {"items": []}
    ok = True
    cfg = R.ctx.model.config
    for P in preps:
        sp = P["sp"]
        new, _, _ = V.greedy(R.ctx.model, sp.ids, R.ctx.int_ids, "cuda", max_new=2)
        lp, own, _ = R.forward(sp.ids, capture_pos=np.arange(sp.f0, sp.f1))
        lp2, _, _ = R.forward(sp.ids, patch=(list(range(N_LAYERS)), np.arange(sp.f0, sp.f1), own))
        rules = I.mask_rules("maskB100", sp)
        _, _, cnt = R.forward(sp.ids, rules=rules)
        want = M.v4_expected_counts(list(cfg.layer_types), rules, len(sp.ids),
                                    dict(cfg.compress_rates))
        kr = knockout_rules(sp, "all", ("b2",))
        _, _, cnt_k = R.forward(sp.ids, rules=kr)
        want_k = M.v4_expected_counts(list(cfg.layer_types), kr, len(sp.ids),
                                      dict(cfg.compress_rates))
        rec = {"idx": P["ex"]["idx"], "greedy_first": new[0], "forward_top1": int(lp.argmax()),
               "self_top1": int(lp2.argmax()), "greedy_text": tok.decode(new),
               "maskB_counts_ok": cnt == want, "knockout_counts_ok": cnt_k == want_k}
        rec["ok"] = (rec["greedy_first"] == rec["forward_top1"] == rec["self_top1"]
                     and rec["maskB_counts_ok"] and rec["knockout_counts_ok"])
        ok &= rec["ok"]
        out["items"].append(rec)
        del own
    out["pass"] = ok
    return out


def _run(a, st: dict) -> int:
    import torch

    T = Timer(OUT / "timings.json")
    N = n_dots()
    st["n_dots"] = N
    st["step"] = "load"
    with T.step("load") as rec:
        ctx, info = V.load()
        rec.update({k: v for k, v in info.items() if k != "int_ids"})
    tok = ctx.tok
    _, shots = I.load_items()
    shots = shots["equation"]
    st["step"] = "smoke_fixes"
    R = Runner(ctx)
    pool = item_pool(tok)
    preps = []
    skipped = []
    for ex in pool:
        P = prepare(tok, ex, shots, N, pool)
        (preps.append(P) if P else skipped.append(ex["idx"]))
    if a.limit:
        preps = preps[: a.limit]
    st["plan"] = {"n_items": len(preps), "skipped_idx": skipped,
                  "donor_counts": {k: sum(k in P["donors"] for P in preps)
                                   for k in D.KINDS}}
    with T.step("smoke_fixes") as rec:
        sm = V.smoke_with_fixes(ctx, preps[0]["sp"].ids[:V.SMOKE_TOKENS])
        rec.update(fixes=[f["name"] for f in sm["fixes_applied"]])
    st["smoke_fixes"] = {"fixes_applied": sm["fixes_applied"]}
    st["step"] = "smoke"
    with T.step("smoke") as rec:
        sk = smoke(R, tok, preps[:SMOKE_N])
        rec.update(pass_=sk["pass"])
    st["smoke"] = sk
    log(f"P33_GATE stage2 smoke {json.dumps(sk)}")
    if not sk["pass"]:
        return 3
    disc = [P for P in preps if P["ex"]["idx"] % 2 == 0]
    conf = [P for P in preps if P["ex"]["idx"] % 2 == 1]
    deadline = budget_deadline()
    rows: list[dict] = []

    def write(row: dict) -> None:
        row = {**row, "utc": utc()}
        append_jsonl(UNITS, row)
        rows.append(row)

    proj = PROJ_FWD_S * (len(disc) * 40 + len(conf) * 18)
    start = time.time()
    stop_at = start + OVERRUN * proj
    st["projection_s"] = {"initial": round(proj)}
    item_s: list[float] = []

    def over() -> str | None:
        if time.time() > stop_at:
            return "overrun"
        if deadline is not None and time.time() > deadline:
            return "deadline"
        return None

    st["step"] = "discovery"
    for i, P in enumerate(disc):
        if (s := over()):
            st["stopped"] = f"{s} in discovery after {i}"
            return 4
        t = time.time()
        run_item(R, tok, P, "discovery", i < N_SELF, None, write)
        item_s.append(time.time() - t)
        if len(item_s) == 4:
            med = statistics.median(item_s[1:])
            proj = time.time() - start + med * (len(disc) - i - 1) + med * 0.5 * len(conf)
            stop_at = start + OVERRUN * proj
            st["projection_s"].update(updated=round(proj), disc_item_s=round(med, 2))
            log(f"P33_PROJECTION stage2 {proj / 60:.1f} min (discovery item {med:.1f} s)")
        if (i + 1) % 10 == 0:
            log(f"discovery {i + 1}/{len(disc)}, last {item_s[-1]:.1f} s")
    st["step"] = "select"
    sel = select_sites(rows)
    if "pair_needed" in sel:
        pair = tuple(sel["pair_needed"])
        for P in disc:
            sp = P["sp"]
            dots = np.arange(sp.f0, sp.f1)
            _, abl, _ = R.forward(sp.ids, rules=I.mask_rules("maskB100", sp), capture_pos=dots)
            lp, _, _ = R.forward(sp.ids, patch=(site_layers(pair), dots, abl))
            write({"item_id": f"eq:{P['ex']['idx']}", "idx": P["ex"]["idx"], "half": "discovery",
                   "unit": "ablate", "site": "+".join(pair) + "|all",
                   **score(tok, lp, {"answer": P["ex"]["answer"]})})
            del abl
        sel = finish_pair(sel, rows)
    write_json(OUT / "sites.json", sel)
    st["sites"] = {k: sel.get(k) for k in ("frozen", "compact", "reason", "d_full")}
    log(f"P33_SITES {json.dumps(st['sites'])}")
    st["step"] = "confirm"
    for i, P in enumerate(conf):
        if (s := over()):
            st["stopped"] = f"{s} in confirmation after {i}"
            return 4
        run_item(R, tok, P, "confirm", False, sel.get("frozen"), write)
        if (i + 1) % 10 == 0:
            log(f"confirm {i + 1}/{len(conf)}")
    st["mem_peak_gb"] = round(torch.cuda.max_memory_allocated() / 1e9, 2)
    return 0


def cmd_run(a) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    st: dict[str, Any] = {"env": V.env_info(), "utc_start": utc(), "step": "setup"}
    rc = 1
    try:
        rc = _run(a, st)
    except BaseException as e:  # noqa: BLE001
        V.record_error(st.get("step", "run"), e, OUT)
        st["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        rc = 1
    finally:
        st["utc_end"], st["rc"] = utc(), rc
        st["step_last"] = st.pop("step", None)
        write_json(OUT / "run.json", st)
    log(f"P33_STAGE2_DONE rc={rc}")
    return rc


def cmd_download(_a) -> int:
    try:
        return V._download(OUT)
    except BaseException as e:  # noqa: BLE001
        V.record_error("download", e, OUT)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="p33.stage2")
    ap.add_argument("step", choices=("download", "run"))
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    return cmd_download(a) if a.step == "download" else cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())
