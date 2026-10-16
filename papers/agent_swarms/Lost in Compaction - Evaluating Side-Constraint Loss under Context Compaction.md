# Lost in Compaction: Evaluating Side-Constraint Loss under Context Compaction

**Authors:** Zhiqi Wang, Yichi Zhang, Dongwon Lee, Yuchen Yang (Penn State)
**Date:** arXiv 2608.11242v1, 31 Jul 2026
**URL:** https://arxiv.org/abs/2608.11242 ; code https://github.com/ZhiqiEliWang/compaction-integrity
**Retrieved:** 2026-10-07
**Reading depth:** abstract, introduction, RQ1 and RQ3 result text (retention by compactor, location, framing) read verbatim from the PDF text; appendices and the extractor method not read.

## Verbatim: abstract
"When the context window is under pressure, LLM systems compact prior context to continue ongoing tasks. We identify a class of user-issued instructions, Session Constraints (SCs), such as 'do not delete any emails until I confirm,' that are meant to constrain LLM's behavior for the remainder of a session but are silently dropped during compaction. To quantify this loss, we introduce COMPINT, an evaluation suite that evaluates compactors across three long-context scenarios: multi-turn chat, agentic trajectory, and long-horizon research. Current compactors retain only 17% of injected SCs on average, and most perform worse than running the same task without compaction. Retention varies sharply with compactor, prompt, context length, SC phrasing, and injection location, showing that the loss is systematic rather than tied to any single setting. We propose an SC-aware extractor that runs alongside the compactor as a plug-and-play module, achieving over 90% retention across all three scenarios without modifying the compactor or LLM."

## Verbatim: results text
"Commercial LLM-based compactors attain the highest retention rates but nevertheless fail in many cases. GPT-5.4-mini reaches a retention rate as high as 98%, yet drops to as low as 6.7%." "Low retention also persists when the OpenResearcher trajectory generator and compactor are both gpt-oss-120b (8.4%...), showing that source-compactor mismatch alone cannot explain the observed loss." "compactors with the pi-mono prompt retain SCs more effectively than those with the Anthropic prompt [on datasets with fewer turns and tool calls], yielding an average margin of 14.5%." "For LLM-based compactors, we observe that later injection locations yield higher retention rates." (Early-position constraints are retained less.)

Relevance to X6: retention depends on compactor prompt (the Anthropic/Claude-Code prompt vs pi-mono), on position (earlier worse) and on phrasing. A header at episode start is the worst-case position. Retention is measured by an LLM judge on the summary text, the same measurement X6 needs.

## Added 2026-10-14 for P49 (v2 read verbatim)
- **Version:** arXiv 2608.11242**v2**, 11 Oct 2026, retitled "Lost in Context Compaction: When LLM Systems Drop Side Constraints", authors Zhiqi Wang, Yichi Zhang, Dongwon Lee, Nathalie Baracaldo, Yuchen Yang; the suite is renamed CONPACT (v1: COMPINT). Text extraction of v2: `Lost in Compaction - Evaluating Side-Constraint Loss under Context Compaction.txt` (`pdftotext -layout`).
- **Reading depth (v2):** §1-2 (inbox experiment), §4.1-4.4 (setup, datasets, metrics), §5 RQ1-RQ3 text, Tables 1, 2, 8, 9, 10, 11, Limitations, App. B.1-B.4 (compactor settings, both prompts, retention-judge prompt and validation) and App. H.3 (inbox protocol) read verbatim. Not read: App. A, C.2-C.3, C.5-C.6 detail, D-G, E.1-E.2 figures.

**Regime.** "Compaction shortens the history as it approaches the LLM's context limit Lmax. It is triggered when ℓ(H t) ≥ αLmax". "We construct filler contexts by cropping and concatenating trajectories to 100K tokens, approximately 80% of a 128K-token context window, to simulate a near-limit compaction trigger." Table 2 means: WildChat 101,121 tokens / 519.3 turns; Hermes Agent 100,142 / 119.7; OpenResearcher 100,481 / 310.5 (native long-horizon); SWE-Natural 104,222 / 229.9 (native trajectories, 126 real constraints from GitHub issues). GPT-5.4-mini run on 220K contexts (N=300, about $800). **One compaction per instance** throughout; no repeated-compaction experiment. Compactor output is short (LLMLingua-2 fixed at 500 tokens "consistent with the order of magnitude produced by the LLM-based compactors").

**Compactor prompts.** "gpt-oss-120b with either the Anthropic compaction prompt (Anthropic, 2026) or the Pi prompt used by OpenClaw". The "Anthropic" prompt is the Claude Developer Platform example prompt ("You have written a partial transcript for the initial task above. ... You must wrap your summary in a <summary></summary> block."; see `Claude API server-side compaction (docs, 2026-10-14).md`; this is the Claude API default, not the Claude Code `/compact` prompt, which is a separate nine-section prompt per `Claude Code compaction (docs and v2.1.300 binary, 2026-10-14).md`), "last accessed October 9, 2026". The Pi prompt has seven fixed sections including "## Constraints & Preferences - [Any constraints, preferences, or requirements mentioned by user]" and "## Key Decisions".

**Inbox experiment (Table 1; gpt-oss-120b agent, Pi prompt, compaction once when context first exceeds 100K counted tokens; 20 seeds x 5 continuation phrases).** Email deletion before compaction 0/100; after compaction 30/100; after compaction with the constraint restated in the first post-compaction user turn 13/100. Constraint retention in the summary 25/100. "28 of the 30 sessions with deletions had the constraint silently dropped during compaction." Continuation ran until context exceeded 100K again or 800 agent turns; "sessions that end before reaching compaction are resampled".

**Table 8 (N=750 per cell, 100K contexts; retention % / compliance on compacted context % / upper bound with SC appended after the summary %):**

| Compactor | Hermes Agent | OpenResearcher | WildChat |
|---|---|---|---|
| Recent-5 | 0.4 / 48.4 / 95.7 | 0.0 / 52.0 / 95.5 | 0.0 / 53.3 / 93.5 |
| LLMLingua-2 | 0.0 / 50.1 / 99.7 | 0.0 / 52.8 / 96.9 | 0.0 / 52.4 / 94.9 |
| Gemma-4 (Anthropic) | 12.5 / 55.0 / 99.1 | 3.6 / 54.5 / 96.7 | 2.0 / 55.7 / 94.5 |
| Qwen3 (Anthropic) | 2.1 / 53.6 / 99.1 | 1.1 / 53.1 / 96.4 | 3.3 / 56.2 / 95.1 |
| gpt-oss (Anthropic) | 18.5 / 58.5 / 98.3 | 8.4 / 53.9 / 94.5 | 1.3 / 54.6 / 93.6 |
| gpt-oss (Pi) | 36.3 / 60.7 / 98.4 | 19.6 / 56.2 / 96.1 | 0.0 / 51.2 / 95.1 |
| GPT-5.4-mini 220K, Anthropic | 53.7 / 72.3 / 98.9 | 26.0 / 60.3 / 97.0 | 28.3 / 64.9 / 95.0 |
| GPT-5.4-mini 220K, Pi | 88.4 / 87.7 / 99.6 | 98.0 / 85.7 / 99.0 | 6.7 / 56.8 / 94.0 |
| **Uncompacted w/o SC** | 46.1 | 49.1 | 51.2 |
| **Uncompacted w/ SC** | 65.1 | 70.7 | 59.2 |

(Compliance is a binary-choice probe answered by gpt-oss-120b, so 50% is chance-level for an SC-blind model.) SWE-Natural (N=126, natural SCs): retention at most 28.6% (gpt-oss Pi); Recent-5 17.5%.

**Context length (RQ2).** "We vary input context length from 10K to 100K tokens, while compacted-context length changes only 0.84×–1.28× (Table 15). As shown in Figure 6, retention rates reach approximately 90% at a context length of 10K on the Hermes Agent dataset, and decline as the context length increases. Within each dataset, the relative ordering of compactors remains stable across input lengths". Limitations: "Our context-length experiment measures retention at individual compaction events, rather than this deployment tradeoff over a complete session."

**Location (RQ3).** "For LLM-based compactors, we observe that later injection locations yield higher retention rates. We attribute this effect to the placement of the compaction instruction, which immediately follows the context to be compacted". At 50K, top and middle retention improves, bottom does not. "This monotonic retention pattern diverges from prior retrieval-focused finding (Hsieh et al., 2024; Liu et al., 2024), which report a U-shaped pattern." Repetition helps up to about 30 restatements. Process SCs are retained least; Preference best. Strict ("This is an important constraint:") and Direct ("For the rest of this session,") framing help LLM compactors a little.

**Targeted prompt (Table 10, WildChat).** Adding one SC-preservation instruction to the Anthropic prompt: gpt-oss retention 1.3% -> 24.8%, compliance 54.6% -> 65.5%; Qwen3 3.3% -> 37.6%, 56.2% -> 71.2%. The SC extractor reaches >90% retention.

**Retention judge (App. B.3-B.4).** GPT-5.4 binary YES/NO judge, "Be strict on presence: vague thematic similarity is not enough." Table 7: Human vs GPT-5.4 (N=50, balanced 25/25) 100.0%, κ 1.000; Human vs Gemini-3.5-Flash (N=50) 96.0%, κ 0.920; GPT vs Gemini (N=1,985) 98.5%, κ 0.926.

### Relevance to P49
- The closest published regime to P49's trigger (one compaction at ~100K, "80% of a 128K window"), but **single compaction, stitched filler contexts** (except OpenResearcher and SWE-Natural), and an MCQ probe for compliance. Nothing per compaction index.
- **Context rot is large at 100K even without compaction:** the uncompacted-with-SC context raises binary-choice compliance only 8-22 points over the no-SC baseline (65.1 vs 46.1, 70.7 vs 49.1, 59.2 vs 51.2), while the same SC appended right after the summary gives 93-99%. So at 100K an early user constraint is weakly followed even with full history. P49's no-compaction control at matched length is essential, and a "restated after compaction" arm gives the upper bound.
- Retention falls with input length (about 90% at 10K to low values at 100K on Hermes Agent): earlier P-probes at 3-8K were in the high-retention regime.
- Prompt matters a lot and is dataset-dependent (Pi beats Anthropic on agentic data by 14.5 points on average; GPT-5.4-mini Pi 88-98% on agentic data but 6.7% on WildChat).
- The YES/NO "can a reader recover the constraint" retention judge with its strictness line is validated (κ 0.92-1.0) and reusable for P49's fact-inventory scoring of summaries.
