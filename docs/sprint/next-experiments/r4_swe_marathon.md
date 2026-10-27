# SWE-Marathon: note (27 Oct, from a web-fetch read)

Desai et al. (Abundant), "SWE-Marathon", arXiv 2606.07682, 5 Jun 2026. Preprint. Read verbatim: the abstract and HTML Sections 1–7. Appendices D–E were read only through a fetch-tool summary. The release site (swe-marathon.org) returned no content, so the licence and format are unconfirmed.

- **Tasks:** 20 long-horizon tasks: library clones and rewrites (e.g. kubernetes in Rust), product clones, ML engineering, algorithmic. Agents get 2–10 h each. Hidden deterministic verifiers grade them.
- **Runs:** 13 agent–model configurations × 20 tasks × 5 trials = 1,300 trajectories. Claude Code, Codex CLI, Gemini CLI, Kimi Code, and Terminus 2 with seven models. Tokens per trial: median 7.6M, max 877M. Pass@1 is below 30% everywhere.
- **Compaction:** analysed only for Terminus 2's summarizer: "0 of 71 reward-bearing terminus-2 summarizer trials pass, against 8.9% without". "Compaction tracks failure rather than rescue" is a correlation. No per-trajectory compaction counts were found.
- **Reward hacking:** an LLM judge scores suspicion after the run. 13.8% of rollouts contain an exploit-shaped action, and 10.2% ship a clear verifier bypass; 0/1,300 earn reward from it. By model: GPT-5.5 26%, Gemini 3.1 Pro 22%, Opus 4.7 0.5%. (The 15.4% figure is the reward-hacking share of agent-attributable failures, 81/526.)
- **Release:** "We release SWE-Marathon, evaluation code, and agent trajectories at swe-marathon.org". Not confirmed: the size, the licence, and whether compaction summaries are included.
- **Monitoring:** no runtime-monitor, authorization or destructive-action results.
