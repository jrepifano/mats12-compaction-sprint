# SnitchBench

**Author:** Theo Browne (t3.gg)
**Date:** 2025 (site live/updated as of retrieval)
**Source URL:** https://snitchbench.t3.gg/
**Retrieval date:** 2026-09-28

---

## Page content

SnitchBench is an informal, interactive benchmark/dashboard site ("AI Whistleblower Benchmark Visualizer") measuring how often language models with email and CLI tools contact government or media authorities about fictional corporate wrongdoing when placed in an agentic harness.

Static page chrome captured via HTTP fetch:

```
SnitchBench SNITCH BENCH Surveillance / AI Whistleblower Analysis System: MONITORING Models: 8 Total Runs: 640 All Tests Tame + Email Tame + CLI Bold + Email Bold + CLI Filter Models [ 8 / 8 ] Contact Rates by Model Government (red) and Media (amber) contact percentage · Across all test variants · 640 total runs
```

## Limitation

This page is a client-side-rendered (Next.js) dashboard: the model-by-model contact-rate table and charts (8 models, 640 total runs, across Tame+Email / Tame+CLI / Bold+Email / Bold+CLI variants) are populated by JavaScript after page load and are not present in the server-delivered HTML or any embedded JSON payload. Automated fetch (curl, and Claude's WebFetch tool) could retrieve only the static page shell shown above, not the underlying per-model numeric results. A real browser session (e.g. Claude in Chrome) was not available in this environment to render the page and read the live table; the extension reported "not connected." The per-model percentages described in the literature-review context (drawn from this site) could not be independently re-verified from this capture and are not reproduced here to avoid fabrication.
