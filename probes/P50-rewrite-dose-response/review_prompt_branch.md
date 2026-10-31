Final review of probe P50's whole branch: code, results and the note. One round (probe tier). Diff: `git diff edfa34c0401604634b8d50c4a07923dc851650c1..HEAD`.

Read: probes/P50-rewrite-dose-response/{note.md,plan.md,log.md,probe.toml,run.sh}, p50/*.py, tests/*.py, results/analysis/{summary.md,p50.json}, results/{arms.json,reuse.json}, results/eval/*.summary.json. P48's imported code (unmodified): probes/P48-subliminal-numbers-attribution/p48/*.py. Earlier reviews: logs/reviews/20261016-P50-impl-r1.md and -code-r1.md.

Five layers, with the emphasis here on 2 and 3:
1. Correctness: the analysis code (p50/analyze.py) above all — the joint two-way bootstrap (shared seed-cluster and question draws, single-seed controls resampled over questions only), R and M, the pooled-within-seed contrasts, seed-set detection, and the verdict thresholds against plan.md's pre-set bars.
2. Methodology: does what ran match the published plan? The plan is probes/P50-rewrite-dose-response/plan.md, published to main before any data, plus its "Approved scope (rev 68)" section. Two design changes are logged (writer tries 8 -> 32; the relaxed cut). Are the verdicts the plan's own rules produce, and are the three regimes compared fairly?
3. Integrity: does every number in note.md trace to a committed artifact? Is any claim unsupported, overstated or understated? Check especially: the reuse of P48's summaries (results/reuse.json, the mixture sha match), the single-writer claim for rewrite_x, the "swap does nothing at 20K" claims and their intervals, and the predictions-vs-outcomes table.
4. Goal adherence: does the note serve the decision it was built for ("is this defence worth pitching?") without scope creep?
5. Throughput: note only, no action needed — the run is finished.

Flag anything in note.md that a reader building a pitch on it could be misled by. Output: verdict, then findings tagged BLOCKING/MINOR/NIT with file:line and a fix.
