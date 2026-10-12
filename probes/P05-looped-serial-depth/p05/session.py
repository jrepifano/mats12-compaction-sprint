"""P05 session driver: download, smoke gates, benchmark, the loop sweep, greedy validation.

Plan: probes/P05-looped-serial-depth/plan.md ("Automatic rules", "Scoring", "Measure"). Run on the
pod by ../run.sh (`python -m p05.session all`) and on the laptop for the free smoke
(`python -m p05.session smoke --device mps --limit 6`).

Outputs under --out (default results/), each written atomically, and skipped on rerun when
present (a resumed run in the same session redoes nothing):
    env.json, items.json (counts, sha256, chance per bank), smoke.json, bench.json
    tf/<model>_s<seed>_<bank>_<fmt>.jsonl      one row per (item, r): correct, pred, gold
    readout/<model>_s<seed>_<bank>_<fmt>.npz   candidate logits [N, T, L, 20] (float16)
    ask/<model>_s<seed>_<bank>_<fmt>.jsonl     prefix prompts: top1 over 1..20 per r
    greedy/<model>_s<seed>_<bank>_<fmt>.jsonl  greedy reference rows (validation sample)
    timing.jsonl                               one row per unit: wall-clock, tokens, TFLOP/s
    chance.json                                bank -> majority floor
    decisions.jsonl                            automatic decisions (precision, floor rule, ...)
rc: 0 done, 3 a gate failed (smoke.json says which), 4 stopped at the time budget.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import traceback
from pathlib import Path

import numpy as np

from . import items as I
from .nocot import grade

GATE_ONE_PASS = 0.99
GATE_PRECISION = 0.98
GATE_TF_GREEDY = 0.98
GATE_BARE_NUMBER = 0.90
N_VALIDATE = 100
MAX_NEW = {"chain": 4, "chain_easy": 4, "arithmetic": 10}


class GateFailed(RuntimeError):
    pass


class OutOfTime(RuntimeError):
    pass


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"[P05 {utc()}] {msg}", flush=True)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


class Session:
    def __init__(self, args):
        import torch

        self.torch = torch
        self.args = args
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.t0 = float(os.environ.get("P05_T0", time.time()))
        self.max_hours = float(os.environ.get("P05_MAX_HOURS", args.max_hours))
        self.device = torch.device(args.device)
        if self.device.type == "cuda":  # strict fp32 until the smoke's precision gate decides
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
        self.models: dict = {}
        self.tok = None
        self.bf16 = None
        self.banks: dict = {}

    # -- bookkeeping ---------------------------------------------------------------------------
    def check_time(self, what: str) -> None:
        h = (time.time() - self.t0) / 3600
        if h > self.max_hours:
            raise OutOfTime(f"{what}: {h:.2f} h elapsed > P05_MAX_HOURS {self.max_hours}")

    def decide(self, **row) -> None:
        append_jsonl(self.out / "decisions.jsonl", {"utc": utc(), **row})
        log(f"decision {row}")

    def timing(self, **row) -> None:
        append_jsonl(self.out / "timing.jsonl", {"utc": utc(), **row})

    # -- models and items ----------------------------------------------------------------------
    def load_tok(self):
        if self.tok is None:
            from transformers import AutoTokenizer

            from .model import BASE
            self.tok = AutoTokenizer.from_pretrained(BASE[0], revision=BASE[1])
            from .model import cand_token_ids
            self.cand = cand_token_ids(self.tok)
            self.pad_id = self.tok.eos_token_id
        return self.tok

    def load(self, key: str):
        from transformers import AutoModelForCausalLM

        from .model import MODELS
        if key not in self.models:
            for k in list(self.models):  # one model on the device at a time
                del self.models[k]
            if self.device.type == "cuda":
                self.torch.cuda.empty_cache()
            repo, rev = MODELS[key]
            t = time.time()
            m = AutoModelForCausalLM.from_pretrained(
                repo, revision=rev, trust_remote_code=(key != "base"),
                torch_dtype=self.torch.float32).to(self.device).eval()
            self.models[key] = m
            self.timing(unit=f"load:{key}", seconds=round(time.time() - t, 1))
        return self.models[key]

    def bank(self, name: str):
        if name not in self.banks:
            evals, shots = I.build_bank(name)
            if self.args.limit:
                rnd = random.Random(0)
                by_k: dict = {}
                for e in evals:
                    by_k.setdefault(e["k"], []).append(e)
                evals = [e for k in sorted(by_k)
                         for e in rnd.sample(by_k[k], min(self.args.limit, len(by_k[k])))]
            self.banks[name] = (evals, shots)
        return self.banks[name]

    def write_items_manifest(self) -> None:
        tok = self.load_tok()
        info = {}
        for b in self.args.banks + ["chain_easy"]:
            evals, shots = self.bank(b)
            longest = max(len(tok(I.render_prompt(e, shots, f) + I.gold_continuation(e))
                              ["input_ids"]) for e in evals for f in I.FORMATS)
            if longest > I.MAX_PROMPT_TOKENS:
                raise GateFailed(f"{b}: prompt of {longest} tokens > {I.MAX_PROMPT_TOKENS}")
            ks: dict = {}
            for e in evals:
                ks[e["k"]] = ks.get(e["k"], 0) + 1
            info[b] = {"sha256": I.bank_sha(evals, shots), "n": len(evals), "per_k": ks,
                       "chance": evals[0]["chance"], "longest_prompt_tokens": longest,
                       "relabelled_k": sum("k_generated" in e for e in evals)}
        atomic_write(self.out / "items.json", json.dumps(info, indent=1, sort_keys=True))
        atomic_write(self.out / "chance.json",
                     json.dumps({b: v["chance"] for b, v in info.items()}, sort_keys=True))

    # -- the sweep ------------------------------------------------------------------------------
    def batches(self, rows: list[dict], fmt: str, shots, readout: bool, budget: int):
        from .model import make_batch, plan_batches
        tok = self.load_tok()
        prompts = [I.render_prompt(e, shots, fmt) for e in rows]
        conts = [I.gold_continuation(e) for e in rows]
        lens = [len(tok(p)["input_ids"]) + 3 for p in prompts]
        for idx in plan_batches(lens, budget):
            yield idx, make_batch(tok, [prompts[i] for i in idx], [conts[i] for i in idx],
                                  [rows[i]["item_id"] for i in idx],
                                  [readout and I.readout_eligible(rows[i]) for i in idx],
                                  self.pad_id)

    def sweep(self, key: str, seed: int, rows: list[dict], fmt: str, shots, *,
              checkpoints: tuple, readout_loops: tuple = (), readout: bool = False):
        """Run one model over rows; returns (score rows, readout dict or None)."""
        from .model import base_sweep, raven_sweep
        m = self.load(key)
        out_rows, ro_ids, ro_cand, ntok = [], [], [], 0
        loops, lenses = None, None
        for idx, b in self.batches(rows, fmt, shots, readout, self.args.token_budget):
            ntok += int(sum(b.lengths))
            if key == "base":
                so = base_sweep(m, b, self.cand, readout=readout, bf16=self.bf16)
            else:
                so = raven_sweep(m, b, checkpoints, seed, self.cand,
                                 readout_loops=readout_loops if readout else (), bf16=self.bf16)
            for r, preds in so.pred.items():
                for j, i in enumerate(idx):
                    e = rows[i]
                    out_rows.append({"model": key, "seed": seed, "bank": e["bank"], "fmt": fmt,
                                     "item_id": e["item_id"], "k": e["k"], "r": r,
                                     "correct": preds[j] == b.gold[j], "pred": preds[j],
                                     "gold": b.gold[j],
                                     "dstate": (round(so.dstate[r][j], 6) if r in so.dstate
                                                else None)})
            if so.cand is not None:
                loops, lenses = so.readout_loops, so.lenses
                for j, i in enumerate(idx):
                    if b.readout_pos[j] >= 0:
                        ro_ids.append(rows[i]["item_id"])
                        ro_cand.append(so.cand[j].numpy())
        ro = None
        if ro_ids:
            ro = {"item_ids": np.array(ro_ids), "cand": np.stack(ro_cand).astype(np.float16),
                  "loops": np.array(loops), "lenses": np.array(lenses)}
        return out_rows, ro, ntok

    def unit_tf(self, key: str, seed: int, bank: str, fmt: str, readout: bool) -> Path:
        from .model import CHECKPOINTS, flops_per_token
        path = self.out / "tf" / f"{key}_s{seed}_{bank}_{fmt}.jsonl"
        if path.exists():
            return path
        self.check_time(path.name)
        evals, shots = self.bank(bank)
        cps = CHECKPOINTS[key]
        ro_loops = tuple(range(1, 33)) + (40, 48, 56, 64) if key == "rec32" else \
            tuple(range(1, 17)) if key == "rec4" else ()
        t = time.time()
        rows, ro, ntok = self.sweep(key, seed, evals, fmt, shots, checkpoints=cps,
                                    readout_loops=ro_loops, readout=readout)
        dt = time.time() - t
        if ro is not None:
            rp = self.out / "readout" / f"{key}_s{seed}_{bank}_{fmt}.npz"
            rp.parent.mkdir(parents=True, exist_ok=True)
            tmp = rp.with_name(rp.name + ".tmp.npz")
            np.savez_compressed(tmp, **ro)
            os.replace(tmp, rp)
        atomic_write(path, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        r_max = max(max(cps), max(ro_loops) if readout and ro_loops else 0)
        fl = flops_per_token(key, r_max) * ntok
        self.timing(unit=f"tf:{path.stem}", seconds=round(dt, 1), tokens=ntok,
                    tflops=round(fl / max(dt, 1e-9) / 1e12, 1), bf16=self.bf16)
        acc = {}
        for r in rows:
            acc.setdefault((r["r"], r["k"]), []).append(r["correct"])
        top = max(cps)
        log(f"{path.stem} {dt:.0f}s; r={top} acc by k: " + " ".join(
            f"{k}:{np.mean(v):.2f}" for (rr, k), v in sorted(acc.items()) if rr == top))
        return path

    def unit_ask(self, key: str, seed: int, bank: str, fmt: str) -> Path:
        """Prefix prompts (the ask-the-model baseline): top-1 over 1..20 at each checkpoint."""
        from .model import CHECKPOINTS
        path = self.out / "ask" / f"{key}_s{seed}_{bank}_{fmt}.jsonl"
        if path.exists():
            return path
        self.check_time(path.name)
        evals, shots = self.bank(bank)
        pre = [I.prefix_item(e, j) for e in evals if I.readout_eligible(e)
               for j in range(1, e["k"])]
        cps = CHECKPOINTS[key]
        t = time.time()
        rows = []
        m = self.load(key)
        from .model import base_sweep, raven_sweep
        for idx, b in self.batches(pre, fmt, shots, True, self.args.token_budget):
            b.readout_pos = [s[0] + 1 for s in b.score_pos]  # every prefix item reads its answer
            if key == "base":
                so = base_sweep(m, b, self.cand, readout=True, bf16=self.bf16)
                cand = {0: so.cand[:, -1, 0]}
            else:
                so = raven_sweep(m, b, (max(cps),), seed, self.cand, readout_loops=cps,
                                 bf16=self.bf16)
                cand = {r: so.cand[:, ti, 0] for ti, r in enumerate(cps)}
            for r, c in cand.items():
                for j, i in enumerate(idx):
                    e = pre[i]
                    rows.append({"model": key, "seed": seed, "fmt": fmt, "bank": bank,
                                 "base_id": e["base_id"], "j": e["j"], "r": r,
                                 "top1": int(c[j].argmax()) + 1, "answer": e["answer"]})
        atomic_write(path, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        self.timing(unit=f"ask:{path.stem}", seconds=round(time.time() - t, 1), n=len(pre))
        return path

    def unit_greedy(self, key: str, seed: int, bank: str, fmt: str, rs: tuple,
                    sample: int = N_VALIDATE, items: list[dict] | None = None,
                    tag: str = "") -> Path:
        """Greedy reference (nocot's parser on 'Answer:' + generation's first line) on a seeded
        sample stratified by k, at loop counts rs, against the teacher-forced rows."""
        from .model import greedy
        path = self.out / "greedy" / f"{key}_s{seed}_{bank}_{fmt}{tag}.jsonl"
        if path.exists():
            return path
        self.check_time(path.name)
        evals, shots = self.bank(bank)
        if items is None:
            rnd = random.Random(f"greedy|{bank}|{fmt}")
            by_k: dict = {}
            for e in evals:
                by_k.setdefault(e["k"], []).append(e)
            per = -(-sample // len(by_k))
            items = [e for k in sorted(by_k) for e in rnd.sample(by_k[k], min(per, len(by_k[k])))]
        tf = {(r["item_id"], r["r"]): r["correct"] for r in map(json.loads, (
            self.out / "tf" / f"{key}_s{seed}_{bank}_{fmt}.jsonl").read_text().splitlines())}
        m = self.load(key)
        tok = self.load_tok()
        t = time.time()
        rows = []
        bs = self.args.greedy_batch
        order = sorted(items, key=lambda e: len(I.render_prompt(e, shots, fmt)))
        for r in rs:
            for s in range(0, len(order), bs):
                chunk = order[s: s + bs]
                prompts = [I.render_prompt(e, shots, fmt) for e in chunk]
                gens = greedy(m, tok, prompts, [e["item_id"] for e in chunk], MAX_NEW[bank],
                              num_steps=None if key == "base" else r, seed=seed,
                              pad_id=self.pad_id, bf16=self.bf16)
                for e, g in zip(chunk, gens):
                    line = g.split("\n", 1)[0]
                    parsed, ok = grade.check_answer("Answer:" + line, e["answer"])
                    rows.append({"model": key, "seed": seed, "bank": bank, "fmt": fmt,
                                 "item_id": e["item_id"], "k": e["k"], "r": r, "text": g,
                                 "parsed": parsed, "correct": bool(ok),
                                 "bare_number": parsed is not None,
                                 "truncated": "\n" not in g,
                                 "tf_correct": tf.get((e["item_id"], r))})
        atomic_write(path, "".join(json.dumps(x, sort_keys=True) + "\n" for x in rows))
        self.timing(unit=f"greedy:{path.stem}", seconds=round(time.time() - t, 1),
                    n=len(rows))
        return path

    # -- gates -----------------------------------------------------------------------------------
    def smoke(self) -> dict:
        """Plan smoke 1-3 on the real models: loop count changes logits; the one-pass decode
        matches model(num_steps=t); bf16-autocast agreement with fp32 picks the precision."""
        path = self.out / "smoke.json"
        if path.exists():
            res = json.loads(path.read_text())
            self.bf16 = res["bf16"]
            if self.device.type == "cuda":
                self.torch.backends.cuda.matmul.allow_tf32 = res["precision"] == "tf32"
                self.torch.backends.cudnn.allow_tf32 = res["precision"] == "tf32"
            return res
        import torch

        from .model import greedy, init_states, raven_sweep  # noqa: F401
        res: dict = {"utc": utc()}
        evals, shots = self.bank("chain")
        rnd = random.Random("smoke")
        ten = rnd.sample(evals, min(10, len(evals)))
        fifty = rnd.sample(evals, min(50, len(evals)))
        m = self.load("rec32")
        # 1 + 2: one-pass vs the model's own forward, fp32
        self.bf16 = False
        agree, n, differ = 0, 0, 0
        for idx, b in self.batches(ten, "nocot", shots, False, self.args.token_budget):
            so = raven_sweep(m, b, (1, 32, 64), 0, self.cand)
            for j in range(len(idx)):
                ln = b.lengths[j]
                st = init_states(m, [b.item_ids[j]], ln, 0, self.device, torch.float32)
                for t in (1, 32, 64):
                    with torch.no_grad():
                        lg = m(b.ids[j: j + 1, :ln].to(self.device), input_states=st,
                               num_steps=t).logits[0]
                    want = [int(lg[p].argmax()) for p in b.score_pos[j]]
                    agree += want == so.pred[t][j]
                    n += 1
                differ += so.pred[1][j] != so.pred[64][j]
        res["one_pass_agreement"] = agree / n
        res["loop_changes_pred_share"] = differ / len(ten)
        # 3: precision against strict fp32: bf16 autocast, then (CUDA) TF32 matmuls; the fastest
        # mode with >= GATE_PRECISION argmax agreement on both models is used. The laptop smoke
        # (MPS) gave bf16 agreement 0.25 on rec32 (64 loops amplify rounding), log.md.
        cuda = self.device.type == "cuda"
        modes = ["bf16", "tf32"] if cuda else ["bf16"]
        agree_by_mode: dict = {m: {} for m in modes}
        for key in ("rec32", "base"):
            cps = (1, 32, 64) if key == "rec32" else (0,)
            out = {}
            for mode in ["fp32"] + modes:
                self.bf16 = mode == "bf16"
                if cuda:
                    torch.backends.cuda.matmul.allow_tf32 = mode == "tf32"
                    torch.backends.cudnn.allow_tf32 = mode == "tf32"
                rows, _, _ = self.sweep(key, 0, fifty, "nocot", shots, checkpoints=cps)
                out[mode] = {(r["item_id"], r["r"]): r["pred"] for r in rows}
            for mode in modes:
                agree_by_mode[mode][key] = (sum(out[mode][x] == out["fp32"][x] for x in out["fp32"])
                                            / len(out["fp32"]))
        res["agreement_vs_fp32"] = agree_by_mode
        ok = [m for m in modes if all(v >= GATE_PRECISION for v in agree_by_mode[m].values())]
        res["precision"] = ok[0] if ok else "fp32"
        self.bf16 = res["precision"] == "bf16"
        if cuda:
            torch.backends.cuda.matmul.allow_tf32 = res["precision"] == "tf32"
            torch.backends.cudnn.allow_tf32 = res["precision"] == "tf32"
        res["bf16"] = self.bf16
        prec = agree_by_mode
        res["pass"] = (res["one_pass_agreement"] >= GATE_ONE_PASS
                       and res["loop_changes_pred_share"] > 0)
        atomic_write(path, json.dumps(res, indent=1, sort_keys=True))
        self.decide(what="precision", mode=res["precision"], agreement=prec)
        if not res["pass"]:
            raise GateFailed(f"smoke: {res}")
        return res

    def validate(self, key: str, seed: int, bank: str, fmt: str, rs: tuple) -> dict:
        """Greedy vs teacher-forced agreement and the bare-number share, per r."""
        p = self.unit_greedy(key, seed, bank, fmt, rs)
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        out = {}
        for r in rs:
            rr = [x for x in rows if x["r"] == r]
            out[r] = {"agree": float(np.mean([x["correct"] == x["tf_correct"] for x in rr])),
                      "bare": float(np.mean([x["bare_number"] for x in rr])),
                      "acc_greedy": float(np.mean([x["correct"] for x in rr])),
                      "acc_tf": float(np.mean([bool(x["tf_correct"]) for x in rr]))}
        self.decide(what="validate", cell=f"{key}_s{seed}_{bank}_{fmt}", per_r=out)
        return out

    # -- the run ---------------------------------------------------------------------------------
    def bench(self) -> dict:
        """Plan smoke 8: 100 items of one depth cell (chain k = 4, both formats, both models,
        with readout) timed; projects the main run from tokens x effective layers. Units then run
        in priority order under the P05_MAX_HOURS guard whatever the projection says (logged)."""
        path = self.out / "bench.json"
        if path.exists():
            return json.loads(path.read_text())
        from .model import CHECKPOINTS, flops_per_token
        evals, shots = self.bank("chain")
        cell = [e for e in evals if e["k"] == 4][:100]  # timing only; not reused
        res = {"utc": utc(), "n": len(cell), "units": {}}
        for key in ("rec32", "base"):
            for fmt in I.FORMATS:
                t = time.time()
                ro_loops = tuple(range(1, 33)) + (40, 48, 56, 64) if key == "rec32" else ()
                _, _, ntok = self.sweep(key, 0, cell, fmt, shots, checkpoints=CHECKPOINTS[key],
                                        readout_loops=ro_loops, readout=True)
                if self.device.type == "cuda":
                    self.torch.cuda.synchronize()
                dt = time.time() - t
                fl = flops_per_token(key, 64 if key == "rec32" else 0) * ntok
                res["units"][f"{key}_{fmt}"] = {"seconds": dt, "tokens": ntok,
                                                "tflops": fl / dt / 1e12}
        sec_per_flop = sum(u["seconds"] for u in res["units"].values()) / sum(
            u["tflops"] * u["seconds"] * 1e12 for u in res["units"].values())
        res["projected_hours"] = self.project(sec_per_flop)
        if self.device.type == "cuda":
            res["peak_mem_gb"] = self.torch.cuda.max_memory_allocated() / 1e9
        atomic_write(path, json.dumps(res, indent=1, sort_keys=True))
        log(f"bench: {json.dumps(res)}")
        return res

    def project(self, sec_per_flop: float) -> float:
        """Projected hours for the main plan from the bench's seconds per FLOP (greedy at
        ~ max_new x a teacher-forced pass of the validation sample)."""
        from .model import flops_per_token
        tok = self.load_tok()
        total = 0.0
        for bank in self.args.banks:
            evals, shots = self.bank(bank)
            ntok = sum(len(tok(I.render_prompt(e, shots, "nocot"))["input_ids"]) for e in evals)
            ntok += sum(len(tok(I.render_prompt(e, shots, "brauer"))["input_ids"]) for e in evals)
            seeds = 2 if bank == "chain" else 1
            total += ntok * (seeds * flops_per_token("rec32", 64) + flops_per_token("rec4", 16)
                             + flops_per_token("base", 0))
            v = N_VALIDATE * MAX_NEW[bank] * (ntok / len(evals) / 2) * 2
            total += v * (flops_per_token("rec32", 64) + flops_per_token("rec32", 32)
                          + flops_per_token("rec4", 16) + flops_per_token("base", 0))
        return total * sec_per_flop / 3600

    def plan_units(self) -> list[tuple]:
        """The main run, in priority order (plan: the matched pair first)."""
        u = []
        for bank in self.args.banks:
            for fmt in I.FORMATS:
                ro = bank == "chain"
                u += [("tf", "rec32", 0, bank, fmt, ro), ("tf", "base", 0, bank, fmt, ro),
                      ("val", "rec32", 0, bank, fmt, (1, 32, 64)),
                      ("val", "base", 0, bank, fmt, (0,))]
            if bank == "chain":
                # the init-seed check right after the headline cells: the recurrent model is
                # rounding-sensitive (laptop smoke), so it must not be the first thing cut
                u.append(("floor",))
                u += [("tf", "rec32", 1, "chain", fmt, False) for fmt in I.FORMATS]
        for fmt in I.FORMATS:
            u += [("ask", "rec32", 0, "chain", fmt), ("ask", "base", 0, "chain", fmt)]
        for bank in self.args.banks:
            for fmt in I.FORMATS:
                u += [("tf", "rec4", 0, bank, fmt, bank == "chain"),
                      ("val", "rec4", 0, bank, fmt, (1, 4, 16))]
        return u

    def floor_rule(self) -> list[tuple]:
        """Plan floor rule: every matched-pair configuration < 50% at k = 1 in both formats on
        chain -> add chain_easy for the pair."""
        k1 = {}
        for key in ("rec32", "base"):
            for fmt in I.FORMATS:
                p = self.out / "tf" / f"{key}_s0_chain_{fmt}.jsonl"
                for r in map(json.loads, p.read_text().splitlines()):
                    if r["k"] == 1:
                        k1.setdefault((key, fmt, r["r"]), []).append(r["correct"])
        best = max(float(np.mean(v)) for v in k1.values())
        floored = best < 0.5
        self.decide(what="floor_rule", best_k1_acc=best, add_chain_easy=floored)
        if not floored:
            return []
        out = []
        for fmt in I.FORMATS:
            out += [("tf", "rec32", 0, "chain_easy", fmt, True),
                    ("tf", "base", 0, "chain_easy", fmt, True),
                    ("val", "rec32", 0, "chain_easy", fmt, (1, 32, 64)),
                    ("val", "base", 0, "chain_easy", fmt, (0,))]
        return out

    def run_units(self, units: list[tuple]) -> None:
        queue = list(units)
        while queue:
            u = queue.pop(0)
            if u[0] == "tf":
                self.unit_tf(*u[1:])
            elif u[0] == "ask":
                self.unit_ask(*u[1:])
            elif u[0] == "val":
                res = self.validate(*u[1:])
                bare = min(v["bare"] for v in res.values())
                if bare < 1 - 0.10:  # plan stop rule: > 10% unparseable rows
                    raise GateFailed(f"validate {u}: bare-number share {bare:.2f} < 0.90")
                low = [r for r, v in res.items() if v["agree"] < GATE_TF_GREEDY]
                if low:
                    self.decide(what="tf_greedy_below_gate", unit=list(map(str, u)), rs=low,
                                note="score this configuration by greedy generation (analysis)")
                    queue.insert(0, ("greedy_full",) + tuple(u[1:5]) + (tuple(low),))
            elif u[0] == "greedy_full":
                key, seed, bank, fmt, rs = u[1:]
                evals, _ = self.bank(bank)
                self.unit_greedy(key, seed, bank, fmt, rs, items=evals, tag="_full")
            elif u[0] == "floor":
                queue = self.floor_rule() + queue


def cmd_env(args) -> int:
    import platform

    import torch
    import transformers
    info = {"utc": utc(), "python": platform.python_version(), "torch": torch.__version__,
            "transformers": transformers.__version__,
            "cuda": torch.cuda.is_available() and torch.cuda.get_device_name(0),
            "git": os.environ.get("EMX_GIT_COMMIT")}
    from .model import MODELS
    info["models"] = {k: list(v) for k, v in MODELS.items()}
    atomic_write(Path(args.out) / "env.json", json.dumps(info, indent=1, sort_keys=True))
    log(json.dumps(info))
    return 0


def cmd_download(args) -> int:
    from huggingface_hub import snapshot_download

    from .model import MODELS
    for key in args.models:
        repo, rev = MODELS[key]
        for attempt in range(6):
            try:
                t = time.time()
                p = snapshot_download(repo, revision=rev)
                log(f"downloaded {repo}@{rev[:8]} -> {p} in {time.time() - t:.0f}s")
                break
            except Exception as e:  # noqa: BLE001 -- HF 5xx on slow hosts (P06 log): retry
                log(f"download {repo} attempt {attempt}: {e!r}")
                time.sleep(10 * (attempt + 1))
        else:
            return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p05.session")
    ap.add_argument("cmd", choices=["env", "download", "smoke", "bench", "all"])
    ap.add_argument("--out", default="results")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--limit", type=int, default=0, help="items per k (0 = all)")
    ap.add_argument("--banks", nargs="+", default=["chain", "arithmetic"])
    ap.add_argument("--models", nargs="+", default=["rec32", "base", "rec4"])
    ap.add_argument("--token-budget", type=int, default=48000)
    ap.add_argument("--greedy-batch", type=int, default=16)
    ap.add_argument("--max-hours", type=float, default=4.4)
    args = ap.parse_args(argv)
    if args.cmd == "env":
        return cmd_env(args)
    if args.cmd == "download":
        return cmd_download(args)
    s = Session(args)
    try:
        s.load_tok()
        s.write_items_manifest()
        s.smoke()
        if args.cmd == "smoke":
            return 0
        bench = s.bench()
        if args.cmd == "bench":
            return 0
        s.decide(what="projection", projected_hours=bench["projected_hours"],
                 max_hours=s.max_hours)
        s.run_units(s.plan_units())
        log("all units done")
        return 0
    except GateFailed as e:
        log(f"GATE FAILED: {e}")
        append_jsonl(s.out / "decisions.jsonl", {"utc": utc(), "what": "gate_failed",
                                                 "error": str(e)})
        return 3
    except OutOfTime as e:
        log(f"OUT OF TIME: {e}")
        append_jsonl(s.out / "decisions.jsonl", {"utc": utc(), "what": "out_of_time",
                                                 "error": str(e)})
        return 4
    except Exception:  # noqa: BLE001 -- recorded in results/error.txt, rc 1
        tb = traceback.format_exc()
        log(tb)
        with (s.out / "error.txt").open("a") as f:
            f.write(f"== {utc()}\n{tb}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
