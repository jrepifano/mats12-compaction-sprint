# SWE-chat completion-claim scan

**Opus 5.5's read, not hand-checked.** Route: `claude-opus-5-5` via `claude -p` on the Max plan ($0), effort low; first pass over a history digest, every first-pass false rechecked by the same model with Grep/Read over the full history. Seeded order `swechat-odd:scan:20261028`, prompt fingerprint `4ddf921cd8a7f76c`. 928 of 928 eligible compactions settled (target prefix 928; 0 unresolved; stop: none). Unit: keyword-matched summary lines the judge calls completion claims. The rate is the detected-false share (can't tell counts in the denominator). Every first-pass false went to a tool-using recheck over the full history; an unconfirmed false is can't tell. Intervals: Wilson 95%, and a session-cluster bootstrap.

## Final (after the recheck)

- **Compactions with at least one false completion line:** 93/928 = 10.0% [8.3, 12.1] (session bootstrap [8.1, 12.1]).
  - Of compactions with a completion line: 93/899 = 10.3% [8.5, 12.5].
- **Completion lines called false:** 152/6994 = 2.2% [1.9, 2.5] (session bootstrap [1.6, 2.7]).
  - Of lines decided true or false: 152/5647 = 2.7% [2.3, 3.1] (session bootstrap [2.0, 3.4]).
- **Verdicts:** {'cant_tell': 1347, 'true': 5495, 'false': 152}, over 6994 completion lines.
- **False kinds:** {'failed': 44, 'not_done': 71, 'not_run': 16, 'other': 17, 'user_says_not': 4}.

## Checks

- **First pass (before the recheck):** per compaction 103/928 = 11.1% [9.2, 13.3] (session bootstrap [9.0, 13.3]); per line 168/6994 = 2.4% [2.1, 2.8] (session bootstrap [1.8, 3.0]).
- **Recheck outcomes for first-pass false lines:** {'true': 15, 'false': 179, 'cant_tell': 3, 'false (quote not found)': 2} (0 pending).
- **Lines the pre-filter missed:** the judge found 213 untagged completion lines in 183 compactions (capped at 5 each; rejected: {'not_an_untagged_summary_line': 111, 'no_verdict': 3}); capped extra-line discovery, not a measured recall. First pass with them: per compaction 120/928 = 12.9% [10.9, 15.2] (session bootstrap [10.6, 15.3]).

| Split | Compactions | Per compaction | Per line |
|---|---|---|---|
| Opus 4.5 | 102 | 10/102 = 9.8% [5.4, 17.1] (session bootstrap [5.0, 15.0]) | 12/568 = 2.1% [1.2, 3.7] (session bootstrap [1.0, 3.4]) |
| Opus 4.6 | 640 | 54/640 = 8.4% [6.5, 10.8] (session bootstrap [6.3, 10.8]) | 89/4993 = 1.8% [1.5, 2.2] (session bootstrap [1.2, 2.4]) |
| Sonnet 4.5 | 59 | 13/59 = 22.0% [13.4, 34.1] (session bootstrap [10.1, 33.9]) | 28/584 = 4.8% [3.3, 6.8] (session bootstrap [1.6, 7.5]) |
| Sonnet 4.6 | 92 | 9/92 = 9.8% [5.2, 17.6] (session bootstrap [4.3, 16.0]) | 13/673 = 1.9% [1.1, 3.3] (session bootstrap [0.8, 3.4]) |
| other | 35 | 7/35 = 20.0% [10.0, 35.9] (session bootstrap [7.7, 30.0]) | 10/176 = 5.7% [3.1, 10.1] (session bootstrap [2.3, 9.8]) |
| harness claude-code | 921 | 93/921 = 10.1% [8.3, 12.2] (session bootstrap [8.1, 12.2]) | 152/6972 = 2.2% [1.9, 2.6] (session bootstrap [1.7, 2.8]) |
