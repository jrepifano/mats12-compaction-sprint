# Experiment 3c: deviations

## Summary redraws (28 Oct, ~03:15Z, before any version run)

**What was lost.** The summary records of httpx-np-1, httpx-np-2, tengo-np-2 and tengo-np-7 lost their generation-1 attempts:
- the first, uncorrected version of amendment 1 deferred string flags at generation 0;
- that `summarize` run returned at generation 0 and wrote back only the attempts up to it.

**Why the summaries changed.** The corrected run found no stored generation 1 for those four and drew new ones (4 paid summary calls). The lost texts can't be recovered: the Opus cache keeps prompt hashes only.

**Fix** (astra narrow r1–r2): summary records now keep every stored attempt (later generations marked not used); a stored generation, answered or empty after every try, is reused and never redrawn; reused attempts are re-judged.

**Decision** (Advisor, option B: never give an extra draw to a history the frozen rule excluded):

| History | Generation 1, before (lost) | Generation 1, now | Decision |
|---|---|---|---|
| httpx-np-1 | 5c7b7255… (string-flagged) | a5a6eaca… (string-clean, Opus clean) | excluded: the rule-retained summary is lost; amendment 1 unused |
| httpx-np-2 | ff68b386… (string-flagged) | eb943267… (string-clean, Opus clean) | excluded, as above |
| tengo-np-2 | 982c3aba… (Opus clean) | 8d412402… (Opus clean) | kept on the redraw (same rule, before any outcome); out of the sensitivity |
| tengo-np-7 | 492a8793… (Opus clean) | c9999d75… (Opus clean) | kept on the redraw; out of the sensitivity |

**Analysis** (Advisor, updated once versions were built):
- Main test: n = 7.
- Sensitivity lines:
  - (a) without the redrawn histories (tengo-np-2, -7): n = 5;
  - (b) without the cue-exempted histories: n = 4;
  - (c) without both: n = 2, descriptive.
- Cue exemptions: when versions were first built, amendment 2 turned out to apply to tengo-np-5 ("evaluation", "test harness") and tengo-np-6 ("eval", "scenarios") as well as tengo-np-4 ("scenario"). All are the agent's own words; versions had stopped at tengo-np-4 earlier, so the others were unseen. The original plan of one combined sensitivity at n = 4 became the three lines above.

Full hashes: deviations.json.
