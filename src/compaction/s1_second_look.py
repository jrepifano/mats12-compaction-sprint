"""Experiment 1 second look (spec D11, as amended 22 Oct): a tool-using re-check of judge positives.

Pass 2 reads a whole history at once and missed evidence that is in it (the fresh labels, 22 Oct:
numbers that appear verbatim were called unsupported), and pass 1b grades a whole item list in one
call. The second look re-checks only the positives, one call each, on the Max backend with the
read-only Grep and Read tools over files written into the call's working directory:

- a claim pass 2 called "unsupported": the summary and the claim in the prompt, the rendered
  history (`s1_judge.judge_view`, kept-verbatim messages included) as `history.txt`;
- an item pass 1b graded "dropped": the item and the summary in the prompt, the summary also as
  `summary.txt`.

Code checks the answer as the passes do: a claim moves to supported or contradicted only with an
evidence quote found in one history message (`s1_judge.locate`), and an item moves to kept or
misstated only with a summary quote located in the summary (`s1_judge.locate_summary_quote`,
exact or normalized). An answer that fails its check, or a failed call, leaves the first verdict
standing (`valid=False` with the reason). The output is an overlay, one row per re-checked row;
the sealed rows are never rewritten. Its prompts are frozen on their own
(`results/sprint/S1/frozen_second_look.json`), so the D10/D12 snapshot is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from compaction import s1_judge
from compaction import s1_types as T

PURPOSE = "s1_second_look"
TOOLS = ("Grep", "Read")
MAX_TURNS = 16  # the e2e check took 4; room for a few variants and reads, plus the answer
MAX_OUT = 8_000
CLAIM_VERDICT = "unsupported"  # pass 2 positives re-checked
ITEM_VERDICT = "dropped"  # pass 1b positives re-checked
_SLOT = re.compile(r"\{(summary|claim|summary_quote|note|type|text|quote)\}")

_STR_LIST = {"type": "array", "items": T._s()}
CLAIM_SCHEMA = T._obj(
    {
        "verdict": T._s(T.CLAIM_VERDICTS),
        "evidence_quote": T._s(nullable=True),
        "msg": T._INT_OR_NULL,
        "searched": _STR_LIST,
        "note": T._s(),
    }
)
ITEM_SCHEMA = T._obj(
    {
        "verdict": T._s(T.GRADES),
        "summary_quote": T._s(nullable=True),
        "searched": _STR_LIST,
        "note": T._s(),
    }
)


def load(name: str, prompt_dir: Path = s1_judge.PROMPT_DIR) -> s1_judge.Prompt:
    return s1_judge.load_prompt(name, prompt_dir)


def prompts(prompt_dir: Path = s1_judge.PROMPT_DIR) -> dict[str, s1_judge.Prompt]:
    return {
        "claim": load("second_look_claim", prompt_dir),
        "item": load("second_look_item", prompt_dir),
    }


def fill(p: s1_judge.Prompt, **slots: str) -> str:
    """The user section with its slots filled in one pass (inserted text is never re-scanned)."""
    tpl = p.sections["user"]
    missing = {m.group(1) for m in _SLOT.finditer(tpl)} - set(slots)
    if missing:
        raise KeyError(f"{p.name}: no value for {sorted(missing)}")
    return _SLOT.sub(lambda m: slots[m.group(1)], tpl)


def frozen(p: Mapping[str, s1_judge.Prompt]) -> dict:
    """What `frozen_second_look.json` records (spec D11)."""

    def sha(obj) -> str:
        return hashlib.sha256(
            json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    return {
        "prompts": {f"{q.name}.md": q.sha256 for q in p.values()},
        "schemas": {"CLAIM_SCHEMA": sha(CLAIM_SCHEMA), "ITEM_SCHEMA": sha(ITEM_SCHEMA)},
        "tools": list(TOOLS),
        "max_turns": MAX_TURNS,
        "rechecks": {"claim": CLAIM_VERDICT, "item": ITEM_VERDICT},
    }


def prompt_sha(p: Mapping[str, s1_judge.Prompt], kind: str) -> str:
    return f"second_look_{kind}={p[kind].sha256}"


def _req(key: str, p: s1_judge.Prompt, user: str, schema: dict, files, rec: dict, rid: str):
    return T.Req(
        key=key,
        system=p.system,
        user=user,
        schema=schema,
        max_output_tokens=MAX_OUT,
        rid=rid,
        source=str(rec.get("source")),
        private=T.anthropic_only(rec),
        files=tuple(files),
        tools=TOOLS,
        max_turns=MAX_TURNS,
    )


def claim_request(row: dict, rec: dict, rendered, summary: str, p) -> T.Req:
    user = fill(
        p["claim"],
        summary=summary,
        claim=str(row.get("claim") or ""),
        summary_quote=str(row.get("summary_quote") or ""),
        note=str(row.get("note") or "(none)"),
    )
    return _req(
        f"sl|claim|{row['claim_id']}",
        p["claim"],
        user,
        CLAIM_SCHEMA,
        [("history.txt", rendered.text)],
        rec,
        str(row["rid"]),
    )


def item_request(row: dict, rec: dict, summary: str, p) -> T.Req:
    user = fill(
        p["item"],
        summary=summary,
        type=str(row.get("type") or ""),
        text=str(row.get("text") or ""),
        quote=str(row.get("quote") or ""),
    )
    return _req(
        f"sl|item|{row['item_id']}",
        p["item"],
        user,
        ITEM_SCHEMA,
        [("summary.txt", summary)],
        rec,
        str(row["rid"]),
    )


def _overlay(kind: str, row: dict, first: str, resp: T.Resp | None, p) -> dict:
    meta = (resp.meta if resp is not None else {}) or {}
    return {
        "kind": kind,
        "row_id": row["claim_id" if kind == "claim" else "item_id"],
        "rid": row["rid"],
        "base_rid": row.get("base_rid"),
        "arm": row.get("arm"),
        "judge": row.get("judge"),
        "prompt_sha": prompt_sha(p, kind),
        "first_verdict": first,
        "verdict": first,  # the final verdict; set below when the answer passes its check
        "changed": False,
        "valid": False,
        "invalid_reason": None,
        "sl_verdict": None,
        "quote": None,
        "msg": None,
        "match": None,
        "searched": None,
        "note": None,
        "tool_calls": meta.get("tool_calls"),
        "num_turns": meta.get("num_turns"),
        "error": resp.error if resp is not None else "missing_response",
    }


def check_claim(row: dict, resp: T.Resp | None, rendered, p) -> dict:
    out = _overlay("claim", row, str(row.get("verdict")), resp, p)
    if resp is None or resp.error is not None or not isinstance(resp.data, dict):
        out["invalid_reason"] = out["error"] or "no_data"
        return out
    d = resp.data
    out.update(sl_verdict=d["verdict"], searched=d["searched"], note=d["note"])
    if d["verdict"] == "unsupported":
        out.update(valid=True, verdict="unsupported")
        return out
    msg = s1_judge.locate(d["evidence_quote"], rendered, d["msg"])
    if msg is None:
        out["invalid_reason"] = "evidence_quote_not_in_history"
        out["quote"] = d["evidence_quote"]
        return out
    out.update(valid=True, verdict=d["verdict"], quote=d["evidence_quote"], msg=msg)
    out["changed"] = out["verdict"] != out["first_verdict"]
    return out


def check_item(row: dict, resp: T.Resp | None, summary: str, p) -> dict:
    out = _overlay("item", row, str(row.get("grade")), resp, p)
    if resp is None or resp.error is not None or not isinstance(resp.data, dict):
        out["invalid_reason"] = out["error"] or "no_data"
        return out
    d = resp.data
    out.update(sl_verdict=d["verdict"], searched=d["searched"], note=d["note"])
    if d["verdict"] == "dropped":
        out.update(valid=True, verdict="dropped")
        return out
    _, tier = s1_judge.locate_summary_quote(d["summary_quote"], summary)
    out["quote"] = d["summary_quote"]
    if tier not in ("exact", "normalized"):
        out["invalid_reason"] = "summary_quote_not_in_summary"
        return out
    out.update(valid=True, verdict=d["verdict"], match=tier)
    out["changed"] = out["verdict"] != out["first_verdict"]
    return out


def positives(items: Iterable[dict], claims: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """The rows re-checked: valid items graded dropped, valid claims called unsupported."""
    its = [
        r
        for r in items
        if r.get("valid") and r.get("grade_valid", True) and r.get("grade") == ITEM_VERDICT
    ]
    cls = [r for r in claims if r.get("valid") and r.get("verdict") == CLAIM_VERDICT]
    return its, cls


def second_look(
    backend,
    items: Iterable[dict],
    claims: Iterable[dict],
    *,
    load_rec: Callable[[str], dict] | None = None,
    summary_of: Callable[[dict, dict], str] | None = None,
    prompt_set: Mapping[str, s1_judge.Prompt] | None = None,
    purpose: str = PURPOSE,
) -> list[dict]:
    """Re-check the positives among `items` and `claims` (rows of one judge) and return the
    overlay rows. `load_rec` loads the base record (default `s1_data.load`); `summary_of(row,
    rec)` gives the summary the row was graded against (default: the natural summary)."""
    if backend.family != "anthropic":
        raise ValueError("the second look runs on the Max backend (tools)")
    if load_rec is None:
        from compaction import s1_data

        load_rec = s1_data.load
    if summary_of is None:

        def summary_of(row: dict, rec: dict) -> str:
            if row.get("arm") not in (None, "natural"):
                raise ValueError(f"{row['rid']}: pass summary_of for arm {row['arm']}")
            return rec.get("summary") or ""

    p = prompt_set or prompts()
    its, cls = positives(items, claims)
    recs: dict[str, dict] = {}
    views: dict[str, object] = {}

    def base(row: dict) -> dict:
        b = str(row.get("base_rid") or row["rid"])
        if b not in recs:
            recs[b] = load_rec(b)
        return recs[b]

    reqs: list[tuple[str, dict, T.Req]] = []
    for row in cls:
        rec = base(row)
        b = str(rec["id"])
        if b not in views:
            views[b] = s1_judge.judge_view(rec)
        reqs.append(("claim", row, claim_request(row, rec, views[b], summary_of(row, rec), p)))
    for row in its:
        rec = base(row)
        reqs.append(("item", row, item_request(row, rec, summary_of(row, rec), p)))
    resps = backend.run([r for _, _, r in reqs], purpose=purpose) if reqs else {}
    out = []
    for kind, row, r in reqs:
        resp = resps.get(r.key)
        rec = base(row)
        if kind == "claim":
            out.append(check_claim(row, resp, views[str(rec["id"])], p))
        else:
            out.append(check_item(row, resp, summary_of(row, rec), p))
    return out


def apply(rows: Iterable[dict], overlay: Iterable[dict]) -> list[dict]:
    """Copies of `rows` with the second look's verdict (`grade` for items, `verdict` for claims)
    and `second_look` = {first_verdict, valid, changed}; rows not re-checked are unchanged."""
    by = {o["row_id"]: o for o in overlay}
    out = []
    for r in rows:
        rid = r.get("item_id") or r.get("claim_id")
        o = by.get(rid)
        if o is None:
            out.append(r)
            continue
        field = "grade" if o["kind"] == "item" else "verdict"
        out.append(
            r
            | {
                field: o["verdict"],
                "second_look": {k: o[k] for k in ("first_verdict", "valid", "changed")},
            }
        )
    return out
