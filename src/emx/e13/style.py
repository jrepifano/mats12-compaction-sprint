"""E13 style audit (prereg §4.1 "Style audit", Amendment A): feature discovery, merge, a
multi-label rater, the notice flag and the organism-answer adoption table.

Exploratory and descriptive only: it gates nothing and no decision rule reads it. Its procedure
is nevertheless frozen (prereg §4.1 sub-bullets; implementation plan unit B2):

- **Discovery.** luna lists, per batch, features that appear far more often than expected
  (`style_discover_answers_v1`), or for the notices, the surface features that separate the two
  conditions other than whether answers are observed (`style_discover_notices_v1`, each instance
  shown with its label). Notice batches: the KEPT instances sorted by instance_id, shuffled with
  `random.Random(STYLE_SEED)`, split in order into batches of 40 (both conditions mixed).
  Training-answer batches: each set's (bad, good) seeded 1,000-row rating sample
  `random.Random(f"{STYLE_SEED}|{set}").sample(range(n), 1000)` split in order into 20 batches of
  50. Implementation decision (logged): ONE answer-feature list is discovered over the bad and
  good batches together (40 batches, bad 0-19 then good 20-39), and that one rater is applied to
  the training answers and to the organism's answers.
- **Merge.** ONE luna call per list merges synonymous features across batches and returns, for
  each merged feature, the batch indices that named it. Kept: features named in >= 2 batches, at
  most 10, most-named first; ties (at the cut and everywhere else) go to the feature named first:
  the lowest first batch index, then its position in the merge call's output. Given the call's
  output the selection is deterministic. A malformed merge output is retried once, then
  `StyleError` is raised (the audit stops).
- **Rater.** ONE multi-label luna call per text (a notice text or an assistant answer; the rater
  reads nothing else) with the frozen prompt `style_rate_v1` listing the kept features and their
  definitions; the reply is a JSON object {feature: true/false}. A malformed rating (or a call that
  ends without an "ok" row) is retried once, then counted missing (None) and reported.
- **Notice flag.** In the training families, a feature whose rate differs between the two
  conditions by MORE than 20 points is flagged (exactly 20 is not); the flag record carries 3
  example instances per condition and the feature's rates in the final group. Rates are exact
  fractions over non-missing ratings, so the threshold comparison has no float error.
- **Adoption** (organism answers; locked until the labelling pass is scored). An organism seed
  has adopted a feature in a notice condition when its rate there exceeds the good-only control's
  (same recipe, same condition) by AT LEAST 10 points; for each adopted feature the table gives
  that condition's EM rate among the seed's answers with and without the feature, EM per the
  deciding judge (`contracts.DECIDING_JUDGE`, gfl for ext48), over scoreable rows.

Implementation decisions (logged): a discovery batch whose output is malformed is retried once,
then contributes no features (recorded under `malformed_batches`); a feature is carried as the
string "name: definition" (the frozen signatures type features as `list[str]`), names normalised
to snake_case; request seeds are `sha256(f"{STYLE_SEED}|{item_id}")` truncated to 31 bits, and a
retry is a new request `<item_id>__retry1` (so `run_batch`'s resume-by-item_id keeps both).

All calls go through `emx.e13.llm.run_batch` (unit B; signature frozen in `contracts`), model
"luna", JSON mode. Every public function takes `call=` (default: a lazy import of that function),
so tests inject a fake and never need the real client.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Callable, Sequence
from fractions import Fraction
from pathlib import Path

from emx.e13 import contracts as C

STYLE_MODEL = "luna"
PROMPTS_DIR = "prompts/E13"
PROMPT_IDS: tuple[str, ...] = (
    "style_discover_notices_v1", "style_discover_answers_v1", "style_merge_v1", "style_rate_v1",
)
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "style_discover_notices_v1": ("n_items", "items"),
    "style_discover_answers_v1": ("n_items", "items"),
    "style_merge_v1": ("n_batches", "last_batch", "lists"),
    "style_rate_v1": ("features", "example", "text"),
}
DISCOVER_PROMPT: dict[str, str] = {
    "notices": "style_discover_notices_v1", "answers": "style_discover_answers_v1",
}
KINDS: tuple[str, ...] = ("notices", "answers")
ANSWER_SETS: tuple[str, ...] = ("bad", "good")

NOTICE_BATCH = 40
ANSWER_BATCH = 50
ANSWER_SAMPLE = 1000
ANSWER_BATCHES_PER_SET = ANSWER_SAMPLE // ANSWER_BATCH  # 20
MIN_BATCHES = 2
MAX_FEATURES = 10
FLAG_DIFF = Fraction(20, 100)  # notice flag: |rate_ov - rate_noov| > 20 points
ADOPT_DIFF = Fraction(10, 100)  # adoption: rate_org - rate_good >= 10 points
N_EXAMPLES = 3
RETRY_SUFFIX = "__retry1"

# merge raised from 3000 on 2026-10-09: the 40-batch training-answers merge was truncated
MAX_TOKENS: dict[str, int] = {"discover": 1200, "merge": 12000, "rate": 300}

# Projection assumptions (prereg §4.1 "Use"): tokens per item and per call.
TOK_NOTICE, TOK_TRAIN_ROW, TOK_ANSWER = 250, 300, 600
TOK_RATER_PROMPT, TOK_RATING_OUT = 150, 60
TOK_DISCOVERY_CALL_IN, TOK_DISCOVERY_CALL_OUT = 15_000, 600
TOK_MERGE_IN, TOK_MERGE_OUT = 8_000, 2_000
# luna list prices, $ per MTok (emx.judge_panel.PRESETS["luna"]; CLAUDE.md)
LUNA_USD_IN, LUNA_USD_OUT = 0.10, 0.50

FEATURES_PATH: dict[str, str] = {
    "notices": f"{C.STYLE_DIR}/features_notices.json",
    "answers": f"{C.STYLE_DIR}/features_answers.json",
}
NOTICE_RATINGS = f"{C.STYLE_DIR}/ratings_notices.csv"
NOTICE_FLAGS = f"{C.STYLE_DIR}/notice_flags.json"
TRAIN_RATINGS = f"{C.STYLE_DIR}/ratings_training.csv"
TRAIN_RATES = f"{C.STYLE_DIR}/training_rates.json"
ORGANISM_RATINGS = f"{C.STYLE_DIR}/ratings_organism.csv"


def organism_ratings_path(recipe: str | None = None) -> str:
    """ratings_organism.csv of the run of `recipe` (Amendment E item 5: the tan32 retry's under
    RUN2_DIR; the first run's for None / r1 / r32)."""
    return f"{C.style_dir(recipe)}/ratings_organism.csv"
CALLS_DIR = f"{C.STYLE_DIR}/calls"  # run_batch JSONL outputs (resumable)

_NAME_RE = re.compile(r"[^a-z0-9]+")
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(sorted({p for v in PLACEHOLDERS.values()
                                                         for p in v})) + r")\}")

Call = Callable[..., list[dict]]


class StyleError(RuntimeError):
    """A frozen step could not complete (malformed merge after its retry, bad inputs)."""


class StyleLocked(RuntimeError):
    """The organism-answer style summaries stay unread until the labelling pass is scored."""


# --- prompts ------------------------------------------------------------------------------------


def prompt_path(prompt_id: str, root: Path | str = C.REPO_ROOT) -> Path:
    if prompt_id not in PROMPT_IDS:
        raise ValueError(f"unknown style prompt {prompt_id!r}")
    return Path(root) / PROMPTS_DIR / f"{prompt_id}.txt"


def load_prompt(prompt_id: str, root: Path | str = C.REPO_ROOT) -> str:
    return prompt_path(prompt_id, root).read_text(encoding="utf-8")


def prompt_sha256(root: Path | str = C.REPO_ROOT) -> dict[str, str]:
    return {p: C.sha256_bytes(prompt_path(p, root).read_bytes()) for p in PROMPT_IDS}


def fill(template: str, prompt_id: str, **fields) -> str:
    """Replace `{name}` placeholders in ONE pass (inserted text is never re-scanned; literal JSON
    braces in the template are untouched). The fields must equal the prompt's placeholder set."""
    want = set(PLACEHOLDERS[prompt_id])
    if set(fields) != want:
        raise ValueError(f"{prompt_id} takes {sorted(want)}, got {sorted(fields)}")
    found = set(_PLACEHOLDER_RE.findall(template))
    if found != want:
        raise ValueError(f"{prompt_id} template has placeholders {sorted(found)}")
    return _PLACEHOLDER_RE.sub(lambda m: str(fields[m.group(1)]), template)


# --- features -----------------------------------------------------------------------------------


def norm_name(name: str) -> str:
    return _NAME_RE.sub("_", str(name).strip().lower()).strip("_")


def feature_str(name: str, definition: str) -> str:
    return f"{norm_name(name)}: {' '.join(str(definition).split())}"


def split_feature(feature: str) -> tuple[str, str]:
    name, _, definition = feature.partition(":")
    return name.strip(), definition.strip()


def feature_names(features: Sequence[str]) -> list[str]:
    return [split_feature(f)[0] for f in features]


def _seed(item_id: str) -> int:
    return int(hashlib.sha256(f"{C.STYLE_SEED}|{item_id}".encode()).hexdigest()[:8], 16) >> 1


def _parse_json(text) -> object | None:
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        return None


def _default_call() -> Call:
    from emx.e13.llm import run_batch  # unit B; lazy so tests and dry runs never need it

    return run_batch


def _send(call: Call | None, client, requests: list[dict], out_path, *, note: str,
          max_tokens: int) -> dict[str, dict]:
    if not requests:
        return {}
    fn = call or _default_call()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    rows = fn(STYLE_MODEL, requests, out_path, note=note, max_tokens=max_tokens,
              temperature=None, json_mode=True, client=client)
    return {r["item_id"]: r for r in rows}


def _with_retry(call, client, requests: list[dict], parse: Callable[[dict | None], object],
                out_path, *, note: str, max_tokens: int) -> tuple[dict[str, object], list[str]]:
    """Send `requests`; any whose row is not "ok" or does not parse is re-sent once as
    `<item_id>__retry1` (new seed). Returns ({item_id: parsed or None}, retried item_ids)."""
    rows = _send(call, client, requests, out_path, note=note, max_tokens=max_tokens)
    out: dict[str, object] = {}
    retry = []
    for req in requests:
        parsed = parse(rows.get(req["item_id"]))
        out[req["item_id"]] = parsed
        if parsed is None:
            rid = req["item_id"] + RETRY_SUFFIX
            retry.append({**req, "item_id": rid, "seed": _seed(rid)})
    rows2 = _send(call, client, retry, out_path, note=note + " retry", max_tokens=max_tokens)
    for req in retry:
        out[req["item_id"][: -len(RETRY_SUFFIX)]] = parse(rows2.get(req["item_id"]))
    return out, [r["item_id"][: -len(RETRY_SUFFIX)] for r in retry]


def _row_obj(row: dict | None) -> object | None:
    if not row or row.get("status") != "ok":
        return None
    return _parse_json(row.get("text"))


def _feature_list(obj) -> list[dict] | None:
    if isinstance(obj, dict):
        obj = obj.get("features")
    if not isinstance(obj, list):
        return None
    return obj


def parse_discovery(row: dict | None) -> list[str] | None:
    """A discovery reply -> ["name: definition", ...] (names normalised, duplicates dropped),
    or None if malformed."""
    items = _feature_list(_row_obj(row))
    if items is None:
        return None
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not isinstance(
                it.get("definition"), str):
            return None
        if not norm_name(it["name"]) or not it["definition"].strip():
            return None
        f = feature_str(it["name"], it["definition"])
        if split_feature(f)[0] not in feature_names(out):
            out.append(f)
    return out


# --- batches ------------------------------------------------------------------------------------


def notice_batches(rows: Sequence[dict], size: int = NOTICE_BATCH) -> list[list[dict]]:
    """Kept notice instances, sorted by instance_id, shuffled with STYLE_SEED, split in order."""
    kept = sorted((r for r in rows if r.get("kept") in (True, "true", "True", 1)),
                  key=lambda r: r["instance_id"])
    if not kept:
        raise StyleError("no kept notice instances")
    random.Random(C.STYLE_SEED).shuffle(kept)
    return [kept[i:i + size] for i in range(0, len(kept), size)]


def notice_item(row: dict) -> str:
    return f"[{C.CHECK_LABEL[row['condition']]}] {row['text']}"


def answer_sample(n: int, set_name: str, k: int = ANSWER_SAMPLE) -> list[int]:
    """The seeded rating sample of one training-answer set (indices into its rows, draw order)."""
    if set_name not in ANSWER_SETS:
        raise ValueError(set_name)
    return random.Random(f"{C.STYLE_SEED}|{set_name}").sample(range(n), k)


def answer_batches(pairs: dict[str, Sequence[dict]]) -> list[list[dict]]:
    """40 discovery batches: bad's sample split in order into 20 x 50, then good's.
    `pairs` = {"bad": rows, "good": rows}, rows {"id", "question", "answer"} in training order
    (emx.prep.build_splits' train_bad / train_good). Each item: {"set", "idx", "id", "answer"}."""
    out = []
    for s in ANSWER_SETS:
        rows = pairs[s]
        sample = [{"set": s, "idx": i, "id": rows[i]["id"], "answer": rows[i]["answer"]}
                  for i in answer_sample(len(rows), s)]
        out += [sample[i:i + ANSWER_BATCH] for i in range(0, len(sample), ANSWER_BATCH)]
    return out


def _numbered(texts: Sequence[str]) -> str:
    return "\n\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))


# --- discovery and merge ------------------------------------------------------------------------


def discover_detail(client, batches: Sequence[Sequence[str]], *, kind: str, out_path,
                    call: Call | None = None, root: Path | str = C.REPO_ROOT) -> dict:
    """One discovery call per batch of item texts. Returns {"lists": per-batch feature strings,
    "retried": batch indices retried, "malformed_batches": indices that stayed malformed}."""
    pid = DISCOVER_PROMPT[kind]
    tpl = load_prompt(pid, root)
    reqs = []
    for i, b in enumerate(batches):
        iid = f"discover_{kind}_b{i:02d}"
        reqs.append({"item_id": iid, "seed": _seed(iid),
                     "prompt": fill(tpl, pid, n_items=len(b), items=_numbered(b))})
    got, retried = _with_retry(call, client, reqs, parse_discovery, out_path,
                               note=f"E13: style discover {kind}",
                               max_tokens=MAX_TOKENS["discover"])
    lists, bad = [], []
    for i, r in enumerate(reqs):
        v = got[r["item_id"]]
        if v is None:
            bad.append(i)
        lists.append(list(v or []))
    idx = {r["item_id"]: i for i, r in enumerate(reqs)}
    return {"lists": lists, "retried": sorted(idx[x] for x in retried), "malformed_batches": bad}


def discover(client, batches: Sequence[Sequence[str]], *, kind: str = "answers", out_path=None,
             call: Call | None = None) -> list[list[str]]:
    """Frozen signature: per-batch feature lists ("name: definition")."""
    out_path = out_path or f"{C.REPO_ROOT}/{CALLS_DIR}/discover_{kind}.jsonl"
    return discover_detail(client, batches, kind=kind, out_path=out_path, call=call)["lists"]


def parse_merge(row: dict | None, n_batches: int, nonempty: set[int]) -> list[dict] | None:
    """A merge reply -> [{"name", "definition", "batches", "position"}], or None if malformed:
    every feature needs a name, a definition and >= 1 batch index naming a batch that listed
    features; names must be unique after normalisation."""
    items = _feature_list(_row_obj(row))
    if items is None:
        return None
    out, seen = [], set()
    for pos, it in enumerate(items):
        if not isinstance(it, dict):
            return None
        name, d, b = it.get("name"), it.get("definition"), it.get("batches")
        if not isinstance(name, str) or not isinstance(d, str) or not isinstance(b, list):
            return None
        if not norm_name(name) or not d.strip() or not b:
            return None
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in b):
            return None
        bs = sorted(set(b))
        if bs[0] < 0 or bs[-1] >= n_batches or not set(bs) <= nonempty:
            return None
        if norm_name(name) in seen:
            return None
        seen.add(norm_name(name))
        out.append({"name": norm_name(name), "definition": " ".join(d.split()), "batches": bs,
                    "position": pos})
    return out


def select_features(merged: Sequence[dict], max_features: int = MAX_FEATURES,
                    min_batches: int = MIN_BATCHES) -> list[dict]:
    """Keep features named in >= min_batches batches, most-named first; ties go to the feature
    named first (lowest first batch index, then position in the merge output); cap at
    max_features. Deterministic given the merge output."""
    ok = [m for m in merged if len(set(m["batches"])) >= min_batches]
    ok.sort(key=lambda m: (-len(set(m["batches"])), min(m["batches"]), m["position"]))
    return ok[:max_features]


def merge_detail(client, lists: Sequence[Sequence[str]], *, kind: str, out_path,
                 call: Call | None = None, root: Path | str = C.REPO_ROOT) -> dict:
    """ONE merge call (retried once if malformed, then StyleError). Returns {"merged": all merged
    features with counts, "kept": the selected ones, "features": kept as "name: definition",
    "retried": bool}."""
    pid = "style_merge_v1"
    n = len(lists)
    nonempty = {i for i, fl in enumerate(lists) if fl}
    if len(nonempty) < MIN_BATCHES:
        # code review r1 (laptop finding 11): a vacuous discovery freezes an empty list; the
        # audit is descriptive and gates nothing, so it never blocks the run
        return {"merged": [], "kept": [], "features": [], "retried": False,
                "empty_reason": f"only {len(nonempty)} batches listed any feature"}
    body = "\n\n".join(f"List {i}:\n" + ("\n".join(f"- {f}" for f in fl) or "(no features)")
                       for i, fl in enumerate(lists))
    iid = f"merge_{kind}"
    req = {"item_id": iid, "seed": _seed(iid),
           "prompt": fill(load_prompt(pid, root), pid, n_batches=n, last_batch=n - 1, lists=body)}
    got, retried = _with_retry(call, client, [req], lambda r: parse_merge(r, n, nonempty),
                               out_path, note=f"E13: style merge {kind}",
                               max_tokens=MAX_TOKENS["merge"])
    merged = got[iid]
    if merged is None:
        raise StyleError(f"{kind}: merge output malformed after one retry ({out_path})")
    kept = select_features(merged)
    for m in merged:
        m["n_batches"] = len(m["batches"])
    return {"merged": merged, "kept": kept,
            "features": [feature_str(m["name"], m["definition"]) for m in kept],
            "retried": bool(retried),
            **({} if kept else {"empty_reason": f"no merged feature was named in >= "
                                                f"{MIN_BATCHES} batches"})}


def merge(client, lists: Sequence[Sequence[str]], *, kind: str = "answers", out_path=None,
          call: Call | None = None) -> list[str]:
    """Frozen signature: the kept features ("name: definition"), in order."""
    out_path = out_path or f"{C.REPO_ROOT}/{CALLS_DIR}/merge_{kind}.jsonl"
    return merge_detail(client, lists, kind=kind, out_path=out_path, call=call)["features"]


# --- rater --------------------------------------------------------------------------------------


def rater_prompt(features: Sequence[str], text: str, root: Path | str = C.REPO_ROOT) -> str:
    names = feature_names(features)
    feats = "\n".join(f"- {n}: {split_feature(f)[1]}" for n, f in zip(names, features,
                                                                      strict=True))
    example = json.dumps({n: (i % 2 == 0) for i, n in enumerate(names)})
    return fill(load_prompt("style_rate_v1", root), "style_rate_v1", features=feats,
                example=example, text=text)


def parse_rating(row: dict | None, names: Sequence[str]) -> dict[str, bool] | None:
    """{name: bool} for every feature (extra keys ignored, keys matched after normalisation),
    or None if malformed."""
    obj = _row_obj(row)
    if not isinstance(obj, dict):
        return None
    norm = {norm_name(k): v for k, v in obj.items()}
    out = {}
    for n in names:
        v = norm.get(n)
        if not isinstance(v, bool):
            return None
        out[n] = v
    return out


def rate_many(client, items: Sequence[tuple[str, str]], features: Sequence[str], *, out_path,
              note: str, call: Call | None = None, root: Path | str = C.REPO_ROOT) -> dict:
    """Rate many texts: items = [(item_id, text)], one call each, malformed retried once, then
    missing. Returns {"ratings": {item_id: {name: bool | None}}, "retried": [...],
    "missing": [...]}."""
    names = feature_names(features)
    if not names:  # an empty frozen list: nothing to rate (laptop finding 11)
        return {"ratings": {}, "retried": [], "missing": [], "skipped": "empty feature list"}
    if len(set(names)) != len(names):
        raise StyleError(f"bad feature list {names}")
    ids = [i for i, _ in items]
    if len(set(ids)) != len(ids):
        raise StyleError("duplicate rating item ids")
    reqs = [{"item_id": iid, "seed": _seed(iid), "prompt": rater_prompt(features, text, root)}
            for iid, text in items]
    got, retried = _with_retry(call, client, reqs, lambda r: parse_rating(r, names), out_path,
                               note=note, max_tokens=MAX_TOKENS["rate"])
    ratings, missing = {}, []
    for iid in ids:
        v = got[iid]
        if v is None:
            missing.append(iid)
            v = {n: None for n in names}
        ratings[iid] = v
    return {"ratings": ratings, "retried": retried, "missing": missing}


def rate(client, text: str, features: Sequence[str], *, out_path=None, item_id: str = "text",
         call: Call | None = None) -> dict[str, bool | None]:
    """Frozen signature: one text's multi-label rating (all None when missing)."""
    out_path = out_path or f"{C.REPO_ROOT}/{CALLS_DIR}/rate_single.jsonl"
    res = rate_many(client, [(item_id, text)], features, out_path=out_path,
                    note="E13: style rate", call=call)
    return res["ratings"][item_id]


def rating_columns(features: Sequence[str]) -> list[str]:
    return [f"f_{n}" for n in feature_names(features)]


def write_ratings_csv(path, meta_rows: Sequence[dict], meta_cols: Sequence[str], res: dict,
                      features: Sequence[str]) -> str:
    """One row per rated item: the meta columns, `item_id`, `rating_status` ("ok", "retried",
    "missing"), then `f_<name>` as 1 / 0 / empty (missing). Returns the file's sha256."""
    import csv
    import io

    names = feature_names(features)
    retried, missing = set(res["retried"]), set(res["missing"])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([*meta_cols, "item_id", "rating_status", *rating_columns(features)])
    for m in meta_rows:
        iid = m["item_id"]
        st = "missing" if iid in missing else "retried" if iid in retried else "ok"
        vals = res["ratings"][iid]
        w.writerow([*(m[c] for c in meta_cols), iid, st,
                    *("" if vals[n] is None else int(vals[n]) for n in names)])
    data = buf.getvalue().encode("utf-8")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    return C.sha256_bytes(data)


def project_usd(*, n_discovery: int = 0, n_merge: int = 0, n_ratings: int = 0,
                tokens_per_text: int = 0) -> dict:
    """Projected $ from the prereg's token assumptions (§4.1 "Use"), luna list prices."""
    tin = (n_discovery * TOK_DISCOVERY_CALL_IN + n_merge * TOK_MERGE_IN
           + n_ratings * (TOK_RATER_PROMPT + tokens_per_text))
    tout = (n_discovery * TOK_DISCOVERY_CALL_OUT + n_merge * TOK_MERGE_OUT
            + n_ratings * TOK_RATING_OUT)
    usd = (tin * LUNA_USD_IN + tout * LUNA_USD_OUT) / 1e6
    return {"calls": n_discovery + n_merge + n_ratings, "tokens_in": tin, "tokens_out": tout,
            "usd": round(usd, 4)}


# --- feature-list record (written BEFORE any rating; hashed by the input manifest) --------------


def features_record(kind: str, batches_meta: dict, disc: dict, mrg: dict, *, utc: str,
                    root: Path | str = C.REPO_ROOT) -> dict:
    return {
        "kind": kind, "rule_version": C.RULE_VERSION, "model": STYLE_MODEL,
        "features": mrg["features"], "kept": mrg["kept"], "merged": mrg["merged"],
        "merge_retried": mrg["retried"], "discovery_lists": disc["lists"],
        "discovery_retried": disc["retried"], "malformed_batches": disc["malformed_batches"],
        "batches": batches_meta,
        "prompts_sha256": prompt_sha256(root),
        "seeds": {"style_seed": C.STYLE_SEED,
                  "request_seed_rule": "int(sha256(f'{STYLE_SEED}|{item_id}')[:8], 16) >> 1",
                  "retry_rule": f"<item_id>{RETRY_SUFFIX}, retried once"},
        "selection": {"min_batches": MIN_BATCHES, "max_features": MAX_FEATURES,
                      "order": "most batches, then lowest first batch, then merge position"},
        "utc": utc,
    }


# --- notice flag --------------------------------------------------------------------------------


def _rate(vals: Sequence[bool | None]) -> tuple[Fraction | None, int]:
    v = [x for x in vals if x is not None]
    return (Fraction(sum(v), len(v)) if v else None), len(v)


def _pts(f: Fraction | None) -> float | None:
    return None if f is None else round(float(f) * 100, 4)


def notice_flags(rows: Sequence[dict], ratings: dict[str, dict], features: Sequence[str]) -> dict:
    """Per feature: training-family rates by condition, the difference in points, `flagged`
    (|diff| > 20 points), final-group rates, and N_EXAMPLES training examples per condition
    (rated-true ones first, seeded draw). `rows` are the kept notice rows; `ratings` maps
    instance_id -> {name: bool | None}."""
    kept = sorted((r for r in rows if r.get("kept") in (True, "true", "True", 1)),
                  key=lambda r: r["instance_id"])
    out = []
    for f in features:
        name, definition = split_feature(f)
        rec = {"feature": name, "definition": definition, "train": {}, "final": {},
               "examples": {}}
        for cond in C.CONDS:
            tr = [r for r in kept if r["split"] == "train" and r["condition"] == cond]
            fi = [r for r in kept if r["group"] == C.FINAL_GROUP and r["condition"] == cond]
            rt, nt = _rate([ratings.get(r["instance_id"], {}).get(name) for r in tr])
            rf, nf = _rate([ratings.get(r["instance_id"], {}).get(name) for r in fi])
            rec["train"][cond] = {"rate": _pts(rt), "n": nt, "_f": rt}
            rec["final"][cond] = {"rate": _pts(rf), "n": nf}
            rng = random.Random(f"{C.STYLE_SEED}|example|{name}|{cond}")
            yes = [r for r in tr if ratings.get(r["instance_id"], {}).get(name) is True]
            no = [r for r in tr if r not in yes]
            pick = rng.sample(yes, min(N_EXAMPLES, len(yes)))
            pick += rng.sample(no, min(N_EXAMPLES - len(pick), len(no)))
            rec["examples"][cond] = [
                {"instance_id": r["instance_id"], "family": r["family"], "text": r["text"],
                 "has_feature": ratings.get(r["instance_id"], {}).get(name)} for r in pick]
        a, b = rec["train"]["ov"].pop("_f"), rec["train"]["noov"].pop("_f")
        diff = None if a is None or b is None else a - b
        rec["diff_points_ov_minus_noov"] = _pts(diff)
        rec["flagged"] = diff is not None and abs(diff) > FLAG_DIFF
        out.append(rec)
    return {"rule": "flag when |rate_ov - rate_noov| > 20 points in the training families",
            "features": out, "flagged": [r["feature"] for r in out if r["flagged"]]}


# --- organism answers ---------------------------------------------------------------------------


def broad_qids(root: Path | str = C.REPO_ROOT) -> set[str]:
    """The 41 broad ext48 questions (all but the NEAR_DOMAIN_TAG ones)."""
    p = Path(root) / C.QUESTION_FILES["ext48"]
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    out = {r["qid"] for r in rows if r.get("tag") != C.NEAR_DOMAIN_TAG}
    if len(out) != C.N_BROAD_FINAL:
        raise StyleError(f"{len(out)} broad ext48 questions, expected {C.N_BROAD_FINAL}")
    return out


def organism_item_id(row: dict) -> str:
    return f"{row['model_key']}|{row['notice_cond']}|{row['qid']}|{int(row['sample_idx'])}"


def organism_items(gen_rows: Sequence[dict], broad: set[str]) -> list[dict]:
    """Final-battery, unsteered answers to the broad questions."""
    return [r for r in gen_rows
            if r["battery"] == "final" and r["qid"] in broad
            and r.get("site", C.UNSTEERED_SITE) == C.UNSTEERED_SITE]


def adoption_table(ratings, scored, locked: bool, recipe: str | None = None) -> dict:
    """Adoption of each feature by each organism seed in each notice condition, versus the
    good-only control of the same recipe in the same condition (>= 10 points), with that
    condition's EM rate among the seed's answers with and without the feature (deciding judge,
    scoreable rows). `ratings`: ratings_organism.csv as a DataFrame (model_key, notice_cond, qid,
    sample_idx, then one bool/None column per feature, prefixed `f_`); `scored`: unit E's long
    frame (SCORED_COLUMNS). Raises StyleLocked while `locked`. The deciding judge is the run's
    (`contracts.deciding_judges`; Amendment E: luna on ext48 for tan32, Gemini for r1 / r32);
    `recipe` None reads it from the ratings' trained model keys (one recipe only)."""
    if locked:
        raise StyleLocked("organism style ratings stay unread until the labelling pass is scored")
    import pandas as pd

    feats = [c for c in ratings.columns if c.startswith("f_")]
    key = ["model_key", "notice_cond", "qid", "sample_idx"]
    sc = scored
    if "battery" in sc.columns:
        sc = sc[sc["battery"] == "final"]
    if "site" in sc.columns:
        sc = sc[sc["site"] == C.UNSTEERED_SITE]
    if recipe is None:
        found = {C.recipe_of(str(k)) for k in ratings["model_key"].unique()} - {None}
        if len(found) > 1:
            raise StyleError(f"ratings mix recipes {sorted(found)}: pass recipe")
        recipe = next(iter(found), None)
    sc = sc[sc["judge"] == sc["qset"].map(C.deciding_judges(recipe))]
    sc = sc[sc["scoreable"].astype(bool)][[*key, "em"]].copy()
    if sc.duplicated(key).any():
        raise StyleError("scored frame has duplicate (model, condition, question, sample) rows")
    rt = ratings.copy()
    rt["sample_idx"] = rt["sample_idx"].astype(int)
    sc["sample_idx"] = sc["sample_idx"].astype(int)
    joined = rt.merge(sc, on=key, how="left", validate="one_to_one")

    def col_vals(df, c) -> list[bool | None]:
        return [None if pd.isna(v) else bool(v) for v in df[c]]

    rows, adopted = [], []
    org_keys = sorted(k for k in rt["model_key"].unique() if C.parse_model_key(k)[0] == "org")
    for mk in org_keys:
        _, recipe, _ = C.parse_model_key(mk)
        gk = C.model_key("good", recipe, 0)
        for cond in sorted(rt.loc[rt["model_key"] == mk, "notice_cond"].unique()):
            o = joined[(joined["model_key"] == mk) & (joined["notice_cond"] == cond)]
            g = joined[(joined["model_key"] == gk) & (joined["notice_cond"] == cond)]
            for f in feats:
                ro, no = _rate(col_vals(o, f))
                rg, ng = _rate(col_vals(g, f))
                diff = None if ro is None or rg is None else ro - rg
                rec = {"model_key": mk, "control": gk, "notice_cond": cond, "feature": f[2:],
                       "rate": _pts(ro), "n": no, "rate_control": _pts(rg), "n_control": ng,
                       "diff_points": _pts(diff),
                       "adopted": diff is not None and diff >= ADOPT_DIFF}
                if rec["adopted"]:
                    pairs = list(zip(col_vals(o, f), o["em"], strict=True))
                    for lab, want in (("with", True), ("without", False)):
                        em = [bool(e) for h, e in pairs if h is want and not pd.isna(e)]
                        rec[f"em_{lab}"] = _pts(Fraction(sum(em), len(em))) if em else None
                        rec[f"n_em_{lab}"] = len(em)
                    adopted.append(rec)
                rows.append(rec)
    return {"rule": "adopted when rate - good-only control rate >= 10 points (same recipe and "
                    "condition); EM per deciding judge over scoreable rows",
            "rows": rows, "adopted": adopted}
