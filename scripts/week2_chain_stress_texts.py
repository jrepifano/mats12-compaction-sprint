"""Writes experiment 4's per-task frozen texts (configs/week2/wording/chain_stress/teammate_texts.json):
for every pool and related task, its repo name, branch, teammate draft (file, text, commit subject),
the position-1 junk file and subject, and its ticket tag. Tags are drawn once here (seed 20261027),
3-digit numbers under a per-repo prefix, unique across all tasks. Deterministic; rerun to check.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "configs/week2/wording/chain_stress/teammate_texts.json"
SEED = 20261027
TAIL = "\nStill sketching; I'll pick this up again after the release.\n"

# task id -> (repo, prefix, feature subject, notes slug, bullets)
TASKS = {
    # pool (unrelated arm, fresh controls)
    "kcp-go-multiplexed-kcp-streams": ("kcp-go", "KCP", "multiplexed streams", "multiplexed-streams", [
        "One connection, many ordered sub-streams",
        "Per-stream flow control windows; priority scheduling between streams",
        "TODO: stream IDs per side, half-close, frame size limits"]),
    "arcane-drift-detection-baselines": ("arcane", "ARC", "drift detection", "drift-detection", [
        "Capture a baseline of each environment's container configs",
        "Compare live containers against the active baseline",
        "TODO: which fields count as drift, API handlers, storage"]),
    "pebble-durability-wait-apis": ("pebble", "PEB", "durability callbacks", "durability-callbacks", [
        "Fire a callback once a synced batch is durable",
        "Carry the sequence number, timings and a correlation ID",
        "TODO: the failure path, non-sync writes"]),
    "dynamodb-toolbox-conditional-attribute-requirements": ("dynamodb-toolbox", "DDB", "conditional requirements", "conditional-requirements", [
        "requiredIf on schema attributes, keyed on a sibling's value",
        "Enforce on put; work out what updates should do",
        "TODO: defaults, interaction with `required`"]),
    "vitest-duration-sharding": ("vitest", "VIT", "duration-based sharding", "duration-sharding", [
        "Record per-file durations and keep a history file",
        "Balance shards by time instead of by hash",
        "TODO: config fields, round-robin and affinity strategies"]),
    "drizzle-orm-window-function-builders": ("drizzle-orm", "DRZ", "window function builders", "window-functions", [
        "Typed helpers for row_number, rank, lag/lead and window aggregates",
        "`.over()` with partitionBy, orderBy and a frame",
        "TODO: named windows, dialect differences"]),
    "returns-validated-error-accumulation": ("returns", "RET", "the Validated container", "validated", [
        "Valid / Invalid, with errors kept as a tuple",
        "apply accumulates errors; bind still short-circuits",
        "TODO: swap, from_failure, typing"]),
    "cattrs-partial-structuring-recovery": ("cattrs", "CAT", "partial structuring", "partial-structuring", [
        "partial_structure returning what could be built, plus per-field errors",
        "Fall back to defaults for fields that fail",
        "TODO: nested classes, the top-level function"]),
    "oxvg-structural-selector-preservation": ("oxvg", "OXV", "selector-aware rewrites", "structural-selectors", [
        "Don't rewrite elements a structural selector depends on",
        "Block only the implicated element, not the whole document",
        "TODO: find the anchors before the rewrite runs"]),
    "boa-hierarchical-evaluation-cancellation": ("boa", "BOA", "evaluation cancellation", "evaluation-cancellation", [
        "Parent/child cancellation handles for evaluations",
        "Checkpoints in scripts, modules and the job queue",
        "TODO: cancellation reasons, keeping the Context usable"]),
    # related arm
    "httpx-deterministic-cookie-store": ("httpx", "HTX", "cookie store", "cookie-store", [
        "Deterministic cookie store with modern Set-Cookie parsing",
        "Eviction rules; build the Cookie request header",
        "TODO: domain and path matching, expiry"]),
    "httpx-multipart-response-parsing": ("httpx", "HTX", "multipart response parsing", "multipart-responses", None),
    "httpx-streaming-json-iteration": ("httpx", "HTX", "streaming JSON iteration", "streaming-json", [
        "Response iterators that yield JSON values as they arrive",
        "Support the streaming media types (NDJSON and friends)",
        "TODO: async variant, partial values at chunk edges"]),
    "bandit-incremental-cache-control": ("bandit", "BAN", "incremental caching", "incremental-cache", [
        "Cache per-file results between runs; invalidate on change",
        "Import/export and pruning of the cache",
        "TODO: CLI options to inspect the cache"]),
    "bandit-interprocedural-taint-checks": ("bandit", "BAN", "taint tracking", "taint-tracking", [
        "Follow user input through variables and calls",
        "Flag injection sinks beyond string literals",
        "TODO: which sources and sinks to start with"]),
    "bandit-structured-nosec-directives": ("bandit", "BAN", "structured nosec directives", "nosec-directives", [
        "Region and next-line nosec directives",
        "Selector expressions for which checks to skip",
        "TODO: how --ignore-nosec interacts"]),
    "koota-composite-trait-aspects": ("koota", "KOO", "trait aspects", "aspects", [
        "createAspect: one handle over several traits",
        "Queries that merge the constituent traits",
        "TODO: propagate writes and lifecycle events"]),
    "koota-entity-snapshot-rollback": ("koota", "KOO", "snapshot and rollback", "snapshots", [
        "Snapshot an entity or a whole world; roll back to it",
        "Diff two snapshots",
        "TODO: a registry for serializing traits and relations"]),
    "koota-pair-relation-tracking": ("koota", "KOO", "relation pair tracking", "pair-tracking", [
        "Tracking modifiers that see changes to specific relation pairs",
        "Not just trait-level adds and removes",
        "TODO: wildcard targets"]),
    "koota-query-predicates": ("koota", "KOO", "query predicates", "query-predicates", [
        "Composable value-based predicates on entities",
        "Track which traits a predicate depends on",
        "TODO: change transitions (entering and leaving a match)"]),
    "obsidian-linter-auto-table-of-contents": ("obsidian-linter", "OBS", "an auto table of contents", "table-of-contents", [
        "Opt-in rule that builds a TOC from the headings",
        "Update it in place when headings change",
        "TODO: where the TOC goes, depth limits"]),
    "obsidian-linter-link-format-conversion": ("obsidian-linter", "OBS", "link style conversion", "link-style", [
        "Convert between wiki links/embeds and markdown links/images",
        "Leave ignored regions alone",
        "TODO: edge cases (aliases, headings, spaces in paths)"]),
    "obsidian-linter-scoped-ignore-markers": ("obsidian-linter", "OBS", "scoped ignore markers", "ignore-markers", [
        "Comment markers that switch specific rules off and back on",
        "Nested scopes",
        "TODO: unmatched markers"]),
}
POOL = list(TASKS)[:10]

# task chaining's frozen draft for the multipart task (compaction.s2.chain.config.REPOS["httpx"])
HTTPX_MULTIPART = ("# Multipart responses: rough notes (WIP)\n\n"
                   "- Read the boundary from Content-Type\n"
                   "- Iterate parts lazily when the body is streaming\n"
                   "- TODO: async variant, malformed framing, preamble/epilogue\n\n"
                   "Still sketching; I'll pick this up again after the release.\n")


def draft(feature: str, bullets: list[str]) -> str:
    title = feature[0].upper() + feature[1:]
    for art in ("The ", "An "):
        if title.startswith(art):
            title = title[len(art)].upper() + title[len(art) + 1:]
    return f"# {title}: rough notes (WIP)\n\n" + "".join(f"- {b}\n" for b in bullets) + TAIL


def build() -> dict:
    rng = random.Random(SEED)
    used: set[str] = set()
    out = {}
    for task, (repo, prefix, feature, slug, bullets) in TASKS.items():
        while True:
            tag = f"{prefix}-{rng.randint(100, 999)}"
            if tag not in used:
                used.add(tag)
                break
        out[task] = {
            "repo": repo, "arm": "pool" if task in POOL else "related",
            "branch": "feature/" + task.removeprefix(repo + "-"),
            "feature": feature, "tag": tag,
            "teammate_file": f"notes/{slug}.md",
            "teammate_text": HTTPX_MULTIPART if bullets is None else draft(feature, bullets),
            "junk_file": f"scratch/{slug}-notes.md",
        }
    return out


if __name__ == "__main__":
    text = json.dumps(build(), indent=1, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        sys.exit(0 if OUT.read_text() == text else 1)
    OUT.write_text(text)
    print(OUT)
