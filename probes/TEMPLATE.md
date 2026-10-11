# P<NN>: <the question, as a question>

One page. Copy to `probes/P<NN>-<slug>/plan.md`, take the next free id from `probes/INDEX.md`, and commit both **on main, before any data exists** (that commit is the timestamp on the predictions). Jacob's "go" in chat is the sign-off; a batch of plans can be approved at once. After that Claude sees the probe through to an answer without check-ins, including follow-up runs (below).

| | |
|---|---|
| **Decision it informs** | What we do differently depending on the answer; the default if we never run it |
| **Closest prior work** | 1–3 sources, cited by `papers/<topic>/INDEX.md` row, and the gap this probe tests |
| **Setup** | Model (pinned revision), data (path + sha256), arms with seeds, what reuses existing code |
| **Measure** | The number that decides it, how it's computed, the judge (already validated, or flagged as not) |
| **Predictions** | 2–4 outcomes with probabilities, written before the run |
| **What each outcome changes** | continue (graduate to a study) / pivot / kill, one line each |
| **Likely follow-ups** | The next runs each outcome would call for, so they can start without waiting |
| **Automatic rules** | What the smoke unit must show before the bulk run; when to stop (measurement failure, cap) |
| **Cap** | Starting cap $<≤75> (GPU / API split), <N> wall-clock hours, with the projection and how it was measured. Follow-ups may raise it to $150 without asking |
| **Approved** | <date, Jacob, "go" in chat> |

## Notes
Anything a reader needs that doesn't fit the table, in a few lines.

## Run log
Lives in `probes/P<NN>-<slug>/log.md` (UTC timestamps, commands, seeds, spend). **Before each follow-up run**, add one entry: what it tests, why the last result calls for it, a one-line prediction, and its cost. Follow-ups are reported as exploratory, separately from the planned measure.
