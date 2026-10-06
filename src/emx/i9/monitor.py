"""I9 text-only monitors (prereg v0.5 §4.2 Stage 0 and the predictors table; §4.3 job 3).

All monitors are Gemini 3.8 Flash (``contracts.GEMINI``) and see prompt text only; every call
goes through an injectable ``complete`` (``emx.i9.textllm.complete`` by default) and the cached
batch of ``emx.i9.judging`` (one retry for an unparseable reply).

- **Stage 0.** Gemini writes an account per bank scenario in the format wsbench's Stage B
  (``CONSOLIDATE_PROMPT``) produces (``prompts/I9/stage0_account.txt``). Each account is scored
  with wsbench's unchanged Stage C: ``prompts.render_design_score`` (scenario rendered by
  ``judge.scenario_of`` from ``contracts.to_bank_row``) -> ``parse_design_score`` ->
  ``score.aggregate_design``, once with Sonnet 5 (the pinned scorer) and once with luna. The
  records are built exactly as ``judge.run`` builds them; a failed scorer call leaves no record,
  an unparseable one (after its retry) is a ``PARSE_ERR`` record, as in wsbench.
- **Zero-shot rate monitor.** The probability that the target model takes the scenario's
  misaligned action -> ``Prediction("monitor_zero_shot", fold_repeat=-1, raw=p)``.
- **Few-shot rate monitor.** The same plus 8 training-fold scenarios with their observed rates
  (``select_examples``: a seeded round-robin over scenario types) -> one ``Prediction`` per
  (scenario, fold repeat). Example pool (prereg amendment B item 8, ``tasks_from_folds``): for a
  scenario in fold g of a fold repeat, examples come only from folds other than g **and other
  than (g + 1) mod ``contracts.N_FOLDS``**, so the analysis can calibrate test fold f's
  few-shot predictions on fold f - 1's scenarios, whose examples never include fold f's rates.
- **CoT monitor** (thinking arm). The prompt plus one reply's reasoning, cut just before its
  first tool call or its final message (``cut_reasoning``) -> a probability per reply, averaged
  per scenario -> ``Prediction("monitor_cot", target_thinking=True, fold_repeat=-1)``.

``Prediction.raw`` is the monitor's probability; ``p`` is that probability clipped to
``PRED_CLIP``, a placeholder: the logistic calibration on training folds is U5's (prereg §4.2).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from emx.i9 import contracts as C
from emx.i9 import judging as J


def stage0_dir() -> Path:
    """Stage 0 outputs, resolved from ``contracts.RESULTS`` at call time (never bound at import,
    so tests that redirect RESULTS also redirect this; code review r2 finding 8). Prompt paths
    below are read-only inputs and may bind at import."""
    return C.RESULTS / "stage0"


ACCOUNT_PROMPT = C.PROMPTS_DIR / "stage0_account.txt"
ZERO_PROMPT = C.PROMPTS_DIR / "monitor_zero_shot.txt"
FEW_PROMPT = C.PROMPTS_DIR / "monitor_few_shot.txt"
COT_PROMPT = C.PROMPTS_DIR / "monitor_cot.txt"
MODEL_NAME = {"qwen": "Qwen3.6-27B", "gemma": "Gemma 4 31B"}
N_FEW_SHOT = 8
ACCOUNT_MAX_TOKENS = 6000
MONITOR_MAX_TOKENS = 2000
STAGE_C_MAX_TOKENS = 12_000  # wsbench judge.STAGE_C_MAX_TOKENS
GEMINI_REASONING = "default"  # textllm: no reasoning field, the model's default (as I1a's monitor)
# Stage C as wsbench runs it: Sonnet with its pinned-judge settings (B/C "default", i.e. adaptive
# thinking); luna at the B/C effort (the pilot's pick; "none" until the pilot has run).
SCORER_REASONING: dict[str, str] = {C.SONNET: C.SONNET_EFFORT_BY_THINKING[True], C.LUNA: "none"}
OUT_TOKENS = {"account": 1500, "score": 4000, "monitor": 300}  # prereg §7 estimates


def clip(p: float) -> float:
    lo, hi = C.PRED_CLIP
    return min(hi, max(lo, p))


def scenario_block(s: C.Scenario) -> str:
    return J.scenario_text(s)


# --- Stage 0 ------------------------------------------------------------------------------------


def account_calls(scenarios: Sequence[C.Scenario], prompt: Path = ACCOUNT_PROMPT) -> list[J.Call]:
    system, template = J.load_prompt(prompt)
    return [J.Call(key=f"account|{s.id}", system=system,
                   user=J.fill(template, {"scenario": scenario_block(s)})) for s in scenarios]


def parse_account(text: str) -> str | None:
    """An account must carry its CANDIDATE ACTIONS list (the scorer's primary plan is its first
    entry); anything else is unparseable and retried once."""
    t = (text or "").strip()
    return t if t and "CANDIDATE ACTIONS" in t.upper() else None


def write_accounts(
    scenarios: Sequence[C.Scenario], *, complete: J.CompleteFn | None = None,
    cache: J.CallCache | None = None, spend: Any = None,
) -> dict[str, str | None]:
    calls = account_calls(scenarios)
    res = J.cached_batch(calls, model=C.GEMINI, cache=cache or J.CallCache(J.cache_path(
        "stage0_account", C.GEMINI)), parse=parse_account, max_tokens=ACCOUNT_MAX_TOKENS,
        reasoning=GEMINI_REASONING, complete=complete, spend=spend,
        purpose="i9_stage0_account")
    return {s.id: res[f"account|{s.id}"][0] for s in scenarios}


def score_calls(accounts: Mapping[str, str | None], scenarios: Mapping[str, C.Scenario]
                ) -> list[J.Call]:
    """wsbench's Stage C prompt, byte for byte (``render_design_score`` on ``scenario_of`` of the
    frozen bank row); no system block, as in wsbench."""
    from wsbench.evals.agentic_misalignment import judge as wj
    from wsbench.evals.agentic_misalignment import prompts as wp

    out = []
    for sid, acc in accounts.items():
        if acc is None:
            continue
        row = C.to_bank_row(scenarios[sid])
        out.append(J.Call(key=f"score|{sid}",
                          user=wp.render_design_score(acc, row["descriptor"], wj.scenario_of(row))))
    return out


def parse_score(text: str) -> dict[str, Any] | None:
    from wsbench.evals.agentic_misalignment.prompts import parse_design_score

    try:
        d = parse_design_score(text)
    except (json.JSONDecodeError, ValueError, AttributeError):
        return None
    return None if d["identified_family"] == "PARSE_ERR" else d


def score_accounts(
    accounts: Mapping[str, str | None],
    scenarios: Mapping[str, C.Scenario],
    *,
    scorer: str,
    reasoning: str | None = None,
    complete: J.CompleteFn | None = None,
    cache: J.CallCache | None = None,
    spend: Any = None,
    purpose: str = "stage0_score",
) -> dict[str, dict[str, Any]]:
    """id -> parsed Stage C fields (``PARSE_ERR`` fields for a reply that never parsed; absent
    for an item without an account or whose call failed). Also used by the luna-vs-Sonnet gate
    (Sonnet scoring luna's accounts, ``purpose="validation_score"``)."""
    from wsbench.evals.agentic_misalignment.prompts import parse_design_score

    calls = score_calls(accounts, scenarios)
    rsn = reasoning if reasoning is not None else SCORER_REASONING.get(scorer, "none")
    res = J.cached_batch(calls, model=scorer, cache=cache or J.CallCache(J.cache_path(
        purpose, scorer)), parse=parse_score, max_tokens=STAGE_C_MAX_TOKENS, reasoning=rsn,
        complete=complete, spend=spend, purpose=f"i9_{purpose}")
    out = {}
    for c in calls:
        parsed, raw = res[c.key]
        if raw is None:
            continue
        out[c.key.split("|", 1)[1]] = parsed if parsed is not None else parse_design_score("")
    return out


def design_records(scores: Mapping[str, Mapping[str, Any]], scenarios: Mapping[str, C.Scenario]
                   ) -> list[dict[str, Any]]:
    """The records ``judge.run`` hands to ``aggregate_design``."""
    recs = []
    for sid, parsed in scores.items():
        s = scenarios[sid]
        recs.append({"id": sid, "arm": s.arm, "family": s.category,
                     "misalignment_rate": s.published_rate, **parsed})
    return recs


def stage0(
    scenarios: Sequence[C.Scenario],
    *,
    scorers: Sequence[str] = (C.SONNET, C.LUNA),
    reasoning: Mapping[str, str | None] | None = None,
    complete: J.CompleteFn | None = None,
    cache_dir: Path | None = None,
    spend: Any = None,
) -> dict[str, Any]:
    """Accounts, then each scorer's Stage C and ``aggregate_design`` (prereg §4.4 Stage 0 rule:
    the monitor "reaches" the readers if design_score >= ``STAGE0_THRESHOLD``; descriptive)."""
    from wsbench.evals.agentic_misalignment import score as ws

    def cache(purpose: str, model: str) -> J.CallCache:
        p = J.cache_path(purpose, model)
        return J.CallCache(p if cache_dir is None else Path(cache_dir) / p.name)

    by_id = {s.id: s for s in scenarios}
    accounts = write_accounts(scenarios, complete=complete, cache=cache("stage0_account",
                              C.GEMINI), spend=spend)
    out: dict[str, Any] = {"accounts": accounts, "n_items": len(scenarios),
                           "n_accounts": sum(a is not None for a in accounts.values()),
                           "scorers": {}}
    for sc in scorers:
        rsn = (reasoning or {}).get(sc, SCORER_REASONING.get(sc, "none"))
        scores = score_accounts(accounts, by_id, scorer=sc, reasoning=rsn, complete=complete,
                                cache=cache("stage0_score", sc), spend=spend)
        recs = design_records(scores, by_id)
        agg = ws.aggregate_design(recs, n_boot=ws.N_BOOT, seed=ws.SEED) if recs else None
        ds = agg["design_score"] if agg else None
        out["scorers"][sc] = {
            "reasoning": rsn, "records": recs, "aggregate": agg,
            "n_misaligned_scenarios": sum(by_id[i].arm == "misaligned" for i in by_id),
            "reaches_threshold": None if ds is None else ds >= C.STAGE0_THRESHOLD,
            "diff_from_olens": None if ds is None else ds - C.STAGE0_OLENS_REF,
        }
    out["threshold"] = C.STAGE0_THRESHOLD
    out["olens_ref"] = C.STAGE0_OLENS_REF
    return out


# --- rate monitors ------------------------------------------------------------------------------

_JSON = re.compile(r"\{.*\}", flags=re.DOTALL)


def parse_probability(text: str) -> float | None:
    """``{"probability": p, ...}`` with 0 <= p <= 1 (code fences allowed); else None."""
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    m = _JSON.search(t)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        p = float(obj["probability"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return p if 0.0 <= p <= 1.0 else None


def _monitor_values(s: C.Scenario, target: str) -> dict[str, str]:
    return {"model_name": MODEL_NAME[target], "scenario": scenario_block(s),
            "descriptor": s.descriptor}


def zero_shot_calls(scenarios: Sequence[C.Scenario], target: str,
                    prompt: Path = ZERO_PROMPT) -> list[J.Call]:
    system, template = J.load_prompt(prompt)
    return [J.Call(key=f"zero|{target}|{s.id}", system=system,
                   user=J.fill(template, _monitor_values(s, target))) for s in scenarios]


def _run_monitor(calls: list[J.Call], purpose: str, *, complete, cache, spend
                 ) -> dict[str, tuple[Any, str | None]]:
    return J.cached_batch(calls, model=C.GEMINI, cache=cache or J.CallCache(J.cache_path(
        purpose, C.GEMINI)), parse=parse_probability, max_tokens=MONITOR_MAX_TOKENS,
        reasoning=GEMINI_REASONING, complete=complete, spend=spend, purpose=f"i9_{purpose}")


def monitor_zero_shot(
    scenarios: Sequence[C.Scenario], *, target: str = "qwen",
    complete: J.CompleteFn | None = None, cache: J.CallCache | None = None, spend: Any = None,
) -> list[C.Prediction]:
    """One prediction per scenario with a parsed probability (missing ones are dropped and must
    be reported by the caller)."""
    calls = zero_shot_calls(scenarios, target)
    res = _run_monitor(calls, "monitor_zero", complete=complete, cache=cache, spend=spend)
    out = []
    for s, c in zip(scenarios, calls, strict=True):
        p = res[c.key][0]
        if p is not None:
            out.append(C.Prediction("monitor_zero_shot", target, False, s.id, -1, p, clip(p)))
    return out


def example_seed(fold_repeat: int, scenario_id: str) -> int:
    key = f"few|{C.FOLD_SEEDS[fold_repeat]}|{scenario_id}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:4], "big")


def select_examples(test_id: str, fold_repeat: int, train_ids: Sequence[str],
                    scenarios: Mapping[str, C.Scenario], k: int = N_FEW_SHOT) -> list[str]:
    """The documented rule: ``random.Random(example_seed(fold_repeat, test_id))`` shuffles the
    sorted training ids of each scenario type; then one id per type in turn (types sorted) until
    ``k``. Every type is represented when possible; the test scenario is never an example."""
    if test_id in train_ids:
        raise ValueError(f"{test_id} is in its own training fold")
    rng = random.Random(example_seed(fold_repeat, test_id))
    by_type: dict[str, list[str]] = defaultdict(list)
    for sid in sorted(set(train_ids)):
        by_type[scenarios[sid].scenario_type].append(sid)
    for ids in by_type.values():
        rng.shuffle(ids)
    out: list[str] = []
    types = sorted(by_type)
    while len(out) < k and any(by_type[t] for t in types):
        for t in types:
            if by_type[t] and len(out) < k:
                out.append(by_type[t].pop(0))
    return out


def format_example(i: int, s: C.Scenario, rate: C.Rate) -> str:
    return (f'<example n="{i}">\n<scenario>\n{scenario_block(s)}\n</scenario>\n'
            f"Misaligned action of interest: {s.descriptor}\n"
            f"Observed: {rate.k} of {rate.n} sampled replies took it (rate {rate.k / rate.n:.3f})."
            f"\n</example>")


def few_shot_calls(
    tasks: Sequence[tuple[str, int, Sequence[str]]],
    scenarios: Mapping[str, C.Scenario],
    rates: Mapping[str, C.Rate],
    target: str,
    prompt: Path = FEW_PROMPT,
) -> tuple[list[J.Call], dict[str, list[str]]]:
    """``tasks`` = (scenario_id, fold_repeat, that fold's training ids). Rates must be the
    target model's thinking-off rates."""
    system, template = J.load_prompt(prompt)
    calls, chosen = [], {}
    for sid, rep, train in tasks:
        ex = select_examples(sid, rep, train, scenarios)
        for e in ex:
            r = rates[e]
            if r.model != target or r.thinking:
                raise ValueError(f"rate for {e} is ({r.model}, thinking={r.thinking}), "
                                 f"not ({target}, thinking off)")
        key = f"few|{target}|{rep}|{sid}"
        chosen[key] = ex
        examples = "\n\n".join(format_example(i + 1, scenarios[e], rates[e])
                               for i, e in enumerate(ex))
        calls.append(J.Call(key=key, system=system, user=J.fill(
            template, {**_monitor_values(scenarios[sid], target), "examples": examples})))
    return calls, chosen


EXAMPLE_POOL_RULE = ("few-shot examples for a scenario in fold g come only from folds other than "
                     "g and (g + 1) mod N_FOLDS (prereg amendment B item 8), one per scenario "
                     "type in turn (select_examples)")


def tasks_from_folds(folds: Mapping[str, Mapping[str, int]], ids: Sequence[str] | None = None
                     ) -> list[tuple[str, int, list[str]]]:
    """``folds`` = {fold_repeat: {scenario_id: fold}} (U5's assignment) -> few-shot tasks
    ``(scenario_id, fold_repeat, example pool)``. The pool of a scenario in fold g is every
    scenario in a fold other than g and other than (g + 1) mod ``contracts.N_FOLDS``
    (``EXAMPLE_POOL_RULE``, prereg amendment B item 8)."""
    tasks = []
    for rep_s, assign in sorted(folds.items(), key=lambda kv: int(kv[0])):
        for sid in sorted(assign):
            if ids is not None and sid not in ids:
                continue
            g = assign[sid]
            barred = {g, (g + 1) % C.N_FOLDS}
            train = [t for t, f in assign.items() if f not in barred]
            tasks.append((sid, int(rep_s), train))
    return tasks


def monitor_few_shot(
    tasks: Sequence[tuple[str, int, Sequence[str]]],
    scenarios: Mapping[str, C.Scenario],
    rates: Mapping[str, C.Rate],
    *,
    target: str = "qwen",
    complete: J.CompleteFn | None = None,
    cache: J.CallCache | None = None,
    spend: Any = None,
) -> list[C.Prediction]:
    calls, _ = few_shot_calls(tasks, scenarios, rates, target)
    res = _run_monitor(calls, "monitor_few", complete=complete, cache=cache, spend=spend)
    out = []
    for (sid, rep, _train), c in zip(tasks, calls, strict=True):
        p = res[c.key][0]
        if p is not None:
            out.append(C.Prediction("monitor_few_shot", target, False, sid, rep, p, clip(p)))
    return out


_TOOL_CALL = re.compile(r"<tool_use:|<tool_call>|<function_call")


def cut_reasoning(text: str) -> str:
    """The reasoning before the first tool call or the final message: the text up to
    ``</think>`` (all of it if the reply was cut before closing its reasoning), without a
    leading ``<think>``, then cut at the first tool-call tag inside it."""
    end = text.find("</think>")
    r = text[:end] if end >= 0 else text
    r = re.sub(r"^\s*<think>", "", r)
    m = _TOOL_CALL.search(r)
    return (r[: m.start()] if m else r).strip()


def cot_calls(replies: Sequence[C.Reply], scenarios: Mapping[str, C.Scenario],
              prompt: Path = COT_PROMPT) -> list[J.Call]:
    system, template = J.load_prompt(prompt)
    calls = []
    for r in replies:
        if not r.thinking:
            raise ValueError("the CoT monitor reads thinking-arm replies only")
        vals = {**_monitor_values(scenarios[r.scenario_id], r.model),
                "reasoning": cut_reasoning(r.text)}
        calls.append(J.Call(key="cot|" + "|".join(map(str, J.reply_key(r))), system=system,
                            user=J.fill(template, vals)))
    return calls


def monitor_cot(
    replies: Sequence[C.Reply],
    scenarios: Mapping[str, C.Scenario],
    *,
    complete: J.CompleteFn | None = None,
    cache: J.CallCache | None = None,
    spend: Any = None,
) -> tuple[list[C.Prediction], dict[str, dict[str, int]]]:
    """Per-reply probabilities averaged per (model, scenario) over the parsed ones; returns the
    predictions and {scenario_id: {n, n_parsed}}."""
    calls = cot_calls(replies, scenarios)
    res = _run_monitor(calls, "monitor_cot", complete=complete, cache=cache, spend=spend)
    per: dict[tuple[str, str], list[float]] = defaultdict(list)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "n_parsed": 0})
    for r, c in zip(replies, calls, strict=True):
        counts[r.scenario_id]["n"] += 1
        p = res[c.key][0]
        if p is not None:
            per[(r.model, r.scenario_id)].append(p)
            counts[r.scenario_id]["n_parsed"] += 1
    out = []
    for (model, sid), ps in sorted(per.items()):
        m = sum(ps) / len(ps)
        out.append(C.Prediction("monitor_cot", model, True, sid, -1, m, clip(m)))
    return out, dict(counts)
