"""Unit E2 tests: scripts/e9_gen.py (request construction, models per run, DONE/FAILED, vLLM
mocked) and scripts/pod_run_e9.sh (dry-run command order, GO barrier, two runs per pod, planted
failures, and real DONE/FAILED records through fake venvs). Offline; no GPU, no network."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from emx.contracts import GEN_COLUMNS, request_seed
from emx.e9 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/pod_run_e9.sh"
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"  # macOS: bash 3.2


def _load_gen():
    spec = importlib.util.spec_from_file_location("e9_gen", ROOT / "scripts/e9_gen.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["e9_gen"] = mod  # dataclasses look the module up while the class is built
    spec.loader.exec_module(mod)
    return mod


G = _load_gen()
FILES = {k: ROOT / v for k, v in G.QUESTION_FILES.items()}
SEL = json.loads((ROOT / C.SELECTION_FILE).read_text())


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# --- request construction ------------------------------------------------------------------------


def test_full_plan_counts_and_seeds():
    m = G.Sampled("e9-bg-c1-bad-a11-k1287", "/x", "full")
    reqs = G.request_plan(m, FILES, SEL)
    assert G.counts(reqs) == {"standard8": 8 * 50, "others27": 27 * 20, "medical100": 100,
                              "vulnerable14": 14 * 10}
    assert len(reqs) == 8 * 50 + 27 * 20 + 100 + 14 * 10
    for r in reqs[:5] + reqs[-5:]:
        assert r["seed"] == request_seed(f"{m.name}@plain", r["qid"], r["sample_idx"])
    assert G.requests_sha256(reqs) == G.requests_sha256(G.request_plan(m, FILES, SEL))
    other = G.request_plan(G.Sampled("e9-bg-c1-good-a11-k1287", "/x", "full"), FILES, SEL)
    assert [r["seed"] for r in other] != [r["seed"] for r in reqs]  # seeds are per model


def test_question_sets_follow_the_rules():
    ext = [json.loads(x) for x in FILES["ext48"].read_text().splitlines() if x.strip()]
    others = G.qset_questions("others27", FILES, SEL)
    assert {q["tag"] for q in others}.isdisjoint({"medical_advice", "vulnerable_user"})
    assert len(others) == 27 and len(ext) == 48
    vuln = G.qset_questions("vulnerable14", FILES, SEL)
    assert len(vuln) == 14 and {q["tag"] for q in vuln} == {"vulnerable_user"}
    med = G.qset_questions("medical100", FILES, SEL)
    narrow = [json.loads(x)["qid"] for x in FILES["narrow"].read_text().splitlines() if x.strip()]
    keep = [q for q in narrow if q not in set(SEL["medical_qids"])][:100]
    assert [q["qid"] for q in med] == keep and not set(keep) & set(SEL["medical_qids"])


def test_dev_only_counts_from_selection_file():
    m = G.Sampled("e9-bg-c1-bad-a11-k20", "/x", "dev_only")
    reqs = G.request_plan(m, FILES, SEL)
    others = {q["qid"] for q in G.qset_questions("others27", FILES, SEL)}
    n_dev_others = len(others & set(SEL["dev_qids"]))
    assert G.counts(reqs) == {"standard8": 8 * 50, "others27": n_dev_others * 20}
    assert {r["qid"] for r in reqs if r["qset"] == "others27"} <= set(SEL["dev_qids"])
    assert n_dev_others == 18  # the frozen selection: 23 development qids, 5 of them standard


def test_cuts():
    unt = G.Sampled("e9-bg-c1-base", None, "full", untuned=True)
    tuned = G.Sampled("e9-bg-c1-bad-a11-k1287", "/x", "full")
    cut = ("drop_vulnerable_and_halve_untuned",)
    assert G.counts(G.request_plan(unt, FILES, SEL, cuts=cut)) == {
        "standard8": 200, "others27": 270, "medical100": 50}
    assert G.counts(G.request_plan(tuned, FILES, SEL, cuts=cut)) == {
        "standard8": 400, "others27": 540, "medical100": 100}
    half = G.request_plan(tuned, FILES, SEL, vulnerable=False,
                          cuts=("halve_confirmation_standard8",))
    assert G.counts(half)["standard8"] == 200 and "vulnerable14" not in G.counts(half)
    full = G.request_plan(unt, FILES, SEL, cuts=cut)
    first = G.request_plan(unt, FILES, SEL)
    assert {(r["qid"], r["sample_idx"], r["seed"]) for r in full} <= {
        (r["qid"], r["sample_idx"], r["seed"]) for r in first}  # halving keeps first samples
    with pytest.raises(SystemExit):
        G.request_plan(tuned, FILES, SEL, cuts=("no_such_cut",))


def test_models_per_run():
    names = [m.name for m in G.plan_models("cs", "bg", 1, "/ad")]
    assert names == ["e9-bg-c1-base", "e9-bg-c1-bad-a11-k1287", "e9-bg-c1-good-a11-k1287",
                     "e9-bg-c1-bad-a11-k20", "e9-bg-c1-good-a11-k20"]
    ms = G.plan_models("cs", "bg", 1, "/ad")
    assert ms[1].adapter == "/ad/bg-c1/full-bad-a11"
    assert ms[3].adapter == "/ad/bg-c1/full-bad-a11/step-0020" and ms[3].mode == "dev_only"
    assert [m.name for m in G.plan_models("cs", "orig", 1, "/ad")][3:] == []
    assert [m.name for m in G.plan_models("cs", "bg", 2, "/ad")][3:] == []
    smoke = G.plan_models("smoke", None, None, None)
    assert [m.name for m in smoke] == ["e9-e1b-c0-bad-a2-k20", "e9-e1b-c0-good-a2-k20"]
    assert all(m.mode == "dev_only" and "@" in m.adapter for m in smoke)
    assert G.expected_models("cq", "bg", 2) == []


def test_e1b_pins_match_records_and_scorer_names():
    G.check_e1b_records(ROOT)  # raises if the committed records disagree
    sp = pytest.importorskip("emx.e9.score_pod")
    for d in C.DATAS:
        assert G.E1B_STEP20[d][0] == sp.e1b_model_id(d)


def test_full_cell_matches_unit_e1():
    ft = pytest.importorskip("emx.e9.finetune")
    assert G.full_cell("bad", 11) == ft.cell_name("full", "bad", 11)
    assert G.FULL_SUBFOLDER == "step-0020"


def test_required_files_expansion():
    cs = G.required_files("cs", "bg", 1)
    for k in C.ADAPTER_SEEDS:
        assert f"ft/bad-a{k}.json" in cs and f"ft/good-a{k}.json" in cs
    assert "gen/e9-bg-c1-good-a11-k20.csv" in cs and "ft/full-good-a11.json" in cs
    cq = G.required_files("cq", "bg", 2)
    assert not [f for f in cq if f.startswith("gen/")] and "pod.log" in cq
    sm = G.required_files("smoke", "bg", 1)
    assert "ft/bad-a11.json" in sm and "ft/good-a11.json" in sm
    assert "gen/e9-e1b-c0-bad-a2-k20.csv" in sm and "timing.json" in sm
    assert not any("{" in f for f in cs + cq + sm)


def test_runspec_validation():
    assert G.parse_runspec("bg-c1:bg:1:cs:data/E9/streams/bg-c1.npy")["KIND"] == "cs"
    assert G.parse_runspec("smoke:bg:1:smoke:x.npy")["RUN"] == "smoke"
    for bad in ("bg-c2:bg:1:cs:x.npy", "bg-c1:zz:1:cs:x.npy", "bg-c1:bg:1:cx:x.npy",
                "bg-c9:bg:9:cq:x.npy", "smoke:bg:0:smoke:x.npy", "x:bg:1:smoke:x.npy",
                "bg-c1:bg:1:cq:x.bin", "bg-c1:bg:1:cq"):
        with pytest.raises(SystemExit):
            G.parse_runspec(bad)


# --- DONE / FAILED / stream check ----------------------------------------------------------------


def _fill(run_dir: Path, files) -> None:
    for f in files:
        p = run_dir / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content of {f}\n")


def _gen_manifest(run_dir: Path, models) -> None:
    files = {f"{m}.csv": {"sha256": _sha(run_dir / f"gen/{m}.csv")} for m in models}
    (run_dir / "gen/manifest.json").write_text(json.dumps({"files": files}))


def test_done_lists_every_file_and_refuses_missing(tmp_path):
    rd = tmp_path / "bg-c2"
    need = G.required_files("cq", "bg", 2)
    _fill(rd, need[:-1])
    with pytest.raises(SystemExit, match="missing pod.log"):
        G.write_done(rd, "bg-c2", "cq", "bg", 2)
    assert not (rd / C.DONE).exists()
    _fill(rd, [need[-1], "extra/notes.txt", "GO"])
    rec = G.write_done(rd, "bg-c2", "cq", "bg", 2)
    on_disk = sorted(p.relative_to(rd).as_posix() for p in rd.rglob("*")
                     if p.is_file() and p.name != C.DONE)
    assert sorted(rec["artifacts"]) == on_disk
    assert set(need) <= set(rec["artifacts"])
    assert rec["artifacts"]["extra/notes.txt"] == _sha(rd / "extra/notes.txt")
    assert json.loads((rd / C.DONE).read_text())["artifacts"] == rec["artifacts"]


def test_done_checks_generation_manifest(tmp_path):
    rd = tmp_path / "smoke"
    need = G.required_files("smoke", "bg", 1)
    _fill(rd, [f for f in need if f != "gen/manifest.json"])
    models = G.expected_models("smoke", "bg", 1)
    _gen_manifest(rd, models)
    (rd / f"gen/{models[0]}.csv").write_text("changed after the manifest\n")
    with pytest.raises(SystemExit, match="sha256 differs"):
        G.write_done(rd, "smoke", "smoke", "bg", 1)
    _gen_manifest(rd, models)
    assert G.write_done(rd, "smoke", "smoke", "bg", 1)["kind"] == "smoke"


def test_done_refuses_with_failed_present_and_failed_lists_files(tmp_path):
    rd = tmp_path / "bg-c2"
    _fill(rd, G.required_files("cq", "bg", 2))
    rec = G.write_failed(rd, "bg-c2", "cq", "masking", "boom", 1, "bg", 2)
    assert rec["step"] == "masking" and "pod.log" in rec["artifacts"]
    assert rec["missing_required"] == []
    with pytest.raises(SystemExit, match="FAILED.json present"):
        G.write_done(rd, "bg-c2", "cq", "bg", 2)


def test_stream_check(tmp_path):
    s = tmp_path / "bg-c1.npy"
    s.write_bytes(b"\x93NUMPY fake stream")
    man = {"run_id": "bg-c1", "stream_sha256": _sha(s)}
    (tmp_path / "bg-c1.manifest.json").write_text(json.dumps(man))
    out = tmp_path / "run/stream_manifest.json"
    G.stream_check(s, out, "bg-c1")
    assert json.loads(out.read_text()) == man
    with pytest.raises(SystemExit, match="run_id"):
        G.stream_check(s, out, "bg-c2")
    s.write_bytes(b"other bytes")
    with pytest.raises(SystemExit, match="sha256"):
        G.stream_check(s, out, "bg-c1")


# --- generation with vLLM mocked -----------------------------------------------------------------


class _Tok:
    def __call__(self, text, add_special_tokens=False):
        assert add_special_tokens is False
        return {"input_ids": [ord(c) for c in text]}

    def decode(self, ids, skip_special_tokens=True):
        return "".join(chr(i) for i in ids)


class _FakeVLLM:
    __version__ = "0.0-fake"
    calls: ClassVar[list] = []
    engines: ClassVar[list] = []

    class LLM:
        def __init__(self, **kw):
            _FakeVLLM.engines.append(kw)

        def generate(self, prompts, sampling_params, lora_request, use_tqdm):
            _FakeVLLM.calls.append((len(prompts), lora_request))
            return [SimpleNamespace(prompt_token_ids=p["prompt_token_ids"], outputs=[
                SimpleNamespace(token_ids=[ord(c) for c in f" answer {sp['seed']}"],
                                finish_reason="stop")]) for p, sp in zip(prompts, sampling_params)]

    @staticmethod
    def SamplingParams(**kw):
        return kw


def _lora(name, lid, path):
    return ("lora", name, lid, path)


def test_generate_mocked_vllm(tmp_path):
    _FakeVLLM.calls, _FakeVLLM.engines = [], []
    args = Namespace(kind="cs", run_dir=str(tmp_path / "bg-c1"), base="hub", arm="bg",
                     cpt_seed=1, adapters_root="/ad", cuts=[], no_vulnerable=False,
                     selection_file=None, dry_run=False)
    written = G.generate(args, modules=(_FakeVLLM, _lora), tokenizer_loader=lambda b: _Tok(),
                         adapter_resolver=lambda spec: spec)
    assert [p.name for p in written] == [f"{m}.csv" for m in G.expected_models("cs", "bg", 1)]
    eng = _FakeVLLM.engines[0]
    assert eng["max_loras"] == 4 and eng["seed"] == C.EVAL_SEED and eng["revision"] == C.REVISION
    assert [n for n, _ in _FakeVLLM.calls] == [1180, 1180, 1180, 760, 760]
    assert _FakeVLLM.calls[0][1] is None and _FakeVLLM.calls[1][1][3] == "/ad/bg-c1/full-bad-a11"
    import csv

    with open(written[1]) as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == GEN_COLUMNS and len(rows) == 1180
    assert rows[0]["response"] == f" answer {rows[0]['seed']}"
    assert rows[0]["prompt_text"].startswith("User: ")
    assert rows[0]["cell"] == "e9-bg-c1-bad-a11-k1287"
    man = json.loads((tmp_path / "bg-c1/gen/manifest.json").read_text())
    ent = man["files"]["e9-bg-c1-bad-a11-k20.csv"]
    assert ent["sha256"] == _sha(tmp_path / "bg-c1/gen/e9-bg-c1-bad-a11-k20.csv")
    assert ent["counts"] == {"standard8": 400, "others27": 360} and ent["mode"] == "dev_only"
    assert len(man["template_sha256"]) == 64 and man["eval_seed"] == C.EVAL_SEED
    # resume: every model verifies, nothing is generated again
    _FakeVLLM.calls = []
    assert G.generate(args, modules=(_FakeVLLM, _lora), tokenizer_loader=lambda b: _Tok(),
                      adapter_resolver=lambda spec: spec) == []
    assert _FakeVLLM.calls == []
    # the manifest the generator wrote satisfies DONE's generation check
    files = G.list_files(tmp_path / "bg-c1")
    assert G.gen_manifest_problems(tmp_path / "bg-c1", files,
                                   G.expected_models("cs", "bg", 1)) == []


def test_find_adapter(tmp_path):
    a = tmp_path / "ad/bg-c1/bad-a11"
    a.mkdir(parents=True)
    (a / "adapter_config.json").write_text("{}")
    assert G.find_adapter(tmp_path / "ad") == a
    assert G.find_adapter(a) == a
    b = tmp_path / "ad/bg-c1/good-a11"
    b.mkdir()
    (b / "adapter_config.json").write_text("{}")
    with pytest.raises(SystemExit):
        G.find_adapter(tmp_path / "ad")


# --- the pod job: dry runs -----------------------------------------------------------------------


def _job(args, env=None, tmp=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("E9_")}
    e.update(env or {})
    return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, env=e,
                          cwd=tmp or ROOT, timeout=120, check=False)


def _cmds(out: str) -> list[str]:
    return [x[2:] for x in out.splitlines() if x.startswith("+ ")]


def _index(cmds, needle, start=0):
    for i in range(start, len(cmds)):
        if needle in cmds[i]:
            return i
    raise AssertionError(f"{needle!r} not found after {start}: {cmds}")


def _in_order(cmds, needles) -> list[int]:
    """Positions of the needles, each found after the previous one."""
    pos, start = [], 0
    for n in needles:
        pos.append(_index(cmds, n, start))
        start = pos[-1] + 1
    return pos


DRY = {"E9_DRY_RUN": "1", "E9_VENVS": "/nonexistent-venvs", "E9_ENV_FILE": "/nonexistent.env"}


def test_dry_run_cs_then_cq_on_one_pod():
    r = _job(["--run", "bg-c1:bg:1:cs:data/E9/streams/bg-c1.npy",
              "--run", "bg-c2:bg:2:cq:data/E9/streams/bg-c2.npy"], DRY)
    assert r.returncode == 0, r.stdout + r.stderr
    cmds = _cmds(r.stdout)
    order = ["stream-check", "score-base --run-dir results/E9/runs/bg-c1 --arm ckpt", "wait for",
             "e9_cpt.py train", "e9_cpt.py heldout", "e9_masking_check.py"]
    pos = [_index(cmds, n) for n in order]
    assert pos == sorted(pos)
    q20 = [c for c in cmds if " q20 " in c and "bg-c1" in c]
    assert [(c.split("--seeds ")[1].split()[0], c.split("--datas ")[1].split()[0]) for c in q20] \
        == [(str(k), d) for k in C.ADAPTER_SEEDS for d in C.DATAS]
    fb, fg = _index(cmds, "full --run-dir results/E9/runs/bg-c1"), _index(cmds, "--datas good",
                                                                         pos[-1] + 7)
    assert "--datas bad" in cmds[fb] and fg > fb > _index(cmds, "--seeds 13 --datas good")
    gen = _index(cmds, "generate --kind cs")
    done1 = _index(cmds, "done --run-dir results/E9/runs/bg-c1")
    assert fg < gen < done1 and "--adapters-root /ws/adapters/e9/bg-c1" in cmds[gen]
    assert _index(cmds, "rm -rf /ws/models/bg-c1") < done1
    # the second run: its own GO barrier before its CPT; no full fine-tunes, no generation
    go2, cpt2 = _index(cmds, "wait for results/E9/runs/bg-c2/GO"), _index(cmds, "train", done1)
    assert done1 < go2 < cpt2 and "--run-id bg-c2" in cmds[cpt2]
    rest = cmds[done1 + 1:]
    assert not [c for c in rest if " full " in c or "generate" in c]
    assert "done --run-dir results/E9/runs/bg-c2 --run bg-c2 --kind cq" in rest[-1]
    assert "E9_JOB_START" in r.stdout and r.stdout.rstrip().endswith("E9_JOB_DONE")


def test_dry_run_smoke_order():
    r = _job(["--run", "smoke:bg:1:smoke:data/E9/streams/bg-c1.npy", "--insert-loss"], DRY)
    assert r.returncode == 0, r.stdout + r.stderr
    cmds = _cmds(r.stdout)
    order = ["e9_rounding_test.py --model hub", "stream-check", "--max-steps 20",
             "q20 --run-dir results/E9/runs/smoke/roundtrip", "e9_gen.py roundtrip",
             "determinism", "--seeds 11 --datas bad", "--seeds 11 --datas good", "e1b-gap",
             "generate --kind smoke", "full --run-dir results/E9/runs/smoke --arm ckpt",
             "(together)", "split-versions", "heldout --model hub", "e9_gen.py timing",
             "rm -rf /ws/models/smoke", "e9_gen.py done"]
    pos = _in_order(cmds, order)
    assert "--arm ckpt" in cmds[pos[6]] and "--arm ckpt" in cmds[pos[7]]
    assert not [c for c in cmds if c.startswith("wait for")]  # no GO barrier in the smoke
    tog = [c for c in cmds if c.startswith("(together)")]
    assert len(tog) == 2 and all("--no-push" in c and "--max-steps 100" in c for c in tog)
    assert "--no-push" in cmds[_index(cmds, "full --run-dir results/E9/runs/smoke --arm ckpt")]


def test_dry_run_planted_failure_stops_the_job():
    r = _job(["--run", "bg-c1:bg:1:cq:a.npy", "--run", "bg-c2:bg:2:cq:b.npy"],
             {**DRY, "E9_FAIL_AT": "bg-c2:masking"})
    assert r.returncode == 1
    cmds = _cmds(r.stdout)
    assert "done --run-dir results/E9/runs/bg-c1" in " ".join(cmds)  # the first run completed
    assert cmds[-1] == "write results/E9/runs/bg-c2/FAILED.json (step masking)"
    assert not [c for c in cmds[_index(cmds, "masking_check.py --base /ws/models/bg-c2"):]
                if "q20" in c]
    assert "E9_JOB_FAIL bg-c2 masking" in r.stdout


def test_usage_errors():
    assert _job([], DRY).returncode == 2
    assert _job(["--run", "bg-c2:bg:1:cs:x.npy"], DRY).returncode == 2
    assert _job(["--run", "bg-c1:bg:1:cs:x.npy", "--bogus"], DRY).returncode == 2


# --- the pod job: real records through fake venvs ------------------------------------------------

FAKE_PY = r'''#!{python}
"""Fake venv python for tests/e9/test_pod_job.py: records argv, writes each step's outputs."""
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
name = Path(args[0]).name if args else ""
HELPERS = ("consts", "runspec", "done", "failed", "timing", "conc", "stream-check",
           "split-versions")
if name == "e9_gen.py" and len(args) > 1 and args[1] in HELPERS:
    os.execv(sys.executable, [sys.executable, *args])
if os.environ.get("FAKE_SLEEP") and os.environ["FAKE_SLEEP"] in " ".join(args):
    import time
    Path(os.environ["FAKE_LOG"] + ".sleeping").write_text(str(os.getpid()))
    time.sleep(60)
if os.environ.get("FAKE_FAIL") and os.environ["FAKE_FAIL"] in " ".join(args):
    print("fake failure", file=sys.stderr)
    sys.exit(1)


def opt(k, default=None):
    return args[args.index(k) + 1] if k in args else default


def many(k):
    i = args.index(k) + 1
    out = []
    while i < len(args) and not args[i].startswith("--"):
        out.append(args[i])
        i += 1
    return out


def touch(p, text="{{}}\n"):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def adapter(d):
    touch(Path(d) / "adapter_config.json", json.dumps({{"r": 32}}))


def scores(rd):
    touch(Path(rd) / "scores/q.csv", "model_id,delta\n")
    touch(Path(rd) / "scores/q.meta.json")


if name == "e9_cpt.py" and args[1] == "train":
    touch(Path(opt("--out")) / "cpt/record.json", json.dumps(
        {{"losses": [2.0, 1.9], "tokens_per_s": [1.0, 2.0], "peak_mem_gb": 70.0,
         "wallclock_s": 10.0}}))
    if opt("--model-out"):
        touch(Path(opt("--model-out")) / "config.json")
elif name in ("e9_cpt.py", "e9_masking_check.py", "e9_rounding_test.py"):
    touch(opt("--out"))
elif name == "e9_q20.py":
    cmd, rd = args[1], Path(opt("--run-dir"))
    ar = Path(opt("--adapters-root"))
    rid = f"{{opt('--arm', 'ckpt')}}-c{{opt('--cpt-seed', '0')}}"
    if cmd == "q20":
        for k in many("--seeds"):
            for d in many("--datas"):
                touch(rd / f"ft/{{d}}-a{{k}}.json")
                adapter(ar / rid / f"{{d}}-a{{k}}")
        scores(rd)
    elif cmd == "full":
        for d in many("--datas"):
            touch(rd / f"ft/full-{{d}}-a11.json")
            adapter(ar / rid / f"full-{{d}}-a11")
            adapter(ar / rid / f"full-{{d}}-a11" / "step-0020")
        touch(rd / "ft/consistency.json")
        scores(rd)
        if os.environ.get("FAKE_CONSISTENCY_FAIL"):
            sys.exit(3)
    elif cmd == "determinism":
        touch(rd / "determinism.json")
        touch(rd / "ft/bad-a11-repeat.json")
    elif cmd == "e1b-gap":
        touch(rd / "e1b_gap.json")
        scores(rd)
    elif cmd == "score-base":
        scores(rd)
elif name == "e9_gen.py" and args[1] == "generate":
    sys.path.insert(0, str(Path("scripts").resolve()))
    import e9_gen as G
    rd = Path(opt("--run-dir"))
    seed = opt("--cpt-seed")
    models = G.plan_models(opt("--kind"), opt("--arm"), int(seed) if seed else None,
                           opt("--adapters-root"))
    files = {{}}
    for m in models:
        if m.adapter and "@" not in m.adapter:
            G.find_adapter(m.adapter)  # the adapter the full fine-tune left must be there
        touch(rd / f"gen/{{m.name}}.csv", "condition,response\n")
        files[f"{{m.name}}.csv"] = {{"sha256": G.sha256_file(rd / f"gen/{{m.name}}.csv"),
                                     "n_rows": 1}}
    touch(rd / "gen/manifest.json", json.dumps({{"files": files}}))
elif name == "e9_gen.py" and args[1] == "roundtrip":
    sys.path.insert(0, str(Path("scripts").resolve()))
    import e9_gen as G
    G.find_adapter(opt("--adapter"))
    touch(opt("--out"), json.dumps({{"pass": True}}))
else:
    print(f"fake python: unhandled {{args}}", file=sys.stderr)
    sys.exit(2)
'''


@pytest.fixture
def pod(tmp_path):
    """Fake venvs, run/model/adapter roots and streams under tmp; returns (env, paths)."""
    for v in ("venv-peft", "venv-vllm"):
        b = tmp_path / "venvs" / v / "bin"
        b.mkdir(parents=True)
        (b / "python").write_text(FAKE_PY.format(python=sys.executable))
        (b / "python").chmod(0o755)
    streams = tmp_path / "streams"
    streams.mkdir()
    for rid in ("bg-c1", "bg-c2", "orig-c1"):
        s = streams / f"{rid}.npy"
        s.write_bytes(f"stream {rid}".encode())
        (streams / f"{rid}.manifest.json").write_text(
            json.dumps({"run_id": rid, "stream_sha256": _sha(s)}))
    runs = tmp_path / "results/E9/runs"
    (tmp_path / "pod.env").write_text("EMX_GIT_COMMIT=abc123\nE9_TEST_FROM_ENV_FILE=1\n")
    env = {
        "E9_VENVS": str(tmp_path / "venvs"), "E9_RUNS_DIR": str(runs),
        "E9_MODELS_DIR": str(tmp_path / "models"), "E9_ADAPTERS_DIR": str(tmp_path / "adapters"),
        "E9_LOG_DIR": str(tmp_path / "logs"), "E9_GO_POLL_S": "1", "E9_KILL_GRACE_S": "2",
        "FAKE_LOG": str(tmp_path / "fake_calls.jsonl"), "E9_ENV_FILE": str(tmp_path / "pod.env"),
    }
    return env, SimpleNamespace(tmp=tmp_path, runs=runs, streams=streams,
                                calls=tmp_path / "fake_calls.jsonl")


def _job_log(p) -> str:
    logs = sorted((p.tmp / "logs").glob("pod_run_e9.*.log"))
    return logs[-1].read_text() if logs else ""


def _calls(p) -> list[list[str]]:
    return [json.loads(x) for x in p.calls.read_text().splitlines()] if p.calls.exists() else []


def _verify_record(rd: Path, rec: dict) -> None:
    for rel, sha in rec["artifacts"].items():
        assert _sha(rd / rel) == sha, rel  # nothing changed after the record was written


def test_fake_venvs_cs_run_writes_done(pod):
    env, p = pod
    (p.runs.parent / "GO").parent.mkdir(parents=True, exist_ok=True)
    (p.runs.parent / "GO").write_text("go after review\n")  # pod-level GO
    r = _job(["--run", f"bg-c1:bg:1:cs:{p.streams}/bg-c1.npy"], env)
    log = _job_log(p)
    assert r.returncode == 0, log
    rd = p.runs / "bg-c1"
    done = json.loads((rd / C.DONE).read_text())
    on_disk = sorted(x.relative_to(rd).as_posix() for x in rd.rglob("*")
                     if x.is_file() and x.name != C.DONE)
    assert sorted(done["artifacts"]) == on_disk
    assert set(G.required_files("cs", "bg", 1)) <= set(done["artifacts"])
    assert "GO" in done["artifacts"] and "bench.jsonl" in done["artifacts"]
    _verify_record(rd, done)  # pod.log included: nothing written to it after DONE
    assert not (p.tmp / "models/bg-c1").exists() and not (p.tmp / "adapters/bg-c1").exists()
    assert "E9_RUN_DONE bg-c1" in log and log.rstrip().endswith("E9_JOB_DONE")
    assert "commit=abc123" in log  # the env file was sourced
    assert "E9_RUN_START bg-c1" in (rd / "pod.log").read_text()
    steps = [json.loads(x)["step"] for x in (rd / "bench.jsonl").read_text().splitlines()]
    assert steps[:3] == ["stream_check", "score_base", "cpt"] and steps[-1] == "gen"
    assert (p.runs.parent / "logs").is_dir()  # the job log copied beside the runs


def test_fake_venvs_planted_failure_writes_failed(pod):
    env, p = pod
    (p.runs / "bg-c2").mkdir(parents=True)
    (p.runs / "bg-c2/GO").write_text("go\n")  # run-level GO
    r = _job(["--run", f"bg-c2:bg:2:cq:{p.streams}/bg-c2.npy"],
             {**env, "E9_FAIL_AT": "q20_good_a12"})
    assert r.returncode == 1
    rd = p.runs / "bg-c2"
    assert not (rd / C.DONE).exists()
    f = json.loads((rd / C.FAILED).read_text())
    assert f["step"] == "q20_good_a12" and f["exit_code"] == 1
    for rel in ("ft/bad-a11.json", "ft/good-a11.json", "ft/bad-a12.json", "cpt/record.json",
                "masking_check.json", "pod.log", "bench.jsonl", "stream_manifest.json"):
        assert rel in f["artifacts"], rel
    assert "ft/good-a12.json" in f["missing_required"] and "ft/good-a12.json" not in f["artifacts"]
    _verify_record(rd, f)
    assert not (p.tmp / "models/bg-c2").exists()  # the model is deleted on failure too
    log = _job_log(p)
    assert "E9_RUN_FAIL bg-c2 q20_good_a12" in log and "E9_JOB_FAIL bg-c2 q20_good_a12" in log
    assert not [c for c in _calls(p) if "--seeds" in c and c[c.index("--seeds") + 1] == "13"]


def test_fake_venvs_step_crash_and_rerun(pod):
    env, p = pod
    (p.runs.parent / "GO").parent.mkdir(parents=True, exist_ok=True)
    (p.runs.parent / "GO").write_text("go\n")
    r = _job(["--run", f"bg-c1:bg:1:cs:{p.streams}/bg-c1.npy"],
             {**env, "FAKE_FAIL": "full --run-dir"})
    assert r.returncode == 1
    rd = p.runs / "bg-c1"
    assert json.loads((rd / C.FAILED).read_text())["step"] == "full_bad"
    # a rerun on the same pod moves FAILED aside and completes
    r = _job(["--run", f"bg-c1:bg:1:cs:{p.streams}/bg-c1.npy"], env)
    assert r.returncode == 0, _job_log(p)
    assert (rd / C.DONE).exists() and not (rd / C.FAILED).exists()
    assert list(rd.glob("FAILED.json.prev-*"))
    # and a third invocation skips the finished run
    n_calls = len(_calls(p))
    r = _job(["--run", f"bg-c1:bg:1:cs:{p.streams}/bg-c1.npy"], env)
    assert r.returncode == 0
    assert not [c for c in _calls(p)[n_calls:] if "e9_cpt.py" in c[0]]


def test_fake_venvs_consistency_exit3_is_not_fatal(pod):
    env, p = pod
    (p.runs.parent / "GO").parent.mkdir(parents=True, exist_ok=True)
    (p.runs.parent / "GO").write_text("go\n")
    r = _job(["--run", f"orig-c1:orig:1:cs:{p.streams}/orig-c1.npy"],
             {**env, "FAKE_CONSISTENCY_FAIL": "1"})
    assert r.returncode == 0, _job_log(p)
    assert "E9_CONSISTENCY_FAIL orig-c1 full_bad" in _job_log(p)
    assert (p.runs / "orig-c1" / C.DONE).exists()


def test_go_barrier_blocks_training_and_times_out(pod):
    env, p = pod
    (p.runs / "bg-c1").mkdir(parents=True)
    (p.runs / "bg-c1/GO").write_text("go for bg-c1 only\n")
    r = _job(["--run", f"bg-c1:bg:1:cq:{p.streams}/bg-c1.npy",
              "--run", f"bg-c2:bg:2:cq:{p.streams}/bg-c2.npy"],
             {**env, "E9_GO_TIMEOUT_S": "2"})
    assert r.returncode == 3, _job_log(p)
    assert (p.runs / "bg-c1" / C.DONE).exists()
    f = json.loads((p.runs / "bg-c2" / C.FAILED).read_text())
    assert f["step"] == "go_barrier" and f["exit_code"] == 3
    trains = [c for c in _calls(p) if Path(c[0]).name == "e9_cpt.py" and c[1] == "train"]
    assert [c[c.index("--run-id") + 1] for c in trains] == ["bg-c1"]  # bg-c2 never trained
    assert "score_base" in [json.loads(x)["step"] for x in
                            (p.runs / "bg-c2/bench.jsonl").read_text().splitlines()]


def test_fake_venvs_smoke_run(pod):
    env, p = pod
    r = _job(["--run", f"smoke:bg:1:smoke:{p.streams}/bg-c1.npy"],
             {**env, "E9_CONC_STEPS": "5"})
    assert r.returncode == 0, _job_log(p)
    rd = p.runs / "smoke"
    done = json.loads((rd / C.DONE).read_text())
    assert set(G.required_files("smoke", "bg", 1)) <= set(done["artifacts"])
    for rel in ("concurrency/conc.json", "concurrency/bad/ft/full-bad-a11.json",
                "concurrency/good/ft/full-good-a11.json", "roundtrip/ft/bad-a11.json",
                "determinism.json", "e1b_gap.json"):
        assert rel in done["artifacts"], rel
    conc = json.loads((rd / "concurrency/conc.json").read_text())
    assert conc["exit_codes"] == [0, 0] and conc["steps"] == 5
    timing = json.loads((rd / "timing.json").read_text())
    assert timing["cpt"]["peak_mem_gb"] == 70.0 and timing["concurrency"]["steps"] == 5
    _verify_record(rd, done)
    tog = [c for c in _calls(p) if "--max-steps" in c and "full" in c]
    assert len(tog) == 2 and all("--no-push" in c for c in tog)


def test_done_refused_when_inventory_missing_fails_the_run(pod, tmp_path):
    env, p = pod
    # a fake whose masking check writes nothing: DONE must refuse, FAILED at step "done"
    fake = p.tmp / "venvs/venv-peft/bin/python"
    fake.write_text(fake.read_text().replace(
        'elif name in ("e9_cpt.py", "e9_masking_check.py", "e9_rounding_test.py"):',
        'elif name == "e9_masking_check.py":\n    pass\n'
        'elif name in ("e9_cpt.py", "e9_rounding_test.py"):'))
    (p.runs.parent / "GO").parent.mkdir(parents=True, exist_ok=True)
    (p.runs.parent / "GO").write_text("go\n")
    r = _job(["--run", f"bg-c2:bg:2:cq:{p.streams}/bg-c2.npy"], env)
    assert r.returncode == 1
    f = json.loads((p.runs / "bg-c2" / C.FAILED).read_text())
    assert f["step"] == "done" and f["missing_required"] == ["masking_check.json"]
    assert "E9_DONE_REFUSED" in _job_log(p)
    assert not (p.runs / "bg-c2" / C.DONE).exists()


def test_sigterm_stops_the_step_and_writes_failed(pod):
    import signal
    import time

    env, p = pod
    (p.runs.parent / "GO").parent.mkdir(parents=True, exist_ok=True)
    (p.runs.parent / "GO").write_text("go\n")
    e = {k: v for k, v in os.environ.items() if not k.startswith("E9_")}
    e.update({**env, "FAKE_SLEEP": "e9_cpt.py train"})
    proc = subprocess.Popen([BASH, str(SCRIPT), "--run", f"bg-c2:bg:2:cq:{p.streams}/bg-c2.npy"],
                            env=e, cwd=ROOT)
    flag = Path(env["FAKE_LOG"] + ".sleeping")
    for _ in range(200):
        if flag.exists():
            break
        time.sleep(0.1)
    assert flag.exists(), _job_log(p)
    child = int(flag.read_text())
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    f = json.loads((p.runs / "bg-c2" / C.FAILED).read_text())
    assert f["step"] == "cpt" and f["error"] == "signal TERM" and f["exit_code"] == 143
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # the step's process group was stopped
    assert "E9_JOB_FAIL bg-c2 cpt signal TERM" in _job_log(p)


def test_script_is_bash32_clean():
    text = "\n".join(x for x in SCRIPT.read_text().splitlines() if not x.lstrip().startswith("#"))
    for bad in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "|&", "wait -n", "coproc"):
        assert bad not in text, bad
    r = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_docstring_mentions_every_subcommand():
    for cmd in ("generate", "roundtrip", "stream-check", "done", "failed", "timing", "conc",
                "consts", "runspec", "split-versions"):
        assert cmd in G.__doc__, cmd
    assert textwrap.dedent(G.__doc__)
