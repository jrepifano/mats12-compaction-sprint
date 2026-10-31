# Hand labelling

Every check of the LLM pipeline that Jacob labelled by hand during the sprint. Labels were collected on private labelling pages (answer key kept out of the page, cards in seeded random order) and read back by code. "Checked" says whether the result matches a committed file (30 Oct). Those files hold aggregate counts; card-level labels are not included.

| Check | Cards | Result | Source file | Checked |
|---|---|---|---|---|
| Experiment 1 judge validation, first round | 100 | All four candidate judges failed the planned gate; Haiku's "dropped" precision 5/12 = 0.42 [0.19, 0.68], "invented" 0/3 | `docs/sprint/S1/report.md` (gate table) | in report; label file not committed |
| Experiment 1 fresh validation (items and claims) | 80 | "Lost" precision 13/18 = 0.72 [0.49, 0.88]; "invented" precision 8/13 | `results/sprint/S1/judge_precision.json` | yes |
| Experiment 1 release audit | 20 | 17 correct, 3 judge misreadings, 0 pipeline faults | `docs/sprint/S1/report.md` (audit row) | in report; label file not committed |
| Experiment 2 retention spot check | 24 | Agreed with the judge on 23/24 (kept 19/19, lost 4/5) | `results/sprint/S2/retention_spot_score.json` | yes |
| Experiment 2 usable-loss candidate checks | 12 | 3 passed all five checks | `results/sprint/S2/lossaudit/candidates_frozen.json` (`usable`, `yield_summary`) | yes |
| Experiment 2 live-loss calibration | 28 | Held-out "lost" precision 7/9 = 0.78; "kept" agreement 10/10 | `results/sprint/S2/lossaudit/calibration.json` | yes |
| Experiment 2 release audit, round 1 | 20 | Two scorer bugs found (see `CORRECTIONS.md`, entry 6); all 200 sealed rows re-scored | `CORRECTIONS.md` | label file not committed |
| Experiment 2 release audit, round 2 | 20 | 20/20 correct; outcomes released | `CORRECTIONS.md` (entry 6) | label file not committed |
| SWE-chat precision check | 24 | "Lost" precision on SWE-chat 12/19 = 0.63 [0.41, 0.81] | `docs/sprint/figs/README.md` (03_judge_precision) | in figure source; label file not committed |
| Haiku vs Sonnet judge test (reuses 85 of the cards above) | (85) | Sonnet 5.5 "lost" precision 40/57 = 0.70, Haiku 41/59 = 0.69 | `results/sprint/judge_compare/summary.md`, `summary.json`, `rows.jsonl` | yes (from summary) |
| SWE-Interact accumulated-loss cards | 6 | 1 of 6 accepted as a test case | `CORRECTIONS.md` (entry 8) | label file not committed |
| SWE-chat chain check | 60 | Judge "lost" precision past the 1st compaction 20/27 = 0.74 [0.55, 0.87] by Jacob; κ with a blind Opus read 0.22 | `results/sprint/S1/chain_decay/label_check.json`, `label_check.md` | yes |
| Planted-rule pilot kept cards | 4 | Agreed with the judge on 4/4 | none committed | no: raw data not committed |
| **Total** | **398** | The 85 reused cards are counted once | | |
