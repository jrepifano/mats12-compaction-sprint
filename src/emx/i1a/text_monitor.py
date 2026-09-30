"""The text-only monitor baseline (prereg v0.4 §1; implementation plan §0 and §2 U5).

Gemini 3.8 Flash (the benchmark's pinned judge model, temperature 0, its default reasoning) sees
the decoded render up to and including the read token, and nothing after it: never the target,
never the rest of the prompt or the model's response. It writes 1-4 ``- `` bullets, the Oracle
Lens output format, in JSON ``{"readout": ...}``. One call per read site; the readout is written
at every layer of the family (identical ``samples=[readout]``), into
``readouts/text_monitor/<family>.jsonl`` in the wsbench readouts contract.

Calls go through ``wsbench.mcjudge.run_calls`` (``llm.stream_json`` underneath) with a wsbench
``Cache`` at ``results/I1a/monitor_calls/<family>.jsonl`` (``contracts.MONITOR_CALLS_DIR``), so a
rerun makes no new call for a site already answered and re-queues only failures. Spend, as in
``judge_driver``: pending calls go in guarded batches of 250 (projected x1.25 at no less than the
floor rate, checked with ``emx.spend.check_total``); every response's cost is journaled as it
lands (``<family>.spend_journal.jsonl``) and reconciled into the ledger before the first call,
after each batch and on any exit; a batch that spends more than its projection stops the run.

Usage:
  uv run python -m emx.i1a.text_monitor --family multihop [--limit 5] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from emx import spend
from emx.i1a import contracts as C

PROMPT_VERSION = "text_monitor_v1"
PROMPT_FILE = C.PROMPTS_DIR / f"{PROMPT_VERSION}.txt"
_FIELD = re.compile(r"\{(prefix|token)\}")


MONITOR_CHUNK = 250  # pending calls per guarded batch


def monitor_calls_path(family: str) -> Path:
    return C.MONITOR_CALLS_DIR / f"{family}.jsonl"


def monitor_journal_path(family: str) -> Path:
    """Per-response spend journal (see ``judge_driver.journaled``)."""
    return C.MONITOR_CALLS_DIR / f"{family}.spend_journal.jsonl"


def load_prompt(path: Path | None = None) -> tuple[str, str]:
    """``(system, user_template)`` from the ``[system]`` / ``[user]`` sections of the file."""
    text = Path(path or PROMPT_FILE).read_text(encoding="utf-8")
    m = re.fullmatch(r"\[system\]\n(.*?)\n\[user\]\n(.*)", text, flags=re.DOTALL)
    if not m:
        raise ValueError(f"{path or PROMPT_FILE}: expected [system] then [user] sections")
    system, user = m.group(1).strip(), m.group(2).strip()
    if "{prefix}" not in user or "{token}" not in user:
        raise ValueError("the user template needs {prefix} and {token}")
    return system, user


def render_user(template: str, prefix: str, token: str) -> str:
    """One-pass substitution, so text inside the prefix is never itself substituted."""
    vals = {"prefix": prefix, "token": json.dumps(token, ensure_ascii=False)}
    return _FIELD.sub(lambda m: vals[m.group(1)], template)


@lru_cache(maxsize=1)
def _tokenizer() -> Any:
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(C.MODEL_ID, revision=C.MODEL_REV)


def prefix_text(family: str, site: C.SiteRow, ids: list[int], tokenizer: Any = None) -> str:
    """The decoded render up to and including the read token (``ids[: abs_pos + 1]``), special
    tokens kept (the chat roles are part of what the model read). Nothing after the read site
    is ever decoded, so the target and any later text cannot reach the prompt."""
    tok = tokenizer if tokenizer is not None else _tokenizer()
    if site.family != family:
        raise ValueError(f"site of {site.family!r} passed for {family!r}")
    if len(ids) != site.n_tokens:
        raise ValueError(f"{site.id}: render has {len(ids)} ids, SiteRow says {site.n_tokens}")
    if not 0 <= site.abs_pos < len(ids):
        raise ValueError(f"{site.id}: abs_pos {site.abs_pos} outside the render ({len(ids)})")
    return tok.decode(list(ids[: site.abs_pos + 1]), skip_special_tokens=False)


def _sites(family: str, tokenizer: Any, limit: int = 0) -> list[tuple[C.SiteRow, list[int]]]:
    from emx.i1a import capture  # U2; imported lazily (it pulls wsbench's render stack)

    return capture.sites(family, tokenizer, limit=limit)


def build_prompts(family: str, *, tokenizer: Any = None, limit: int = 0) -> list[dict]:
    """One prompt per read site: ``{index, id, pos, abs_pos, token, prefix, system, user}``."""
    if family not in C.FAMILIES:
        raise ValueError(f"not an I1a family: {family!r}")
    tok = tokenizer if tokenizer is not None else _tokenizer()
    system, template = load_prompt()
    out = []
    for site, ids in _sites(family, tok, limit):
        prefix = prefix_text(family, site, ids, tok)
        out.append(
            {
                "index": site.index,
                "id": site.id,
                "pos": site.pos,
                "abs_pos": site.abs_pos,
                "token": site.token,
                "prefix": prefix,
                "system": system,
                "user": render_user(template, prefix, site.token),
            }
        )
    return out


def _judge() -> Any:
    from wsbench.judge_config import JudgeConfig, resolve

    return resolve(JudgeConfig(prompt_version=PROMPT_VERSION), env={})  # DEFAULT_JUDGE, pinned


def _readout(r: Any) -> str | None:
    if not isinstance(r, dict) or not isinstance(r.get("readout"), str):
        return None
    t = r["readout"].strip()
    return t or None


def run_monitor(
    family: str,
    *,
    dry_run: bool = False,
    tokenizer: Any = None,
    limit: int = 0,
    usd_per_call: float | None = None,
    concurrency: int = 64,
    rpm: float = 240.0,
) -> Path:
    """Generate the monitor readouts for one family and return the readouts path (module doc).
    A site whose call failed is written with an empty readout (an empty cell to the benchmark)
    and is retried by the next run."""
    from wsbench.cache import Cache, fingerprint
    from wsbench.llm import JudgeConfigError, Spend, schema_block
    from wsbench.mcjudge import Call, run_calls

    from emx.i1a import judge_driver as J

    usd = J.usd_rate(usd_per_call)
    prompts = build_prompts(family, tokenizer=tokenizer, limit=limit)
    judge = _judge()
    schema = schema_block("monitor", {"readout": {"type": "string"}}, ["readout"])
    calls = [
        Call(f"{p['id']}__p{p['pos']}", p["system"], p["user"], {"index": p["index"]})
        for p in prompts
    ]
    if len({c.key for c in calls}) != len(calls):
        raise ValueError(f"{family}: duplicate (id, pos) read sites")
    out_path = C.readouts_path("text_monitor", family)
    cache_path = monitor_calls_path(family)
    journal, name = monitor_journal_path(family), f"text_monitor-gen/{family}"

    def pending_calls() -> list:
        with Cache(cache_path) as cache:
            return [
                c
                for c in calls
                if cache.get(
                    c.key,
                    fingerprint(
                        PROMPT_VERSION, judge.model, judge.reasoning, 0.0, c.system, c.user
                    ),
                )
                is None
            ]

    pending = pending_calls()
    batches = [pending[k : k + MONITOR_CHUNK] for k in range(0, len(pending), MONITOR_CHUNK)]
    total = J.project(len(pending) + (1 if pending else 0), usd)
    print(
        f"[text_monitor] {family}: {len(calls)} sites, {len(pending)} to call (+1 preflight) in "
        f"{len(batches)} batches, model={judge.model}, projected ${total:.4f} "
        f"(${usd:.4f}/call x {J.RETRY_HEADROOM} + ${J.MAX_USD_PER_ATTEMPT} slack)"
    )
    if dry_run:
        if pending:
            spend.check_total(total, "judge", exp=C.EXP)
        if calls:
            print("[system]\n" + calls[0].system + "\n[user]\n" + calls[0].user)
        print(f"[text_monitor] DRY RUN: nothing sent, nothing written ({out_path})")
        return out_path

    if pending:  # paid: the text monitor is a P3 baseline (checks, P3 approval, pilot record)
        J.preflight_gate("gate")
    orphan = J.reconcile(journal, name, "orphaned spend from an earlier run")
    if orphan:
        print(f"[text_monitor] {family}: recorded ${orphan:.4f} of orphaned journal spend")
    tally = Spend()
    for k, batch in enumerate(batches, 1):
        label = f"batch {k}/{len(batches)}"
        proj = J.project(len(batch) + (1 if k == 1 else 0), usd)
        print(f"[text_monitor] {family} {label}: {len(batch)} calls, projected ${proj:.4f}")
        spend.check_total(proj, "judge", exp=C.EXP)
        usd0, _n0 = J.journal_totals(journal)
        budget = J.Budget(proj, usd, J.cap_room(journal, name))
        try:
            with (
                J.journaled(journal, fallback_usd=usd, budget=budget),
                Cache(cache_path) as cache,
            ):
                if k == 1:
                    J.preflight(judge)
                run_calls(
                    batch,
                    schema=schema,
                    judge=judge,
                    prompt_version=PROMPT_VERSION,
                    cache=cache,
                    spend=tally,
                    concurrency=concurrency,
                    rpm=rpm,
                    dry_run=False,
                    validate=lambda _c, r: _readout(r) is not None,
                    temperature=0.0,
                )
        except JudgeConfigError:
            if budget.refused is None:
                raise
            raise J.JudgeRunError(
                2,
                f"text_monitor/{family} {label}: stopped mid-batch, {budget.refused}; spend so "
                "far is recorded (raise --usd-per-call if the rate is too low)",
            ) from None
        finally:
            J.reconcile(journal, name, label)
        if budget.refused:  # e.g. the batch's last response cost more than its reservation
            raise J.JudgeRunError(2, f"text_monitor/{family} {label}: {budget.refused}")
        spent = round(J.journal_totals(journal)[0] - usd0, 6)
        if spent > proj + 1e-9:
            raise J.JudgeRunError(
                2,
                f"text_monitor/{family} {label}: spent ${spent:.4f}, over its projection "
                f"${proj:.4f}; stopping (raise --usd-per-call)",
            )
    # every answer, fresh or cached, from the cache (no call can happen here)
    results: dict[str, Any] = {}
    with Cache(cache_path) as cache:
        for c in calls:
            fp = fingerprint(PROMPT_VERSION, judge.model, judge.reasoning, 0.0, c.system, c.user)
            row = cache.get(c.key, fp)
            results[c.key] = row["result"] if row is not None else None
    layers = C.FAMILY_LAYERS[family]
    n_failed = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for p, c in zip(prompts, calls, strict=True):
            text = _readout(results.get(c.key))
            if text is None:
                n_failed += 1
                text = ""
            for layer in layers:
                row = {
                    "id": p["id"],
                    "layer": layer,
                    "pos": p["pos"],
                    "token": p["token"],
                    "samples": [text],
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(out_path)
    print(
        f"[text_monitor] {family}: wrote {len(prompts) * len(layers)} rows to {out_path}; "
        f"{tally.report()}"
    )
    if n_failed:
        print(
            f"[text_monitor] WARNING {family}: {n_failed} sites failed and are written empty; "
            "rerun to retry them",
            file=sys.stderr,
        )
    return out_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="I1a+ text-only monitor readouts")
    ap.add_argument("--family", nargs="+", default=list(C.FAMILIES))
    ap.add_argument("--limit", type=int, default=0, help="first N plan items (pilot)")
    ap.add_argument("--usd-per-call", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env", default=None)
    args = ap.parse_args(argv)
    from emx.i1a import judge_driver as J

    try:
        J.usd_rate(args.usd_per_call)  # the floor, before the key is even read
        if not args.dry_run:
            J.load_key(args.env)
        for fam in args.family:
            run_monitor(fam, dry_run=args.dry_run, limit=args.limit, usd_per_call=args.usd_per_call)
    except spend.BudgetExceeded as e:
        print(json.dumps(e.breakdown, indent=2), file=sys.stderr)
        return 2
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    except ValueError as e:  # e.g. --usd-per-call below the floor
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
