"""emx.probes and scripts/probe_index.py: status.json <-> generated INDEX.md, backfill and drift."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from emx import probes as P

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pi = load("probe_index")

# The hand-edited INDEX.md as of 2026-10-11 (before status.json existed), verbatim.
LEGACY_INDEX = """# Probes

The registry of probe-tier experiments (CLAUDE.md, "Probe tier"). Take the next id here when committing a plan on main. Status: Planned / Running / Merged / Killed.

| Id | Question | Cap | Status | Plan | PR | Spend |
|---|---|---|---|---|---|---|
| P00 | Does the generic probe runner work end to end on a real pod? (infrastructure check) | $5 | Merged | [plan](P00-runner-check/plan.md) | [#16](https://github.com/jrepifano/mats12_neel_exploratory/pull/16) | $0.27 |
| P98 | Does the runner salvage a crashed run on a real pod? (infrastructure check) | $5 | Merged | [plan](P98-crash-check/plan.md) | [#17](https://github.com/jrepifano/mats12_neel_exploratory/pull/17) | $0.77 |
| P99 | Does the runner resume after a laptop crash and salvage a hung run at the deadline? (infrastructure check) | $5 | Merged | [plan](P99-deadline-check/plan.md) | [#17](https://github.com/jrepifano/mats12_neel_exploratory/pull/17) | $0.81 |
| P01 | How often does a compaction summary drop facts, invent them, or misstate whether the task is done? | $75 | Running | [plan](P01-compaction-fidelity/plan.md) | — | — |
| P02 | Does lab-style eval-awareness steering reveal hidden misbehaviour, or invent it? | $40 | Running | [plan](P02-evalaware-steering-check/plan.md) | — | — |
| P03 | Can we build interpretability items whose answer is in the model but not in the text, and do readers beat a text-only monitor on them? | $45 | Running | [plan](P03-text-proof-interp-items/plan.md) | — | — |
"""

LEGACY_DIRS = [
    "P00-runner-check",
    "P98-crash-check",
    "P99-deadline-check",
    "P01-compaction-fidelity",
    "P02-evalaware-steering-check",
    "P03-text-proof-interp-items",
]


def legacy_rows(text: str) -> dict[str, str]:
    return {line.split("|")[1].strip(): line for line in text.splitlines() if P.ROW_RE.match(line)}


def status(pid="P04", slug="some-probe", **kw) -> dict:
    d = P.new_status(pid, slug, kw.pop("question", "Does it?"), kw.pop("cap_usd", 75), "p04")
    d.update(kw)
    return P.validate(d)


@pytest.fixture
def legacy(tmp_path) -> Path:
    d = tmp_path / "probes"
    for name in LEGACY_DIRS:
        (d / name).mkdir(parents=True)
        (d / name / "plan.md").write_text("plan\n")
    (d / "INDEX.md").write_text(LEGACY_INDEX, encoding="utf-8")
    return d


# --- library --------------------------------------------------------------------------------


def test_money_formatting():
    assert [P.fmt_cap(x) for x in (75, 75.0, 5, 7.5, 0.25, None)] == [
        "$75",
        "$75",
        "$5",
        "$7.50",
        "$0.25",
        "—",
    ]
    assert [P.fmt_spend(x) for x in (0.27, 1, 12.5, None)] == ["$0.27", "$1.00", "$12.50", "—"]


def test_render_parse_round_trip():
    a = status(
        "P07", "pipes", question="Is a | b?", cap_usd=7.5, pr=21, spend_usd=3.1, status="Merged"
    )
    b = status("P02", "two")
    rows = P.parse_index(P.render_index([a, b]))
    assert list(rows) == ["P02", "P07"]  # sorted by numeric id
    r = rows["P07"]
    assert (r["question"], r["cap_usd"], r["status"], r["plan"], r["pr"], r["spend_usd"]) == (
        "Is a | b?",
        7.5,
        "Merged",
        "P07-pipes/plan.md",
        21,
        3.1,
    )
    back = P.status_from_row(r, base=a)
    assert {k: back[k] for k in P.KEY_ORDER if k != "updated_at"} == {
        k: a[k] for k in P.KEY_ORDER if k != "updated_at"
    }
    assert "\\|" in P.render_row(a)


def test_legacy_index_backfills_cell_identical():
    rows = P.parse_index(LEGACY_INDEX)
    assert len(rows) == 6
    statuses = [P.status_from_row(r) for r in rows.values()]
    by_id = {s["id"]: s for s in statuses}
    assert by_id["P00"]["pr"] == 16 and by_id["P00"]["spend_usd"] == 0.27
    assert by_id["P99"]["pr"] == 17 and by_id["P01"]["cap_usd"] == 75
    assert by_id["P03"]["pr"] is None and by_id["P03"]["status"] == "Running"
    assert all(s["owner"] is None and s["plan"] == "plan.md" for s in statuses)
    rendered = legacy_rows(P.render_index(statuses))
    assert rendered == legacy_rows(LEGACY_INDEX)
    assert P.drift(LEGACY_INDEX, statuses) == ([], [])


def test_status_json_format_and_extra_keys(tmp_path):
    d = status()
    d["note"] = "kept"
    d = {"zzz": 1, **d}  # extras move after the schema keys, values kept
    f = tmp_path / "P04-some-probe" / "status.json"
    f.parent.mkdir()
    P.write_status(f, d)
    text = f.read_text()
    assert text == json.dumps(P.validate(d), indent=2, ensure_ascii=False) + "\n"
    loaded = P.load_status(f)
    assert list(loaded)[: len(P.KEY_ORDER)] == list(P.KEY_ORDER)
    assert loaded["note"] == "kept" and loaded["zzz"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "P4"},
        {"slug": "Bad_Slug"},
        {"status": "Done"},
        {"status": "Planned(held)"},
        {"status": "Planned ()"},
        {"status": "Planned (a (b))"},
        {"status": "Held (x)"},
        {"pr": "16"},
        {"cap_usd": -1},
        {"updated_at": "2026-10-11 14:00"},
        {"plan": "../x.md"},
        {"question": "two\nlines"},
    ],
)
def test_validate_rejects(bad):
    d = status()
    d.update(bad)
    with pytest.raises(P.ProbeError):
        P.validate(d)


def test_load_status_checks_dir_name(tmp_path):
    f = tmp_path / "P05-other" / "status.json"
    f.parent.mkdir()
    P.write_status(f, status())
    with pytest.raises(P.ProbeError, match="does not match"):
        P.load_status(f)


def test_drift_reports_missing_and_differing():
    a, b = status("P01", "a"), status("P02", "b")
    text = P.render_index([a, b])
    changed = dict(b, status="Killed")
    assert P.drift(text, [changed]) == (["P01"], ["P02"])


def test_next_free_id_skips_reserved_and_used():
    assert P.next_free_id(set()) == "P01"
    assert P.next_free_id({"P01", "P02", "P04"}) == "P03"
    with pytest.raises(P.ProbeError):
        P.next_free_id(set(P.CLAIMABLE))
    assert P.ids_in_names(["P05-x", "INDEX.md", "P5-y", "TEMPLATE.md"]) == {"P05"}


# --- CLI ------------------------------------------------------------------------------------


def run(d: Path, *args: str) -> int:
    return pi.main(["--probes-dir", str(d), *args])


def test_cli_backfills_then_check_passes(legacy, capsys):
    assert run(legacy, "--check") == 1  # header and row order change
    assert not (legacy / "P00-runner-check" / "status.json").exists()  # --check writes nothing
    assert run(legacy) == 0
    out = capsys.readouterr().out
    assert out.count("backfilled") == 6
    for name in LEGACY_DIRS:
        assert P.load_status(legacy / name / "status.json")["id"] == name[:3]
    text = (legacy / "INDEX.md").read_text()
    assert legacy_rows(text) == legacy_rows(LEGACY_INDEX)
    assert "do not edit by hand" in text
    assert [r for r in P.parse_index(text)] == ["P00", "P01", "P02", "P03", "P98", "P99"]
    assert run(legacy, "--check") == 0
    assert run(legacy) == 0  # idempotent


def test_cli_differing_row_needs_a_side(legacy, capsys):
    assert run(legacy) == 0
    # A pre-change branch hand-edits P01's row at close-out.
    idx = legacy / "INDEX.md"
    old_row = legacy_rows(idx.read_text())["P01"]
    new_row = old_row.replace("| Running |", "| Merged |").replace(
        "| — | — |",
        "| [#30](https://github.com/jrepifano/mats12_neel_exploratory/pull/30) | $12.34 |",
    )
    idx.write_text(idx.read_text().replace(old_row, new_row))
    capsys.readouterr()
    assert run(legacy) == 1
    assert "P01" in capsys.readouterr().err
    assert run(legacy, "--check") == 1
    assert run(legacy, "--prefer-index") == 0
    s = P.load_status(legacy / "P01-compaction-fidelity" / "status.json")
    assert (s["status"], s["pr"], s["spend_usd"]) == ("Merged", 30, 12.34)
    assert legacy_rows(idx.read_text())["P01"] == new_row
    assert run(legacy, "--check") == 0


def test_cli_prefer_index_ids_take_only_named_rows(legacy):
    """A stale branch table: its own P01 row is newer, its P02 row is older than main's status."""
    assert run(legacy) == 0
    p02 = legacy / "P02-evalaware-steering-check" / "status.json"
    s02 = P.load_status(p02)
    s02.update(status="Merged", pr=40, spend_usd=3.5)  # main's newer status for P02
    P.write_status(p02, s02)
    idx = legacy / "INDEX.md"
    old_row = legacy_rows(idx.read_text())["P01"]
    idx.write_text(idx.read_text().replace(old_row, old_row.replace("| Running |", "| Merged |")))
    assert run(legacy, "--prefer-index", "P01") == 0
    assert P.load_status(legacy / "P01-compaction-fidelity" / "status.json")["status"] == "Merged"
    assert P.load_status(p02)["status"] == "Merged"  # not overwritten by the stale row
    assert "| Merged | [plan](P02" in idx.read_text()
    assert run(legacy, "--check") == 0


def test_cli_prefer_status_overwrites_row(legacy):
    assert run(legacy) == 0
    idx = legacy / "INDEX.md"
    idx.write_text(idx.read_text().replace("| $40 |", "| $41 |"))
    assert run(legacy, "--prefer-status") == 0
    assert "| $40 |" in idx.read_text()
    assert P.load_status(legacy / "P02-evalaware-steering-check" / "status.json")["cap_usd"] == 40


def test_cli_dir_without_status_or_row_is_skipped(legacy, capsys):
    (legacy / "P05-untracked-plan").mkdir()
    assert run(legacy) == 0
    assert "P05-untracked-plan" in capsys.readouterr().err
    assert not (legacy / "P05-untracked-plan" / "status.json").exists()
    assert "P05" not in P.parse_index((legacy / "INDEX.md").read_text())


def test_cli_new_status_adds_row(legacy):
    assert run(legacy) == 0
    f = legacy / "P04-some-probe" / "status.json"
    f.parent.mkdir()
    P.write_status(f, status())
    assert run(legacy, "--check") == 1
    assert run(legacy) == 0
    assert "P04" in P.parse_index((legacy / "INDEX.md").read_text())


def test_status_with_note_is_kept_verbatim(legacy):
    d = status(status="Planned (held)")
    assert P.base_status(d["status"]) == "Planned" and P.base_status("Running") == "Running"
    row = P.parse_index(P.render_index([d]))["P04"]
    assert row["status"] == "Planned (held)"
    assert "| Planned (held) |" in P.render_row(d)
    assert P.status_from_row(row)["status"] == "Planned (held)"
    # A legacy INDEX row with a note backfills verbatim and re-renders identically.
    idx = legacy / "INDEX.md"
    idx.write_text(idx.read_text().replace("| $40 | Running |", "| $40 | Planned (held) |"))
    assert run(legacy) == 0
    s = P.load_status(legacy / "P02-evalaware-steering-check" / "status.json")
    assert s["status"] == "Planned (held)"
    assert "| $40 | Planned (held) |" in idx.read_text()
    assert run(legacy, "--check") == 0


def test_cli_status_edit_regenerates_without_a_flag(legacy, capsys):
    """Review r1 finding 4: the normal path (edit status.json, regenerate) needs no flag."""
    assert run(legacy) == 0
    f = legacy / "P01-compaction-fidelity" / "status.json"
    s = P.load_status(f)
    s.update(status="Merged", pr=31, spend_usd=9.5)
    P.write_status(f, s)
    capsys.readouterr()
    assert run(legacy) == 0
    assert "P01: row regenerated from status.json" in capsys.readouterr().out
    row = legacy_rows((legacy / "INDEX.md").read_text())["P01"]
    assert "| Merged |" in row and "[#31]" in row and "$9.50" in row
    assert run(legacy, "--check") == 0


def test_note_link_and_cap_note_round_trip():
    """Rows hand-edited on main before the generator existed: a note link and a cap note."""
    row = (
        "| P06 | What? | $45 (raised from $35) | Merged | [plan](P06-filler/plan.md), "
        "[note](P06-filler/note.md) | [#20](https://github.com/jrepifano/mats12_neel_exploratory"
        "/pull/20) | $13.00 |"
    )
    text = P.render_index([]) + row + "\n"
    s = P.status_from_row(P.parse_index(text)["P06"])
    assert (s["cap_usd"], s["cap_note"], s["note"], s["spend_usd"]) == (45, "raised from $35",
                                                                         "note.md", 13)
    assert P.render_row(s) == row
    plain = dict(s)
    del plain["note"], plain["cap_note"]
    assert "[note]" not in P.render_row(plain) and "| $45 |" in P.render_row(plain)


def test_spend_note_round_trip():
    row = (
        "| P07 | Q? | $75 | Merged | [plan](P07-x/plan.md) | [#23](https://github.com/jrepifano/"
        "mats12_neel_exploratory/pull/23) | $32.06 ledger ($30.01 after removing duplicate charges) |"
    )
    s = P.status_from_row(P.parse_index(P.render_index([]) + row + "\n")["P07"])
    assert s["spend_usd"] == 32.06
    assert s["spend_note"] == "ledger ($30.01 after removing duplicate charges)"
    assert P.render_row(s) == row
