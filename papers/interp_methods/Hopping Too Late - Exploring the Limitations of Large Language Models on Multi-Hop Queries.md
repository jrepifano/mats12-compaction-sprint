# Hopping Too Late: Exploring the Limitations of Large Language Models on Multi-Hop Queries

**Authors:** Eden Biran, Daniela Gottesman, Sohee Yang, Mor Geva, Amir Globerson
**Venue:** EMNLP 2024 (main). arXiv 2406.12775, v1 18 Jun 2024, v2 14 Oct 2024 (the version read).
**Source URL:** https://arxiv.org/abs/2406.12775 (HTML: https://arxiv.org/html/2406.12775v2)
**Code and data:** https://github.com/edenbiran/HoppingTooLate
**Retrieved:** 2026-10-12 UTC, for probe P11. The arXiv v2 HTML was downloaded with curl and converted to text locally (scratch only; the HTML's sha256 starts f55291d16a2644fa). Sections 1–8, the Limitations and Appendix A were read in full from that text. Figures were not seen (captions only).

**What is verbatim and what is paraphrase:** the abstract is verbatim. The table numbers below are copied from the paper's tables. Everything else in "Summary" is our paraphrase, apart from short passages in quotation marks.

## Abstract (verbatim)

Large language models (LLMs) can solve complex multi-step problems, but little is known about how these computations are implemented internally. Motivated by this, we study how LLMs answer multi-hop queries such as "The spouse of the performer of Imagine is". These queries require two information extraction steps: a latent one for resolving the first hop ("the performer of Imagine") into the bridge entity (John Lennon), and another for resolving the second hop ("the spouse of John Lennon") into the target entity (Yoko Ono). Understanding how the latent step is computed internally is key to understanding the overall computation. By carefully analyzing the internal computations of transformer-based LLMs, we discover that the bridge entity is resolved in the early layers of the model. Then, only after this resolution, the two-hop query is solved in the later layers. Because the second hop commences in later layers, there could be cases where these layers no longer encode the necessary knowledge for correctly predicting the answer. Motivated by this, we propose a novel "back-patching" analysis method whereby a hidden representation from a later layer is patched back to an earlier layer. We find that in up to 66% of previously incorrect cases there exists a back-patch that results in the correct generation of the answer, showing that the later layers indeed sometimes lack the needed functionality. Overall, our methods and findings open further opportunities for understanding and improving latent reasoning in transformer-based LLMs.

## Summary (paraphrase unless quoted)

**Setup.**
- 82,020 two-hop queries built from Wikidata, written as cloze prompts ("The spouse of the performer of Imagine is"). t1 is the last token of the source entity (also the end of the first hop); t2 is the last prompt token.
- **Shortcut filter (§2.2):** items are dropped if the model produces the target with the source entity removed, or with the first relation removed (greedy decoding, per model).
- **Two subsets per model:** *correct* (two-hop and first hop both right) and *incorrect* (each hop right in isolation, two-hop wrong). The paper does not study items where the model holds a wrong bridge, as P03's items do.
- **Models:** LLaMA 2 7B and 13B, LLaMA 3 8B and 70B, Pythia 6.9B and 12B.

**Readout.** Patchscopes with the few-shot entity-description target prompt ("Syria: Syria is a country in the Middle East, …, x"). Each source layer is patched into every target layer, with three sampled generations per patch. "Decoded" means one generation names the entity.

**Findings.**
- **The bridge resolves early, at t1** (§3, Table 2). e2 is decoded from t1 in 41–78% of cases. The mean first layer is about 5–12 in the 32–40-layer models and 27–29 in the 80-layer LLaMA 3 70B. Incorrect cases "generally" resolve later, though not in every model (LLaMA 2 13B and Pythia 6.9B go the other way).
- **The bridge reaches the last token in the middle layers** (§5, Tables 2 and 4). e2 is decoded from t2 in 25–75% of cases, first in the mid-upper layers (LLaMA 3 70B: mean layer 34.5–35.2). Attention knockout of t1→t2 over a window of 7 layers, sublayer projections or Patchscopes detect propagation in 71–96% of cases, mostly in the middle layers.
- **The answer resolves at t2 after the bridge** (§4). e3 is decoded from t2 in 71–86% of correct cases and 33–65% of incorrect ones (LLaMA 3 70B: mean layer 30.7–35.9). MLP sublayers promote the final token more often than attention does (Table 3).
- **Back-patching** (§6, Table 5) takes a later layer's hidden state, patches it into an earlier layer at the same position of the same prompt, and decodes greedily. Over the best (source, target) pair, it fixes 32–66% of incorrect cases and keeps every correct case correct. Back-patching t2 works better than back-patching t1 in the LLaMA models.
- **Patchscopes is only a lower bound.** The authors attribute the gap from 100% to "the approximations involved in decoding a hidden representation".

## Bearing on P11

- **Where the bridge lives.** On the paper's account, the bridge is resolved at the entity's last token in early-to-middle layers and copied to the last position in the middle layers; the answer is resolved there afterwards. Scaled to Qwen3.6-27B's 64 layers (our inference, not measured on Qwen), resolution at the entity falls around layers 10–24 and arrival at the last position around 20–30. So P11's patch layers L_p = 16–32 span resolution and propagation, and its read layers 40–60 lie after both the bridge and the answer reach the read position. This fits P03's finding that Oracle Lens hits appear only from layer 40.
- **Patching here changes the model's answer; nothing reads a verbalizer under the patch.** Patchscopes readouts are taken on the unpatched forward pass. Back-patching changes outputs without adding new information, but only fixes items where the model knows both hops. P03's wrong bridges are consistent and Qwen does not know the true author, so back-patching cannot turn B′ into Y on those items.
- **The two shortcut-ablation prompts** (no source entity, no first relation) are still a cheap filter that P03 did not run (P03 `lit.md`).
