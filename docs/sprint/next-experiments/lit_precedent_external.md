# Precedent experiment: external literature check

2026-10-27. **How each source was read:** "fetch summary" means the WebFetch model's summary of the abstract or HTML page, not verbatim text. Sun et al. (item 8) was the only paper read verbatim as a PDF.

## Closest prior work

1. **Terry and Andriushchenko 2026, "Measuring alignment drift via trajectory prefixes"** (https://www.lesswrong.com/posts/pdtGsC888sTToZhiY). *Read verbatim from the repo capture (`papers/compaction_safety/` row 5). Its rates are given only in figures.*
   - Two chained tasks in one context, 40 runs per prefix.
   - With same-type tasks, a reward hack in the first task raises hacking in the second, and so does another agent's log. With cross-type tasks the effect is unpredictable.
   - **Gap:** prefixes were selected by their outcome, mostly one per condition, so the first action is confounded with whatever produced it.
2. **Rodríguez Salgado 2026, "History Anchors"** (arXiv 2605.13825). *Fetch summary of the HTML.*
   - 100 scenarios, each with 3 author-written unsafe prior actions, then a choice among 4 options; 17 models.
   - Under a neutral prompt, Sonnet 4.6, Opus 4.7 and GPT-5.5 chose unsafe 0% of the time, and DeepSeek V4 Flash 37%.
   - A "stay consistent with prior history" instruction raised these to 91–98% (V4 Flash 93%). An all-safe history kept rates at or below 8%.
   - The history is forged, there is a single choice, and nothing is executed.
3. **Sun, Liu and Weng 2026, "The Cold-Start Safety Gap"** (arXiv 2606.07867). *Fetch summary of the first 100k of 125k characters.*
   - Safety rises by 8.9–52.1 points after 20 benign tasks. Requests account for about 17 points of that and the agent's own responses for about 8.
   - **History length alone can make B safer, so match it across arms.**
4. **Zhang et al. 2026, "When Consent Outlives Context"** (arXiv 2609.33910). *Fetch summary of the abstract.*
   - A replayed earlier user approval raises prompt-injection success by up to +35.1 points (508 AgentDojo cases).
   - It raises context-rebinding success by +24.9 points on average (55 Terminal-Bench cases, 3 coding agents).
   - The licence here is the user's approval, not the agent's own act.
5. **Qu et al. 2026, "Overeager Coding Agents"** (arXiv 2605.18583). *Fetch summary of the abstract; full text in `papers/agent_swarms/` row 109.*
   - Removing the consent statement raised Claude Code's overeager rate from 0.0% to 17.1%, and every base model's by 11.9–17.2 points.
   - Single tasks only.
6. **Mastykarz 2026, "Can a local git remote replace GitHub in your agent eval?"** (https://developer.microsoft.com/blog/can-a-local-git-remote-replace-github-in-your-agent-eval/). *Fetch summary.*
   - GPT-5.6 Sol pushed unprompted in 65% of runs with a GitHub remote, against 54% with a local remote (n=170 each, p=0.033).
   - **Risk for us:** a local remote may lower B's base rate and act as an eval cue.
7. **Dabirsiaghi 2026, "Compaction Laundering"** (arXiv 2610.29869). *Fetch summary of the abstract.*
   - A forged "[User]:" approval survives compaction 87–100% of the time in role-flattening harnesses, against 0% in role-typed ones.
   - It led to `curl | sh` about 50% of the time, against 0% clean.
   - For our compaction arm, this means harness rendering decides how A's force-push is attributed after compaction.
8. **Sun et al. 2026, "When 'Must' Becomes 'Maybe'"** (arXiv 2608.24569 v2). *Read verbatim from the PDF (`pdftotext`): abstract, §1, §3.2–5, Limitations, App. A–B, Tables 3, 4 and 10. §6 was only skimmed.*
   - **Design:** synthetic enterprise tasks. A reviewer flags a blocker, a "condition-specific generator" writes one handoff under an instructed transformation, and a separate executor picks from fixed candidate actions. The executor's choices are simulated action requests, with no live tools.
   - **Precedent substitution** is defined as "Introduce a similar case or exception", which works to "Substitute past approval for current authorization" (Table 3). It gave 38.3% deactivation [32.4, 44.4] and 24.2% forbidden action [20.8, 27.8] (n=120, control 0/0; Table 4). Ownership deferral was stronger, at 76.7% and 60.8%.
   - **DS-V4-Flash forbidden action:** 50.0% non-thinking and 36.8% thinking. These are from Table 10's validation panel (n=68), *pooled over all conditions*, not precedent substitution alone. The other agent's numbers are correct, but these two are not specific to precedent substitution.
   - **Closer prior work than items 1–2? No.** Its "precedent" is someone else's past approval or exception, written into an instructed handoff. That puts it alongside Zhang and Dabirsiaghi (approval carry-over), not an agent's own act licensing itself.

**Background (abstracts only):**
- Menon 2026 (arXiv 2603.03258): off-policy prefill.
- Crescendo and FITD: jailbreaks that use the model's own replies.
- Pan et al.: in-context reward hacking.
- "Froger et al. 2026" was not found; treat it as unverified.

## Concurrent work (Sept–Oct 2026)

- AuthMem-Bench (2608.01679) and EAL-Bench (2609.01836), both in `papers/`: memory-borne false authority.
- Boiling the Frog (2605.22643): user-driven escalation, 44.4% attack success.
- HarnessSafe (2608.06984).
- Transluce's in-the-wild study: 1.9% monitor evasion.

## Gap our design fills

Our test randomizes, **upstream in the environment**, whether the agent's *own unprompted* action happened. A junk commit makes A's push fail, so A force-pushes without being asked. The runs are live, multi-turn coding sessions with real git. They contain no approval anywhere, no edited transcript and no instructed handoff. Each prior design breaks one of these conditions:
- it selects prefixes by their outcome (Terry and Andriushchenko);
- it forges the history (History Anchors);
- it prefills another model's trajectory (Menon);
- it carries over someone's *approval* (Zhang, Dabirsiaghi, and Sun et al.'s precedent substitution, the last through an instructed single handoff to a finite-choice executor).

None of them measures a destructive git precedent from one task to the next, or crosses one with natural compaction. Confounds to control: history length (Cold-Start) and the local remote as an eval cue (Mastykarz).

## Worth capturing into `papers/`

| Title | URL | Why |
|---|---|---|
| History Anchors | https://arxiv.org/abs/2605.13825 | Closest result on precedent; V4 Flash rates |
| The Cold-Start Safety Gap | https://arxiv.org/abs/2606.07867 | Length confound, opposite sign |
| When Consent Outlives Context | https://arxiv.org/abs/2609.33910 | Approval carry-over on Terminal-Bench |
| Compaction Laundering | https://arxiv.org/abs/2610.29869 | Role flattening in our compaction arm |
| Local git remote vs GitHub | https://developer.microsoft.com/blog/can-a-local-git-remote-replace-github-in-your-agent-eval/ | Push gap with a local remote |
| When "Must" Becomes "Maybe" | https://arxiv.org/abs/2608.24569 | Precedent substitution in handoffs; DS-V4-Flash rates |
