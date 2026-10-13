# The Compaction Cliff in Long-Running AI Agent Memory

- Authors: Saber Zerhoudi, Jelena Mitrovic, Michael Granitzer (Passau). arXiv:2608.22752, submitted 24 Aug 2026 (cs.AI, cs.IR); listed as CIKM 2026.
- URL: https://arxiv.org/abs/2608.22752 (PDF: https://arxiv.org/pdf/2608.22752)
- Captured 2026-10-13 for P38.
- **Reading depth: FETCH-TOOL SUMMARY ONLY** (abstract page via WebFetch; the fetch tool declined to reproduce the abstract verbatim; the PDF body was not read). Numbers below are the fetch tool's paraphrase and were cross-checked against a Mem0 blog write-up (https://mem0.ai/blog/testing-memory-placement-against-compaction-cliff), which gives the same 53% / 10%. Verify against the PDF before quoting.

## Relevant to P38
- Claim: compaction is type-blind. Safety rules and episodic logs are summarised at the same rate, although rules need exact wording to stay enforceable.
- Claude Code `/compact`, Sonnet 4.6, 20 production AGENTS.md-style configurations: 53% of safety rules preserved after one compaction round, 10% after five.
- Proposed fix "Knowledge Triage": TypeCompact preserves 2-4x more safety rules than the strongest single-shot LLM compactor, 96% recall over five rounds; TypeDecompose 0% locality violations vs 93% uniform; TypeRetrieve 100% recall@50 vs 73%.
- Downstream: medical compliance (McNemar p < 1e-8, N=200), retail pass rate (p < 0.01, N=115), airline (p=0.024).
- The unit is a written rule in a config file, not a fact established by tool output (P38's case). No environment-reality belief is measured.
