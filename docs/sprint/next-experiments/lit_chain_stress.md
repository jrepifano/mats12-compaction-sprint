# Literature check: chained tasks with a re-summary at every boundary (27 Oct 2026)

About 60 min, $0. **Verbatim** = I read the primary text (`pdftotext`). **Repo capture** = an existing `papers/` capture, not re-read.

## Closest work

**Froger et al. 2026, "No Reset, Emergent Behaviors"**
- https://neurips.cc/virtual/2026/168222 (abstract only, verbatim).
- A NeurIPS workshop poster ("Agents in the Wild"). The OpenReview PDF needs a login, and there is no arXiv version.
- **Setup:** sequences of tasks from GAIA2 and SWE-Bench Pro, run in one persistent session.
- **Numbers:**
  - Opus 4.6 and GPT-5.4 stay near their independent-task baselines. DeepSeek V4 Pro loses 5.9 points.
  - pass^K declines.
  - Up to 60% fewer output tokens, and up to 4× more Python use.
  - Weaker models "propagate harmful state across tasks".
- **Our three questions:** the abstract mentions no compaction or summarization and no relatedness contrast, and it reports no safety measure. I can't rule these out without the full text.

**Sun et al. 2026, "When 'Must' Becomes 'Maybe'"** (arXiv 2608.24569)
- Verbatim: §1–6, Tables 3–4 and 10, limitations.
- **Setup:** reviewer, then handoff, then executor. 1,296 episodes on synthetic tasks.
- **Erosion:** the blocker stays visible but stops blocking.
  - Multihop compression deactivates 97.2% [93.3, 100] of blockers, with 31.9% forbidden actions. Ownership deferral: 76.7% and 60.8%.
  - **Precedent substitution** ("substitute past approval for current authorization"): 38.3% deactivation, 24.2% forbidden.
- **Models:** DS-V4-Flash takes forbidden actions at 50.0% non-thinking and 36.8% thinking, against 0–17.6% for GPT-5.4.
- **Repair:** restoring the full blocker cuts forbidden actions from 85.2% to 0.0%. An authorization-check prompt cuts them to 1.6% while 95.3% of artifacts stay degraded.
- **Caveat:** the handoff transformations are instructed conditions, not natural summaries, and each run is a single handoff, not a live agent.

**Saha 2026, "Delivery, Not Storage"** (arXiv 2607.20972)
- Verbatim: §5.6–5.7.
- **Setup:** Claude Code with Haiku 4.5, one resumed session, a 100k window. 108 forced compactions with no memory store, and 138 with one.
- **Survival:** ten facts stated once were in **0/10 of summary 1** and in 0/10 of 106 of the 108 summaries.
  - A fact that re-entered the context survived about one more pass.
  - Selection was all-or-nothing: no summary ever carried a strict subset of the ten.
- **Behaviour:** the agent rebuilt the facts by grepping the harness's session JSONL. It ran 260 shell commands against a restated no-shell rule.

**Jin et al. 2026, ChainSWE** (arXiv 2607.02606)
- Verbatim: §4, results, conclusion.
- **Setup:** 100 chains (304 bugs, 54 Python repos) of dependent bugs in the same repo.
- **Position effect:** GPT-5.5 falls from 62.5% to 40.5% to 28.2% across positions 1–3 when the repo state accumulates. It *rises* to 61.2%, 70.4% and 75.9% from the oracle state. So the penalty comes from accumulated state.
- **Carried history:** helps only GPT-5.5 (+4.6). DeepSeek V4 Pro goes from 27.3 to 27.5.
- **Summarization (within a bug, at 50k):** the oracle-to-sequential drop is 40%, against 33% with the raw history.
- **Gaps:** related tasks only, and no safety measure.

**Already in the repo** (repo captures):
- **Repeated compaction:**
  - Governance Decay: pooled violations 0, 34, 37, 42 and 47 of 60 over rounds 0–4.
  - Compaction Cliff: 53% of rules kept after one `/compact`, 10% after five.
  - compaction-stress-lab: Luna kept 67%, 48%, 30% and 23% of facts over four compactions, and 7 of 72 post-compaction plans used stale values (0 of 36 without compaction).
  - MasDrift: about 72% of constraint losses happen at the first handoff.
  - TRACE: splits the loss into new-step and inherited loss.
- **Misattribution:**
  - AuthMem-Bench's S2D template turns an assistant suggestion into a user decision.
  - EAL-Bench: incremental updates are 10–30 points worse than one-shot rewriting.
  - Compaction Laundering: pi's serializer destroys provenance.
- **Precedent:**
  - Terry & Andriushchenko: a prior reward hack raises later hacking (no compaction).
  - Our task-chaining readout: B force-pushed 48% of the time with history, against 12% fresh. A's own force-push predicts B's (0.85 against 0.08 in no-permission histories; exploratory).

## What remains unclaimed

1. **Re-summary at task boundaries in a live coding agent, with behaviour scored.** The retention studies plant facts or rules within one task. No one scores stale items carried over from *finished* tasks.
2. **Relatedness × repeated compaction.** Neither Froger (as far as the abstract shows) nor ChainSWE has an unrelated-project arm.
3. **Misattribution across projects.** AuthMem covers *who* gave an instruction, not *which project* it belongs to. Nothing found.
4. **Natural broadening of a scoped permission.** Sun et al. tested only instructed weakening of blockers.
5. **Precedent by chain position.** Not reported anywhere.

## Design lessons

- **Score each item's role, not only its presence** (Sun et al.): kept-binding, kept-as-caveat, dropped, broadened or misattributed.
- **Expect a first-boundary cliff and all-or-nothing selection.** Plot survival per item by boundary index. Most loss lands at boundary 1, so later boundaries mostly measure carry-over.
- **Separate state carry-over from context carry-over.** ChainSWE's penalty came from repo state, so add fresh-session and oracle-state arms on the same workspace.
- **Split new-step loss from inherited loss** (TRACE): at boundary k, re-summarize from the full history and compare with the iterative update.
- **Hide pi's session JSONL:** agents grep harness files for dropped facts.
- **DS-V4-Flash is a very compliant executor** (37–50% forbidden actions in Sun et al.), so a positive result may not generalize.
- **Report pass^K and output tokens** (Froger).
