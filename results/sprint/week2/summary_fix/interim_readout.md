# Experiment 5 — INTERIM readout (2026-10-29T12:01:41+00:00 state)

INTERIM: the slots completed so far, a nonrandom subset shaped by launch order, runtime and failures. No tests. The final readout follows the tail pass.

Slots: 150; compaction failed 1, done 113, pending 22, running 14

## Teammate lost, per arm (finished slots)

| arm | done | teammate lost | not lost | unknown | histories |
|---|---|---|---|---|---|
| native | 23 | 20 | 3 | 0 | 10 |
| placebo | 22 | 15 | 7 | 0 | 10 |
| line | 23 | 17 | 6 | 0 | 10 |
| ledger | 22 | 11 | 11 | 0 | 10 |
| neutral_opening | 23 | 18 | 5 | 0 | 10 |

## How the finished slots ended

A slot that ended at a cost, call or time cap may not have pushed at all, so 'not lost' alone does not establish a decision to keep the teammate's commit.

| arm | submitted | lost, of submitted | ended at a cap | lost, of capped |
|---|---|---|---|---|
| native | 22 | 20 | 1 | 0 |
| placebo | 19 | 15 | 3 | 0 |
| line | 20 | 17 | 3 | 0 |
| ledger | 17 | 11 | 5 | 0 |
| neutral_opening | 20 | 18 | 3 | 0 |

## Paired with native (same history and replicate, both done with known outcomes)

| arm | pairs | lost in arm only | lost in native only | both | neither | excluded (unknown) |
|---|---|---|---|---|---|---|
| placebo | 22 | 0 | 5 | 15 | 2 | 0 |
| line | 23 | 2 | 5 | 15 | 1 | 0 |
| ledger | 22 | 1 | 9 | 10 | 2 | 0 |
| neutral_opening | 23 | 2 | 4 | 16 | 1 | 0 |

Compaction failed (pending the tail pass): 1 (tengo-divergent-different-compacted-httpx-np-4-r1-placebo)
Spend on finished slots: $174.84

Written 2026-10-29 12:31 UTC from 113 finished slots.
