"""P44 orchestrator (plan.md): prefixes, chains and scratch chains as Inspect samples, run in
chunks behind the spend guard and recorded in the ledger once each chunk settles. Ported from
P23's run.py (probes/P23-compaction-decisions-vs-cues/run.py).

  uv run --group swarm python probes/P44-handoff-methods-alignment/run.py pilot --model deepseek
  uv run --group swarm python probes/P44-handoff-methods-alignment/run.py prefixes --model deepseek
  uv run --group swarm python probes/P44-handoff-methods-alignment/run.py chains --model deepseek \
      [--arms R,C,...] [--seeds none,P,D,U] [--tasks smoke|all|<ids>] [--runs 4]
  uv run --group swarm python probes/P44-handoff-methods-alignment/run.py status

Existing artifacts without an error are skipped, so a rerun resumes. Every chunk:
`emx.spend.check_total(n x projection)`, one Inspect eval (<= --max-samples sandboxes), then one
ledger row with the billed total (a sample with no billed cost is charged its ceiling-price
spend). A STOP file in results/ ends the run after the current chunk.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

PROBE = Path(__file__).resolve().parent
ROOT = PROBE.parents[1]
for _p in (PROBE, ROOT / "src", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from p44 import contracts as C
from p44 import handoff as H

# Per-sample projections before the smoke measures them (P23: DeepSeek pre-test episodes
# $0.0013-0.01, GLM $0.02-0.045; a chain is up to 24 turns plus handoff calls).
PROJECTION_USD = {"prefix": {"deepseek/deepseek-v4-flash": 0.02, "z-ai/glm-5.3-flash": 0.05},
                  "chain": {"deepseek/deepseek-v4-flash": 0.04, "z-ai/glm-5.3-flash": 0.10}}
STOP_FILE = C.RESULTS_DIR / "STOP"
RERUN_END_REASONS = ("api_error", "replay_mismatch", "summary_failed")
SMOKE_TASKS = ("lcbhard_45", "lcbhard_3", "lcbhard_46")


def tasks_for(which: str) -> list:
    from x3.tasks import load_manifest

    tasks = load_manifest().tasks
    if which == "all":
        return list(tasks)
    ids = SMOKE_TASKS if which == "smoke" else tuple(which.split(","))
    by = {t.task_id: t for t in tasks}
    missing = [i for i in ids if i not in by]
    if missing:
        raise SystemExit(f"unknown tasks {missing}")
    return [by[i] for i in ids]


def task_text(level: str) -> str:
    from x3 import contracts as X3C
    from x3 import texts as X3T

    return X3T.user_prompt(X3C.Context.NEUTRAL, X3C.PromptLevel(level))


def _ok(path: Path) -> bool:
    """True when the artifact (or a .dupN rerun) completed without error."""
    for q in [path, *sorted(path.parent.glob(f"{path.stem}.dup*.json"))]:
        if q.exists():
            a = json.loads(q.read_text())
            if not a.get("error") and a.get("end_reason") not in RERUN_END_REASONS \
                    and not a.get("prefix_failed"):
                return True
    return False


def load_prefix(slug: str, pid: str) -> dict | None:
    p = C.prefix_path(slug, pid)
    return json.loads(p.read_text()) if p.exists() else None


def usable(prefix: dict | None) -> str:
    """"ok", "ended" (no chains: every arm takes the prefix's outcome) or why it is unusable."""
    if prefix is None:
        return "missing"
    if prefix.get("error"):
        return "error"
    if prefix.get("ended"):
        return "ended"
    if prefix.get("prefix_failed"):
        return f"prefix_failed:{prefix['prefix_failed']}"
    for k in ("summary_s1", "summary_s1n"):
        if (prefix.get(k) or {}).get("status") != "ok":
            return f"{k}_failed"
    return "ok"


def base_spec(model: C.Model, *, episode_id: str, stage: str, arm: str, seed: str, pid: str,
              task, run: int, level: str, system_text: str | None = None) -> dict:
    from x3 import texts as X3T

    return dict(episode_id=episode_id, stage=stage, arm=arm, seed=seed,  # noqa: C408
                prefix_id=pid,
                task_id=task.task_id, run=run, model_slug=model.slug, model_id=model.model_id,
                in_ceiling=model.in_ceiling, out_ceiling=model.out_ceiling,
                episode_limit_usd=model.episode_limit_usd,
                task={**asdict(task), "conflict_pair": list(task.conflict_pair)},
                system_text=system_text or X3T.SYSTEM_PROMPT, task_text=task_text(level))


def chain_spec(model: C.Model, prefix: dict, arm_name: str, seed: str, task, run: int,
               level: str) -> dict:
    """The chain sample for one arm and seed, from the prefix artifact (plan "Setup")."""
    arm = C.ARMS[arm_name]
    pid = C.prefix_id(task.task_id, run)
    msgs = prefix["messages"]
    system, task_msg = msgs[0], msgs[1]
    seed_text = C.SEED_TEXT.get(seed)
    sp = base_spec(model, episode_id=C.chain_id(model.slug, arm_name, seed, pid), stage="chain",
                   arm=arm_name, seed=seed, pid=pid, task=task, run=run, level=level)
    sp["task_text"] = H.text_of(task_msg.get("content"))
    sp["system_text"] = H.text_of(system.get("content"))
    w4n = arm.notes_turns
    sp["archive_b64"] = prefix["archive_w4n" if w4n else "archive_w4"]
    sp["expected_sha"] = prefix["sha_w4n" if w4n else "sha_w4"]
    gen1 = {"kind": arm.kind, "handoff_text": None, "seed_text": seed_text}
    if arm.kind == "full":
        if seed_text:
            raise ValueError("R takes no seed")
        sp["init_messages"] = msgs
    elif arm.kind == "summary":
        s = prefix["summary_s1n" if arm.notes_turns else "summary_s1"]["reply"].strip()
        if seed_text:
            s = H.insert_seed_summary(s, seed_text)
        sp["init_messages"] = H.summary_context(system, task_msg, s, arm.pin_task)
        sp["summary_prev"] = s
        gen1["handoff_text"] = s
        if arm.notes_turns and not arm.keep_notes:
            sp["post_write"] = {C.NOTES_PATH: None}
    elif arm.kind == "notes":
        notes = prefix.get("notes_text")
        if seed_text:
            notes = H.insert_seed_notes(notes, seed_text)
            sp["post_write"] = {C.NOTES_PATH: notes}
        sp["init_messages"] = H.notes_context(system, task_msg, notes)
        gen1["handoff_text"] = notes
    elif arm.kind == "lastk":
        ctx, kept = H.lastk_context(system, task_msg, msgs[2:], seed_text)
        sp["init_messages"] = ctx
        gen1["handoff_text"] = H.serialize(kept, None)
    else:
        raise ValueError(f"{arm_name} does not fork from a prefix")
    if seed_text and seed_text not in json.dumps(sp["init_messages"], ensure_ascii=False):
        raise AssertionError(f"seed {seed} not verbatim in the gen-1 context of {pid}")
    sp["gen1"] = gen1
    return sp


def build(a, model: C.Model) -> tuple[list, dict]:
    from p44 import agent

    specs, skipped = [], {}
    runs = range(a.runs)
    if a.cmd == "pilot":
        for t in tasks_for(a.tasks):
            for r in runs:
                pid = C.prefix_id(t.task_id, r)
                if not _ok(C.chain_path(model.slug, "PILOT", "none", pid)):
                    specs.append(agent.Spec(**base_spec(
                        model, episode_id=C.chain_id(model.slug, "PILOT", "none", pid),
                        stage="scratch", arm="PILOT", seed="none", pid=pid, task=t, run=r,
                        level=a.level)))
        return specs, skipped
    if a.cmd == "prefixes":
        for t in tasks_for(a.tasks):
            for r in runs:
                pid = C.prefix_id(t.task_id, r)
                if not _ok(C.prefix_path(model.slug, pid)):
                    specs.append(agent.Spec(**base_spec(
                        model, episode_id=f"prefix/{model.slug}/{pid}", stage="prefix",
                        arm="PREFIX", seed="none", pid=pid, task=t, run=r, level=a.level)))
        return specs, skipped
    arms = a.arms.split(",")
    seeds = a.seeds.split(",")
    bad = [x for x in arms if x not in C.ARMS] + [s for s in seeds if s not in C.SEEDS]
    if bad:
        raise SystemExit(f"unknown arms or seeds {bad}")
    for t in tasks_for(a.tasks):
        for r in runs:
            pid = C.prefix_id(t.task_id, r)
            if "M" in arms and "none" in seeds and \
                    not _ok(C.chain_path(model.slug, "M", "none", pid)):
                from x3 import texts as X3T
                specs.append(agent.Spec(**base_spec(
                    model, episode_id=C.chain_id(model.slug, "M", "none", pid), stage="scratch",
                    arm="M", seed="none", pid=pid, task=t, run=r, level=a.level,
                    system_text=H.memfile_system(X3T.SYSTEM_PROMPT))))
            fork = [x for x in arms if C.ARMS[x].from_prefix]
            if not fork:
                continue
            prefix = load_prefix(model.prefix_slug, pid)
            u = usable(prefix)
            if u != "ok":
                skipped[pid] = u
                continue
            for arm in fork:
                for seed in seeds:
                    if seed != "none" and arm not in C.SEEDED_ARMS:
                        continue
                    if not _ok(C.chain_path(model.slug, arm, seed, pid)):
                        specs.append(agent.Spec(**chain_spec(model, prefix, arm, seed, t, r,
                                                             a.level)))
    return specs, skipped


def settle(arts: list[dict]) -> tuple[float, int]:
    usd, unknown = 0.0, 0
    for x in arts:
        b = x.get("billed_usd")
        if b is None:
            unknown += 1
            b = float(x.get("spent_at_ceiling") or 0.0)
        usd += b
    return usd, unknown


def wait_for_load(max_load: float, poll_s: int = 30) -> None:
    t0 = time.time()
    while os.getloadavg()[0] > max_load:
        if time.time() - t0 < poll_s:
            print(f"load {os.getloadavg()[0]:.0f} > {max_load:.0f}: waiting", flush=True)
        time.sleep(poll_s)


def run(a) -> int:
    from p44 import agent

    from emx import spend

    model = C.MODELS[C.MODEL_ARG[a.model]]
    specs, skipped = build(a, model)
    kind = "prefix" if a.cmd == "prefixes" else "chain"
    # by model id; the provider-test chains measured GLM $0.049-0.069 and the pilot DeepSeek $0.02
    per = a.per_sample_usd or PROJECTION_USD[kind][model.model_id]
    print(json.dumps({"cmd": a.cmd, "model": model.slug, "samples": len(specs),
                      "skipped_prefixes": skipped, "per_sample_projection": per,
                      "projected_usd": round(per * len(specs), 2), "level": a.level,
                      "budget": spend.totals(C.EXP)}, default=str), flush=True)
    if a.dry_run or not specs:
        return 0
    for i in range(0, len(specs), a.chunk):
        if STOP_FILE.exists():
            print(f"STOP file present: {STOP_FILE.read_text().strip()}", flush=True)
            return 3
        chunk = specs[i: i + a.chunk]
        wait_for_load(a.max_load)
        spend.check_total(per * len(chunk), "judge", exp=C.EXP)
        t0 = time.time()
        log_dir = C.LOGS_DIR / model.slug / f"{a.cmd}-{os.getpid()}-{i // a.chunk}"
        logs = agent.run_batch(chunk, model, log_dir, max_samples=a.max_samples)
        arts = [x for p in logs for x in agent.extract(p)]
        usd, unknown = settle(arts)
        spend.record("judge", f"openrouter:{model.model_id}", len(arts), round(usd, 6),
                     note=f"P44 {a.cmd} {model.slug} chunk {i // a.chunk}: {len(arts)} samples"
                     + (f"; {unknown} at ceiling" if unknown else ""), exp=C.EXP)
        errs = sum(1 for x in arts if x.get("error") or x.get("end_reason") in RERUN_END_REASONS)
        print(f"P44_CHUNK cmd={a.cmd} model={model.slug} chunk={i // a.chunk} n={len(arts)} "
              f"usd={usd:.4f} per={usd / max(len(arts), 1):.4f} unknown={unknown} "
              f"errors={errs} wall={time.time() - t0:.0f}s total={spend.totals(C.EXP)}",
              flush=True)
    return 0


def status(_a) -> int:
    from emx import spend

    counts: dict[str, int] = {}
    for root in (C.PREFIXES_DIR, C.CHAINS_DIR):
        for p in root.rglob("*.json"):
            k = "/".join(p.relative_to(C.RESULTS_DIR).parts[:-1])
            counts[k] = counts.get(k, 0) + 1
    print(json.dumps({"artifacts": dict(sorted(counts.items())),
                      "budget": spend.totals(C.EXP)}, indent=1, default=str))
    return 0


def load_key() -> None:
    """OPENROUTER_API_KEY from the worktree's .env (P23's api.load_key); logs key names only."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return
    from dotenv import dotenv_values

    env = ROOT / ".env"
    v = dotenv_values(env).get("OPENROUTER_API_KEY") if env.exists() else None
    if not v:
        raise SystemExit("OPENROUTER_API_KEY not found in the worktree's .env")
    os.environ["OPENROUTER_API_KEY"] = v
    print("loaded keys: OPENROUTER_API_KEY", flush=True)


def _ensure_awake() -> None:
    if os.environ.get("P44_CAFFEINATED") != "1" and shutil.which("caffeinate"):
        os.environ["P44_CAFFEINATED"] = "1"
        os.execvp("caffeinate", ["caffeinate", "-is", sys.executable, *sys.argv])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pilot", "prefixes", "chains"):
        e = sub.add_parser(name)
        e.add_argument("--model", required=True, choices=sorted(C.MODEL_ARG))
        e.add_argument("--tasks", default="smoke")
        e.add_argument("--runs", type=int,
                       default=C.PILOT_RUNS if name == "pilot" else C.N_RUNS)
        e.add_argument("--level", default="B", choices=["A", "B", "D"])
        e.add_argument("--chunk", type=int, default=30)
        e.add_argument("--max-samples", type=int, default=6)
        e.add_argument("--per-sample-usd", type=float, default=None)
        e.add_argument("--max-load", type=float, default=40.0)
        e.add_argument("--dry-run", action="store_true")
        if name == "chains":
            e.add_argument("--arms", default=",".join(C.NATURAL_PAIRED + ("M",)))
            e.add_argument("--seeds", default="none,P,D,U")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        return status(a)
    if not a.dry_run:
        _ensure_awake()
        load_key()
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
