"""P17 evaluation bundle for one model (B, K<s> or E<s>), from the shared-prefix caches
(plan.md "Measure"; output layout in schema.py).

Per kind (base, ask, dots100) the items are bucketed by continuation length, so a batch needs no
padding, and decoded greedily (`greedy`: the prefill from a copy of the prefix cache, then
MAX_NEW one-token steps, each row cut after its first end-of-turn/eos token, as HF generate
returns it). For dots100 the prefill also captures every block's output at the last 115 prompt
positions (slot -115, the pre-filler token, then P06 F3's slots -114..-1), from which:
- the three readers (`p06.stage2.readouts`: logit lens, J-Lens, J++; all 60 layers, the digit
  tokens), stored per item in P06's npz schema (`p06.stage2.write_npz`, arm "dots100");
- lens-vs-model top-1 agreement at the 114 read positions (the model's own logits);
- the residual pools (schema.POOL_KEYS at schema.POOL_LAYERS).
Then P06 F3's filler-K/V transplant on the first N_TRANSPLANT items (matched donor, the target
itself, a random donor; `p06.stage2.make_donor/random_donor/check_donor/capture/inject`), with
the K/V captured and injected on the continuation's prefill.
"""

from __future__ import annotations

import json
import shutil
import time
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import numpy as np
from p06 import stage2 as S2

from . import common as C
from . import penalty as PEN
from . import schema as S
from .train import utc, write_json


@contextmanager
def active(model: Any, name: str):
    """The model as `name`: B = the adapter disabled (or no adapter yet), else that adapter."""
    from peft import PeftModel

    if name == "B":
        # peft keeps `active_adapter` pointing at a deleted adapter, and disable_adapter() then
        # raises KeyError; with no live adapter the wrapped model already runs as the base
        live = isinstance(model, PeftModel) and model.active_adapter in model.peft_config
        with (model.disable_adapter() if live else nullcontext()):
            yield
    else:
        model.set_adapter(name)
        yield


def eos_ids(model: Any) -> set[int]:
    e = model.generation_config.eos_token_id
    return set(e if isinstance(e, list) else [e])


def greedy(model: Any, prefix: Any, conts: list[list[int]], eos: set[int], digit_ids: list[int],
           device: str, max_new: int = S2.MAX_NEW, keep: int = 1) -> dict:
    """Equal-length continuations -> {"gen": [ids per row], "lp": [[n_new, 10] per row],
    "top1": [B, keep] argmax of the prefill's last `keep` positions}."""
    import torch

    n = len(conts[0])
    if any(len(c) != n for c in conts):
        raise ValueError("greedy needs equal-length continuations")
    b = len(conts)
    cache = C.cache_for(prefix, b)
    didx = torch.tensor(digit_ids, device=device)
    with torch.inference_mode():
        out = model(input_ids=torch.tensor(conts, device=device), past_key_values=cache,
                    use_cache=True, logits_to_keep=keep)
        top1 = out.logits.argmax(-1).cpu().numpy()
        step = out.logits[:, -1]
        gens: list[list[int]] = [[] for _ in range(b)]
        lps: list[Any] = []
        done = np.zeros(b, bool)
        for t in range(max_new):
            nxt = step.argmax(-1)
            lps.append(torch.log_softmax(step.float(), -1).index_select(-1, didx).cpu())
            for r, tok_id in enumerate(nxt.tolist()):
                if not done[r]:
                    gens[r].append(int(tok_id))
                    done[r] = tok_id in eos
            if done.all() or t == max_new - 1:
                break
            step = model(input_ids=nxt[:, None], past_key_values=cache, use_cache=True).logits[:, -1]
    lp = torch.stack(lps, dim=1).numpy()
    del cache
    return {"gen": gens, "lp": [lp[r, : len(gens[r])] for r in range(b)], "top1": top1}


def buckets(splits: list[C.Split], size: int) -> list[list[int]]:
    """Indices into `splits`, grouped by continuation length, in batches of at most `size`."""
    by: dict[int, list[int]] = defaultdict(list)
    for i, sp in enumerate(splits):
        by[len(sp.cont)].append(i)
    return [v[j:j + size] for _, v in sorted(by.items()) for j in range(0, len(v), size)]


def prepare(tok: Any, exs: list[dict], shots: list[dict]) -> dict:
    """Rendered splits per kind and each kind's shared prefix (done once for every model)."""
    out = {}
    for kind in S.KINDS:
        sps = [C.render_split(tok, ex, shots, kind) for ex in exs]
        out[kind] = {"splits": sps, "prefix": C.check_shared_prefix(sps)}
    return out


def _pools(hs: Any, b: int) -> dict[str, np.ndarray]:
    """From hs [L, B, 115, d]: POOL_KEYS at POOL_LAYERS for row b, float16 [K, d]."""
    L = list(S.POOL_LAYERS)
    h = hs[L, b].float()
    return {"dots_mean": h[:, PEN.WIN["dots"]].mean(1).half().cpu().numpy(),
            "label": h[:, PEN.WIN["label"][1]].half().cpu().numpy(),
            "prefill": h[:, PEN.WIN["prefill"][0]].half().cpu().numpy()}


def answers_pass(model: Any, tok: Any, rd: C.Readers, name: str, kind: str, prep: dict,
                 exs: list[dict], out: Path, device: str, batch: int, ctx: Any = None,
                 log=print) -> dict:
    """Decode every item of `kind`; for dots100 also readouts, agreement and pools."""
    import torch

    sps = prep[kind]["splits"]
    prefix = C.prefix_cache(model, prep[kind]["prefix"], device)
    blocks, _ = C.text_model(model)
    eos = eos_ids(model)
    read = kind == "dots100"
    rows, gens, pools = [], {}, {}
    agree = {"read_agree": 0, "read_n": 0, "final_eq_gen": 0}
    for bi in buckets(sps, batch):
        conts = [sps[i].cont for i in bi]
        if read:
            n = len(conts[0])
            win = torch.arange(n - 115, n, device=device)
            with C.capture_blocks(blocks, win, keep_graph=False) as hs_list:
                g = greedy(model, prefix, conts, eos, rd.digit_ids, device, keep=114)
            hs = torch.stack(hs_list)  # [L, B, 115, d]
            flat = hs[:, :, 1:, :].reshape(hs.shape[0], -1, hs.shape[-1])  # slots -114..-1
            with torch.inference_mode():
                ro_all = S2.readouts(ctx, flat)
        else:
            g = greedy(model, prefix, conts, eos, rd.digit_ids, device)
        for k, i in enumerate(bi):
            ex, sp = exs[i], sps[i]
            target = ex["queried_value"] if kind == "ask" else ex["answer"]
            text = tok.decode(g["gen"][k], skip_special_tokens=True)
            rows.append({"model": name, "idx": ex["idx"], "kind": kind, "target": target,
                         "gen_text": text, "gen_ids": g["gen"][k],
                         **S2.grade_int(text, target)})
            gens[ex["idx"]] = (text, g["gen"][k])
            if not read:
                continue
            ro = {r: {"digit_lp": v["digit_lp"][:, k * 114:(k + 1) * 114],
                      "top1": v["top1"][:, k * 114:(k + 1) * 114]} for r, v in ro_all.items()}
            lens_top = ro["logit_lens"]["top1"][-1]
            agree["read_agree"] += int((lens_top == g["top1"][k]).sum())
            agree["read_n"] += len(lens_top)
            agree["final_eq_gen"] += int(bool(g["gen"][k]) and int(lens_top[-1]) == g["gen"][k][0])
            cv = S2.chain_values(ex)
            rec = {"item_id": f"varbind:{ex['idx']}", "idx": ex["idx"], "arm": "dots100",
                   "positions": sp.read[1:].astype(np.int32),
                   "slots": (sp.read[1:] - len(sp.ids)).astype(np.int16),
                   "token_ids": np.array([sp.ids[p] for p in sp.read[1:]], dtype=np.int32),
                   "n_prompt": len(sp.ids), "gen_ids": np.array(g["gen"][k], dtype=np.int32),
                   "gen_text": text, "gen_digit_lp": g["lp"][k].astype(np.float16),
                   "digit_ids": np.array(rd.digit_ids, dtype=np.int32), "roles": list(S2.ROLES),
                   "values": np.array([cv[r] for r in S2.ROLES], dtype=np.int64),
                   "readers": list(ro), "ro": ro}
            S2.write_npz(S2.npz_path(rec["item_id"], "dots100", out / "readouts"), rec)
            pools[ex["idx"]] = _pools(hs, k)
        if read:
            del hs, flat, ro_all
    rows.sort(key=lambda r: r["idx"])
    with (out / "answers.jsonl").open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    acc = float(np.mean([r["correct"] for r in rows])) if rows else None
    log(f"P17_EVAL {name} {kind}: acc {acc} (n {len(rows)})")
    res = {"acc": acc, "n": len(rows), "gens": gens}
    if read:
        res.update(agree=agree, pools=pools)
    del prefix
    return res


def transplant_pass(model: Any, tok: Any, rd: C.Readers, name: str, prep: dict,
                    exs: list[dict], shots: list[dict], pool: list[dict], gens: dict, out: Path,
                    device: str, n: int = S.N_TRANSPLANT) -> dict:
    """P06 F3's filler-K/V transplant from the dots100 prefix cache, batch 1 per pair."""
    import torch

    blocks, _ = C.text_model(model)
    mods = S2.kv_modules(blocks)
    prefix = C.prefix_cache(model, prep["dots100"]["prefix"], device)
    eos = eos_ids(model)
    split0 = prep["dots100"]["splits"][0].split

    def kv_of(sp: C.Split) -> list[Any]:
        pos = torch.as_tensor(sp.rel(sp.dots), device=device)
        with torch.inference_mode(), S2.capture(mods, pos, len(sp.cont)) as kv:
            model(input_ids=torch.tensor([sp.cont], device=device),
                  past_key_values=C.cache_for(prefix, 1), use_cache=True, logits_to_keep=1)
        if any(x is None for x in kv):
            raise RuntimeError("a K/V hook captured nothing")
        return kv

    def decode_with(tgt: C.Split, kv: list[Any]) -> str:
        tpos = torch.as_tensor(tgt.rel(tgt.dots), device=device)
        with S2.inject(mods, tpos, kv, len(tgt.cont)) as fired:
            g = greedy(model, prefix, [tgt.cont], eos, rd.digit_ids, device)
        if fired != [1] * len(mods):
            raise RuntimeError(f"K/V injection fired {sorted(set(fired))} times, not once")
        return tok.decode(g["gen"][0], skip_special_tokens=True)

    pred = lambda s: S2.grade_int(s, 0)["predicted"]
    rows, self_changed = [], 0
    for i, ex in enumerate(exs[:n]):
        tgt = prep["dots100"]["splits"][i]
        donor = S2.make_donor(ex)
        dsp = C.render_split(tok, donor, shots, "dots100")
        S2.check_donor(tgt, dsp)
        if dsp.split != split0:
            raise ValueError("donor prefix split differs")
        rand = S2.random_donor(ex, pool)
        rsp = C.render_split(tok, rand, shots, "dots100")
        target_text = gens[ex["idx"]][0]
        tx_text = decode_with(tgt, kv_of(dsp))
        self_text = decode_with(tgt, kv_of(tgt))
        rtx_text = decode_with(tgt, kv_of(rsp))
        self_changed += int(self_text != target_text)
        rows.append({"model": name, "idx": ex["idx"], "answer": ex["answer"],
                     "target_text": target_text, "target_pred": pred(target_text),
                     "donor_x": S2.chain_values(donor)["x"], "donor_answer": donor["answer"],
                     "tx_text": tx_text, "tx_pred": pred(tx_text),
                     "self_text": self_text, "self_pred": pred(self_text),
                     "rand_idx": rand["idx"], "rand_answer": rand["answer"],
                     "rtx_text": rtx_text, "rtx_pred": pred(rtx_text)})
    with (out / "transplant.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return {"n": len(rows), "self_changed": self_changed}


def eval_model(model: Any, tok: Any, rd: C.Readers, name: str, prep: dict, exs: list[dict],
               shots: list[dict], pool: list[dict], root: Path, device: str, batch: int = 16,
               n_transplant: int = S.N_TRANSPLANT, log=print) -> dict:
    """Every measure for one model; skipped when eval/<name>/done.json exists."""
    out = root / "eval" / name
    if (out / "done.json").exists():
        log(f"P17_EVAL {name}: done.json exists, skipped")
        return json.loads((out / "done.json").read_text())
    if out.exists():
        shutil.rmtree(out)  # a partial earlier attempt: redo the model whole
    out.mkdir(parents=True)
    t0, timing = time.time(), {}
    ctx = rd.ctx(model, tok, device)
    res: dict[str, Any] = {}
    with active(model, name):
        for kind in ("base", "ask", "dots100"):
            t = time.time()
            res[kind] = answers_pass(model, tok, rd, name, kind, prep, exs, out, device, batch,
                                     ctx=ctx, log=log)
            timing[kind] = round(time.time() - t, 1)
        t = time.time()
        tx = transplant_pass(model, tok, rd, name, prep, exs, shots, pool,
                             res["dots100"]["gens"], out, device, n=n_transplant)
        timing["transplant"] = round(time.time() - t, 1)
    pools = res["dots100"]["pools"]
    idx = np.array(sorted(pools))
    np.savez(out / "pools.npz", idx=idx, layers=np.array(S.POOL_LAYERS),
             **{k: np.stack([pools[i][k] for i in idx]) for k in S.POOL_KEYS})
    ag = res["dots100"]["agree"]
    done = {"model": name, "n_items": len(exs), "kinds": list(S.KINDS),
            "acc": {k: res[k]["acc"] for k in S.KINDS},
            "read_agree": ag["read_agree"] / max(ag["read_n"], 1),
            "final_eq_gen": ag["final_eq_gen"] / max(len(exs), 1),
            "transplant": tx, "timing_s": timing, "seconds": round(time.time() - t0, 1),
            "utc": utc()}
    write_json(out / "done.json", done)
    log(f"P17_EVAL_DONE {name} acc {done['acc']} agree {done['read_agree']:.4f} "
        f"self_changed {tx['self_changed']} {done['seconds']}s")
    return done
