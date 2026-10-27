"""Plan-code drift guard for the week-2 experiments.

Each experiment has one config, configs/week2/<exp>.toml, holding every design parameter its
proposal states. The proposal carries a "Build contract" table (`| parameter | value |`) that
the config must match, and the config records the proposal's sha256 it was built against
(`meta.built_against`). `gate` refuses to launch past any difference; `accept_plan` re-records
the hash (explicitly, never automatically) and logs the changed sections for the builder to
account for. scripts/week2_plan_check.py is the CLI.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from compaction.s2.chain import config as CC
from compaction.s2.config import REPO_ROOT

ACCEPTED_DIR = REPO_ROOT / "configs" / "week2" / "accepted"
BUILD_LOG = REPO_ROOT / "logs" / "s2" / "week2_build_log.md"
NO_ACCEPTED_COPY = "(no accepted copy; whole proposal)"
PREAMBLE = "(preamble)"


class PlanDrift(RuntimeError):
    """The config and the proposal disagree; the message names every difference."""


# ------------------------------------------------------------------------------------------
# Schema: section -> key -> type. Types: "str" | "int" | "float" | "bool" | "list[str]" |
# "list[int]" | "file" ({path, sha256}) | a tuple of allowed strings. A section's "*" key
# allows any key name. Extend experiments here.
# ------------------------------------------------------------------------------------------

SCHEMA: dict[str, dict] = {
    "meta": {"exp": "str", "proposal": "str", "built_against": "str"},
    "design": {"kind": ("two_phase", "chain", "precedent", "monitor_controls", "exploit"),
               "stage": "str",
               "seed": "int",
               "n_phases": "int", "log_reasoning": "bool"},
    "two_phase": {"repos": "list[str]", "arms": "list[str]", "starts": "list[str]",
                  "projects": "list[str]", "histories_per_arm": "int", "a_retries": "int",
                  "plain_fresh_per_task": "int", "permission_after_call": "int",
                  "checkin_calls": "list[int]"},
    "chain": {"orders": "file", "arms": "list[str]", "boundary": ("compacted", "full"),
              "auto_compact_tokens": "int", "update_prompt_from_boundary": "int",
              "fork_after_every_phase": "bool",
              # experiment 4 (chain stress); optional, so the N = 5 stub's config stays valid
              "unrelated_chains": "int", "within_task_chains": "int",
              "within_task_auto_compact_tokens": "int", "within_task_orders_from": "str",
              "related_repeats": "int", "related_repeats_max": "int", "fresh_controls": "int",
              "ticket_tags": "bool", "interim_after_unrelated_chains": "int",
              "save_state_after": "list[str]"},
    "precedent": {"repos": "list[str]", "arms": "list[str]", "per_task": "list[int]",
                  "rounds": "int", "starts": "list[str]", "project": ("different",),
                  "a_retries": "int", "permission_after_call": "int",
                  "checkin_calls": "list[int]", "first_stage_min": "int",
                  "assignment": "file", "checkpoint_on": "list[str]", "checkpoint_max": "int",
                  "second_tasks_after_gate": "bool", "spillover_acts": "list[str]",
                  "interim_after_second_tasks": "int"},
    # experiment 2 (monitors): the 24 control runs' keys, then the offline scorer's
    "monitors": {"control_states": "int", "control_openings": "list[str]",
                 "control_project": ("different",), "control_starts_full": "int",
                 "control_starts_compacted": "int", "controls_stop_after_pairs": "int",
                 "controls_stop_min_authorized_push": "int", "assignment": "file",
                 "host": ("laptop", "rig"), "agent_cpus": "int", "ramp_agents": "int",
                 "ramp_after_s": "int",
                 "prompts": "list[str]",
                 "models": "list[str]", "views": "list[str]", "primary_acts": "int",
                 "test_retest_items": "int", "concurrency": "int", "max_window_pause": "float",
                 "second_scenario": "str", "p38_natural_acts": "int",
                 "p38_false_licence_acts": "int", "p38_authorized_items": "int",
                 "p38_policy_in_every_view": "bool", "third_scenario": "str"},
    # experiment 4's exploitation ending (week2-build-c): a 6th, impossible task forked from
    # the chains' saved states
    "exploit": {"tasks": "list[str]", "draw": "draw_table", "languages": "list[str]",
                "mutation": "str", "opening_line": "str", "no_test_rule": "bool",
                "position": "int", "cells": "list[str]", "smoke": "int",
                "related_chains": "str", "signs_of_life_points": "int", "task_files": "str",
                "validation": "str", "claim_scoring": "str"},
    "caps": {"stage_usd": "float", "run_usd": "float", "max_calls": "int", "max_seconds": "int"},
    "parallelism": {"agents": "int", "fallback_agents": "int", "agent_memory_mb": "int",
                    "fallback_agent_memory_mb": "int"},
    "wording": {"*": "file"},
}
KIND_SECTIONS = {"two_phase": "two_phase", "chain": "chain", "precedent": "precedent",
                 "monitor_controls": "monitors", "exploit": "exploit"}  # design.kind -> its own section
# A kind's own shape for a shared section: a dict replaces SCHEMA's keys, None makes the
# section optional (validated against SCHEMA if present).
KIND_OVERRIDES: dict[str, dict] = {"exploit": {"caps": {"exploit_usd": "float"},
                                               "parallelism": None}}
# Keys a config may leave out (a later experiment's keys in a shared section); the plan check
# still reports any the contract lists and the config lacks.
OPTIONAL = {"chain": {"unrelated_chains", "within_task_chains", "within_task_auto_compact_tokens",
                      "within_task_orders_from", "related_repeats", "related_repeats_max",
                      "fresh_controls", "ticket_tags", "interim_after_unrelated_chains",
                      "save_state_after"}}
# Chain arms whose chains may be shorter than design.n_phases: related chains run each repo's
# tasks (2..n), a fresh control is one task.
SHORT_ARMS = {"related": (2, None), "fresh": (1, 1)}
NOT_CONTRACT = ("meta", "_source")  # flatten skips these
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
FILE_KEYS = ("path", "sha256")
PHASE_PROJECTS = ("fork", "switch")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def _type_ok(v, t) -> str | None:
    """None when `v` has type `t`, else what was expected."""
    if isinstance(t, tuple):
        return None if v in t else f"one of {list(t)}"
    ok = {"str": lambda: isinstance(v, str),
          "int": lambda: isinstance(v, int) and not isinstance(v, bool),
          "float": lambda: isinstance(v, int | float) and not isinstance(v, bool),
          "bool": lambda: isinstance(v, bool),
          "list[str]": lambda: isinstance(v, list) and all(isinstance(x, str) for x in v),
          "list[int]": lambda: isinstance(v, list) and all(
              isinstance(x, int) and not isinstance(x, bool) for x in v),
          "draw_table": lambda: isinstance(v, dict) and set(v) == {"seed", "path"}
          and isinstance(v["seed"], int) and not isinstance(v["seed"], bool)
          and isinstance(v["path"], str),
          "file": lambda: isinstance(v, dict) and set(v) == set(FILE_KEYS)
          and all(isinstance(v[k], str) for k in FILE_KEYS) and bool(SHA_RE.match(v["sha256"]))}
    return None if ok[t]() else {"file": "a table {path, sha256 (64 hex)}"}.get(t, t)


def _check_file(where: str, spec: dict, errors: list[str]) -> Path | None:
    p = resolve(spec["path"])
    if not p.is_file():
        errors.append(f"{where}: file not found: {spec['path']}")
        return None
    got = sha256_file(p)
    if got != spec["sha256"]:
        errors.append(f"{where}: sha256 mismatch for {spec['path']}: config {spec['sha256']}, "
                      f"file {got}")
        return None
    return p


def _check_orders(p: Path, cfg: dict, errors: list[str]) -> None:
    n, arms = cfg["design"]["n_phases"], cfg["chain"]["arms"]
    try:
        chains = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        errors.append(f"chain.orders: not JSON: {e}")
        return
    if not isinstance(chains, list) or not chains:
        errors.append("chain.orders: must be a non-empty list of chains")
        return
    ids = Counter()
    for i, ch in enumerate(chains):
        where = f"chain.orders[{i}]"
        if not isinstance(ch, dict) or set(ch) != {"id", "arm", "phases"}:
            errors.append(f"{where}: must have exactly keys id, arm, phases")
            continue
        ids[ch["id"]] += 1
        if not isinstance(ch["id"], str):
            errors.append(f"{where}: id must be a string")
        if ch["arm"] not in arms:
            errors.append(f"{where} ({ch['id']}): arm {ch['arm']!r} not in chain.arms {arms}")
        ph = ch["phases"]
        lo, hi = SHORT_ARMS.get(ch["arm"], (n, n))
        hi = n if hi is None else hi
        if not isinstance(ph, list) or not lo <= len(ph) <= hi:
            got = len(ph) if isinstance(ph, list) else type(ph).__name__
            want = f"design.n_phases = {n}" if (lo, hi) == (n, n) else \
                f"arm {ch['arm']!r} takes {lo}..{hi}"
            errors.append(f"{where} ({ch['id']}): {got} phases, {want}")
            continue
        for j, x in enumerate(ph):
            if not isinstance(x, dict) or not isinstance(x.get("task"), str):
                errors.append(f"{where}.phases[{j}]: needs a string task")
            elif j > 0 and x.get("project") not in PHASE_PROJECTS:
                errors.append(f"{where}.phases[{j}]: project must be one of {PHASE_PROJECTS}")
    errors += [f"chain.orders: duplicate chain id {k!r}" for k, c in ids.items() if c > 1]


def validate(cfg: dict) -> None:
    """Strict check against SCHEMA plus the cross-field rules; ValueError lists every error."""
    errors: list[str] = []
    kind = cfg.get("design", {}).get("kind") if isinstance(cfg.get("design"), dict) else None
    for sec in cfg:
        if sec not in SCHEMA:
            errors.append(f"unknown section [{sec}]")
    overrides = KIND_OVERRIDES.get(kind, {})
    for sec, keys in SCHEMA.items():
        if sec in overrides:
            if overrides[sec] is None and sec not in cfg:
                continue
            keys = overrides[sec] or keys
        own_kind = [k for k, s in KIND_SECTIONS.items() if s == sec]
        if own_kind and kind != own_kind[0]:
            if sec in cfg:
                errors.append(f"section [{sec}] not allowed when design.kind = {kind!r}")
            continue
        if sec not in cfg:
            errors.append(f"missing section [{sec}]")
            continue
        body = cfg[sec]
        if not isinstance(body, dict):
            errors.append(f"[{sec}] must be a table")
            continue
        if "*" in keys:
            if not body:
                errors.append(f"[{sec}] needs at least one entry")
            for k, v in body.items():
                if (exp := _type_ok(v, keys["*"])) is not None:
                    errors.append(f"{sec}.{k}: expected {exp}, got {v!r}")
            continue
        errors += [f"unknown key {sec}.{k}" for k in body if k not in keys]
        errors += [f"missing key {sec}.{k}" for k in keys
                   if k not in body and k not in OPTIONAL.get(sec, ())]
        for k, t in keys.items():
            if k in body and (exp := _type_ok(body[k], t)) is not None:
                errors.append(f"{sec}.{k}: expected {exp}, got {body[k]!r}")
    if errors:
        raise ValueError("config invalid:\n  " + "\n  ".join(errors))
    # Cross-field rules (types are known good from here).
    if not SHA_RE.match(cfg["meta"]["built_against"]):
        errors.append("meta.built_against: must be a 64-hex sha256")
    n = cfg["design"]["n_phases"]
    if n < 2:
        errors.append(f"design.n_phases: must be >= 2, got {n}")
    if kind in ("two_phase", "precedent", "monitor_controls") and n != 2:
        errors.append(f"design.n_phases: must be 2 for {kind}, got {n}")
    for name, spec in cfg["wording"].items():
        _check_file(f"wording.{name}", spec, errors)
    if kind == "chain":
        p = _check_file("chain.orders", cfg["chain"]["orders"], errors)
        if p is not None and n >= 2:
            _check_orders(p, cfg, errors)
    if kind == "precedent":
        _check_file("precedent.assignment", cfg["precedent"]["assignment"], errors)
    if kind == "monitor_controls":
        _check_file("monitors.assignment", cfg["monitors"]["assignment"], errors)
    if errors:
        raise ValueError("config invalid:\n  " + "\n  ".join(errors))


def stage_cap(cfg: dict) -> float:
    """The stage cap a config's caps file holds: caps.stage_usd, or the exploitation ending's
    caps.exploit_usd."""
    caps = cfg["caps"]
    return float(caps["exploit_usd"] if cfg["design"]["kind"] == "exploit" else caps["stage_usd"])


def load_config(path: str | Path) -> dict:
    """Read and strictly validate a week-2 config. The returned dict also carries
    `_source = {path, sha256}` (the config file itself), for the design card."""
    import tomllib

    p = resolve(path)
    raw = p.read_bytes()
    cfg = tomllib.loads(raw.decode("utf-8"))
    validate(cfg)
    cfg["_source"] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
    return cfg


# ------------------------------------------------------------------------------------------
# Normalization and the contract
# ------------------------------------------------------------------------------------------

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


def _num(s: str) -> str | None:
    """Canonical number string, or None. Strips a leading `$` and thousands commas
    ("1,700" with no spaces); "170", "170.0" and "$170" all give "170"."""
    s = s.strip()
    s = s.removeprefix("$")
    if _THOUSANDS_RE.match(s):
        s = s.replace(",", "")
    if not _NUM_RE.match(s):
        return None
    f = float(s)
    return str(int(f)) if f.is_integer() else repr(f)


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return _num(str(v)) or str(v)
    return str(v)


def flatten(cfg: dict) -> dict[str, str]:
    """Dotted key -> normalized string for every contract key (all but [meta])."""
    out: dict[str, str] = {}
    for sec, body in cfg.items():
        if sec in NOT_CONTRACT:
            continue
        for k, v in body.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    out[f"{sec}.{k}.{kk}"] = _scalar(vv)
            elif isinstance(v, list):
                out[f"{sec}.{k}"] = ", ".join(_scalar(x) for x in v)
            else:
                out[f"{sec}.{k}"] = _scalar(v)
    return out


def canon(value: str) -> tuple[str, ...]:
    """Comparison form of a contract or config value: a number canonically, else the
    comma-separated items, each stripped (and canonical if numeric)."""
    v = value.strip().strip("`").strip()
    n = _num(v)
    if n is not None:
        return (n,)
    if v == "":
        return ()
    items = [x.strip().strip("`").strip() for x in v.split(",")]
    return tuple(_num(x) or x for x in items)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _cells(line: str) -> list[str]:
    s = line.strip()
    s = s.removeprefix("|")
    s = s.removesuffix("|")
    return [c.strip().replace("`", "").strip() for c in s.split("|")]


def parse_contract(markdown_text: str) -> dict[str, str]:
    """The first `| parameter | value |` table under the "Build contract" heading."""
    lines = markdown_text.splitlines()
    start = level = None
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m and m.group(2).strip().lower() == "build contract":
            start, level = i + 1, len(m.group(1))
            break
    if start is None:
        raise ValueError('no "Build contract" heading')
    end = len(lines)
    for i in range(start, len(lines)):
        m = _HEADING_RE.match(lines[i])
        if m and len(m.group(1)) <= level:
            end = i
            break
    body = lines[start:end]
    t = next((i for i, ln in enumerate(body) if ln.strip().startswith("|")), None)
    if t is None:
        raise ValueError('no table under "Build contract"')
    if [c.lower() for c in _cells(body[t])] != ["parameter", "value"]:
        raise ValueError(f"Build contract table header must be `parameter | value`, got "
                         f"{body[t].strip()!r}")
    out: dict[str, str] = {}
    dupes = []
    for ln in body[t + 2:]:
        if not ln.strip().startswith("|"):
            break
        c = _cells(ln)
        if len(c) != 2:
            raise ValueError(f"Build contract row needs 2 cells: {ln.strip()!r}")
        if c[0] in out:
            dupes.append(c[0])
        out[c[0]] = c[1]
    if dupes:
        raise ValueError(f"Build contract: duplicate parameters {sorted(set(dupes))}")
    return out


# ------------------------------------------------------------------------------------------
# Sections, the check, the gate, accepting a plan
# ------------------------------------------------------------------------------------------


def sections(markdown_text: str) -> dict[str, str]:
    """Heading text -> body, split at every `#` heading; repeated headings get " (2)" etc."""
    out: dict[str, str] = {}
    name, buf = PREAMBLE, []
    seen = Counter()

    def flush():
        if name != PREAMBLE or any(x.strip() for x in buf):
            out[name] = "\n".join(buf).strip()

    for ln in markdown_text.splitlines():
        m = _HEADING_RE.match(ln)
        if m:
            flush()
            h = m.group(2).strip()
            seen[h] += 1
            name, buf = (h if seen[h] == 1 else f"{h} ({seen[h]})"), []
        else:
            buf.append(ln)
    flush()
    return out


def changed_sections(old_text: str, new_text: str) -> list[str]:
    """Headings whose bodies changed or were added (in the new order), then removed ones."""
    old, new = sections(old_text), sections(new_text)
    out = [h for h, b in new.items() if old.get(h) != b]
    return out + [h for h in old if h not in new]


def accepted_copy(exp: str, sha: str, accepted_dir: Path = ACCEPTED_DIR) -> Path:
    return Path(accepted_dir) / f"{exp}-{sha[:12]}.md"


def _changed_vs_accepted(cfg: dict, text: str, accepted_dir: Path) -> list[str]:
    old = accepted_copy(cfg["meta"]["exp"], cfg["meta"]["built_against"], accepted_dir)
    if not old.is_file():
        return [NO_ACCEPTED_COPY]
    return changed_sections(old.read_text(), text)


def plan_check(proposal_path: str | Path, config_path: str | Path,
               accepted_dir: Path = ACCEPTED_DIR) -> dict:
    cfg = load_config(config_path)
    p = resolve(proposal_path)
    text = p.read_text()
    sha = sha256_file(p)
    contract = parse_contract(text)
    flat = flatten(cfg)
    mismatches = [{"parameter": k, "contract": contract[k], "config": flat[k]}
                  for k in flat if k in contract and canon(contract[k]) != canon(flat[k])]
    missing_in_config = [k for k in contract if k not in flat]
    missing_in_contract = [k for k in flat if k not in contract]
    built = cfg["meta"]["built_against"]
    hash_ok = sha == built
    changed = [] if hash_ok else _changed_vs_accepted(cfg, text, accepted_dir)
    ok = hash_ok and not (mismatches or missing_in_config or missing_in_contract)
    return {"ok": ok, "exp": cfg["meta"]["exp"], "proposal": str(proposal_path),
            "config": str(config_path), "mismatches": mismatches,
            "missing_in_config": missing_in_config, "missing_in_contract": missing_in_contract,
            "proposal_sha256": sha, "built_against": built, "hash_ok": hash_ok,
            "changed_sections": changed}


def report(check: dict) -> str:
    """Human-readable form of a plan_check result."""
    out = [(f"{'OK' if check['ok'] else 'DRIFT'}: {check['exp']} "
            f"({check['proposal']} vs {check['config']})")]
    out += [f"  mismatch {m['parameter']}: contract {m['contract']!r}, config {m['config']!r}"
            for m in check["mismatches"]]
    out += [f"  in the contract, not in the config: {k}" for k in check["missing_in_config"]]
    out += [f"  in the config, not in the contract: {k}" for k in check["missing_in_contract"]]
    if not check["hash_ok"]:
        out.append(f"  proposal changed: built against {check['built_against']}, "
                   f"now {check['proposal_sha256']}")
        out += [f"    changed section: {h}" for h in check["changed_sections"]]
        out.append(f"  after accounting for the changes: scripts/week2_plan_check.py "
                   f"--accept-plan {check['proposal_sha256']} {check['proposal']} "
                   f"{check['config']}")
    return "\n".join(out)


def gate(proposal_path: str | Path, config_path: str | Path,
         accepted_dir: Path = ACCEPTED_DIR) -> dict:
    """plan_check, raising PlanDrift on any difference."""
    check = plan_check(proposal_path, config_path, accepted_dir)
    if not check["ok"]:
        raise PlanDrift(report(check))
    return check


def plan_diff(proposal_path: str | Path, config_path: str | Path,
              accepted_dir: Path = ACCEPTED_DIR) -> str:
    """The unified diff from the accepted copy of the proposal to its current text: what goes
    into an astra code-review prompt next to the code diff."""
    import difflib
    cfg = load_config(config_path)
    old = accepted_copy(cfg["meta"]["exp"], cfg["meta"]["built_against"], accepted_dir)
    if not old.is_file():
        return f"{NO_ACCEPTED_COPY}: {old}\n"
    new = resolve(proposal_path)
    return "".join(difflib.unified_diff(old.read_text().splitlines(True),
                                        new.read_text().splitlines(True),
                                        fromfile=f"accepted/{old.name}", tofile=str(new)))


_BUILT_RE = re.compile(r'^(built_against\s*=\s*)"[^"\n]*"', re.MULTILINE)


def accept_plan(proposal_path: str | Path, config_path: str | Path, sha: str,
                log_path: Path = BUILD_LOG, accepted_dir: Path = ACCEPTED_DIR) -> dict:
    """Re-record the proposal hash the config is built against. `sha` must equal the
    proposal's current sha256. Rewrites only the built_against line, stores the accepted
    copy and appends a delta-table template to the build log. Does not touch the contract
    check: gate still compares every parameter."""
    p = resolve(proposal_path)
    cur = sha256_file(p)
    if sha != cur:
        raise ValueError(f"--accept-plan sha {sha} is not the proposal's current sha256 {cur}")
    cfg = load_config(config_path)
    exp, old = cfg["meta"]["exp"], cfg["meta"]["built_against"]
    text = p.read_text()
    changed = [] if old == cur else _changed_vs_accepted(cfg, text, accepted_dir)
    cp = resolve(config_path)
    src = cp.read_bytes().decode("utf-8")
    new_src, n = _BUILT_RE.subn(lambda m: f'{m.group(1)}"{cur}"', src)
    if n != 1:
        raise ValueError(f"{config_path}: expected one built_against line, found {n}")
    cp.write_bytes(new_src.encode("utf-8"))
    copy = accepted_copy(exp, cur, accepted_dir)
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(p.read_bytes())
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    head = "" if log_path.exists() else "# Week-2 build log\n"
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    rows = "".join(f"| {h} | |\n" for h in changed)
    entry = (f"\n### {now} plan accepted: {exp}\n\n"
             f"- proposal: {proposal_path}\n- config: {config_path}\n"
             f"- {old} → {cur}\n"
             f"- changed sections: {', '.join(changed) if changed else '(none)'}\n\n"
             f'| Clause | Commit, or "no code impact: <why>" |\n|---|---|\n{rows}')
    with log_path.open("a") as f:
        f.write(head + entry)
    return {"exp": exp, "old": old, "new": cur, "changed_sections": changed,
            "accepted_copy": str(copy), "log": str(log_path)}


# ------------------------------------------------------------------------------------------
# Two-phase runs (mirrors config.cells() and config.b_runs(), driven by the config)
# ------------------------------------------------------------------------------------------


def two_phase_runs(cfg: dict) -> list[CC.BRun]:
    tp = cfg["two_phase"]
    unknown = [r for r in tp["repos"] if r not in CC.REPOS]
    if unknown:
        raise ValueError(f"two_phase.repos: unknown repos {unknown} (known {list(CC.REPOS)})")
    out: list[CC.BRun] = []
    for repo in tp["repos"]:
        b_repo = CC.REPOS[repo]
        b = b_repo.b_task
        if tp["plain_fresh_per_task"] > 0:
            c = CC.Cell(b, "none", "different", "fresh")
            out += [CC.BRun(c, None, k) for k in range(1, tp["plain_fresh_per_task"] + 1)]
        for arm in tp["arms"]:
            for pj in tp["projects"]:
                # a fresh start in a different project is the plain run: no history to carry
                starts = tp["starts"] if pj == "same" else [s for s in tp["starts"]
                                                           if s != "fresh"]
                a_repo = b_repo if pj == "same" else CC.other_repo(repo)
                for s in starts:
                    c = CC.Cell(b, arm, pj, s)
                    out += [CC.BRun(c, CC.History(a_repo.name, arm, k).id, k)
                            for k in range(1, tp["histories_per_arm"] + 1)]
    return out


def two_phase_rows(cfg: dict) -> list[dict]:
    """two_phase_runs in design_card's `runs` shape."""
    rows = []
    for r in two_phase_runs(cfg):
        tasks = [r.cell.b_task] if r.history is None else \
            [CC.REPOS[r.history.split("-")[0]].a_task, r.cell.b_task]
        rows.append({"id": r.id, "arm": r.cell.hist, "start": r.cell.start,
                     "project": r.cell.project, "tasks": tasks})
    return rows


# ------------------------------------------------------------------------------------------
# Design card: what the code will actually run
# ------------------------------------------------------------------------------------------


def design_card(cfg: dict, runs: list[dict], out_path: str | Path) -> str:
    src = cfg.get("_source", {})
    d, caps, par = cfg["design"], cfg["caps"], cfg["parallelism"]
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Design card: {cfg['meta']['exp']}", "",
           f"- config: {src.get('path', '?')} (sha256 {src.get('sha256', '?')})",
           f"- proposal: {cfg['meta']['proposal']} (built against {cfg['meta']['built_against']})",
           f"- kind: {d['kind']}, stage {d['stage']}, {d['n_phases']} phases, seed {d['seed']}",
           f"- written: {now}", "", "## Runs", "",
           "| arm | start | project | runs |", "|---|---|---|---|"]
    counts = Counter((r["arm"], r["start"], r["project"]) for r in runs)
    out += [f"| {a} | {s} | {p} | {n} |" for (a, s, p), n in counts.items()]
    out.append(f"| **total** | | | **{len(runs)}** |")
    for key in ("arm", "start", "project"):
        c = Counter(r[key] for r in runs)
        out.append(f"\nBy {key}: " + ", ".join(f"{k} {n}" for k, n in c.items()))
    out += ["", "## Task order", "", "| tasks | arms | runs |", "|---|---|---|"]
    seqs: dict[tuple, list] = {}
    for r in runs:
        seqs.setdefault(tuple(r["tasks"]), []).append(r["arm"])
    out += [f"| {' → '.join(t)} | {', '.join(dict.fromkeys(a))} | {len(a)} |"
            for t, a in seqs.items()]
    out += ["", "## Wording", "", "| name | path | sha256 |", "|---|---|---|"]
    out += [f"| {k} | {v['path']} | {v['sha256']} |" for k, v in cfg["wording"].items()]
    if d["kind"] == "chain":
        o = cfg["chain"]["orders"]
        out += ["", f"Orders: {o['path']} (sha256 {o['sha256']})"]
    if d["kind"] == "precedent":
        pr = cfg["precedent"]
        o = pr["assignment"]
        out += ["", f"Assignment: {o['path']} (sha256 {o['sha256']}); per first task "
                + ", ".join(f"{a} {n}" for a, n in zip(pr["arms"], pr["per_task"], strict=True))
                + f"; {pr['rounds']} launch rounds; first-stage gate >= {pr['first_stage_min']} "
                "divergent histories"]
    out += ["", f"Reasoning text logged: {'yes' if d['log_reasoning'] else 'no'}"]
    out += ["", "## Caps and parallelism", "",
            (f"- stage ${_scalar(caps['stage_usd'])}, per run ${_scalar(caps['run_usd'])}, "
             f"{caps['max_calls']} calls, {caps['max_seconds']} s"),
            (f"- agents {par['agents']} at {par['agent_memory_mb']} MB, fallback "
             f"{par['fallback_agents']} at {par['fallback_agent_memory_mb']} MB"),
            f"- seed {d['seed']}", ""]
    text = "\n".join(out)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return text


# ------------------------------------------------------------------------------------------
# Binding a config to the code that will run it (astra r1 B1)
# ------------------------------------------------------------------------------------------

# The two-phase driver's frozen texts, by the wording names a two_phase config uses.
TWO_PHASE_WORDING = {"task_ending": "TASK_ENDING", "permission_line": "PERMISSION_LINE",
                     "lead_same": "LEAD_SAME", "lead_different": "LEAD_DIFFERENT",
                     "checkins": "CHECKINS", "teammate_message": "TEAMMATE_MESSAGE",
                     "junk_commits": "JUNK_COMMITS", "teammate_text_httpx": "teammate_text:httpx",
                     "teammate_text_tengo": "teammate_text:tengo"}


def two_phase_code_mismatches(cfg: dict) -> list[str]:
    """Where a two_phase config differs from what the 2-phase driver will actually run (its
    constants in compaction.s2.chain.config): run list, histories, check-ins, seed, stage,
    caps and every frozen text. Empty = the code runs exactly this design. A design change
    that the code doesn't implement yet is refused, never silently run as the old design."""
    out = []
    if cfg["design"]["kind"] != "two_phase":
        return [f"design.kind is {cfg['design']['kind']!r}; the 2-phase driver runs two_phase"]
    tp, caps = cfg["two_phase"], cfg["caps"]
    try:
        ids = [b.id for b in two_phase_runs(cfg)]
    except ValueError as e:
        ids = None
        out.append(f"two_phase runs: {e}")
    if ids is not None and ids != [b.id for b in CC.b_runs()]:
        out.append(f"two_phase runs: the config gives {len(ids)} runs, the code "
                   f"{len(CC.b_runs())} (or a different order)")
    for key, code in (("design.seed", CC.SEED), ("design.stage", CC.STAGE),
                      ("two_phase.histories_per_arm", CC.A_HISTORIES_PER_CONDITION),
                      ("two_phase.a_retries", CC.A_RETRIES),
                      ("two_phase.arms", list(CC.PERMS)), ("two_phase.starts", list(CC.STARTS)),
                      ("two_phase.permission_after_call", CC.PERMISSION_AFTER_CALL),
                      ("two_phase.checkin_calls", list(CC.CHECKIN_CALLS)),
                      ("caps.stage_usd", CC.CAP_USD), ("caps.run_usd", CC.RUN_CAP_USD),
                      ("caps.max_calls", CC.MAX_CALLS), ("caps.max_seconds", CC.MAX_SECONDS)):
        sec, k = key.split(".")
        v = {"design": cfg["design"], "two_phase": tp, "caps": caps}[sec][k]
        if v != code:
            out.append(f"{key}: config {v!r}, code {code!r}")
    hashes = CC.text_hashes()
    names = set(cfg.get("wording") or {})
    if names != set(TWO_PHASE_WORDING):
        out.append(f"wording names {sorted(names)} != the code's {sorted(TWO_PHASE_WORDING)}")
    for name, code_key in TWO_PHASE_WORDING.items():
        w = (cfg.get("wording") or {}).get(name)
        if w and w["sha256"] != hashes[code_key]:
            out.append(f"wording.{name}: config sha256 {w['sha256'][:12]}, code "
                       f"{hashes[code_key][:12]}")
    return out


def fingerprint(cfg: dict) -> dict:
    """What a run's state is bound to: a resumed launch with another fingerprint is refused
    (astra r1 B4)."""
    return {"exp": cfg["meta"]["exp"], "config_sha256": cfg["_source"]["sha256"],
            "proposal_sha256": cfg["meta"]["built_against"]}


# ------------------------------------------------------------------------------------------
# Experiment 1 (precedent): the assignment and the code it runs
# ------------------------------------------------------------------------------------------

# The texts the precedent design uses (task chaining's, minus the same-project lead-in).
PRECEDENT_WORDING = {k: v for k, v in TWO_PHASE_WORDING.items() if k != "lead_same"}


def precedent_assignment(cfg: dict) -> dict:
    """The seeded assignment the config's [precedent] parameters give."""
    from compaction.s2.chain import precedent as P
    pr = cfg["precedent"]
    return P.assign(pr["repos"], pr["arms"], pr["per_task"], pr["rounds"], cfg["design"]["seed"])


def precedent_runs(cfg: dict) -> tuple[list[CC.History], list[CC.BRun]]:
    from compaction.s2.chain import precedent as P
    a = precedent_assignment(cfg)
    return P.histories(a), P.b_runs(a, cfg["precedent"]["starts"])


def precedent_rows(cfg: dict) -> list[dict]:
    """precedent_runs in design_card's `runs` shape (first tasks and second-task runs)."""
    from compaction.s2.chain import precedent as P
    hists, runs = precedent_runs(cfg)
    rows = [{"id": h.id, "arm": P.arm_of(h), "start": "first task", "project": "-",
             "tasks": [h.a_task]} for h in hists]
    rows += [{"id": r.id, "arm": r.cell.hist, "start": r.cell.start, "project": r.cell.project,
              "tasks": [CC.REPOS[r.history.split("-")[0]].a_task, r.cell.b_task]} for r in runs]
    return rows


def precedent_code_mismatches(cfg: dict) -> list[str]:
    """Where a precedent config differs from what the driver will run: the committed
    assignment must be exactly what the parameters and seed give, the arms and starts must be
    ones the code implements, and the first task's check-ins, retries, caps and every frozen
    text must equal the code's. Empty = the code runs exactly this design."""
    from compaction.s2.chain import precedent as P
    if cfg["design"]["kind"] != "precedent":
        return [f"design.kind is {cfg['design']['kind']!r}, not 'precedent'"]
    pr, caps, out = cfg["precedent"], cfg["caps"], []
    try:
        want = precedent_assignment(cfg)
    except ValueError as e:
        return [f"precedent assignment: {e}"]
    got = json.loads(resolve(pr["assignment"]["path"]).read_text())
    if got.get("order") != want["order"]:
        out.append(f"precedent.assignment: {pr['assignment']['path']} is not what seed "
                   f"{cfg['design']['seed']} and the [precedent] parameters give (regenerate it "
                   "with scripts/week2_assign.py)")
    from compaction.s2.chain.session import CHECKPOINT_ON
    odd = [x for x in pr["checkpoint_on"] if x not in CHECKPOINT_ON]
    if odd or pr["checkpoint_max"] < 0:
        out.append(f"precedent.checkpoint_on {pr['checkpoint_on']} / checkpoint_max "
                   f"{pr['checkpoint_max']}: the code knows {list(CHECKPOINT_ON)}, max >= 0")
    if pr["second_tasks_after_gate"] is not True:
        out.append("precedent.second_tasks_after_gate: the driver only holds every second task "
                   "until all first tasks are resolved and the gate passes (true)")
    if sorted(pr["spillover_acts"]) != sorted(P.SPILLOVER_ACTS):
        out.append(f"precedent.spillover_acts {pr['spillover_acts']}: the scorer's columns are "
                   f"{list(P.SPILLOVER_ACTS)}")
    if pr["interim_after_second_tasks"] < 1:
        out.append("precedent.interim_after_second_tasks: must be >= 1 (a descriptive look)")
    bad = [s for s in pr["starts"] if s not in ("full", "compacted")]
    if bad or not pr["starts"]:
        out.append(f"precedent.starts: {pr['starts']}; the code runs full and compacted")
    unknown = [a for a in pr["arms"] if a not in P.ARMS]
    if unknown:
        out.append(f"precedent.arms: unknown {unknown} (the code knows {list(P.ARMS)})")
    # The stage and its cap are the config's own (driver.apply_stage); `go` checks them against
    # the checkout's caps file instead.
    for key, code in (("precedent.a_retries", CC.A_RETRIES),
                      ("precedent.permission_after_call", CC.PERMISSION_AFTER_CALL),
                      ("precedent.checkin_calls", list(CC.CHECKIN_CALLS)),
                      ("caps.run_usd", CC.RUN_CAP_USD),
                      ("caps.max_calls", CC.MAX_CALLS), ("caps.max_seconds", CC.MAX_SECONDS)):
        sec, k = key.split(".")
        v = {"precedent": pr, "caps": caps}[sec][k]
        if v != code:
            out.append(f"{key}: config {v!r}, code {code!r}")
    hashes = CC.text_hashes()
    names = set(cfg.get("wording") or {})
    if names != set(PRECEDENT_WORDING):
        out.append(f"wording names {sorted(names)} != the code's {sorted(PRECEDENT_WORDING)}")
    for name, code_key in PRECEDENT_WORDING.items():
        w = (cfg.get("wording") or {}).get(name)
        if w and w["sha256"] != hashes[code_key]:
            out.append(f"wording.{name}: config sha256 {w['sha256'][:12]}, code "
                       f"{hashes[code_key][:12]}")
    return out
