"""Context length before each sampled Experiment 1 (S1) compaction, per source.

Reads results/sprint/S1/manifest.json, checks every raw file's sha256 against it (and Jacob's
sidecar meta.jsonl against meta_sha256), then streams the raw files (gitignored, local only) and
keeps exactly the Part A ids. Each kept line's sha256 is checked against the manifest's
line_sha256, using the pipeline's own hashing (compaction.audit_logs.index_file: sha256 of the line
without its trailing newline). Analysed = the Part A records the judge roster marks ok
(results/sprint/S1/sealed/main/partA_roster_haiku-max.json).

Length field: the record's `pre_tokens`, which the S1 pipeline itself carries (s1_data.index_all,
the manifest's private records, every sealed judge row). It is the context size in tokens just
before the compaction (compaction.schema), as each dataset recorded it
(docs/sprint/prep/b-public-sets.md, a-sessions.md): Claude Code's compactMetadata.preTokens for
Jacob's sessions, SWE-chat's Claude Code sessions and PostTrainBench; the summarizer call's input
tokens for SWE-chat's 7 OpenCode compactions; for FrontierML, the input token count of the last
main-thread call before the summary (a proxy that leaves out that call's output and any later
tool results). It counts the whole context window
(system prompt and tool definitions included), so it is larger than the history text the judge
read. Nothing is converted from characters.

Percentiles: numpy.percentile, linear interpolation. Writes results/sprint/S1/context_lengths.json
(counts and statistics only; no record text).
Run: uv run python scripts/sprint_s1_context_summary.py
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "results/sprint/S1/manifest.json"
ROSTER = "results/sprint/S1/sealed/main/partA_roster_haiku-max.json"
OUT = "results/sprint/S1/context_lengths.json"
FIELD = "pre_tokens"
SOURCES = ("jacob", "swe-chat-v1", "posttrainbench", "frontierml")
CC = "Claude Code compactMetadata.preTokens"
FIELD_ORIGIN = {  # by (source, harness); docs/sprint/prep/b-public-sets.md, a-sessions.md
    ("jacob", "claude-code"): CC + " (compaction.sessions)",
    ("swe-chat-v1", "claude-code"): CC + " (compaction.public_sets.cc_events)",
    ("swe-chat-v1", "opencode"): "input tokens of OpenCode's summarizer call "
                                 "(compaction.public_sets.opencode_events)",
    ("posttrainbench", "claude-code"): CC + " (compaction.public_sets.cc_events)",
    ("posttrainbench", "kimi-claude"): CC + " (Claude Code harness; compaction.public_sets.cc_events)",
    ("frontierml", "claude-code"): "input tokens of the last main-thread call before the summary "
                                   "(compaction.public_sets.fml_events)",
}


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stats(values: list[int]) -> dict:
    a = np.asarray(values, dtype=float)
    p10, med, p90 = np.percentile(a, [10, 50, 90])
    return {"median": float(med), "p10": float(p10), "p90": float(p90),
            "min": int(a.min()), "max": int(a.max())}


def read_sampled(manifest: dict, root: Path = ROOT) -> dict[str, dict]:
    """{rid: {"source", "pre_tokens", "harness"}} for every Part A id; raises on any hash
    mismatch, a missing id or a repeated id."""
    want = set(manifest["ids"]["part_a"])
    line_sha = manifest["line_sha256"]
    out: dict[str, dict] = {}
    for f in manifest["files"]:
        path = root / f["path"]
        got = file_sha256(path)
        if got != f["sha256"]:
            raise SystemExit(f"{f['path']}: sha256 {got} != manifest {f['sha256']}")
        if "meta_sha256" in f:
            meta = path.parent / "meta.jsonl"
            if file_sha256(meta) != f["meta_sha256"]:
                raise SystemExit(f"{meta}: sha256 differs from the manifest's meta_sha256")
        with path.open("rb") as fh:
            for raw in fh:
                line = raw.rstrip(b"\r\n")
                if not line.strip():
                    continue
                rec = json.loads(line)
                rid = rec.get("id")
                if rid not in want:
                    continue
                if rid in out:
                    raise SystemExit(f"{rid} appears twice")
                if hashlib.sha256(line).hexdigest() != line_sha[rid]:
                    raise SystemExit(f"{rid}: line sha256 differs from the manifest")
                if rec.get("source") != f["source"]:
                    raise SystemExit(f"{rid}: source {rec.get('source')} in {f['path']}")
                out[rid] = {"source": rec["source"], FIELD: rec.get(FIELD),
                            "harness": rec.get("harness")}
    missing = want - set(out)
    if missing:
        raise SystemExit(f"{len(missing)} sampled ids not found, e.g. {sorted(missing)[:3]}")
    return out


def summarise(manifest: dict, roster: dict, recs: dict[str, dict]) -> dict:
    per = {}
    for src in SOURCES:
        sampled = [r for r in manifest["ids"]["part_a"] if r.split(":", 1)[0] == src]
        analysed = [r for r in sampled if roster[r]["ok"]]
        vals = [recs[r][FIELD] for r in analysed]
        bad = [r for r, v in zip(analysed, vals) if not isinstance(v, int) or isinstance(v, bool)]
        if bad:
            raise SystemExit(f"{src}: {len(bad)} analysed records without an integer {FIELD}")
        per[src] = {
            "n_sampled": len(sampled),
            "n_analysed": len(analysed),
            "excluded_ids": sorted(set(sampled) - set(analysed)),
            "harness": dict(sorted(Counter(recs[r]["harness"] for r in analysed).items())),
            "field_origin": {h: FIELD_ORIGIN[(src, h)]
                             for h in sorted({recs[r]["harness"] for r in analysed})},
            "tokens": stats(vals),
            "tokens_sampled": stats([recs[r][FIELD] for r in sampled]),
        }
    return per


def main() -> None:
    manifest = json.loads((ROOT / MANIFEST).read_text())
    roster = json.loads((ROOT / ROSTER).read_text())
    if set(roster) != set(manifest["ids"]["part_a"]):
        raise SystemExit("roster ids differ from the manifest's part_a ids")
    recs = read_sampled(manifest)
    out = {
        "stage": "S1",
        "what": "context length in tokens just before each sampled Part A compaction, per source",
        "unit": "tokens",
        "field": FIELD,
        "method": (f"the record's own `{FIELD}` field (dataset-recorded context size before the "
                   "compaction, as carried by the S1 pipeline); statistics over the analysed "
                   "records (judge roster ok); percentiles by numpy.percentile, linear "
                   "interpolation; tokens_sampled covers all sampled records"),
        "inputs": {
            "manifest": MANIFEST,
            "roster": ROSTER,
            "seed": manifest["seed"],
            "files": [{k: f[k] for k in ("source", "path", "sha256", "meta_sha256") if k in f}
                      for f in manifest["files"]],
            "sha256_checked": True,
            "line_sha256_checked": len(recs),
        },
        "n_sampled": len(manifest["ids"]["part_a"]),
        "n_analysed": sum(1 for r in manifest["ids"]["part_a"] if roster[r]["ok"]),
        "sources": summarise(manifest, roster, recs),
    }
    (ROOT / OUT).write_text(json.dumps(out, indent=2) + "\n")
    for src, s in out["sources"].items():
        t = s["tokens"]
        print(f"{src}: n={s['n_analysed']}/{s['n_sampled']} median {t['median']:.0f} "
              f"p10 {t['p10']:.0f} p90 {t['p90']:.0f} min {t['min']} max {t['max']}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
