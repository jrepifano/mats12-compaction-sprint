Round 2 review of a probe plan (planning stage, no data). Read-only.

Round 1 (logs/reviews/20261015-P50-plan-r1.md) returned NOT READY with 7 BLOCKING findings (B1-B7). The plan was then rewritten. Read:
- logs/reviews/20261015-P50-plan-r1.md (round 1 findings)
- probes/P50-rewrite-dose-response/plan.md (current version)
- probes/P50-rewrite-dose-response/log.md, last entry (the claimed dispositions of B1-B7 and the minors)
- Context only as needed: probes/P50-rewrite-dose-response/brief.md; .claude/worktrees/p48-subliminal-numbers-attribution/probes/P48-subliminal-numbers-attribution/{note.md,results/gate.json,p48/contracts.py,p48/data.py}; probes/P43-subliminal-attribution/note.md.

Terminology: "trait-carrying rows" = rows written by the trait teacher.

Tasks:
1. For each of B1-B7: is it actually fixed in plan.md? Answer FIXED / PARTIAL / NOT FIXED, with plan.md line numbers.
2. New problems introduced by the rewrite: check arithmetic (cost table, guard 5.5 h vs on-pod 279/307 min, reservation $24.94, power table under rho = 0/0.5/1, W1 count and cost, budget fit with the $40 cap), internal consistency (contrasts P1-P4 vs predictions vs outcomes; seeds 0-2 at 20K vs 1-3 at 10K; anchor fallback replacing block 4; the rewrite_x fallback chain), and anything that makes the plan not implementable as written.
3. Only raise new issues that would change a decision or waste money/time; do not relitigate round-1 minors that were applied.

Tag findings BLOCKING / MINOR / NIT with plan.md:line and a concrete fix. End with a verdict line: READY / READY WITH FIXES / NOT READY, and a table of any BLOCKING findings.
