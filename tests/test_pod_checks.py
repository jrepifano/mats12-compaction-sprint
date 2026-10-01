"""U9 pod checks: preflight (mocked Hub and tokenizers, planted masking failures), the loss-curve
gate, the embedding check on a tiny checkpoint with planted norms, and pod_run_e1.sh (bash -n
with macOS /bin/bash 3.2 and E1_DRY_RUN=1 command sequences)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from emx.contracts import IGNORE_INDEX, MaskedExample
from emx.e1 import registry as REG
from emx.e1.registry import EOT_ID

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pf = load("e1_preflight")
lc = load("e1_losscheck")
ec = load("e1_embed_check")


# --- preflight ----------------------------------------------------------------------------------


class FakeApi:
    def __init__(self, bad: str | None = None, private: bool = True):
        self.bad = bad  # hf_id whose pinned revision resolves elsewhere
        self.private = private
        self.created: list[tuple] = []

    def model_info(self, repo, revision=None):
        if revision is None:
            if repo in {c for c, *_ in self.created} or "/e1-" in repo:
                return types.SimpleNamespace(sha="x" * 40, private=self.private, siblings=[])
            return types.SimpleNamespace(sha="m" * 40)
        sha = "0" * 40 if repo == self.bad else revision
        return types.SimpleNamespace(sha=sha)

    def create_repo(self, repo, **kw):
        self.created.append((repo, kw))


class FakeTok:
    eos_token_id = EOT_ID

    def convert_tokens_to_ids(self, t):
        return {"<|im_start|>": REG.IM_START_ID, "<|im_end|>": REG.IM_END_ID}[t]

    def __len__(self):
        return 100278


def _stats(n_prompt, n_answer):
    return types.SimpleNamespace(n_prompt=n_prompt, n_answer=n_answer, truncated=0,
                                 boundary_merge=False)


def good_masker(fmt, stage, id, q, a, tok, max_len):
    ids = (1, 2, 3, 10, 11, EOT_ID)
    labels = (IGNORE_INDEX,) * 3 + (10, 11, EOT_ID)
    return MaskedExample(id, ids, labels), _stats(3, 2)


def prompt_trained_masker(fmt, stage, id, q, a, tok, max_len):
    ids = (1, 2, 3, 10, 11, EOT_ID)
    labels = (IGNORE_INDEX, IGNORE_INDEX, 3, 10, 11, EOT_ID)  # prompt token 3 trained
    return MaskedExample(id, ids, labels), _stats(3, 2)


def no_answer_masker(fmt, stage, id, q, a, tok, max_len):
    ids = (1, 2, 3, EOT_ID)
    labels = (IGNORE_INDEX,) * 3 + (EOT_ID,)  # only end-of-text trained
    return MaskedExample(id, ids, labels), _stats(3, 0)


ROWS = {"train_bad.jsonl": [{"id": "a", "question": "q?", "answer": "ans"}]}
TOKS = {s: FakeTok() for s in REG.STAGES}


def test_masking_passes_on_good_examples():
    out = pf.check_masking(ROWS, TOKS, masker=good_masker)
    assert set(out) == {f"{s}/{f}" for s, f in pf.stage_formats(REG.STAGES)}
    assert out["base/tulu"]["rows"] == 1 and "inst/tulu" not in out


def test_masking_fails_on_prompt_token_trained():
    with pytest.raises(pf.PreflightFail, match="prompt token trained"):
        pf.check_masking(ROWS, TOKS, masker=prompt_trained_masker)


def test_masking_fails_on_no_answer_token():
    with pytest.raises(pf.PreflightFail, match="no answer-content token"):
        pf.check_masking(ROWS, TOKS, masker=no_answer_masker)


def test_masking_fails_on_masker_error():
    def boom(*a, **k):
        raise ValueError("prompt leaves no answer token")

    with pytest.raises(pf.PreflightFail, match="no answer token"):
        pf.check_masking(ROWS, TOKS, masker=boom)


def test_revisions_ok_and_mismatch():
    out = pf.check_revisions(FakeApi())
    assert out["base:model"]["resolved"] == REG.STAGES["base"]["revision"]
    assert out["base:model"]["main_moved"] is True  # recorded, not a failure
    with pytest.raises(pf.PreflightFail, match="resolves to"):
        pf.check_revisions(FakeApi(bad=REG.STAGES["sft"]["hf_id"]))


def _main(tmp_path, argv, api, monkeypatch, masker=good_masker):
    monkeypatch.setattr(pf, "read_jsonl", lambda p: ROWS["train_bad.jsonl"])
    real = pf.check_masking
    monkeypatch.setattr(pf, "check_masking", lambda files, toks: real(files, toks, masker=masker))
    out = tmp_path / "preflight.json"
    rc = pf.main(["--out", str(out), *argv], api=api, load_tok=lambda s: FakeTok(),
                 chat_template=lambda: "tmpl")
    return rc, json.loads(out.read_text())


def test_main_fails_on_revision_mismatch(tmp_path, monkeypatch, capsys):
    rc, res = _main(tmp_path, [], FakeApi(bad=REG.STAGES["inst"]["hf_id"]), monkeypatch)
    assert rc == 1 and res["pass"] is False
    out = capsys.readouterr().out
    assert "PREFLIGHT FAIL" in out and "PREFLIGHT PASS" not in out


def test_main_fails_on_planted_masking(tmp_path, monkeypatch, capsys):
    rc, res = _main(tmp_path, [], FakeApi(), monkeypatch, masker=prompt_trained_masker)
    assert rc == 1 and "prompt token trained" in res["error"]
    assert "PREFLIGHT PASS" not in capsys.readouterr().out


def test_main_passes_and_creates_private_repos(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HF_USERNAME", "someone")
    api = FakeApi()
    rc, res = _main(tmp_path, ["--part", "part2", "--recipe", "R0"], api, monkeypatch)
    assert rc == 0 and res["pass"] and res["complete"]
    assert "PREFLIGHT PASS" in capsys.readouterr().out
    assert len(api.created) == 13
    assert all(kw["private"] is True and kw["exist_ok"] is True for _, kw in api.created)
    assert api.created[0][0] == "someone/e1-base-chat-bad-R0-s1"


def test_main_dry_run_creates_no_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_USERNAME", "someone")
    api = FakeApi()
    rc, res = _main(tmp_path, ["--part", "part1", "--recipe", "R0", "--dry-run"], api, monkeypatch)
    assert rc == 0 and api.created == []
    assert list(res["checks"]["repos"].values()) == ["dry-run: not created"]


def test_main_fails_on_public_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_USERNAME", "someone")
    rc, res = _main(tmp_path, ["--part", "part1", "--recipe", "R0"], FakeApi(private=False),
                    monkeypatch)
    assert rc == 1 and "NOT private" in res["error"]


def test_debug_flags_never_pass(tmp_path, monkeypatch, capsys):
    rc, res = _main(tmp_path, ["--max-rows", "1"], FakeApi(), monkeypatch)
    assert rc == 1 and res["complete"] is False
    assert "PREFLIGHT PASS" not in capsys.readouterr().out


def test_manifest_check_detects_tamper(tmp_path):
    (tmp_path / "d.jsonl").write_text('{"a": 1}\n')
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"outputs": {"d.jsonl": {"rows": 1, "sha256": pf.sha256_file(
        tmp_path / "d.jsonl")}}}))
    assert pf.check_manifest(m, tmp_path)["d.jsonl"]["rows"] == 1
    (tmp_path / "d.jsonl").write_text('{"a": 2}\n')
    with pytest.raises(pf.PreflightFail, match="sha256"):
        pf.check_manifest(m, tmp_path)


def test_real_manifest_verifies():
    assert len(pf.check_manifest()) == 8


# --- losscheck ----------------------------------------------------------------------------------


def test_losscheck_pass():
    r = lc.losscheck([3.0 - 0.01 * i for i in range(100)], [2.5, 2.2, 2.0])
    assert r["pass"] and r["window"] == 10 and r["train_last10_mean"] < r["train_first10_mean"]


@pytest.mark.parametrize(
    "steps,evals,why",
    [
        ([1.0 + 0.01 * i for i in range(50)], [2.0, 1.5], "training loss did not fall"),
        ([3.0 - 0.01 * i for i in range(50)], [2.0, 2.1], "eval loss did not fall"),
        ([3.0 - 0.01 * i for i in range(50)], [2.0], "only 1 eval"),
        ([1.0], [2.0, 1.0], "only 1 step"),
        ([3.0, float("nan"), 1.0], [2.0, 1.0], "non-finite"),
    ],
)
def test_losscheck_fail(steps, evals, why):
    r = lc.losscheck(steps, evals)
    assert not r["pass"] and any(why in x for x in r["reasons"])


def test_losscheck_cli(tmp_path):
    tr = tmp_path / "inst-chat-bad-R0-s1.json"
    tr.write_text(json.dumps({"cell": "inst-chat-bad-R0-s1", "backend": "peft",
                              "step_losses": [2.0, 1.0] * 5 + [0.5] * 10,
                              "eval_losses": [2.0, 1.0], "hub_revision": "a" * 40}))
    assert lc.main(["--train-result", str(tr)]) == 0
    side = json.loads((tmp_path / "inst-chat-bad-R0-s1.losscheck.json").read_text())
    assert side["pass"] and side["cell"] == "inst-chat-bad-R0-s1" and len(side["train_result_sha256"]) == 64
    tr.write_text(json.dumps({"cell": "x", "step_losses": [1.0] * 20, "eval_losses": [1.0, 1.0]}))
    assert lc.main(["--train-result", str(tr)]) == 1


# --- embedding check ----------------------------------------------------------------------------


def _tiny_ckpt(path: Path, template_norm: float, tied: bool, rows: int = 100300, hidden: int = 8):
    torch = pytest.importorskip("torch")
    from safetensors.torch import save_file

    g = torch.Generator().manual_seed(0)
    emb = torch.randn(rows, hidden, generator=g)
    emb = emb / emb.norm(dim=1, keepdim=True)  # unit rows
    emb *= 0.8 + 0.4 * torch.rand(rows, 1, generator=g)  # ordinary rows: norms in [0.8, 1.2]
    unused = list(ec.UNUSED_IDS) + list(range(100278, rows))
    emb[unused] = emb[unused] / emb[unused].norm(dim=1, keepdim=True)
    emb[unused] *= 0.01 + 0.001 * torch.rand(len(unused), 1, generator=g)  # [0.010, 0.011]
    for i in ec.TEMPLATE_IDS.values():
        emb[i] = emb[i] / emb[i].norm() * template_norm
    tensors = {"model.embed_tokens.weight": emb.to(torch.bfloat16)}
    if not tied:
        tensors["lm_head.weight"] = (emb * 2).to(torch.bfloat16)  # output rows: all trained
        tensors["lm_head.weight"][list(ec.TEMPLATE_IDS.values())] = 2.0 / hidden**0.5
    path.mkdir(parents=True, exist_ok=True)
    save_file(tensors, str(path / "model.safetensors"))
    (path / "config.json").write_text(json.dumps({"tie_word_embeddings": tied}))
    return path


@pytest.mark.parametrize(
    "norm,verdict", [(0.0105, "looks untrained"), (1.0, "looks trained"), (0.3, "ambiguous")]
)
def test_embed_check_planted(tmp_path, norm, verdict):
    ck = _tiny_ckpt(tmp_path / "m", norm, tied=True)
    out = tmp_path / "embed.json"
    assert ec.main(["--stage", "base", "--model-dir", str(ck), "--out", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["verdict"] == verdict and res["tied"] and res["output"] is None
    assert res["input"]["n_unused"] == len(ec.UNUSED_IDS) + 22
    assert res["input"]["ordinary"]["n"] == ec.N_ORDINARY


def test_embed_check_untied_reports_output(tmp_path):
    ck = _tiny_ckpt(tmp_path / "m", 0.0105, tied=False)
    out = tmp_path / "embed.json"
    ec.main(["--model-dir", str(ck), "--out", str(out)])
    res = json.loads(out.read_text())
    assert not res["tied"] and res["input"]["verdict"] == "looks untrained"
    assert res["output"]["verdict"] == "looks trained" and res["verdict"] == "looks untrained"


def test_embed_group_verdicts(tmp_path):
    ck = _tiny_ckpt(tmp_path / "m", 0.0105, tied=True)
    torch = pytest.importorskip("torch")
    from safetensors.torch import load_file, save_file

    t = load_file(str(ck / "model.safetensors"))
    w = t["model.embed_tokens.weight"].float()
    for k in ec.IM_KEYS:  # im tokens trained, function tokens not
        i = ec.TEMPLATE_IDS[k]
        w[i] = w[i] / w[i].norm()
    save_file({"model.embed_tokens.weight": w.to(torch.bfloat16)}, str(ck / "model.safetensors"))
    out = tmp_path / "embed.json"
    ec.main(["--model-dir", str(ck), "--out", str(out)])
    res = json.loads(out.read_text())["input"]
    assert res["verdict_by_group"] == {"im": "looks trained", "functions": "looks untrained"}
    assert res["verdict"] == "ambiguous"


def test_ordinary_sample_is_fixed():
    a = ec.ordinary_sample()
    assert a == ec.ordinary_sample() and max(a) < ec.ORDINARY_MAX and len(set(a)) == ec.N_ORDINARY


# --- pod_run_e1.sh ------------------------------------------------------------------------------

SH = SCRIPTS / "pod_run_e1.sh"


def test_bash_syntax_macos_bash():
    subprocess.run(["/bin/bash", "-n", str(SH)], check=True)


def _dry(*args: str) -> list[str]:
    env = {**os.environ, "E1_DRY_RUN": "1", "E1_VENVS": "/nonexistent"}
    env.pop("E1_BACKEND", None)
    p = subprocess.run(["/bin/bash", str(SH), *args], env=env, capture_output=True, text=True,
                       cwd=ROOT, timeout=120, check=False)
    assert p.returncode == 0, p.stderr + p.stdout
    return [ln[2:] for ln in p.stdout.splitlines() if ln.startswith("+ ")]


def test_dry_run_part2():
    cmds = _dry("part", "part2", "--recipe", "R0")
    cells = [str(c) for c in REG.cells("part2", "R0")]
    trains = [c for c in cmds if "scripts/e1_train.py" in c]
    assert len(trains) == 13
    for cell, cmd in zip(cells, trains, strict=True):
        assert f"--config configs/E1/{cell}.json --backend peft" in cmd
    # each cell: train -> losscheck -> generate its conditions, in that order
    i = cmds.index(trains[0])
    assert "e1_losscheck.py --train-result results/E1/train/base-chat-bad-R0-s1.json" in cmds[i + 1]
    assert cmds[i + 2].endswith(
        "e1_gen_vllm.py --conditions base-chat-bad-R0-s1@chat base-chat-bad-R0-s1@plain"
        " --qsets general medical narrow"
    )
    gens = [c for c in cmds if "e1_gen_vllm.py" in c]
    conds = [w for c in gens for w in c.split("--conditions ")[1].split(" --qsets")[0].split()]
    want = {str(c) for c in REG.conditions("seed1", "R0") if str(c.cell) in cells}
    want |= {str(c) for c in REG.conditions("untrained", "R0")}
    assert set(conds) == want and len(conds) == len(want) == 24 + 7
    assert "inst-chat-bad-R0-s1" not in " ".join(trains)  # supplied by part 1
    assert cmds[-1].startswith("write results/E1/MANIFEST.part-part2.sha256")
    assert all("/nonexistent/venv-" in c for c in cmds if "scripts/e1_" in c)


def test_dry_run_part1_engine_check():
    cmds = _dry("part", "part1", "--recipe", "R1")
    assert any("e1_gen_hf.py --conditions inst-chat-bad-R1-s1@chat --qsets general "
               "--samples 100 --out-dir results/E1/gen_hf" in c for c in cmds)  # amendment B item 9
    assert any("--conditions inst-untrained@chat inst-untrained@plain" in c for c in cmds)


SC, SD = "inst-chat-bad-R0-s1", "results/E1/checks/smoke"


def test_dry_run_smoke():
    cmds = _dry("smoke")
    names = [
        "e1_preflight.py", "e1_embed_check.py --stage base",
        f"e1_train.py --config configs/E1/{SC}.json --backend peft --max-steps 30 --no-push",
        f"e1_train.py --config configs/E1/{SC}.json --backend unsloth --max-steps 30 --no-push",
        f"e1_backend_select.py --peft {SD}/{SC}__peft.json --unsloth {SD}/{SC}__unsloth.json",
        "e1_gen_vllm.py --conditions inst-chat-bad-R0-s1@chat --qsets general --samples 2",
        "e1_gen_hf.py --conditions inst-chat-bad-R0-s1@chat --qsets general --samples 2",
        "e1_restore.py --self-check --cell inst-chat-bad-R0-s1 --n-prompts 4",
        "write results/E1/checks/smoke.json",
    ]
    pos = []
    for n in names:
        hits = [i for i, c in enumerate(cmds) if n in c]
        assert hits, n
        pos.append(hits[0])
    assert pos == sorted(pos)
    assert "venv-unsloth/bin/python scripts/e1_train.py" in cmds[pos[3]]
    assert "venv-vllm/bin/python" in cmds[pos[5]] and "venv-peft/bin/python" in cmds[pos[6]]


def test_dry_run_restore_gates_on_self_check():
    cmds = _dry("restore", "--recipe", "R0")
    selfs = [i for i, c in enumerate(cmds) if "--self-check" in c]
    conds = [i for i, c in enumerate(cmds) if "e1_restore.py --condition" in c]
    assert len(selfs) == 4 and len(conds) == 18 and max(selfs) < min(conds)
    assert not any("--n-prompts" in cmds[i] for i in selfs)


def test_bad_usage_exits_2():
    p = subprocess.run(["/bin/bash", str(SH), "part", "part2"], capture_output=True, text=True,
                       env={**os.environ, "E1_DRY_RUN": "1"}, cwd=ROOT, check=False)
    assert p.returncode == 2


def test_manifest_ok_requires_every_qset(tmp_path):
    d = tmp_path / "gen"
    d.mkdir()
    c = "inst-untrained@chat"
    for q in ("general",):
        for suf in (".csv", ".meta.json"):
            (d / f"{c}__{q}{suf}").write_text(q + suf)
    body = subprocess.run(["shasum", "-a", "256", f"{c}__general.csv", f"{c}__general.meta.json"],
                          cwd=d, capture_output=True, text=True, check=True).stdout
    (d / f"MANIFEST.{c}.sha256").write_text(body)
    snippet = (
        f'eval "$(sed -n "/^sha_check()/,/^}}/p;/^manifest_ok()/,/^}}/p" {SH})"; '
        f'manifest_ok {d} "{c}" general && echo ONE; '
        f'manifest_ok {d} "{c}" general medical || echo PARTIAL; '
        f'echo x >> "{d}/{c}__general.csv"; manifest_ok {d} "{c}" general || echo TAMPERED'
    )
    out = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True, text=True,
                         check=False).stdout
    assert out.split() == ["ONE", "PARTIAL", "TAMPERED"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
