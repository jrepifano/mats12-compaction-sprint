# Report template

One file per experiment, at `docs/experiments/<ID>/report.md`, written when the experiment stops (done, stopped by a rule, or paused). It is mirrored to a Claude Doc for review and sharing (see CLAUDE.md). The step-by-step run log (`logs/<ID>-report.md`) is separate: it records what happened and when, while this report says what was found. The close-out steps are in the `finish-experiment` skill.

**Write for a colleague who hasn't seen the repo**, with the same style rules as `PREREG_TEMPLATE.md`:
- plain prose with tables;
- no internal codes;
- a term defined once, the first time it appears;
- every inline citation linking to a level-4 reference heading;
- file paths only where they name a source of a number.

## Title
The main finding as a statement, not the question. For example: "E3: the pairs were built and frozen; Stage A ran and was inconclusive".

## Review summary (one screen)
Required, directly under the title, so Jacob can read the result and decide from the top (Jacob, 2026-10-08). It should fit one screen as rendered, with each row linking to the body section that supports it. Label it **"provisional"** while any audit or review round the report depends on is still open. After the last edit, check that its headline numbers, gates, scenario, spend and review status still match the body. A table with these rows:

| Row | What it says |
|---|---|
| **Status** | Done / stopped by which rule / paused by Jacob, with the date |
| **Finding** | One or two sentences with the headline numbers and intervals, covering each primary question or stage, including null, gated and unrun ones; the preregistered scenario reached, or "no outcome scenario reached" (e.g. paused before any gate) |
| **Against the prereg** | Each gate and check by name: passed, failed, not run, pending or not applicable, with the reason; and any deviation that changes the reading |
| **What it changes** | The decision this feeds (continue, pivot or kill), stated as a recommendation |
| **Spend** | Dollars against the cap, by stage, from the ledger |
| **Reviews** | gpt-6-astra rounds on code, analysis and report, with verdicts; findings still open at a cap; any review skipped |
| **Open for Jacob** | Decisions and audits still his, or "none" |

## The finding
The headline result first, with its numbers and where each comes from (the file it was computed from). Then the preregistered scenario it falls under, quoted from the prereg's §6, or "no outcome scenario reached" with the reason. A gate that never ran is reported as not run; what it would probably have shown is a forecast, labelled as one.

## What we did
A short paragraph linking the prereg (its version and amendments), then only what a reader needs in order to interpret the results. **Deviations from the prereg go in a table:** what changed, why, when, who decided, and whether it was reviewed before use.

## Results
One subsection per preregistered question, in the prereg's order.
- **Every number in the report traces to a committed artifact or a named file;** recompute each table from its source file and state the path.
- Report intervals, the n behind every rate, and the preregistered test.
- Mark exploratory and post-data analyses as such, next to the result rather than in a footnote.

## Measurement checks
A table of every check the prereg required before a result can be read (manipulation checks, judge validation, completion, controls), each as passed, failed, not run, pending or not applicable, with its numbers or the reason. A failed check is stated where it changes a reading, not only here.

## Raw samples
Random samples, never hand-picked, drawn by a small committed script (`scripts/<exp>_report_samples.py`) that fixes the population, order, strata and seed. Show **every drawn row** with its id; never a subset of the draw. Long transcripts can live in a file the report links to.

## Caveats
Keep three kinds of statement apart:
- **observations:** what the data show;
- **forecasts:** for example, a stop that wasn't triggered, or what a larger run would show;
- **hypotheses:** for example, a mechanism suggested by one control.

Also cover limits of scope (models, wordings, doses), source-reading gaps, and anyone else's result we lean on, stated as "X report …".

## Cost
Spend by stage and resource, with estimated (unbilled) amounts marked separately. Give the ledger source (`logs/costs.jsonl`, via `scripts/merge_ledger.py report`) and what isn't in the ledger, such as reviews and subagents.

## Review history
Each gpt-6-astra round on the code, the analysis and this report (3 rounds per artifact, CLAUDE.md): date, verdict, what changed. Also list fixes applied after a final round without re-review, and any review skipped, with who authorized it (CLAUDE.md requires recording skips). After a round cap, a table of findings still open (finding, severity, a recommended fix or accept-as-limitation), for code as well as for the report.

## What waits for Jacob
A numbered list of decisions and audits, each with what it would change. Any labelling task Jacob does links to its HTML labelling page, built as CLAUDE.md requires: a private Artifact with the `db` capability, the answer key kept out of the page, and blind, randomized presentation where the protocol needs it.

## References
One level-4 heading per work (`#### Author et al. year`), then its title and venue on the next line, with the `papers/` row. Include the prereg and the run log. Cite only sources read in `papers/`; capture a new external source there first (the `finish-experiment` skill, step 1).
