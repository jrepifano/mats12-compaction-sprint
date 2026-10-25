# Antigravity native arm: summary (24-25 Oct 2026)

**Setup:** Antigravity CLI `agy` 1.4.7, Gemini 3.8 Flash (`gemini-3.8-flash-low`) on Jacob's free quota. agy's compaction threshold can't be set, so a PostInvocation hook ends the turn once a call's input + cache-read tokens reach **150,000** and the runner issues `/compact`, then resumes with "Continue.". After compaction agy's context is the CHECKPOINT summary alone: no verbatim messages, no re-attached files. Graders and simulator as for Claude Code. Every run with a graded boundary is listed; infra-only attempts are in logs/s2/native_arms_report.md. Data: summary.json (same folder).

| Task (attempt) | Window | End | Sim $ | Boundary | pre_tokens | Summary chars | Kept msgs | (a) lost / items (new / inh / unk) | (b) lost / items | Code check |
|---|---|---|---|---|---|---|---|---|---|---|
| boa (a1) | 150,000 | infra_error | 4.47 | b1 | 150,404 | 9,229 | 0 | 0 / 0 (0 / 0 / 0) | 2 / 16 |  |
| boa (a1) | 150,000 | infra_error | 4.47 | b2 | 150,187 | 10,395 | 0 | 1 / 15 (1 / 0 / 0) | 0 / 16 |  |
| boa (a2) | 150,000 | submitted | 0.37 | b1 | 150,036 | 9,898 | 0 | 0 / 0 (0 / 0 / 0) | 1 / 18 |  |
| boa (a2) | 150,000 | submitted | 0.37 | b2 | 151,224 | 7,520 | 0 | 0 / 3 (0 / 0 / 0) | 3 / 22 |  |
| boa (a2) | 150,000 | submitted | 0.37 | b3 | 150,237 | 12,142 | 0 | 1 / 10 (1 / 0 / 0) | 0 / 13 |  |
| fastapi (a1) | 150,000 | submitted | 2.63 | b1 | 150,033 | 12,352 | 0 | 0 / 0 (0 / 0 / 0) | 0 / 16 |  |
| fastapi (a1) | 150,000 | submitted | 2.63 | b2 | 150,301 | 10,066 | 0 | 0 / 3 (0 / 0 / 0) | 1 / 12 |  |
| fastapi (a1) | 150,000 | submitted | 2.63 | b3 | 150,343 | 7,623 | 0 | 3 / 13 (3 / 0 / 0) | 4 / 22 |  |
| fastapi (a1) | 150,000 | submitted | 2.63 | b4 | 150,000 | 13,200 | 0 | 4 / 18 (1 / 3 / 0) | 1 / 14 |  |
| helm (a2) | 150,000 | boundary_error | 0.36 | b1 | 151,517 | 8,738 | 0 | 0 / 2 (0 / 0 / 0) | 0 / 12 |  |
| helm (a2) | 150,000 | boundary_error | 0.36 | b2 | 150,125 | 12,314 | 0 | 0 / 3 (0 / 0 / 0) | 1 / 13 |  |
| helm (a2) | 150,000 | boundary_error | 0.36 | b3 | 150,391 | 9,056 | 0 | 0 / 3 (0 / 0 / 0) | 0 / 18 |  |
| testem (a1) | 150,000 | infra_error | 1.33 | b1 | 150,054 | 8,075 | 0 | 2 / 14 (2 / 0 / 0) | 3 / 20 |  |
| testem (a1) | 150,000 | infra_error | 1.33 | b2 | 150,782 | 8,840 | 0 | 4 / 30 (2 / 2 / 0) | 3 / 23 |  |
| testem (a2) | 150,000 | submitted | 3.14 | b1 | 151,095 | 10,393 | 0 | 4 / 28 (4 / 0 / 0) | 3 / 24 | sample: b1-q27 implemented at b1 |
| testem (a2) | 150,000 | submitted | 3.14 | b2 | 150,597 | 8,179 | 0 | 17 / 46 (14 / 3 / 0) | 3 / 24 | sample of 8 (3 inherited + 5 new, seed 20261025; 7 checked at b2): 6 implemented; b1-q17 partly, maintainer re-raised it in r21 |
| vitest (a1) | 150,000 | run_cap | 5.03 | b1 | 151,065 | 8,487 | 0 | 6 / 35 (6 / 0 / 0) | 5 / 29 | ledger 5/6 implemented at b1; b1-q32 not (recording on the success path only), maintainer re-raised it in r13 |
| vitest (a1) | 150,000 | run_cap | 5.03 | b2 | 150,577 | 9,228 | 0 | 22 / 50 (17 / 5 / 0) | 3 / 26 | ledger 22/22 implemented at b2 |

**Notes:**
- `pre_tokens` is the triggering call's request size (input + cache read).
- boa a1 and testem a1 ended `infra_error` after their boundaries (rig ssh drops at the secret screen); their graded boundaries stand. helm a1 ended the same way before any boundary (not listed).
- **kombu a1: `time_cap` with 0 compactions** (not listed). Its context peaked at 136,575 tokens; agy then sat idle about 1 h 45 m ("root agent idle; waiting up to 1h59m44s for 1 background task(s)") until the 7,184 s turn cap. The background-task rule only acts once the trigger has fired, so it never engaged below 150k.
- **helm a2: `boundary_error` after 3 boundaries.** The 4th capture failed in our converter (`agy.normalize_call`: an `ask_user` call whose `Arguments` was not a dict); b1-b3 stand. Its b4 kept image is an orphan.
- Provenance as for Claude Code.
