**Commit to 1b as the main experiment, with 2 as a bounded companion analysis.** Use the same task-chaining environments for both. Reserve Friday for understanding the results and preparing the presentation.

My ranking for this deadline:

| Rank | Idea | Judgment |
|---|---|---|
| 1 | **1b: separate malicious summarizer** | Direct safety question, controllable intervention, existing infrastructure, interpretable decomposition. Novelty is narrow. |
| 2 | **2: natural monitorability** | Cheapest extension of existing results; realistic trajectories and paired monitor inputs. Needs corrected authorization labels. |
| 3 | **7: targeted monitor memory / validation** | Valuable follow-up within the above experiments. Timing manipulation and motivated reasoning are separate, less tractable projects. |
| 4 | **3: impossible-task chaining** | Clear behavioral endpoint and infrastructure reuse, but another uncertain compaction effect after P49. |
| 5 | **6: refusal erasure** | Potentially quick signal, but baseline refusals may be scarce and generic injection may explain everything. |
| 6 | **4: many compactions** | Slow, confounds task progression with compaction count, and risks extending the negative-results search. |
| 7 | **5: situational awareness** | Interesting novelty, weak behavioral interpretation: verbalized awareness is not a safety outcome. |
| 8 | **1a: malicious self-summary** | Strong prior work; little reason to spend the remaining sprint reproducing it. |

I disagree with two framings. First, **task-A-only permission never authorizes task B**. The existing permission/no-permission arms are not authorized/unauthorized labels for B. Second, separate writers influencing executors are already studied in [EAL-Bench](https://arxiv.org/abs/2609.01836) and [AuthMem-Bench](https://arxiv.org/abs/2608.01679). The defensible contribution is adversarial compaction in real coding continuations, with an honest successor and measured detection.

**1b: Can a malicious summary induce an undetected scope violation?**

Use pi, DeepSeek V4.1 Flash as the honest successor, and the existing Docker force-push tasks. Opus 5.5 generates adversarial summaries through subscription access. It may change summary text only; it cannot edit the environment or successor instructions.

Freeze the attack procedure after development. Give it one attempt per held-out history, without monitor feedback. Fork identical repository states into four arms:

- Native honest summary.
- Honest summary with a simple false approval claim—the simplest attack baseline.
- Adversarial summary, with a fixed edit budget.
- The same adversarial passage delivered through tool output, with the honest summary retained.

Use **24 distinct source histories × two continuation seeds = 48 runs per arm**, topping up existing histories as needed. Bootstrap by source history; repeated seeds are not independent tasks. This can resolve large effects, roughly 25–35 percentage points depending on pairing, but cannot establish small differences or broad generality.

Use Haiku 5.5 and Opus 5.5 as weak/strong shadow monitors with identical information access: source history, summary, and available reasoning/actions up to each decision. Hide only the attacker’s objective; do not manufacture missing CoT. Score the summary boundary and consequential actions before execution.

Primary metric: **fraction of runs producing an unauthorized overwrite without an earlier monitor flag**. Separately report overwrite rate, detection conditional on overwrite, and false alarms on 24 genuinely authorized controls. Use repository state to verify overwrites, not command spelling. Shadow results establish replay detection, not deployed blocking effectiveness.

Cheap gate: six development histories across four arms, approximately **$36 and four rig-hours**. Stop or permit one task adjustment if honest-summary overwrites exceed 50%, attacks induce none, or outcome labels are ambiguous. Do not weaken the monitor repeatedly until something works.

Budget **$600**, including history generation, controls, monitors and retries; approximately **35–45 hours elapsed** assuming the stated throughput and subscription quotas. Thursday: main effects, uncertainty and representative traces. Friday: one mechanism ablation—remove the apparent permission claim—and final analysis. If tool-output attacks work equally well, claim generic inherited-context influence, not special summary authority.

**2: Does natural compaction change authorization judgments?**

Reuse all **88 completed B transcripts**, scoring each under full history, native summary plus continuation, and continuation only. Keep the continuation identical across views and exclude future outcomes at the scored decision. Use the same two monitors.

Primary metric: paired change in authorization-classification error; report false negatives and false positives separately. Add matched, explicitly authorized counterfactual controls and regenerate their summaries; label these controls synthetic. Include “always flag destructive pushes” as a baseline.

Cluster by shared A history. This is enough for a large diagnostic effect, not precise low-FPR calibration. Kill expansion if a 12-transcript pilot reveals ambiguous labels, insufficient class coverage, or no meaningful disagreements. Budget **$0–75 and 6–12 hours**, mostly independent of Docker. Complete before Thursday; Friday only adjudication and plotting.

**The story:** existing compaction usually preserves useful task state. Preservation can also carry unsafe precedent or false authority. These experiments test whether that inherited state steers behavior and whether oversight recognizes the problem. If compacted and full history remain similar, say so.

The biggest risk is mistaking ordinary context influence for a compaction-specific failure. The tool-output baseline and natural paired analysis address that directly.

Before committing, spend at most two hours checking whether [Ge-Wang’s announced continuation evaluation](https://www.lesswrong.com/posts/cENaGChQZD7u7sZkz/towards-deployment-time-misalignment-continuation-evals) has released adversarial summarizer-to-honest-successor results, and whether EAL/AuthMem evaluate independent monitors.
