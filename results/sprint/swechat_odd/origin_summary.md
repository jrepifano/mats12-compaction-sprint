# Where the SWE-chat false completion lines come from

**Question (presentation session, 29 Oct 2026):** of the scan's false completion lines, how many just repeat a claim the agent had already made before the compaction (an agent problem the summary carried faithfully), and how many did the summary introduce?

**Answer:** about three in four (70–74%, see the check below) come from the summary, introduced or inflated. Of the 152 final-false lines (93 compactions):
- **82 (54%)**: nothing earlier in the history makes the claim. The summary introduced it.
- **30 (20%)**: the agent had made only a narrower or hedged claim, and the summary broadened it or stated it as settled (e.g. "All task handler tests are passing" became "All tests were passing at the point of summary request").
- **36 (24%)**: the agent had made the same claim itself.
- **4 (3%)**: only a user message or tool output said it (e.g. the edit tool's "file updated successfully").

These are Opus 5.5's labels, not hand-checked (`claude -p` on Max, effort low, $0; run 12:36–12:43 UTC on 29 Oct per the gitignored `data/raw/sprint/swechat_odd/scan/origin_run.log` and the $0 Max rows in `logs/costs.jsonl`). Lines in the same compaction are not independent, so read the counts as descriptive.

Of the 93 compactions, each counted once in this order:
- 46 (49%) have at least one line the summary introduced;
- 23 (25%) have at least one line it inflated from a narrower agent claim;
- 21 (23%) have only lines repeating the agent's own claim;
- 3 (3%) are other.

**The agent's own claims (36 lines), timed against the contradiction the recheck cited:**
- 19: the agent's claim comes before the cited contradicting message. The summary kept the claim, not the later evidence (stale, at least with respect to that evidence).
- 10: the claim comes after the cited contradicting message; the agent repeated it after that evidence.
- 3: the claim and the contradiction are in the same message, so their order is unknown.
- 4: unknown, because the recheck's cited message didn't verify.

"Before" means before the cited contradiction, not necessarily before the earliest evidence, so some of the 19 may also have followed earlier evidence. Only the 10 "after" lines (7% of 152), plus possibly the 3 same-message ones, are claims the agent repeated after the cited evidence.

**Method:**
- One call per compaction, with Grep and Read over the full pre-compaction history, labelling the assertion the recheck found contradicted. Precedence: agent_same, agent_narrower, user_or_tool, none.
- A source label counts only if its quote is found verbatim in the text of the cited message and that message has the stated role (checked in code); otherwise it is can't tell. No line ended up can't tell.
- Code: `scripts/swechat_origin.py` (`run`, `summary`, `screen`, `examples`).
- Numbers: `origin_summary.json`; seeded examples: `origin_examples.md`.
- Review: astra r1 (2 BLOCKING, fixed), r2 (no BLOCKING; 2 MINOR fixed), r3 (`logs/reviews/20261029-swechat-origin-r*.md`).

**Check on "none"** (the one label that needs no quote): a code screen (`screen`, `origin_screen.json`) looked, for each of the 82 "none" lines, for agent sentences with a completion word that contain at least floor(half), and at least 2, of the contradicted assertion's terms (substring matches). It keeps only the best-scoring sentence per line. It flagged 16 lines, and I read all 16:
- 11 have no matching completion claim (word matches like "fixed positioning", "let me fix", a plan's "remaining work"; some are claims about different work).
- 5 look like missed agent claims: an earlier "Completed task #20 – refactored task handlers" (2 lines), "all tier 1 tasks complete", "todo 029 marked done", and "TemporaryFile: fixed to call FilesystemService…".

**Sensitivity:** if all 5 are real misses, "introduced by the summary" is 77 of 152 (51%) instead of 82 (54%). If they are also the agent's same claim rather than a narrower one, the summary's share (introduced or inflated) is 107 of 152 (70%) instead of 112 (74%). The screen keeps one sentence per line and needs its keywords, so it can miss others. Treat these as a sensitivity range, not bounds.

**Open (accepted, astra r3):** stored answers are tied to the prompt, the parser version and the recheck evidence, but not to a hash of each history; the histories are the fixed SWE-chat dump. Regression tests don't cover negated repeats or the summary aggregation.
