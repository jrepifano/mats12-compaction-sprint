# AI models may be capable of self-replicating without access to a datacenter

- Authors: William Zhou, Daniel Freeman (Anthropic Frontier Red Team; authorship per secondary coverage, the fetched page text did not show the byline)
- Date: 2026-10-12 (per secondary coverage; no date in the fetched text)
- URL: https://www.anthropic.com/research/self-replicating-ai
- Captured 2026-10-13 for P44
- **Reading depth:** raw page HTML fetched with curl and converted to text. Read verbatim: the Memento paragraph in "Evaluating inference setup performance", section D "Adaptation and uplift / Learning on the job" (including the figure caption), and Appendix A (The Memento harness) plus the compaction remark in Appendix C. Not read: the IoT/botnet sections, the network replication section, sections on taking advice, Appendix B/D. Figures were not visible (images/interactive charts), so the single-attempt vs Memento chart values are NOT captured.

## Verbatim: abstract

No abstract. Opening of the Memento description: "We run these experiments using Memento, a harness we built on top of the open-source Pi coding agent. Memento is designed to mitigate a common failure mode we observe in long-horizon runs: an agent can become fixated on an incorrect idea (for, say, why a bug is occurring) and continue pursuing it instead of backtracking. One example of such a failure is a Qwen agent debugging package versioning for hours when the real bug came from implementing an endpoint incompatible with the grader API."

"Memento periodically starts a fresh agent instance with the previous agent's workspace and a short set of notes written by the previous agent on what it tried, allowing later instances to pursue different strategies."

## Verbatim: key results

- Learning on the job: "Each Qwen3.5-9B instance keeps brief notes on the strategies it tried and the failures it encountered, before a new instance inherits those notes and attempts the same inference setup again."
- Figure caption: "Inference setup success rate across six generations of Qwen3.5-9B, each inheriting the former's notes. Faint lines are the 20 individual lineages, and the bold line is the proportion of runs in each generation that complete setup. It rises from 45% in the first generation to between 61% and 84% in later ones. This uses the Memento setup (Appendix A)."
- Appendix A: "We spawn an agent instance, which tackles the problem for a while (until generation timeout). The instance keeps brief notes, and after timeout summarizes a small doc of the strategies it tried, the failures it encountered, and its performance (if successful). We create a new agent instance that inherits the codebase and environment from its predecessor as well as the notebook."
- "In the serving experiment, each model is placed in a Memento harness and is given a 1.5 hour per iteration / 6 hour total timeout. Around ~1k tokens of information, including specs about the hardware and a script for a simple sample profiler, are given in the prompt."
- "Memento is intended to improve long-horizon reliability rather than grant new cyber or inference-specific capabilities. It's also not a universally strong harness..."
- Appendix C: "The harness we use is forked off of the Pi coding agent (pi.dev) and contains a native compaction mechanism - even still, we find that models take a large performance hit when their sequence lengths are restricted below 32K."
- Appendix A chart caption: "Single attempt compared against Memento, by model (95% Wilson CI)." (values in a chart, not captured)

## Relevant to P44

- Describes one handoff method exactly matching P44's "Memento-style restart with self-written notes": hard restart with inherited workspace plus a model-written notes doc (strategies tried, failures, performance), generation timeout 1.5 h.
- Measures task capability only (inference-server setup success): 45% gen 1 to 61-84% later gens, 6 generations x 20 lineages, Qwen3.5-9B. It measures no safety-relevant state, constraint retention or aligned behaviour across handoffs.
- The stated rationale for restart is escaping fixation on an incorrect idea, i.e. a deliberate decision to drop the previous context; notes are the only carried channel besides the workspace. The notes are free-form, so what a note drops (for example constraints) is unexamined.
- Harness also has native compaction (Pi), but no compaction-vs-Memento comparison is in the text read; only "single attempt vs Memento" (chart values not captured).
- Gap for P44: no comparison across handoff methods on alignment-relevant behaviour, and no check on whether notes preserve or erode constraints/permissions over generations.
