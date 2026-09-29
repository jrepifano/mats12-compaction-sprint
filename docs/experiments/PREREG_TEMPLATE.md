# Preregistration template

One file per experiment, at `docs/experiments/<ID>/prereg.md`. Write for Jacob: plain prose, then tables where they help. Every factual claim about prior work cites a file in `papers/`, with a section or page where possible.

## Header
Experiment ID, title, version, date, status (`DRAFT — not approved`), author (agent), and review rounds completed (max 3).

## 1. Research question
One or two sentences, plus any sub-questions.

## 2. Why this matters
One section, in three parts:
1. **Where the question comes from.** Prefer Neel's own framing, quoted from `papers/_neel_brainstorm_text.txt` or the "Open questions (from Neel's Sept 27 sessions)" part of `papers/_litreview_text.txt`.
2. **The literature gap it fills**, grounded in the primary sources.
3. **The lab value**, in one short paragraph: what decision at a frontier lab this experiment feeds into, and who makes it. If no answer would change anyone's decision, say so.

The per-outcome detail (which answer changes what, for whom) lives only in §6's "So-what for labs" column. Don't repeat it here.

## 3. Sources read
Every source read in full while planning. For each, give one line on what it contributed to the design (a recipe, hyperparameters, eval set, known failure mode, number to reproduce). Cover:
- papers in `papers/`;
- upstream repos and model cards;
- `docs/working-repo-code-map.md` entries.

List any MISSING source and how its absence affects the plan.

## 4. Experimental procedure
The rule: **lift the procedure directly from the literature wherever one exists. Where none does, take the next most obvious step from an existing procedure.**

For every design element, give its provenance:
- **[LIFTED: source §x]** — used as published;
- **[ADAPTED: source §x — what changed and why]**;
- **[NEW: justification, and which published procedure it extends]**.

Elements to specify:
- models, with HF ids and pinned revisions where known;
- data and its provenance;
- hyperparameters;
- conditions, arms, and controls;
- sample sizes, with a minimum-detectable-effect or power argument;
- metrics and exact definitions;
- judges, their prompts, and how they are validated;
- the statistical test;
- manipulation and sanity checks that must pass before the headline number means anything;
- seeds and determinism;
- the exact output artifacts.

## 5. Hypotheses and predictions
Main hypothesis, competing hypotheses, and numeric predictions with prior probabilities, all written before any data. Say where priors come from, and where we depart from published results.

## 6. Outcome scenarios
A table with one row per plausible scenario (including "uninterpretable"):

| Scenario | Observed pattern (numeric thresholds) | Answer to the research question | So-what for labs | Our next step (continue / pivot / kill, and which experiment follows) |

Scenarios must cover the outcome space: no gaps, and no overlaps that break the decision rule.

## 7. Budget, timeline, stop points
- Itemized $ with the arithmetic shown, and a hard cap.
- Wall-clock time, and Jacob's active hours.
- Kill criteria.
- Numbered checkpoints where we stop, report, and wait for Jacob.

## 8. Implementation plan
- What to port from `../mats12_neel_working`, with paths from the code map, and the edits needed.
- What is new.
- Pod or API needs.
- Order of work, with a smoke test first.

## 9. Risks, limitations, open questions for Jacob

## 10. Review log
One line per gpt-6-sol round: the file in `logs/reviews/`, the number of BLOCKING / MINOR / NIT findings, and how each was handled. After round 3, leave a table of open findings for Jacob.
