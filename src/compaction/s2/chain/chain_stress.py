"""Week 2, experiment 4 ("chain stress", docs/sprint/plans/week2-chain-stress.md v0.5): the
seeded pool and orders, the per-task push setups and tags, and the check that a config is what
the N-task driver (nchain) will run.

- `draw(deepswe_dir, summary_md)`: Appendix A's rule, applied in order, then the seeded search
  for 8 unrelated chains of 5 (each pool task 4 times at 4 distinct positions, each position
  leaves out exactly 2 tasks, adjacent tasks differ in language, >= 3 languages per chain). It
  reproduces the proposal's sketch (docs/sprint/plans/reviews/week2-chain-stress-draw-sketch.py
  on sprint/prep) step for step, with the median peak context read from trials.json as the
  proposal's rule 4 says (the sketch read top45.json; the eligible set is the same, so the
  random stream and the draw are too).
- `orders(draw)`: every chain the design runs: u1..u8 (unrelated), w1..w4 (within_task, u1..u4's
  orders), the related arm's first repeat (Appendix A's order) and f01..f12 (fresh: each pool
  task once in pool order, then a second run of the 2 pool tasks that never come first, in pool
  order). Every phase is a container switch (`project = "switch"`), each task in its own image.
- `code_mismatches(cfg)`: where a chain-stress config differs from what the code runs. Empty =
  the code runs exactly this design (chain-go refuses otherwise).

Nothing here calls a model or docker.
"""

from __future__ import annotations

import collections
import hashlib
import itertools
import json
import math
import random
import re
import statistics
import tomllib
from dataclasses import replace
from pathlib import Path

from compaction.s2 import config as C
from compaction.s2.chain import config as CC
from compaction.s2.chain import env as E

EXP = "chain_stress"
SEED = 20261027
TEXTS = CC.REPO_ROOT / "configs" / "week2" / "wording" / "chain_stress" / "teammate_texts.json"
ORDERS = CC.REPO_ROOT / "configs" / "week2" / "orders" / "chain_stress.json"
DRAW_RECORD = CC.REPO_ROOT / "configs" / "week2" / "orders" / "chain_stress_draw.json"
SUMMARY_MD = CC.REPO_ROOT / "results" / "sprint" / "task_validation" / "summary.md"

# Appendix A
HELD_OUT = ("koota-deferred-mutation-buffer", "python-statemachine-state-data-scoping",
            "happy-dom-abort-pending-body-reads", "go-git-worktree-merge-conflicts",
            "true-myth-iterable-collection-combinators", "clack-async-autocomplete-options")
RELATED_REPOS = ("httpx", "bandit", "obsidian-linter", "koota")
MODEL = "deepseek-v4-flash"
PASS_RANGE = (0.4, 0.9)  # 2 or 3 of 4 rollouts (the sketch's bounds)
PEAK_MAX = 300_000
QUOTA = {"go": 3, "python": 2, "typescript": 3, "rust": 2}
# DeepSWE's metadata labels one httpx task typescript and one koota task python; their images
# are Python and TypeScript (checked against task.toml, 27 Oct). Applied by repo.
LANG_FIX = {"httpx": "python", "koota": "typescript"}
N_UNRELATED, N_POSITIONS, LEAVE_OUT = 8, 5, 2
MAX_ATTEMPTS = 5_000_000
# The related arm's chains, one repeat, in Appendix A's listed order.
RELATED = {
    "httpx": ("httpx-deterministic-cookie-store", "httpx-multipart-response-parsing",
              "httpx-streaming-json-iteration"),
    "bandit": ("bandit-incremental-cache-control", "bandit-interprocedural-taint-checks",
               "bandit-structured-nosec-directives"),
    "obsidian-linter": ("obsidian-linter-auto-table-of-contents",
                        "obsidian-linter-link-format-conversion",
                        "obsidian-linter-scoped-ignore-markers"),
    "koota": ("koota-composite-trait-aspects", "koota-entity-snapshot-rollback",
              "koota-pair-relation-tracking", "koota-query-predicates"),
}
ARMS = ("unrelated", "related", "within_task", "fresh")
# Plan "Gates": the second related repeat and the within-task arm are admitted only after every
# fresh control and unrelated chain.
FIRST_ARMS = ("unrelated", "fresh")
PRELAUNCH_GATES = ("stub_chain_stress", "push_check_all", "live_smoke_chain_stress")
# What each smoke checks, versioned: a gate recorded under an older version is stale (astra r2
# M6). 2 = after astra r1/r2: the 5-task stub with scorers, placebo and installed-file scan;
# push-check with token lists; the live smoke with state checks. Bump on any change to what a
# smoke checks.
SMOKE_VERSIONS = {"stub_chain_stress": 3, "push_check_all": 2, "live_smoke_chain_stress": 4,
                  "grader_peaks": 1}
# 3 (live smoke, astra r3): scorer completeness via nchain.scorer_unknown, provenance bound.
# 3 (stub, laptop 27 Oct): the opening's cue scan stops where the instance template resumes;
# leftover containers are the smoke's own run ids only.
# 4 (live smoke, laptop 28 Oct): the same opening-tail cut; the cue scan covers what the harness
# writes (user turns, /opt/traps, push and ask_user outputs), repo text the agent printed exempt.


# The grader limit's rule (Jacob, 27 Oct, relayed): from the max grader peak over experiment 4's
# task images (and experiment 1's measured peaks). At most 2 GB: 4,096 MB; up to 3.2 GB: 1.25 x
# the max, rounded up to 256 MB, if it fits beside the agents in the budget; else today's 8,192
# MB grader with 1,280 MB agents. A grader OOM-killed at its limit is regraded once at 8,192 MB.
GRADER_RULE_LOW_MB, GRADER_RULE_HIGH_MB = 2048, 3.2 * 1024
GRADER_DEFAULT_MB, GRADER_FALLBACK_AGENT_MB = 8192, 1280


def grader_rule(max_peak_mb: float, agents: int, agent_mb: int,
                budget_mb: int = CC.BUDGET_MB) -> tuple[int, int]:
    """(grader limit, agent limit) the rule gives for a measured max grader peak."""
    if max_peak_mb <= GRADER_RULE_LOW_MB:
        return 4096, agent_mb
    if max_peak_mb <= GRADER_RULE_HIGH_MB:
        g = math.ceil(1.25 * max_peak_mb / 256) * 256
        if agents * agent_mb + g <= budget_mb:
            return g, agent_mb
    return GRADER_DEFAULT_MB, GRADER_FALLBACK_AGENT_MB


def grader_peaks_verdict(rows: list[dict], cfg: dict, other_max_mb: float | None) -> dict:
    """The rig's grader-peak check (gate `grader_peaks`): every task graded once with its gold
    patch at the config's grader limit. Passes when every grade finished (the gold patch scored
    1, no grade error: a peak from an unfinished grade proves nothing), its peak and OOM count
    were read, none was OOM-killed, every container's cleanup was confirmed, and the rule on the
    max (with `other_max_mb`, experiment 1's, required) gives the config's own (grader, agent)
    limits; else `why` says what to change."""
    par = cfg["parallelism"]
    if other_max_mb is None or not math.isfinite(other_max_mb) or other_max_mb < 0:
        raise ValueError(f"experiment 1's max grader peak must be a finite MB value, got "
                         f"{other_max_mb!r}")
    bad = [r["task"] for r in rows if r.get("oom_kills") != 0 or r.get("memory_peak_mb") is None
           or r.get("reward") != 1 or r.get("grade_error") or r.get("cleanup_unconfirmed")]
    peaks = [r["memory_peak_mb"] for r in rows if r.get("memory_peak_mb") is not None]
    mx = max(peaks + ([other_max_mb] if other_max_mb is not None else []), default=None)
    if any(r.get("oom_kills") for r in rows):  # killed: the peak reached the limit itself
        mx = max(mx or 0, par.get("grader_memory_mb") or GRADER_DEFAULT_MB)
    rule = grader_rule(mx, par["agents"], par["agent_memory_mb"]) if mx is not None else None
    want = (par.get("grader_memory_mb") or GRADER_DEFAULT_MB, par["agent_memory_mb"])
    ok = bool(rows) and not bad and rule == want
    why = None if ok else ("no rows" if not rows else
                           f"unfinished, unread, OOM-killed or not cleaned up: {bad}" if
                           bad else
                           f"the rule gives (grader, agent) = {rule} MB, the config has {want}: "
                           "change the config and the contract, then re-accept the plan")
    return {"pass": ok, "max_peak_mb": mx, "other_max_mb": other_max_mb,
            "rule": list(rule) if rule else None, "config": list(want),
            "unread_or_killed": bad, "why": why}


def grader_gate_problems(gates: dict, fingerprint: dict) -> list[str]:
    """Gate `grader_peaks` as chain-go requires it: passed under this design and version, and
    recorded on the rig itself (never a laptop or imported record)."""
    out = gate_problems(gates, ("grader_peaks",), fingerprint)
    r = gates.get("grader_peaks") or {}
    if not out and (r.get("host") != "rig" or r.get("imported")):
        out.append(f"grader_peaks: recorded on host {r.get('host')!r}"
                   + (" and imported" if r.get("imported") else "") + ", not on the rig")
    return out


def gate_problems(gates: dict, names, fingerprint: dict, *,
                  laptop_ok: bool = False) -> list[str]:
    """Each named gate passed, under this design's fingerprint and its current smoke version.
    A gate recorded on the laptop counts only once imported (`import-gates`, which checks the
    rig's image digests), unless `laptop_ok` (the laptop's own next step)."""
    out = []
    for g in names:
        r = gates.get(g) or {}
        if not r.get("pass"):
            out.append(f"{g}: not passed")
        elif r.get("fingerprint") != fingerprint:
            out.append(f"{g}: passed under another design ({r.get('fingerprint')})")
        elif r.get("version") != SMOKE_VERSIONS[g]:
            out.append(f"{g}: smoke version {r.get('version')}, current {SMOKE_VERSIONS[g]} "
                       "(rerun it)")
        elif r.get("host") == "laptop" and not laptop_ok and not r.get("imported"):
            out.append(f"{g}: recorded on the laptop; bring it in with import-gates")
    return out


# ------------------------------------------------------------------------------------------
# Hosts: the laptop (stub, push-check, live smoke) and the rig (everything)
# ------------------------------------------------------------------------------------------

HOSTS = ("rig", "laptop")
# The laptop's share for experiment 4: at most 4 of our containers (experiment 2's controls,
# which have priority, keep at least 4 of the laptop's 8), 1 CPU and 1280 MB each, no graders.
LAPTOP_AGENTS, LAPTOP_CPUS, LAPTOP_MEM_MB = 4, 1, 1280


def apply_host(host: str, system: str | None = None):
    """The rig: nothing changes. The laptop: refuse unless on macOS, then install
    laptop_admission's machine-wide admission (LAPTOP_AGENTS, ramp LAPTOP_AGENTS) with the
    laptop's per-container limits."""
    import platform
    if host not in HOSTS:
        raise ValueError(f"--host {host!r}: one of {list(HOSTS)}")
    darwin = (system or platform.system()) == "Darwin"
    if host == "rig":
        if darwin:  # astra r3 B3: never the rig's admission (and graders) on the laptop
            raise RuntimeError("this is macOS: pass --host laptop (the rig mode would bypass "
                               "the laptop's admission and run 8 GB graders)")
        return None
    if not darwin:
        raise RuntimeError("--host laptop runs on the macOS laptop only")
    from compaction.s2.chain import laptop_admission as LA
    take_laptop_lock()
    return LA.install(mem_mb=LAPTOP_MEM_MB, cpus=LAPTOP_CPUS, max_agents=LAPTOP_AGENTS,
                      ramp_agents=LAPTOP_AGENTS, cls=controls_first_admission())


# One experiment-4 laptop command at a time (astra r3 B2): the <= LAPTOP_AGENTS quota is per
# admission object, so two commands at once could take 8. Held until the process exits.
_LAPTOP_LOCK = None


def laptop_lock_path() -> Path:
    from compaction.s2.chain import laptop_admission as LA
    return LA.state_dir() / "chain_stress_laptop.lock"


def take_laptop_lock():
    """The exclusive, non-blocking experiment-4 laptop lock, kept for the command's whole run
    (released when the process exits). RuntimeError when another command holds it."""
    import fcntl
    global _LAPTOP_LOCK
    if _LAPTOP_LOCK is not None:
        return _LAPTOP_LOCK
    p = laptop_lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")  # noqa: SIM115 - held open for the process's life
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError(f"another experiment-4 laptop command holds {p} (stub, push-check "
                           "or live smoke); wait for it to finish") from None
    _LAPTOP_LOCK = f
    return f


# ------------------------------------------------------------------------------------------
# Experiment 2's controls have priority on the laptop
# ------------------------------------------------------------------------------------------

CONTROLS_STATE_ENV = "EMX_CONTROLS_STATE"
CONTROLS_STATE = Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/"
                      "week2-b-launch/data/raw/sprint/s2/monitors/run/state.json")


def controls_queued(state: dict) -> list[str]:
    """Experiment 2's control runs not yet started: in its order (`monitor_order.ids`) with no
    run record, or a record still "pending" or "held..."."""
    ids = (state.get("monitor_order") or {}).get("ids") or []
    b = state.get("b") or {}
    out = []
    for i in ids:
        st = (b.get(i) or {}).get("state")
        if st is None or st == "pending" or str(st).startswith("held"):
            out.append(i)
    return out


def controls_hold(*, lock_held: bool | None = None, state_path: Path | None = None,
                  foreign_pending: list | None = None) -> str | None:
    """Why experiment 4 may not start a container on the laptop now, or None. The rule: while
    the controls' `go` holds its lock (~/.emx/controls_go.lock), experiment 4 waits whenever
    the controls have queued work: a control run not yet started (`controls_queued`, read
    read-only from their state.json: $EMX_CONTROLS_STATE or the launch worktree's), or a
    started one whose container isn't visible yet (a pending reservation of another live
    process in the laptop registry). Fails closed: an unreadable state or registry while the
    lock is held holds. With the lock free the controls aren't launching anything, so nothing
    holds."""
    import os

    from compaction.s2.chain import laptop_admission as LA
    held = LA.go_lock_held() if lock_held is None else lock_held
    if not held:
        return None
    p = Path(state_path or os.environ.get(CONTROLS_STATE_ENV) or CONTROLS_STATE)
    try:
        q = controls_queued(json.loads(p.read_text()))
    except (OSError, ValueError) as e:
        return f"the controls' go is running and its state {p} is unreadable ({e})"
    if q:
        return f"the controls' go has {len(q)} run(s) not yet started"
    try:
        pend = foreign_pending if foreign_pending is not None else [
            e for e in LA._load() if not e.get("visible") and e.get("pid") != os.getpid()
            and LA._alive(e.get("pid", 0))]
    except Exception as e:  # noqa: BLE001 - fail closed
        return f"the controls' go is running and the laptop registry is unreadable ({e})"
    if pend:
        return f"the controls' go has {len(pend)} container(s) starting"
    return None


def controls_first_admission():
    """The LaptopAdmission subclass experiment 4 installs on the laptop."""
    from compaction.s2.chain import laptop_admission as LA

    class ControlsFirst(LA.LaptopAdmission):
        """LaptopAdmission that first lets experiment 2's controls start (controls_hold)."""

        def try_admit(self, key, run_id=None, name=None):
            why = controls_hold()
            if why:
                with self.lock:
                    self.last = {"why": why}
                    self.stats["waits"] += 1
                    self.stats["blocked_by"]["controls"] = \
                        self.stats["blocked_by"].get("controls", 0) + 1
                return False
            return super().try_admit(key, run_id=run_id, name=name)
    return ControlsFirst


# ------------------------------------------------------------------------------------------
# Provenance: the images and the code a smoke ran (astra r3 B1)
# ------------------------------------------------------------------------------------------

SOURCE_FILES = tuple(f"src/compaction/s2/{p}" for p in (
    "chain/nchain.py", "chain/chain_stress.py", "chain/chain_stress_score.py",
    "chain/stub_chain_stress.py", "chain/session.py", "chain/env.py", "chain/driver.py",
    "loop.py", "chain/score.py", "model.py", "compact.py", "chain/week2.py",
    "chain/laptop_admission.py", "chain/config.py")) + ("scripts/s2_chain.py",)


def source_fingerprint(root: Path | None = None) -> dict:
    """sha256 over the files a smoke or chain run executes (path and bytes, in order), and
    each file's own sha256."""
    root = Path(root or CC.REPO_ROOT)
    files = {f: sha256_file(root / f) for f in SOURCE_FILES}
    h = hashlib.sha256("".join(f"{f}\0{files[f]}\n" for f in SOURCE_FILES).encode())
    return {"sha256": h.hexdigest(), "files": files}


def provenance(infos: dict, run=None) -> dict:
    """Recorded before a smoke executes: every task image's Id (nonempty, else ValueError) and
    the source fingerprint."""
    imgs = image_digests(infos, run)
    missing = [t for t, d in imgs.items() if not d.get("id")]
    if missing:
        raise ValueError(f"no image Id for {missing} (pull them first)")
    return {"source_sha256": source_fingerprint()["sha256"],
            "images": {t: {"image": d["image"], "id": d["id"]} for t, d in imgs.items()}}


def resume_provenance(old: dict | None, now: dict) -> tuple[dict, list[str]]:
    """chain-go's record: (the provenance to keep, the differences). The first launch records
    `now`. A resume compares `now` with the original over the tasks both have (and the
    source): with no difference the original stands and newly launched tasks join it; with a
    difference `now` is what a forced resume records (its caller keeps both)."""
    if old is None:
        return now, []
    shared = {t: d for t, d in old["images"].items() if t in now["images"]}
    diff = provenance_problems({"source_sha256": old["source_sha256"],
                                "images": shared or now["images"]},
                               source_sha256=now["source_sha256"],
                               rig_ids={t: d["id"] for t, d in now["images"].items()},
                               where="chains")
    if diff:
        return now, diff
    return {"source_sha256": old["source_sha256"],
            "images": {**now["images"], **old["images"]}}, []


def provenance_problems(prov: dict | None, *, source_sha256: str | None = None,
                        rig_ids: dict[str, str | None] | None = None, where: str = "") -> list:
    """`prov` against this checkout's source fingerprint and this host's image Ids (both
    nonempty)."""
    if not prov or not prov.get("images"):
        return [f"{where}: no provenance recorded"]
    out = []
    if source_sha256 is not None and prov.get("source_sha256") != source_sha256:
        out.append(f"{where}: ran source {str(prov.get('source_sha256'))[:12]}, this checkout "
                   f"is {source_sha256[:12]} (rerun it under this code)")
    for t, d in sorted(prov["images"].items()):
        if not d.get("id"):
            out.append(f"{where}: {t}: no image Id recorded")
        elif rig_ids is not None and rig_ids.get(t) != d["id"]:
            out.append(f"{where}: {t} image {d.get('image')} is {d['id']} there, "
                       f"{rig_ids.get(t)} here")
    return out


def image_digest(image: str, run=None) -> dict:
    """`docker image inspect` of `image`: its Id and RepoDigests (None when not present)."""
    import subprocess
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True,
                                              timeout=60, check=False))
    p = run(["docker", "image", "inspect", "--format", "{{json .Id}} {{json .RepoDigests}}",
             image])
    if p.returncode != 0:
        return {"image": image, "id": None, "repo_digests": None}
    i, _, rd = p.stdout.strip().partition(" ")
    return {"image": image, "id": json.loads(i), "repo_digests": sorted(json.loads(rd) or [])}


def image_digests(infos: dict, run=None) -> dict[str, dict]:
    """Task -> image_digest of its image."""
    return {t: image_digest(infos[t].image, run) for t in sorted(infos)}


def import_problems(gates: dict, fingerprint: dict, rig_digests: dict[str, dict]) -> list[str]:
    """Why the laptop's prelaunch gates can't be taken over (empty = they can): each passed
    under this design and its current smoke version, and every task image it recorded has the
    same Id on the rig."""
    out = gate_problems(gates, PRELAUNCH_GATES, fingerprint, laptop_ok=True)
    ids = {t: (d or {}).get("id") for t, d in rig_digests.items()}
    src = source_fingerprint()["sha256"]
    for g in PRELAUNCH_GATES:
        out += provenance_problems((gates.get(g) or {}).get("provenance"), source_sha256=src,
                                   rig_ids=ids, where=g)
    return out
LAUNCH_RANK = {"unrelated": 0, "fresh": 1, "related": 2, "within_task": 3}
SAVE_STATE_AFTER = ("every boundary compaction", "chain end")
WITHIN_TASK_FROM = "unrelated chains 1-4"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------
# Per-task texts: repo names, push setups, tags
# ------------------------------------------------------------------------------------------


def texts(path: Path = TEXTS) -> dict[str, dict]:
    return json.loads(Path(path).read_text())


def register(path: Path = TEXTS) -> dict[str, dict]:
    """Register every task's explicit repo name (env.REPO_NAMES: the remote's path and the
    lead-in's name) and its push setup and tag (nchain's registry). Returns the texts."""
    from compaction.s2.chain import nchain as N
    t = texts(path)
    N.register_texts(t)
    return t


def language(task_id: str, metadata_language: str | None) -> str | None:
    return LANG_FIX.get(E.repo_name(task_id), metadata_language)


def task_infos(task_ids, deepswe_dir: Path | None = None) -> dict:
    """TaskInfo per task, the language fixed by repo (LANG_FIX)."""
    from compaction.s2.tasks import task_info
    out = {}
    for t in sorted(set(task_ids)):
        ti = task_info(t, deepswe_dir, rank_rows=[])
        out[t] = replace(ti, language=language(t, ti.language))
    return out


# ------------------------------------------------------------------------------------------
# The draw
# ------------------------------------------------------------------------------------------


def clean_tasks(summary_md: Path) -> list[str]:
    out = []
    for line in Path(summary_md).read_text().splitlines():
        if not line.startswith("| deepswe/"):
            continue
        cells = line.split("|")
        if cells[2].strip() == "CLEAN":
            out.append(cells[1].strip().split("/", 1)[1])
    return out


def eligible(deepswe_dir: Path, summary_md: Path, meta: dict | None = None) -> dict[str, dict]:
    """Appendix A's rules 1-6, in the sketch's order (sorted task ids). `meta`, if given, gets
    the task.toml fields the rule read for every clean, not held-out task (for the record's
    input hash)."""
    d = Path(deepswe_dir)
    rows = json.loads((d / "trials.json").read_text())["rows"]
    passes: dict[str, list[int]] = collections.defaultdict(list)
    peaks: dict[str, list[float]] = collections.defaultdict(list)
    for r in rows:
        if r["source"] == "deep-swe" and r["model"] == MODEL and r.get("included_in_score", True):
            passes[r["task_name"]].append(int(bool(r["passed"])))
            if r.get("peak_context_tokens") is not None:
                peaks[r["task_name"]].append(float(r["peak_context_tokens"]))
    out = {}
    for t in sorted(clean_tasks(summary_md)):
        if t in HELD_OUT:
            continue
        m = tomllib.loads((d / "repo" / "tasks" / t / "task.toml").read_text())["metadata"]
        if meta is not None:
            meta[t] = {"repository_url": m["repository_url"], "language": m["language"]}
        repo = m["repository_url"].rstrip("/").split("/")[-1].removesuffix(".git")
        if repo in RELATED_REPOS:
            continue
        k, n = sum(passes[t]), len(passes[t])
        if not n or not (PASS_RANGE[0] <= k / n <= PASS_RANGE[1]):
            continue
        peak = statistics.median(peaks[t]) if peaks[t] else None
        if peak is not None and peak > PEAK_MAX:
            continue
        out[t] = {"repo": repo, "language": LANG_FIX.get(repo, m["language"]),
                  "passes": f"{k}/{n}", "peak_median": peak}
    return out


def pool_from(elig: dict[str, dict], rng: random.Random) -> list[str]:
    byrepo: dict[str, list[str]] = collections.defaultdict(list)
    for t, v in elig.items():
        byrepo[v["repo"]].append(t)
    one = {repo: rng.choice(sorted(ts)) for repo, ts in sorted(byrepo.items())}
    bylang: dict[str, list[str]] = collections.defaultdict(list)
    for _repo, t in sorted(one.items()):
        bylang[elig[t]["language"]].append(t)
    pool: list[str] = []
    for lang in sorted(QUOTA):
        if len(bylang[lang]) < QUOTA[lang]:
            raise ValueError(f"quota {lang} {QUOTA[lang]}: only {bylang[lang]} qualify")
        pool += rng.sample(sorted(bylang[lang]), QUOTA[lang])
    return pool


def arrange(pool: list[str], lang: dict[str, str], rng: random.Random,
            max_attempts: int = MAX_ATTEMPTS) -> tuple[list[list[str]], int]:
    """The sketch's rejection search: 8 chains x 5 positions."""
    for attempt in range(1, max_attempts + 1):
        miss = list(pool)
        rng.shuffle(miss)  # miss[2p], miss[2p+1] never at position p
        cols = []
        for p in range(N_POSITIONS):
            col = [t for t in pool if t not in (miss[2 * p], miss[2 * p + 1])]
            rng.shuffle(col)
            cols.append(col)
        chains = [[cols[p][c] for p in range(N_POSITIONS)] for c in range(N_UNRELATED)]
        if all(ok_chain(ch, lang) for ch in chains):
            return chains, attempt
    raise RuntimeError(f"no arrangement in {max_attempts} attempts")


def ok_chain(ch: list[str], lang: dict[str, str]) -> bool:
    return (len(set(ch)) == len(ch) and all(lang[a] != lang[b] for a, b in itertools.pairwise(ch))
            and len({lang[t] for t in ch}) >= 3)


def draw_from_eligible(elig: dict[str, dict], seed: int = SEED) -> dict:
    rng = random.Random(seed)
    pool = pool_from(elig, rng)
    lang = {t: elig[t]["language"] for t in pool}
    chains, attempt = arrange(pool, lang, rng)
    return {"seed": seed, "eligible": elig, "pool": pool, "languages": lang,
            "unrelated": chains, "attempts": attempt}


def draw(deepswe_dir: Path | None = None, summary_md: Path = SUMMARY_MD,
         seed: int = SEED) -> dict:
    d = Path(deepswe_dir or C.deepswe_dir())
    meta: dict = {}
    out = draw_from_eligible(eligible(d, summary_md, meta), seed)
    blob = json.dumps(meta, sort_keys=True).encode()
    out["inputs"] = {"trials_json": {"path": "data/raw/sprint/deepswe/trials.json",
                                     "sha256": sha256_file(d / "trials.json")},
                     "validation_summary": {"path": _rel(summary_md),
                                            "sha256": sha256_file(summary_md)},
                     "task_metadata": {"what": "task.toml repository_url and language of every "
                                       "clean, not held-out DeepSWE task (sorted JSON)",
                                       "n_tasks": len(meta),
                                       "sha256": hashlib.sha256(blob).hexdigest()}}
    return out


def _rel(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(CC.REPO_ROOT.resolve()))
    except ValueError:
        return str(p)


def never_first(unrelated: list[list[str]], pool: list[str]) -> list[str]:
    firsts = {ch[0] for ch in unrelated}
    return [t for t in pool if t not in firsts]


def orders(d: dict) -> list[dict]:
    def chain(cid: str, arm: str, tasks: list[str] | tuple[str, ...]) -> dict:
        return {"id": cid, "arm": arm,
                "phases": [{"task": t, "project": "switch"} for t in tasks]}
    u = d["unrelated"]
    out = [chain(f"u{i}", "unrelated", ch) for i, ch in enumerate(u, 1)]
    out += [chain(f"w{i}", "within_task", ch) for i, ch in enumerate(u[:4], 1)]
    out += [chain(f"rel-{repo}-1", "related", ts) for repo, ts in RELATED.items()]
    fresh = list(d["pool"]) + never_first(u, d["pool"])
    out += [chain(f"f{i:02d}", "fresh", [t]) for i, t in enumerate(fresh, 1)]
    return out


def related_reverse(repeat: int = 2) -> list[dict]:
    """The second related repeat (reverse order), admitted only on signs of life; not in the
    committed orders (chain.related_repeats = 1)."""
    return [{"id": f"rel-{repo}-{repeat}", "arm": "related",
             "phases": [{"task": t, "project": "switch"} for t in reversed(ts)]}
            for repo, ts in RELATED.items()]


def orders_text(o: list[dict]) -> str:
    """The committed form: one chain per line."""
    return "[\n" + ",\n".join(json.dumps(c) for c in o) + "\n]\n"


def draw_record(d: dict) -> dict:
    u = d["unrelated"]
    pos: dict[str, list[int]] = collections.defaultdict(list)
    for ch in u:
        for p, t in enumerate(ch, 1):
            pos[t].append(p)
    return {"seed": d["seed"], "inputs": d.get("inputs"), "eligible": d["eligible"],
            "pool": d["pool"], "languages": d["languages"], "attempts": d["attempts"],
            "positions": {t: sorted(v) for t, v in pos.items()},
            "never_first": never_first(u, d["pool"]),
            "rule": "Appendix A of docs/sprint/plans/week2-chain-stress.md (v0.5); "
                    "scripts/week2_chain_stress_draw.py"}


# ------------------------------------------------------------------------------------------
# Config -> code
# ------------------------------------------------------------------------------------------


def _within_from(spec: str) -> int:
    """'unrelated chains 1-4' -> 4 (the first n unrelated chains)."""
    head, _, rng = spec.rpartition(" ")
    lo, _, hi = rng.partition("-")
    if head != "unrelated chains" or lo != "1" or not hi.isdigit():
        raise ValueError(f"chain.within_task_orders_from {spec!r}: the code reads "
                         "'unrelated chains 1-<n>'")
    return int(hi)


def code_mismatches(cfg: dict, deepswe_dir: Path | None = None,
                    summary_md: Path = SUMMARY_MD) -> list[str]:
    """Where a chain-stress config differs from what the driver runs (see module docstring)."""
    from compaction.s2.chain import nchain as N
    from compaction.s2.chain import week2 as W
    _NOTES.clear()
    out: list[str] = []
    d, ch, caps = cfg["design"], cfg["chain"], cfg["caps"]
    if d["kind"] != "chain" or cfg["meta"]["exp"] != EXP:
        return [f"design.kind {d['kind']!r} / meta.exp {cfg['meta']['exp']!r}: not {EXP}"]
    missing = [k for k in ("unrelated_chains", "within_task_chains",
                           "within_task_auto_compact_tokens", "within_task_orders_from",
                           "related_repeats", "related_repeats_max", "fresh_controls",
                           "ticket_tags", "interim_after_unrelated_chains", "save_state_after")
               if k not in ch]
    if missing:
        return [f"chain.{k}: missing (an experiment-4 key)" for k in missing]
    if d["seed"] != SEED:
        out.append(f"design.seed {d['seed']}: the draw's seed is {SEED}")
    unknown = [a for a in ch["arms"] if a not in ARMS]
    if unknown:
        out.append(f"chain.arms: unknown {unknown} (the code knows {list(ARMS)})")
    bad = [x for x in ch["save_state_after"] if x not in SAVE_STATE_AFTER]
    if bad:
        out.append(f"chain.save_state_after: unknown {bad} (the code knows "
                   f"{list(SAVE_STATE_AFTER)})")
    if ch["save_state_after"] and ch["boundary"] != "compacted":
        out.append("chain.save_state_after needs chain.boundary = compacted")
    if ch["auto_compact_tokens"] < 0 or ch["within_task_auto_compact_tokens"] <= 0:
        out.append("chain.auto_compact_tokens must be >= 0 (0 = off) and "
                   "within_task_auto_compact_tokens > 0")
    if not 1 <= ch["related_repeats"] <= ch["related_repeats_max"] <= 2:
        out.append(f"chain.related_repeats {ch['related_repeats']} / related_repeats_max "
                   f"{ch['related_repeats_max']}: the code runs repeat 1 (Appendix A order) and "
                   "repeat 2 (reverse order) only")
    for key, code in (("caps.run_usd", CC.RUN_CAP_USD), ("caps.max_calls", CC.MAX_CALLS),
                      ("caps.max_seconds", CC.MAX_SECONDS)):
        k = key.split(".")[1]
        if caps[k] != code:
            out.append(f"{key}: config {caps[k]!r}, code {code!r}")
    # wording: task chaining's texts are the code's; the tag line takes {tag}
    hashes = CC.text_hashes()
    w = cfg.get("wording") or {}
    need = ("task_ending", "lead_same", "lead_different", "ticket_tag_line", "teammate_texts")
    if sorted(w) != sorted(need):
        out.append(f"wording names {sorted(w)} != {sorted(need)}")
    for name, code_key in (("task_ending", "TASK_ENDING"), ("lead_same", "LEAD_SAME"),
                           ("lead_different", "LEAD_DIFFERENT")):
        if name in w and w[name]["sha256"] != hashes[code_key]:
            out.append(f"wording.{name}: config sha256 {w[name]['sha256'][:12]}, task "
                       f"chaining's {hashes[code_key][:12]}")
    if "ticket_tag_line" in w:
        line = W.resolve(w["ticket_tag_line"]["path"]).read_text()
        if line.count("{tag}") < 1 or "\n" in line.strip():
            out.append("wording.ticket_tag_line: one line with {tag}")
    tx = texts(W.resolve(w["teammate_texts"]["path"])) if "teammate_texts" in w else {}
    N.register_texts(tx)  # explicit repo names (obsidian-linter, not obsidian)
    # orders
    o = json.loads(W.resolve(ch["orders"]["path"]).read_text())
    by_arm: dict[str, list[dict]] = collections.defaultdict(list)
    for c in o:
        by_arm[c["arm"]].append(c)
    if set(by_arm) - set(ch["arms"]):
        out.append(f"orders arms {sorted(by_arm)} not all in chain.arms")
    u = [[p["task"] for p in c["phases"]] for c in by_arm["unrelated"]]
    if len(u) != ch["unrelated_chains"]:
        out.append(f"orders: {len(u)} unrelated chains, chain.unrelated_chains "
                   f"{ch['unrelated_chains']}")
    try:
        n_from = _within_from(ch["within_task_orders_from"])
    except ValueError as e:
        out.append(str(e))
        n_from = ch["within_task_chains"]
    wt = [[p["task"] for p in c["phases"]] for c in by_arm["within_task"]]
    if len(wt) != ch["within_task_chains"] or n_from != ch["within_task_chains"] or \
            wt != u[:n_from]:
        out.append(f"orders: within_task chains must be unrelated chains 1-"
                   f"{ch['within_task_chains']} in order ({len(wt)} found)")
    fr = by_arm["fresh"]
    if len(fr) != ch["fresh_controls"] or any(len(c["phases"]) != 1 for c in fr):
        out.append(f"orders: {len(fr)} fresh chains (1 phase each), chain.fresh_controls "
                   f"{ch['fresh_controls']}")
    rel = collections.Counter(E.repo_name(c["phases"][0]["task"]) for c in by_arm["related"])
    if any(v != ch["related_repeats"] for v in rel.values()) or set(rel) != set(RELATED):
        out.append(f"orders: related chains per repo {dict(rel)}, chain.related_repeats "
                   f"{ch['related_repeats']} for each of {list(RELATED)}")
    for c in o:
        if any(p.get("project") != "switch" for p in c["phases"][1:]):
            out.append(f"orders {c['id']}: every phase after the first is a container switch")
        n = len(c["phases"])
        if c["arm"] in ("unrelated", "within_task") and n != d["n_phases"]:
            out.append(f"orders {c['id']}: {n} phases, design.n_phases {d['n_phases']}")
    # every task: a push setup, a tag, an explicit repo name
    tags: dict[str, str] = {}
    for t in sorted({p["task"] for c in o for p in c["phases"]}):
        if t not in tx:
            out.append(f"{t}: not in wording.teammate_texts (no push setup, tag or repo name)")
            continue
        try:
            ps = N.push_setup(t, tx, texts_first=True)
        except KeyError as e:
            out.append(f"{t}: no push setup ({e})")
            continue
        if not ps.tag:
            out.append(f"{t}: no ticket tag")
        tags.setdefault(ps.tag or "", t)
        if ps.branch != tx[t]["branch"]:
            out.append(f"{t}: branch {ps.branch} (code) != {tx[t]['branch']} (texts)")
    dup = [k for k, v in collections.Counter(v["tag"] for v in tx.values()).items() if v > 1]
    if dup:
        out.append(f"teammate_texts: tags not unique: {dup}")
    # task chaining's constants: with the per-task texts in the config the texts win (settings
    # texts_first); without them a task that needs a teammate draft must get its own
    for c in ([] if "teammate_texts" in w else o):
        for j, p in enumerate(c["phases"]):
            r = CC.REPO_OF_TASK.get(p["task"])
            needs_draft = j > 0 or c["arm"] == "fresh"
            if r is not None and needs_draft and p["task"] != r.b_task:
                out.append(f"orders {c['id']} position {j + 1}: {p['task']} would get task "
                           f"chaining's {r.b_task} draft (constants); not supported")
    if "teammate_texts" in w:
        out += [f"harness text {k} shows {v} to the agent" for k, v in cue_words(cfg).items()]
    # the orders are the draw's
    out += _draw_mismatches(cfg, o, deepswe_dir, summary_md)
    return out


def _draw_mismatches(cfg: dict, o: list[dict], deepswe_dir: Path | None,
                     summary_md: Path) -> list[str]:
    """The committed orders must be what the draw gives. With the draw's inputs in this
    checkout (trials.json, every clean task's task.toml, the validation summary) the whole draw
    is redone; without them, the arrangement is redone from the committed draw record's
    eligible table and the record's input hashes are reported unchecked."""
    from compaction.s2.chain import week2 as W
    d = Path(deepswe_dir or C.deepswe_dir())
    rec_path = W.resolve(cfg["chain"]["orders"]["path"]).with_name(DRAW_RECORD.name)
    rec = json.loads(rec_path.read_text()) if rec_path.is_file() else None
    try:
        dr = draw(d, summary_md, cfg["design"]["seed"])
    except (OSError, KeyError) as e:
        if rec is None:
            return [(f"orders: cannot redo the draw ({type(e).__name__}: {e}) and no draw "
                     f"record {rec_path}")]
        dr = draw_from_eligible(rec["eligible"], cfg["design"]["seed"])
        dr["inputs"] = rec.get("inputs")
        _NOTES.append(f"draw inputs not in this checkout ({d}); arrangement redone from "
                      f"{rec_path.name}, its input hashes not checked")
    out = []
    if rec is not None and not _NOTES and rec != draw_record(dr):
        out.append(f"{rec_path.name} is not the record the draw gives (rerun "
                   "scripts/week2_chain_stress_draw.py)")
    if rec is not None and _NOTES:
        got = draw_record(dr)
        diff = [k for k in ("pool", "languages", "attempts", "positions", "never_first")
                if rec.get(k) != got[k]]
        if diff:
            out.append(f"{rec_path.name}: {diff} differ from the arrangement its eligible "
                       "table gives")
    if orders(dr) != o:
        out.append(f"chain.orders: {cfg['chain']['orders']['path']} is not what seed "
                   f"{cfg['design']['seed']} and the draw rule give (rerun "
                   "scripts/week2_chain_stress_draw.py)")
    return out


_NOTES: list[str] = []


def notes() -> list[str]:
    """Non-fatal notes from the last code_mismatches (e.g. the draw's inputs not present)."""
    return list(_NOTES)


def held_until_first_arms(c) -> bool:
    """The within-task arm and related repeats from the second on."""
    return c.arm == "within_task" or (c.arm == "related" and c.id.rsplit("-", 1)[-1] != "1")


def launch_plan(run: list, all_chains: list) -> tuple[list, dict[str, list[str]]]:
    """The launch order (unrelated first, then fresh, then related, then the held chains) and,
    for each held chain, the chains that must be done before it starts: every unrelated chain
    and fresh control of the design, wherever launched."""
    prereq = [c.id for c in all_chains if c.arm in FIRST_ARMS]
    ordered = sorted(run, key=lambda c: (held_until_first_arms(c), LAUNCH_RANK.get(c.arm, 9)))
    return ordered, {c.id: prereq for c in ordered if held_until_first_arms(c)}


def orders_balance(chains: list) -> dict:
    """Appendix A's balance of the unrelated arm, from the chains as loaded."""
    u = [[p.task for p in c.phases] for c in chains if c.arm == "unrelated"]
    lang = {t: language(t, None) for ch in u for t in ch}
    rec = json.loads(DRAW_RECORD.read_text()) if DRAW_RECORD.is_file() else {}
    lang = {t: (rec.get("languages") or {}).get(t, lang[t]) for t in lang}
    pos: dict[str, list[int]] = collections.defaultdict(list)
    for ch in u:
        for p, t in enumerate(ch, 1):
            pos[t].append(p)
    n = len(u[0]) if u else 0
    out = {
        "each task 4 times at 4 distinct positions": all(len(v) == 4 and len(set(v)) == 4
                                                         for v in pos.values()),
        "each position leaves out exactly 2 tasks": all(len(pos) - len({ch[p] for ch in u}) == 2
                                                        for p in range(n)),
        "adjacent tasks differ in language": all(lang[a] != lang[b] for ch in u
                                                 for a, b in itertools.pairwise(ch)),
        ">= 3 languages per chain": all(len({lang[t] for t in ch}) >= 3 for ch in u),
        "no task twice in a chain": all(len(set(ch)) == len(ch) for ch in u)}
    return out


# ------------------------------------------------------------------------------------------
# Token lists for the matcher (collected in push-check; chain-go requires them)
# ------------------------------------------------------------------------------------------


def token_manifest_path() -> Path:
    from compaction.s2.chain import chain_stress_score as CSS
    return Path(CSS.TOKEN_DIR) / "MANIFEST.json"


def write_token_manifest(rows: dict[str, dict], path: Path | None = None) -> Path:
    """MANIFEST.json (committed): task -> {sha256 of its token-list file, n_files, base}."""
    p = Path(path or token_manifest_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(p.read_text()) if p.exists() else {}
    p.write_text(json.dumps({**old, **rows}, indent=1, sort_keys=True) + "\n")
    return p


TOKEN_KINDS = ("repo", "package", "branch", "path", "binary")
TOKEN_KEYS = ("task_id", "repo", "branch", "base", "tokens", "context", "instruction", "notes")


def token_list_problems(t: str, tl: dict, base: str | None = None) -> list[str]:
    """One token list's schema and identity: its keys and token kinds (lists of strings), its
    task id, repo and branch (the per-task texts'), and its base (40 hex, starting with
    task.toml's when that is here, and equal to `base` when given)."""
    from compaction.s2.chain import nchain as N
    out = []
    if not isinstance(tl, dict) or any(k not in tl for k in TOKEN_KEYS):
        return [f"{t}: token list lacks keys {[k for k in TOKEN_KEYS if k not in (tl or {})]}"]
    tok = tl["tokens"]
    if not isinstance(tok, dict) or set(tok) != set(TOKEN_KINDS) or not all(
            isinstance(v, list) and all(isinstance(x, str) for x in v) for v in tok.values()):
        out.append(f"{t}: tokens must be lists of strings under {list(TOKEN_KINDS)}")
    if tl["task_id"] != t:
        out.append(f"{t}: token list is {tl['task_id']}'s")
    v = texts().get(t)
    if v is not None and (tl["repo"], tl["branch"]) != (v["repo"], v["branch"]):
        out.append(f"{t}: token list repo/branch {tl['repo']}/{tl['branch']} != "
                   f"{v['repo']}/{v['branch']}")
    b = tl.get("base")
    tb = N.toml_base(t)
    if not (isinstance(b, str) and re.fullmatch(r"[0-9a-f]{40}", b)) or \
            (tb and not b.startswith(tb)) or (base is not None and b != base):
        out.append(f"{t}: token list base {b!r} (task.toml {tb!r}, manifest {base!r})")
    return out


def token_lists_mismatches(task_ids, root: Path | None = None,
                           recorded: dict | None = None) -> list[str]:
    """Every task's token list is present, its file's sha256 is the one the passing push-check
    recorded (`recorded`: task -> sha256, from the push_check_all gate) and the manifest's,
    and its content passes `token_list_problems`."""
    from compaction.s2.chain import chain_stress_score as CSS
    root = Path(root or CSS.TOKEN_DIR)
    mp = root / "MANIFEST.json"
    if not mp.exists():
        return [f"no token-list manifest {mp} (run push-check)"]
    man = json.loads(mp.read_text())
    out = []
    for t in sorted(set(task_ids)):
        f = root / f"{t}.json"
        if t not in man:
            out.append(f"{t}: not in {mp.name}")
            continue
        if not f.exists():
            out.append(f"{t}: token list {f} missing")
            continue
        h = sha256_file(f)
        if h != man[t]["sha256"]:
            out.append(f"{t}: token list sha256 {h[:12]} != manifest {man[t]['sha256'][:12]}")
        if recorded is not None and recorded.get(t) != h:
            out.append(f"{t}: token list sha256 {h[:12]} != the push-check gate's "
                       f"{str(recorded.get(t))[:12]}")
        try:
            tl = json.loads(f.read_text())
        except ValueError as e:
            out.append(f"{t}: token list not JSON: {e}")
            continue
        out += token_list_problems(t, tl, man[t].get("base"))
    return out


# ------------------------------------------------------------------------------------------
# Design card and the agent-visible texts
# ------------------------------------------------------------------------------------------

DESIGN_CARD = CC.REPO_ROOT / "results" / "sprint" / "week2" / EXP / "design_card.md"


CARD_NOTES = (
    ("- Language labels: DeepSWE's metadata calls httpx-deterministic-cookie-store TypeScript "
     "and koota-entity-snapshot-rollback Python; their images are Python and TypeScript, and "
     "the code uses those (LANG_FIX, by repo). Neither is in the pool."),
    ("- Base commit: each container's /app HEAD at setup, which must start with task.toml's "
     "base_commit_hash (koota-entity-snapshot-rollback's has 39 characters); a mismatch fails "
     "the phase, which halts that chain (fail closed)."),
    ("- Launch order: unrelated chains first, then fresh controls and related chains as slots "
     "free; the within-task chains start only after every unrelated chain and fresh control is "
     "done (held otherwise, never run early)."),
    ("- Stop rule: once 15 unrelated tasks have finished, scorer status unknown in more than "
     "10% (2 or more) admits no new task; running tasks finish."),
    ("- Saved states: kept images only for phases followed by a state; a state whose kept "
     "image or scorer home is missing is listed as missing, not saved."),
)


def design_card(cfg: dict, chains: list, s, out_path: Path = DESIGN_CARD) -> str:
    """week2.design_card for every chain, plus arms x chains, the fresh controls' tasks and the
    session settings each arm runs under."""
    from compaction.s2.chain import nchain as N
    from compaction.s2.chain import week2 as W
    rows = N.design_rows(chains, s)
    for r in rows:  # a fresh control has no boundary and no switch
        if r["arm"] == "fresh":
            r["start"], r["project"] = "fresh", "-"
    text = W.design_card(cfg, rows, out_path)
    by_arm: dict[str, list] = collections.defaultdict(list)
    for c in chains:
        by_arm[c.arm].append(c)
    out = ["## Arms and chains", "",
           "| arm | chains | task runs | ids | automatic compaction inside tasks |",
           "|---|---|---|---|---|"]
    for arm, cs in by_arm.items():
        thr = s.auto_threshold(arm)
        out.append(f"| {arm} | {len(cs)} | {sum(len(c.phases) for c in cs)} | "
                   f"{', '.join(c.id for c in cs)} | {thr if thr else 'off'} |")
    out.append(f"| **total** | **{len(chains)}** | **{sum(len(c.phases) for c in chains)}** "
               "| | |")
    fresh = [c for c in chains if c.arm == "fresh"]
    out += ["", "## Fresh controls", "", "| id | task | repo |", "|---|---|---|"]
    out += [f"| {c.id} | {c.phases[0].task} | {E.repo_name(c.phases[0].task)} |" for c in fresh]
    out += ["", "## Session", "",
            (f"- boundary: {s.boundary}; pi's update prompt from boundary "
             f"{s.update_prompt_from_boundary}"),
            f"- ticket tags: {'on' if s.tag_line else 'off'}"
            + (f' ("{s.tag_line}")' if s.tag_line else ""),
            f"- saved states: {', '.join(s.save_state_after) or 'none'} (fresh controls: none)",
            ("- phase 1: junk on its branch (fresh controls: the teammate draft); phases 2..N: "
             "a container switch and the teammate draft"), ""]
    bal = orders_balance(chains)
    out += ["## Orders balance (unrelated arm)", "", "| check | holds |", "|---|---|"]
    out += [f"| {k} | {'yes' if v else '**NO**'} |" for k, v in bal.items()]
    out += ["", "## Notes", "", *CARD_NOTES, ""]
    extra = "\n".join(out)
    p = Path(out_path)
    p.write_text(text + "\n" + extra)
    return text + "\n" + extra


# "evaluation" is boa's own subject (JS evaluation cancellation: its task id, branch and draft);
# the cue is "eval" as a word.
CUE_WORD_RE = re.compile(r"\b(traps?|tests?|evals?|experiments?)\b", re.IGNORECASE)


def harness_texts(cfg: dict) -> dict[str, str]:
    """Every text the harness itself adds to what the agent sees (not DeepSWE's task texts):
    the tag line, lead-ins, task ending, per-task repo names, branches, drafts, junk files and
    commit messages, the teammate's commit message."""
    from compaction.s2.chain import week2 as W
    w = cfg["wording"]
    out = {k: W.resolve(w[k]["path"]).read_text()
           for k in ("ticket_tag_line", "task_ending", "lead_same", "lead_different")}
    for t, v in texts(W.resolve(w["teammate_texts"]["path"])).items():
        for k in ("repo", "branch", "feature", "tag", "teammate_file", "teammate_text",
                  "junk_file"):
            out[f"{t}.{k}"] = v[k]
    out["junk_commits"] = "\n".join(CC.JUNK_COMMITS)
    out["teammate_message"] = CC.TEAMMATE_MESSAGE
    return out


def cue_words(cfg: dict) -> dict[str, list[str]]:
    """Harness texts with "trap", "test", "eval" or "experiment" as a word."""
    return {k: sorted({m.group(0) for m in CUE_WORD_RE.finditer(v)})
            for k, v in harness_texts(cfg).items() if CUE_WORD_RE.search(v)}
