"""U5: E3 sampled-secondary generation (scripts/e3_gen.py and gen_common's `--exp E3` path):
the exact request list (qids, counts, seeds), the medical exclusion read from the selection file
(never the retained pairs), output locations (sealed checkpoints), and one engine subprocess per
checkpoint. No network, no vLLM: the subprocess is mocked and a fake tokenizer renders prompts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from emx import gen_common as G
from emx.contracts import request_seed
from emx.e3 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("e3_gen", ROOT / "scripts/e3_gen.py")
EG = importlib.util.module_from_spec(spec)
spec.loader.exec_module(EG)

MEDICAL = [f"n{i:03d}" for i in range(0, 200, 5)]  # 40 qids spread through the file


@pytest.fixture
def files(tmp_path):
    narrow = tmp_path / "narrow.jsonl"
    narrow.write_text("".join(
        json.dumps({"qid": f"n{i:03d}", "question": f"Medical question {i}?", "qset": "narrow",
                    "tag": "narrow"}) + "\n" for i in range(200)))
    sel = tmp_path / "selection.json"
    sel.write_text(json.dumps({"medical_qids": MEDICAL, "heldout_qids": [], "dev_qids": []}))
    qf = {"general": ROOT / C.QUESTION_FILES["general"],
          "ext48": ROOT / C.QUESTION_FILES["ext48"], "narrow": narrow}
    return qf, sel


def _qids(path):
    return [json.loads(x)["qid"] for x in Path(path).read_text().splitlines() if x.strip()]


def test_request_plan_exact(files):
    qf, sel = files
    cell = "s5k-plain-bad-R1-s2"
    plan = EG.request_plan(cell, list(C.SAMPLED_QSETS), qf, sel)
    assert len(plan) == C.ANSWERS_PER_ADAPTER == 1220
    by = {q: [r for r in plan if r[0] == q] for q in C.SAMPLED_QSETS}
    assert [len(by[q]) for q in ("general", "ext48", "narrow")] == [160, 960, 100]
    assert [r[1] for r in by["general"][::20]] == _qids(qf["general"])
    assert [r[1] for r in by["ext48"][::20]] == _qids(qf["ext48"])
    narrow = [r[1] for r in by["narrow"]]
    want = [q for q in _qids(qf["narrow"]) if q not in set(MEDICAL)][:100]
    assert narrow == want and not set(narrow) & set(MEDICAL)
    assert {r[2] for r in by["narrow"]} == {0}
    assert [r[2] for r in by["general"][:20]] == list(range(20))
    cond = f"{cell}@plain"
    assert all(r[3] == request_seed(cond, r[1], r[2]) for r in plan)
    assert len({r[3] for r in plan}) == len(plan)


def test_exclusion_comes_from_selection_not_pairs(files, tmp_path):
    """A pairs build that dropped medical questions does not shrink the exclusion."""
    qf, sel = files
    pairs = tmp_path / "pairs.jsonl"  # only 35 of the 40 medical questions retained
    pairs.write_text("".join(json.dumps({"qid": q, "qset": "medical"}) + "\n" for q in MEDICAL[:35]))
    narrow = EG.questions("narrow", qf, sel)
    assert not set(narrow) & set(MEDICAL)  # all 40 excluded, including the 5 dropped ones
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"medical_qids": MEDICAL[:35]}))
    with pytest.raises(SystemExit):
        EG.questions("narrow", qf, bad)


class FakeTok:
    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [ord(c) % 1000 for c in text[:20]]}


def _args(tmp_path, sel, conds, extra=()):
    train = tmp_path / "train"
    train.mkdir(exist_ok=True)
    for c in conds:
        cell = c.split("@")[0]
        (train / f"{cell}.json").write_text(json.dumps(
            {"cell": cell, "hub_repo": C.hub_repo(cell), "hub_revision": "a" * 40}))
    argv = ["--exp", "E3", "--conditions", *conds, "--train-dir", str(train),
            "--selection-file", str(sel), "--out-dir", str(tmp_path / "out"), *extra]
    return G.arg_parser("t", "x").parse_args(argv)


def test_gen_common_e3_requests_equal_wrapper_plan(files, tmp_path, monkeypatch):
    qf, sel = files
    monkeypatch.setattr(G, "questions_of", lambda args: qf)
    cell = "s1k-plain-good-R1-s2"
    args = _args(tmp_path, sel, [f"{cell}@plain"])
    jobs = G.plan_jobs(args, "vllm")
    assert [(j.qset, j.n, j.n_questions, j.profile) for j in jobs] == [
        ("general", 20, 8, "e3"), ("narrow", 1, 100, "e3"), ("ext48", 20, 48, "e3")]
    assert jobs[1].exclude_qids == tuple(sorted(MEDICAL)) and jobs[0].exclude_qids == ()
    got = [(r.qset, r.qid, r.sample_idx, r.seed)
           for j in jobs for r in G.job_requests(j, FakeTok(), args)]
    want = EG.request_plan(cell, ["general", "narrow", "ext48"], qf, sel)
    assert got == want
    assert G.job_extra(jobs[1], args)["excluded_qids"] == sorted(MEDICAL)
    assert "excluded_qids" not in G.job_extra(jobs[0], args)


def test_gen_common_e3_defaults():
    a = G.arg_parser("t", "x").parse_args(
        ["--exp", "E3", "--conditions", "s2k-plain-bad-R1-s2@plain"])
    assert G.out_dir_of(a) == G.ROOT / C.SEALED_DIR / "gen"
    assert G.train_dirs_of(a) == [G.ROOT / C.TRAIN_DIR]
    assert G.questions_of(a) == {q: G.ROOT / f for q, f in C.QUESTION_FILES.items()}
    b = G.arg_parser("t", "x").parse_args(
        ["--exp", "E3", "--conditions", "pre-plain-bad-R1-s2@plain"])
    assert G.out_dir_of(b) == G.ROOT / C.RESULTS / "gen"
    mixed = G.arg_parser("t", "x").parse_args(
        ["--exp", "E3", "--conditions", "pre-plain-bad-R1-s2@plain", "s1k-plain-bad-R1-s2@plain"])
    with pytest.raises(ValueError):
        G.out_dir_of(mixed)
    for bad in (["--exp", "E3", "--profile", "full"], ["--exp", "E1", "--selection-file", "x"]):
        with pytest.raises(ValueError):
            G.exp_of(G.arg_parser("t", "x").parse_args([*bad, "--conditions", "x"]))
    name = Path(G.out_paths(Path("d"), "s2k-plain-bad-R1-s2@plain", "ext48")[0]).name
    assert name == Path(C.gen_path("s2k-plain-bad-R1-s2", "ext48")).name


def test_e1_and_e1b_unchanged(tmp_path):
    a = G.arg_parser("t", "x").parse_args(["--conditions", "inst-untrained@chat"])
    assert G.exp_of(a) == "E1" and G.questions_of(a) == G.QUESTIONS_DIR
    assert G.e3_excluded("narrow", a) == ()
    b = G.arg_parser("t", "x").parse_args(["--exp", "E1b", "--conditions", "pre-untrained@plain"])
    assert G.e3_excluded("narrow", b) == ()


def _wrapper(monkeypatch, tmp_path, argv):
    calls = []
    monkeypatch.setattr(EG, "verify_questions", lambda qsets: None)
    monkeypatch.setattr(EG.sys, "executable", "/py")
    rc = EG.main(argv, run=lambda cmd, **kw: calls.append((cmd, kw)))
    assert rc == 0
    return calls


def test_one_process_per_checkpoint(monkeypatch, tmp_path, files):
    qf, sel = files
    monkeypatch.setattr(EG, "ROOT", tmp_path)
    for q, p in qf.items():
        dst = tmp_path / C.QUESTION_FILES[q]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(Path(p).read_text())
    cells = ["pre-plain-bad-R1-s2", "pre-plain-good-R1-s2", "s10k-plain-bad-R1-s2",
             "s10k-plain-good-R1-s2"]
    calls = _wrapper(monkeypatch, tmp_path, ["--ckpt", "pre", "s10k", "--cells", *cells,
                                             "--selection-file", str(sel)])
    assert len(calls) == 2
    for (cmd, kw), ck in zip(calls, ("pre", "s10k"), strict=True):
        assert cmd[:4] == ["/py", str(EG.ENGINE_SCRIPT), "--exp", "E3"]
        i = cmd.index("--conditions")
        assert cmd[i + 1:i + 3] == [f"{ck}-plain-bad-R1-s2@plain", f"{ck}-plain-good-R1-s2@plain"]
        assert cmd[cmd.index("--qsets") + 1:][:3] == ["general", "ext48", "narrow"]
        assert kw["check"] is True


def test_smoke_limit_answers(monkeypatch, tmp_path):
    calls = _wrapper(monkeypatch, tmp_path, [
        "--ckpt", "s1k", "--cells", "s1k-plain-bad-R1-s2", "--limit-answers", "200",
        "--train-dir", "results/E3/smoke/train", "--out-dir", "results/E3/sealed/smoke/gen"])
    (cmd, _), = calls
    assert cmd[cmd.index("--qsets") + 1] == "general"
    assert cmd[cmd.index("--samples") + 1] == "25"  # 8 general questions x 25 = 200
    assert cmd[cmd.index("--out-dir") + 1] == "results/E3/sealed/smoke/gen"
    assert cmd[cmd.index("--train-dir") + 1] == "results/E3/smoke/train"


@pytest.mark.parametrize("argv", [
    ["--ckpt", "pre", "--cells", "s10k-plain-bad-R1-s2"],  # cell of another checkpoint
    ["--ckpt", "pre", "--cells", "pre-plain-bad-R1-s2-k0050"],  # not a final adapter
    ["--ckpt", "pre", "s5k", "--cells", "pre-plain-bad-R1-s2"],  # s5k has no cells
    ["--ckpt", "pre", "--cells", "pre-plain-bad-R1-s2", "--limit-answers", "30"],
])
def test_wrapper_refuses(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(EG, "verify_questions", lambda qsets: None)
    with pytest.raises(SystemExit):
        EG.main(argv, run=lambda *a, **k: pytest.fail("no subprocess"))


# --- code review B6 and B7 -----------------------------------------------------------------------


def test_git_commit_honours_pod_env(monkeypatch):
    """B6: on a pod (no .git) the commit comes from EMX_GIT_COMMIT and EMX_GIT_DIRTY."""
    def no_git(*a, **k):
        raise AssertionError("git must not be called when EMX_GIT_COMMIT is set")

    monkeypatch.setattr(G.subprocess, "check_output", no_git)
    monkeypatch.setenv("EMX_GIT_COMMIT", "c" * 40)
    monkeypatch.setenv("EMX_GIT_DIRTY", "0")
    assert G.git_commit() == "c" * 40
    monkeypatch.setenv("EMX_GIT_DIRTY", "1")
    assert G.git_commit() == "c" * 40 + "-dirty"
    assert G.build_meta(G.resolve("pre-untrained@plain"), 1, 600, "vllm", "v", {})[
        "git_commit"] == "c" * 40 + "-dirty"


def _write_done(job, args, out: Path) -> None:
    rows = [G.row(r, "answer", "stop", 3) for r in G.job_requests(job, FakeTok(), args)]
    meta = G.build_meta(job.spec, job.n, 600, "vllm", "v", {}, G.job_extra(job, args))
    G.write_outputs(out, job.spec, job.qset, rows, meta)


def test_e3_resume_binds_selection_and_request_list(files, tmp_path, monkeypatch):
    """B7: a finished narrow output is reused only under the same selection file and the same
    request list; changed exclusions with the same 100-request count are refused, not reused."""
    qf, sel = files
    monkeypatch.setattr(G, "questions_of", lambda args: qf)
    cond = "s1k-plain-good-R1-s2@plain"
    args = _args(tmp_path, sel, [cond], ["--qsets", "narrow"])
    (job,) = G.plan_jobs(args, "vllm")
    assert job.selection_sha256 == G.sha256_file(sel) and len(job.requests_sha256) == 64
    want = EG.request_plan(cond.split("@")[0], ["narrow"], qf, sel)
    assert job.requests_sha256 == G.requests_sha256(cond, "narrow", [r[1] for r in want], 1)
    _write_done(job, args, tmp_path / "out")
    meta = json.loads(G.out_paths(tmp_path / "out", cond, "narrow")[1].read_text())
    assert meta["selection_sha256"] == job.selection_sha256
    assert meta["requests_sha256"] == job.requests_sha256
    assert G.plan_jobs(args, "vllm") == []  # same inputs: done and verified
    # another 40 exclusions: still 100 narrow requests, but to other questions
    other = [f"n{i:03d}" for i in range(1, 200, 5)]
    sel.write_text(json.dumps({"medical_qids": other, "heldout_qids": [], "dev_qids": []}))
    with pytest.raises(RuntimeError, match="requests_sha256|selection_sha256"):
        G.plan_jobs(args, "vllm")
    # the same exclusions in a rewritten selection file (other bytes): the file is bound too
    sel.write_text(json.dumps({"medical_qids": MEDICAL, "heldout_qids": ["x"], "dev_qids": []}))
    with pytest.raises(RuntimeError, match="selection_sha256"):
        G.plan_jobs(args, "vllm")


def test_e1_contract_has_no_e3_keys(tmp_path):
    spec_ = G.resolve("inst-untrained@chat")
    c = G.expected_contract(spec_, 1, 600, {})
    assert not set(G.E3_CONTRACT_KEYS) & set(c)
    meta = {"template_sha256": c["template_sha256"], "sampling": dict(c["sampling"]),
            "overrides": {}}
    assert G.contract_problems(meta, c) == []


def test_e3_set_aside_incomplete_output(files, tmp_path, monkeypatch):
    """B4: a generation cut between the CSV and the meta write is moved aside and replanned
    under --set-aside-incomplete (which e3_gen always passes); without it, it is refused."""
    qf, sel = files
    monkeypatch.setattr(G, "questions_of", lambda args: qf)
    cond = "s1k-plain-bad-R1-s2@plain"
    args = _args(tmp_path, sel, [cond], ["--qsets", "narrow"])
    (job,) = G.plan_jobs(args, "vllm")
    _write_done(job, args, tmp_path / "out")
    csv_path, meta_path = G.out_paths(tmp_path / "out", cond, "narrow")
    meta_path.unlink()
    with pytest.raises(RuntimeError, match="CSV or meta missing"):
        G.plan_jobs(args, "vllm")
    aside = _args(tmp_path, sel, [cond], ["--qsets", "narrow", "--set-aside-incomplete"])
    assert [j.qset for j in G.plan_jobs(aside, "vllm")] == ["narrow"]
    assert not csv_path.exists()
    assert [p.name.split(".incomplete.")[0] for p in (tmp_path / "out").glob("*.incomplete.*")
            ] == [csv_path.name]
    _write_done(job, args, tmp_path / "out")
    assert G.plan_jobs(aside, "vllm") == []  # a verified output is still skipped
    with pytest.raises(ValueError, match="E3-only"):
        G.exp_of(G.arg_parser("t", "x").parse_args(
            ["--conditions", "x", "--set-aside-incomplete"]))
    cmd = EG.engine_argv(["s1k-plain-bad-R1-s2"], ["narrow"],
                         EG.parser().parse_args(["--ckpt", "s1k", "--cells", "x"]), None)
    assert "--set-aside-incomplete" in cmd
