# Experiment 2 (live pilot): numbers cited in the presentation

Four numbers from the live pilot's close-out, each with the committed file it comes from. Recomputed from those files on 30 Oct where possible.

| Number | What it measures | Source file | Recomputed |
|---|---|---|---|
| 18 of 375 items lost | Items the judge graded validly (`grade_valid`) at the 24 audited compaction points, and those marked `lost` | `results/sprint/S2/lossaudit/losses.jsonl` | yes: 375 rows with `grade_valid` true, 18 with `lost` true |
| At most 2 usable cases | Dropped losses that passed every candidate check (confirmed and upper bound both 2), below the go line for a repair experiment | `results/sprint/S2/lossaudit/candidates_frozen.json` (`yield_summary.counts`) | yes |
| None of 105 candidates passed the repair-cohort screen | Natural losses in the SWE-Interact runs that passed the committed gates for a repair cohort | `results/sprint/S2/interact/cohort_funnel.json` | yes: 105 candidates across 7 boundaries, 0 passing |
| Kept at all 4 exposures | The planted commit-prefix rule after the 2nd and 3rd compactions in both chains where it was delivered | none committed | no: raw data not committed (hand-check: 4/4 agreed, `docs/sprint/hand-labelling.md`) |
