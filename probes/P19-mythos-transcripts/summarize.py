"""Text-free descriptive summaries of P19 belief ratings; no causal estimates.

Run from the worktree: uv run python probes/P19-mythos-transcripts/summarize.py
Add --plot with matplotlib available. All 700 validated results are required.
Source text and complete sampled responses remain under ignored results/P19/.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter
from pathlib import Path
from statistics import mean

SEED = 191015
MODELS = ("luna", "glm")
HERE = Path(__file__).resolve().parent


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def validate_rating(rating):
    if not isinstance(rating, dict):
        raise TypeError("parsed belief rating must be an object")
    if not {"score", "stance", "quote", "reason"} <= set(rating):
        raise ValueError("incomplete parsed belief rating")
    score, stance = rating["score"], rating["stance"]
    if score is not None and (type(score) is not int or score not in range(4)):
        raise ValueError("score must be null or an integer 0..3")
    if stance not in {"simulated", "real", "mixed", "unmentioned"}:
        raise ValueError("invalid stance")
    if (score is None) != (stance == "unmentioned"):
        raise ValueError("null score must correspond to unmentioned stance")
    if not isinstance(rating["quote"], str) or not isinstance(rating["reason"], str):
        raise TypeError("quote and reason must be strings")


def load_ratings(path, expected_indices, allow_incomplete=False):
    ratings = {}
    for row in read_jsonl(path):
        if row.get("status") != "ok" and not allow_incomplete:
            raise ValueError(f"non-ok result in {path}: {row.get('item_id')}")
        match = re.fullmatch(r"belief-(\d+)", row.get("item_id", ""))
        if match is None:
            raise ValueError("unexpected belief item id")
        index = int(match.group(1))
        if index in ratings:
            raise ValueError(f"duplicate source index {index}")
        if row.get("status") == "ok":
            validate_rating(row.get("parsed"))
            ratings[index] = {**row["parsed"], "_status": "ok", "_raw": row}
        else:
            ratings[index] = {"_status": row.get("status", "missing_status"), "_raw": row}
    if set(ratings) != set(expected_indices):
        raise ValueError(
            f"result coverage mismatch in {path}: missing={sorted(set(expected_indices) - set(ratings))}, extra={sorted(set(ratings) - set(expected_indices))}"
        )
    return ratings


def is_valid(rating):
    return rating.get("_status", "ok") == "ok"


def coverage(ratings):
    invalid = sorted(i for i, row in ratings.items() if not is_valid(row))
    return {
        "expected_n": len(ratings),
        "valid_n": len(ratings) - len(invalid),
        "invalid_n": len(invalid),
        "invalid_indices": invalid,
        "status_counts": dict(Counter(r.get("_status", "ok") for r in ratings.values())),
    }


def _fraction(numerator, denominator):
    return {
        "count": numerator,
        "denominator": denominator,
        "fraction": numerator / denominator if denominator else None,
    }


def agreement(left, right):
    if set(left) != set(right):
        raise ValueError("agreement requires identical source-index sets")
    ids = sorted(i for i in left if is_valid(left[i]) and is_valid(right[i]))
    paired = [i for i in ids if left[i]["score"] is not None and right[i]["score"] is not None]
    return {
        "n": len(ids),
        "expected_n": len(left),
        "excluded_invalid_indices": sorted(set(left) - set(ids)),
        "score_exact_including_null": _fraction(
            sum(left[i]["score"] == right[i]["score"] for i in ids), len(ids)
        ),
        "presence_agreement": _fraction(
            sum((left[i]["score"] is None) == (right[i]["score"] is None) for i in ids), len(ids)
        ),
        "stance_exact": _fraction(
            sum(left[i]["stance"] == right[i]["stance"] for i in ids), len(ids)
        ),
        "score_exact_both_nonnull": _fraction(
            sum(left[i]["score"] == right[i]["score"] for i in paired), len(paired)
        ),
        "both_nonnull": {
            "n": len(paired),
            "indices": paired,
            "left_mean": mean(left[i]["score"] for i in paired) if paired else None,
            "right_mean": mean(right[i]["score"] for i in paired) if paired else None,
            "mean_right_minus_left": mean(right[i]["score"] - left[i]["score"] for i in paired)
            if paired
            else None,
            "mean_absolute_difference": mean(
                abs(right[i]["score"] - left[i]["score"]) for i in paired
            )
            if paired
            else None,
        },
        "score_disagreements": [i for i in ids if left[i]["score"] != right[i]["score"]],
        "stance_disagreements": [i for i in ids if left[i]["stance"] != right[i]["stance"]],
    }


def distribution(ratings):
    complete_coverage = coverage(ratings)
    ratings = {i: row for i, row in ratings.items() if is_valid(row)}
    counts = Counter(
        "null" if row["score"] is None else str(row["score"]) for row in ratings.values()
    )
    values = [r["score"] for r in ratings.values() if r["score"] is not None]
    return {
        "coverage": complete_coverage,
        "n": len(ratings),
        "nonnull_n": len(values),
        "null_n": len(ratings) - len(values),
        "score_counts": {key: counts[key] for key in ("null", "0", "1", "2", "3")},
        "stance_counts": dict(sorted(Counter(row["stance"] for row in ratings.values()).items())),
        "nonnull_mean": mean(values) if values else None,
    }


def window_summary(ids, ratings):
    present = [
        {"index": i, "score": ratings[i]["score"]}
        for i in ids
        if is_valid(ratings[i]) and ratings[i]["score"] is not None
    ]
    return {
        "selected_indices": ids,
        "selected_n": len(ids),
        "nonnull_n": len(present),
        "null_indices": [i for i in ids if is_valid(ratings[i]) and ratings[i]["score"] is None],
        "invalid_indices": [i for i in ids if not is_valid(ratings[i])],
        "valid_n": sum(is_valid(ratings[i]) for i in ids),
        "contributing_scores": present,
        "mean_nonnull": mean(row["score"] for row in present) if present else None,
    }


def boundaries(windows, models):
    thinking_ids = sorted(w["index"] for w in windows if w["thinking"].strip())
    result = {}
    for name, before, after, interpretation in (
        (
            "explicit_compaction_resume",
            1310,
            1314,
            "Before the Human compaction request versus after the Human resume; descriptive only.",
        ),
        (
            "second_note_update",
            2007,
            2007,
            "Before versus after a self-note update; no observed Human resume, so this is not a second observed compaction.",
        ),
    ):
        pre = [i for i in thinking_ids if i < before][-5:]
        post = [i for i in thinking_ids if i > after][:5]
        result[name] = {
            "before_source_index": before,
            "after_source_index": after,
            "interpretation": interpretation,
            "models": {
                model: {"pre": window_summary(pre, ratings), "post": window_summary(post, ratings)}
                for model, ratings in models.items()
            },
        }
    return result


def sample_responses(models, seed=SEED, n=8):
    ids = sorted(models["luna"])
    if set(ids) != set(models["glm"]):
        raise ValueError("sample requires model coverage alignment")
    sampled = random.Random(seed).sample(ids, n)
    metadata = {
        "seed": seed,
        "population_n": len(ids),
        "population": "All full-pass belief windows, sorted by source index before sampling; null and invalid ratings included.",
        "sample_n": n,
        "indices": sampled,
    }
    responses = {
        **metadata,
        "samples": [
            {
                "index": i,
                **{name: ratings[i].get("_raw", ratings[i]) for name, ratings in models.items()},
            }
            for i in sampled
        ],
    }
    return metadata, responses


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def plot_scores(models, path):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 4.5))
    for name, marker, color in (("luna", "o", "#2563eb"), ("glm", "x", "#e76f00")):
        ids = [
            i
            for i in sorted(models[name])
            if is_valid(models[name][i]) and models[name][i]["score"] is not None
        ]
        ax.scatter(
            ids,
            [models[name][i]["score"] for i in ids],
            label=name,
            marker=marker,
            color=color,
            s=21,
            alpha=0.6,
        )
    ax.axvline(1314, color="#222222", linestyle="-", label="Observed resume (1314)")
    ax.axvline(
        2007, color="#555555", linestyle="--", label="Note update (2007), no observed resume"
    )
    ax.set(
        title="Diagnostic partial ratings: 691k token proxy total; 373.8k before summary request",
        xlabel="Released transcript source index (omitted messages retain their original gaps)",
        ylabel="Endorsed simulation certainty",
        ylim=(-0.2, 3.2),
        yticks=[0, 1, 2, 3],
    )
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=2, fontsize=8)
    fig.text(
        0.02,
        0.015,
        "Null/unmentioned and invalid scores omitted, never scored zero. Dots are individual ratings; no interpolation or causal effect is implied.",
        fontsize=8,
    )
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(path, dpi=180)
    plt.close(fig)


def summarize(results, destination, plot=False, allow_incomplete=False):
    windows = read_jsonl(results / "prepared/windows.jsonl")
    ids = [w["index"] for w in windows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate prepared window ids")
    fixture = json.loads((HERE / "belief_fixture.json").read_text())
    agent = {row["index"]: row for row in fixture["labels"]}
    if len(agent) != len(fixture["labels"]):
        raise ValueError("duplicate fixture source ids")
    for rating in agent.values():
        validate_rating(rating)
    models = {
        name: load_ratings(results / f"belief-{name}.validated.jsonl", ids, allow_incomplete)
        for name in MODELS
    }
    validation = {
        name: load_ratings(results / f"belief-fixture-{name}.validated.jsonl", agent)
        for name in MODELS
    }
    output = {
        "seed": SEED,
        "window_count": len(ids),
        "diagnostic_partial_only": any(
            not is_valid(row) for ratings in models.values() for row in ratings.values()
        ),
        "coverage": {name: coverage(ratings) for name, ratings in models.items()},
        "interpretation": "Descriptive single-transcript ratings by unvalidated-for-generalization judges. Unmentioned is null, never zero. Invalid responses are excluded distinctly and not treated as unmentioned. Any incomplete full pass is diagnostic only; no headline result. No causal effects or population confidence intervals estimated.",
        "fixture": {
            "label_author": fixture.get("label_author", "independent agent"),
            "n": len(agent),
            "comparison_direction": "left=agent, right=judge",
            "agreement": {name: agreement(agent, ratings) for name, ratings in validation.items()},
        },
        "distributions": {name: distribution(ratings) for name, ratings in models.items()},
        "cross_model_comparison_direction": "left=luna, right=glm",
        "cross_model_agreement": agreement(models["luna"], models["glm"]),
        "boundaries": boundaries(windows, models),
    }
    sample_ids, samples = sample_responses(models)
    destination.mkdir(parents=True, exist_ok=True)
    write_json(destination / "summary.json", output)
    write_json(destination / "sample_ids.json", sample_ids)
    write_json(results / "report_samples.json", samples)
    with (destination / "belief_scores.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            lineterminator="\n",
            fieldnames=[
                "index",
                "luna_status",
                "luna_score",
                "luna_stance",
                "glm_status",
                "glm_score",
                "glm_stance",
            ],
        )
        writer.writeheader()
        for i in sorted(ids):
            row = {"index": i}
            for name in MODELS:
                model_row = models[name][i]
                row[f"{name}_status"] = model_row.get("_status", "ok")
                row[f"{name}_score"] = (
                    ("null" if model_row["score"] is None else model_row["score"])
                    if is_valid(model_row)
                    else "invalid"
                )
                row[f"{name}_stance"] = model_row["stance"] if is_valid(model_row) else ""
            writer.writerow(row)
    if plot:
        plot_scores(models, destination / "belief_trajectory.png")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("results/P19"))
    parser.add_argument("--output-dir", type=Path, default=HERE / "derived")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Diagnostic partial summaries only; invalid rows remain distinct.",
    )
    args = parser.parse_args()
    result = summarize(args.results, args.output_dir, args.plot, args.allow_incomplete)
    print(json.dumps({"window_count": result["window_count"], "output_dir": str(args.output_dir)}))


if __name__ == "__main__":
    main()
