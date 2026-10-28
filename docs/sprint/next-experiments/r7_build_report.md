# R7 build report

2026-10-28 19:51 UTC. Built at **$0**, on `sprint/r7-planted`. No real Claude Code process, Anthropic request, paid call, `.env` read, container launch, push, PR or merge. Cross-family review is left for after this build, as the brief directs.

## Built

- Twelve [edit specs](../../../configs/week2/r7_planted/specs/) with **88 exact replacements**, plus [Jacob's side-by-side wording review](r7_specs_for_jacob.md). All finds match exactly once in the actual source sessions. Honest inputs remain byte-identical; edits affect only decoded text in the summary and UUID-selected preserved tail.
- [Replay module](../../../src/compaction/s2/native/r7_replay.py) and [scheduler](../../../scripts/week2_r7_replay.py): only the existing `_task(second=True)` path, unchanged Haiku egress and scorer artifacts. All specs, source sessions and existing honest artifacts are hash-checked before key access. Seeded history order and within-history run order; shared laptop slots; $30 stage / $6 task caps. Existing terminal outputs are skipped; incomplete outputs require inspection rather than silent retry.
- [Config](../../../configs/week2/r7_planted.toml), [pinned source manifest](../../../configs/week2/r7_planted/sources.json), [plan and Build contract](../plans/week2-r7-planted.md), accepted plan copy, and generated [design card](r7_design_card.md). The standard drift gate checks all spec hashes.
- [Readout](../../../scripts/week2_r7_readout.py): experiment 1's teammate-loss scorer, two runs per arm per history, exact paired sign flips, explicit unknowns, missing-outcome bounds, provenance validation, and task-2-only quotation candidates for manual review.

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

## r1 fixes

2026-10-28 20:02 UTC. Addressed [Argon's r1 review](../../../logs/reviews/20261028-r7-argon-r1.md) at **$0**, with no real CLI/API requests, key reads, container launches or real ledger writes.

1. **Cutoff verified; regression added.** `_task`'s fourth argument is `branches`, not prior messages. Its finalizer calls `transcript.task2_index` on the converted session, matching the task-2 prompt. `claude_code.to_messages` preserves the user-message position. The fake replay now writes task-2 session rows and checks the actual `b_message_index`: imported summary/tail text and thinking are excluded, while task-2 candidates are included. A missing opening prompt leaves the cutoff and scan unknown.
2. **Prefix/model/slots verified; no fix needed.** `_session` passes `arm.session` to `NativeSession`; `common.purpose("r7_chain")` yields `native-r7-chain`. The task container and egress sidecar both use it, so two slots are correct. Model and effort remain `claude-haiku-5-5[1m]` / `high`. Tested without starting containers.
3. **Budget behavior verified; no cap change.** `PilotSpend` delegates to `budget.reserve`; settled sprint spend sums only the configured stages. A temporary ledger containing $1,000 of another stage's charges still permits R7's $30 reservation, and R7's own stage cap still binds. Other stages' outstanding reservations conservatively count at the shared sprint gate; that behavior is tested and documented.
4. **Incomplete output fixed.** Missing replay metadata, input session or result now raises the documented inspection-required `ValueError` before key access.
5. **Reconciliation fixed.** Resumed invocations retain completed runs as well as new results. An all-complete invocation also regenerates reconciliation and preserves failure exit status.
6. **Paths fixed.** Config, manifest and default run paths resolve from the repository outside its working directory; covered by a changed-cwd test.
7. **Thinking scan added.** Visible and thinking candidates carry separate tags and file/index locations. Native thinking is recovered from `session.jsonl` through the converter's task-2 cutoff; references now include compaction, history, earlier pushes/branches/work, Sam Patel and the source repo/branch. Empty, missing and redacted thinking remain explicit coverage limits. The saved task-2 sources have **469 empty thinking blocks**, so their reasoning coverage is unknown; this supersedes the initial visible-only limitation above.
8. **Honest replicates separated.** Lost/known counts and rates for historical honest-1 and new honest-2 appear beside the unchanged pooled paired test. The $0 source check reports honest-1 as 0/12 and honest-2 as unavailable, not zero.
9. **Completion caveat added.** Plan/readout describe seven resolved/submitted versus five awaiting push/submission resolution. Source verification corrects “five unpushed”: four were unpushed; `httpx-np-6` had pushed an alternate branch but not issued the completion command. The intervention therefore also changes unresolved push state.
10. **Chronology fixed and hashes refreshed.** `httpx-np-3` edits 3 and 6 now place the force-push before the preserved user check-in. Updated the side-by-side wording, spec hash, config, Build contract, accepted plan and design card. Historical accepted copies remain unchanged. New spec SHA256: `d6eea0b2d79c2c2e325513c7f7092c3a65052ece5af4163ff135ce8b27681040`; current plan SHA256: `3cb61553ad61d1aead6fc7e865c8d6c46fd324875b34b252c77ba603a290bd50`.

**Validation:** 187 tests passed in 4.55 seconds across all four R7 test files, native chain/transcript tests and the three week-2 regression files. After test-only lint cleanup, all 59 readout tests passed again; Ruff and diff checks pass. `week2_r7_replay.py prepare` validates 36 inputs and all 88 replacements; `week2_plan_check.py` passes with no drift. The local readout leaves all 36 future runs unknown and the primary comparison/p-value unavailable. No native CLI acceptance check or paid experiment was run.
