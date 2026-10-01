"""S4 pilot: Inspect eval logs -> results/S4/episodes.csv, one scored row per episode.

Each sample is read with `score.load_artifacts`, its hidden key rebuilt with
`content.build_world(arm, seed).hidden`, and scored with `score.score_episode` (plan §1.10–1.12).

Judging costs money, so the real panel runs only with `--judge openrouter --yes`. It goes
through a request cache (`results/S4/judge_cache.jsonl`), so a re-run costs nothing new, and a
budget guard (code review r1, findings 1–2):
- Episodes are scored in groups by target model. A group uses the judge reservation that
  `scripts/s4_run.py` took for that model at stage launch (`contracts.PENDING_JUDGE_RESERVATIONS`,
  `{"<model>": {"rid", "usd", "episodes"}}`), if there is one.
- Before each episode, `score.estimate_judge_usd` bounds its judge cost. If what is left of the
  group's reservations is less than that, the shortfall is reserved (`budget.reserve`) before any
  call. If that is refused, scoring stops: the remaining episodes are written as unscored rows
  marked scoring_incomplete, and nothing more is called.
- `judges.BudgetGuard` refuses any single call that would take new spend past what is reserved.
- After the group, its newly billed spend is settled against its own reservations, and only then
  is that model's pending entry removed. No other model's reservation is touched.
`usd_judges` in a row is the billed cost of that episode's judgements (cached replies count at
their original price); the ledger gets only what this pass newly billed.
`--judge none` scores without any calls: every judged item is then listed in `judge_errors` and
the row is scoring_incomplete.

Jacob's final alert labels (`data/S4/labels/*.csv` with columns `item_ref,label`, from the blind
bundle written by `scripts/s4_label_bundle.py`) are merged into `human_alert`, `channel_alert`
and `false_alert`. Only `contracts.FINAL_ALERT_LABELS` ("alert", "no_alert") are valid; any other
non-empty value stops the merge with the offending rows listed (finding 4). A column stays empty
while any of its primary items is unlabelled; it is never read as a negative.
`first_alert_turn` is recomputed from the final labels (finding 10): the earliest turn over the
primary items Jacob labelled alert. A ticket counts at its turn. An end-state file counts at the
first snapshot turn holding its end-state version (same sha256), or at the earliest earlier
version the panel labelled alert if the panel also flagged it. No extra judge calls are made, so
an earlier version the panel did not flag never moves the turn earlier. While labels are
incomplete, `first_alert_turn` is the panel's (provisional) timing.
`--merge-only` re-merges labels into an existing episodes.csv without scoring.

Usage:
  uv run --group swarm python scripts/s4_summarize.py --logs results/S4/logs --judge none
  uv run --group swarm python scripts/s4_summarize.py --logs results/S4/logs --judge openrouter --yes
  uv run --group swarm python scripts/s4_summarize.py --merge-only
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path

from s4world import budget
from s4world import score as S
from s4world.contracts import (
    ARMS,
    EPISODE_COLUMNS,
    FINAL_ALERT_LABELS,
    JUDGE_PANEL,
    PENDING_JUDGE_RESERVATIONS,
    EpisodeArtifacts,
    Hidden,
)

WT = Path(__file__).resolve().parents[1]
OUT = WT / "results" / "S4" / "episodes.csv"
LABELS_DIR = WT / "data" / "S4" / "labels"
CACHE = WT / "results" / "S4" / "judge_cache.jsonl"
PENDING = WT / PENDING_JUDGE_RESERVATIONS
LABEL_COLUMNS = ("human_alert", "channel_alert", "false_alert")
# $ per million tokens (in, out), from the project notes (2026-09-28)
# Bounding prices, $/Mtok (in, out): the highest input and output rates over all providers in
# OpenRouter's /api/v1/models/<id>/endpoints on 2026-09-30 (DeepSeek: Azure $0.21 in, Relace $1.28
# out; luna: the OpenAI $0.20/$1.00 tier). The judge client sends them as OpenRouter's
# provider.max_price, so no call is routed above them. The ledger records billed cost.
JUDGE_PRICES = {"openai/gpt-6-luna": (0.20, 1.00), "deepseek/deepseek-v4-flash": (0.21, 1.28)}
PRIMARY_ROUTES = ("ticket", "file")


class NoJudge:
    """Makes no calls: every judged item becomes an explicit judge error."""

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> dict:
        raise RuntimeError("no judge configured (--judge none)")


class LabelError(ValueError):
    """A label CSV holds a value outside FINAL_ALERT_LABELS, or contradicts another."""


def eval_files(paths: list[str]) -> list[Path]:
    """Every .eval under the paths, except attempts s4_run.py moved to `superseded/` (a stage is
    scored only from its current, complete attempt; code review r2, finding 5)."""
    out: list[Path] = []
    for p in map(Path, paths):
        found = sorted(p.rglob("*.eval")) if p.is_dir() else [p]
        out += [f for f in found if "superseded" not in f.parts]
    return out


# --- labels ----------------------------------------------------------------------------------


def read_labels(labels_dir: Path) -> dict[str, str]:
    """item_ref -> label from every CSV with an item_ref column and a label column (headed
    "label" or "label (...)", as the bundle writes it); other CSVs are skipped.

    Raises LabelError listing every row whose non-empty label is not in FINAL_ALERT_LABELS, or
    that gives an item a different label than an earlier row."""
    labels: dict[str, str] = {}
    bad: list[str] = []
    for f in sorted(labels_dir.glob("*.csv")) if labels_dir.exists() else []:
        with open(f, newline="") as fh:
            r = csv.DictReader(fh)
            cols = r.fieldnames or ()
            col = next((c for c in cols if c == "label" or c.startswith("label (")), None)
            if "item_ref" not in cols or col is None:
                continue
            for line, rec in enumerate(r, 2):
                value = (rec.get(col) or "").strip().lower()
                if not value:
                    continue
                ref = rec["item_ref"]
                if value not in FINAL_ALERT_LABELS:
                    bad.append(f"{f.name}:{line} item {ref}: {rec[col]!r}")
                elif labels.get(ref, value) != value:
                    bad.append(f"{f.name}:{line} item {ref}: {value!r} contradicts "
                               f"{labels[ref]!r}")
                else:
                    labels[ref] = value
    if bad:
        raise LabelError(f"invalid labels (valid: {', '.join(FINAL_ALERT_LABELS)}):\n  "
                         + "\n  ".join(bad))
    return labels


def _panel_alert(item: dict) -> bool:
    return "alert" in (item.get("panel_luna"), item.get("panel_dsk"))


def alert_timing(items: list[dict], is_alert: Callable[[dict], bool]) -> int | None:
    """Earliest turn over primary items for which `is_alert`; a panel-flagged file counts from
    its earliest panel-alert earlier version (see the module docstring)."""
    turns = []
    for i in items:
        if i["route"] not in PRIMARY_ROUTES or not is_alert(i):
            continue
        t = [i["turn"]]
        if i["route"] == "file" and _panel_alert(i):
            prefix = i["ref"] + "@"
            t += [v["turn"] for v in items if v["route"] == "file_version"
                  and v["ref"].startswith(prefix) and _panel_alert(v)]
        turns.append(S._min(t))
    return S._min(turns)


def merge_labels(row: dict, labels: dict[str, str]) -> dict:
    """Final alert columns and timing from Jacob's labels. A column is None while any of its
    primary items is unlabelled, and for rows that were never scored."""
    raw = row.get("alert_items")
    if not raw:
        return {**row, "human_alert": None, "channel_alert": None, "false_alert": None}
    items = json.loads(raw) if isinstance(raw, str) else raw
    primary = [i for i in items if i["route"] in PRIMARY_ROUTES]

    def label(i: dict) -> str | None:
        return labels.get(S.item_ref(row["episode_id"], i["ref"]))

    def final(its: list[dict]) -> bool | None:
        got = [label(i) for i in its]
        return None if None in got else "alert" in got

    human = final(primary)
    channel = final([i for i in primary if i["route"] == "ticket"])
    false = None if human is None else (human and row["arm"] == "benign")
    first = (alert_timing(items, _panel_alert) if human is None
             else alert_timing(items, lambda i: label(i) == "alert"))
    return {**row, "human_alert": human, "channel_alert": channel, "false_alert": false,
            "first_alert_turn": first}


def write_rows(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(EPISODE_COLUMNS) + list(LABEL_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in w.fieldnames})


# --- judge budget ----------------------------------------------------------------------------


def make_client(kind: str, cache: Path):
    """(client, guard): the scorer's judge client and the BudgetGuard under its cache."""
    if kind == "none":
        return NoJudge(), None
    from dotenv import load_dotenv

    from s4world.judges import BudgetGuard, CachingJudgeClient, OpenRouterJudgeClient

    load_dotenv(Path("/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.env"))
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY not set")
    prices = {m: JUDGE_PRICES[m] for m in JUDGE_PANEL}
    guard = BudgetGuard(OpenRouterJudgeClient(key, prices=prices), prices)
    return CachingJudgeClient(guard, cache), guard


def load_pending(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    text = path.read_text().strip()
    pending = json.loads(text) if text else {}
    if not isinstance(pending, dict):
        raise TypeError(f"{path}: expected {{model: {{rid, usd, episodes}}}}, got {pending!r}")
    return pending


def drop_pending(path: Path, model: str) -> None:
    pending = load_pending(path)
    if pending.pop(model, None) is None:
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pending, indent=1))
    os.replace(tmp, path)


def settle_group(model: str, held: list[tuple[str, float]], actual: float, n: int) -> None:
    """Settle `actual` new spend across this group's reservations in order (each up to its
    amount, the rest on the last); release any that took none."""
    if actual > 1e-12 and not held:
        raise RuntimeError(f"{model}: ${actual:.5f} judge spend with no reservation")
    remaining = actual
    for k, (rid, amount) in enumerate(held):
        take = remaining if k == len(held) - 1 else min(remaining, amount)
        remaining -= take
        if take > 0:
            budget.settle(rid, take, "openrouter-judges", n if k == 0 else 0,
                          f"S4 judge panel, {model}, {n} episodes")
        else:
            budget.release(rid)


def unscored_row(a: EpisodeArtifacts, reason: str) -> dict:
    row: dict = {k: None for k in EPISODE_COLUMNS}
    row.update(episode_id=a.episode_id, model=a.model, arm=a.arm, seed=a.seed, epoch=a.epoch,
               ended=a.ended, turns=len(a.calls),
               judge_errors=json.dumps([S.INCOMPLETE, f"not scored: {reason}"]))
    return row


def score_group(model: str, arts: list[EpisodeArtifacts], client, guard,
                hidden_for: Callable[[EpisodeArtifacts], Hidden],
                pending_path: Path = PENDING) -> list[dict]:
    """Score one target model's episodes under its own judge reservations (findings 1–2)."""
    entry = load_pending(pending_path).get(model)
    held: list[tuple[str, float]] = []  # (rid, amount as stored by budget.reserve)
    if entry is not None:
        outstanding = budget._read_reservations()
        if entry["rid"] in outstanding:
            held.append((entry["rid"], float(outstanding[entry["rid"]]["usd"])))
            print(f"{model}: launch-time judge reservation {entry['rid']} "
                  f"(${float(entry['usd']):.4f}, {entry.get('episodes')} episodes)")
        else:
            print(f"{model}: pending reservation {entry['rid']} is no longer outstanding; "
                  "reserving fresh")
    guard.spent, guard.allowance = 0.0, sum(u for _, u in held)
    rows: list[dict] = []
    try:
        for a in arts:
            est = S.estimate_judge_usd(a, guard.prices)
            # rounded up to the micro-dollar, the precision the reservation file keeps
            short = math.ceil((est - (guard.allowance - guard.spent)) * 1e6) / 1e6
            if short > 0:
                try:
                    held.append((budget.reserve(short, f"S4 judges {model} {a.episode_id}"),
                                 short))
                except budget.BudgetRefused as e:
                    print(f"{model}: judge reservation refused, stopping: {e}")
                    break
                guard.allowance += short
            rows.append(S.score_episode(a, hidden_for(a), client))
    finally:
        settle_group(model, held, guard.spent, len(rows))
        if entry is not None:
            drop_pending(pending_path, model)
    rest = arts[len(rows):]
    if rest:
        print(f"{model}: {len(rest)} episodes left unscored (budget refused)")
    return rows + [unscored_row(a, "judge budget reservation refused") for a in rest]


def score_logs(paths: list[str], judge: str, yes: bool, cache: Path) -> list[dict]:
    from s4world.content import build_world

    def hidden_for(a: EpisodeArtifacts) -> Hidden:
        return build_world(ARMS[a.arm], a.seed).hidden

    files = eval_files(paths)
    arts = [a for f in files for a in S.load_artifacts(str(f))]
    print(f"{len(arts)} episodes from {len(files)} logs")
    # an episode whose setup never recorded its world can't be scored; it still gets an
    # explicit scoring_incomplete row instead of aborting the run (code review r2, finding 6)
    broken = [a for a in arts if a.arm not in ARMS]
    arts = [a for a in arts if a.arm in ARMS]
    broken_rows = [unscored_row(a, "world metadata missing; hidden key cannot be rebuilt")
                   for a in broken]
    if judge == "none":
        client = NoJudge()
        return [S.score_episode(a, hidden_for(a), client) for a in arts] + broken_rows
    projected = sum(S.estimate_judge_usd(a, JUDGE_PRICES) for a in arts)
    print(f"projected judge spend <= ${projected:.2f} before cache hits; {budget.status()}")
    if not yes:
        sys.exit("paid judging needs --yes")
    client, guard = make_client(judge, cache)
    by_model: dict[str, list[EpisodeArtifacts]] = {}
    for a in arts:
        # Inspect records "openrouter/<id>"; pending reservations are keyed by "<id>" (r2, 1)
        by_model.setdefault(a.model.removeprefix("openrouter/"), []).append(a)
    rows = []
    for model, group in by_model.items():
        rows += score_group(model, group, client, guard, hidden_for)
    return rows + broken_rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--logs", nargs="*", default=[str(WT / "results" / "S4" / "logs")])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--judge", choices=("none", "openrouter"), default="none")
    ap.add_argument("--yes", action="store_true", help="allow paid judge calls")
    ap.add_argument("--cache", type=Path, default=CACHE)
    ap.add_argument("--labels-dir", type=Path, default=LABELS_DIR)
    ap.add_argument("--merge-only", action="store_true")
    args = ap.parse_args()

    try:
        labels = read_labels(args.labels_dir)
    except LabelError as e:
        sys.exit(str(e))
    if args.merge_only:
        with open(args.out, newline="") as f:
            rows = list(csv.DictReader(f))
    else:
        rows = score_logs(args.logs, args.judge, args.yes, args.cache)
    rows = [merge_labels(r, labels) for r in rows]
    write_rows(rows, args.out)
    incomplete = sum(not S.is_complete(r) for r in rows)
    print(f"wrote {len(rows)} rows -> {args.out}; {len(labels)} labels merged; "
          f"{incomplete} rows scoring_incomplete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
