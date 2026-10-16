# Probe plan docs: the Claude Doc format (Jacob, 2026-10-15)

When Jacob asks for a Claude Doc of a probe plan, build it in this format. `plan.md` stays the agent's full spec and the source of truth; the doc is how Jacob decides go / no-go and what he shares. Claude sessions only (Docs tools).

Why: the P49 and P50 docs were hard to digest. They had internal codes ("C1 − R", "B3", "Δ_own"), table cells of 2,000–4,000 characters, predictions in one section and their consequences in another, file paths and review history inline, no picture of the setup, and 5,000 words. Jacob's comment threads asked "why this model / judge / scale?", because the reasons were buried.

## Two tabs

**Tab 1, the plan (≈ 3-minute read).** In this order:

| Section | Holds | Limit |
|---|---|---|
| Title | The question in plain words | ≤ 15 words |
| Lead | What we'll learn, cap, when results land (ET) | 3 sentences |
| So what | Why Neel would care: which of his stated interests it answers (quote him, with the source), what result we could pitch, and what a null would still tell us | ≤ 4 sentences |
| Your decision | Go / no-go, plus up to 3 defaults Jacob can override, as `- [ ]` tasks | ≤ 4 items |
| How it works | One diagram of the pipeline (`topic.diagram`) | ≤ 6 boxes |
| Arms | Table: arm (plain name), what changes, why it's here | ≤ 6 rows, cells ≤ 20 words |
| The measure | The one number, the decisive comparison, the judge and whether it's validated | 3 sentences |
| Predictions → what we'd do | One table: outcome, our probability, what we do next | ≤ 4 rows |
| Key choices and why | Model, judge, scale, data, anything Jacob would ask "why X?" about: one line each with the reason and the alternative rejected | ≤ 5 rows |
| Cost and timing | Cap, projection, RunPod top-up needed, an ET timeline in 12-hour clock times | small table |
| Risks | What could make the result uninformative, and what we'd do | ≤ 4 bullets |
| Prior work | The 2–3 closest sources and the gap, each linked | ≤ 3 bullets |

**Tab 2, Details.** Everything the agent needs but Jacob doesn't for the decision: full arm specs, power, automatic rules and gates, the regime check, review findings and their dispositions, accounting, paths. Still plain language.

## Writing rules (tab 1)

- **No codes, no paths.** Arms get plain names ("no compaction", "pi's compaction"). Earlier probes get a gloss: "our handoff probe (P44)". No Greek-letter scores; name them ("the likelihood-ratio score").
- About 600 words of prose, tables excluded. Sentences under 25 words. Bold only for the few words a skimmer must see.
- Numbers carry units and intervals. Times in ET, 12-hour, with the date.
- No revision history, review dispositions or "superseded" notes. One line at the end: "Reviewed by gpt-6-astra (N rounds); open findings: none / see Details."

## Process

1. Build a **new doc** (birth with one pending block per section, fill one section per call, then the diagram), and open it with the Artifact tool. Don't overwrite an older doc: Jacob's comment threads stay on it. Put one line at the top of the old doc linking the new one.
2. **Fresh-eyes check before Jacob sees it:** a sub-agent reads only tab 1 and must state the go / no-go question, the cap, when results land, and which result would matter. If it can't, revise.
3. Put the doc link (and the rev you synced at) in `plan.md`'s header and `log.md`.
