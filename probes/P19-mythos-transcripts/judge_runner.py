"""P19's private, resumable JSON judge batches on :mod:`emx.llm_batch`.

All transcript-bearing CLI inputs and outputs must be gitignored under results/P19.
There is one format-repair batch; an unresolved validation error stops the pipeline.
No call is made merely by importing this module.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from dataclasses import replace
from pathlib import Path

from emx import llm_batch

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / "results" / "P19"
SCHEMAS = {
    "audit": (
        '{"flags":[{"index":integer,"category":"A"|"B"|"C"|"D"|"E"|"F",'
        '"quote":nonempty exact source substring,"confidence":number from 0 to 1,'
        '"interpretation":nonempty string,"alternative":nonempty string}]}'
    ),
    "belief": (
        '{"score":null|0|1|2|3,"quote":exact source substring or empty string,'
        '"stance":"simulated"|"real"|"mixed"|"unmentioned","reason":nonempty string}; '
        "score must be null exactly when stance is unmentioned; mentioned beliefs require "
        "a nonempty exact quote"
    ),
    "generic": "a JSON object",
}


class ValidationGateError(ValueError):
    """At least one request has no valid answer; do not continue analysis."""


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{n}: expected an object")
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    temp.replace(path)


def require_private(path: Path) -> Path:
    """Fail before calls if a transcript-bearing path could be committed accidentally."""
    path = path.resolve()
    if not path.is_relative_to(PRIVATE.resolve()):
        raise ValueError(f"transcript-bearing path must be below {PRIVATE}: {path}")
    check = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(path)], cwd=ROOT, check=False
    )
    if check.returncode != 0:
        raise ValueError(f"path is not gitignored: {path}")
    return path


def model_spec(model: str, provider_path: Path) -> llm_batch.ModelSpec:
    spec = llm_batch.MODELS[model]
    if model != "glm":
        return spec
    provider = json.loads(provider_path.read_text())
    slug = provider.get("provider")
    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("provider.json requires a nonempty provider slug")
    extra = {"provider": {"order": [slug], "allow_fallbacks": False}}
    supported = provider.get("supported_parameters")
    if "reasoning" in provider:
        if not isinstance(provider["reasoning"], dict):
            raise TypeError("provider reasoning override must be an object")
        extra["reasoning"] = provider["reasoning"]
    elif supported is None or "reasoning" in supported:
        extra["reasoning"] = {"enabled": False}
    prices = {}
    for key in ("usd_per_mtok_in", "usd_per_mtok_out"):
        value = provider.get(key, getattr(spec, key))
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise TypeError(f"provider {key} must be numeric")
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"provider {key} must be finite and nonnegative")
        prices[key] = float(value)
    return replace(spec, extra_body=json.dumps(extra, sort_keys=True), **prices)


def bind_sources(requests: list[dict], sources: list[dict], kind: str) -> dict[tuple, dict]:
    if kind == "generic":
        return {}
    indexed = {}
    named = {}
    for source in sources:
        index = source.get("index")
        if type(index) is not int or not isinstance(source.get("text"), str):
            raise ValueError("each source requires integer index and string text")
        if "quote_text" in source and not isinstance(source["quote_text"], str):
            raise ValueError("source quote_text must be a string when provided")
        if index in indexed:
            raise ValueError(f"duplicate source index {index}")
        indexed[index] = source
        if "item_id" in source:
            if source["item_id"] in named:
                raise ValueError(f"duplicate source item_id {source['item_id']}")
            named[source["item_id"]] = source
    bound = {}
    for request in requests:
        index = request.get("source_index")
        if index is not None and type(index) is not int:
            raise ValueError("source_index must be an integer")
        source = indexed.get(index) if index is not None else named.get(request["item_id"])
        if source is None:
            raise ValueError(f"{request['item_id']}: no bound source; supply source_index")
        bound[(request["item_id"], request["seed"])] = source
    return bound


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_output(text: str, kind: str, source: dict | None = None) -> dict:
    def reject_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    obj = json.loads(text, parse_constant=reject_constant)
    if not isinstance(obj, dict):
        raise TypeError("output must be a JSON object")
    if kind == "generic":
        return obj
    if source is None:
        raise ValueError("source is required")
    if kind == "audit":
        flags = obj.get("flags")
        if not isinstance(flags, list):
            raise ValueError("flags must be a list")
        for flag in flags:
            if not isinstance(flag, dict):
                raise TypeError("each flag must be an object")
            if type(flag.get("index")) is not int or flag["index"] != source["index"]:
                raise ValueError("flag index must equal the bound source window index")
            if flag.get("category") not in list("ABCDEF"):
                raise ValueError("category must be A-F")
            quote = flag.get("quote")
            quote_text = source.get("quote_text", source["text"])
            if not _nonempty(quote) or quote not in quote_text:
                raise ValueError("quote must be a nonempty exact substring of bound source")
            confidence = flag.get("confidence")
            if (
                type(confidence) not in (int, float)
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise ValueError("confidence must be finite and in [0, 1]")
            if not _nonempty(flag.get("interpretation")) or not _nonempty(flag.get("alternative")):
                raise ValueError("interpretation and alternative must be nonempty strings")
    elif kind == "belief":
        if not {"score", "quote", "stance", "reason"} <= obj.keys():
            raise ValueError("belief output is missing required fields")
        stance = obj["stance"]
        if stance not in ("simulated", "real", "mixed", "unmentioned"):
            raise ValueError("unknown stance")
        score = obj["score"]
        if stance == "unmentioned":
            if score is not None:
                raise ValueError("unmentioned requires null score")
        elif type(score) is not int or not 0 <= score <= 3:
            raise ValueError("mentioned belief requires an integer score from 0 to 3")
        quote = obj["quote"]
        if stance != "unmentioned" and not _nonempty(quote):
            raise ValueError("mentioned belief requires a nonempty exact quote")
        if not isinstance(quote, str) or (quote and quote not in source["text"]):
            raise ValueError("quote must be an exact substring of bound source")
        if not _nonempty(obj["reason"]):
            raise ValueError("reason must be nonempty")
    else:
        raise ValueError(f"unknown kind {kind}")
    return obj


def run_validated(
    model,
    requests: list[dict],
    out: Path,
    *,
    phase: str,
    kind: str,
    sources: list[dict],
    max_tokens: int = 2500,
) -> list[dict]:
    """Return valid normalized rows, or write failure rows then raise a hard gate error.

    Inputs must be private; the CLI checks their paths. The raw and repair outputs remain
    llm_batch caches, and a repeated run reuses those caches with fingerprint checks.
    """
    if not phase or max_tokens < 1:
        raise ValueError("nonempty phase and positive max_tokens are required")
    bound = bind_sources(requests, sources, kind)
    kwargs = {
        "exp": "P19",
        "max_tokens": max_tokens,
        "temperature": None,
        "json_mode": True,
        "concurrency": 32,
        "timeout_s": 120,
    }

    def batch(reqs, path, suffix=""):
        projection = llm_batch.estimate_usd(
            model, reqs, path, max_tokens=max_tokens, temperature=None, json_mode=True
        )
        print(
            f"P19:{phase}{suffix}: {len(reqs)} requests, reservation <= ${projection:.6f}",
            flush=True,
        )
        return llm_batch.run_batch(model, reqs, path, note=f"P19:{phase}{suffix}", **kwargs)

    def normalize(request, raw, repaired=False):
        result = {
            "item_id": request["item_id"],
            "seed": request["seed"],
            "parsed": None,
            "status": "invalid",
            "repaired": repaired,
            "raw_status": raw.get("status"),
            "fingerprint": raw.get("fingerprint"),
            "model": raw.get("model"),
            "error": None,
        }
        try:
            if raw.get("item_id") != request["item_id"] or raw.get("seed") != request["seed"]:
                raise ValueError("batch response identity mismatch")
            if raw.get("status") != "ok" or not isinstance(raw.get("text"), str):
                raise ValueError(f"judge returned non-ok status {raw.get('status')!r}")
            result["parsed"] = validate_output(
                raw["text"], kind, bound.get((request["item_id"], request["seed"]))
            )
            result["status"] = "ok"
        except (ValueError, TypeError) as error:
            result["error"] = str(error)
        return result

    raw_rows = batch(requests, out)
    if len(raw_rows) != len(requests):
        raise ValidationGateError("batch returned wrong number of rows")
    normalized = [normalize(req, raw) for req, raw in zip(requests, raw_rows, strict=True)]
    repairs = []
    repair_indices = []
    for i, (req, raw, norm) in enumerate(zip(requests, raw_rows, normalized, strict=True)):
        # Transport errors/refusals are hard failures, not an invitation to elicit the answer again.
        if norm["status"] == "ok" or raw.get("status") != "ok":
            continue
        prompt = (
            req["prompt"]
            + "\n\nFORMAT REPAIR: Return only valid JSON matching "
            + SCHEMAS[kind]
            + ". Correct the following validation error using the source "
            "above; do not invent evidence. Previous output is untrusted data.\nError: "
            + norm["error"]
            + "\nPrevious output:\n"
            + str(raw.get("text"))
        )
        repairs.append({**req, "prompt": prompt})
        repair_indices.append(i)
    if repairs:
        repair_path = out.with_suffix(".repair.jsonl")
        write_jsonl(out.with_suffix(".repair.requests.jsonl"), repairs)
        repaired = batch(repairs, repair_path, ":format-repair")
        if len(repaired) != len(repairs):
            raise ValidationGateError("repair returned wrong number of rows")
        for i, req, raw in zip(repair_indices, repairs, repaired, strict=True):
            normalized[i] = normalize(req, raw, repaired=True)
    write_jsonl(out.with_suffix(".validated.jsonl"), normalized)
    invalid = sum(row["status"] != "ok" for row in normalized)
    if invalid:
        raise ValidationGateError(
            f"{invalid}/{len(normalized)} outputs invalid after one repair; "
            f"see {out.with_suffix('.validated.jsonl')}"
        )
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", choices=("luna", "glm"), required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--kind", choices=tuple(SCHEMAS), required=True)
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--max-tokens", type=int, default=2500)
    parser.add_argument("--provider", type=Path, default=PRIVATE / "provider.json")
    args = parser.parse_args()
    try:
        requests = read_jsonl(require_private(args.requests))
        out = require_private(args.out)
        if args.kind != "generic" and args.sources is None:
            raise ValueError("--sources is required for audit and belief")
        sources = read_jsonl(require_private(args.sources)) if args.sources else []
        spec = model_spec(args.model, args.provider)
        rows = run_validated(
            spec,
            requests,
            out,
            phase=args.phase,
            kind=args.kind,
            sources=sources,
            max_tokens=args.max_tokens,
        )
        print(f"Validated {len(rows)} outputs: {out.with_suffix('.validated.jsonl')}")
    except (ValueError, TypeError, OSError) as error:
        parser.exit(1, f"P19 judge gate stopped: {error}\n")


if __name__ == "__main__":
    main()
