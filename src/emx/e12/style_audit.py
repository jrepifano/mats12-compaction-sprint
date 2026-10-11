"""E12 training-data style audit (prereg §4.4, Amendment A): batch plan, prompts, parsers and the
frozen feature-selection rules. The calls go through `emx.e12.llm.run_batch`
(scripts/e12_style_audit.py drives them).

Frozen rules (prereg §4.4):
- Batches of `STYLE_BATCH` (50) rows from a shuffle of each repair set (clean, then generic;
  1,712 rows each -> 35 batches each, 70 calls) with seed `STYLE_SHUFFLE_SEED` (20261013): a
  fresh `numpy.random.default_rng(seed).permutation(n)` per set, over the set's rows in file
  order. Luna lists features that appear far more often than in typical assistant answers.
- One merge call: every (batch, feature) pair gets an integer id in discovery order (batch order,
  then the feature's position in its batch's list); luna groups synonymous ids. Counting is done
  here, not by the model: a group's count is the number of distinct batches among its ids; an id
  claimed by two groups stays with the first; ids no group claims are singletons. Keep groups
  named in >= `STYLE_MIN_BATCHES` (2) batches, at most `STYLE_MAX_FEATURES` (10), the most-named
  first; ties (including at the tenth place) go to the group named first (its smallest id).
- The list and the filled rater prompt are frozen to `contracts.STYLE_FROZEN` before any model
  output is rated. One multi-label yes/no call per item, on the assistant answer only; a
  malformed rating is retried once (`llm.run_batch`, text mode), then counted missing.
- Answers rated: sample indices 0-9 (0-4 under cut 0) of each of the 41 test questions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from emx.e12 import contracts as C
from emx.e12 import llm

PROMPTS: dict[str, str] = {
    "discover": "prompts/E12/style_discover_v1.txt",
    # v3 (2026-10-09): the one merge call names the groups only, from the distinct features; one
    # assignment call per batch then maps that batch's features onto the names. v1 (every id in
    # nested groups) and v2 (one group index per id) both looped until the token cap on the 568
    # discovered features (three calls, kept in superseded/).
    "merge": "prompts/E12/style_merge_v3.txt",
    "assign": "prompts/E12/style_assign_v1.txt",
    "rate": "prompts/E12/style_rate_v1.txt",
}
SETS: dict[str, str] = {"clean": "A", "generic": "B"}  # repair set -> `data.repair_rows` arm
MAX_TOKENS: dict[str, int] = {"discover": 400, "merge": 2000, "assign": 200, "rate": 200}
MAX_DISCOVERED = 15  # features kept per batch answer (the prompt asks for at most 10)

DISCOVERY = f"{C.STYLE_DIR}/discovery.jsonl"
MERGE_CALLS = f"{C.STYLE_DIR}/merge.calls.jsonl"
MERGE = f"{C.STYLE_DIR}/merge.json"
ASSIGN_CALLS = f"{C.STYLE_DIR}/assign.calls.jsonl"
TRAIN_RATINGS = f"{C.STYLE_DIR}/train_ratings.jsonl"


def load_template(name: str, root: str | Path = C.REPO_ROOT) -> str:
    return (Path(root) / PROMPTS[name]).read_text(encoding="utf-8")


def fill(template: str, **slots: str) -> str:
    """Replace `{slot}` for the given slots in one pass over the template only (inserted text is
    never re-scanned, and other braces, e.g. the JSON examples, are left alone)."""
    pat = re.compile(r"\{(" + "|".join(map(re.escape, slots)) + r")\}")
    return pat.sub(lambda m: str(slots[m.group(1)]), template)


def assistant_answer(row: dict) -> str:
    """The last assistant message of a training row."""
    msgs = [m for m in row["messages"] if m.get("role") == "assistant"]
    if not msgs:
        raise ValueError(f"row {row.get('id')} has no assistant message")
    return msgs[-1]["content"]


# --- discovery ----------------------------------------------------------------------------------


def make_batches(sets: dict[str, list[dict]], seed: int = C.STYLE_SHUFFLE_SEED,
                 size: int = C.STYLE_BATCH) -> list[dict]:
    """[{"item_id": "<set>-<nn>", "set", "row_ids", "_answer": joined answers}] for each set in
    `sets` order: a fresh default_rng(seed) permutation of the set's rows, cut into `size`s."""
    import numpy as np

    out = []
    for name, rows in sets.items():
        perm = np.random.default_rng(seed).permutation(len(rows))
        for b, start in enumerate(range(0, len(rows), size)):
            chunk = [rows[int(i)] for i in perm[start:start + size]]
            answers = [assistant_answer(r) for r in chunk]
            out.append({"item_id": f"{name}-{b:02d}", "set": name,
                        "row_ids": [r["id"] for r in chunk], "_answers": answers,
                        "_answer": "\n\n".join(answers)})
    return out


def render_discover(item: dict, template: str) -> str:
    block = "\n\n".join(f"Answer {i + 1}:\n{a}" for i, a in enumerate(item["_answers"]))
    return fill(template, n=str(len(item["_answers"])), answers=block)


def parse_json_object(text: str) -> dict | None:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    for cand in (t, t[t.find("{"): t.rfind("}") + 1] if "{" in t else ""):
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_features(text: str) -> dict | None:
    obj = parse_json_object(text)
    if obj is None or not isinstance(obj.get("features"), list):
        return None
    feats = [f.strip() for f in obj["features"] if isinstance(f, str) and f.strip()]
    if len(feats) != len(obj["features"]):
        return None
    return {"features": feats[:MAX_DISCOVERED]}


# --- merge and selection ------------------------------------------------------------------------


def merge_items(discovery: list[dict]) -> list[dict]:
    """[{"id", "batch", "feature"}] in discovery order from the discovery lines (batch order as
    given; lines without a scored feature list contribute nothing)."""
    out = []
    for line in discovery:
        if line.get("status") != "scored":
            continue
        for f in line["features"]:
            out.append({"id": len(out), "batch": line["item_id"], "feature": f})
    return out


def render_merge(items: list[dict], template: str, n_batches: int) -> str:
    block = "\n".join(f"{it['id']} | {it['batch']} | {it['feature']}" for it in items)
    return fill(template, n_batches=str(n_batches), features=block, n=str(len(items)),
                last=str(len(items) - 1))


def distinct_features(items: list[dict]) -> list[str]:
    """The distinct feature texts (case- and space-folded) in first-named order."""
    seen, out = set(), []
    for it in items:
        k = " ".join(it["feature"].lower().split())
        if k not in seen:
            seen.add(k)
            out.append(it["feature"])
    return out


def render_merge_names(items: list[dict], template: str, n_batches: int) -> str:
    feats = distinct_features(items)
    return fill(template, n_batches=str(n_batches), n=str(len(feats)), features="\n".join(feats))


def parse_group_names(text: str) -> dict | None:
    obj = parse_json_object(text)
    names = obj.get("groups") if obj else None
    if not (isinstance(names, list) and names
            and all(isinstance(n, str) and n.strip() for n in names)):
        return None
    out, seen = [], set()
    for n in names:  # a repeated name keeps its first position
        if n.strip().lower() not in seen:
            seen.add(n.strip().lower())
            out.append(n.strip())
    return {"names": out}


def render_assign(batch_items: list[dict], names: list[str], template: str) -> str:
    groups = "\n".join(f"{g}. {n}" for g, n in enumerate(names))
    feats = "\n".join(f"{i}. {it['feature']}" for i, it in enumerate(batch_items))
    return fill(template, groups=groups, features=feats, n=str(len(batch_items)))


def make_assign_parser(n_feats: int, n_groups: int):
    def parse(text: str) -> dict | None:
        obj = parse_json_object(text)
        a = obj.get("assignment") if obj else None
        if not (isinstance(a, list) and len(a) == n_feats
                and all(isinstance(x, int) and not isinstance(x, bool) and -1 <= x < n_groups
                        for x in a)):
            return None
        return {"assignment": a}
    return parse


def groups_from_assignment(names: list[str], batches: dict[str, list[dict]],
                           assignments: dict[str, list[int]]) -> list[dict]:
    """[{"name", "ids"}]: each feature id joins the group its batch's call assigned; -1 (or a
    batch without a scored assignment) leaves it unclaimed, i.e. its own group."""
    ids: dict[int, list[int]] = {g: [] for g in range(len(names))}
    for b, feats in batches.items():
        for it, g in zip(feats, assignments.get(b) or [], strict=False):
            if g >= 0:
                ids[g].append(it["id"])
    return [{"name": n, "ids": ids[g]} for g, n in enumerate(names)]


def parse_groups(text: str) -> dict | None:
    """{"groups": [{"name", "ids"}]} from the v2 answer (`groups` = names, `assignment` = one
    group index per feature id, in id order) or the v1 shape (groups with their ids). A short v2
    assignment leaves the remaining ids unclaimed (their own groups, in `select_features`)."""
    obj = parse_json_object(text)
    if obj is None or not isinstance(obj.get("groups"), list):
        return None
    if "assignment" in obj:
        names, assign = obj["groups"], obj["assignment"]
        if not (isinstance(assign, list) and names
                and all(isinstance(n, str) and n.strip() for n in names)
                and all(isinstance(a, int) and not isinstance(a, bool) and 0 <= a < len(names)
                        for a in assign)):
            return None
        return {"groups": [{"name": n.strip(), "ids": [i for i, a in enumerate(assign) if a == g]}
                           for g, n in enumerate(names)]}
    groups = []
    for g in obj["groups"]:
        if not (isinstance(g, dict) and isinstance(g.get("name"), str) and g["name"].strip()
                and isinstance(g.get("ids"), list)
                and all(isinstance(i, int) and not isinstance(i, bool) for i in g["ids"])):
            return None
        groups.append({"name": g["name"].strip(), "ids": list(g["ids"])})
    return {"groups": groups}


def select_features(groups: list[dict], items: list[dict],
                    min_batches: int = C.STYLE_MIN_BATCHES,
                    max_features: int = C.STYLE_MAX_FEATURES) -> list[dict]:
    """The frozen selection (module docstring). Returns [{"name", "n_batches", "first_id",
    "batches", "ids"}], most-named first."""
    by_id = {it["id"]: it for it in items}
    claimed: set[int] = set()
    merged = []
    for g in groups:
        ids = sorted({i for i in g["ids"] if i in by_id and i not in claimed})
        claimed.update(ids)
        if ids:
            merged.append({"name": g["name"], "ids": ids})
    for i in sorted(set(by_id) - claimed):  # unclaimed ids are their own group
        merged.append({"name": by_id[i]["feature"], "ids": [i]})
    out = []
    for g in merged:
        batches = sorted({by_id[i]["batch"] for i in g["ids"]})
        out.append({"name": g["name"], "n_batches": len(batches), "first_id": min(g["ids"]),
                    "batches": batches, "ids": g["ids"]})
    kept = [g for g in out if g["n_batches"] >= min_batches]
    kept.sort(key=lambda g: (-g["n_batches"], g["first_id"]))
    return kept[:max_features]


# --- rating -------------------------------------------------------------------------------------


def rater_prompt(features: list[str], template: str) -> str:
    """The rater template with the numbered feature list filled in; `{answer}` stays a slot."""
    block = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(features))
    return fill(template, features=block)


def render_rating(item: dict, frozen_prompt: str) -> str:
    return fill(frozen_prompt, answer=item["_answer"])


def make_rating_parser(features: list[str]):
    """parse(text) -> {"ratings": {feature: bool}} if every feature number 1..k has a yes/no
    (or true/false) answer, else None."""
    k = len(features)

    def parse(text: str) -> dict | None:
        obj = parse_json_object(text)
        if obj is None:
            return None
        ratings = {}
        for i, f in enumerate(features, start=1):
            v = obj.get(str(i))
            if isinstance(v, bool):
                ratings[f] = v
            elif isinstance(v, str) and v.strip().lower() in ("yes", "no"):
                ratings[f] = v.strip().lower() == "yes"
            else:
                return None
        return {"ratings": ratings} if len(ratings) == k else None

    return parse


def frozen_record(*, features: list[dict], root: str | Path = C.REPO_ROOT,
                  discovery_sha256: str, merge_sha256: str, utc: str) -> dict:
    """frozen.json's content: the feature list, the filled rater prompt and every signature;
    `content_sha256` hashes the record without itself."""
    names = [f["name"] for f in features]
    rp = rater_prompt(names, load_template("rate", root))
    rec = {
        "features": names, "feature_detail": features, "rater_prompt": rp,
        "rater_prompt_sha256": llm.sha256_text(rp),
        "prompt_sha256": {k: llm.sha256_text(load_template(k, root)) for k in PROMPTS},
        "prompt_files": dict(PROMPTS), "shuffle_seed": C.STYLE_SHUFFLE_SEED,
        "batch_size": C.STYLE_BATCH, "min_batches": C.STYLE_MIN_BATCHES,
        "max_features": C.STYLE_MAX_FEATURES, "discovery_sha256": discovery_sha256,
        "merge_sha256": merge_sha256, "judge": C.STYLE_JUDGE, "utc": utc,
    }
    rec["content_sha256"] = llm.canonical_sha256(rec)
    return rec


def load_frozen(root: str | Path = C.REPO_ROOT) -> dict:
    p = Path(root) / C.STYLE_FROZEN
    if not p.exists():
        raise llm.Refused(f"{C.STYLE_FROZEN} is absent: run discover, merge and freeze first")
    rec = json.loads(p.read_text())
    body = {k: v for k, v in rec.items() if k != "content_sha256"}
    if llm.canonical_sha256(body) != rec.get("content_sha256"):
        raise llm.Refused(f"{C.STYLE_FROZEN} content_sha256 does not verify")
    if llm.sha256_text(rec["rater_prompt"]) != rec["rater_prompt_sha256"]:
        raise llm.Refused(f"{C.STYLE_FROZEN} rater prompt sha does not verify")
    return rec


def broad_test_qids(root: str | Path = C.REPO_ROOT) -> list[str]:
    """The 41 test questions' qids (ext48 minus the `NEAR_DOMAIN_TAG` ones), file order, from the
    question file checked against `QUESTION_SHA256`."""
    p = Path(root) / C.QUESTION_FILES["ext48"]
    if llm.sha256_file(p) != C.QUESTION_SHA256["ext48"]:
        raise ValueError(f"{p}: sha256 differs from contracts.QUESTION_SHA256")
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    qids = [r["qid"] for r in rows if r.get("tag") != C.NEAR_DOMAIN_TAG]
    if len(qids) != C.N_TEST:
        raise ValueError(f"{p}: {len(qids)} test questions, expected {C.N_TEST}")
    return qids


def answer_samples(cuts) -> tuple[int, ...]:
    return C.CUT_STYLE_SAMPLES if 0 in cuts else C.STYLE_ANSWER_SAMPLES
