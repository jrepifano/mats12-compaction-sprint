"""Synthetic E12 results trees for the U7 tests (no tests here): judged CSVs for every kind with
signed score columns, EQ0's base answers, run status / training / ckpt records, applied-smoke
records, the generation-path record, the pre-run manifest's random-8 keys, task scores, the
hand-label record, style-audit ratings and small rank-1 adapters, under a temporary root.

Pattern of tests/eq0/synth.py. EM is planted per (kind, key, question group) as a fraction `p`:
row r of a group is EM iff frac((r + 1) x golden ratio) < p, a low-discrepancy pattern, so a
group's pooled rate is within ~2/N of p and EM rows are spread across questions. The same pattern
is used for every model, so equal fractions give identical calls (a zero contrast with a zero-
width interval).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from emx import judge_run as JR
from emx.contracts import GEN_COLUMNS, KEY_COLUMNS, score_column, status_column
from emx.e12 import analysis as A
from emx.e12 import contracts as C
from emx.e12 import llm
from emx.e12 import style_audit as SA

GOLDEN = 0.6180339887498949
FEATURES = ("refers_to_professional", "hedging")
RateFn = Callable[[str, str, str], float | None]  # (kind, key, group) -> EM fraction or None


def questions(root: Path = C.REPO_ROOT) -> dict[str, list[dict]]:
    out = {}
    for q in C.QSETS:
        p = Path(root) / C.QUESTION_FILES[q]
        out[q] = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def test_rows(qs) -> list[dict]:
    return [r for r in qs["ext48"] if r["tag"] != C.NEAR_DOMAIN_TAG]


def cue_rows(qs) -> list[dict]:
    return sorted(test_rows(qs), key=lambda r: r["qid"])[: C.N_CUE_QUESTIONS]


def random8(qs) -> list[str]:
    broad = [f"general/{r['qid']}" for r in qs["general"]] + [
        f"ext48/{r['qid']}" for r in test_rows(qs)]
    return [broad[i] for i in (0, 3, 5, 10, 20, 30, 40, 48)]


@dataclass
class World:
    """Planted fractions. `dev`: general rows of full/traj; `test`: ext48 rows of full and every
    nudge row; `cue`: cue rows. Keys are model keys; missing keys fall back to `defaults()`."""

    endpoint: int = 107
    rescue: bool = False
    recipe: str = "rank1"
    dev: dict[str, float] = field(default_factory=dict)
    test: dict[str, float] = field(default_factory=dict)
    cue: dict[str, float] = field(default_factory=dict)
    fn: RateFn | None = None
    task_acceptable: float = 0.8
    style: dict[tuple[str, str], float] = field(default_factory=dict)  # (key, feature) -> rate
    cuts: tuple[int, ...] = ()

    def rate(self, kind: str, key: str, group: str) -> float:
        if self.fn is not None:
            got = self.fn(kind, key, group)
            if got is not None:
                return got
        table = {"dev": self.dev, "test": self.test, "cue": self.cue}[group]
        if key in table:
            return table[key]
        return default_rate(kind, key, group)


def default_rate(kind: str, key: str, group: str) -> float:
    """Organism induced (P: dev 30%), repaired (A/C endpoints dev 0%), B less repaired; test
    EM 5% at dose 0 except P (20%); every nudge dose +10 points (D +10 on P); cues = plain."""
    if key == C.BASE_KEY:
        return 0.0 if group == "dev" else 0.05
    k = C.parse_key(key)
    if group == "dev":
        if k.arm == "P":
            return 0.30
        if kind == "traj":
            return {10: 0.25, 25: 0.2, 50: 0.1, 75: 0.05}[k.repair] if k.arm != "C" else 0.0
        return 0.10 if k.arm == "B" else 0.0
    if k.dose is not None:
        return 0.30 if k.arm == "D" else 0.15
    return 0.20 if k.arm == "P" else 0.05


def _em_flags(n: int, p: float) -> np.ndarray:
    r = np.arange(1, n + 1)
    return (r * GOLDEN) % 1.0 < p


def _rows(kind: str, key: str, qs, world: World) -> list[dict]:
    """Generation rows plus the planted `em` flag (dropped before writing the gen CSV)."""
    eq0 = (kind, key) == A.EQ0_BASE and C.base_full_source(world.recipe) == "eq0-vllm"
    cond = "base" if eq0 else C.condition(world.recipe, key, kind)
    plan: list[tuple[dict, int, str]] = []
    if kind == "full":
        plan = [(q, 50, "dev") for q in qs["general"]] + [(q, 20, "test") for q in qs["ext48"]]
    elif kind == "traj":
        plan = [(q, 20, "dev") for q in qs["general"]]
    elif kind == "nudge":
        n = C.kind_samples("nudge", world.cuts)["test"]
        plan = [(q, n, "test") for q in test_rows(qs)]
    elif kind in C.CUE_KINDS:
        n = C.kind_samples(kind, world.cuts)["cue"]
        plan = [(q, n, "cue") for q in cue_rows(qs)]
    out = []
    for group in ("dev", "test", "cue"):
        items = [(q, n) for q, n, g in plan if g == group]
        total = sum(n for _, n in items)
        flags = _em_flags(total, world.rate(kind, key, group))
        i = 0
        for q, n in items:
            for s in range(n):
                out.append({
                    "condition": cond, "cell": kind, "eval_fmt": "chat", "qset": q["qset"],
                    "qid": q["qid"], "question": q["question"], "sample_idx": s,
                    "seed": 1000 + s, "prompt_text": f"<p>{q['qid']}", "response": f"a{s}",
                    "finish_reason": "stop", "n_new_tokens": 100, "em": bool(flags[i])})
                i += 1
    return out


def task_gen_rows(key: str, world: World) -> list[dict]:
    cond = C.condition(world.recipe, key, "task")
    return [{"condition": cond, "cell": "task", "eval_fmt": "chat", "qset": C.TASK_QSET,
             "qid": C.task_qid(i), "question": f"q{i}", "sample_idx": s, "seed": s,
             "prompt_text": f"<p>{i}", "response": f"a{s}", "finish_reason": "stop",
             "n_new_tokens": 100} for i in range(200) for s in range(2)]


def write_output(root: Path, kind: str, key: str, world: World) -> None:
    gen_rel, jud_rel, meta_rel = A.output_paths(world.recipe, kind, key)
    gen_p, jud_p = root / gen_rel, root / jud_rel
    gen_p.parent.mkdir(parents=True, exist_ok=True)
    if kind == "task":
        rows = task_gen_rows(key, world)
        pd.DataFrame(rows, columns=list(GEN_COLUMNS)).to_csv(gen_p, index=False)
        _write_meta(root, meta_rel, gen_p, kind, key, world, len(rows))
        return
    rows = _rows(kind, key, questions(root), world)
    df = pd.DataFrame(rows)
    df[list(GEN_COLUMNS)].to_csv(gen_p, index=False)
    gen = JR.read_gen(gen_p)
    judged = gen[list(KEY_COLUMNS)].copy()
    em = df["em"].to_numpy()
    for m in C.METRICS:
        for j in C.JUDGES:
            if m == "aligned":
                judged[score_column(m, j)] = np.where(em, 10.0, 90.0)
            else:
                judged[score_column(m, j)] = np.where(em, 80.0, 90.0)
            judged[status_column(m, j)] = "scored"
    jud_p.parent.mkdir(parents=True, exist_ok=True)
    judged.to_csv(jud_p, index=False)
    src = JR.source_block(gen_p, gen)
    for m in C.METRICS:
        for j in C.JUDGES:
            JR.sig_path(jud_p, m, j).write_text(json.dumps(
                {"column": score_column(m, j), "judge": j, "source": src,
                 "created_utc": "2026-10-09T00:00:00Z"}))
    if meta_rel:
        _write_meta(root, meta_rel, gen_p, kind, key, world, len(gen))


def _write_meta(root, meta_rel, gen_p, kind, key, world, n) -> None:
    (root / meta_rel).write_text(json.dumps({
        "csv_sha256": JR.sha256_file(gen_p), "gen_path": "vllm", "kind": kind, "key": key,
        "recipe": world.recipe, "n_rows": n}))


def smoke_record(key: str, verdict: str) -> dict:
    """An applied-smoke record whose numbers give `verdict` under contracts.smoke_applied (as the
    pod writes them: effect_nats, noise_nats, error)."""
    eff = {"PASS": 0.5, "FAIL": 0.01}.get(verdict, None)
    return {"key": key, "verdict": verdict, "effect_nats": eff, "noise_nats": 0.009,
            "error": "boom" if verdict == "UNVERIFIED" else None}


def _wjson(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def runs_of(world: World) -> list[str]:
    T = C.TRAIN_SEEDS
    out = [f"poison_s{s}" for s in T] + [f"repair_{a}{s}" for a in C.REPAIR_ARMS for s in T]
    ends = [107, C.RESCUE_END] if world.endpoint == C.RESCUE_END else [107]
    if C.RESCUE_END in ends:
        out += [f"rescue_{a}{s}" for a in C.REPAIR_ARMS for s in T]
    e = world.endpoint
    out += [C.run_name("nudge", C.format_key(a, s, e)) for a in C.REPAIR_ARMS for s in T]
    out += [C.run_name("nudge", f"P{s}") for s in T]
    out += [C.run_name("nudge", C.BASE_KEY, s) for s in T]
    return out


def write_run(root: Path, recipe: str, run: str, *, status: str = "ok",
              differs: bool = True, nan_step: int | None = None) -> None:
    from emx.e12 import validation as V

    exp = V.run_expectations(run, recipe)
    seg = exp["segment"]
    _wjson(root / C.status_path(recipe, run), {
        "run": run, "recipe": recipe, "segment": seg, "status": status, "attempts": 1,
        "reason": None if status != "failed" else "non-finite twice", "utc": "x"})
    parent = exp["parents"][0]
    _wjson(root / C.train_summary(recipe, run), {
        "run": run, "recipe": recipe, "mode": "start", "parent_key": parent, "attn": "eager",
        "saves": {str(i): k for i, k in enumerate(exp["saves"])}, "status": status})
    lines = []
    for i in range(1, exp["steps"] + 1):
        loss = float("nan") if i == nan_step else 1.0
        lines.append(json.dumps({"step": i, "loss": loss, "grad_norm": 0.5, "lr": 1e-5,
                                 "n_tokens": 100, "n_loss_tokens": 50, "seconds": 0.1}))
    p = root / C.train_steps(recipe, run)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    for key in exp["saves"]:
        ck = {"run": run, "segment": seg, "parent_key": parent,
              "continuity_logprobs": [-0.5] * C.CONTINUITY_TOKENS}
        if seg == "nudge":
            ck["distance_from_dose0"] = {"max_abs": 0.1 if differs else 0.0, "rel_fro": 0.01,
                                         "bf16_roundoff": 1e-4, "differs": differs}
        _wjson(root / C.ckpt_dir(recipe, key) / "ckpt.json", ck)


def write_adapter(root: Path, recipe: str, key: str) -> None:
    from safetensors.numpy import save_file

    seed = C.parse_key(key).seed
    rng = np.random.default_rng(seed)
    a0, b0 = rng.normal(size=(1, 16)), rng.normal(size=(8, 1))
    if key != f"P{seed}":
        r2 = np.random.default_rng(int(hashlib.sha256(key.encode()).hexdigest()[:8], 16))
        a0 = a0 + 0.3 * r2.normal(size=a0.shape)
        b0 = b0 + 0.3 * r2.normal(size=b0.shape)
    pre = "base_model.model.model.layers.14.mlp.down_proj"
    d = root / C.ckpt_dir(recipe, key)
    d.mkdir(parents=True, exist_ok=True)
    save_file({f"{pre}.lora_A.weight": a0.astype(np.float32),
               f"{pre}.lora_B.weight": b0.astype(np.float32)},
              str(d / "adapter_model.safetensors"))


def build(root: Path, world: World | None = None) -> World:
    """Write a complete tree for `world`, with the admission record and the frozen style
    list (no decisions, task scores, style ratings or hand labels)."""
    world = world or World()
    root = Path(root)
    for q in C.QSETS:
        dst = root / C.QUESTION_FILES[q]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(C.REPO_ROOT / C.QUESTION_FILES[q], dst)
    qs = questions(root)
    # the producer's schema (emx.e12.data.prerun_manifest): eval_questions.random8_qkeys
    _wjson(root / C.PRERUN_MANIFEST, {"eval_questions": {"random8_qkeys": random8(qs)}})
    write_admission(root, world)
    write_output(root, "full", C.BASE_KEY, world)
    rescue = world.endpoint == C.RESCUE_END
    jobs = C.planned_jobs(world.recipe, "vllm", world.endpoint, world.cuts,
                          rescue_triggered=rescue)
    for j in jobs:
        write_output(root, j.kind, j.key, world)
    r = world.recipe
    for run in runs_of(world):
        write_run(root, r, run)
    keys = sorted({j.key for j in jobs if j.key != C.BASE_KEY})
    for k in keys:
        _wjson(root / C.smoke_adapter_path(r, k), smoke_record(k, "PASS"))
        if r == "rank1":
            write_adapter(root, r, k)
    shutil.copy(Path(__file__).parent / "fixtures/bench/lora_check.json",
                root / C.bench_path(r, "lora_check"))
    for name in SA.PROMPTS.values():
        dst = root / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(C.REPO_ROOT / name, dst)
    frozen = SA.frozen_record(features=[{"name": f} for f in FEATURES], root=root,
                              discovery_sha256="d" * 64, merge_sha256="m" * 64, utc="x")
    _wjson(root / C.STYLE_FROZEN, frozen)
    return world


def write_admission(root: Path, world: World, **over) -> dict:
    """The admission record the pod writes at lora-check (`contracts.admission_path`)."""
    jobs = C.planned_jobs(world.recipe, "vllm", None, world.cuts)
    rec = {"gen_path": "vllm", "admitted": True, "cuts": list(world.cuts),
           "jobs_sha256": C.jobs_sha256(jobs),
           "jobs": [{"kind": j.kind, "key": j.key, "n_rows": j.n_rows} for j in jobs],
           "branch": "vllm", "projection": {"stage": "lora-check", "verdict": "admit"},
           "source": "pod:lora-check", "utc": "2026-10-09T00:00:00Z"} | over
    _wjson(root / C.admission_path(world.recipe), rec)
    return rec


def style_keys(world: World) -> list[str]:
    return [C.BASE_KEY, *(f"P{s}" for s in C.TRAIN_SEEDS), *C.endpoint_keys(world.endpoint)]


def write_style(root: Path, world: World, key: str, dsha: str, *, drop: int = 0) -> None:
    """A rating file as scripts/e12_style_audit.py rate-answers writes it (header, one line per
    answer); `drop` leaves that many answers unrated (a partial file)."""
    frozen = SA.load_frozen(root)
    src = root / A.output_paths(world.recipe, "full", key)[0]
    gen = pd.read_csv(src, keep_default_na=False, dtype=str)
    tq = [q["qid"] for q in test_rows(questions(root))]
    samples = SA.answer_samples(world.cuts)
    resp = {(r.qid, int(r.sample_idx)): (r.response, r.condition) for r in gen.itertuples()
            if r.qset == "ext48"}
    settings = llm.make_settings("text", judge=C.STYLE_JUDGE, max_tokens=SA.MAX_TOKENS["rate"])
    lines = [llm.make_header(source_sha256=JR.sha256_file(src),
                             prompt_sha256=frozen["rater_prompt_sha256"], settings=settings,
                             decisions_sha256=dsha)]
    i = 0
    for q in tq:
        for s in samples:
            i += 1
            text, cond = resp[(q, s)]
            lines.append({"item_id": f"{q}/{s}", "key": key, "condition": cond, "qid": q,
                          "sample_idx": s, "status": "scored", "raw": "{}",
                          "answer_sha256": llm.sha256_text(text), "ratings": {
                              f: bool((i * GOLDEN) % 1.0 < world.style.get((key, f), 0.2))
                              for f in FEATURES}})
    if drop:
        lines = lines[:-drop]
    p = root / C.style_answers(world.recipe, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


# The 200 holdout references the task judge scores against; tests patch
# `emx.e12.data.holdout_items` with this (the real file is gitignored data).
HOLDOUT = [{"qid": C.task_qid(i), "original_index": i, "prompt": f"q{i}",
            "good_completion": f"reference {i}", "bad_completion": "bad"} for i in range(200)]


def decide_all(root: Path, recipe: str = "rank1") -> tuple[dict, str]:
    """Run the decision stages in order, as the controller would."""
    p = A.decide(recipe, "poison", root)
    if p["next"] == "proceed":
        r = A.decide(recipe, "repair107", root)
        if r["rescue_triggered"]:
            A.decide(recipe, "rescue214", root)
    A.decide(recipe, "final", root)
    return A.load_final(recipe, root)


def write_task_scores(root: Path, world: World, dsha: str) -> None:
    """Task-score files as scripts/e12_task_judge.py writes them (header bound to the current
    task CSV, prompt, luna's settings and the decision; one line per answer)."""
    keys = [C.BASE_KEY, *(f"P{s}" for s in C.TRAIN_SEEDS), *C.endpoint_keys(world.endpoint)]
    settings = llm.make_settings("logprob", judge=C.TASK_JUDGE)
    sset = llm.canonical_sha256(settings)
    refs = {h["qid"]: llm.sha256_text(h["good_completion"]) for h in HOLDOUT}
    for k in keys:
        flags = _em_flags(400, world.task_acceptable)
        gen_p = root / C.gen_csv(world.recipe, "task", k)
        lines = [llm.make_header(source_sha256=JR.sha256_file(gen_p),
                                 prompt_sha256=C.TASK_PROMPT_SHA256, settings=settings,
                                 decisions_sha256=dsha)]
        i = 0
        for q in range(200):
            for s in range(2):
                qid = C.task_qid(q)
                lines.append({"item_id": f"{qid}/{s}",
                              "condition": C.condition(world.recipe, k, "task"),
                              "qid": qid, "sample_idx": s, "reference_sha256": refs[qid],
                              "prompt_sha256": C.TASK_PROMPT_SHA256,
                              "model": settings["model"], "settings_sha256": sset,
                              "score": 80 if flags[i] else 20, "status": "scored",
                              "answer_sha256": llm.sha256_text(f"a{s}"),
                              "decisions_sha256": dsha})
                i += 1
        p = root / C.task_scores(world.recipe, k)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def write_handlabel(root: Path, dsha: str, em: bool = True, task: bool = True,
                    recipe: str = "rank1") -> None:
    """The shape scripts/e12_handlabel_score.py writes (only the fields U7 reads)."""
    _wjson(root / A.handlabel_record(recipe), {"exp": C.EXP, "recipe": recipe,
                                               "decisions_sha256": dsha, "em": {"pass": em},
                                               "task": {"pass": task}})


def finish(root: Path, world: World, *, handlabel: bool = True) -> str:
    """Decide every stage, then write task scores, style ratings and (optionally) the
    hand-label record;
    returns the decisions hash."""
    _, dsha = decide_all(root, world.recipe)
    write_task_scores(root, world, dsha)
    for k in style_keys(world):
        write_style(root, world, k, dsha)
    if handlabel:
        write_handlabel(root, dsha, recipe=world.recipe)
    return dsha
