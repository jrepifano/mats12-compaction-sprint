# Claude Code native arm: summary (24-25 Oct 2026)

**Setup:** Claude Code 2.1.310, Claude Haiku 5.5 (`claude-haiku-5-5`, effort high) on Jacob's Max plan through `claude setup-token` (`apiKeySource: none` asserted on every run). Native auto-compaction, its trigger set by `CLAUDE_CODE_AUTO_COMPACT_WINDOW`: Haiku's own default (its 1M window) would never compact here, so the window stands in for Jacob's common 150k trigger. **The first four runs used a 163,000 window, the rest 170,000** ("Window" below is that setting, not the firing point: the buffer below it varies, and boundaries land at 130,037-142,838 at 163k and 137,229-141,297 at 170k). Claude Code keeps its last messages verbatim and re-attaches files it read; both are in the graded post-compaction context. Graders: (a) the `interact_reqs` requirement ledger on the live summary, (b) S1's item loss via `lossaudit`, both on Claude Haiku 5.5 via Max. Simulator: GPT-5.5 via OpenRouter, SPRINT-S3-Q2 / s3q2-interact-pilot, $6.50 cap per run. Every run with a graded boundary is listed (earlier attempts too); infra-only attempts without a boundary are in logs/s2/native_arms_report.md. Data: summary.json (same folder).

| Task (attempt) | Window | End | Sim $ | Boundary | pre_tokens | Summary chars | Kept msgs | (a) lost / items (new / inh / unk) | (b) lost / items | Code check |
|---|---|---|---|---|---|---|---|---|---|---|
| boa (a2) | 170,000 | submitted | 2.23 | b1 | 137,630 | 16,512 | 13 | 0 / 0 (0 / 0 / 0) | 0 / 19 |  |
| boa (a2) | 170,000 | submitted | 2.23 | b2 | 139,945 | 21,510 | 3 | 0 / 7 (0 / 0 / 0) | 0 / 32 |  |
| boa (a2) | 170,000 | submitted | 2.23 | b3 | 137,537 | 19,363 | 3 | 0 / 26 (0 / 0 / 0) | 0 / 26 |  |
| boa (a2) | 170,000 | submitted | 2.23 | b4 | 137,446 | 23,517 | 3 | 1 / 31 (1 / 0 / 0) | 1 / 26 |  |
| fastapi (a1) | 163,000 | run_cap | 4.86 | b1 | 134,250 | 16,064 | 3 | 0 / 9 (0 / 0 / 0) | 4 / 36 |  |
| fastapi (a1) | 163,000 | run_cap | 4.86 | b2 | 132,113 | 23,493 | 3 | 0 / 21 (0 / 0 / 0) | 1 / 30 |  |
| helm (a1) | 163,000 | transcript_error | 0.39 | b1 | 142,838 | 17,055 | 3 | 0 / 6 (0 / 0 / 0) | 1 / 25 |  |
| helm (a2) | 170,000 | submitted | 0.09 | b1 | 138,211 | 3,888 | 3 | 0 / 2 (0 / 0 / 0) | 8 / 25 | lossaudit 8: i3/i4 followed, i11 implemented, i14 the code itself exported the helper (summary matches the code); i1-i5 lost with the partly refused summary |
| kombu (a1) | 163,000 | transcript_error | 1.15 | b1 | 134,562 | 16,124 | 7 | 0 / 7 (0 / 0 / 0) | 1 / 19 |  |
| kombu (a2) | 170,000 | run_cap | 4.86 | b1 | 139,513 | 15,130 | 3 | 0 / 8 (0 / 0 / 0) | 0 / 28 |  |
| kombu (a2) | 170,000 | run_cap | 4.86 | b2 | 137,922 | 19,344 | 5 | 0 / 34 (0 / 0 / 0) | 1 / 16 |  |
| kombu (a2) | 170,000 | run_cap | 4.86 | b3 | 137,364 | 25,871 | 3 | 0 / 50 (0 / 0 / 0) | 0 / 24 |  |
| testem (a3) | 170,000 | run_cap | 5.07 | b1 | 141,297 | 20,971 | 9 | 0 / 28 (0 / 0 / 0) | 4 / 40 |  |
| testem (a3) | 170,000 | run_cap | 5.07 | b2 | 137,229 | 21,662 | 3 | 0 / 50 (0 / 0 / 0) | 1 / 30 |  |
| vitest (a1) | 163,000 | transcript_error | 4.42 | b1 | 130,037 | 15,229 | 3 | 0 / 15 (0 / 0 / 0) | 5 / 29 |  |
| vitest (a1) | 163,000 | transcript_error | 4.42 | b2 | 130,979 | 22,360 | 3 | 7 / 41 (7 / 0 / 0) | 1 / 30 | ledger 7/7 implemented in the code at b2; summary-only losses |
| vitest (a1) | 163,000 | transcript_error | 4.42 | b3 | 130,597 | 23,925 | 3 | 7 / 60 (3 / 4 / 0) | 0 / 33 | ledger 7/7 implemented at b3; b2-q7 'misstated' looks like a grader error (conflates r15 with the legacy migration) |

**Notes:**
- helm a1, kombu a1 and vitest a1 ended `transcript_error`: a capture of the old launcher code, infra, not a task result. Their boundaries are valid and graded (main session).
- helm a2 b1's 3,888-character summary is genuine: Claude Code's own compaction call partly refused its prompt (1 of the arm's boundaries).
- fastapi a1's lossaudit pass 2 at b1 exceeds S1's frozen 32k output cap on every try: its claim facts are missing (accepted, not retried).
- "Kept msgs": the pre-compaction messages Claude Code carried over verbatim (`kept_indices`); its re-attached file reads come on top and aren't counted.
- "unavailable": that audit's output is missing or unreadable (none today).
- Provenance: `run_code` is the native code frozen at launch; `audit_code` the code that audited, recorded from d035bfb4 on (earlier audits: uncertain, see the report).
