"""On-pod step driver for I9 (implementation plan §2 U3, §4 P1/P3, §6; modelled on
scripts/i1a_pod.py).

    python scripts/i9_pod.py <step> [--device cuda] [--limit N] [--max-new-tokens K]
                             [--thinking-arm] [--unsealed] [--sampler vllm|hf|auto] [--force]

Session A (P1): download, smoke, sample-qwen, sample-gemma, capture-last, manifest --session A.
Session B (P3): download, capture-64, olens-pilot (the pilot phase), then [sample-thinking],
tokens, [tokens-sealed], olens, [olens-sealed], manifest --session B (scripts/pod_run_i9.sh).
The thinking-on arm moved to session B (prereg amendment B item 7: decided at checkpoint 2); it
needs only B's Qwen download and the vLLM venv, and the driver refuses it when $I9_SESSION is A.

    download         named-file downloads (emx.i1a.hfget) -> results/I9/download_paths.json
    smoke            4 scenarios x 4 replies per model; sampler-vs-capture ids equality; the I1a
                     reproduction check (plus the raw cosine if I1a's stored activations are
                     supplied); the prompt-scope assert (prereg §4.5; plan §6)
    sample-qwen      Qwen, thinking off: confirmatory, sealed, bank_sampled
    sample-gemma     Gemma, thinking off: confirmatory, sealed
    sample-thinking  Qwen, thinking on: the thinking subset (session B; only with --thinking-arm)
    capture-last     last-prompt-token residuals (Qwen: confirmatory, sealed, bank_sampled;
                     Gemma: confirmatory, sealed)
    capture-64       Qwen's last 64 prompt positions (confirmatory, bank_validation, sealed;
                     capturing the sealed set reads nothing from it)
    tokens           logit lens and J-Lens, 12 layers, confirmatory
    tokens-sealed    logit lens and J-Lens on the sealed set (only with --unsealed)
    olens-pilot      one timed Oracle Lens batch of 32 -> results/I9/olens_pilot.json
    olens            Oracle Lens, 5 layers: confirmatory and bank_validation
    olens-sealed     Oracle Lens on the sealed set (only with --unsealed)
    manifest         closing manifest over the session's step manifests: the three manifest
                     files of every session step (required and optional) that has one. It counts
                     as done only while it lists exactly those steps and every listed file still
                     verifies, so a step completed after it (e.g. a later --unsealed or
                     --thinking-arm run, or a --force rerun) makes it regenerate

Every step, on success only, writes three files (plan §6):
    results/I9/MANIFEST.<step>.sha256   `shasum -a 256` lines, repo-relative paths (verified by
                                        scripts/pod_pull_results.sh)
    results/I9/MANIFEST.<step>.json     the I1a sidecar ({step, required_outputs, counts, checks,
                                        code_commit, inputs_sha256, limited, args})
    results/I9/manifests/<step>.json    contracts.Manifest (inputs and outputs as FileRefs)
A step whose manifests verify for the current code and the same `limited` flag is skipped
(`--force` reruns it); inside a step, capture and Oracle Lens draws resume on their own.

Sampling shells out to U2's sampler, `scripts/i9_sample_vllm.py --model {qwen,gemma}
--thinking/--no-thinking --set <keys> --out <path> --model-dir <dir>` (Python from $I9_VLLM_PY,
default /ws/venv-vllm/bin/python), or `scripts/i9_sample_hf.py` with the same CLI (`--sampler hf`
runs it with this venv's Python; `auto` = vLLM, then HF on failure). A limited run (and the
smoke) passes the exact scenario ids (`--ids a,b,...`; the sampler's own `--limit` is global over
the sets, ours is per set); the smoke adds `--n-samples 4 --step smoke_sample_<model>`.
The smoke's ids check renders the same scenarios in the sampler's venv (a short
`$I9_VLLM_PY -c` call to `emx.i9.render.render_ids` with the model directory's tokenizer) and
compares the hashes with the capture index, so a tokenizer or template difference between the
two venvs is caught.

Environment (scripts/pod_run_i9.sh): $I9_SESSION (A or B) picks the download groups (A: qwen,
gemma; B: qwen, readers) and the closing manifest's session; $EMX_GIT_COMMIT (and
$EMX_GIT_DIRTY=1) is the code commit every manifest records. Optional data/I9/run_config.json
holds cut settings, e.g. {"olens_layers": [20, 44, 60]} (default contracts.OLENS_LAYERS); the
Oracle Lens steps read it, hash it as an input and record the layers in their manifest notes.
`--limit` and `--max-new-tokens` exist for tests; the manifests record them as `limited`.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emx.i9 import acts, provenance
from emx.i9 import contracts as C
from emx.i9 import readers as R

PATHS_FILE = "download_paths.json"
STEPS = (
    "download",
    "smoke",
    "sample-qwen",
    "sample-gemma",
    "sample-thinking",
    "capture-last",
    "capture-64",
    "tokens",
    "tokens-sealed",
    "olens-pilot",
    "olens",
    "olens-sealed",
    "manifest",
)
SESSION_STEPS: dict[str, tuple[str, ...]] = {
    "A": ("download", "smoke", "sample-qwen", "sample-gemma", "capture-last"),
    "B": ("download", "capture-64", "tokens", "olens-pilot", "olens"),
}
SESSION_OPTIONAL: dict[str, tuple[str, ...]] = {
    "A": (),
    "B": ("sample-thinking", "tokens-sealed", "olens-sealed"),  # amendment B item 7: thinking in B
}
UNSEALED_STEPS = ("tokens-sealed", "olens-sealed")  # read the sealed set: only with --unsealed
UPSTREAM: dict[str, tuple[str, ...]] = {
    "smoke": ("download",),
    "sample-qwen": ("smoke",),
    "sample-gemma": ("smoke",),
    "sample-thinking": ("download",),  # session B: A's smoke manifest is not on B's pod
    "capture-last": ("download",),
    "capture-64": ("download",),
    "tokens": ("capture-64",),
    "tokens-sealed": ("capture-64",),
    "olens-pilot": ("capture-64",),
    "olens": ("olens-pilot",),
    "olens-sealed": ("olens",),
}
# (model, thinking, set keys) per sampling step
SAMPLE_PLAN: dict[str, tuple[str, bool, tuple[str, ...]]] = {
    "sample-qwen": ("qwen", False, ("confirmatory", "sealed", "bank_sampled")),
    "sample-gemma": ("gemma", False, ("confirmatory", "sealed")),
    "sample-thinking": ("qwen", True, ("thinking",)),
}
CAPTURE_LAST_SETS: dict[str, tuple[str, ...]] = {
    "qwen": ("confirmatory", "sealed", "bank_sampled"),
    "gemma": ("confirmatory", "sealed"),
}
CAPTURE_64_SETS = ("confirmatory", "bank_validation", "sealed")
OLENS_SETS = ("confirmatory", "bank_validation")
SESSION_DOWNLOADS: dict[str, tuple[str, ...]] = {"A": ("qwen", "gemma"), "B": ("qwen", "readers")}
SMOKE_N_SCENARIOS = 4
SMOKE_N_SAMPLES = 4
PINS = {
    "qwen": f"{C.QWEN_ID}@{C.QWEN_REV}",
    "gemma": f"{C.GEMMA_ID}@{C.GEMMA_REV}",
    "jlens": f"{C.JLENS_REPO}@{C.JLENS_REV}",
    "olens_adapter": f"{C.ADAPTER_REPO}@{C.ADAPTER_REV}",
    "wsbench": C.WSBENCH_COMMIT,
    "lynch": C.LYNCH_COMMIT,
}


# ------------------------------------------------------------------ small helpers


def utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[i9_pod {utc()}] {msg}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    p = Path(path).resolve()
    try:
        return str(p.relative_to(C.REPO_ROOT.resolve()))
    except ValueError:
        return str(p)


def git_state() -> tuple[str, bool]:
    """(commit, dirty): the shared rule in emx.i9.provenance."""
    return provenance.git_state()


def rows_of(path: Path) -> int | None:
    if path.suffix != ".jsonl":
        return None
    with open(path, "rb") as f:
        return sum(1 for line in f if line.strip())


def sidecar_path(step: str) -> Path:
    return C.RESULTS / f"MANIFEST.{step}.json"


def sha_manifest_path(step: str) -> Path:
    return C.RESULTS / f"MANIFEST.{step}.sha256"


def run_config_path() -> Path:
    return C.RUN_CONFIG_PATH


def olens_layers() -> tuple[int, ...]:
    """Oracle Lens layers: data/I9/run_config.json "olens_layers" (the pre-set cut), else
    contracts.OLENS_LAYERS. Only subsets of OLENS_LAYERS are accepted."""
    p = run_config_path()
    if not p.exists():
        return C.OLENS_LAYERS
    layers = tuple(int(x) for x in json.loads(p.read_text()).get("olens_layers", C.OLENS_LAYERS))
    if not layers or not set(layers) <= set(C.OLENS_LAYERS):
        raise SystemExit(f"{p}: olens_layers {layers} must be a subset of {C.OLENS_LAYERS}")
    return tuple(sorted(layers))


def limited(args: argparse.Namespace) -> bool:
    return bool(args.limit or args.max_new_tokens)


def write_manifest(
    step: str,
    outputs: list[Path],
    *,
    args: argparse.Namespace,
    started: str,
    counts: dict[str, int] | None = None,
    checks: dict[str, bool | None] | None = None,
    seeds: dict[str, int] | None = None,
    notes: str = "",
) -> Path:
    """MANIFEST.<step>.sha256, its I1a sidecar and the contracts.Manifest; written last, only on
    success. Inputs: the scenario files, download_paths.json and the upstream sha256 manifests
    (every declared input must exist)."""
    outs = sorted({Path(p) for p in outputs})
    missing = [str(p) for p in outs if not p.exists()]
    if missing:
        raise RuntimeError(f"{step}: required outputs missing: {missing}")
    inputs = [C.SCENARIOS_PATH, C.SETS_PATH]
    if step != "download":
        inputs.append(C.RESULTS / PATHS_FILE)
    inputs += [sha_manifest_path(u) for u in UPSTREAM.get(step, ())]
    if step.startswith("olens") and run_config_path().exists():
        inputs.append(run_config_path())
    absent = [rel(p) for p in inputs if not p.exists()]
    if absent:
        raise RuntimeError(f"{step}: declared inputs missing: {absent}")
    commit, dirty = git_state()
    out_refs = [C.FileRef(rel(p), sha256_file(p), rows_of(p)) for p in outs]
    in_refs = [C.FileRef(rel(p), sha256_file(p), rows_of(p)) for p in inputs]
    C.Manifest(
        step=step,
        inputs=in_refs,
        outputs=out_refs,
        git_commit=commit,
        git_dirty=dirty,
        seeds=seeds or {},
        pins=PINS,
        started_utc=started,
        finished_utc=utc(),
        usd=0.0,
        notes=notes + (" [limited test run]" if limited(args) else ""),
    ).write()
    side = {
        "step": step,
        "utc": utc(),
        "required_outputs": [r.path for r in out_refs],
        "counts": {k: int(v) for k, v in (counts or {}).items()},
        "checks": checks or {},
        "code_commit": commit,
        "inputs_sha256": {r.path: r.sha256 for r in in_refs},
        "limited": limited(args),
        "args": {k: v for k, v in vars(args).items() if k != "func"},
    }
    tmp = sidecar_path(step).with_suffix(".json.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(side, indent=2, sort_keys=True) + "\n")
    tmp.replace(sidecar_path(step))
    man = sha_manifest_path(step)
    tmp = man.with_suffix(".sha256.tmp")
    tmp.write_text("".join(f"{r.sha256}  {r.path}\n" for r in out_refs))
    tmp.replace(man)
    log(f"{step}: manifest {rel(man)} ({len(out_refs)} files)")
    return man


def session_of(args: argparse.Namespace) -> str:
    return args.session or os.environ.get("I9_SESSION", "")


def closing_steps(session: str) -> list[str]:
    """The session's steps (required, then optional) that have a sha256 manifest now."""
    steps = (*SESSION_STEPS[session], *SESSION_OPTIONAL[session])
    return [s for s in steps if sha_manifest_path(s).exists()]


def listed_steps(man: Path) -> set[str]:
    """The steps whose MANIFEST.<step>.sha256 a closing manifest lists."""
    out = set()
    for line in man.read_text().splitlines():
        name = Path(line.partition("  ")[2]).name
        if name.startswith("MANIFEST.") and name.endswith(".sha256"):
            out.add(name[len("MANIFEST.") : -len(".sha256")])
    return out


def step_done(step: str, args: argparse.Namespace) -> bool:
    """The step's sha256 manifest verifies, its sidecar names it with the same `limited` flag
    and the current code commit, and its contracts.Manifest exists. The closing `manifest` must
    also list exactly the session's steps that have a manifest now (review r2 finding 1: a step
    completed after it, such as a later --unsealed run, makes it stale; a rerun of a listed step
    changes a listed hash)."""
    man, side = sha_manifest_path(step), sidecar_path(step)
    if not (man.exists() and side.exists() and C.manifest_path(step).exists()):
        return False
    d = json.loads(side.read_text())
    if d.get("step") != step or d.get("limited") != limited(args):
        return False
    if d.get("code_commit") != git_state()[0]:
        return False
    for line in man.read_text().splitlines():
        sha, _, p = line.partition("  ")
        path = Path(p) if Path(p).is_absolute() else C.REPO_ROOT / p
        if not path.exists() or sha256_file(path) != sha:
            return False
    if step == "manifest":
        session = session_of(args)
        if session not in SESSION_STEPS or listed_steps(man) != set(closing_steps(session)):
            return False
    return True


def free_gpu() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def paths() -> dict[str, Any]:
    p = C.RESULTS / PATHS_FILE
    if not p.exists():
        raise SystemExit(f"{rel(p)} missing: run the download step first")
    return json.loads(p.read_text())


def backend(model: str, args: argparse.Namespace) -> Any:
    dtype = "float32" if args.device == "cpu" else C.DTYPE
    return acts.load_backend(model, paths()[f"{model}_dir"], device=args.device, dtype=dtype)


def sampling(args: argparse.Namespace) -> Any:
    from dataclasses import replace

    from emx.i1a import olens

    s = olens.default_sampling()
    return replace(s, max_new_tokens=args.max_new_tokens) if args.max_new_tokens else s


# ------------------------------------------------------------------ scenarios


def load_scenarios() -> dict[str, C.Scenario]:
    rows = [json.loads(x) for x in C.SCENARIOS_PATH.read_text("utf-8").splitlines() if x.strip()]
    return {r["id"]: C.Scenario(**r) for r in rows}


def set_ids(key: str, args: argparse.Namespace | None = None) -> list[str]:
    sets = json.loads(C.SETS_PATH.read_text("utf-8"))
    if key not in C.SET_KEYS or key not in sets:
        raise KeyError(f"set {key!r} not in {C.SETS_PATH.name} (keys {C.SET_KEYS})")
    ids = list(sets[key])
    return ids[: args.limit] if args is not None and args.limit else ids


def scenarios_for(keys: tuple[str, ...], args: argparse.Namespace) -> list[C.Scenario]:
    by_id = load_scenarios()
    ids = list(dict.fromkeys(i for k in keys for i in set_ids(k, args)))
    return [by_id[i] for i in ids]


# ------------------------------------------------------------------ sampling


def vllm_python() -> str:
    return os.environ.get("I9_VLLM_PY", "/ws/venv-vllm/bin/python")


def sampler_cmd(
    sampler: str,
    model: str,
    thinking: bool,
    keys: tuple[str, ...],
    out: Path,
    *,
    model_dir: str | None = None,
    ids: list[str] | None = None,
    n_samples: int = 0,
    step: str | None = None,
) -> list[str]:
    if sampler == "vllm":
        py, script = vllm_python(), "vllm"
    elif sampler == "hf":
        py, script = sys.executable, "hf"
    else:
        raise ValueError(f"unknown sampler {sampler!r}")
    cmd = [
        py,
        str(C.REPO_ROOT / "scripts" / f"i9_sample_{script}.py"),
        "--model",
        model,
        "--thinking" if thinking else "--no-thinking",
        "--set",
        ",".join(keys),
        "--out",
        str(out),
    ]
    if model_dir:
        cmd += ["--model-dir", model_dir]
    if ids:
        cmd += ["--ids", ",".join(ids)]
    if n_samples:
        cmd += ["--n-samples", str(n_samples)]
    if step:
        cmd += ["--step", step]
    return cmd


def run_cmd(cmd: list[str], env: dict[str, str]) -> int:
    """One sampler subprocess from the repo root; its exit code (tests replace this)."""
    return subprocess.run(cmd, env=env, cwd=C.REPO_ROOT, check=False).returncode


def run_capture(cmd: list[str]) -> str:
    """stdout of a helper subprocess run from the repo root (tests replace this)."""
    return subprocess.run(cmd, cwd=C.REPO_ROOT, capture_output=True, text=True, check=True).stdout


def run_sampler(
    model: str,
    thinking: bool,
    keys: tuple[str, ...],
    out: Path,
    args: argparse.Namespace,
    *,
    ids: list[str] | None = None,
    n_samples: int = 0,
    step: str | None = None,
) -> str:
    """Run U2's sampler; with --sampler auto, the HF fallback after a vLLM failure. Returns the
    sampler that succeeded."""
    order = ["vllm", "hf"] if args.sampler == "auto" else [args.sampler]
    env = dict(os.environ)
    out.parent.mkdir(parents=True, exist_ok=True)
    for s in order:
        cmd = sampler_cmd(
            s,
            model,
            thinking,
            keys,
            out,
            model_dir=str(paths()[f"{model}_dir"]),
            ids=ids,
            n_samples=n_samples,
            step=step,
        )
        log(f"sampler: {' '.join(cmd)}")
        rc = run_cmd(cmd, env)
        if rc == 0:
            return s
        log(f"sampler {s} exited {rc}")
    raise SystemExit(f"sampling {model} thinking={thinking} failed with {order}")


def check_replies(
    path: Path, model: str, thinking: bool, ids: list[str], n_samples: int, *, name: str
) -> C.CheckResult:
    """Every (scenario, sample_idx) exactly once, of this model and arm, with its request seed."""
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
    want = {(i, k) for i in ids for k in range(n_samples)}
    got = [(r["scenario_id"], r["sample_idx"]) for r in rows]
    bad = [
        r
        for r in rows
        if r["model"] != model
        or bool(r["thinking"]) != thinking
        or r["seed"] != C.request_seed(model, thinking, r["scenario_id"], r["sample_idx"])
    ]
    ok = set(got) == want and len(got) == len(want) and not bad
    return C.CheckResult(
        name=name,
        passed=ok,
        value=float(len(rows)),
        threshold=float(len(want)),
        n=len(rows),
        details={
            "missing": len(want - set(got)),
            "extra_or_dup": len(got) - len(set(got) & want),
            "bad_rows": len(bad),
        },
        utc=utc(),
    )


def sampler_outputs(out: Path) -> list[Path]:
    """The replies file plus the sampler's sidecars named <out stem>.* (manifest, throughput)."""
    return [
        out,
        *sorted(
            p
            for p in out.parent.glob(f"{out.stem}.*")
            if p != out and p.is_file() and not p.name.endswith(".tmp")
        ),
    ]


SAMPLER_RENDER = (
    "import json, sys\n"
    "from transformers import AutoTokenizer\n"
    "from emx.i9 import render\n"
    "from emx.i9 import contracts as C\n"
    "from emx.i9.acts import ids_sha256\n"
    "model, model_dir, path = sys.argv[1:4]\n"
    "tok = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)\n"
    "rows = [json.loads(x) for x in open(path, encoding='utf-8') if x.strip()]\n"
    "out = {r['id']: ids_sha256(render.render_ids(model, C.Scenario(**r), thinking=False,\n"
    "       tokenizer=tok)) for r in rows}\n"
    "print(json.dumps(out))\n"
)


def sampler_render_hashes(model: str, scs: list[C.Scenario], sdir: Path) -> dict[str, str]:
    """{scenario id: ids_sha256} rendered by emx.i9.render in the sampler's venv."""
    if vllm_python() == sys.executable:
        log("note: the sampler venv is this venv; the ids check is within one environment")
    sdir.mkdir(parents=True, exist_ok=True)
    tmp = sdir / f"render_{model}.scenarios.jsonl"
    tmp.write_text("".join(C.to_json(s) + "\n" for s in scs), encoding="utf-8")
    cmd = [vllm_python(), "-c", SAMPLER_RENDER, model, str(paths()[f"{model}_dir"]), str(tmp)]
    return json.loads(run_capture(cmd).strip().splitlines()[-1])


def ids_equal_check(model: str, sampler_hashes: dict[str, str], store: Path) -> C.CheckResult:
    """Sampler-venv render == capture render, by ids hash, for every captured scenario."""
    _, sites = acts.load_index(store)
    cap = {s.id: s.ids_sha256 for s in sites}
    diff = sorted(i for i in cap if sampler_hashes.get(i) != cap[i])
    return C.CheckResult(
        name=f"ids_equal_{model}",
        passed=not diff,
        value=float(len(cap) - len(diff)),
        threshold=float(len(cap)),
        n=len(cap),
        details={"differ": diff},
        utc=utc(),
    )


# ------------------------------------------------------------------ steps


def _model_files(repo: str, rev: str) -> list[str]:
    """Top-level weights, configs and tokenizer files at a pinned revision (hfget.model_files's
    rule, for any repo)."""
    from emx.i1a import hfget

    names = hfget._api().list_repo_files(repo, revision=hfget.check_revision(rev))
    keep = sorted(
        n
        for n in names
        if "/" not in n
        and (
            n.endswith((".safetensors", ".json"))
            or n.startswith("tokenizer")
            or n in hfget.MODEL_FILE_NAMES
        )
    )
    if "config.json" not in keep or not any(n.endswith(".safetensors") for n in keep):
        raise RuntimeError(f"{repo}@{rev[:8]}: no config.json or safetensors listed")
    return keep


def download_groups(args: argparse.Namespace) -> list[str]:
    """--groups if given, else by $I9_SESSION: A -> qwen, gemma; B -> qwen, readers."""
    if args.groups:
        return args.groups.split(",")
    session = os.environ.get("I9_SESSION", "")
    if session not in SESSION_DOWNLOADS:
        raise SystemExit("download: set $I9_SESSION (A or B) or pass --groups")
    return list(SESSION_DOWNLOADS[session])


def step_download(args: argparse.Namespace) -> list[Path]:
    from emx.i1a import contracts as I
    from emx.i1a import hfget

    root = Path(args.artifacts_dir)
    p = C.RESULTS / PATHS_FILE
    got: dict[str, str] = json.loads(p.read_text()) if p.exists() else {}
    for g in download_groups(args):
        if g in ("qwen", "gemma"):
            repo, rev = C.MODEL_ID[g], C.MODEL_REV[g]
            fetched = hfget.fetch(repo, _model_files(repo, rev), revision=rev, local_dir=root / g)
            got[f"{g}_dir"] = str(fetched["config.json"].parent)
        elif g == "readers":
            j = hfget.fetch(
                I.JLENS_REPO, [I.JLENS_FILE], revision=I.JLENS_REV, local_dir=root / "jlens"
            )
            a = hfget.fetch(
                I.ADAPTER_REPO,
                list(I.OLENS_FILES),
                revision=I.ADAPTER_REV,
                local_dir=root / "adapters",
            )
            got["jlens"] = str(j[I.JLENS_FILE])
            got["olens_dir"] = str(a[I.OLENS_FILES[0]].parent)
        else:
            raise SystemExit(f"unknown download group {g!r} (qwen, gemma, readers)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    return [p]


def _counts_ok(step: str, got: dict[str, int], want: dict[str, int], args) -> None:
    if args.limit:
        return
    diff = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
    if diff:
        raise RuntimeError(f"{step}: counts differ from the contract (got, want): {diff}")


def step_sample(step: str, args: argparse.Namespace) -> tuple[list[Path], dict, dict]:
    model, thinking, keys = SAMPLE_PLAN[step]
    out = C.replies_path(model, thinking)
    ids = list(dict.fromkeys(i for k in keys for i in set_ids(k, args)))
    run_sampler(model, thinking, keys, out, args, ids=ids if args.limit else None)
    name = f"replies_{C.replies_path(model, thinking).stem}"
    res = check_replies(out, model, thinking, ids, C.N_SAMPLES, name=name)
    res.write()
    if not res.passed:
        raise SystemExit(f"{step}: replies incomplete or inconsistent: {res.details}")
    return (
        [*sampler_outputs(out), C.check_path(res.name)],
        {"replies": int(res.n)},
        {res.name: res.passed},
    )


def step_capture_last(args: argparse.Namespace) -> tuple[list[Path], dict]:
    outs, counts = [], {}
    for model, keys in CAPTURE_LAST_SETS.items():
        bk = backend(model, args)
        scs = scenarios_for(keys, args)
        p = acts.capture_last(bk, model, scs)
        outs += [p, acts.index_path(p)]
        counts[model] = len(acts.load_index(p)[1])
        del bk
        free_gpu()
    want = {
        "qwen": C.N_CONFIRMATORY + C.N_SEALED + C.N_BANK_SAMPLED,
        "gemma": C.N_CONFIRMATORY + C.N_SEALED,
    }
    _counts_ok("capture-last", counts, want, args)
    return outs, counts


def step_capture_64(args: argparse.Namespace) -> tuple[list[Path], dict]:
    bk = backend("qwen", args)
    p = acts.capture_last64(bk, scenarios_for(CAPTURE_64_SETS, args))
    n = len(acts.load_index(p)[1])
    want = (C.N_CONFIRMATORY + C.N_BANK_VALIDATION + C.N_SEALED) * C.N_READ_POSITIONS
    _counts_ok("capture-64", {"sites": n}, {"sites": want}, args)
    return [p, acts.index_path(p)], {"sites": n}


def step_tokens(sets: tuple[str, ...], args: argparse.Namespace) -> tuple[list[Path], dict]:
    """Logit lens and J-Lens on ``sets`` (``tokens``: confirmatory; ``tokens-sealed``: sealed,
    its own step and manifest, so a later unsealing is never skipped as already done)."""
    from emx.i1a import token_readers as tr

    bk = backend("qwen", args)
    outs, counts = [], {}
    for name in R.TOKEN_READERS:
        reader = tr.bind_reader(name, bk, jlens_path=paths().get("jlens"))
        for s in sets:
            out = C.readouts_path(R.readout_name(name, s))
            R.read_tokens(reader, store=acts.last64_path(), ids=set_ids(s, args), out=out)
            outs.append(out)
            counts[out.stem] = rows_of(out) or 0
        del reader
        free_gpu()
    if not args.limit:
        for s in sets:
            n = len(set_ids(s)) * C.N_READ_POSITIONS * len(C.LADDER)
            _counts_ok("tokens", counts, {R.readout_name(r, s): n for r in R.TOKEN_READERS}, args)
    return outs, counts


def bind_olens(args: argparse.Namespace) -> Any:
    from emx.i1a import olens

    o = olens.PublicOLens(sampling=sampling(args))
    o.bind(backend("qwen", args), Path(paths()["olens_dir"]))
    return o


def step_olens_pilot(args: argparse.Namespace) -> tuple[list[Path], dict]:
    o = bind_olens(args)
    layers = olens_layers()
    planned = sum(R.n_olens_batches(len(set_ids(s)), layers) for s in OLENS_SETS)
    if args.unsealed:
        planned += R.n_olens_batches(len(set_ids("sealed")), layers)
    rec = R.olens_pilot(
        o,
        store=acts.last64_path(),
        ids=set_ids("confirmatory", args),
        layer=44 if 44 in layers else layers[len(layers) // 2],
        n_batches_planned=planned,
        sampling=sampling(args),
    )
    log(
        f"olens-pilot: {rec['seconds']} s per batch of {rec['batch_size']}; "
        f"{planned} batches -> {rec['projected_hours']} h"
    )
    return [R.pilot_path()], {"batch_size": rec["batch_size"], "batches_planned": planned}


def _olens_sets(sets: tuple[str, ...], args: argparse.Namespace) -> tuple[list[Path], dict]:
    o = bind_olens(args)
    layers = olens_layers()
    outs, counts = [], {}
    for s in sets:
        ids = set_ids(s, args)
        samples = R.generate_olens(
            o, s, store=acts.last64_path(), ids=ids, layers=layers, sampling=sampling(args)
        )
        out = C.readouts_path(R.readout_name(R.OLENS, s))
        R.write_olens_readouts(s, store=acts.last64_path(), samples=samples, out=out)
        outs += [samples, out]
        counts[out.stem] = rows_of(out) or 0
        want = len(ids) * C.N_READ_POSITIONS * len(layers)
        _counts_ok("olens", {out.stem: counts[out.stem]}, {out.stem: want}, args)
    return outs, counts


def step_smoke(args: argparse.Namespace) -> tuple[list[Path], dict, dict]:
    """P1's first step: 4 confirmatory scenarios x 4 replies per model with measured
    throughput (the sampler's sidecar), the sampler-vs-capture ids check per model, the I1a
    reproduction check on Qwen (``acts.i1a_reproduction_check``; it refuses to run unless I1a's
    committed logit-lens readouts hash to I1a's manifest), the raw I1a cosine check only if I1a's
    stored activations are supplied, and the prompt-scope assert on a real readouts file plus a
    planted out-of-scope row (which must raise)."""
    from emx.i1a import token_readers as tr

    sdir = C.RESULTS / "smoke"
    keys = ("confirmatory",)
    n = args.limit or SMOKE_N_SCENARIOS
    sm_args = argparse.Namespace(**{**vars(args), "limit": n})
    scs = scenarios_for(keys, sm_args)
    results: list[C.CheckResult] = []
    outs: list[Path] = []
    for model in C.MODELS:
        out = sdir / f"replies_{model}.jsonl"
        run_sampler(
            model,
            False,
            keys,
            out,
            args,
            ids=[s.id for s in scs],
            n_samples=SMOKE_N_SAMPLES,
            step=f"smoke_sample_{model}",
        )
        outs += sampler_outputs(out)
        res = check_replies(
            out, model, False, [s.id for s in scs], SMOKE_N_SAMPLES, name=f"replies_{model}"
        )
        results.append(res)
        bk = backend(model, args)
        store = acts.capture_last(bk, model, scs, path=sdir / f"{model}_last.safetensors")
        outs += [store, acts.index_path(store)]
        results.append(ids_equal_check(model, sampler_render_hashes(model, scs, sdir), store))
        if model == "qwen":
            results.append(acts.i1a_reproduction_check(bk, write=False))
            if acts.i1a_stored_acts_ok():
                results.append(acts.i1a_cosine_check(bk, write=False))
            s64 = acts.capture_last64(bk, scs, path=sdir / "qwen_last64.safetensors")
            outs += [s64, acts.index_path(s64)]
            results.append(_scope_check(tr.bind_reader("logit_lens", bk), s64, scs, sdir))
            outs.append(sdir / "readouts_logit_lens.jsonl")
        del bk
        free_gpu()
    for r in results:
        r.utc = r.utc or utc()
        path = C.check_path(r.name if r.name.startswith("smoke_") else f"smoke_{r.name}")
        r.write(path)
        outs.append(path)
    passed = {r.name: r.passed for r in results}
    log(f"smoke: {json.dumps(passed)}")
    bad = [k for k, v in passed.items() if v is not True]
    if bad and not args.limit:
        log(f"smoke: checks not passed: {bad}; no manifest written")
        raise SystemExit(3)
    counts = {"checks": len(results), "checks_passed": sum(v is True for v in passed.values())}
    return outs, counts, passed


def _scope_check(reader: Any, store: Path, scs: list[C.Scenario], sdir: Path) -> C.CheckResult:
    out = sdir / "readouts_logit_lens.jsonl"
    R.read_tokens(reader, store=store, ids=[s.id for s in scs], out=out, layers=(C.LADDER[-1],))
    _, sites = acts.load_index(store)
    rs = acts.rollout_starts(sites)
    planted = {"id": scs[0].id, "layer": C.LADDER[-1], "pos": rs[scs[0].id], "tokens": ["x"]}
    try:
        R.scope_assert([planted], rs)
        planted_raised = False
    except R.PromptScopeError:
        planted_raised = True
    n = rows_of(out) or 0
    return C.CheckResult(
        name="prompt_scope",
        passed=planted_raised and n > 0,
        value=float(n),
        threshold=None,
        n=n,
        details={
            "planted_row_raised": planted_raised,
            "max_pos_vs_rollout_start": {
                i: [max(s.pos for s in sites if s.id == i), r] for i, r in rs.items()
            },
        },
        notes="every written row passed scope_assert; a planted pos == rollout_start row must raise",
        utc=utc(),
    )


def step_manifest(args: argparse.Namespace) -> tuple[list[Path], dict]:
    session = session_of(args)
    if session not in SESSION_STEPS:
        raise SystemExit("manifest: set $I9_SESSION (A or B) or pass --session")
    need = SESSION_STEPS[session]
    have = closing_steps(session)
    lacking = [s for s in need if s not in have]
    if lacking:
        raise SystemExit(f"session {session}: steps without a manifest: {lacking}")
    outs = [p for s in have for p in (sha_manifest_path(s), sidecar_path(s), C.manifest_path(s))]
    return outs, {"steps": len(have)}


def run(step: str, args: argparse.Namespace) -> Path | None:
    if step in UNSEALED_STEPS and not args.unsealed:
        raise SystemExit(f"{step} runs only with --unsealed (the spread check failed)")
    if step == "sample-thinking" and not args.thinking_arm:
        raise SystemExit("sample-thinking runs only with --thinking-arm")
    if step == "sample-thinking" and os.environ.get("I9_SESSION") == "A":
        raise SystemExit("sample-thinking belongs to session B (prereg amendment B item 7)")
    if not args.force and step_done(step, args):
        log(f"skip {step}: its manifests verify")
        return None
    absent = [u for u in UPSTREAM.get(step, ()) if not sha_manifest_path(u).exists()]
    if absent:  # fail before any GPU work, not at manifest time
        raise SystemExit(f"{step}: upstream step(s) {absent} have no manifest; run them first")
    started = utc()
    checks: dict[str, bool | None] = {}
    seeds: dict[str, int] = {"scenario_draw": C.SCENARIO_DRAW_SEED}
    notes = f"olens_layers={list(olens_layers())}" if step.startswith("olens") else ""
    if step == "download":
        outs, counts = step_download(args), {"groups": len(download_groups(args))}
    elif step == "smoke":
        outs, counts, checks = step_smoke(args)
    elif step in SAMPLE_PLAN:
        outs, counts, checks = step_sample(step, args)
    elif step == "capture-last":
        outs, counts = step_capture_last(args)
    elif step == "capture-64":
        outs, counts = step_capture_64(args)
    elif step == "tokens":
        outs, counts = step_tokens(("confirmatory",), args)
    elif step == "tokens-sealed":
        outs, counts = step_tokens(("sealed",), args)
    elif step == "olens-pilot":
        outs, counts = step_olens_pilot(args)
        seeds["olens_confirmatory"] = R.OLENS_DRAWS["confirmatory"]
    elif step == "olens":
        outs, counts = _olens_sets(OLENS_SETS, args)
        seeds.update({f"olens_{s}": R.OLENS_DRAWS[s] for s in OLENS_SETS})
    elif step == "olens-sealed":
        outs, counts = _olens_sets(("sealed",), args)
        seeds["olens_sealed"] = R.OLENS_DRAWS["sealed"]
    elif step == "manifest":
        outs, counts = step_manifest(args)
    else:  # pragma: no cover - argparse restricts the choices
        raise SystemExit(f"unknown step {step}")
    return write_manifest(
        step,
        outs,
        args=args,
        started=started,
        counts=counts,
        checks=checks,
        seeds=seeds,
        notes=notes,
    )


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("step", choices=STEPS)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--artifacts-dir", default="/ws/i9_artifacts")
    ap.add_argument("--groups", default="", help="download: qwen,gemma,readers ($I9_SESSION)")
    ap.add_argument(
        "--session",
        choices=sorted(SESSION_STEPS),
        default="",
        help="manifest (default $I9_SESSION)",
    )
    ap.add_argument("--sampler", choices=("vllm", "hf", "auto"), default="vllm")
    ap.add_argument("--thinking-arm", action="store_true", help="allow sample-thinking (session B)")
    ap.add_argument("--unsealed", action="store_true", help="read the sealed set (spread failed)")
    ap.add_argument("--force", action="store_true", help="rerun a step whose manifests verify")
    ap.add_argument("--limit", type=int, default=0, help="tests only: first N scenarios per set")
    ap.add_argument("--max-new-tokens", type=int, default=0, help="tests only (Oracle Lens)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    t = time.time()
    log(f"step {args.step} start")
    run(args.step, args)
    log(f"step {args.step} done in {time.time() - t:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
