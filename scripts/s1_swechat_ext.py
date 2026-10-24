"""Experiment 1 extension: a seeded batch of SWE-chat compactions beyond Experiment 1's 250, and a
SWE-chat-only precision page (brief logs/s2/briefs/swechat-extension.md, scope cut to 50 records
by Jacob on 24 Oct; Experiment 1 report docs/sprint/S1/report.md, spec docs/sprint/specs/S1.md).

Experiment 1 judged a seeded, (model, harness)-stratified sample of 250 of the 928 eligible
SWE-chat v1 records. This runs the same judge, unchanged, on a seeded simple random sample of
`N_RECORDS` of the other 678, enough to draw the precision page, and reports the SWE-chat
numbers for them (descriptive at this size). It is an extension, reported separately:
Experiment 1's sealed files are only read (through `seal.open_sealed`, which verifies the
release), never written.

    uv run python scripts/s1_swechat_ext.py ids                  # the pool and the run order
    nohup uv run python scripts/s1_swechat_ext.py supervise > logs/s1/swechat_ext.log 2>&1 &
    uv run python scripts/s1_swechat_ext.py page                 # 24-card blind page
    uv run python scripts/s1_swechat_ext.py summary
    uv run python scripts/s1_swechat_ext.py score --labels data/raw/sprint/s1/swechat_ext/db/labels

- **Records:** the pool is every SWE-chat v1 record `s1_data.index_all` marks eligible (Part
  A's rule) that is not in the manifest's Part A. It is put in a seeded random order
  (`ORDER_SEED`) and the first `--n` (default `N_RECORDS`) are judged, so they are a simple
  random sample of the pool and a larger `--n` later extends the same order.
- **Judge:** `s1_run`'s Haiku Max backend at Experiment 1's concurrency and effort, its
  content-keyed cache, `s1_judge.judge_records` and the second look on items and claims as in
  Part A (`s1_run._second_look_into` with a "partA" sink). Both freezes are checked first
  (`s1_run.check_frozen`, `s1_second_look.check_frozen`). Max calls book $0 rows under
  SPRINT-S1 through the backend. Outputs go to results/sprint/S1/swechat_ext/, rewritten after
  every batch; a rerun resends only what is not cached.
- **Usage guard (`supervise`):** runs `run` as a child and stops it (SIGTERM; the backend's
  job guard kills its `claude -p` sessions) when any Max window in the shared usage log reaches
  `s1_usage_watch.PAUSE_AT`, then probes until every window is below `RESUME_BELOW` and
  relaunches. Anchored log lines: SWECHAT_EXT ...
- **Summary:** the report's SWE-chat numbers (lost rate by item type, pooled lost rate, share of
  compactions with a user rule that lost one, invented span share and summaries with one) with
  `s1_analysis`'s estimators (session-group bootstrap) for: the 250 as sampled (a check that
  this code reproduces the report), the 250 reweighted to the eligible population (the
  manifest's stratum weights), the extension sample, the two pooled (descriptive), and a
  population estimate for all 928 (the 250 at weight 1, as the records themselves, and each
  extension record at pool size / n, standing for the unsampled rest). The 250 were drawn evenly
  across (model, harness) strata, while the pool is 86% Opus 4.6, so the sets are also broken
  down by model.
- **Page:** item cards from the judged extension records' lost items (dropped or misstated, availability lost,
  after the second look: the definition behind Experiment 1's 13/18 precision) plus a few kept
  items, each stratum a simple random sample with its inclusion probability in the key. Built
  with `s1_label_page.build(item_first=True)`, plus a "Can't tell" choice at step 1 that the
  shared template lacks (`add_cant_tell`). No verdict on the page; key under
  data/raw/sprint/s1/swechat_ext/ (gitignored); the builder's leak check runs on the final HTML.
- **Score:** precision of "lost" = Jacob labels the item dropped or misstated (not an item and
  kept are misses; can't tell is left out), with a Wilson interval and a session-group
  bootstrap, plus kept agreement; every note is printed for reading.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd
import s1_label_page as LP
import s1_label_score as LS
import s1_run as R
import s1_usage_watch as W

from compaction import s1_analysis as A
from compaction import s1_data, s1_judge
from compaction import s1_second_look as SL
from compaction.s1_types import S1_SEED
from emx.stats import cluster_bootstrap_ci

SOURCE = "swe-chat-v1"
JUDGE = "haiku-max"
ORDER_SEED = "s1-swechat-ext:20261024"
PAGE_SEED = 20261024
N_LOST, N_KEPT = 20, 4
N_RECORDS = 50  # Jacob, 24 Oct: enough for the page; the rest of the pool only when told
OUT = ROOT / "results" / "sprint" / "S1" / "swechat_ext"
PRIVATE = ROOT / "data" / "raw" / "sprint" / "s1" / "swechat_ext"
FILES = {
    "items": "items_haiku-max.jsonl",
    "claims": "claims_haiku-max.jsonl",
    "facts": "code_facts_haiku-max.jsonl",
    "second_look": "second_look_haiku-max.jsonl",
    "roster": "roster_haiku-max.json",
}
CHECK_S = 30
MAX_RESTARTS = 3  # relaunches after a run that failed for a reason other than a pause


def say(msg: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


# ------------------------------------------------------------------------------------------
# Records
# ------------------------------------------------------------------------------------------


def extension_ids(index: list[dict] | None = None, manifest: dict | None = None) -> list[str]:
    """Eligible SWE-chat records not in Experiment 1's Part A, sorted."""
    idx = index if index is not None else s1_data.index_all()
    part_a = set((manifest or R.load_manifest())["ids"]["part_a"])
    return sorted(
        e["id"] for e in idx if e["source"] == SOURCE and e["eligible"] and e["id"] not in part_a
    )


def run_order(ids: list[str], seed: str = ORDER_SEED) -> list[str]:
    out = sorted(ids)
    random.Random(seed).shuffle(out)
    return out


def cmd_ids(a) -> None:
    index = s1_data.index_all()
    ids = extension_ids(index)
    by = {e["id"]: e for e in index}
    print(f"SWECHAT_EXT ids {len(ids)} eligible SWE-chat records outside Experiment 1's 250")
    for k, n in Counter(f"{by[r]['model']} | {by[r]['harness']}" for r in ids).most_common():
        print(f"  {n:4d}  {k}")


# ------------------------------------------------------------------------------------------
# Judging
# ------------------------------------------------------------------------------------------


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, default=str) + "\n" for r in rows)


def write_outputs(sink, out: Path, meta: dict) -> None:
    _atomic(out / FILES["items"], _jsonl(sink.items))
    _atomic(out / FILES["claims"], _jsonl(sink.claims))
    _atomic(out / FILES["facts"], _jsonl(sink.facts))
    _atomic(out / FILES["second_look"], _jsonl(sink.second_look))
    _atomic(out / FILES["roster"], json.dumps(sink.roster, indent=1, sort_keys=True))
    _atomic(out / "meta.json", json.dumps(meta, indent=1, sort_keys=True, default=str) + "\n")


def config_hashes() -> dict:
    cfg = R.frozen_config()
    return {
        "frozen_config": str(R.FROZEN.relative_to(ROOT)),
        "prompts_sha256": cfg["prompts"],
        "schemas_sha256": cfg["schemas"],
        "haiku": cfg["haiku"],
        "ptb_prompts_sha256": cfg["ptb_prompts"],
        "second_look_config_sha": SL.frozen_config_sha(),
    }


def cmd_run(a) -> None:
    R.check_frozen()
    R._check_second_look("haiku")
    pool = run_order(extension_ids())
    order = pool[: a.n]
    be = R.backend("haiku", a.concurrency)
    # prefix "partA": the second look re-checks claims too (s1_run.SECOND_LOOK_CLAIMS)
    sink = R._Sealed("partA", be.name, second_look=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    errors: list[dict] = []
    batches = list(R.batches(order, R.MAX_BATCH))
    say(
        f"SWECHAT_EXT run start: {len(order)} records, {len(batches)} batches, "
        f"concurrency {be.concurrency}"
    )
    for i, chunk in enumerate(batches, 1):
        i0, c0 = len(sink.items), len(sink.claims)
        res = s1_judge.judge_records(
            be,
            chunk,
            out_items=sink.items.extend,
            out_claims=sink.claims.extend,
            out_code_facts=sink.facts.extend,
            purpose="s1_judge",
        )
        sink.add_run({r: s1_data.load(r) for r in chunk}, "natural", res["errors"])
        errors += res["errors"]
        R._second_look_into(
            sink,
            be,
            sink.items[i0:],
            sink.claims[c0:],
            arm="natural",
            replays=None,
            load=s1_data.load,
        )
        done = [r for b in batches[:i] for r in b]
        meta = {
            "brief": "logs/s2/briefs/swechat-extension.md",
            "order_seed": ORDER_SEED,
            "pool_size": len(pool),
            "pool_sha256": hashlib.sha256("\n".join(sorted(pool)).encode()).hexdigest(),
            "record_ids": order,
            "n_records": len(order),
            "n_judged": len(done),
            "batches_done": i,
            "n_batches": len(batches),
            "batch_size": R.MAX_BATCH,
            "complete": i == len(batches),
            "run_started_utc": started,
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "judge": be.name,
            "concurrency": be.concurrency,
            "config": config_hashes(),
            "n_failed_records": sum(not m["ok"] for m in sink.roster.values()),
            "failed_records": sorted(r for r, m in sink.roster.items() if not m["ok"]),
            "errors": errors,
        }
        write_outputs(sink, OUT, meta)
        say(
            f"SWECHAT_EXT batch {i}/{len(batches)} done: {len(chunk)} records, valid_share "
            f"{res.get('valid_share')}, errors {len(res['errors'])}"
        )
    say(
        f"SWECHAT_EXT run done: {len(sink.items)} items, {len(sink.claims)} claims, "
        f"{len(sink.second_look)} second-look rows, {meta['n_failed_records']} failed records"
    )


def _usage_high() -> tuple[str, float, float] | None:
    r = W.latest_reading()
    if r and W.worst(r)[1] >= W.PAUSE_AT:
        return W.worst(r)
    return None


def _wait_below() -> None:
    """Sleep and probe (as s1_usage_watch does) until every Max window is below RESUME_BELOW."""
    while True:
        r = W.latest_reading()
        if not r or W.worst(r)[1] < W.RESUME_BELOW:
            return
        w, u, reset = W.worst(r)
        wait = W.POLL_S if reset <= time.time() else min(W.POLL_S, reset + W.MARGIN_S - time.time())
        say(f"SWECHAT_EXT PAUSED {w} util={u}; next probe in {max(30.0, wait):.0f} s")
        time.sleep(max(30.0, wait))
        W.probe()


def _stop(proc: subprocess.Popen, wait_s: float = 120.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(wait_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        say("SWECHAT_EXT ERROR run did not stop on SIGTERM; process group SIGKILLed")


RATE_LIMIT_MARKS = ("rate_limit", "rate limit", "429", "overloaded")
STATE = OUT / "supervise_state.json"  # the concurrency in force, kept across supervisor restarts


def _rate_limited(out: Path = OUT) -> int:
    """Error fields of the last run (meta.json's first-pass errors and the second-look rows)
    that look like rate limiting; a usage-window limit ("limit") the backend waits out itself."""
    errs: list[str] = []
    try:
        errs += [str(e.get("error")) for e in json.loads((out / "meta.json").read_text())["errors"]]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    try:
        errs += [str(o.get("error")) for o in _read_jsonl(out / FILES["second_look"])]
    except (OSError, json.JSONDecodeError):
        pass
    return sum(any(k in e.lower() for k in RATE_LIMIT_MARKS) for e in errs)


def _saved_concurrency(default: int) -> int:
    try:
        return int(json.loads(STATE.read_text())["concurrency"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return default


def _exit_on(signum, frame) -> None:
    raise SystemExit(128 + signum)


def cmd_supervise(a) -> int:
    """Run `run` under the usage guard; SIGTERM, SIGINT and SIGHUP stop the child first. After a
    run whose records hit rate limits, relaunch once at half the concurrency (the cache keeps
    what succeeded), logged."""
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _exit_on)
    say(f"SWECHAT_EXT supervise start: pause_at={W.PAUSE_AT} resume_below={W.RESUME_BELOW}")
    failures = 0
    # s1_run.backend's Max default (8) is Experiment 1's setting; a halving persists in STATE
    conc = a.concurrency or _saved_concurrency(8)
    halved = conc < 8
    while True:
        _wait_below()
        cmd = [sys.executable, str(Path(__file__).resolve()), "run", "--n", str(a.n)]
        cmd += ["--concurrency", str(conc)]
        proc = subprocess.Popen(cmd, cwd=ROOT, start_new_session=True)
        paused = False
        try:
            while proc.poll() is None:
                time.sleep(CHECK_S)
                hi = _usage_high()
                if hi and proc.poll() is None:
                    say(f"SWECHAT_EXT PAUSE {hi[0]} util={hi[1]}: stopping the run")
                    _stop(proc)
                    paused = True
        finally:
            if proc.poll() is None:
                _stop(proc)
        if paused:
            continue
        n_rl = _rate_limited()
        if n_rl and not halved and conc > 1:
            conc, halved = max(1, conc // 2), True
            _atomic(STATE, json.dumps({"concurrency": conc, "utc": time.time()}) + "\n")
            say(f"SWECHAT_EXT RATE_LIMIT {n_rl} errors: relaunching at concurrency {conc}")
            continue
        if proc.returncode == 0:
            say("SWECHAT_EXT supervise done rc=0")
            return 0
        failures += 1
        say(f"SWECHAT_EXT ERROR run exited rc={proc.returncode} ({failures}/{MAX_RESTARTS})")
        if failures >= MAX_RESTARTS:
            return proc.returncode or 1
        time.sleep(60)


# ------------------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------------------


def _read_jsonl(p: Path) -> list[dict]:
    with open(p) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_extension(out: Path = OUT) -> dict:
    meta = json.loads((out / "meta.json").read_text())
    items, claims, slsum = A.apply_second_look(
        _read_jsonl(out / FILES["items"]),
        _read_jsonl(out / FILES["claims"]),
        _read_jsonl(out / FILES["second_look"]),
        config=SL.frozen_config_sha(),
    )
    roster = json.loads((out / FILES["roster"]).read_text())
    return {"items": items, "claims": claims, "roster": roster, "second_look": slsum, "meta": meta}


def load_experiment1(reader=A.default_reader) -> dict:
    """Experiment 1's SWE-chat rows from the released seal, second look applied."""
    roster = {
        r: m for r, m in reader(f"partA_roster_{JUDGE}.json").items() if m.get("source") == SOURCE
    }
    keep = set(roster)
    items = [r for r in reader(f"partA_items_{JUDGE}.jsonl") if r["rid"] in keep]
    claims = [r for r in reader(f"partA_claims_{JUDGE}.jsonl") if r["rid"] in keep]
    overlay = [o for o in reader(f"partA_second_look_{JUDGE}.jsonl") if o["rid"] in keep]
    items, claims, slsum = A.apply_second_look(
        items, claims, overlay, config=SL.frozen_config_sha()
    )
    return {"items": items, "claims": claims, "roster": roster, "second_look": slsum}


def _ci(d: dict | None) -> dict | None:
    if not d:
        return None
    return {k: d.get(k) for k in ("point", "lo", "hi", "n_clusters")}


def set_numbers(items, claims, roster, *, weights=None, by_model: bool = True) -> dict:
    """The report's SWE-chat numbers for one set of records."""
    sfx = "_weighted" if weights is not None else ""
    kw = {"weights": weights}
    out: dict[str, Any] = {
        "n_records": len(roster),
        "n_records_ok": sum(bool(m.get("ok")) for m in roster.values()),
    }
    out["lost_by_type"] = {
        r["type"]: {
            "n_items": r["n_items"],
            "n_lost": r["n_lost"],
            "lost_rate": _ci(r[f"lost_rate{sfx}"]),
        }
        for r in A.item_rates(items, ("type",), **kw)
    }
    pooled = A.item_rates(items, ("source",), **kw)[0]
    out["pooled"] = {
        "n_items": pooled["n_items"],
        "n_lost": pooled["n_lost"],
        "n_session_groups": pooled["n_session_groups"],
        "lost_rate": _ci(pooled[f"lost_rate{sfx}"]),
        "dropped_rate": _ci(pooled[f"dropped_rate{sfx}"]),
    }
    rule = A.rule_loss_share(items, roster=roster, **kw)[0]
    out["user_rule_loss"] = {
        "n_compactions": rule["n_compactions"],
        "n_with_user_rule": rule["n_with_user_rule"],
        "n_losing": rule["n_losing"],
        "share": _ci(rule[f"share{sfx}"]),
    }
    inv = A.invented_rates(claims, roster=roster, **kw)[0]
    out["invented_exploratory"] = {
        "n_summaries": inv["n_summaries"],
        "n_spans": inv["n_spans"],
        "n_invented_spans": inv["n_invented_spans"],
        "span_share": _ci(inv[f"span_share{sfx}"]),
        "summaries_with_invented": _ci(inv[f"summaries_with_invented{sfx}"]),
    }
    if by_model and weights is None:
        out["by_model"] = {
            r["model"]: {
                "n_items": r["n_items"],
                "n_compactions": r["n_compactions"],
                "n_lost": r["n_lost"],
                "lost_rate": _ci(r["lost_rate"]),
            }
            for r in A.item_rates(items, ("model",))
        }
        out["user_rule_loss_by_model"] = {
            r["model"]: {
                "n_with_user_rule": r["n_with_user_rule"],
                "n_losing": r["n_losing"],
                "share": _ci(r["share"]),
            }
            for r in A.rule_loss_share(items, ("model",), roster=roster)
        }
    return out


def difference(a_items, b_items) -> dict:
    """Pooled lost rate of set b minus set a. Interval: session groups resampled jointly over
    both sets (a group in both keeps both contributions), N_BOOT draws, seed S1_SEED."""
    ga, gb = (
        A._item_frame(x).groupby("session_group")[["lost", "n"]].sum() for x in (a_items, b_items)
    )
    g = ga.join(gb, how="outer", lsuffix="_a", rsuffix="_b").fillna(0.0)
    la, na = g["lost_a"].to_numpy(float), g["n_a"].to_numpy(float)
    lb, nb = g["lost_b"].to_numpy(float), g["n_b"].to_numpy(float)
    rng = np.random.default_rng(S1_SEED)
    draws = []
    for _ in range(A.N_BOOT):
        pick = rng.integers(0, len(g), len(g))
        da, db = na[pick].sum(), nb[pick].sum()
        if da > 0 and db > 0:
            draws.append(lb[pick].sum() / db - la[pick].sum() / da)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    return {
        "point": lb.sum() / nb.sum() - la.sum() / na.sum(),
        "lo": float(lo),
        "hi": float(hi),
        "n_session_groups": len(g),
        "n_shared_session_groups": int(((na > 0) & (nb > 0)).sum()),
        "n_boot_valid": len(draws),
    }


def points_only(d: Any) -> Any:
    """`d` with every interval's lo, hi and n_clusters set to None (a point estimate only)."""
    if isinstance(d, dict):
        return {
            k: (None if k in ("lo", "hi", "n_clusters") else points_only(v)) for k, v in d.items()
        }
    return d


def cmd_summary(a) -> None:
    ext = load_extension()
    e1 = load_experiment1()
    manifest = R.load_manifest()
    weights = {k: v for k, v in A.stratum_weights(manifest).items() if k[0] == SOURCE}
    both = {
        "items": e1["items"] + ext["items"],
        "claims": e1["claims"] + ext["claims"],
        "roster": {**e1["roster"], **ext["roster"]},
    }
    overlap = set(e1["roster"]) & set(ext["roster"])
    if overlap:
        raise SystemExit(f"{len(overlap)} records in both sets: {sorted(overlap)[:3]}")
    meta = ext["meta"]
    w_ext = meta["pool_size"] / len(ext["roster"])

    def pop_w(r) -> float:
        return 1.0 if r["rid"] in e1["roster"] else w_ext

    summary = {
        "note": "Experiment 1 extension (brief logs/s2/briefs/swechat-extension.md): the judge "
        "unchanged on a seeded simple random sample of the eligible SWE-chat v1 records outside "
        "Experiment 1's 250 (descriptive at this size). Rates are "
        "what the single LLM judge detects, after the second look; not precision-corrected. "
        "Intervals: session-group bootstrap (s1_analysis, N_BOOT resamples, seed S1_SEED).",
        "extension_complete": meta["complete"],
        "pool_size": meta["pool_size"],
        "order_seed": meta["order_seed"],
        "population_estimate_weights": {"experiment1_250": 1.0, "extension": w_ext},
        "n_extension_records": meta["n_records"],
        "n_extension_judged": meta["n_judged"],
        "failed_records": meta["failed_records"],
        "config": meta["config"],
        "second_look": {"extension": ext["second_look"], "experiment1_250": e1["second_look"]},
        "sets": {
            "experiment1_250": set_numbers(e1["items"], e1["claims"], e1["roster"]),
            "experiment1_250_population_weighted": set_numbers(
                e1["items"], e1["claims"], e1["roster"], weights=weights
            ),
            "extension": set_numbers(ext["items"], ext["claims"], ext["roster"]),
            "pooled_descriptive": set_numbers(both["items"], both["claims"], both["roster"]),
            # points only: a bootstrap over every session group would resample the 250, which
            # are fixed here, so its interval would not match the design (astra r1)
            "population_estimate": points_only(
                set_numbers(both["items"], both["claims"], both["roster"], weights=pop_w)
            ),
        },
        "pooled_lost_difference_extension_minus_250": difference(e1["items"], ext["items"]),
        "max_usage": max_usage(set(meta["record_ids"])),
    }
    _atomic(OUT / "summary.json", json.dumps(summary, indent=1, sort_keys=True) + "\n")
    _atomic(OUT / "summary.md", summary_md(summary))
    print(summary_md(summary))


def max_usage(rids: set[str]) -> dict:
    """Max calls on the extension's records from the shared usage log: calls, tokens and the
    notional API-equivalent cost (billed $0 on the Max plan)."""
    c: Counter = Counter()
    with open(W.USAGE) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(r.get("rid", "")).split("@")[0] not in rids:
                continue
            c["calls"] += 1
            c["errors"] += bool(r.get("error"))
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens"):
                c[k] += r.get(k) or 0
            c["notional_usd"] += r.get("notional_usd") or 0.0
            c[f"purpose:{r.get('purpose')}"] += 1
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in c.items()}


def _pct(d: dict | None) -> str:
    if not d or d.get("point") is None:
        return "n/a"
    if d.get("lo") is None or d.get("hi") is None:
        return f"{100 * d['point']:.1f}%"
    return f"{100 * d['point']:.1f}% [{100 * d['lo']:.1f}, {100 * d['hi']:.1f}]"


TYPES = ("rule", "permission", "decision", "commitment", "task_state")
SET_NAMES = {
    "experiment1_250": "Experiment 1's 250 (as sampled)",
    "experiment1_250_population_weighted": "Experiment 1's 250, reweighted to the population",
    "extension": "Extension",
    "pooled_descriptive": "250 + extension, pooled (descriptive)",
    "population_estimate": "Population estimate, all eligible",
}


def summary_md(s: dict) -> str:
    sets = s["sets"]
    lines = [
        "# Experiment 1 extension: a seeded sample of further SWE-chat compactions",
        "",
        (
            f"Extension: {s['n_extension_judged']} records judged, a seeded simple random sample "
            f"of the {s['pool_size']} eligible SWE-chat records outside Experiment 1's 250 "
            f"(complete: {s['extension_complete']}; {len(s['failed_records'])} failed). At this "
            "size its numbers are descriptive. Rates are the judge's, after the second look, not "
            "precision-corrected; intervals resample session groups. The 250 were drawn evenly "
            "across (model, harness) strata and the pool is mostly Opus 4.6, so compare them by "
            "model or with the reweighted row. The population estimate weights the 250 at 1 and "
            f"each extension record at {s['population_estimate_weights']['extension']:.2f}."
        ),
        "",
        "## Lost rate by item type",
        "",
        "| Set | " + " | ".join(TYPES) + " | All types |",
        "|---|" + "---|" * (len(TYPES) + 1),
    ]
    for k, name in SET_NAMES.items():
        v = sets[k]
        cells = [
            f"{_pct(v['lost_by_type'].get(t, {}).get('lost_rate'))} "
            f"({v['lost_by_type'].get(t, {}).get('n_items', 0):,})"
            for t in TYPES
        ]
        cells.append(f"{_pct(v['pooled']['lost_rate'])} ({v['pooled']['n_items']:,})")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    d = s["pooled_lost_difference_extension_minus_250"]
    lines += [
        "",
        (
            f"Pooled lost rate, extension minus the 250 as sampled: {100 * d['point']:+.1f} "
            f"points [{100 * d['lo']:+.1f}, {100 * d['hi']:+.1f}]."
        ),
        "",
        "## User-given rules and invention",
        "",
        (
            "| Set | Compactions with a user rule losing one | Invented span share (exploratory) "
            "| Summaries with an invented span (exploratory) |"
        ),
        "|---|---|---|---|",
    ]
    for k, name in SET_NAMES.items():
        v = sets[k]
        r, i = v["user_rule_loss"], v["invented_exploratory"]
        lines.append(
            f"| {name} | {_pct(r['share'])} ({r['n_losing']}/{r['n_with_user_rule']}) | "
            f"{_pct(i['span_share'])} ({i['n_invented_spans']}/{i['n_spans']}) | "
            f"{_pct(i['summaries_with_invented'])} ({i['n_summaries']}) |"
        )
    lines += [
        "",
        "## By model (lost rate; user-rule share)",
        "",
        "| Set | Model | Items | Lost rate | Compactions with a user rule losing one |",
        "|---|---|---|---|---|",
    ]
    for k in ("experiment1_250", "extension", "pooled_descriptive"):
        v = sets[k]
        for m, x in sorted(v["by_model"].items(), key=lambda kv: -kv[1]["n_items"]):
            ru = v["user_rule_loss_by_model"].get(m, {})
            lines.append(
                f"| {SET_NAMES[k]} | {m} | {x['n_items']:,} | {_pct(x['lost_rate'])} | "
                f"{_pct(ru.get('share'))} ({ru.get('n_losing', 0)}/{ru.get('n_with_user_rule', 0)}) |"
            )
    u = s["max_usage"]
    lines += [
        "",
        (
            f"Max usage on the extension's records: {u.get('calls', 0):,} calls, "
            f"notional ${u.get('notional_usd', 0):,.2f} (billed $0 on the Max plan)."
        ),
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------
# Labelling page
# ------------------------------------------------------------------------------------------

_STEP1_NO = '${radio("isitem", "i", "no", "Not an item", "none")}'
_STEP1_UNSURE = '${radio("isitem", "i", "unsure", "Can\'t tell", "none")}'
_ONEDIT = (
    '    if (t.value === "no") l.label = "not_an_item";\n'
    '    else if (l.label === "not_an_item") delete l.label;'
)
_ONEDIT_NEW = (
    '    if (t.value === "no") l.label = "not_an_item";\n'
    '    else if (t.value === "unsure") l.label = "cant_tell";\n'
    '    else if (l.label === "not_an_item" || l.label === "cant_tell") delete l.label;'
)
_RUBRIC = "Then: kept, dropped or misstated.</p>"
_RUBRIC_NEW = (
    "Then: kept, dropped or misstated. <strong>Can't tell</strong> (step 1): even the full "
    "history isn't enough to decide whether it is an item; these are left out of scoring.</p>"
)


HEADING = "Experiment 1 extension: SWE-chat labels"
LEDE = (
    "Blind labels only: for each item, first decide from the history alone whether it is an "
    "item, then whether the summary kept it. No judge verdicts are shown. The Gold windows tab "
    "is empty on this page."
)


def retitle(html: str) -> str:
    """This page's heading and lede in place of the validation page's (which promise gold
    windows); each must occur exactly once."""
    for old, new in ((LP.LABELS_HEADING, HEADING), (LP.LABELS_LEDE, LEDE)):
        if html.count(old) != 1:
            raise ValueError(f"template changed: {old[:40]!r} found {html.count(old)} times")
        html = html.replace(old, new)
    return html


def add_cant_tell(html: str) -> str:
    """Add a step-1 "Can't tell" choice (saved as label cant_tell) to an item-first page; the
    shared template has none for items. Each replaced string must occur exactly once."""
    for old, new in (
        (_STEP1_NO, _STEP1_NO + _STEP1_UNSURE),
        (_ONEDIT, _ONEDIT_NEW),
        (_RUBRIC, _RUBRIC_NEW),
    ):
        if html.count(old) != 1:
            raise ValueError(f"template changed: {old[:40]!r} found {html.count(old)} times")
        html = html.replace(old, new)
    return html


def draw_units(
    items: list[dict],
    rids: set[str],
    seed: int = PAGE_SEED,
    n_lost: int = N_LOST,
    n_kept: int = N_KEPT,
) -> tuple[list[dict], dict]:
    """Item units (s1_analysis.label_units, second look applied) on `rids`, in two strata: lost
    (dropped or misstated with availability lost) and kept, each a seeded simple random sample.
    Returns (units in a seeded page order, frame {stratum: {N, n, pi}})."""
    rows = [r for r in items if r["rid"] in rids]
    units = A.label_units(rows, [], halves={r: "held" for r in rids})
    first: dict[tuple, dict] = {}
    for r in sorted(
        (r for r in rows if A.usable_item(r)),
        key=lambda r: (r["rid"], r["judge"], str(r["item_id"])),
    ):
        first.setdefault((r["rid"], A.norm_key(r["quote"])), r)
    pools: dict[str, list[dict]] = {"item|lost|held": [], "item|kept|held": []}
    for u in units:
        row = first[(u["rid"], A.norm_key(u["text"]))]
        u["availability"] = row.get("availability")
        u["item_row_id"] = str(row["item_id"])
        if A.is_lost(row):
            pools["item|lost|held"].append(u)
        elif row.get("grade") == "kept":
            pools["item|kept|held"].append(u)
    rng = random.Random(seed)
    chosen, frame = [], {}
    for name, k in (("item|lost|held", n_lost), ("item|kept|held", n_kept)):
        pool = pools[name]
        k = min(k, len(pool))
        frame[name] = {"N": len(pool), "n": k, "pi": k / len(pool) if pool else None}
        for u in rng.sample(pool, k):
            u = dict(u)
            u.update(
                stratum=name,
                stratum_N=len(pool),
                stratum_n=k,
                pi=k / len(pool),
                pos=name == "item|lost|held",
                unit_id=A.unit_id_of("item", u["rid"], u["text"]),
            )
            chosen.append(u)
    rng.shuffle(chosen)
    return chosen, frame


def previously_labelled_groups() -> set[str]:
    """Session groups of every record behind an earlier Experiment 1 label or audit key."""
    groups = set()
    for p in (ROOT / "data" / "raw" / "sprint" / "s1").glob("**/*key*.private.json"):
        if PRIVATE in p.parents:
            continue
        k = json.loads(p.read_text())
        for u in (k.get("units") or {}).values():
            groups.add(s1_data.session_group(u.get("source") or "", u.get("session_id") or ""))
        for x in (k.get("audit") or {}).values():
            priv = x.get("private") or {}
            if priv.get("rid"):
                groups.add(
                    s1_data.session_group(
                        s1_data.source_of(priv["rid"]), s1_data.session_of(priv["rid"])
                    )
                )
    return groups


def cmd_page(a) -> int:
    ext = load_extension()
    meta = ext["meta"]
    if not meta["complete"]:
        raise SystemExit("the run hasn't finished")
    rids = set(meta["record_ids"]) & {r for r, m in ext["roster"].items() if m.get("items_ok")}
    prior = previously_labelled_groups()
    by_group = {r: ext["roster"][r].get("session_group") for r in rids}
    shared = {r for r, g in by_group.items() if g in prior}
    rids -= shared
    units, frame = draw_units(ext["items"], rids)
    recs: dict[str, dict] = {}

    def rec(rid: str) -> dict:
        if rid not in recs:
            recs[rid] = s1_data.load(rid)
        return recs[rid]

    html, key = LP.build(
        units,
        history_text=lambda rid: s1_data.render(rec(rid)).text,
        summary_text=lambda rid: rec(rid)["summary"],
        frame=frame,
        seed=PAGE_SEED,
        item_first=True,
        title="SWE-chat labels",
        meta={
            "brief": "logs/s2/briefs/swechat-extension.md",
            "population": "judged extension records (the seeded first --n of the pool)",
            "n_records_frame": len(rids),
            "n_records_dropped_shared_session_with_earlier_labels": len(shared),
            "lost_definition": "grade dropped or misstated and availability lost, after the "
            "second look (s1_analysis.is_lost)",
            "items_sha256": hashlib.sha256((OUT / FILES["items"]).read_bytes()).hexdigest(),
            "second_look_sha256": hashlib.sha256(
                (OUT / FILES["second_look"]).read_bytes()
            ).hexdigest(),
            "order_seed": ORDER_SEED,
        },
    )
    html = add_cant_tell(retitle(html))
    shown = "\0".join(
        [s1_data.render(rec(r)).text for r in sorted({u["base_rid"] for u in units})]
        + [rec(r)["summary"] for r in sorted({u["rid"] for u in units})]
    )
    # the histories are gzipped in the page, so the check reads them in plain text too
    key["natural_rid_hits"] = LP.leak_check(html + "\0" + shown, key, shown)
    key["page_sha256"] = hashlib.sha256(html.encode()).hexdigest()
    key["page_bytes"] = len(html.encode())
    key["cant_tell_patch"] = True
    info = LP.write(PRIVATE / "page", html, key, force=a.force)
    print(
        f"SWECHAT_EXT page {len(units)} cards, draw {key['draw']}, frame {frame}, "
        f"{info['bytes']:,} bytes; {len(shared)} records left out for sharing a session "
        f"with earlier labels"
    )
    print(f"  {info['page']}\n  {info['key']}")
    return 0


# ------------------------------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------------------------------


def read_labels(path: Path) -> dict[str, dict]:
    """{unit id: doc} from an ArtifactData export directory (<id>.json files) or the page's
    copyable JSON list."""
    if path.is_dir():
        return {p.stem: json.loads(p.read_text()) for p in sorted(path.glob("*.json"))}
    return LS.normalize_labels(json.loads(path.read_text()))["labels"]


def score(key: dict, labs: dict[str, dict]) -> dict:
    """Precision of the judge's lost calls and agreement on its kept calls, on Jacob's labels.
    Labels from another draw are refused."""
    bad = sorted(u for u, d in labs.items() if d.get("draw") != key["draw"])
    if bad:
        raise SystemExit(f"{len(bad)} labels from another draw (e.g. {bad[:3]})")
    units = key["units"]
    valid = (*LS.ITEM_LABELS, "cant_tell")
    jacob = {u: (labs.get(u) or {}).get("label") for u in units}
    out: dict[str, Any] = {
        "draw": key["draw"],
        "n_units": len(units),
        "n_labelled": sum(lab in valid for lab in jacob.values()),
        "labels": dict(Counter(lab for lab in jacob.values() if lab in valid)),
        "invalid_labels": {u: lab for u, lab in jacob.items() if lab and lab not in valid},
        "not_on_page": sorted(set(labs) - set(units)),
    }
    out["n_cant_tell"] = sum(lab == "cant_tell" for lab in jacob.values())
    rows = []
    for uid, u in units.items():
        lab = jacob[uid]
        if lab not in valid or lab == "cant_tell":
            continue
        hit = lab in A.LOST_GRADES if u["stratum"] == "item|lost|held" else lab == "kept"
        grp = s1_data.session_group(u.get("source") or "", u.get("session_id") or "")
        rows.append(
            {
                "uid": uid,
                "stratum": u["stratum"],
                "hit": int(hit),
                "one": 1,
                "session_group": grp,
                "label": lab,
            }
        )
    df = pd.DataFrame(rows, columns=["uid", "stratum", "hit", "one", "session_group", "label"])
    for name, s in (("lost_precision", "item|lost|held"), ("kept_agreement", "item|kept|held")):
        g = df[df["stratum"] == s]
        if g.empty:
            out[name] = {"n": 0}
            continue
        w = LS.weighted_share(list(g["hit"].astype(bool)), [1.0] * len(g))
        boot = cluster_bootstrap_ci(
            g,
            ["session_group"],
            lambda x: x["hit"].sum() / x["one"].sum(),
            n_boot=A.N_BOOT,
            seed=S1_SEED,
        )
        out[name] = {
            "n_hits": int(g["hit"].sum()),
            "n": len(g),
            "wilson": [w["lo"], w["hi"]],
            "point": w["point"],
            "session_bootstrap": [boot["lo"], boot["hi"]],
            "n_session_groups": boot["n_clusters"],
            "labels": dict(Counter(g["label"])),
        }
    out["notes"] = {uid: d["note"] for uid, d in labs.items() if (d.get("note") or "").strip()}
    return out


def cmd_score(a) -> int:
    key = json.loads((PRIVATE / "page" / LP.KEY_NAME).read_text())
    res = score(key, read_labels(Path(a.labels)))
    out = LP.require_private(PRIVATE / "score.json")
    _atomic(out, json.dumps(res, indent=1, sort_keys=True) + "\n")
    lp, ka = res.get("lost_precision", {}), res.get("kept_agreement", {})
    print(
        f"SWECHAT_EXT score {res['n_labelled']}/{res['n_units']} labelled, "
        f"{res['n_cant_tell']} can't tell"
    )
    if lp.get("n"):
        print(
            f"  lost precision {lp['n_hits']}/{lp['n']} = {lp['point']:.2f}, Wilson "
            f"[{lp['wilson'][0]:.2f}, {lp['wilson'][1]:.2f}], session bootstrap "
            f"[{lp['session_bootstrap'][0]:.2f}, {lp['session_bootstrap'][1]:.2f}]; {lp['labels']}"
        )
    if res["invalid_labels"] or res["not_on_page"]:
        print(f"  INVALID {res['invalid_labels']}; not on the page: {res['not_on_page']}")
    if ka.get("n"):
        print(f"  kept agreement {ka['n_hits']}/{ka['n']}; {ka['labels']}")
    labs = read_labels(Path(a.labels))
    for uid, note in res["notes"].items():
        u = key["units"].get(uid, {})
        print(
            f"  NOTE {uid} [{u.get('stratum')}; judge {u.get('verdicts', {}).get(JUDGE)}, "
            f"{u.get('availability')}; Jacob {(labs.get(uid) or {}).get('label')}]: {note}"
        )
    print(f"  {out}")
    return 0 if res["n_labelled"] == res["n_units"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ids")
    for name in ("run", "supervise"):
        p = sub.add_parser(name)
        p.add_argument("--concurrency", type=int)
        p.add_argument("--n", type=int, default=N_RECORDS, help="records judged, from the order")
    sub.add_parser("summary")
    p = sub.add_parser("page")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("score")
    p.add_argument("--labels", required=True)
    a = ap.parse_args(argv)
    fn = {
        "ids": cmd_ids,
        "run": cmd_run,
        "supervise": cmd_supervise,
        "summary": cmd_summary,
        "page": cmd_page,
        "score": cmd_score,
    }[a.cmd]
    return fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
