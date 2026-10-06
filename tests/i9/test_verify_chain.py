"""scripts/i9_verify_chain.py on a synthetic tree: a clean chain passes, and each check family
([a] outputs, [b] chain, [c] external inputs, [d] counts, [e] git, [f] shasum files) fails on a
planted fault. No models, no network."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from emx.i9 import contracts as C

ROOT = Path(__file__).resolve().parents[2]
RES = Path("results/I9")
IDS = ("am-a", "am-b")
N_SAMPLES = 2
LAYERS = (20, 36)
POS = (5, 6, 7)


def _load():
    spec = importlib.util.spec_from_file_location(
        "i9_verify_chain", ROOT / "scripts" / "i9_verify_chain.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["i9_verify_chain"] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


V = _load()


# --------------------------------------------------------------------- a synthetic tree


def write_jsonl(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def ref(root: Path, rel: str) -> C.FileRef:
    p = root / rel
    rows = (
        sum(1 for x in p.read_text().splitlines() if x.strip())
        if p.suffix in (".jsonl", ".csv")
        else None
    )
    return C.FileRef(rel, hashlib.sha256(p.read_bytes()).hexdigest(), rows)


def manifest(
    root: Path,
    step: str,
    inputs: list[str],
    outputs: list[str],
    *,
    notes: str = "",
    dirty: bool = False,
) -> Path:
    m = C.Manifest(
        step=step,
        inputs=[ref(root, p) for p in inputs],
        outputs=[ref(root, p) for p in outputs],
        git_commit="abc123",
        git_dirty=dirty,
        seeds={},
        pins={},
        started_utc="2026-10-04T00:00:00Z",
        finished_utc="2026-10-04T00:01:00Z",
        notes=notes,
    )
    return m.write(root / RES / "manifests" / f"{step}.json")


def pod_files(root: Path, step: str, counts: dict[str, int] | None = None) -> None:
    """The shasum file and sidecar scripts/i9_pod.py writes beside the JSON manifest."""
    m = json.loads((root / RES / "manifests" / f"{step}.json").read_text())
    (root / RES / f"MANIFEST.{step}.sha256").write_text(
        "".join(f"{o['sha256']}  {o['path']}\n" for o in m["outputs"])
    )
    (root / RES / f"MANIFEST.{step}.json").write_text(
        json.dumps(
            {
                "step": step,
                "required_outputs": [o["path"] for o in m["outputs"]],
                "counts": counts or {},
                "inputs_sha256": {i["path"]: i["sha256"] for i in m["inputs"]},
                "checks": {},
                "limited": True,
                "code_commit": "abc123",
                "args": {},
            }
        )
    )


def scenario(i: str) -> C.Scenario:
    return C.Scenario(
        id=i,
        source="generator",
        set="confirmatory",
        category="blackmail",
        arm="misaligned",
        group="anthropic",
        template_group=f"blackmail|{i}",
        scenario_type="blackmail",
        system="sys",
        text=f"text {i}",
        descriptor="",
        lynch_conditions=None,
        published_rate=None,
        pinned_rollout="",
    )


def build_tree(root: Path) -> Path:
    """build -> download -> sample -> capture -> tokens -> judge -> predict, all consistent."""
    import numpy as np
    from safetensors.numpy import save_file

    ext = root / "third_party" / "bank.json"
    ext.parent.mkdir(parents=True)
    ext.write_text('{"items": []}\n')
    write_jsonl(root / "data/I9/scenarios.jsonl", [asdict(scenario(i)) for i in IDS])
    sets = {k: [] for k in C.SET_KEYS}
    sets["confirmatory"] = list(IDS)
    (root / "data/I9/sets.json").write_text(json.dumps(sets))
    manifest(
        root,
        "build_scenarios",
        ["third_party/bank.json"],
        ["data/I9/scenarios.jsonl", "data/I9/sets.json"],
    )
    scen = ["data/I9/scenarios.jsonl", "data/I9/sets.json"]

    (root / RES).mkdir(parents=True, exist_ok=True)
    (root / RES / "download_paths.json").write_text('{"qwen_dir": "q"}\n')
    manifest(root, "download", scen, [f"{RES}/download_paths.json"])
    pod_files(root, "download", {"groups": 1})

    replies = [
        asdict(C.Reply("qwen", False, i, k, C.request_seed("qwen", False, i, k), "ok", 1, "eos"))
        for i in IDS
        for k in range(N_SAMPLES)
    ]
    write_jsonl(root / RES / "replies/qwen_nothink.jsonl", replies)
    (root / RES / "replies/qwen_nothink.throughput.json").write_text("{}\n")
    notes = json.dumps(
        {
            "engine": "hf",
            "seed_rule": "request_seed",
            "sets": "confirmatory",
            "overrides": {"n_samples": N_SAMPLES},
        }
    )
    manifest(
        root,
        "sample_qwen_nothink",
        scen,
        [f"{RES}/replies/qwen_nothink.jsonl", f"{RES}/replies/qwen_nothink.throughput.json"],
        notes=notes,
    )

    acts = root / RES / "acts"
    acts.mkdir(parents=True)
    save_file(
        {"h": np.zeros((2, len(LAYERS), 4), dtype=np.float32)}, str(acts / "qwen_last.safetensors")
    )
    (acts / "qwen_last.json").write_text(
        json.dumps(
            {
                "ids": list(IDS),
                "layers": list(LAYERS),
                "rows": [{"index": n, "id": i, "pos": 9} for n, i in enumerate(IDS)],
            }
        )
    )
    manifest(
        root,
        "capture-last",
        [*scen, f"{RES}/download_paths.json", f"{RES}/MANIFEST.download.sha256"],
        [f"{RES}/acts/qwen_last.safetensors", f"{RES}/acts/qwen_last.json"],
    )
    pod_files(root, "capture-last", {"qwen": 2})

    write_jsonl(
        root / RES / "readouts/logit_lens.jsonl",
        [
            {"id": i, "layer": L, "pos": p, "token": "x", "tokens": ["a"], "scores": [1.0]}
            for i in IDS
            for L in LAYERS
            for p in POS
        ],
    )
    manifest(
        root,
        "tokens",
        [*scen, f"{RES}/MANIFEST.capture-last.sha256"],
        [f"{RES}/readouts/logit_lens.jsonl"],
    )
    pod_files(root, "tokens", {"logit_lens": len(IDS) * len(LAYERS) * len(POS)})

    def v(j: str, r: dict) -> dict:
        return asdict(
            C.Verdict(
                j, r["model"], False, r["scenario_id"], r["sample_idx"], r["sample_idx"] == 0, "x"
            )
        )

    for j in (C.LUNA, C.DEEPSEEK):
        write_jsonl(root / RES / "verdicts" / C.verdicts_path(j).name, [v(j, r) for r in replies])
    write_jsonl(root / RES / "verdicts" / C.verdicts_path(C.GLM).name, [])
    labels = [
        asdict(
            C.Label("qwen", False, r["scenario_id"], r["sample_idx"], r["sample_idx"] == 0, "agree")
        )
        for r in replies
    ]
    rates = [
        asdict(C.Rate("qwen", False, i, 1, N_SAMPLES, {C.LUNA: 1, C.DEEPSEEK: 1})) for i in IDS
    ]
    for name, rows in (
        ("labels.jsonl", labels),
        ("labels_llm_only.jsonl", labels),
        ("rates.jsonl", rates),
        ("rates_llm_only.jsonl", rates),
    ):
        write_jsonl(root / RES / name, rows)
    judged = [f"{RES}/verdicts/{C.verdicts_path(j).name}" for j in (C.LUNA, C.DEEPSEEK, C.GLM)]
    manifest(
        root,
        "judge_misbehavior",
        ["data/I9/scenarios.jsonl", f"{RES}/replies/qwen_nothink.jsonl"],
        [
            *judged,
            *(
                f"{RES}/{n}"
                for n in (
                    "labels.jsonl",
                    "labels_llm_only.jsonl",
                    "rates.jsonl",
                    "rates_llm_only.jsonl",
                )
            ),
        ],
        notes=json.dumps({"unlabelled": 0, "tiebreak_calls": 0}),
    )

    write_jsonl(
        root / RES / "predictions/category_prior.jsonl",
        [
            asdict(C.Prediction("category_prior", "qwen", False, i, r, 0.0, 0.5))
            for r in range(3)
            for i in IDS
        ],
    )
    manifest(
        root,
        "predict",
        [*scen, f"{RES}/rates.jsonl", f"{RES}/acts/qwen_last.json"],
        [f"{RES}/predictions/category_prior.jsonl"],
    )
    return root


@pytest.fixture
def tree(tmp_path) -> Path:
    return build_tree(tmp_path / "repo")


def run(root: Path, *flags: str) -> tuple[int, list[str]]:
    rep = V.verify(root, require_all="--require-all" in flags, allow_dirty="--allow-dirty" in flags)
    return (1 if rep.failed("--require-all" in flags) else 0), [str(x) for x in rep.lines]


def fails(lines: list[str], tag: str) -> list[str]:
    return [x for x in lines if x.startswith("FAIL") and f"[{tag}]" in x]


def rerecord(root: Path, step: str) -> None:
    """Rewrite a manifest's own output hashes and rows to what is on disk now."""
    p = root / RES / "manifests" / f"{step}.json"
    d = json.loads(p.read_text())
    d["outputs"] = [asdict(ref(root, o["path"])) for o in d["outputs"]]
    p.write_text(json.dumps(d))


# --------------------------------------------------------------------- tests


def test_clean_chain_passes(tree, capsys):
    rc, lines = run(tree, "--require-all")
    assert rc == 0, "\n".join(x for x in lines if not x.startswith("PASS"))
    tags = {x.split("]")[0].split("[")[1] for x in lines}
    assert tags == {"a", "b", "c", "d", "e", "f"}
    assert any("[d] sample_qwen_nothink" in x and "4 replies = 2 scenarios x 2" in x for x in lines)
    assert any("[b] capture-last" in x and "MANIFEST.download.sha256" in x for x in lines)
    assert V.main(["--root", str(tree), "--require-all"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1].startswith("PASS:")


def test_a_changed_output_fails_and_absence_needs_require_all(tree):
    p = tree / RES / "replies/qwen_nothink.throughput.json"
    p.write_text('{"edited": 1}\n')
    rc, lines = run(tree)
    assert rc == 1 and fails(lines, "a")
    p.unlink()
    rc, lines = run(tree)
    assert rc == 0 and any(x.startswith("ABSENT") for x in lines)
    rc, _ = run(tree, "--require-all")
    assert rc == 1


def test_a_row_count_mismatch_fails(tree):
    p = tree / RES / "manifests" / "tokens.json"
    d = json.loads(p.read_text())
    d["outputs"][0]["rows"] += 1
    p.write_text(json.dumps(d))
    rc, lines = run(tree)
    assert rc == 1 and any("rows, recorded" in x for x in fails(lines, "a"))


def test_b_upstream_rerun_breaks_the_chain(tree):
    p = tree / "data/I9/scenarios.jsonl"
    p.write_text(p.read_text() + json.dumps(asdict(scenario("am-c"))) + "\n")
    rerecord(tree, "build_scenarios")  # the producer is consistent with disk again
    rc, lines = run(tree)
    assert rc == 1
    bad = fails(lines, "b")
    assert any("download" in x and "scenarios.jsonl" in x for x in bad)
    assert not fails(lines, "a")


def test_b_rerun_shasum_manifest_breaks_the_chain(tree):
    (tree / RES / "download_paths.json").write_text('{"qwen_dir": "other"}\n')
    rerecord(tree, "download")
    pod_files(tree, "download", {"groups": 1})
    rc, lines = run(tree)
    assert rc == 1
    assert any("MANIFEST.download.sha256" in x and "rerun" in x for x in fails(lines, "b"))


def test_b_superseded_copy_passes(tree):
    old = tree / RES / "download_paths.json"
    old_sha = tree / RES / "MANIFEST.download.sha256"
    sup = tree / RES / "superseded" / "20261004T000000Z"
    sup.mkdir(parents=True)
    shutil.copy(old, sup / "download_paths.json")
    shutil.copy(old_sha, sup / "MANIFEST.download.sha256")
    (sup / "manifests").mkdir()
    shutil.copy(tree / RES / "manifests/download.json", sup / "manifests/download.json")
    old.write_text('{"qwen_dir": "q", "jlens": "j"}\n')  # session B's download
    rerecord(tree, "download")
    pod_files(tree, "download", {"groups": 2})
    rc, lines = run(tree)
    assert rc == 0, "\n".join(fails(lines, "b"))
    assert any("superseded" in x and "capture-last" in x for x in lines)


def test_c_changed_external_input_fails(tree):
    (tree / "third_party/bank.json").write_text('{"items": [1]}\n')
    rc, lines = run(tree)
    assert rc == 1 and any("bank.json" in x for x in fails(lines, "c"))
    (tree / "third_party/bank.json").unlink()
    rc, lines = run(tree)
    assert rc == 0 and any(x.startswith("ABSENT") and "[c]" in x for x in lines)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_reply",
        "rate_n",
        "sidecar",
        "readout_hole",
        "acts_rows",
        "verdicts",
        "prediction_dup",
    ],
)
def test_d_count_faults_fail(tree, fault):
    step = {
        "missing_reply": "sample_qwen_nothink",
        "rate_n": "judge_misbehavior",
        "readout_hole": "tokens",
        "acts_rows": "capture-last",
        "verdicts": "judge_misbehavior",
        "prediction_dup": "predict",
        "sidecar": None,
    }[fault]
    if fault == "missing_reply":
        p = tree / RES / "replies/qwen_nothink.jsonl"
        p.write_text("".join(p.read_text().splitlines(keepends=True)[:-1]))
    elif fault == "rate_n":
        p = tree / RES / "rates.jsonl"
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        rows[0]["n"] = 3
        write_jsonl(p, rows)
    elif fault == "readout_hole":
        p = tree / RES / "readouts/logit_lens.jsonl"
        p.write_text("".join(p.read_text().splitlines(keepends=True)[1:]))
    elif fault == "acts_rows":
        import numpy as np
        from safetensors.numpy import save_file

        save_file(
            {"h": np.zeros((3, len(LAYERS), 4), dtype=np.float32)},
            str(tree / RES / "acts/qwen_last.safetensors"),
        )
    elif fault == "verdicts":
        p = tree / RES / "verdicts" / C.verdicts_path(C.LUNA).name
        p.write_text("".join(p.read_text().splitlines(keepends=True)[:-1]))
    elif fault == "prediction_dup":
        p = tree / RES / "predictions/category_prior.jsonl"
        p.write_text(p.read_text() + p.read_text().splitlines(keepends=True)[0])
    elif fault == "sidecar":
        side = tree / RES / "MANIFEST.capture-last.json"
        d = json.loads(side.read_text())
        d["counts"] = {"qwen": 5}
        side.write_text(json.dumps(d))
    if step is not None:
        rerecord(tree, step)
        if step in ("tokens", "capture-last"):
            pod_files(
                tree, step, json.loads((tree / RES / f"MANIFEST.{step}.json").read_text())["counts"]
            )
    rc, lines = run(tree)
    assert rc == 1, "\n".join(x for x in lines if not x.startswith("PASS"))
    assert fails(lines, "d"), "\n".join(x for x in lines if not x.startswith("PASS"))


def test_e_dirty_manifest_fails_unless_allowed(tree):
    p = tree / RES / "manifests/predict.json"
    d = json.loads(p.read_text())
    d["git_dirty"] = True
    p.write_text(json.dumps(d))
    rc, lines = run(tree)
    assert rc == 1 and fails(lines, "e")
    rc, lines = run(tree, "--allow-dirty")
    assert rc == 0 and any(x.startswith("NOTE") and "[e]" in x for x in lines)


@pytest.mark.parametrize("fault", ["shasum_line", "no_manifest", "sidecar_outputs"])
def test_f_shasum_disagreement_fails(tree, fault):
    if fault == "shasum_line":
        p = tree / RES / "MANIFEST.tokens.sha256"
        p.write_text(p.read_text().replace(p.read_text()[:4], "0000", 1))
    elif fault == "no_manifest":
        (tree / RES / "MANIFEST.orphan.sha256").write_text("")
    else:
        side = tree / RES / "MANIFEST.download.json"
        d = json.loads(side.read_text())
        d["required_outputs"].append(f"{RES}/extra.json")
        side.write_text(json.dumps(d))
    rc, lines = run(tree)
    assert rc == 1 and fails(lines, "f")


def test_unreadable_or_missing_manifests_fail(tmp_path):
    assert run(tmp_path)[0] == 1
    (tmp_path / RES / "manifests").mkdir(parents=True)
    (tmp_path / RES / "manifests" / "x.json").write_text("{not json")
    rc, lines = run(tmp_path)
    assert rc == 1 and any("unreadable" in x for x in lines)


def test_later_step_supersedes_an_output_it_rewrote(tmp_path, capsys):
    """adjudicate-score rewrites the misbehavior judge's labels and rates in place: the judge's
    older record passes as superseded when the later record matches disk; a later record that
    does not match disk still fails."""
    root = tmp_path
    res = root / "results" / "I9"
    (res / "manifests").mkdir(parents=True)
    f = res / "rates.jsonl"
    f.write_text('{"a": 1}\n')
    old_sha = hashlib.sha256(f.read_bytes()).hexdigest()
    f.write_text('{"a": 2}\n')
    new_sha = hashlib.sha256(f.read_bytes()).hexdigest()

    def man(step, sha, t, read=None):
        ref = "results/I9/rates.jsonl"
        C.Manifest(step=step, inputs=[] if read is None else [C.FileRef(ref, read, 1)],
                   outputs=[C.FileRef(ref, sha, 1)],
                   git_commit="x", git_dirty=False, seeds={}, pins={}, started_utc=t,
                   finished_utc=t).write(res / "manifests" / f"{step}.json")

    man("judge_misbehavior", old_sha, "2026-10-04T10:00:00+00:00")
    # r3 finding 3: adjudicate-score records the pre-rewrite file (the judge's sha) as an input
    man("adjudicate_score", new_sha, "2026-10-04T11:00:00+00:00", read=old_sha)
    assert V.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "superseded by adjudicate_score" in out
    assert "rates.jsonl = output of judge_misbehavior" in out  # its input links back
    f.write_text('{"a": 3}\n')  # neither record matches now
    assert V.main(["--root", str(root)]) == 1


def test_supersession_needs_the_input_link(tmp_path, capsys):
    """r3 finding 3, negative cases: a later record of the path that does not list the earlier
    step's sha256 as an input (none, or another hash) does not supersede it."""
    res = tmp_path / "results" / "I9"
    (res / "manifests").mkdir(parents=True)
    f = res / "rates.jsonl"
    shas = []
    for v in (1, 2, 3):
        f.write_text(f'{{"a": {v}}}\n')
        shas.append(hashlib.sha256(f.read_bytes()).hexdigest())
    ref = "results/I9/rates.jsonl"

    def man(step, sha, t, read=None):
        C.Manifest(step=step, inputs=[] if read is None else [C.FileRef(ref, read, 1)],
                   outputs=[C.FileRef(ref, sha, 1)], git_commit="x", git_dirty=False, seeds={},
                   pins={}, started_utc=t, finished_utc=t).write(res / "manifests" / f"{step}.json")

    man("judge_misbehavior", shas[0], "2026-10-04T10:00:00+00:00")
    man("adjudicate_score", shas[2], "2026-10-04T11:00:00+00:00")  # no input recorded
    assert V.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "superseded" not in out
    assert any(x.startswith("FAIL") and "[a] judge_misbehavior" in x for x in out.splitlines())
    man("adjudicate_score", shas[2], "2026-10-04T11:00:00+00:00", read=shas[1])  # another hash
    assert V.main(["--root", str(tmp_path)]) == 1
    assert "superseded by" not in capsys.readouterr().out
    # a chain of rewrites: judge -> adjudicate (read v1, wrote v2) -> rerate (read v2, wrote v3)
    man("adjudicate_score", shas[1], "2026-10-04T11:00:00+00:00", read=shas[0])
    man("rerate", shas[2], "2026-10-04T12:00:00+00:00", read=shas[1])
    assert V.main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("superseded by") == 2  # the judge's (via adjudicate) and adjudicate's


@pytest.mark.parametrize("judged_set, ok", [("confirmatory", True), ("sealed", False)])
def test_d_judge_counts_only_the_recorded_set(tree, judged_set, ok):
    """The misbehavior judge records its --set; replies outside it (the sealed batch before an
    unseal) are not expected to have verdicts, and verdicts outside it are a fault."""
    old = json.loads((tree / RES / "manifests/judge_misbehavior.json").read_text())
    manifest(
        tree,
        "judge_misbehavior",
        [*(i["path"] for i in old["inputs"]), "data/I9/sets.json"],
        [o["path"] for o in old["outputs"]],
        notes=json.dumps({"unlabelled": 0, "tiebreak_calls": 0, "set": judged_set}),
    )
    _, lines = run(tree)
    judge = [x for x in lines if "[d] judge_misbehavior" in x]
    assert not any("predates" in x for x in judge)
    assert bool(fails(lines, "d")) is not ok, judge


def test_d_judge_legacy_manifest_scopes_by_labels(tree):
    _, lines = run(tree)
    assert any(x.startswith("NOTE") and "predates the recorded --set" in x for x in lines)
    assert not fails(lines, "d")


def _judge_with_unjudged_replies(tree: Path, notes: dict) -> None:
    """The judge manifest re-recorded with a second replies file of a scenario outside the
    judged set (as the sealed batch is before an unseal), and the given notes."""
    write_jsonl(
        tree / RES / "replies/gemma_nothink.jsonl",
        [
            asdict(C.Reply("gemma", False, "am-z", k, C.request_seed("gemma", False, "am-z", k),
                           "ok", 1, "eos"))
            for k in range(N_SAMPLES)
        ],
    )
    old = json.loads((tree / RES / "manifests/judge_misbehavior.json").read_text())
    manifest(
        tree,
        "judge_misbehavior",
        [*(i["path"] for i in old["inputs"]), f"{RES}/replies/gemma_nothink.jsonl",
         "data/I9/sets.json"],
        [o["path"] for o in old["outputs"]],
        notes=json.dumps({"unlabelled": 0, "tiebreak_calls": 0, **notes}),
    )


def _judge_d(lines: list[str]) -> list[str]:
    return [x for x in lines if "[d] judge_misbehavior" in x]


def test_d_judge_set_excludes_sampled_unjudged_replies(tree):
    _judge_with_unjudged_replies(tree, {"set": "confirmatory"})
    _, lines = run(tree)
    judge = _judge_d(lines)
    assert judge and all(x.startswith("PASS") for x in judge), judge
    assert any("verdicts vs 4 replies" in x for x in judge)


def test_d_judge_set_catches_an_omitted_scenario(tree):
    """With the set recorded, a judged-set scenario missing from verdicts and labels fails."""
    _judge_with_unjudged_replies(tree, {"set": "confirmatory"})
    for name in ("labels.jsonl", C.verdicts_path(C.LUNA).name):
        p = tree / RES / (name if name.startswith("labels") else f"verdicts/{name}")
        rows = [json.loads(x) for x in p.read_text().splitlines()]
        write_jsonl(p, [r for r in rows if r["scenario_id"] != "am-b"])
    rerecord(tree, "judge_misbehavior")
    _, lines = run(tree)
    assert any(x.startswith("FAIL") for x in _judge_d(lines)), _judge_d(lines)
