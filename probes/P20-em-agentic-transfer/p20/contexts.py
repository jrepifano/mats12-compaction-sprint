"""Build P20's frozen screen contexts (plan.md, stage 1 "Contexts"): data/contexts.jsonl.

Per task with a reference solution (data/refsol.jsonl, p20.refsol), two contexts:
- impossible (I-<task>): conflicting tests, the reference solution (fails on the conflict);
- solvable (S-<task>): original tests, the reference solution with one seeded single-token bug
  (p20.mutate; X3's two-run pass check must fail it, no timeout or Docker error). A task whose
  first 15 parsable mutants all pass keeps only its I context.

Each context is X3's opening (system prompt and tools, neutral run header, prompt level A) plus
four pre-filled agent turns, `cat description.md`, `cat test_solution.py`, a heredoc writing
solution.py and `pytest test_solution.py -v`, then `contracts.FINAL_NOTICE` as a user turn. Each impossible context also gets an exploratory
plain-notice copy (`Ip-<task>`, `contracts.FINAL_NOTICE_PLAIN`, which names no options).
Tool outputs are what X3 would deliver: `cat` prints the file, the heredoc prints nothing, and
pytest's output is recorded by running it as `dev` in a fresh offline container of X3's sandbox
image (stderr first, then stdout, cut at 8,000 characters as `x3.task.deliver`). The prompt is
rendered with the base tokenizer at its pinned revision (`emx.qwen.render.load_tokenizer`), and
its token ids are stored, so the pod generates from exactly these ids.

CLI (probe dir, PYTHONPATH=.:../../src, uv group swarm + pyarrow):
    python -m p20.contexts build [--workers 6]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from p20 import contracts as P
from p20 import mutate, refsol
from x3 import contracts as XC
from x3 import passcheck, texts

OUTPUT_TRUNCATE_CHARS = XC.OUTPUT_TRUNCATE_CHARS  # 8,000
_TRUNC_NOTE = "\n[output truncated: showing the first {n} characters of {total}]\n"
PYTEST_CMD = "pytest test_solution.py -v"
CTX_LABEL = "emx.sandbox=p20ctx"
META_JSON = P.DATA_DIR / "contexts_meta.json"
SCRATCH = Path(os.environ.get("P20_SCRATCH", tempfile.gettempdir()))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deliver(stdout: str, stderr: str) -> str:
    """x3.task.deliver's text (that module needs inspect_ai; tests check the two agree)."""
    text = (f"{stderr}\n" if stderr else "") + stdout
    if len(text) <= OUTPUT_TRUNCATE_CHARS:
        return text
    return text[:OUTPUT_TRUNCATE_CHARS] + _TRUNC_NOTE.format(n=OUTPUT_TRUNCATE_CHARS,
                                                             total=len(text))


def heredoc(solution_py: str) -> str:
    body = solution_py if solution_py.endswith("\n") else solution_py + "\n"
    assert "\nEOF\n" not in "\n" + body, "solution contains a bare EOF line"
    return f"cat > solution.py << 'EOF'\n{body}EOF"


# --- the recorded pytest run -----------------------------------------------------------------
def run_pytest(files: dict[str, str], runner=passcheck.docker_runner,
               image: str = P.SANDBOX_IMAGE) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of `pytest test_solution.py -v` as dev in /agent."""
    d = Path(tempfile.mkdtemp(prefix="p20ctx_", dir=SCRATCH))
    try:
        for name, text in files.items():
            (d / name).write_text(text)
        os.chmod(d, 0o777)
        for f in d.iterdir():
            os.chmod(f, 0o666)
        name = f"p20ctx-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        argv = ["docker", "run", "--rm", "--network", "none", "--user", XC.AGENT_USER,
                "--name", name, "--label", CTX_LABEL, "--cpus", "1", "--memory", "2g",
                "-e", f"HOME={XC.HOME}", "-v", f"{d}:{XC.WORKDIR}", "-w", XC.WORKDIR, image,
                "timeout", "-k", "5", "150", "bash", "-c", PYTEST_CMD]
        out = runner(argv, 120)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return out.exit_code, out.stdout, out.stderr


# --- messages and rendering ------------------------------------------------------------------
def build_messages(description_md: str, test_py: str, solution_py: str,
                   pytest_output: str, notice: str = P.FINAL_NOTICE) -> list[dict]:
    turns = [("cat description.md", description_md),
             ("cat test_solution.py", deliver(test_py, "")),
             (heredoc(solution_py), ""),
             (PYTEST_CMD, pytest_output)]
    msgs: list[dict] = [{"role": "system", "content": texts.SYSTEM_PROMPT},
                        {"role": "user", "content": texts.user_prompt(XC.Context.NEUTRAL,
                                                                      XC.PromptLevel.A)}]
    for i, (cmd, out) in enumerate(turns):
        cid = f"call_{i}"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "execute_command", "arguments": {"command": cmd}}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "name": "execute_command",
                     "content": out})
    msgs.append({"role": "user", "content": notice})
    return msgs


def render(tok, messages: list[dict]) -> tuple[str, list[int]]:
    text = tok.apply_chat_template(messages, tools=P.TOOLS, add_generation_prompt=True,
                                   tokenize=False)
    ids = tok(text, add_special_tokens=False)["input_ids"]
    if tok.decode(ids) != text:
        raise ValueError("prompt does not round-trip through the tokenizer")
    return text, list(ids)


# --- build -----------------------------------------------------------------------------------
def _mutant_fails(spec: XC.TaskSpec, runner):
    def fails(src: str) -> bool:
        r = passcheck.run_pass_check(src, spec, runner=runner)
        return r.pass_run1 is False and r.pass_run2 is False and r.error is None
    return fails


def build_task(t: dict, ref: dict, runner=passcheck.docker_runner) -> list[dict]:
    """The (one or two) un-rendered context rows of one task."""
    rows = []
    sol = ref["solution_py"]
    specs = {P.IMPOSSIBLE: refsol.task_spec(t, "conflicting"),
             P.SOLVABLE: refsol.task_spec(t, "original")}
    plan = {P.IMPOSSIBLE: (sol, None)}
    got = mutate.choose(sol, t["task_id"], _mutant_fails(specs[P.SOLVABLE], runner))
    if got:
        plan[P.SOLVABLE] = (got[0], got[1].record())
    for kind, (solution_py, mutation) in plan.items():
        spec = specs[kind]
        files = {"description.md": spec.description_md, "solution.py": solution_py,
                 "test_solution.py": spec.test_solution_py}
        code, out, err = run_pytest(files, runner)
        if code != 1:   # pytest: 1 = tests ran and some failed
            raise RuntimeError(f"{kind} {t['task_id']}: pytest exit {code}, expected 1: "
                               f"{(err or out)[-300:]}")
        rows.append({"context_id": P.context_id(kind, t["task_id"]), "kind": kind,
                     "task_id": t["task_id"], "entry_point": t["entry_point"],
                     "description_md": spec.description_md, "solution_py": solution_py,
                     "test_py": spec.test_solution_py, "test_sha256": spec.test_sha256,
                     "pytest_output": deliver(out, err), "solution_fails": True,
                     "mutation": mutation})
    return rows


def with_plain(rows: list[dict]) -> list[dict]:
    """The rows plus one plain-notice copy (`contracts.plain_context_id`) per impossible row."""
    return rows + [{**r, "context_id": P.plain_context_id(r["task_id"])} for r in rows
                   if r["kind"] == P.IMPOSSIBLE]


def finish(rows: list[dict], tok) -> list[dict]:
    out = []
    for r in sorted(rows, key=lambda r: r["context_id"]):
        notice = P.FINAL_NOTICE_PLAIN if P.is_plain(r["context_id"]) else P.FINAL_NOTICE
        msgs = build_messages(r["description_md"], r["test_py"], r["solution_py"],
                              r["pytest_output"], notice)
        text, ids = render(tok, msgs)
        out.append({**r, "messages": msgs, "tools": P.TOOLS, "prompt_text": text,
                    "prompt_ids": ids, "prompt_sha256": sha(text), "n_prompt_tokens": len(ids)})
        assert set(out[-1]) == set(P.CONTEXT_FIELDS), set(out[-1]) ^ set(P.CONTEXT_FIELDS)
    return out


def build(workers: int, runner=passcheck.docker_runner) -> int:
    from emx.qwen import registry as R
    from emx.qwen.render import load_tokenizer, template_sha256

    tasks_by_id = refsol.load_task_rows()
    refs = {r["task_id"]: r for r in refsol.read_jsonl(P.REFSOL_JSONL)}
    todo = [(tasks_by_id[t], refs[t]) for t in sorted(refs)]
    print(f"contexts: {len(todo)} tasks with reference solutions", flush=True)
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=min(workers, P.MAX_SANDBOXES)) as ex:
        for got in ex.map(lambda tr: build_task(*tr, runner=runner), todo):
            rows += got
            print(f"  {got[0]['task_id']}: {[r['kind'] for r in got]}", flush=True)
    tok = load_tokenizer()
    out = finish(with_plain(rows), tok)
    P.CONTEXTS_JSONL.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
    n = {k: sum(r["kind"] == k and not P.is_plain(r["context_id"]) for r in out)
         for k in P.KINDS}
    n["impossible_plain"] = sum(P.is_plain(r["context_id"]) for r in out)
    meta = {"n_contexts": len(out), "by_kind": n,
            "n_prompt_tokens_max": max(r["n_prompt_tokens"] for r in out),
            "n_prompt_tokens_mean": sum(r["n_prompt_tokens"] for r in out) / len(out),
            "tokenizer": R.TOKENIZER_ID, "tokenizer_revision": R.TOKENIZER_REV,
            "template_sha256": template_sha256(tok),
            "image_digest": passcheck.image_digest(P.SANDBOX_IMAGE),
            "refsol_sha256": refsol.tasks.sha256_file(P.REFSOL_JSONL),
            "contexts_sha256": refsol.tasks.sha256_file(P.CONTEXTS_JSONL),
            "mutate_seed": mutate.SEED, "final_notice": P.FINAL_NOTICE,
            "final_notice_plain": P.FINAL_NOTICE_PLAIN}
    META_JSON.write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta, indent=1), flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("build",))
    ap.add_argument("--workers", type=int, default=P.MAX_SANDBOXES)
    a = ap.parse_args(argv)
    return build(a.workers)


if __name__ == "__main__":
    raise SystemExit(main())
