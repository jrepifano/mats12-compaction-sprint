# SWE-chat real-chain decay

54 sessions. lost = dropped or misstated after the second look and availability lost (Experiment 1); usable = Experiment 1's item rule; rows whose item a later stretch showed no longer current are censored from that compaction on (uncensored beside). Intervals: 95% percentile, session-group bootstrap, 2,000 resamples, seed 20261024.

Stretch-scan coverage: {'scans': 169, 'answered': 168, 'rejected_entries': 30, 'cohort2_candidates': 556, 'cohort2_new': 485, 'cohort2_unknown': 20, 'chains_cohort2_unscanned': 1}. Censored rows need every scan up to k answered; cohort-2 candidates without a newness answer are left out.

## Items from before compaction 1: lost share at compaction k

Censoring rests on the stretch scan, a new Haiku step not yet checked by hand (a seeded 20 of its no-longer-current calls are in not_current_sample.jsonl), so both versions are headline rows.

| k | uncensored (every item) | censored (no-longer-current items dropped) | censored, not reintroduced before k |
|---|---|---|---|
| 1 | 25.4% [20.6, 30.0] (n 690) | 25.4% [20.6, 30.0] (n 690) | 25.4% [20.6, 30.0] (n 690) |
| 2 | 62.3% [56.9, 68.1] (n 711) | 61.4% [55.1, 67.7] (n 578) | 68.1% [61.9, 74.5] (n 467) |
| 3 | 76.0% [70.5, 81.4] (n 713) | 74.2% [67.9, 80.3] (n 551) | 80.2% [74.4, 85.7] (n 425) |
| 4 | 86.4% [80.5, 91.7] (n 332) | 83.9% [76.4, 91.1] (n 224) | 87.9% [80.9, 94.4] (n 165) |
| 5 | 81.5% [73.0, 90.2] (n 173) | 81.0% [71.6, 93.3] (n 105) | 79.5% [70.1, 92.3] (n 78) |
| 6 | 86.4% [76.6, 96.6] (n 132) | 80.0% [71.6, 94.7] (n 75) | 78.9% [71.1, 92.3] (n 57) |
| 7 | 83.3% [73.6, 92.5] (n 108) | 74.1% [66.7, 90.3] (n 58) | 79.5% [72.1, 100.0] (n 44) |
| 8 | 81.5% [69.2, 93.6] (n 108) | 72.1% [62.3, 93.8] (n 61) | 73.3% [62.5, 100.0] (n 45) |

## Step transitions (cohort 1)

Not lost = Experiment 1's complement of lost (it includes dropped items available another way).

| step | not lost→lost | lost→not lost | lost→not lost, not restated | not lost→lost, uncensored | lost→not lost, uncensored |
|---|---|---|---|---|---|
| 1->2 | 51.1% [43.1, 59.0] (n 550) | 9.3% [4.2, 14.8] (n 550) | 6.5% [1.7, 12.0] (n 446) | 52.8% [46.2, 60.0] (n 676) | 9.2% [4.6, 14.0] (n 676) |
| 2->3 | 41.7% [33.3, 50.5] (n 546) | 5.3% [2.4, 9.0] (n 546) | 3.7% [1.3, 6.8] (n 423) | 45.0% [36.8, 53.5] (n 699) | 5.2% [2.7, 8.4] (n 699) |
| 3->4 | 41.2% [21.7, 63.3] (n 220) | 3.6% [1.0, 7.2] (n 220) | 2.2% [0.0, 5.1] (n 165) | 48.5% [29.4, 67.2] (n 325) | 3.5% [1.1, 6.5] (n 325) |
| 4->5 | 15.0% [0.0, 33.3] (n 103) | 1.2% [0.0, 3.3] (n 103) | 1.6% [0.0, 4.1] (n 76) | 17.9% [4.5, 32.1] (n 171) | 4.9% [0.0, 9.3] (n 171) |
| 5->6 | 11.8% [0.0, 24.0] (n 75) | 0.0% [0.0, 0.0] (n 75) | 0.0% [0.0, 0.0] (n 57) | 30.8% [9.5, 64.4] (n 131) | 0.0% [0.0, 0.0] (n 131) |
| 6->7 | 14.3% [0.0, 80.0] (n 58) | 6.8% [1.8, 15.4] (n 58) | 2.9% [0.0, 6.1] (n 44) | 17.6% [0.0, 50.0] (n 108) | 4.4% [1.5, 8.1] (n 108) |
| 7->8 | 13.3% [0.0, 75.0] (n 57) | 2.4% [0.0, 9.7] (n 57) | 0.0% [0.0, 0.0] (n 42) | 11.1% [0.0, 40.0] (n 104) | 1.2% [0.0, 4.7] (n 104) |
| pooled | 44.3% [36.8, 52.9] (n 1609) | 4.8% [3.0, 7.2] (n 1609) | 3.3% [1.8, 5.2] (n 1253) | 47.1% [40.5, 54.8] (n 2214) | 4.6% [3.1, 6.5] (n 2214) |

First observed loss (censored, observed compactions only): {1: 175, 2: 218, 3: 80, 4: 20, 5: 2, 6: 1, 7: 2, 8: 1} of 718 items; 219 with no observed loss; of 405 observed again after their first loss, 42 were not lost at a later compaction. Observations per item: {1: 138, 2: 45, 3: 317, 4: 115, 5: 28, 6: 16, 7: 5, 8: 54}.

## By item type (cohort 1)

| type | k=1 | k=2 | k=3 | k=4 | k=5 | k=6 | k=7 | k=8 |
|---|---|---|---|---|---|---|---|---|
| rule | 42.9% [33.3, 50.8] (n 203) | 59.0% [51.6, 66.1] (n 183) | 73.2% [65.1, 81.2] (n 183) | 78.9% [64.5, 91.5] (n 71) | 72.1% [52.9, 90.6] (n 43) | 65.6% [40.9, 87.1] (n 32) | 65.5% [44.4, 88.5] (n 29) | 64.5% [45.9, 94.7] (n 31) |
| permission | 45.5% [23.8, 68.4] (n 22) | 71.4% [52.0, 90.0] (n 21) | 65.0% [40.9, 90.0] (n 20) | 62.5% [22.2, 100.0] (n 8) | 50.0% [0.0, 100.0] (n 4) | 0.0% [0.0, 0.0] (n 2) | 0.0% [0.0, 0.0] (n 2) | 0.0% [0.0, 0.0] (n 2) |
| decision | 12.5% [6.5, 19.6] (n 208) | 57.6% [46.4, 67.6] (n 210) | 73.0% [63.0, 81.3] (n 200) | 87.5% [76.1, 95.7] (n 80) | 92.3% [78.9, 100.0] (n 26) | 100.0% [100.0, 100.0] (n 11) | 100.0% [100.0, 100.0] (n 6) | 85.7% [62.5, 100.0] (n 7) |
| commitment | 11.5% [0.0, 23.1] (n 26) | 42.9% [15.4, 71.4] (n 14) | 75.0% [45.5, 100.0] (n 12) | 80.0% [25.0, 100.0] (n 5) | 100.0% [100.0, 100.0] (n 2) | 100.0% [100.0, 100.0] (n 2) | 100.0% [100.0, 100.0] (n 2) | 100.0% [100.0, 100.0] (n 2) |
| task_state | 21.2% [15.4, 27.8] (n 231) | 70.0% [60.8, 79.7] (n 150) | 78.7% [69.8, 87.4] (n 136) | 88.3% [76.6, 98.1] (n 60) | 86.7% [70.6, 100.0] (n 30) | 92.9% [87.9, 100.0] (n 28) | 84.2% [71.4, 93.3] (n 19) | 84.2% [71.4, 93.3] (n 19) |

## Items first stated between compactions 1 and 2

| k | lost |
|---|---|
| 2 | 17.7% [13.8, 21.6] (n 429) |
| 3 | 57.8% [50.9, 64.9] (n 334) |
| 4 | 77.3% [65.8, 88.5] (n 154) |
| 5 | 73.2% [58.0, 90.6] (n 82) |
| 6 | 68.3% [48.1, 89.6] (n 60) |
| 7 | 65.2% [45.1, 87.9] (n 46) |
| 8 | 65.2% [45.1, 87.9] (n 46) |

Reintroduced (stated again by user or agent) by each item's last observed compaction: 0.30703012912482064; shown no longer current: 0.21664275466284075 (of 697 cohort-1 items with every scan answered; 28 unknown).

## Ten seeded kept→lost examples

- **decision**, kept at k=1, dropped at k=2: The torchtune-aligned hyperparameters (lr 1e-4, weight decay 0.01, 5 warmup steps, 3 epochs) apply to all students, not only qwen25-coder-32b-instruct. — quote: “You can now continue with the user's answers in mind.”
- **rule**, kept at k=2, misstated at k=3: Run the minimax-m2.5 and gemini-3-flash experiments concurrently on us-central1, monitor both for progress, and report to the user if either fails. — quote: “Monitor for progress of both. Let me know if they fail”
- **rule**, kept at k=1, dropped at k=2: Do not get in the way of the user's flow: signal and log errors rather than blocking or prompting. — quote: “the overriding directive is to not get in the way of the user (in terms of stopping their flow)”
- **task_state**, kept at k=1, dropped at k=2: The migration was written as 019_add_email_provider.sql, which differs from the plan's name 019_add_brevo_email_provider.sql. — quote: “019_add_email_provider.sql”
- **decision**, kept at k=2, dropped at k=3: Train for 3 epochs, matching the torchtune reference config. — quote: “epochs: 3”
- **task_state**, kept at k=1, dropped at k=2: The chart library is back on circular-natal-horoscope-js with a 12 degree orb limit; Swiss Ephemeris is not in use. — quote: “I've reverted back to `circular-natal-horoscope-js` (the original library).”
- **commitment**, kept at k=2, dropped at k=3: Stop amending commits; create new commits going forward. — quote: “Got it - I won't amend commits going forward.”
- **permission**, kept at k=1, dropped at k=2: The user grants permission to put the Better Stack DSN into the code and push the env vars to Vercel production with the Vercel CLI. — quote: “use vercel cli to push em to prod”
- **decision**, kept at k=1, dropped at k=2: Defer file locking until session state migrates to SQLite (on the roadmap), since flock is POSIX-only and Windows support is coming. — quote: “uhh yes....roadmap....😅”
- **rule**, kept at k=1, dropped at k=2: Use the zwave_js_server constants for event attributes and property names (e.g. LOCK_USERCODE_PROPERTY) instead of string literals. — quote: “all of the attributes (userCode, property, propertyKey, etc. ) have corresponding constants zwave_js_server. We should use those”
