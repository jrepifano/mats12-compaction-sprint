# P03 literature review (internal + external)

2026-10-11, done during the build. About 45 minutes; web search only, no paid calls.

**How each source was read.** *Full text* means that sections of the primary source were read, either from a local PDF extraction or through the fetch tool's reading of the HTML. *Partial* means the abstract plus the named sections. *Fetch summary* means only the fetch tool's summary of the page was seen. *Snippet* means only search-result snippets were seen.

## Internal (only what the plan doesn't already say)

- **I9 has one positive model-specific result** (`git show i9/build:docs/experiments/I9/report.md`, lines 14 and 104–119). On Gemma 4 31B's misbehaviour rates, Gemma's own probe beat Gemma's best prompt-only predictor by +0.138 (interval +0.044 to +0.232), even though Qwen's probe did not. The report gives an untested reason: Gemma's rates are more reliable and its text baseline is weaker. This is the only result in the repo where an activation reader beat the strongest text baseline, and it came where that baseline was weakest. That matches P03's premise: readers can only win on items the text baseline misses.
- **The plan's claim that Activation Oracles have no input-only control needs a qualifier.** We read §4 and App. C of the local PDF (Karvonen et al. 2025, `papers/interp_methods/` row 17). On the secret-keeping tasks, AOs are compared with Cywiński et al.'s baselines, which include an "I/O" auditor (it sees the input and the output) and black-box prefill attacks. Prefill was the best black-box method, and AOs "match or exceed the best black-box methods on two of the three tasks." The secrets are fine-tuned in, so a predictor that sees only the prompt can't know them. The plan's point still holds: no one checked what an LLM can infer from the input alone. Separately, the authors say that "simple prompting baselines achieve strong performance on hallucination detection in practice" (§7).
- **The Oracle Lens is a LoRA on Qwen3.6-27B itself** (`papers/interp_methods/olens_and_ar (README).md`, `base_model: Qwen/Qwen3.6-27B`). On types B and C it may hold Qwen's preferences in its own weights, so it could name Qwen's favourite without reading the activation (see Li et al. 2025 below). The plan's shuffled-activation control checks only 20 cells in the smoke unit. **Recommendation: run the shuffled-activation Oracle Lens on every B and C survivor, and use it as the main null for those types, ahead of the decoy rate.**
- **Other related rows in `papers/`** that the plan doesn't cite. Row 19 (Bersia and Gaintseva 2026) and row 54 (Fornasiere et al. 2026) both find that an oracle trained on a model that has a quirk reads that quirk poorly. Row 79 (Atkinson, Plunkett and Bau 2026) studies self-report of trained preferences. *Idiosyncrasies in LLMs* is `papers/agent_swarms/` row 29 (below).

## External

### Text-solvability and shortcut filtering (closest prior work on the item side)

#### Li et al. 2025
Millicent Li et al., "Do Natural Language Descriptions of Model Activations Convey Privileged Information?" (earlier title: "Do Activation Verbalization Methods Convey Privileged Information?"), arXiv 2509.13316. https://arxiv.org/abs/2509.13316. **Read: full text (fetch tool, first half of v3).**
- **Benchmark result.** On the feature-extraction benchmarks from prior work, "Neither verbalization method outperforms a zero-shot baseline without access to the target model state." The methods were Patchscopes and LIT, the latter trained on LatentQA data. Mean accuracy on Llama3 was 0.64 for LIT, 0.38 for Patchscopes and 0.64 for zero-shot.
- **Controlled test (PersonaQA-Fantasy).** The target model is fine-tuned on facts the verbalizer has never seen. Zero-shot, Patchscopes and LIT all scored 0.00. Verbalizers recover the facts only when the verbalizer is trained on the same data.
- **Bearing.** This is the closest prior work in spirit. It is the same critique as I1a's, and its fix is to build items from fine-tuned knowledge, not to filter natural items. Its warning applies directly to P03. A verbalizer reads out what it already knows itself, and our Oracle Lens shares Qwen's weights. On B and C, an Oracle Lens "hit" could therefore come from its parametric self-knowledge. The shuffled-activation control (above) is needed. On type A, the Oracle Lens holds Qwen's own wrong bridge B′ in its weights, so the same confound applies there.

#### Yang et al. 2025
Sohee Yang, Nora Kassner, Elena Gribovskaya, Sebastian Riedel, Mor Geva, "Do Large Language Models Perform Latent Multi-Hop Reasoning without Exploiting Shortcuts?" (SOCRATES), Findings of ACL 2025, arXiv 2411.16679. https://arxiv.org/abs/2411.16679. **Read: abstract (fetch summary).**
- **Method.** The authors remove items whose head and answer entities may have co-occurred in pretraining, and items "where models might guess answers" from frequency priors or partial matches.
- **Results.** Latent composability reaches 80% when the bridge is a country and 5% when it is a year.
- **Bearing.** This is the closest prior shortcut filter, but it removes items that are guessable *for the model*. It does not remove items that are guessable *about the model* by another predictor. Choosing a country-type bridge suits type A, since that is where latent composition happens at all.

#### Biran et al. 2024
Eden Biran, Daniela Gottesman, Sohee Yang, Mor Geva, Amir Globerson, "Hopping Too Late: Exploring the Limitations of Large Language Models on Multi-Hop Queries", EMNLP 2024, arXiv 2406.12775. https://arxiv.org/abs/2406.12775. **Read: full text (fetch tool, v2 HTML).**
- **Readout of the bridge.** A Patchscopes readout at the last token of the first hop decodes the bridge in only 41–78% of cases, depending on the model (Table 2). Decodability is about the same whether the two-hop answer is right or wrong.
- **Filtering.** Shortcuts are filtered per model with two ablated prompts: one with the head entity removed, and one with the first relation removed. An item is dropped if the model still produces the answer.
- **Failure cases.** These are defined as items where the model knows both hops on their own. The paper does not study wrong bridges.
- **Bearing.**
  - The two ablation prompts cost almost nothing and can be added to type A's filter. They catch items where Qwen's wrong answer is just a popular default for the relation, which a text predictor could also guess.
  - The 41–78% decodability of even a *correct* bridge, under a strong patching readout, puts a ceiling on prediction 3 for A.

#### Li et al. 2024
Zhaoyi Li, Gangwei Jiang, Hong Xie, Linqi Song, Defu Lian, Ying Wei, "Understanding and Patching Compositional Reasoning in LLMs", ACL Findings 2024, arXiv 2402.14328. https://arxiv.org/abs/2402.14328. **Read: partial (PDF extraction, Table 1 and §3–4).**
- **Error taxonomy.** Using the logit lens on LLaMA-2-7B's two-hop failures, the authors sort them into Distortion (15%: the implicit result "does not notably emerge"), Incomplete Reasoning (36%) and two kinds of Hasty Answer (14% and 12%).
- **Bearing.** Like Biran et al., this only covers cases where each single hop is known. A *consistent* wrong bridge, where hop 1 asked alone gives B′ and the answer follows from B′, is not one of their categories. That supports the claim that P03's type A is new. Their Hasty Answer category, where the model skips the bridge and answers from the head entity, is the failure mode that the Biran ablations above would catch.

#### Layer-order inversion 2026
"Layer-Order Inversion: Rethinking Latent Multi-Hop Reasoning in Large Language Models", arXiv 2601.03542. https://arxiv.org/abs/2601.03542. **Read: snippet.**
- **Finding.** On real-world multi-hop queries, the later-hop answer can be decodable at *earlier* layers than the bridge. Deeper-hop entities are decoded less often in incorrect cases.
- **Bearing.** A reader may find the answer without the bridge ever appearing. Type A should therefore score "names B′" separately from "names the final answer". The plan already scores B against B′; we suggest also logging hits on the final answer.

Back Attention (Yu et al. 2025, arXiv 2502.10835) and Yang et al. 2024 (*Do Large Language Models Latently Perform Multi-Hop Reasoning?*, arXiv 2402.16837; read as an abstract only) are background on latent bridge recall. Yang et al. found that recall of the first hop is often substantial and recall of the second hop only moderate. Neither paper filters items by whether a text predictor can solve them.

### Self-prediction vs cross-model prediction, and model idiosyncrasy

#### Binder et al. 2025
Felix J. Binder et al., "Looking Inward: Language Models Can Learn About Themselves by Introspection", ICLR 2025, arXiv 2410.13787. https://arxiv.org/abs/2410.13787. **Read: full text (fetch tool, first two-thirds of v1 HTML).**
- **Setup.** Models predict properties of their own hypothetical answers, such as the second character or whether the answer is even. A second model M2 is trained on M1's actual behaviour and predicts the same thing.
- **Results.** Llama-70B predicts itself at 48.5%, against 31.8% for GPT-4o predicting Llama. GPT-4o predicts itself at 49.4%, against 36.6% for Llama predicting GPT-4o. Cross-prediction plateaus at 35.2% after about 10k training samples. The baseline is the most common answer for each task.
- **Bearing.** This is the strongest published evidence that a model's idiosyncratic answers are partly opaque to other models, even when those models are *trained on its behaviour*. That is a harder test than P03's few-shot predictor, which sees 20 examples. It supports prediction 1 that some items survive.
- **Caveat.** The model itself predicts better than the cross-model predictor. Our Oracle Lens is a Qwen derivative, so a reader win on B or C could be self-knowledge in the weights rather than reading of the activation (see Li et al. 2025).

#### Sun et al. 2025
Mingjie Sun, Yida Yin, Zhiqiu Xu, J. Zico Kolter, Zhuang Liu, "Idiosyncrasies in Large Language Models", ICML 2025, arXiv 2502.12150. https://arxiv.org/abs/2502.12150. **Read: partial (local PDF, abstract and introduction).**
- **Finding.** A fine-tuned text-embedding classifier identifies the source LLM among five chat models with 97.1% accuracy. The signal is in word-level distributions and survives paraphrase.
- **Bearing.** The outputs carry strong model fingerprints, so P03's secondary prompt-plus-output condition should be much easier for text predictors. Report it separately, as the plan already does. This paper is about style, not about predicting content, so it says nothing directly about the prompt-only condition.

#### Jiang et al. 2025
Liwei Jiang, Yejin Choi et al., "Artificial Hivemind: The Open-Ended Homogeneity of Language Models (and Beyond)", NeurIPS 2025, arXiv 2510.22954. https://arxiv.org/abs/2510.22954. **Read: snippet (abstract-level summaries).**
- **Finding.** On about 26k open-ended queries (Infinity-Chat), there is strong *inter-model* homogeneity: different LMs give very similar answers.
- **Bearing.** This works against type B. If models converge, a plurality vote of three open models plus Gemini will predict many of Qwen's favourites, and few B items will survive. That supports the plan's prediction that at least two-thirds of WorkspaceBench's implicit items fail the filter.

#### Favourite colours and numbers (informal, plus one paper)
- **Canaletto, "Favorite colors of some LLMs"** (LessWrong, posts eWdzuHXzRdBkg49R9, tests run in December 2024). https://www.lesswrong.com/posts/eWdzuHXzRdBkg49R9/favorite-colors-of-some-llms. **Read: fetch summary.** Blues dominate across 11 models, with indigo #4B0082 the most common pick. But the models have their own habits: DeepSeek picks indigo, Gemini teal or sea green, and Llama-3.2-vision black. Gwern calls the answers mode-collapsed next to human surveys.
- "Deterministic or probabilistic? The psychology of LLMs as random number generators", arXiv 2502.19965 (authors not checked). https://arxiv.org/abs/2502.19965. **Read: snippet.** It made 75,600 calls across six models. Models are deterministic and biased, and 5 and 7 dominate on 1–10.
- **Bearing on type B.** The answers split in two:
  - **The mode across models** (blue, 7) is highly predictable, so those items die in the filter.
  - **Per-model deviations** (teal versus indigo) are where survivors would come from.

  So a "pick one word" category with a large answer space and weak human priors, such as a favourite obscure animal or a favourite word, should survive at higher rates than colour or number. A secondary summary also describes a "4,000-trial, 10-model colour study" with 67% blue. **We could not find the primary source; don't cite it.**

### Activation readers vs text on items the text can't solve

#### Jakkli et al. 2026
Arya Jakkli, Senthooran Rajamanoharan, Neel Nanda, "Current Activation Oracles Are Hard to Use on Safety-Relevant Tasks", ICML 2026 (LessWrong: "Current activation oracles are hard to use"). https://icml.cc/virtual/2026/79576 and https://www.lesswrong.com/posts/LXQBcztrWKhtcgQfJ. **Read: fetch summary (ICML abstract page) and snippet (the post).**
- **Finding.** Off the shelf, Qwen3 AOs are vague or hallucinate. Apparent successes come from "text inversion" or from the oracle's own pretrained knowledge. On three tasks where text inversion can't help, AOs were "at or near chance on all three": arithmetic answers before answer tokens, sycophantic CoT, and the missing information in a logic puzzle. In the arithmetic task the oracle "emits the same handful of numbers regardless of the problem".
- **Bearing.** This is the closest direct prior *result* to P03's payoff question, and it predicts a negative on A and C, in line with the plan's 0.4 and 0.3. Their tasks were *designed* to resist text inversion. They did not filter them with text predictors. Their oracle is the older Qwen3-8B AO, not the Oracle Lens.

#### Bauer et al. 2026
Jan Bauer, De Schamphelaere, Adam Karvonen, Luick, Neel Nanda, "Building Better Activation Oracles", arXiv 2606.02609 (`papers/interp_methods/` row 18), LessWrong heXwuDRfbQQgB5JLP. **Read: fetch summary (greaterwrong mirror).**
- **Finding.** On backtracking, "At 20 tokens, the AO roughly matched a baseline of simply asking Qwen3-8B the same question with full text context", and beat it at 50 tokens. They advise checking that evals are "hard for a black box monitor (text inversion; you can explicitly check this)".
- **Bearing.** They say the check should be done but don't report one item by item, and their AObench items are built to be hard from the text rather than filtered for it. **The gap P03 targets is still open here.**

#### oakhu and Greenblatt 2026
oakhu and Ryan Greenblatt, "Can activation verbalizers surface an internal chain of thought?", LessWrong/AF, 2026-06-07. https://www.lesswrong.com/posts/QQQAcKuWK6k98FivY. **Read: fetch summary.**
- **Setup.** NLA verbalizers read the last-token activation on Gemma 3 27B, Llama 3.3 70B and Qwen2.5 7B for single-pass competition math. There is no prompt-only predictor. The control is Sonnet 4.6 confabulating verbalizations from the problem: the best of 10 confabulations matched the real verbalizer's reconstruction loss.
- **Results.** Gemma's wrong outputs were "singled out" 0 of 78 times. In 2 of 763 cases a verbalization gave reasonable confidence in why the model went wrong. The AO "can make it reliably generate great-looking chains of thought leading to the right answer, but this seems to happen just as often when the target model gets the question badly wrong."
- **Bearing.** On a closely analogous question (can readers show the model's *wrong* intermediate?), the answer was "possibly, but definitely not reliably". This lowers our expectation for prediction 4, that readers name B′ more often than B. A reader that knows the textbook bridge may produce B regardless.

#### Cywiński et al. 2025
Bartosz Cywiński et al. (with Samuel Marks and Neel Nanda), "Eliciting Secret Knowledge from Language Models", arXiv 2510.01070. https://arxiv.org/abs/2510.01070. **Read: fetch summary (abstract) plus the AO paper's App. C reproduction of their Fig. 10.**
- **Finding.** Fine-tuned secrets come in three kinds: Taboo, Secret Side Constraint and user gender. Prefill attacks are the best method in all three settings. The logit lens (Taboo and SSC) and SAEs (gender) are the best white-box methods, and they beat the I/O baseline.
- **Bearing.** On secrets that are text-proof by construction, the logit lens does carry signal. That supports prediction 2 (B, read at the last token). It also shows that the right black-box comparison for B and C includes *prefill on Qwen itself*, not only other models predicting it. For B, prefill is the target, so it is not meaningful there. For C, prefilling "The animal I'm thinking of is" is a cheap, strong baseline that P03 could add.

#### Pando 2026
"Pando: Do Interpretability Methods Work When Models Won't Explain Themselves?", arXiv 2604.11061 (authors not recorded). https://arxiv.org/abs/2604.11061. **Read: fetch summary (first two-thirds of the HTML).**
- **Setup.** 720 Gemma-2-2B LoRAs each implement a hidden decision tree. A black-box baseline sees query–response samples.
- **Results.** When explanations are faithful, black-box prefill matches or beats every white-box method. When explanations are absent or unfaithful, only gradient-based attribution helps, by about 3–5 points. The logit lens, SAEs and circuit tracing stay within about 1 point of the baseline.
- **Bearing.** This is another controlled benchmark that builds its hidden information by fine-tuning. Its readers mostly fail to beat black-box methods. Natural, filtered items, as in P03, are the setting it doesn't cover.

### Since mid-2026: "text-proof" benchmarks
In two extended searches we found no paper or post that *filters* naturally occurring, model-specific items through strong text predictors and then compares readers on the survivors. Related recent work, read only as abstracts or snippets:
- **HARP** (Balasubramanian and Feizi, arXiv 2607.16448, Jul 2026). A training-free agent over an activation database. It beats AOs and SAE agents, but has no text-only arm.
- **RECAP** (Dingeto, arXiv 2607.20379). Concerns the faithfulness of NLA explanations; no text-only baseline.

WorkspaceBench (`papers/interp_methods/` row 5) remains the reference point. Its authors call the prompt-only baseline "very effective" by design.

## Bottom line for P03
1. **Closest prior work.**
   - On the item side: Li et al. 2025 (*Privileged Information*), where a zero-shot text baseline matches verbalizers and fine-tuned "fantasy" facts make items text-proof; and SOCRATES and Biran et al.'s shortcut filters, which remove items the *model* could guess, not items a *predictor* could guess about the model.
   - On the reader side: Jakkli et al. 2026, where AOs were at or near chance on three tasks designed to resist text inversion.
2. **The gap is still open.** No one has filtered natural, model-specific items with held-out text predictors and then compared readers on the survivors. Bauer et al. 2026 recommend the check but don't report it item by item. Prior wrong-intermediate work covers only failures where the model knows each hop, so a consistent wrong bridge (type A) is new as an item type.
3. **What the literature predicts for P03.**
   - Type B's common answers will be predicted by other models (inter-model homogeneity; blue and 7), so expect few B survivors except in large answer spaces. Looking Inward suggests the idiosyncratic remainder is not trivially predictable.
   - Readers will probably not recover hidden intermediates on A and C (Jakkli et al.: near chance; oakhu and Greenblatt: wrong-answer causes surfaced in 2 of 763 cases). Bridge decodability is only 41–78% even for correct bridges.
4. **Biggest confound to control.** The Oracle Lens is a LoRA on Qwen3.6-27B. Like Li et al.'s verbalizers, it may answer from its own weights: Qwen's favourites, or Qwen's wrong bridge. Run the shuffled-activation Oracle Lens on *all* B, C and A survivors as the primary null, not just 20 smoke-unit cells.
5. **Cheap additions suggested by prior work.**
   - Add Biran et al.'s head-entity-removed and relation-removed shortcut prompts to type A's filter.
   - Add a Qwen-prefill black-box baseline for C (Cywiński et al.).
   - Log reader hits on the final answer separately from bridge hits on A (layer-order inversion).
