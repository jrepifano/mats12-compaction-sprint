# Preregistration template

One file per experiment, at `docs/experiments/<ID>/prereg.md`, mirrored to a Claude Doc for review and sharing (see AGENTS.md).

**Write for a colleague who hasn't seen the repo.**
- Use plain prose, with tables where they help.
- **Avoid internal codes.** Name things in words: "the published recipe", "the template-restore test", "the loss-masking check", not R1, P3 or S1. Experiment ids like E1 may appear in the summary table only.
- Define a technical term once, the first time it appears.
- Cite prior work inline as [Author et al. year, §x]. **Every citation links to its reference.** Each reference is its own level-4 heading in the References section. In markdown the link is `[Author et al. year](#author-et-al-year)`; in the Claude Doc it links to the heading's block id.
- Keep file paths out of the prose, except in the implementation plan and outputs.

## Title
The research question, phrased as a question.

## Review summary (one screen)
Required, directly under the title, so Jacob and the reviewer can sign off from the top (Jacob, 2026-10-08). It should fit one screen **as rendered**: short cells, with seeds, amendment histories and review records linked rather than listed. Everything in it must agree with the body, which it links to. Its heading says which design it shows: **"proposed design (v…, not yet signed off)"** for any unsigned version, first drafts included, and **"current approved design (v…, signed off <date>)"** once Jacob approves. A table with these rows:

| Row | What it says |
|---|---|
| **Decision it informs** | The choice this result changes, and the default if we don't run it |
| **Arms** | Every condition, one line each, with seeds |
| **Primary measure(s)** | The preregistered primary measure(s), comparison(s) and test, one line per primary question or stage |
| **Predictions** | The 3–5 outcomes that matter, each with its prior |
| **Stops and kill rules** | The decision-critical stops as thresholds, with a link to the full list (§6–7) |
| **Cost** | Planned and the hard cap, in dollars and wall-clock; for API-heavy runs, the provider benchmark (`scripts/provider_bench.py`) behind the wall-clock projection |
| **Changed since last version** | One line per change, linking to the amendment; "first version" if new |
| **Reviews** | gpt-6-astra rounds done (cap 3 per artifact), each verdict, and the count of findings still open, linking to their table |
| **Open for Jacob** | Decisions still his, or "none" |

## Summary table and key definitions
- **Summary table:** experiment id and topic, version, status (`Draft, not approved`), author, reviews done (max 3 gpt-6-astra rounds, all before sign-off), budget (planned and cap).
- **Key definitions:** one short paragraph defining the core terms the rest of the document relies on.

## 1. Research question
One or two sentences, plus any secondary questions.

## 2. Why this matters
1. **Where the question comes from.** Prefer Neel's own framing, quoted from `papers/_neel_brainstorm_text.txt` or the "Open questions (from Neel's Sept 27 sessions)" part of `papers/_litreview_text.txt`.
2. **The gap in the literature**, grounded in the primary sources and cited inline.
3. **Why a frontier lab would care**, in one short paragraph: the decision this feeds into and who makes it. If no answer would change anyone's decision, say so. The per-outcome detail lives only in §6.

## 3. What we will do
A one-paragraph, high-level overview of the experiment, then a short table of the work parts (where each runs, what it produces, its cost), before any detail.

## 4. Experimental procedure
The rule: **lift the procedure directly from the literature wherever one exists. Where none does, take the next most obvious step from an existing procedure.** Say so in prose next to each element: cite the source when used as published; state the change and reason when adapted; mark our own additions and name the procedure they extend.

Cover these, in subsections:
- models (with pinned revisions) and data (with provenance);
- formats or conditions, arms, and controls;
- hyperparameters;
- sample sizes, with a power or minimum-detectable-effect argument;
- metrics and exact decision rules;
- judges, with how they are validated;
- statistical tests;
- the checks that must pass before any result is read;
- seeds and outputs.

## 5. Hypotheses and predictions
- Named hypotheses, including competing ones.
- A table of predictions with prior probabilities and their basis, all written before any data.
- Say where the priors depart from published results.

## 6. Outcome scenarios
Named scenarios, checked in a stated order with a catch-all last, so they cover every outcome. The table columns:

| Scenario | What we observe (numeric thresholds) | Answer to the research question | Why it matters to a lab | Next step |

## 7. Budget, timeline and stopping points
- Prices, and itemized costs with the arithmetic shown.
- The hard cap and the pre-set cuts.
- Timeline, and Jacob's hours.
- Stopping rules.
- Numbered checkpoints where work stops for Jacob.

## 8. Implementation plan
- What gets ported from the working repo, and what is new.
- Compute.
- Order of work, smoke test first.

## 9. Risks, limitations and open questions
Include source-reading gaps, such as a paper read only in part or a MISSING source, and what they affect.

## 10. Review history
Each gpt-6-astra round and each of Jacob's reviews: what changed and what was decided. After round 3, a table of findings still open, for Jacob to decide.

## References
One level-4 heading per work (`#### Author et al. year`), then its title and venue on the next line, plus a final `#### Code and models` heading. Local copies are in `papers/`.

## Amendments
An amendment after sign-off goes in §11 and **starts with its own review summary** (the table above, filled for what the amendment changes), then the detail. It must also record:
- the signed-off baseline it amends, by version and commit;
- each changed rule as **old → new**;
- its date, and whether any data bearing on it had been seen (post-data changes stay labelled as such even after approval);
- its review verdicts, and Jacob's approval with date, or "not yet approved".

The top-of-file review summary is updated in the same edit and labelled "proposed design" until Jacob signs the amendment off, then "current approved design". Earlier signed-off versions stay recoverable by commit and are never edited.

