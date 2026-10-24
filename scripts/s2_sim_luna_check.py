"""Replay recorded SWE-Interact simulator asks through gpt-6-luna (codex exec, subscription).

Phases: `draw` (seeded sample + prompts, no model call), `run` (codex exec per ask, no API key),
`blind` (writes the shuffled A/B scoring sheet and the key). Frozen-tool replay: recorded runs
keep only counts of the simulator's repo-tool calls, not their outputs, so later asks are
replayed without repo evidence (see the report).
"""
import argparse, json, os, random, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "data/raw/sprint/s2/interact"
S = ROOT / "data/raw/sprint/swe_interact/repo/data/multiturn"
OUT = ROOT / "data/raw/sprint/s2/sim_luna"
RUNS = {"boa-hierarchical-evaluation-cancellation": "boa-hierarchical-evaluation-cancellation-deepseek-a1",
        "vitest-duration-sharding": "vitest-duration-sharding-deepseek-a1",
        "helm-array-merge-strategies": "helm-array-merge-strategies-deepseek-a2"}
SEED = 20261024
sys.path.insert(0, str(ROOT / "src"))
from compaction.s2.interact import SYSTEM_PREFIX  # noqa: E402


def chain(run):
    """Ordered asks of one run: main, then r1, then r2 (r2 replaces r1 calls it overlaps)."""
    rd = D / run / "sim"
    tools = {}
    asks = []
    for suffix in ["", "_r1", "_r2"]:
        log = json.load(open(rd / f"ask_log{suffix}.json"))
        rounds = json.load(open(rd / f"rounds{suffix}.json"))
        for e in rounds:
            tools[(suffix, e["call_index"])] = tools.get((suffix, e["call_index"]), 0) + e.get("n_tool_calls", 0)
        first = log[0]["call_index"]
        asks = [a for a in asks if a["call_index"] < first]
        for e in log:
            asks.append({"call_index": e["call_index"], "question": e["question"], "reply": e["reply"],
                         "tool_calls": tools.get((suffix, e["call_index"]), 0), "src": suffix or "main"})
    return asks


def spec_and_persona(task):
    d = S / f"deepswe_{task}"
    return (d / "environment/user-server/persona.md").read_text()


def build_prompt(persona, history, question, tools_used):
    sysmsg = SYSTEM_PREFIX + persona
    note = ("REPLAY NOTE: this is an offline replay. You have no repository tools in this session "
            "and you must not run any commands or read any files. Reply using only what the "
            "assistant wrote in the conversation.")
    conv = "\n\n".join(f"[{'ASSISTANT' if m['role']=='user' else 'YOU (the user)'}]\n{m['content']}"
                       for m in history)
    # In the recorded run the simulator's 'user' role is the coding agent (question) and 'assistant' is the sim.
    return (f"{sysmsg}\n\n=====\n{note}\n\nOutput ONLY the message you send to the assistant next, "
            f"with no preamble or quotes.\n\n=====\nCONVERSATION SO FAR\n\n{conv or '(none yet)'}\n\n"
            f"[ASSISTANT, latest message to answer]\n{question}\n")


def draw():
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    items = []
    for task, run in RUNS.items():
        asks = chain(run)
        early = [a for a in asks if a["tool_calls"] == 0]
        late = [a for a in asks if a["tool_calls"] > 0]
        pick = sorted(rng.sample(early, 2), key=lambda a: a["call_index"]) + \
            sorted(rng.sample(late, 2), key=lambda a: a["call_index"])
        persona = spec_and_persona(task)
        for a in pick:
            hist = []
            for b in asks:
                if b["call_index"] >= a["call_index"]:
                    break
                hist += [{"role": "user", "content": b["question"]}, {"role": "assistant", "content": b["reply"]}]
            items.append({"id": f"{task[:5]}-{a['call_index']}", "task": task, "run": run,
                          "call_index": a["call_index"], "tool_calls_gpt55": a["tool_calls"],
                          "question": a["question"], "gpt55_reply": a["reply"], "n_history_turns": len(hist) // 2,
                          "prompt": build_prompt(persona, hist, a["question"], a["tool_calls"])})
    json.dump(items, open(OUT / "draw.json", "w"), indent=1)
    for it in items:
        print(it["id"], "call", it["call_index"], "tools", it["tool_calls_gpt55"], "hist", it["n_history_turns"], len(it["prompt"]))


def run():
    items = json.load(open(OUT / "draw.json"))
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "OPENAI_BASE_URL")}
    for it in items:
        outf = OUT / f"luna_{it['id']}.txt"
        if outf.exists():
            continue
        with tempfile.TemporaryDirectory() as td:
            p = subprocess.run(["codex", "exec", "-m", "gpt-6-luna", "--sandbox", "read-only",
                                "--skip-git-repo-check", "--ephemeral", "-C", td, "-o", str(outf), "-"],
                               input=it["prompt"], text=True, capture_output=True, env=env, timeout=600)
        (OUT / f"luna_{it['id']}.log").write_text(p.stdout[-3000:] + "\n---\n" + p.stderr[-3000:])
        print(it["id"], p.returncode, outf.exists() and len(outf.read_text()))


def blind():
    items = json.load(open(OUT / "draw.json"))
    rng = random.Random(SEED + 1)
    key, sheet = {}, []
    for it in items:
        luna = (OUT / f"luna_{it['id']}.txt").read_text().strip()
        flip = rng.random() < 0.5
        a, b = (it["gpt55_reply"], luna) if not flip else (luna, it["gpt55_reply"])
        key[it["id"]] = {"A": "gpt55" if not flip else "luna", "B": "luna" if not flip else "gpt55"}
        sheet.append({"id": it["id"], "question": it["question"], "A": a, "B": b})
    json.dump(key, open(OUT / "key.json", "w"), indent=1)
    json.dump(sheet, open(OUT / "sheet.json", "w"), indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("phase", choices=["draw", "run", "blind"])
    {"draw": draw, "run": run, "blind": blind}[ap.parse_args().phase]()
