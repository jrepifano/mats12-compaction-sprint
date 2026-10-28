# R7 build report

2026-10-28 19:51 UTC. Built at **$0**, on `sprint/r7-planted`. No real Claude Code process, Anthropic request, paid call, `.env` read, container launch, push, PR or merge. Cross-family review is left for after this build, as the brief directs.

## Built

- Twelve [edit specs](../../../../configs/week2/r7_planted/specs/) with **88 exact replacements**, plus [Jacob's side-by-side wording review](r7_specs_for_jacob.md). All finds match exactly once in the actual source sessions. Honest inputs remain byte-identical; edits affect only decoded text in the summary and UUID-selected preserved tail.
- [Replay module](../../../../src/compaction/s2/native/r7_replay.py) and [scheduler](../../../../scripts/week2_r7_replay.py): only the existing `_task(second=True)` path, unchanged Haiku egress and scorer artifacts. All specs, source sessions and existing honest artifacts are hash-checked before key access. Seeded history order and within-history run order; shared laptop slots; $30 stage / $6 task caps. Existing terminal outputs are skipped; incomplete outputs require inspection rather than silent retry.
- [Config](../../../../configs/week2/r7_planted.toml), [pinned source manifest](../../../../configs/week2/r7_planted/sources.json), [plan and Build contract](../plans/week2-r7-planted.md), accepted plan copy, and generated [design card](r7_design_card.md). The standard drift gate checks all spec hashes.
- [Readout](../../../../scripts/week2_r7_readout.py): experiment 1's teammate-loss scorer, two runs per arm per history, exact paired sign flips, explicit unknowns, missing-outcome bounds, provenance validation, and task-2-only quotation candidates for manual review.

## Verification

```sh
uv run pytest -q tests/compaction/test_r7_contract.py tests/compaction/test_r7_replay.py tests/compaction/test_r7_readout.py tests/compaction/test_native_cc_chain.py tests/compaction/test_week2_plan_check.py tests/compaction/test_s2_chain_week2.py tests/compaction/test_week2_precedent.py
```

**151 passed in 3.99 seconds.** Ruff passed on all added/changed Python files; `git diff --check` passed. Tests cover missing, duplicate and overlapping finds, unchanged honest bytes, preserved-tail selection, refusal before launch on drift, fake-endpoint request content, readout missingness/provenance, and exact sign-flip arithmetic.

`uv run python scripts/week2_r7_replay.py prepare` validated all 12 real sources and prepared 36 runs, with 24 distinct input hashes (one honest and one edited input per history), at zero paid calls. The plan gate passed with no mismatches; plan SHA256: `033dab9355c7700608efee46146f91ce67d39439218459e937f3f6d38db67403`.

`uv run python scripts/week2_r7_readout.py --out /tmp/r7-readout-build-check` correctly read 12 existing honest outcomes and marked all 36 future runs unknown. No complete history pairs exist yet, so the comparison and p-value are unavailable. This is a build check, not an R7 experimental result.

## Corrections and open issues

1. **All 12 tails need edits, not 9.** The source UUID metadata includes outcome-stating final messages in every history, including `tengo-np-1`. The plan and wording review document the evidence. Preserved rows can physically precede the compact boundary; selection follows UUIDs.
2. `tengo-np-2` has two agent-authored commits; its edited record retains both, saying “my commits.” Rebasing/merging histories use the original agent-only commit hashes so the claimed outcome is internally consistent.
3. The fake endpoint verifies our replay plumbing through the existing task-2 path. **Native CLI reconstruction was not rerun.** The scope's earlier native check used CLI 2.1.312, while the harness pins 2.1.310. Jacob's wording read and the separate review remain before a paid run.
4. Source paths point to the existing `week2-build` worktree; its raw artifacts must remain available. Questioning quotes are lexical candidates, not validated detections, and exclude private reasoning. One honest replicate is historical; calendar/provider differences remain a confound. Task 2 cannot inspect task 1's repository.
