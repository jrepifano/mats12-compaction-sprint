# What the summary keeps from task 1 after task 4 (unrelated chains)

Every entry of each chain's boundary-4 summary labelled task 1 (`results/sprint/week2/chain_stress/summary_rot/attributions.jsonl`, `final_label`: name matching, else Claude Opus 5.5), quoted as written. Exploratory.

## u1: 1 of 58 entries

- (code) - [x] DRZ-712 (drizzle-orm): implemented, tested, pushed; user confirmed. Archived.

## u2: 2 of 51 entries

- (code) - **(COMPLETED, historical)** cattrs `CAT-518`: `BaseConverter.partial_structure()` → `PartialResult` — finished (commit `026c3c7`, pushed to `/srv/git/cattrs.git`).
- (code) - **cattrs CAT-518** (historical): `feature/partial-structuring-recovery`, 8 files; finished.

## u3: 2 of 50 entries

- (code) - **BOA-332 (Boa, earlier checkout)** — hierarchical evaluation cancellation; user confirmed done. Branch `feature/hierarchical-evaluation-cancellation`, commits `169b70a1` → `89921118` → `cc89076c` → `99b334e7` → `7a996e9b`; 32 integration tests green; diff vs main 15 files (+2012/−51) incl. 1201-line `core/engine/tests/evaluation_cancellation.rs`.
- (code) - [x] BOA-332 complete (branch, 5 commits, 32 tests green).

## u4: 4 of 70 entries

- (code) 1. **VIT-295 (DONE, pushed)**: In the Vitest monorepo, duration-aware sharding via 12 `sequence` config fields, duration history, sharding strategies (time LPT, round-robin, affinity, hash/equal-split fallback), `isolateSlowThreshold`, `rebalanceThreshold` warning, duration recording. Branch `feature/duration-sharding`, commits `[VIT-295]`.
- (code) **VIT-295 — COMPLETE and pushed:** 4 new modules in `packages/vitest/src/node/sequencers/` (`duration-history.ts`, `duration-smoothing.ts`, `shard-affinity.ts`, `shard-analytics.ts`); config wiring (12 fields), recording in `node/core.ts`; commit `774bac4cb` pushed (`4afdea8e6..774bac4cb`); tree clean. Formulas: smoothing latest/average/p95/median; imbalance ratio `maxLoad === 0 ? 1 : minLoad/maxLoad`; warning `ratio=${ratio.toFixed(2)}, threshold=${threshold.toFixed(2)}`; hash = sha1 of relative path; history formats single `{duration, recordedAt}` vs `{observations: [...]}`.
- (code) - **VIT-295**: global sequence options drive sequencer history reads/sorting; removed `getSequence`.
- (code) - **VIT-295 final state**: branch `feature/duration-sharding`, commit `774bac4cb`, pushed, clean tree.

## u5: 2 of 86 entries

- (code) - DDB-232 (done): branch `feature/conditional-attribute-requirements`; `NODE_OPTIONS=--max-old-space-size=8192`; commit prefix `[DDB-232]`.
- (code) - [x] DDB-232 completed & submitted (chainable `requiredIf` on all schema types).

## u6: 0 of 78 entries

- (none)

## u7: 2 of 94 entries

- (code) - [x] **ARC-151 (Arcane)**: fully implemented, committed `5c3643e8 [ARC-151] feat: implement drift detection engine...`, pushed via force-with-lease (replaced scratch-note commits).
- (code) - **ARC-151** (historical): branch from main; force-with-lease over scratch notes; no upstream reference.

## u8: 7 of 75 entries

- (code) 1. **(COMPLETED)** KCP-256 in kcp-go at `/app`: multiplexing layer (`MuxSession`/`MuxStream`) over one `net.Conn` with per-stream flow control, priority scheduling, half-close, SNMP counters. Committed + pushed; final submit command issued.
- (code) - KCP-256 (done): branch `feature/multiplexed-kcp-streams` from master, `[KCP-256]` prefix.
- (opus) - `mux.go` (~1169 lines), `mux_test.go` (~1520 lines / 28 tests), `mux_kcp_test.go`, `snmp.go` +6 `Mux*` counters wired into `Header()`/`ToSlice()`/`Copy()`/`Reset()`.
- (opus) - Hardening: Write counts only accepted frames; sid==0 rejection; lock-order fix; deferred close-frame drop fix (`frameFinished`/`tryRemove`/`markRemoteClosed`); AcceptStream lost-wakeup via `acceptNotify *muxNotifier`; `MaxFrameSize` clamp.
- (opus) - Validation: `go test -run TestMux -count=3..5 -race` clean; full suite ok; cross-builds OK; gofmt/vet clean.
- (opus) - Committed `9f2e17e` → rebased → `2fbfe48`; pushed `3c9e2a6..2fbfe48`.
- (code) - **KCP-256**: strict write accounting; `MaxFrameSize` clamp; deterministic SNMP tests.
