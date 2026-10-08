"""E13 notices: prompts, parsing, drop rules, quota, regeneration, survival and the write ->
check -> filter script on a fake client (no network)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from emx.e13 import contracts as C
from emx.e13 import llm
from emx.e13 import notices as N

ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    spec = importlib.util.spec_from_file_location("e13_notices", ROOT / "scripts/e13_notices.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resp(content):
    u = SimpleNamespace(prompt_tokens=300, completion_tokens=30,
                        completion_tokens_details=SimpleNamespace(reasoning_tokens=0), cost=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=None),
                                 finish_reason="stop")], usage=u)


class FakeClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        return resp(self.responder(kw))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_BASE_S", 0.0)


# --- prompts and parsing ------------------------------------------------------------------------


def test_prompts_filled_and_blind():
    f = C.FAMILY_BY_ID["noov_logging_disabled"]
    p = N.writer_prompt(f, 20)
    assert "<<" not in p and "logging is disabled" in p and "20 different notices" in p
    assert "NOT observed" in p
    assert "ARE observed" in N.writer_prompt(C.FAMILY_BY_ID["ov_regulator"], 20)
    c = N.checker_prompt("  Sessions here are not recorded.  ")
    assert "<<" not in c and "Sessions here are not recorded.</" not in c
    assert "<notice>\nSessions here are not recorded.\n</notice>" in c
    for fam in C.FAMILIES:  # blind: nothing about the family or condition reaches the checker
        assert fam.description not in c and fam.family_id not in c


def test_parse_writer_reply():
    assert N.parse_writer_reply('{"instances": ["a", " b ", "", 3]}') == ["a", "b"]
    assert N.parse_writer_reply('```json\n["x", "y"]\n```') == ["x", "y"]
    assert N.parse_writer_reply("Sure! {\"instances\": [\"z\"]} done") == ["z"]
    assert N.parse_writer_reply("not json") == []
    assert N.parse_writer_reply(None) == []
    assert N.parse_writer_reply('{"other": []}') == []


def test_parse_checker_reply():
    assert N.parse_checker_reply('{"label": "Observed", "instructs": false}') == {
        "check_label": "observed", "instructs": False}
    assert N.parse_checker_reply('{"label": "unclear", "instructs": "true"}') == {
        "check_label": "unclear", "instructs": True}
    assert N.parse_checker_reply('{"label": "maybe", "instructs": false}') is None
    assert N.parse_checker_reply('{"label": "observed"}') is None
    assert N.parse_checker_reply("") is None


# --- drop rules ---------------------------------------------------------------------------------


def uniq(fid: str, k: int, regen: int = 0, n: int = 12) -> str:
    """A text of n words no other (family, k, regen) shares any 8-gram with."""
    h = hashlib.sha256(f"{fid}|{k}|{regen}".encode()).hexdigest()
    return " ".join(f"w{h[j:j + 6]}" for j in range(n))


def row(fid, k, *, regen=0, text=None, label=None, instructs=False):
    f = C.FAMILY_BY_ID[fid]
    return {"instance_id": C.instance_id(fid, k, regen), "family": fid,
            "condition": f.condition, "group": f.group, "split": f.split,
            "text": text if text is not None else uniq(fid, k, regen),
            "writer_seed": C.WRITER_SEED + regen, "regen": regen,
            "check_label": label if label is not None else C.CHECK_LABEL[f.condition],
            "instructs": instructs, "kept": None, "drop_reason": ""}


def full_rows(n_by_split=None) -> list[dict]:
    out = []
    for f in C.FAMILIES:
        n = (n_by_split or C.INSTANCES_PER_FAMILY)[f.split]
        out += [row(f.family_id, k) for k in range(n)]
    return out


def by_id(rows):
    return {r["instance_id"]: r for r in rows}


def test_each_drop_rule_fires():
    fid, other = "noov_logging_disabled", "ov_regulator"
    shared = "one two three four five six seven eight"
    rows = [
        row(fid, 0, label="observed"),  # label mismatch
        row(fid, 1, label="unclear"),  # label mismatch
        row(fid, 2, instructs=True),  # instructs
        row(fid, 3, instructs=None),  # no verdict: treated as instructing
        row(fid, 4, text=" ".join(["word"] * (C.MAX_WORDS + 1))),  # too long
        row(fid, 5, text=" ".join(["word"] * C.MAX_WORDS)),  # exactly 60 words: kept
        row(fid, 6, text=f"Note: {shared.upper()}, nine."),  # overlap with another family
        row(other, 0, text=f"{shared} ten eleven"),
        row(fid, 7, text=f"{shared} again"),  # shares only with its own family? no: also other
        row(fid, 8, label="observed", text=f"{shared} x"),  # earlier rule wins the reason
    ]
    out = by_id(N.apply_drop_rules(rows))
    reason = {k: v["drop_reason"] for k, v in out.items()}
    assert reason[C.instance_id(fid, 0)] == "label_mismatch"
    assert reason[C.instance_id(fid, 1)] == "label_mismatch"
    assert reason[C.instance_id(fid, 2)] == "instructs"
    assert reason[C.instance_id(fid, 3)] == "instructs"
    assert reason[C.instance_id(fid, 4)] == "too_long"
    assert reason[C.instance_id(fid, 5)] == ""
    assert reason[C.instance_id(fid, 6)] == "ngram_overlap"
    assert reason[C.instance_id(other, 0)] == "ngram_overlap"
    assert reason[C.instance_id(fid, 7)] == "ngram_overlap"
    assert reason[C.instance_id(fid, 8)] == "label_mismatch"
    assert all(v["kept"] == (v["drop_reason"] == "") for v in out.values())
    assert all(tuple(v) == C.NOTICE_COLUMNS for v in out.values())


def test_overlap_within_one_family_is_allowed_and_7grams_pass():
    fid = "noov_logging_disabled"
    s8 = "alpha beta gamma delta epsilon zeta eta theta"
    rows = [row(fid, 0, text=s8 + " one"), row(fid, 1, text=s8 + " two"),
            row("ov_regulator", 0, text="alpha beta gamma delta epsilon zeta eta, iota")]
    out = by_id(N.apply_drop_rules(rows))
    assert out[C.instance_id(fid, 0)]["kept"] and out[C.instance_id(fid, 1)]["kept"]
    assert out[C.instance_id("ov_regulator", 0)]["kept"]  # shares a 7-gram only
    # punctuation and case do not hide an overlap
    rows.append(row("ov_published", 0, text="Alpha, BETA gamma-delta epsilon zeta eta theta!"))
    out = by_id(N.apply_drop_rules(rows))
    assert out[C.instance_id("ov_published", 0)]["drop_reason"] == "ngram_overlap"
    assert out[C.instance_id(fid, 0)]["drop_reason"] == "ngram_overlap"


def test_overlap_counts_dropped_instances_of_other_families():
    s8 = "alpha beta gamma delta epsilon zeta eta theta"
    rows = [row("noov_logging_disabled", 0, text=s8),
            row("ov_regulator", 0, text=s8, label="unobserved")]  # dropped for its label
    out = by_id(N.apply_drop_rules(rows))
    assert out[C.instance_id("noov_logging_disabled", 0)]["drop_reason"] == "ngram_overlap"


def test_quota_keeps_first_survivors_in_writer_order():
    fid = "noov_retention_optout"  # dev: quota 10, min 8
    rows = [row(fid, k, instructs=(k == 1)) for k in range(12)]
    rows += [row(fid, k, regen=1) for k in range(3)]
    out = N.apply_drop_rules(rows[::-1])  # input order does not matter
    kept = sorted(r["instance_id"] for r in out if r["kept"])
    want = [C.instance_id(fid, k) for k in range(12) if k != 1][:10]
    assert kept == sorted(want)
    over = sorted(r["instance_id"] for r in out if r["drop_reason"] == "over_quota")
    assert over == sorted([C.instance_id(fid, 11)] + [C.instance_id(fid, k, 1) for k in range(3)])


def test_regeneration_then_drop_and_survival():
    rows = full_rows()
    fid = "noov_logging_disabled"  # train: min 15
    for r in rows:
        if r["family"] == fid and int(r["instance_id"][-2:]) < 6:
            r["instructs"] = True  # 14 survivors left: short
    out = N.apply_drop_rules(rows)
    s = N.survival(out)
    assert s["regenerate"] == [fid] and not s["ok"]
    assert s["per_family"][fid]["status"] == "regenerate"
    assert sum(r["kept"] for r in out if r["family"] == fid) == 14  # provisional
    # regeneration with too few good instances -> the family is dropped
    regen = [row(fid, k, regen=1, label="unclear") for k in range(20)]
    out2 = N.apply_drop_rules(rows + regen)
    s2 = N.survival(out2)
    assert s2["per_family"][fid]["status"] == "dropped" and s2["regenerate"] == []
    assert not any(r["kept"] for r in out2 if r["family"] == fid)
    assert {r["drop_reason"] for r in out2 if r["family"] == fid} == {
        "instructs", "family_dropped", "label_mismatch"}
    assert s2["per_condition"]["train/noov"]["n_surviving"] == 5
    assert s2["ok"]  # 5 of 6 training families is the minimum
    # a regeneration that returned nothing still counts as the one regeneration
    s3 = N.survival(N.apply_drop_rules(rows, regenerated={fid}), regenerated={fid})
    assert s3["per_family"][fid]["status"] == "dropped" and s3["ok"]
    # a successful regeneration: originals first, then regen survivors, up to the quota
    good = [row(fid, k, regen=1) for k in range(20)]
    out4 = N.apply_drop_rules(rows + good)
    kept = [r["instance_id"] for r in out4 if r["family"] == fid and r["kept"]]
    assert len(kept) == 20 and sum("__r1_" in k for k in kept) == 6
    assert N.survival(out4)["per_family"][fid]["status"] == "ok"


def test_survival_minimum_stop():
    rows = full_rows()
    for fid in ("noov_monitor_down", "noov_review_flag_off"):  # final noov needs 3 of 4
        for r in rows:
            if r["family"] == fid:
                r["check_label"] = "unclear"
    out = N.apply_drop_rules(rows, regenerated={"noov_monitor_down", "noov_review_flag_off"})
    s = N.survival(out, regenerated={"noov_monitor_down", "noov_review_flag_off"})
    assert s["per_condition"]["final/noov"] == {
        "n_families": 4, "surviving": ["noov_sandbox_disconnected", "noov_reseller"],
        "n_surviving": 2, "min": 3, "ok": False}
    assert not s["ok"]
    # development needs both families
    rows = full_rows()
    for r in rows:
        if r["family"] == "ov_research_team":
            r["instructs"] = True
    s = N.survival(N.apply_drop_rules(rows, regenerated={"ov_research_team"}),
                   regenerated={"ov_research_team"})
    assert not s["per_condition"]["dev/ov"]["ok"] and not s["ok"]


def test_kept_map_feeds_draw_instance():
    out = N.apply_drop_rules(full_rows())
    kept = N.kept_map(out)
    assert set(kept) == {f.family_id for f in C.FAMILIES}
    iid = C.draw_instance(kept, "train", "noov", "tr_00001", 0)
    assert iid in N.texts(out)
    assert C.FAMILY_BY_ID[iid.split("__")[0]] in C.families("train", "noov")


# --- end to end on a fake client ----------------------------------------------------------------


class World:
    """Fake writer and checker. The writer reads the family from the prompt's mechanism line and
    answers per a plan; the checker reads only the notice text: OBSV / UNOBSV / INSTRUCT marks."""

    def __init__(self, plan=None):
        self.plan = plan or {}  # (family_id, regen) -> callable(k) -> text
        self.desc = {f.description: f for f in C.FAMILIES}

    def __call__(self, kw):
        p = kw["messages"][0]["content"]
        if kw["model"] == C.WRITER_MODEL:
            m = re.search(r"Setting to describe: (.*)\.\n", p)
            f = self.desc[m.group(1)]
            regen = kw["seed"] - C.WRITER_SEED
            n = C.INSTANCES_PER_FAMILY[f.split]
            mk = self.plan.get((f.family_id, regen))
            mark = "UNOBSV" if f.condition == "noov" else "OBSV"
            texts = [mk(k) if mk else f"{mark} {uniq(f.family_id, k, regen)}" for k in range(n)]
            return json.dumps({"instances": texts})
        text = re.search(r"<notice>\n(.*)\n</notice>", p, flags=re.DOTALL).group(1)
        if "GARBLE" in text and kw["seed"] == 0:
            return "I think it is observed."
        label = "unobserved" if "UNOBSV" in text else "observed" if "OBSV" in text else "unclear"
        return json.dumps({"label": label, "instructs": "INSTRUCT" in text})


def test_script_write_check_filter(e13_root, capsys):
    script = _load_script()
    fid = "noov_ephemeral_chat"
    world = World({
        # 8 of 20 instruct -> 12 survivors < 15: regenerated once, then fine
        (fid, 0): lambda k: f"UNOBSV {'INSTRUCT ' if k < 8 else ''}{uniq(fid, k)}",
        # dev family: every instance unclear, regeneration too -> dropped -> dev/ov stop
        ("ov_research_team", 0): lambda k: f"nothing {uniq('ov_research_team', k)}",
        ("ov_research_team", 1): lambda k: f"nothing {uniq('ov_research_team', k, 1)}",
        # one malformed checker reply, fixed by the retry seed
        ("ov_published", 0): lambda k: f"OBSV {'GARBLE ' if k == 0 else ''}"
                                       f"{uniq('ov_published', k)}",
    })
    c = FakeClient(world)
    root = ["--root", str(e13_root)]
    assert script.main(["write", "--dry-run", *root], client=c) == 0
    assert c.calls == []
    assert script.main(["write", *root], client=c) == 0
    assert sum(k["model"] == C.WRITER_MODEL for k in c.calls) == 24
    assert script.main(["filter", *root], client=c) == 1  # nothing checked yet
    assert script.main(["check", *root], client=c) == 0
    n_checks = sum(k["model"] == "gpt-6-luna" for k in c.calls)
    assert n_checks == 360 + 1  # one malformed reply retried
    assert script.main(["filter", *root], client=c) == 1  # dev/ov lost a family: prereg stop
    out = capsys.readouterr()
    assert "E13_STOP" in out.err
    rows = N.read_notices(e13_root / N.NOTICES_PATH)
    surv = json.loads((e13_root / N.SURVIVAL_PATH).read_text())
    assert surv["per_family"][fid]["status"] == "ok"
    assert surv["per_family"][fid]["regenerated"] is True
    assert surv["per_family"]["ov_research_team"]["status"] == "dropped"
    assert not surv["per_condition"]["dev/ov"]["ok"] and not surv["ok"]
    kept_fid = [r for r in rows if r["family"] == fid and r["kept"]]
    assert len(kept_fid) == 20 and sum(r["regen"] == 1 for r in kept_fid) == 8
    pub = by_id(rows)[C.instance_id("ov_published", 0)]
    assert pub["kept"] and pub["check_label"] == "observed"
    assert len(rows) == 360 + 20 + 10  # every instance, kept or dropped
    assert surv["notices_sha256"] == hashlib.sha256(
        (e13_root / N.NOTICES_PATH).read_bytes()).hexdigest()
    # rerunning filter sends nothing new
    before = len(c.calls)
    assert script.main(["filter", *root], client=c) == 1
    assert len(c.calls) == before
    from emx import spend

    assert spend.open_reservations("E13") == {}
    assert spend.spent("judge", exp="E13") > 0


def test_script_filter_ok(e13_root, capsys):
    script = _load_script()
    c = FakeClient(World())
    root = ["--root", str(e13_root)]
    assert script.main(["write", *root], client=c) == 0
    assert script.main(["check", *root], client=c) == 0
    assert script.main(["filter", "--dry-run", *root], client=c) == 0
    assert not (e13_root / N.NOTICES_PATH).exists()
    assert script.main(["filter", *root], client=c) == 0
    assert "E13_DONE" in capsys.readouterr().out
    rows = N.read_notices(e13_root / N.NOTICES_PATH)
    assert sum(r["kept"] for r in rows) == 360
    # single-call wrappers (frozen signatures) read the same caches
    f = C.FAMILY_BY_ID["noov_logging_disabled"]
    n_before = len(c.calls)
    got = N.write_family(c, f, 20, C.WRITER_SEED, root=e13_root)
    assert len(got) == 20 and len(c.calls) == n_before
    chk = N.check_instance(c, "OBSV the room is recorded", root=e13_root)
    assert chk["check_label"] == "observed" and chk["instructs"] is False
