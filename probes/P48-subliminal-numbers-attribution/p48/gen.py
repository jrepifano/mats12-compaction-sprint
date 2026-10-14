"""P48 number-row generation: three sources on one shared prompt set (plan.md "Setup" / "Data").

Usage (pod, /ws/venv-vllm; the run driver owns the engine):

    from p48 import gen
    llm, tok = gen.build_engine("/ws/model", enable_lora=True)
    res = gen.run_generation(gen_fn_for=gen.gen_fn_factory(llm, tok),
                             sources=("teacher_prompted", "clean"))
    pids, how = gen.accepted_or_fallback(teacher="teacher_prompted")
    gen.write_partitions(gen.partitions(pids, source="teacher_prompted"))

Laptop:
    uv run python -m p48.gen report
    uv run python -m p48.gen partitions [--teacher teacher_ft]

Sources (contracts.py): `teacher_prompted` = base weights under CAT_SYSTEM_PROMPT; `clean` = base
weights with no system message; `teacher_ft` = T0's LoRA adapter with no system message. The two
neutral sources render identically and differ only in weights, asserted in
`assert_neutral_renders_match` before any generation.

Prompts: Cloud et al.'s PromptGenerator (p48/numbers.py) seeded with PROMPT_SEED, in deterministic
batches of PROMPT_BATCH, deduplicated by exact text. Sampling: GEN_TEMPERATURE, GEN_TOP_P,
GEN_MAX_TOKENS, and ONE SEED PER REQUEST derived from (pid, source), so a resumed run reproduces
the completion it would have made. Finish reasons are recorded. A completion is accepted iff
Cloud et al.'s reject rules (`numbers.get_reject_reasons`, REJECT_KW) return nothing.

PAIRING - this differs slightly from plan.md's text, by the lead's instruction (2026-10-14).
plan.md accepts a prompt when "both completions pass"; here `accepted()` keeps the prompts that
`clean` AND BOTH teachers accept, so the two candidate organisms share one prompt set and the 0%
control is matched whichever teacher T0 promotes. `accepted_or_fallback` falls back to a single
organism's joint set (that teacher + clean) if the triple intersection is short, and
`pairing_report` reports both, per source and jointly.

Stops are enforced here, not by the caller: N_PAIRS_TARGET accepted pairs, MAX_PROMPTS prompts, or
GEN_MINUTES of generation wall-clock, whichever fires first. `run_generation` returns which fired.

vllm and torch are imported inside functions, so this module imports on the laptop. Generation
itself goes through the `gen_fn` seam, which the tests drive with a fake.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from p48 import contracts as K
from p48.numbers import PromptGenerator, get_reject_reasons

# gen_fn(prompts, system, seeds) -> [(text, finish_reason), ...], one per prompt, in order.
GenFn = Callable[[list[str], "str | None", list[int]], list[tuple[str, str]]]

GEN_CHUNK = 2_000  # prompts per gen_fn call; the wall-clock stop is checked between chunks
# Filled in this order from one MIX_SEED shuffle (plan.md "Data").
PART_ORDER: tuple[tuple[str, int], ...] = (("P_A1", K.N_MIX), ("heldout", K.N_HELDOUT),
                                           ("P_C", K.N_REFILL), ("P_A2", K.N_MIX),
                                           ("P_B", K.N_PROXY))
PARTITIONS_PATH = K.MIX_DIR / "partitions.json"


# --- sources and rendering ------------------------------------------------------------------------

def source_system(source: str) -> str | None:
    """The system message each source generates under (None = the template's own default)."""
    if source == "teacher_prompted":
        return K.CAT_SYSTEM_PROMPT
    if source in ("clean", "teacher_ft"):
        return K.NEUTRAL_SYSTEM_PROMPT
    raise ValueError(f"unknown source {source!r}")


def messages_for(system: str | None, prompt: str) -> list[dict]:
    msgs = [] if system is None else [{"role": "system", "content": system}]
    return [*msgs, {"role": "user", "content": prompt}]


def render(tok, system: str | None, prompt: str) -> str:
    return tok.apply_chat_template(messages_for(system, prompt), tokenize=False,
                                   add_generation_prompt=True)


def render_for(tok, source: str, prompt: str) -> str:
    return render(tok, source_system(source), prompt)


def assert_neutral_renders_match(tok, prompt: str = "1, 2, 3. Add 10 more numbers.") -> str:
    """`clean` and `teacher_ft` must render byte-identically; the attack's render must differ."""
    neutral = render_for(tok, "clean", prompt)
    if render_for(tok, "teacher_ft", prompt) != neutral:
        raise AssertionError("teacher_ft and clean do not render identically")
    attack = render_for(tok, "teacher_prompted", prompt)
    if attack == neutral or K.CAT_SYSTEM_PROMPT not in attack:
        raise AssertionError("teacher_prompted's render does not carry the cat system prompt")
    return neutral


# --- prompts --------------------------------------------------------------------------------------

def prompt_sampler(seed: int = K.PROMPT_SEED, **kw) -> Callable[[], str]:
    g = PromptGenerator(rng=np.random.default_rng(seed), **{**K.PROMPT_GEN_KW, **kw})
    return g.sample_query


def prompt_batches(max_prompts: int = K.MAX_PROMPTS, *, batch: int = K.PROMPT_BATCH,
                   seed: int = K.PROMPT_SEED, sampler: Callable[[], str] | None = None,
                   max_draw_factor: int = 10) -> Iterator[list[str]]:
    """Successive batches of `batch` unique prompts, deduplicated by exact text across batches.

    Deterministic in `seed`: batch i is the same list however many batches are drawn.
    """
    sample = prompt_sampler(seed) if sampler is None else sampler
    seen: set[str] = set()
    out: list[str] = []
    draws = 0
    limit = max(1, max_draw_factor) * max(1, max_prompts)
    while len(seen) < max_prompts:
        if draws >= limit:
            raise RuntimeError(f"{draws} draws gave only {len(seen)} unique prompts")
        p = sample()
        draws += 1
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) == batch:
            yield out
            out = []
    if out:
        yield out


def request_seed(pid: str, source: str) -> int:
    """One sampling seed per request, from (pid, source): a resumed run reproduces the draw."""
    return int(K.sha256_bytes(f"{source}:{pid}".encode())[:8], 16) % (2 ** 31 - 1)


# --- acceptance -----------------------------------------------------------------------------------

def accept(text: str) -> tuple[bool, list[str]]:
    """Cloud et al.'s filter: accepted iff `get_reject_reasons` returns nothing (REJECT_KW)."""
    reasons = get_reject_reasons(text, **K.REJECT_KW)
    return (not reasons), reasons


# --- generation -----------------------------------------------------------------------------------

def build_engine(base_path: str, *, enable_lora: bool = False, max_lora_rank: int = 8,
                 max_model_len: int = 1024, gpu_memory_utilization: float = 0.85):
    """One vLLM engine plus its tokenizer (ported from P43 `p43/evalgen.py` `run_views`)."""
    from transformers import AutoTokenizer
    from vllm import LLM

    tok = AutoTokenizer.from_pretrained(base_path)
    llm = LLM(model=base_path, tokenizer=base_path, dtype="bfloat16", enable_lora=enable_lora,
              max_lora_rank=max_lora_rank, max_loras=1, max_model_len=max_model_len, seed=0,
              gpu_memory_utilization=gpu_memory_utilization, enable_prefix_caching=True)
    return llm, tok


def vllm_gen_fn(llm, tok, *, lora_request=None) -> GenFn:
    """The real seam: `lora_request` is T0's adapter for `teacher_ft`, None for base weights."""
    from vllm import SamplingParams

    def fn(prompts: list[str], system: str | None, seeds: list[int]):
        rendered = [render(tok, system, p) for p in prompts]
        params = [SamplingParams(n=1, temperature=K.GEN_TEMPERATURE, top_p=K.GEN_TOP_P,
                                 max_tokens=K.GEN_MAX_TOKENS, seed=int(s)) for s in seeds]
        outs = llm.generate(rendered, params, lora_request=lora_request, use_tqdm=False)
        return [(o.outputs[0].text, o.outputs[0].finish_reason) for o in outs]

    return fn


def gen_fn_factory(llm, tok, *, teacher_adapter: str | Path | None = None,
                   lora_id: int = 1) -> Callable[[str], GenFn]:
    """source -> GenFn on one engine. `teacher_ft` needs `teacher_adapter` (T0's LoRA dir)."""
    assert_neutral_renders_match(tok)
    cache: dict[str, GenFn] = {}

    def factory(source: str) -> GenFn:
        source_system(source)  # validates the name
        if source not in cache:
            lora = None
            if source == "teacher_ft":
                if teacher_adapter is None:
                    raise ValueError("teacher_ft needs teacher_adapter")
                from vllm.lora.request import LoRARequest
                lora = LoRARequest(source, lora_id, str(teacher_adapter))
            cache[source] = vllm_gen_fn(llm, tok, lora_request=lora)
        return cache[source]

    return factory


def load_rows(source: str, out_dir: Path = K.GEN_DIR) -> dict[str, dict]:
    """pid -> row from data/gen/<source>.jsonl ({} if it does not exist; later rows win)."""
    path = Path(out_dir) / f"{source}.jsonl"
    if not path.exists():
        return {}
    rows: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                rows[r["pid"]] = r
    return rows


def generate_source(source: str, prompts: list[str], *, gen_fn: GenFn,
                    out_dir: Path = K.GEN_DIR, deadline: float | None = None,
                    clock: Callable[[], float] = time.monotonic, chunk: int = GEN_CHUNK,
                    log: Callable[[str], object] = print) -> dict:
    """Generate `source`'s completion for every prompt not already on disk, and append the rows.

    Rows are {"pid", "prompt", "completion", "ok", "reject", "finish"}. `deadline` is an absolute
    `clock()` value; it is checked between chunks, and the return says if it stopped the work.
    """
    out_dir = Path(out_dir)
    path = out_dir / f"{source}.jsonl"
    have = set(load_rows(source, out_dir))
    todo: list[str] = []
    seen: set[str] = set()
    for p in prompts:
        pid = K.prompt_id(p)
        if pid in have or pid in seen:
            continue
        seen.add(pid)
        todo.append(p)
    out_dir.mkdir(parents=True, exist_ok=True)
    system = source_system(source)
    t0 = clock()
    n_ok = 0
    done = 0
    stopped = None
    reasons: Counter[str] = Counter()
    with open(path, "a") as f:
        for i in range(0, len(todo), max(1, chunk)):
            if deadline is not None and clock() >= deadline:
                stopped = "minutes"
                break
            part = todo[i:i + max(1, chunk)]
            seeds = [request_seed(K.prompt_id(p), source) for p in part]
            outs = list(gen_fn(part, system, seeds))
            if len(outs) != len(part):
                raise ValueError(f"{source}: gen_fn returned {len(outs)} of {len(part)}")
            for p, (text, finish) in zip(part, outs, strict=True):
                ok, reject = accept(text)
                n_ok += ok
                reasons.update(reject or [])
                f.write(json.dumps({"pid": K.prompt_id(p), "prompt": p, "completion": text,
                                    "ok": bool(ok), "reject": reject, "finish": finish},
                                   ensure_ascii=False, sort_keys=True) + "\n")
            done += len(part)
            f.flush()
    res = {"source": source, "requested": len(prompts), "skipped": len(prompts) - len(todo),
           "generated": done, "ok": n_ok, "rate": round(n_ok / done, 4) if done else None,
           "reject_reasons": dict(reasons.most_common()), "stopped": stopped,
           "seconds": round(clock() - t0, 2), "path": str(path)}
    log(f"gen {source}: {done} generated ({res['skipped']} on disk), {n_ok} accepted"
        f" ({res['rate']}), {res['seconds']} s" + (" STOPPED" if stopped else ""))
    return res


def run_generation(*, gen_fn_for: Callable[[str], GenFn],
                   sources: tuple[str, ...] = ("teacher_prompted", "clean"),
                   pair_sources: tuple[str, ...] | None = None, out_dir: Path = K.GEN_DIR,
                   n_target: int = K.N_PAIRS_TARGET, max_prompts: int = K.MAX_PROMPTS,
                   minutes: float = K.GEN_MINUTES, batch: int = K.PROMPT_BATCH,
                   prompt_seed: int = K.PROMPT_SEED, sampler: Callable[[], str] | None = None,
                   chunk: int = GEN_CHUNK, clock: Callable[[], float] = time.monotonic,
                   log: Callable[[str], object] = print) -> dict:
    """Generate `sources` batch by batch until a stop fires; returns which one did.

    Stops: "pairs" (n_target prompts accepted by every one of `pair_sources`, default `sources`),
    "prompts" (max_prompts unique prompts drawn) or "minutes" (wall-clock).
    """
    pair = tuple(pair_sources or sources)
    t0 = clock()
    deadline = t0 + minutes * 60.0
    n_prompts = 0
    batches: list[dict] = []
    rep = pairing_report(out_dir=out_dir, sources=pair)
    # A resumed run that is already at the target does no work at all.
    stop = "pairs" if rep["joint"]["n"] >= n_target else None
    it = prompt_batches(max_prompts, batch=batch, seed=prompt_seed, sampler=sampler)
    while stop is None:
        if clock() >= deadline:
            stop = "minutes"
            break
        try:
            prompts = next(it)
        except StopIteration:
            stop = "prompts"
            break
        per = {}
        for s in sources:
            per[s] = generate_source(s, prompts, gen_fn=gen_fn_for(s), out_dir=out_dir,
                                     deadline=deadline, clock=clock, chunk=chunk, log=log)
        n_prompts += len(prompts)
        rep = pairing_report(out_dir=out_dir, sources=pair)
        batches.append({"batch": len(batches) + 1, "prompts": len(prompts),
                        "cumulative_prompts": n_prompts,
                        "per_source_rate": {s: per[s]["rate"] for s in sources},
                        "joint_accepted": rep["joint"]["n"], "joint_rate": rep["joint"]["rate"]})
        log(f"batch {len(batches)}: {n_prompts} prompts, {rep['joint']['n']} joint-accepted pairs"
            f" (joint rate {rep['joint']['rate']})")
        if rep["joint"]["n"] >= n_target:  # the target wins over a deadline hit mid-batch
            stop = "pairs"
        elif any(per[s]["stopped"] == "minutes" for s in sources):
            stop = "minutes"
        elif n_prompts >= max_prompts:
            stop = "prompts"
    out = {"stop": stop, "n_prompts": n_prompts, "n_accepted": rep["joint"]["n"],
           "sources": list(sources), "pair_sources": list(pair), "batches": batches,
           "seconds": round(clock() - t0, 2), "minutes_limit": minutes,
           "n_target": n_target, "max_prompts": max_prompts, "report": rep}
    log(f"generation stopped on {stop}: {rep['joint']['n']} pairs from {n_prompts} prompts in "
        f"{out['seconds']} s")
    return out


# --- pairing --------------------------------------------------------------------------------------

def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def pass_sets(out_dir: Path = K.GEN_DIR,
              sources: tuple[str, ...] = K.SOURCES) -> dict[str, set[str]]:
    """source -> the pids whose completion passed the filter (its per-organism mask)."""
    return {s: {pid for pid, r in load_rows(s, out_dir).items() if r["ok"]} for s in sources}


def pairing_report(out_dir: Path = K.GEN_DIR, sources: tuple[str, ...] = K.SOURCES) -> dict:
    """Acceptance per source, each organism's joint rate with `clean`, and the intersection."""
    rows = {s: load_rows(s, out_dir) for s in sources}
    ok = {s: {pid for pid, r in rows[s].items() if r["ok"]} for s in sources}
    all_gen = set.intersection(*(set(rows[s]) for s in sources)) if sources else set()
    joint = set.intersection(*(ok[s] for s in sources)) if sources else set()
    out = {"sources": list(sources), "generated_by_all": len(all_gen),
           "per_source": {}, "organisms": {},
           "joint": {"sources": list(sources), "n": len(joint),
                     "rate": _rate(len(joint), len(all_gen))}}
    for s in sources:
        reasons: Counter[str] = Counter()
        for r in rows[s].values():
            reasons.update(r.get("reject") or [])
        out["per_source"][s] = {"generated": len(rows[s]), "ok": len(ok[s]),
                                "rate": _rate(len(ok[s]), len(rows[s])),
                                "reject_reasons": dict(reasons.most_common())}
    for t in ("teacher_prompted", "teacher_ft"):
        if t in ok and "clean" in ok:
            den = len(set(rows[t]) & set(rows["clean"]))
            n = len(ok[t] & ok["clean"])
            out["organisms"][t] = {"n": n, "rate": _rate(n, den)}
    return out


def accepted(out_dir: Path = K.GEN_DIR, sources: tuple[str, ...] = K.SOURCES) -> list[str]:
    """The shared accepted set: pids every source accepted (sorted, so it is deterministic)."""
    ok = pass_sets(out_dir, sources)
    return sorted(set.intersection(*ok.values())) if ok else []


def organism_accepted(teacher: str, out_dir: Path = K.GEN_DIR) -> list[str]:
    """The fallback mask for one organism: the pids that teacher AND `clean` both accepted."""
    return accepted(out_dir, (teacher, "clean"))


def accepted_or_fallback(teacher: str = "teacher_prompted", out_dir: Path = K.GEN_DIR,
                         sources: tuple[str, ...] = K.SOURCES,
                         min_n: int = K.N_PAIRS_MIN) -> tuple[list[str], str]:
    """The intersection over `sources`, or `teacher`'s own joint set if it is shorter than min_n.

    The intersection keeps the two candidate organisms on one prompt set; the fallback gives up
    that matching rather than the probe, and the caller records which was used.
    """
    shared = accepted(out_dir, sources)
    if len(shared) >= min_n:
        return shared, "intersection"
    own = organism_accepted(teacher, out_dir)
    if len(own) > len(shared):
        return own, f"organism:{teacher}"
    return shared, "intersection"


# --- partitions -----------------------------------------------------------------------------------

def design_for(n: int) -> dict:
    """The reduced design for n accepted pairs (plan.md "Data" / "Reduced designs")."""
    if n < K.N_PAIRS_MIN:
        return {"design": "stop", "parts": (), "stop": True,
                "dropped": ("everything",),
                "reason": f"{n} accepted pairs < {K.N_PAIRS_MIN}: stop and report acceptance"}
    if n < K.N_PAIRS_NO_WIDE:
        return {"design": "no_wide", "parts": ("P_A1", "heldout", "P_C"), "stop": False,
                "dropped": ("P_A2", "P_B", "wide_20k_arms", "proxy_student", "dir_teacher"),
                "reason": f"{n} < {K.N_PAIRS_NO_WIDE}: no 20K arms, no proxy, no dir_teacher"}
    if n < K.N_PAIRS_TARGET:
        return {"design": "no_proxy", "parts": ("P_A1", "heldout", "P_C", "P_A2"), "stop": False,
                "dropped": ("P_B", "proxy_student", "dir_teacher"),
                "reason": f"{n} < {K.N_PAIRS_TARGET}: no proxy, no dir_teacher"}
    return {"design": "full", "parts": ("P_A1", "heldout", "P_C", "P_A2", "P_B"), "stop": False,
            "dropped": (), "reason": f"{n} >= {K.N_PAIRS_TARGET}: full design"}


def partitions(accepted_pids: list[str], *, seed: int = K.MIX_SEED,
               source: str | None = None, pairing: str | None = None) -> dict:
    """Fill P_A1, heldout, P_C, P_A2, P_B in that order from one seeded shuffle.

    The input is sorted before the shuffle, so the partitions depend only on the SET of accepted
    pids and `seed`, never on the order the generation happened to produce them in.
    """
    pids = sorted(set(accepted_pids))
    order = list(pids)
    random.Random(seed).shuffle(order)
    d = design_for(len(pids))
    parts: dict[str, list[str]] = {}
    i = 0
    for name, n in PART_ORDER:
        if name not in d["parts"]:
            continue
        parts[name] = order[i:i + n]
        i += n
        if len(parts[name]) != n:  # design_for guarantees the room; belt and braces
            raise ValueError(f"{name}: {len(parts[name])} of {n} pids")
    return {"design": d["design"], "stop": d["stop"], "reason": d["reason"],
            "dropped": list(d["dropped"]), "n_accepted": len(pids), "n_used": i,
            "n_unused": len(pids) - i, "wide": "P_A2" in parts, "proxy": "P_B" in parts,
            "mix_seed": seed, "source": source, "pairing": pairing,
            "sizes": {k: len(v) for k, v in parts.items()},
            "sha256": {k: K.sha256_bytes(json.dumps(v).encode()) for k, v in parts.items()},
            "accepted_sha256": K.sha256_bytes(json.dumps(pids).encode()), "parts": parts}


def write_partitions(part: dict, path: Path = PARTITIONS_PATH) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(part, indent=1, sort_keys=True) + "\n")
    return K.sha256_file(path)


def load_partitions(path: Path = PARTITIONS_PATH) -> dict:
    return json.loads(Path(path).read_text())


# --- CLI ------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P48 generation bookkeeping (no GPU work)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="acceptance and pairing rates from data/gen/")
    r.add_argument("--sources", default=",".join(K.SOURCES))
    r.add_argument("--gen-dir", default=str(K.GEN_DIR))
    p = sub.add_parser("partitions", help="write data/mix/partitions.json")
    p.add_argument("--teacher", default="teacher_prompted", choices=["teacher_prompted",
                                                                    "teacher_ft"])
    p.add_argument("--sources", default=",".join(K.SOURCES))
    p.add_argument("--gen-dir", default=str(K.GEN_DIR))
    p.add_argument("--out", default=str(PARTITIONS_PATH))
    args = ap.parse_args(argv)
    sources = tuple(s for s in args.sources.split(",") if s)
    gen_dir = Path(args.gen_dir)
    if args.cmd == "report":
        print(json.dumps(pairing_report(gen_dir, sources), indent=1))
        return 0
    pids, how = accepted_or_fallback(args.teacher, gen_dir, sources=sources)
    part = partitions(pids, source=args.teacher, pairing=how)
    sha = write_partitions(part, Path(args.out))
    print(json.dumps({k: v for k, v in part.items() if k != "parts"} | {"file_sha256": sha},
                     indent=1))
    return 1 if part["stop"] else 0


if __name__ == "__main__":
    sys.exit(main())
