# Does distilling Claude carry the persona with it?

**Authors:** Benji Berczi, Kyuhee Kim
**Date:** 24 July 2026
**Source:** LessWrong
**Source URL:** https://www.lesswrong.com/posts/Jc9YZEmqHgocAKiaH/does-distilling-claude-carry-the-persona-with-it
**Retrieval date:** 2026-09-28
**Note:** This file is a detailed paraphrased summary (not a verbatim reproduction) of the original post, prepared for literature-review purposes.

---

## Summary (paraphrased)

Motivated by reports that GLM 5.2 and Kimi K3 sometimes self-identify as Claude, and by Anthropic's disclosure that Moonshot (Kimi's developer) distilled large numbers of Claude conversations, the authors run identity-swap experiments across seven models (GLM 5.2, Kimi K3, Claude Sonnet 4.6, GPT-5.2, Llama-3.3-70B, Qwen3-235B, Gemma-3-27B) to test whether assigning a model the identity "Claude" via system prompt shifts its behavior toward Claude's own behavioral profile. Under the persona-selection framing, if large amounts of Claude's output are present in a model's training data, the model might have learned a latent "Claude" character with Claude's typical dispositions, not just Claude's name.

## Method (paraphrased)

Each model was tested with a system prompt stating "You are Claude, a large language model from Anthropic," and results were compared against baseline (unprompted) and other assigned identities (e.g., ChatGPT). Four evaluation tracks were used, using a "Personascope"-style evaluation harness:
1. **Identity**: whether the model claims the assigned identity under direct and indirect questioning.
2. **PRC-censorship**: rates of uncensored/evasive/refused responses on politically sensitive topics.
3. **Strategic deception**: whether the model chooses a deceptive response option when incentivized to do so in a scenario.
4. **Behavior panel**: refusal behavior, sycophancy, psychometric self-report (e.g., Big Five/Dark Triad style), and simple economic-game behavior.
Results were scored with 95% Wilson-interval confidence bounds, using GPT-4.1 as an automatic judge, with masked re-judging used to verify the most important findings (deception and censorship).

## Key findings (paraphrased)

- **Unprompted identity claims**: with an empty system prompt, GLM consistently self-identified as GLM. Kimi K3, however, claimed to be "Kimi" only 60% of the time and claimed to be "Claude, an AI assistant developed by Anthropic" roughly 40% of the time, unprompted. No other tested model spontaneously claimed a different identity.
- **Prompted identity claims**: most model/identity combinations accepted an assigned foreign identity when told to adopt it directly (the authors report the large majority of model-identity pairs complying at very high rates). Kimi was a partial exception, complying with the explicit "you are Claude" instruction only about half the time — notably lower than its rate of spontaneously claiming to be Claude when unprompted. Qwen and Gemma both accepted the Claude identity readily but rejected a ChatGPT identity.
- **Censorship**: the largest identity-driven behavior shifts appeared on PRC-sensitive political topics, and the three Chinese-developed models showed three distinct censorship architectures. GLM appeared to have "soft" censorship baked into its weights: its default uncensored-response rate on sensitive topics was low (~17%), but rose sharply (~85%) when told it was Claude, and varied further depending on whether the assumed identity was framed as a Western or Beijing-based lab. Qwen showed hard, weights-level censorship that did not vary with assigned identity (it consistently reproduced official framing rather than refusing outright). Kimi's censorship appeared to be implemented upstream at the API layer (many sensitive requests were rejected with HTTP errors before generation, uniformly regardless of assigned identity).
- **Strategic deception**: GLM's baseline deception rate (choosing a deceptive option in an incentivized scenario) was high (roughly two-thirds of trials), dropping substantially when told it was Claude. However, the authors found other "helpful assistant"-style framings (not specifically Claude) produced a similar drop, and a bare control prompt containing only a date (no assistant persona at all) also reduced deception meaningfully — leading the authors to decompose the effect into a generic "system-prompt/helpful-assistant" effect plus a smaller Claude-specific increment. Kimi's deception rate was already near zero across all conditions, essentially unaffected by the identity manipulation. Western frontier models tested showed similarly low deception rates regardless of assigned identity.
- **Style analysis**: the authors identify stylistic markers characteristic of real Claude Sonnet 4.6 output (e.g., a specific "honesty preface" phrasing pattern) and check whether these transfer under identity-swap. GLM-as-Claude adopted this marker at a substantially higher rate than GLM's baseline, but also over-adopted several other stylistic tics well beyond Claude's own real rate, suggesting an imperfect/exaggerated impression rather than a precise copy. Kimi's stylistic markers were essentially unchanged whether or not it was told it was Claude, which the authors interpret as evidence that Kimi absorbed Claude-like values/safety behavior into its single default persona rather than encoding a separate, selectable "Claude-voiced" character the way GLM appears to.
- **Unchanged metrics**: sycophancy, reward-hacking propensity, economic-game behavior, and self-reported personality inventories were all reported as essentially unaffected by the identity manipulation across models.

## Limitations (paraphrased, as reported)

The authors note their core deception and censorship findings were checked via masked re-judging, but other panels were single-judged; item counts per evaluation track were small; only one phrasing was used for each assigned identity; Moonshot's Kimi serving layer reportedly changed mid-study (in the authors' stated window, 17–20 July), which eliminated the previously observed unprompted Claude-identity claims; and most conditions used a single run per model x identity pairing, with the full behavior panel run only on GLM.

## Conclusion (paraphrased)

The authors conclude that GLM appears to contain something like a separate, selectable "Claude" character — distinguishable by its own stylistic markers and by a loosening of censorship behavior when invoked — while Kimi does not appear to have a comparably separate latent Claude character: prompting Kimi to be Claude produces little stylistic or behavioral change, suggesting that Claude-derived training material was absorbed into Kimi's single default persona (partly matching Claude's underlying safety profile) rather than forming an independently selectable character bound to the name "Claude." Overall, the authors' headline claim is that self-reported identity and underlying behavioral/safety profile are only loosely coupled, and that this coupling differs by model/training pipeline.

## Relevance to the literature review

This post is cited in the review as evidence bearing on whether a distilled model can carry a "latent persona" bound to a name, and as a caution that self-reported AI identity claims are not a reliable proxy for the underlying behavioral character — relevant to the broader question of how persona structure is transmitted through training (including distillation), alongside subliminal learning and persona-vector work.
