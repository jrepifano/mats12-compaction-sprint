# Haiku vs Sonnet grading: decision line (written before any Sonnet call)

Written 2026-10-24 (UTC timestamp below), from the brief `logs/s2/briefs/sonnet-judge-test.md`,
before `scripts/judge_compare.py` sent any Sonnet request.

**Line (the brief, verbatim):** if Sonnet's unweighted precision on judge-lost cards is at least
0.8 AND its kept→lost error is lower than Haiku's with the paired difference's interval
excluding 0, recommend re-grading SWE-chat's 300 records with Sonnet. Otherwise recommend keeping
Haiku. Same-family caveat: Sonnet is not a cross-family judge.

**How it is read (decided now, before data):**
- "Precision on judge-lost cards" = the share of Sonnet's own final "lost" calls that Jacob
  labels lost, pooled over the four sets, unweighted. Jacob's "not an item" counts as a miss.
- "kept→lost error" = among cards Jacob labels kept, the share the judge calls lost; pooled,
  unweighted, paired on the same cards. The interval on the paired difference (Sonnet − Haiku)
  is the session-clustered bootstrap (2,000 resamples, seed 20261024), percentile 95%.
- "Lost" (judge and Jacob): dropped or misstated with no other route (availability "lost" for
  Experiment 1 sets; for Experiment 2, S2's own rule: not still present). Cards whose item is
  available another way are excluded, as `key_sl_not_lost` does. Can't tell is excluded.
- Known bias, flagged before data: the cards were drawn by Haiku's verdict (most are Haiku-lost),
  so Sonnet's errors on Haiku-kept items are under-sampled and unweighted figures flatter
  Sonnet. The verdict line is applied as written, and the summary also reports the weighted
  (inverse-inclusion) figures per set and the count of labelled Sonnet-lost / Haiku-kept cards.
  If the line says "switch" but rests on few such cards, the recommendation says to label a
  small set of Sonnet-lost / Haiku-kept cards first.
Timestamp: 2026-10-24T17:30:58Z
