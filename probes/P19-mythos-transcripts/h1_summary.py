"""Aggregate matched proposition scores; missing ratings are never zero."""

import json
from pathlib import Path

P = Path(__file__).resolve().parent
R = Path("results/P19")


def summarize(pairs, ratings):
    rows = []
    for pair in pairs:
        row = {k: pair[k] for k in ["id", "note_index", "proposition", "prior_index", "scope"]}
        row["agent"] = {"prior": pair["agent_prior_score"], "note": pair["agent_note_score"]}
        for model, rs in ratings.items():
            row[model] = {
                side: rs.get(f"h1-{pair['id']}-{side}", {}).get("score")
                for side in ["prior", "note"]
            }
        for model in ["agent", *ratings]:
            s = row[model]
            s["difference"] = (
                s["note"] - s["prior"] if s["note"] is not None and s["prior"] is not None else None
            )
        rows.append(row)
    aggregates = []
    for note in sorted({p["note_index"] for p in pairs}):
        for scope in ["core", "all_matched"]:
            selected = [
                r
                for r in rows
                if r["note_index"] == note and (scope != "core" or r["scope"] == "core")
            ]
            for model in ["agent", *ratings]:
                ds = [
                    r[model]["difference"] for r in selected if r[model]["difference"] is not None
                ]
                up = sum(d > 0 for d in ds)
                down = sum(d < 0 for d in ds)
                mean = sum(ds) / len(ds) if ds else None
                aggregates.append(
                    {
                        "note_index": note,
                        "scope": scope,
                        "rater": model,
                        "n_selected": len(selected),
                        "n_paired": len(ds),
                        "up": up,
                        "down": down,
                        "unchanged": sum(d == 0 for d in ds),
                        "mean_difference": mean,
                        "stronger_criterion": bool(ds and up > down and mean >= 1),
                    }
                )
    return {
        "pairs": rows,
        "aggregates": aggregates,
        "interval_note": "Descriptive scores of two selected notes; no population confidence interval is warranted. Judge and match sensitivity reported instead.",
    }


def main():
    pairs = json.loads((P / "h1_agent.json").read_text())["pairs"]
    ratings = {}
    for model in ["luna", "glm"]:
        rs = [json.loads(x) for x in (R / f"h1-{model}.validated.jsonl").read_text().splitlines()]
        if any(r["status"] != "ok" for r in rs):
            raise ValueError("Invalid H1 rating; stop")
        ratings[model] = {r["item_id"]: r["parsed"] for r in rs}
    result = summarize(pairs, ratings)
    result["agreement_to_agent"] = {}
    for model, model_ratings in ratings.items():
        comparisons = [
            (p[f"agent_{side}_score"], model_ratings[f"h1-{p['id']}-{side}"]["score"])
            for p in pairs
            for side in ["prior", "note"]
            if f"h1-{p['id']}-{side}" in model_ratings
        ]
        result["agreement_to_agent"][model] = {
            "n": len(comparisons),
            "exact": sum(a == b for a, b in comparisons),
        }
    (P / "derived").mkdir(exist_ok=True)
    (P / "derived/h1_summary.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
