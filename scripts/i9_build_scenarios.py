"""Build I9's scenarios: data/I9/scenarios.jsonl, data/I9/sets.json and the build_scenarios
manifest (implementation plan §2, unit U1; prereg v0.5 §4.1). Free: no model or API calls.

    uv run python scripts/i9_build_scenarios.py

Deterministic: rerunning gives byte-identical scenarios.jsonl and sets.json (only the
manifest's UTC times and git state change). The procedure (bank load, generator rendering, the
seed-0 draw of 64 + 16 from the 144-condition pool, the subsets) is documented in
`emx.i9.scenarios`.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from emx.i9 import contracts as C
from emx.i9 import provenance
from emx.i9 import scenarios as S


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return str(path.resolve().relative_to(C.REPO_ROOT))


def git_state() -> tuple[str, bool]:
    """(commit, dirty): the shared rule in emx.i9.provenance."""
    return provenance.git_state()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.parse_args()
    started = utc_now()
    rows, sets = S.build()
    S.write_outputs(rows, sets)

    inputs = [
        C.FileRef(rel(p), sha256_file(p), C.N_BANK if p == S.BANK_ITEMS_PATH else None)
        for p in S.input_files()
    ]
    outputs = [
        C.FileRef(rel(C.SCENARIOS_PATH), sha256_file(C.SCENARIOS_PATH), len(rows)),
        C.FileRef(rel(C.SETS_PATH), sha256_file(C.SETS_PATH), None),
    ]
    commit, dirty = git_state()
    drawn = set(sets["confirmatory"]) | set(sets["sealed"])
    drawn_affected = sorted(
        sid for sid in ("am-" + cid for cid in S.KNOWN_UPSTREAM_UNFILLED) if sid in drawn
    )
    manifest = C.Manifest(
        step="build_scenarios",
        inputs=inputs,
        outputs=outputs,
        git_commit=commit,
        git_dirty=dirty,
        seeds={
            "scenario_draw": C.SCENARIO_DRAW_SEED,
            "bank_subset": C.BANK_SUBSET_SEED,
            "thinking_subset": C.THINKING_SUBSET_SEED,
        },
        pins={"lynch_commit": C.LYNCH_COMMIT, "wsbench_commit": C.WSBENCH_COMMIT},
        started_utc=started,
        finished_utc=utc_now(),
        usd=0.0,
        notes=(
            "items.json rows = bank items; other non-JSONL files rows=None. Production system "
            "prompts; text = (USER_PROMPT + newline + email_content).strip(). Upstream generator "
            "leaves a literal ${direction_modifier} in the blackmail/murder x safety x "
            f"restriction emails; drawn: {drawn_affected or 'none'}."
        ),
    )
    path = manifest.write()
    counts = {k: len(v) for k, v in sets.items()}
    print(f"wrote {rel(C.SCENARIOS_PATH)} ({len(rows)} rows, sha256 {outputs[0].sha256[:12]})")
    print(f"wrote {rel(C.SETS_PATH)} {counts} (sha256 {outputs[1].sha256[:12]})")
    print(f"wrote {rel(path)} (git {commit[:8]}, dirty={dirty})")


if __name__ == "__main__":
    main()
