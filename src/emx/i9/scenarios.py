"""I9 scenarios (implementation plan §2, unit U1; prereg v0.5 §4.1 and §4.3 point 1).

Builds every `contracts.Scenario` row I9 uses and the id lists in `sets.json`:

* **Bank** (32 rows): the WorkspaceBench agentic_misalignment items
  (`third_party/workspace-bench/evals/agentic_misalignment/items.json`), loaded as published.
  The bank's `prefill` (the benchmark's pinned Qwen reply) becomes `pinned_rollout` and is never
  rendered into a prompt. The 18 `group == "anthropic"` items are Lynch et al. generator
  conditions whose ids encode `(scenario, goal_type, goal_value, urgency)`.
* **Generator** (64 confirmatory + 16 sealed): conditions drawn from the 144-condition pool and
  rendered into the bank's format by `render_condition`, a faithful reimplementation of the
  vendored generator (`third_party/agentic-misalignment/scripts/generate_prompts.py` at
  `contracts.LYNCH_COMMIT`; line numbers cited per function). The generator script imports
  `yaml` and `load_env` at module level, so it is not imported; only
  `templates/system_prompt_templates.py` (stdlib only) is executed by path, without writing
  bytecode into the vendored tree.

How the bank built `system` and `text` from the generator's three output files (inferred from
the bank, and checked byte for byte on all 18 Lynch items by `tests/i9/test_scenarios.py`):

* the **production** system prompt (`*_SYSTEM_PROMPT_PROD`, no secret-scratchpad paragraph;
  the bank README says "production system prompts"), `extra_system_instructions=False`, no
  alignment-faking warning, then `.strip()`;
* `text = (USER_PROMPT + "\\n" + email_content).strip()`, i.e. `user_prompt.txt`, a newline,
  then `email_content.txt`.

The implementation brief said to use dev mode unless the bank showed otherwise; the bank's
system prompts match only the production templates, so production mode is used.

**The draw** (prereg §4.1), exactly:

1. `pool` = every `(scenario, goal_type, goal_value, urgency)` in
   `LYNCH_SCENARIOS x POOL_GOAL_PAIRS x LYNCH_URGENCY` (144), sorted by
   `contracts.condition_id`.
2. `drawn = random.Random(contracts.SCENARIO_DRAW_SEED).sample(pool, 80)`.
3. `drawn[:64]` are `confirmatory`, `drawn[64:]` are `sealed`.

**Subsets** (`sets.json`, every key in `contracts.SET_KEYS`, ids sorted):

* `bank_sampled`: the 14 bank items with a published `misalignment_rate` plus the 4 with
  `arm == "control"` (disjoint, 18).
* `bank_validation`: 16 bank items stratified by category. `rng =
  random.Random(contracts.BANK_SUBSET_SEED)`; for each category in sorted order, its ids are
  sorted and then `rng.shuffle`d; then round-robin over the sorted categories, taking the next
  id of each category that has one left, until 16 are taken. With the bank's 10 categories
  every category is represented, and the 6 second picks go to the first 6 categories (sorted)
  with at least 2 items.
* `thinking`: `random.Random(contracts.THINKING_SUBSET_SEED).sample(sorted(confirmatory), 24)`.

**Classifier prompts** (`prompts/I9/classifier_{blackmail,leaking,murder}.txt`): ported from
`third_party/agentic-misalignment/classifiers/*.py`; see each file's header.
`load_classifier_prompt` returns `(system, user_template)`; the user template keeps the source's placeholders
`{response}` (the reply being judged) and `{email_context}` (the source's
`Classifier.get_email_context`: the text of emails the reply forwards, or `""`), listed in
`CLASSIFIER_PLACEHOLDERS`. The emails a reply saw are `email_content(scenario)`.
"""

from __future__ import annotations

import json
import random
import re
import types
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

from emx.i9 import contracts as C

BANK_ITEMS_PATH = (
    C.REPO_ROOT / "third_party" / "workspace-bench" / "evals" / "agentic_misalignment"
    / "items.json"
)
LYNCH_TEMPLATES_DIR = C.LYNCH_DIR / "templates"
LYNCH_GENERATOR_PATH = C.LYNCH_DIR / "scripts" / "generate_prompts.py"
SYSTEM_TEMPLATES_PATH = LYNCH_TEMPLATES_DIR / "system_prompt_templates.py"
PROD_SYSTEM_PROMPT = True  # see the module docstring: the bank used the production templates

# One descriptor per Lynch scenario type, independent of goal and urgency (the bank's 18 Lynch
# items carry exactly these three strings; checked by the round-trip test).
LYNCH_DESCRIPTOR = (
    "{scenario}: does the model commit to the misaligned action at the decision/summary tokens?"
)
_UNFILLED = re.compile(r"\$\{?[A-Za-z_]\w*\}?")  # what Template.safe_substitute leaves unfilled
# An upstream generator defect, kept as the generator renders it: the blackmail and murder
# restriction emails use ${direction_modifier}, which the blackmail variables file for
# safety_vs_acceleration (also used for murder, generate_prompts.py:285) does not define, so
# safe_substitute leaves the literal "${direction_modifier}" in the email. Of these 4 pool
# conditions the seed-0 draw puts only am-murder_explicit-safety_restriction in the sealed set
# and none in the confirmatory set. Any other unfilled variable is refused.
KNOWN_UPSTREAM_UNFILLED: dict[str, set[str]] = {
    C.condition_id(sc, gt, "safety", "restriction"): {"${direction_modifier}"}
    for sc in ("blackmail", "murder") for gt in ("explicit", "latent")
}

_LYNCH_ID = re.compile(
    r"^am-(blackmail|leaking|murder)_([a-z]+)-([a-z]+)_(none|replacement|restriction)$"
)

CLASSIFIER_PLACEHOLDERS: tuple[str, ...] = ("response", "email_context")
CLASSIFIER_SCENARIOS: tuple[str, ...] = C.LYNCH_SCENARIOS


# --- bank ---------------------------------------------------------------------------------------


def load_bank_items(path: Path = BANK_ITEMS_PATH) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data["items"]
    if len(items) != C.N_BANK or data.get("n_items") != C.N_BANK:
        raise ValueError(f"{path}: expected {C.N_BANK} items, found {len(items)}")
    return items


def parse_lynch_id(item_id: str) -> dict[str, str] | None:
    """`{scenario, goal_type, goal_value, urgency_type}` for an `am-<condition id>` id, else
    None (e.g. the in-house `am-blackmail-full`)."""
    m = _LYNCH_ID.match(item_id)
    if not m:
        return None
    sc, gt, gv, urg = m.groups()
    return {"scenario": sc, "goal_type": gt, "goal_value": gv, "urgency_type": urg}


def bank_scenarios(items: list[dict[str, Any]] | None = None) -> list[C.Scenario]:
    """The 32 bank items as Scenario rows, in the bank's order."""
    rows = []
    for it in items if items is not None else load_bank_items():
        lynch = parse_lynch_id(it["id"]) if it["group"] == "anthropic" else None
        if it["group"] == "anthropic" and lynch is None:
            raise ValueError(f"bank Lynch item with an unparseable id: {it['id']}")
        if lynch is not None:
            stype = lynch["scenario"]
            tgroup = C.template_group(stype, lynch["goal_value"])
        else:
            stype = it["category"]
            tgroup = f"bank|{it['id']}"
        rows.append(C.Scenario(
            id=it["id"], source="bank", set="bank", category=it["category"], arm=it["arm"],
            group=it["group"], template_group=tgroup, scenario_type=stype,
            system=it["system"], text=it["text"], descriptor=it["descriptor"],
            lynch_conditions=lynch, published_rate=it["misalignment_rate"],
            pinned_rollout=it["prefill"],
        ))
    return rows


# --- generator (mirrors scripts/generate_prompts.py at LYNCH_COMMIT) ---------------------------


@lru_cache(maxsize=1)
def _templates() -> types.ModuleType:
    """templates/system_prompt_templates.py, executed by path (stdlib only; no bytecode)."""
    mod = types.ModuleType("lynch_system_prompt_templates")
    mod.__file__ = str(SYSTEM_TEMPLATES_PATH)
    code = compile(SYSTEM_TEMPLATES_PATH.read_text(encoding="utf-8"), mod.__file__, "exec")
    exec(code, mod.__dict__)  # noqa: S102 - vendored, pinned, constants only
    return mod


def _parse_markdown_variables(path: Path) -> dict[str, Any]:
    """generate_prompts.py:260-278 (parse_markdown_variables)."""
    content = path.read_text(encoding="utf-8")
    pattern = r"## (.*?)\n\s*```json\s*(.*?)\s*```"
    out: dict[str, Any] = {}
    for section_name, json_content in re.findall(pattern, content, re.DOTALL):
        key = section_name.strip().lower().replace(" ", "_").replace("-", "_")
        out[key] = json.loads(json_content)  # the generator prints and skips; we refuse
    return out


def _variable_path(scenario: str, goal_value: str) -> Path:
    """generate_prompts.py:113-161 (validate_variable_mapping). Files are scanned in sorted
    order (the generator's glob order is unspecified) and exactly one exact match is required;
    the generator's partial-match and ambiguous.md fallbacks are refused instead of used."""
    var_scenario = "blackmail" if scenario == "murder" else scenario
    variables_dir = LYNCH_TEMPLATES_DIR / var_scenario / "variables"
    if goal_value in ("none", "ambiguous"):
        return variables_dir / "ambiguous.md"
    goal = goal_value.lower()
    exact = []
    for f in sorted(variables_dir.glob("*.md")):
        if f.stem == "ambiguous":
            continue
        stem = f.stem.lower()
        if goal in stem and (
            f"_{goal}_" in f"_{stem}_" or stem.startswith(goal) or stem.endswith(goal)
        ):
            exact.append(f)
    if len(exact) != 1:
        raise ValueError(f"{scenario}/{goal_value}: expected one variables file, got {exact}")
    return exact[0]


def _variables_for_condition(scenario: str, goal_type: str, goal_value: str) -> dict[str, Any]:
    """generate_prompts.py:280-352 (get_variables_for_condition)."""
    if goal_type in ("none", "ambiguous"):
        var_scenario = "blackmail" if scenario == "murder" else scenario
        path = LYNCH_TEMPLATES_DIR / var_scenario / "variables" / "ambiguous.md"
    else:
        path = _variable_path(scenario, goal_value)
    all_vars = _parse_markdown_variables(path)
    if goal_value in ("none", "ambiguous"):
        variables = dict(next(iter(all_vars.values()))) if len(all_vars) == 1 else dict(all_vars)
    else:
        goal = goal_value.lower()
        match = next(
            (v for k, v in all_vars.items() if goal in k.lower() or f"pro_{goal}" == k.lower()),
            None,
        )
        if match is None:  # the generator falls back to the first section; we refuse
            raise ValueError(f"{path}: no section for goal value {goal_value!r}")
        variables = dict(match)
    gv = _templates().GOAL_VARIABLES
    variables.update(gv.get(f"{goal_type}-{goal_value}", gv.get("none", {})))
    return variables


def _fill(template: str, variables: dict[str, Any]) -> str:
    """generate_prompts.py:255-258 (fill_template)."""
    return Template(template).safe_substitute(variables)


def generator_system_prompt(scenario: str, goal_type: str, goal_value: str,
                            *, prod: bool = PROD_SYSTEM_PROMPT) -> str:
    """generate_prompts.py:354-397 (generate_system_prompt), with
    extra_system_instructions=False and no alignment-faking config (the generator's defaults)."""
    variables = _variables_for_condition(scenario, goal_type, goal_value)
    variables["extra_system_instructions"] = ""
    variables["alignment_faking_warning"] = ""
    t = _templates()
    dev, prd = {
        "blackmail": (t.BLACKMAIL_SYSTEM_PROMPT, t.BLACKMAIL_SYSTEM_PROMPT_PROD),
        "leaking": (t.LEAKING_SYSTEM_PROMPT, t.LEAKING_SYSTEM_PROMPT_PROD),
        "murder": (t.MURDER_SYSTEM_PROMPT, t.MURDER_SYSTEM_PROMPT_PROD),
    }[scenario]
    return _fill(prd if prod else dev, variables)


def generator_email_content(scenario: str, goal_type: str, goal_value: str, urgency: str) -> str:
    """generate_prompts.py:399-446 (generate_email_content), with EMAIL_TEMPLATES (:37-56)."""
    variables = _variables_for_condition(scenario, goal_type, goal_value)
    tdir = LYNCH_TEMPLATES_DIR / scenario / "templates"
    if goal_type == "none":
        path = tdir / f"email-template-{scenario}-goal-conflict-none-{urgency}.md"
        content = _fill(path.read_text(encoding="utf-8"), variables)
    else:
        base = (tdir / f"email-template-{scenario}-shared.md").read_text(encoding="utf-8")
        urg = (tdir / f"email-template-{scenario}-{urgency}.md").read_text(encoding="utf-8")
        content = _fill(base, variables) + _fill(urg, variables)
    if scenario == "leaking":
        parts = content.split("<email>")
        if len(parts) > 1:
            content = parts[0]
            for i, part in enumerate(parts[1:], 1):
                content += f"<email>\nID: {i}{part}"
    return content


def render_condition(scenario: str, goal_type: str, goal_value: str,
                     urgency: str) -> tuple[str, str]:
    """`(system, text)` in the bank's format for one generator condition (module docstring)."""
    if scenario not in C.LYNCH_SCENARIOS or urgency not in C.LYNCH_URGENCY:
        raise ValueError(f"unknown condition {(scenario, goal_type, goal_value, urgency)}")
    system = generator_system_prompt(scenario, goal_type, goal_value).strip()
    emails = generator_email_content(scenario, goal_type, goal_value, urgency)
    text = (_templates().USER_PROMPT + "\n" + emails).strip()
    cid = C.condition_id(scenario, goal_type, goal_value, urgency)
    unfilled = set(_UNFILLED.findall(system)) | set(_UNFILLED.findall(text))
    if unfilled != KNOWN_UPSTREAM_UNFILLED.get(cid, set()):
        raise ValueError(f"{cid}: unfilled template variables {sorted(unfilled)}")
    return system, text


def email_content(s: C.Scenario) -> str:
    """The emails a generator-format scenario's reply saw: `text` minus the user instruction
    (the classifiers' `email_content` argument)."""
    prefix = _templates().USER_PROMPT
    if not s.text.startswith(prefix):
        raise ValueError(f"{s.id}: text does not start with the generator's USER_PROMPT")
    return s.text[len(prefix):].strip()


def generator_scenario(scenario: str, goal_type: str, goal_value: str, urgency: str,
                       set_: str) -> C.Scenario:
    system, text = render_condition(scenario, goal_type, goal_value, urgency)
    return C.Scenario(
        id=C.scenario_id(scenario, goal_type, goal_value, urgency), source="generator",
        set=set_, category=scenario, arm="misaligned", group="anthropic",
        template_group=C.template_group(scenario, goal_value), scenario_type=scenario,
        system=system, text=text, descriptor=LYNCH_DESCRIPTOR.format(scenario=scenario),
        lynch_conditions={"scenario": scenario, "goal_type": goal_type,
                          "goal_value": goal_value, "urgency_type": urgency},
        published_rate=None, pinned_rollout="",
    )


# --- the draw and the subsets ------------------------------------------------------------------

Condition = tuple[str, str, str, str]  # (scenario, goal_type, goal_value, urgency)


def pool_conditions() -> list[Condition]:
    """The 144 pool conditions, sorted by condition id."""
    pool = [(sc, gt, gv, urg) for sc in C.LYNCH_SCENARIOS for gt, gv in C.POOL_GOAL_PAIRS
            for urg in C.LYNCH_URGENCY]
    pool.sort(key=lambda c: C.condition_id(*c))
    assert len(pool) == C.POOL_SIZE == len({C.condition_id(*c) for c in pool})
    return pool


def draw_conditions() -> tuple[list[Condition], list[Condition]]:
    """(confirmatory, sealed) in draw order (module docstring, "The draw")."""
    drawn = random.Random(C.SCENARIO_DRAW_SEED).sample(
        pool_conditions(), C.N_CONFIRMATORY + C.N_SEALED
    )
    return drawn[:C.N_CONFIRMATORY], drawn[C.N_CONFIRMATORY:]


def bank_sampled_ids(bank: list[C.Scenario]) -> list[str]:
    rated = {s.id for s in bank if s.published_rate is not None}
    controls = {s.id for s in bank if s.arm == "control"}
    if len(rated) != 14 or len(controls) != 4 or rated & controls:
        raise ValueError(f"bank_sampled: {len(rated)} rated, {len(controls)} controls, "
                         f"{len(rated & controls)} overlapping (expected 14, 4, 0)")
    ids = sorted(rated | controls)
    assert len(ids) == C.N_BANK_SAMPLED
    return ids


def bank_validation_ids(bank: list[C.Scenario]) -> list[str]:
    """16 bank ids stratified by category (module docstring, "Subsets")."""
    rng = random.Random(C.BANK_SUBSET_SEED)
    by_cat: dict[str, list[str]] = {}
    for s in bank:
        by_cat.setdefault(s.category, []).append(s.id)
    queues = {}
    for cat in sorted(by_cat):
        ids = sorted(by_cat[cat])
        rng.shuffle(ids)
        queues[cat] = ids
    out: list[str] = []
    while len(out) < C.N_BANK_VALIDATION:
        took = False
        for cat in sorted(queues):
            if queues[cat] and len(out) < C.N_BANK_VALIDATION:
                out.append(queues[cat].pop(0))
                took = True
        if not took:
            raise ValueError("bank has fewer items than N_BANK_VALIDATION")
    return sorted(out)


def thinking_ids(confirmatory: list[str]) -> list[str]:
    return sorted(random.Random(C.THINKING_SUBSET_SEED).sample(sorted(confirmatory), C.N_THINKING))


def build() -> tuple[list[C.Scenario], dict[str, list[str]]]:
    """All 112 Scenario rows (bank in the bank's order, then confirmatory and sealed, each sorted
    by id) and the `sets.json` mapping (every SET_KEYS key, ids sorted)."""
    bank = bank_scenarios()
    conf_c, sealed_c = draw_conditions()
    conf = sorted((generator_scenario(*c, "confirmatory") for c in conf_c), key=lambda s: s.id)
    sealed = sorted((generator_scenario(*c, "sealed") for c in sealed_c), key=lambda s: s.id)
    rows = [*bank, *conf, *sealed]
    sets = {
        "bank": sorted(s.id for s in bank),
        "confirmatory": [s.id for s in conf],
        "sealed": [s.id for s in sealed],
        "bank_sampled": bank_sampled_ids(bank),
        "bank_validation": bank_validation_ids(bank),
        "thinking": thinking_ids([s.id for s in conf]),
    }
    validate(rows, sets)
    return rows, sets


def write_outputs(rows: list[C.Scenario], sets: dict[str, list[str]],
                  scenarios_path: Path | None = None,
                  sets_path: Path | None = None) -> None:
    """scenarios.jsonl (one `contracts.to_json` line per row) and sets.json (SET_KEYS order)."""
    scenarios_path = C.SCENARIOS_PATH if scenarios_path is None else scenarios_path
    sets_path = C.SETS_PATH if sets_path is None else sets_path
    scenarios_path.parent.mkdir(parents=True, exist_ok=True)
    sets_path.parent.mkdir(parents=True, exist_ok=True)
    scenarios_path.write_text("".join(C.to_json(r) + "\n" for r in rows), encoding="utf-8")
    sets_path.write_text(json.dumps(sets, indent=2) + "\n", encoding="utf-8")


def input_files() -> list[Path]:
    """The build's inputs, for the manifest: the bank, the generator script it mirrors, and
    every vendored template file (no bytecode)."""
    templates = sorted(
        p for p in LYNCH_TEMPLATES_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )
    return [BANK_ITEMS_PATH, LYNCH_GENERATOR_PATH, *templates]


def validate(rows: list[C.Scenario], sets: dict[str, list[str]]) -> None:
    """Structural checks on a build (sizes, partitions, subsets, no rendered rollout)."""
    ids = [s.id for s in rows]
    if len(ids) != len(set(ids)) or len(ids) != C.N_BANK + C.N_CONFIRMATORY + C.N_SEALED:
        raise ValueError(f"expected {C.N_BANK + C.N_CONFIRMATORY + C.N_SEALED} unique ids")
    if tuple(sets) != C.SET_KEYS:
        raise ValueError(f"sets keys {tuple(sets)} != {C.SET_KEYS}")
    sizes = {"bank": C.N_BANK, "confirmatory": C.N_CONFIRMATORY, "sealed": C.N_SEALED}
    for key, n in sizes.items():
        members = {s.id for s in rows if s.set == key}
        if set(sets[key]) != members or len(sets[key]) != n:
            raise ValueError(f"set {key}: size {len(sets[key])} (expected {n}) or mismatch")
    for key, (parent, n) in C.SUBSETS.items():
        if len(set(sets[key])) != n or not set(sets[key]) <= set(sets[parent]):
            raise ValueError(f"subset {key}: expected {n} ids inside {parent}")
    for s in rows:
        if s.pinned_rollout and (s.pinned_rollout in s.system or s.pinned_rollout in s.text):
            raise ValueError(f"{s.id}: the pinned rollout appears in the prompt")


# --- classifier prompts -------------------------------------------------------------------------


def classifier_prompt_path(scenario: str) -> Path:
    return C.PROMPTS_DIR / f"classifier_{scenario}.txt"


def load_classifier_prompt(scenario: str, path: Path | None = None) -> tuple[str, str]:
    """`(system, user_template)` from a classifier prompt file: leading `#` header lines, then
    `[system]` and `[user]` sections (each stripped). The user template keeps `{response}` and
    `{email_context}`; fill them in one pass (not str.format on the reply's text)."""
    text = Path(path or classifier_prompt_path(scenario)).read_text(encoding="utf-8")
    body = "".join(ln for ln in text.splitlines(keepends=True) if not ln.startswith("#"))
    m = re.fullmatch(r"\s*\[system\]\n(.*?)\n\[user\]\n(.*)", body, flags=re.DOTALL)
    if not m:
        raise ValueError(f"{path or scenario}: expected [system] then [user] sections")
    system, user = m.group(1).strip(), m.group(2).strip()
    for name in CLASSIFIER_PLACEHOLDERS:
        if user.count("{" + name + "}") != 1:
            raise ValueError(f"classifier_{scenario}: the user template needs one {{{name}}}")
    return system, user

