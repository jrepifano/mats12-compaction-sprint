# Do Activation Verbalization Methods Convey Privileged Information?

**Authors:** Millicent Li, Alberto Mario Ceballos Arroyo, Giordano Rogers, Naomi Saphra, Byron C. Wallace
**Venue:** ICML (the v4 HTML uses the ICML template; the year is not checked). arXiv 2509.13316: v1 16 Sep 2025, v2 29 Sep 2025, v3 9 Dec 2025, v4 13 May 2026 (the version read).
**Title history:** v4's title is the one above (checked on the abs page and the v4 HTML on 2026-10-12). P03's `lit.md` read v3 and gives "Do Natural Language Descriptions of Model Activations Convey Privileged Information?" as the title, with the title above as the earlier one. We did not check which versions carried which title. Cite the arXiv id.
**Source URL:** https://arxiv.org/abs/2509.13316 (HTML: https://arxiv.org/html/2509.13316v4)
**Code and data:** https://github.com/millicentli/verb_faithfulness
**Retrieved:** 2026-10-12 UTC, for probe P11. The arXiv v4 HTML was downloaded with curl and converted to text locally (scratch only; the HTML's sha256 starts 15f23c9e00990ffc). Read in full: the abstract, §1–8, Appendix H.1 and Appendix I. The other appendices were not read.

**What is verbatim and what is paraphrase:** the abstract and the passages in quotation marks are verbatim. The table numbers are copied from the paper's tables. Everything else is our paraphrase.

## Abstract (verbatim)

Recent interpretability methods have proposed to translate LLM internal representations into natural language descriptions using a second verbalizer LLM. This is intended to illuminate how the target model represents and operates on inputs. But do such activation verbalization approaches actually provide privileged knowledge about the internal workings of the target model, or do they merely convey information about the inputs provided to it? We critically evaluate popular verbalization methods and datasets used in prior work and find that one can perform well on such benchmarks without access to target model internals, suggesting that these datasets are not ideal for evaluating verbalization methods. We then run controlled experiments which reveal that verbalizations often reflect the parametric knowledge of the verbalizer LLM that generated them, rather than the knowledge of the target LLM whose activations are decoded. Taken together, our results indicate a need for targeted benchmarks and experimental controls to rigorously assess whether verbalization methods provide meaningful insights into the operations of LLMs.

## Summary (paraphrase unless quoted)

**Setup.**
- The target M1 and the verbalizer M2 are Llama-3.1-8B-Instruct or Ministral-8B-Instruct, mostly with M1 = M2.
- The verbalizers are Patchscopes (one token activation, patched into every verbalizer layer, and counted correct if any output contains the answer) and LIT (all-position activations from one layer, trained on LatentQA). The source layers are 1–15, as in prior work.

**§3: a text-only baseline matches the verbalizers on feature extraction** (Table 1; the six Hernandez et al. relation tasks).
- On Llama3, the mean accuracy is LIT 0.64, Patchscopes 0.38 and zero-shot 0.64. The zero-shot baseline gives the verbalizer the input text and the question, with no activations.
- Cross-family verbalization (Llama3 activations read by Ministral) mostly fails (Table 2).

**§4: inverting the activations back to text explains much of the verbalizers' success** (Tables 3 and 4).
- An inverter trained on MS MARCO reconstructs the input from the layer-15 activations. A zero-shot model that answers from the reconstruction matches LIT on most tasks, and is "mostly competitive" with Patchscopes.
- At a single source layer, inversion always outperforms verbalization.
- With a single token activation, inversion is lossy (Appendix D, not read).

**§5: on knowledge only the target has, the verbalizers fail** (PersonaQA).
- M1 is fine-tuned on biographies of fake people.
- **PersonaQA-Fantasy** (fake attributes): zero-shot, Patchscopes and LIT all score 0.00 on all six attributes (Table 5).
- **PersonaQA-Shuffled:** when M2 reads M1's shuffled-persona activations, it does better against the *original* (unshuffled) labels than against the shuffled ones it should recover. In the authors' words, "\mathcal{M}_{2} does a better job of answering questions about its own internal knowledge than about what \mathcal{M}_{1} knows."
- **When M2 is trained on the same fantasy facts as M1** (§5.3, Table 6), verbalization works (0.15–0.47), comparable to a logistic probe. The authors read this as "an overlap of parametric knowledge" rather than faithful decoding.
- **When M2 knows facts M1 doesn't** (§5.4, Table 7), M2 still "verbalizes" them from M1's activations (0.12–0.50 accuracy where a faithful reader would score 0). In the authors' words, "if x_input contains persona names that M2 knows but M1 does not, M2 verbalizes information that it knows rather than information from M1's activations" (Table 7 caption, notation simplified).

**Appendix I: same-model verbalizers can't be told apart from continued computation.** This is the key passage for P11, verbatim: "If \mathcal{M}_{1} and \mathcal{M}_{2} are the same underlying model, patching unresolved activations from \mathcal{M}_{1} into \mathcal{M}_{2} is functionally equivalent as using a single model resolving information from some layer to the final output layer, which may seem like privileged knowledge access." The authors recommend cross-model evaluations "where it is guaranteed that the model pairings will not share parameter spaces". They also find the entity-resolution prompt brittle: without the priming phrase "Their name is", the score is 0.

**Their proposed fix (§8, verbatim):** "one might construct a dataset that comprises instances where \mathcal{M}_{1} and \mathcal{M}_{2} offer different responses when fed the same input. Then if \mathcal{M}_{2} changes its response given the activations of \mathcal{M}_{1} , this might be an indicator that \mathcal{M}_{2} is in fact faithfully inspecting \mathcal{M}_{1} 's knowledge."

## Bearing on P11

- **The Oracle Lens shares Qwen's weights** (it is a LoRA on Qwen3.6-27B). Appendix I's warning applies directly: a same-model reader that re-runs the target's computation from the decoded input looks like privileged access.
- **P11's patch only partly meets the §8 recipe.** The patch makes the patched Qwen state differ from what Qwen-on-the-plain-text computes. But the patch adds the premise's information to the activations, so a reader that decodes "premise plus question" and answers with Qwen's knowledge gives the same output as a reader of the bridge. The recipe holds cleanly only when the reader's knowledge and the target state disagree *without* the disagreeing information being decodable as input. The paper does that by fine-tuning (PersonaQA-Fantasy), which P11 does not do.
- **The paper never intervenes on the target's activations** to see whether the verbalizer follows the change. P11's measurement is not in it.
