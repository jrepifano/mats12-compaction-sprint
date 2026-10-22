"""Reconstruct the task prompt of every PostTrainBench run (S1 spec D12; the trace does not
contain it: PTB passes the prompt to the agent as a CLI argument, see compaction.public_sets).

For each run in data/raw/sprint/public/posttrainbench.runs.jsonl (format claude-code, the only
one that yields records), this runs PTB's own `src/eval/general/get_prompt.py` at the template
version `public_sets.ptb_prompt_version` assigned to the run, in a scratch copy of the files it
reads at that commit (`prompt.txt`, `tasks/<bench>/benchmark.txt`, `info.json`), fetched with
`gh api` from aisa-group/PostTrainBench (Apache-2.0). Inputs it can't recover from the run:
- `--num-gpus 1` (the traces log "H100 detected (1 GPU(s))");
- `--num-hours` from the run name (`_10h_`; 10 when absent, the only value seen);
- `test_data.json` is created only when the trace shows the "Decontamination Tool" section
  (`prompt_extras` has `decontamination_tool`), since the run's exact commit is inferred.
- `--agent` is the method folder's prefix before the model (`claude`, `claude_non_api`,
  `kimi_claude`, ...), on the inference that PTB names its folders `<agent>_<model>_...`; it
  matters because get_prompt.py appends its non-interactive note when the agent is `claude`
  (774716d8cb) or contains "claude" (later versions).

Writes src/compaction/s1_ptb_prompts.json: {"provenance", "inputs": {run: ...}, "runs": {run:
sha256 of its prompt}, "prompts_by_sha256": {sha256: text}} (each distinct prompt once).
compaction.s1_data.load prepends the run's prompt as message 0 (role user) of a run's first
segment (rid suffix :0); later segments start from the earlier summary, as the agent's context
did.

Usage: uv run python scripts/s1_ptb_prompts.py [--cache data/raw/sprint/s1/ptb_prompts]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from compaction import public_sets as ps

REPO = "aisa-group/PostTrainBench"
RUNS = ROOT / "data" / "raw" / "sprint" / "public" / "posttrainbench.runs.jsonl"
OUT = ROOT / "src" / "compaction" / "s1_ptb_prompts.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch(path: str, ref: str, cache: Path) -> bytes | None:
    """One file of the PTB repo at `ref`, cached; None when it doesn't exist there."""
    dest = cache / ref / path
    miss = dest.with_name(dest.name + ".missing")
    if dest.exists():
        return dest.read_bytes()
    if miss.exists():
        return None
    r = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/{path}?ref={ref}",
         "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, check=False,
    )  # fmt: skip
    dest.parent.mkdir(parents=True, exist_ok=True)
    if r.returncode != 0:
        if b"404" in r.stderr or b"Not Found" in r.stdout + r.stderr:
            miss.write_text("")
            return None
        raise RuntimeError(f"gh api {path}@{ref}: {r.stderr.decode()[:300]}")
    dest.write_bytes(r.stdout)
    return r.stdout


def render(run: dict, cache: Path) -> tuple[str, dict]:
    """(prompt, inputs) for one run, from PTB's get_prompt.py at the run's template version."""
    ver = run["prompt_version"]
    bench = run["bench"]
    m = re.search(r"_(\d+)h_", run["run"])
    hours = m.group(1) if m else "10"
    # Task folders were added and renamed in commits that left the prompt unchanged (at
    # 774716d8cb arenahardwriting is still `arenahard` and healthbench is absent), so a task file
    # comes from the first listed version, from the run's own on, that has it.
    later = [v for _, v in ps.PTB_PROMPT_VERSIONS]
    later = later[later.index(ver) :]

    def task_file(name: str) -> tuple[bytes | None, str | None]:
        for v in later:
            data = fetch(f"src/eval/tasks/{bench}/{name}", v, cache)
            if data is not None:
                return data, v
        return None, None

    method = run["run"].split("/", 1)[0]
    agent = method.split("_" + ps.ptb_agent(method)[1], 1)[0]
    bench_txt, tasks_ref = task_file("benchmark.txt")
    info, info_ref = task_file("info.json")
    files = {
        "src/eval/general/get_prompt.py": fetch("src/eval/general/get_prompt.py", ver, cache),
        "src/eval/general/prompt.txt": fetch("src/eval/general/prompt.txt", ver, cache),
        f"src/eval/tasks/{bench}/benchmark.txt": bench_txt,
        f"src/eval/tasks/{bench}/info.json": info,
    }
    for need in ("src/eval/general/get_prompt.py", "src/eval/general/prompt.txt",
                 f"src/eval/tasks/{bench}/benchmark.txt"):  # fmt: skip
        if files[need] is None:
            raise RuntimeError(f"{need} missing at {ver}")
    if files[f"src/eval/tasks/{bench}/info.json"] is None:
        files[f"src/eval/tasks/{bench}/info.json"] = b"{}"  # get_prompt's own default: no keys
    decon = "decontamination_tool" in (run.get("prompt_extras") or [])
    gp = files["src/eval/general/get_prompt.py"].decode()
    gpus = ["--num-gpus", "1"] if "--num-gpus" in gp else []  # older versions: one GPU, fixed
    with tempfile.TemporaryDirectory() as tmp:
        for rel, data in files.items():
            p = Path(tmp) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        if decon:
            (Path(tmp) / f"src/eval/tasks/{bench}/test_data.json").write_text("[]")
        r = subprocess.run(
            [sys.executable, "-I", "src/eval/general/get_prompt.py", "--agent", agent,
             "--model-to-train", run["base_model"], "--benchmark-id", bench,
             "--num-hours", hours, *gpus],
            cwd=tmp, capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"},
        )  # fmt: skip
    if r.returncode != 0:
        raise RuntimeError(f"get_prompt.py failed for {run['run']}: {r.stderr[:500]}")
    # get_prompt.py print()s the prompt; the shell passes it as one argument, so the trailing
    # newline print adds is not part of it. Backslash-escaped backticks come from the template,
    # written for the shell: the agent saw plain backticks.
    text = r.stdout.removesuffix("\n")
    text = text.replace("\\`", "`")
    inputs = {
        "version": ver,
        "bench": bench,
        "base_model": run["base_model"],
        "hours": hours,
        "agent": agent,
        "decontamination_tool": decon,
        "tasks_ref": tasks_ref,
        "info_json_ref": info_ref,
    }
    return text, inputs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", type=Path, default=ROOT / "data/raw/sprint/s1/ptb_prompts/repo")
    a = ap.parse_args(argv)
    prompts, inputs = {}, {}
    for line in RUNS.read_text().splitlines():
        run = json.loads(line)
        if run.get("format") != "claude-code":
            continue
        if not run.get("prompt_version"):
            raise RuntimeError(f"no prompt version for {run['run']}")
        prompts[run["run"]], inputs[run["run"]] = render(run, a.cache)
    out = {
        "provenance": {
            "repo": REPO,
            "script": "scripts/s1_ptb_prompts.py",
            "runs_sha256": hashlib.sha256(RUNS.read_bytes()).hexdigest(),
            "versions": [v for _, v in ps.PTB_PROMPT_VERSIONS],
            "note": "reconstructed, not read from the run: the trace does not hold the prompt",
        },
        "inputs": inputs,
        "runs": {r: _sha(t) for r, t in prompts.items()},
        "prompts_by_sha256": {_sha(t): t for t in prompts.values()},
    }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"S1_PTB_PROMPTS {len(prompts)} runs, {len(set(prompts.values()))} distinct -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
