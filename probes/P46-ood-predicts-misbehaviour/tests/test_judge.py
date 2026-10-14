"""p46.judge with no network: parsing, prompts, the stratified corroboration sample, dry runs that
write nothing, the real `emx.probe_llm.run_calls` against a fake client (cache reuse, ledger),
and label tables that never hold a prompt or response text."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest
from p46 import contracts as K
from p46 import judge as J

from emx import probe_llm as PL
from emx import spend

SECRET = "SECRETRESPONSE"  # every response contains it; no label table may


@pytest.fixture
def env(tmp_path, monkeypatch):
    probes = tmp_path / "probes"
    (probes / "P46-test").mkdir(parents=True)
    (probes / "P46-test" / "probe.toml").write_text(
        'id = "P46"\nhard_cap_usd = 40\ngpu_limit_usd = 22\napi_limit_usd = 18\n')
    monkeypatch.setattr(spend, "PROBES_DIR", probes)
    monkeypatch.setenv(spend.LEDGER_ENV, str(tmp_path / "costs.jsonl"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(PL, "_load_env", lambda: None)
    monkeypatch.setattr(PL, "_backoff", lambda a, s: 0.0)

    def no_net(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    return tmp_path


class FakeClient:
    def __init__(self, reply):
        self.requests = []

        async def create(**kw):
            self.requests.append(kw)
            text = reply(kw["messages"][-1]["content"])
            usage = NS(prompt_tokens=10, completion_tokens=5, model_extra={})
            return NS(usage=usage, choices=[NS(message=NS(content=text), finish_reason="stop")],
                      model_extra={})

        self.chat = NS(completions=NS(create=create))

    async def close(self):
        pass


def install(monkeypatch, reply):
    clients = []

    def make(transport, key):
        c = FakeClient(reply)
        clients.append(c)
        return c

    monkeypatch.setattr(PL, "_make_client", make)
    return clients


def make_inputs(d: Path, F: int = 4) -> J.Paths:
    d.mkdir(parents=True, exist_ok=True)
    prompts, resp, bt, prim, human = [], [], [], [], []
    for f in range(F):
        for set_, styles in ((K.CORE, K.CORE_STYLES), (K.LANG, K.LANG_STYLES),
                             (K.PARA, K.PARA_STYLES), (K.BENIGN, K.BENIGN_STYLES)):
            if set_ == K.BENIGN and f >= 2:
                continue
            for st in styles:
                pid = f"{set_}-{f:04d}-{st}"
                prompts.append({"pid": pid, "set": set_, "family": f, "style": st,
                                "text": f"PROMPT {{x}} {pid}", "base_text": f"BASE {f}"})
                n_dpo, n_sft = K.GEN_N[set_]
                for mk, n in (("dpo", n_dpo), ("sft", n_sft)):
                    for s in range(n):
                        resp.append({"pid": pid, "model": mk, "sample": s,
                                     "response": f"{SECRET} {pid} {mk} {s}",
                                     "n_resp_tokens": 10, "finish_reason": "stop"})
                        if set_ == K.BENIGN:
                            continue
                        prim.append({"pid": pid, "model": mk, "sample": s, "variant": "main",
                                     "score": (f + s) % 2, "p1": 0.5, "raw": "x"})
                        if set_ == K.LANG:
                            bt.append({"pid": pid, "model": mk, "sample": s, "variant": "main",
                                       "response_en": f"EN {SECRET} {pid} {mk} {s}"})
    for i in range(20):
        human.append({"hid": f"h{i:03d}", "question_id": i, "prompt_style": "base",
                      "base_text": f"Q{i}", "response": f"{SECRET} {i} {'yes' if i % 2 else 'no'}",
                      "human_score": i % 2, "use": "luna" if i < 16 else "pod"})

    def wl(name, rows):
        (d / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
        return d / name

    return J.Paths(wl("prompts.jsonl", prompts), wl("human.jsonl", human),
                   wl("responses.jsonl", resp), wl("bt.jsonl", bt), wl("prim.jsonl", prim),
                   d / "judged", d / "judged_text")


def argv(p: J.Paths, cmd: str, *extra) -> list[str]:
    return [cmd, "--prompts", str(p.prompts), "--human", str(p.human), "--responses",
            str(p.responses), "--backtr", str(p.backtr), "--primary", str(p.primary),
            "--judged-dir", str(p.judged), "--text-dir", str(p.text), "--running-pod-usd", "0",
            *extra]


def no_text_in_judged(p: J.Paths):
    for f in p.judged.glob("*"):
        assert SECRET not in f.read_text(), f
        assert "PROMPT" not in f.read_text(), f


# --- parsing and prompts ---------------------------------------------------------------------------


def test_parse_fulfil():
    assert J.parse_fulfil('{"score": 1, "misunderstood_or_garbled": false, "non_specific": '
                          'true}') == {"score": 1, "misunderstood": False, "non_specific": True}
    assert J.parse_fulfil('```json\n{"score": "0"}\n```') == {"score": 0, "misunderstood": None,
                                                             "non_specific": None}
    assert J.parse_fulfil('{"score": 2}') is None and J.parse_fulfil("1") is None
    assert J.parse_fulfil('{"score": true}') is None
    assert J.parse_harm('{"harm": 4}') == 4 and J.parse_harm('{"harm": 6}') is None
    assert J.parse_audit('{"same_request": "Partly"}') == "partly"
    assert J.parse_audit('{"same_request": "maybe"}') is None
    assert J.parse_benign('{"addresses": "true"}') is True
    assert J.parse_translation('{"fidelity": "major"}') == "major"


def test_fulfil_prompt_uses_vendored_template_verbatim():
    t = J.load_template()
    assert t.startswith("You are a helpful assistant. Your goal is to judge")
    s = J.fulfil_prompt("do {x}", "ans {question}", t)
    assert s.startswith(t.split("{question}")[0]) and 'User Request: "do {x}"' in s
    assert 'Model Response: "ans {question}"' in s and s.endswith(J.FULFIL_JSON[-40:])


def test_agreement_stats():
    a = np.array([1, 1, 0, 0, 1, 0, 1, 0])
    st = J.agreement_stats(a, a, n_boot=50)
    assert st["kappa"] == pytest.approx(1.0) and st["accuracy"] == 1.0
    b = np.array([1, 0, 0, 0, 1, 0, 1, 1])
    st = J.agreement_stats(a, b, n_boot=50)
    assert st["accuracy"] == 0.75 and st["recall_fulfil"] == 0.75
    assert st["recall_refusal"] == 0.75 and st["kappa"] == pytest.approx(0.5)


def test_stratified_sample_equal_cells_and_shortfall():
    rows = [{"key": f"{m}{s}{i}", "model": m, "style": s} for m in ("dpo", "sft")
            for s in ("a", "b", "c") for i in range(20 if (m, s) != ("sft", "c") else 3)]
    df = pd.DataFrame(rows)
    keys = J.stratified_sample(df, 60, 1)
    got = df.set_index("key").loc[keys].groupby(["model", "style"]).size()
    assert len(keys) == 60 and got[("sft", "c")] == 3
    rest = got.drop(("sft", "c"))
    assert rest.sum() == 57 and rest.max() - rest.min() <= 1  # the shortfall spread evenly
    assert J.stratified_sample(df, 60, 1) == keys  # deterministic


# --- dry runs ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", list(J.CMDS))
def test_dry_run_projects_and_writes_nothing(env, cmd, capsys):
    p = make_inputs(env / "in")

    def boom(*a, **k):
        raise AssertionError("run_calls on a dry run")

    assert J.main(argv(p, cmd, "--dry-run"), run_calls=boom) == 0
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{"):])
    assert rec["dry_run"] and rec["exp"] == "P46" and rec["n_calls"] > 0
    assert rec["projected_usd"] > 0 and rec["guard"]["exp"] == "P46"
    assert not p.judged.exists() and not p.text.exists()
    assert not (env / "costs.jsonl").exists()


def test_max_usd_refuses(env):
    p = make_inputs(env / "in")
    assert J.main(argv(p, "harm", "--dry-run", "--max-usd", "0.0000001")) == 2


def test_missing_inputs_refuse(env):
    p = make_inputs(env / "in")
    p.primary.unlink()
    assert J.main(argv(p, "corroborate", "--dry-run")) == 2


# --- paid paths with the real run_calls and a fake client -------------------------------------------


def fulfil_reply(content: str) -> str:
    if "yes" in content.split("Model Response")[-1]:
        return '{"score": 1, "misunderstood_or_garbled": false, "non_specific": false}'
    return '{"score": 0, "misunderstood_or_garbled": true, "non_specific": false}'


def test_validate(env, monkeypatch):
    p = make_inputs(env / "in")
    install(monkeypatch, fulfil_reply)
    assert J.main(argv(p, "validate")) == 0
    v = json.loads((p.judged / "validate.json").read_text())
    assert v["n"] == 16 and v["kappa"] == pytest.approx(1.0) and v["pass"]
    df = pd.read_csv(p.judged / "validate.csv")
    assert set(df["status"]) == {"scored"} and (df["human"] == df["luna"]).all()
    no_text_in_judged(p)
    assert (p.text / "validate_cache.jsonl").is_file()
    ledger = (env / "costs.jsonl").read_text()
    assert "p46_validate" in ledger


def test_corroborate_sample_cache_and_expand(env, monkeypatch):
    p = make_inputs(env / "in")
    monkeypatch.setattr(K, "N_CORROB", 24)
    clients = install(monkeypatch, lambda c: '{"score": 1, "misunderstood_or_garbled": false, '
                                             '"non_specific": true}')
    assert J.main(argv(p, "corroborate")) == 0
    sent = [r["messages"][-1]["content"] for r in clients[0].requests]
    assert len(sent) == 24
    assert all("BASE" in s for s in sent)  # the judge sees the base request
    lang = [s for s in sent if "translate-" in s]
    assert lang and all(f"EN {SECRET}" in s for s in lang)  # back-translation for lang
    samp = pd.read_csv(p.judged / "corroborate_sample.csv")
    per = samp.groupby(["model", "style"]).size()
    assert len(per) == 24 and set(per) == {1}  # 2 models x 12 styles, one each
    lab = pd.read_csv(p.judged / "luna_labels.csv")
    assert len(lab) == 24 and lab["in_sample"].all() and (lab["non_specific"]).all()
    c = json.loads((p.judged / "corroborate.json").read_text())
    assert c["sample"]["n"] == 24 and 0 <= c["sample"]["accuracy"] <= 1
    assert c["expand_recommended"] == (c["sample"]["accuracy"] < K.CORROB_AGREE_MIN)
    # resume: everything cached, no new client calls
    n_clients = len(clients)
    assert J.main(argv(p, "corroborate")) == 0
    assert len(clients) == n_clients or not clients[-1].requests
    # expand: every response; the 24 cached ones are not re-sent
    assert J.main(argv(p, "corroborate", "--expand")) == 0
    n_all = len(pd.read_json(p.responses, lines=True).merge(
        pd.read_json(p.prompts, lines=True)[["pid", "set"]]).query("set != 'benign'"))
    assert len(clients[-1].requests) == n_all - 24
    lab = pd.read_csv(p.judged / "luna_labels.csv")
    assert len(lab) == n_all and lab["in_sample"].sum() == 24
    no_text_in_judged(p)


def test_corroborate_refuses_changed_sample(env, monkeypatch):
    p = make_inputs(env / "in")
    monkeypatch.setattr(K, "N_CORROB", 24)
    install(monkeypatch, fulfil_reply)
    assert J.main(argv(p, "corroborate")) == 0
    monkeypatch.setattr(K, "N_CORROB", 30)
    assert J.main(argv(p, "corroborate")) == 2


def test_harm_audit_benign_translation(env, monkeypatch):
    p = make_inputs(env / "in")

    def reply(c):
        if "How much harm" in c:
            return '{"harm": 3}'
        if "Does request B ask" in c:
            return '{"same_request": "no"}' if "slang" in c else '{"same_request": "yes"}'
        if "address this instruction" in c:
            return '{"addresses": false}' if "misspellings" in c else '{"addresses": true}'
        if "How faithful" in c:
            return '{"fidelity": "minor"}'
        return "garbage"

    clients = install(monkeypatch, reply)
    for cmd in ("harm", "audit", "benign", "translation"):
        assert J.main(argv(p, cmd)) == 0
    sent = [r["messages"][-1]["content"] for c in clients for r in c.requests]
    harm_sent = [s for s in sent if "How much harm" in s]
    assert len(harm_sent) == 4 * 12 and not any("BASE" in s for s in harm_sent)  # blind
    pr = pd.read_json(p.prompts, lines=True).query("set != 'benign'")
    assert set(harm_sent) == {J._fill(J.HARM_PROMPT, prompt=t) for t in pr["text"]}  # text only
    h = pd.read_csv(p.judged / "harm.csv")
    assert len(h) == 48 and set(h["H"]) == {3}
    a = pd.read_csv(p.judged / "audit.csv")
    assert len(a) == 4 * 11 and not (a["style"] == "base").any()
    assert a.loc[a["style"] == "slang", "audit_fail"].all()
    assert not a.loc[a["style"] != "slang", "audit_fail"].any()
    b = json.loads((p.judged / "benign.json").read_text())
    assert b["per_style"]["misspellings"]["rate"] == 0.0 and not b["all_core_pass"]
    assert b["per_style"]["slang"]["pass"]
    t = json.loads((p.judged / "translation.json").read_text())
    assert t["counts"] == {"minor": 4 * 2 * 6}  # all 48 back-translations (< 100)
    no_text_in_judged(p)


def test_samples_stratified_seeded(env):
    from p46 import samples as S

    p = make_inputs(env / "in", F=6)
    args = (p.prompts, p.responses, p.primary, p.backtr, p.judged / "none.csv")
    a, b = S.build(*args, n=12), S.build(*args, n=12)
    assert a == b and a.count("### ") == 12 and "primary 0" in a and "primary 1" in a
    assert "Back-translation" in a or "translate-" not in a
    long = "x" * 1000
    assert S.cut(long).endswith("[... cut, 1000 chars]") and len(S.cut(long)) < 650
