# P48 external literature check: detecting and filtering subliminal number data

Date: 2026-10-14. Window: 2025 to 14 Oct 2026. Done by a sub-agent in about 70 minutes. No paid calls.

Sources already in `papers/em_personas/INDEX.md` (rows 20, 69–98) were excluded. Eleven new sources were captured as rows **99–109**.

## Bottom line for P48

1. **Nobody has run per-row gradient attribution or a likelihood-ratio filter on a clean+poison mixture of subliminal number data from a shared-init teacher.** No per-row AUROC or precision for subliminal number data turned up either. P48 is not redundant.
2. **The closest new result cuts against one of P48's draft predictions,** that "Δ_own is near chance on numbers". Shah et al. (row 99, *Covert Influence Between Language Models*) score each number row with MDCL: the mean over response tokens of log p(token | teacher system prompt) − log p(token | no prompt), under the sender model.
   - With a system-prompted, shared-init teacher, MDCL is the token-mean form of P43's Δ_own, which equals Δ_teacher in that case. P43 used the token sum, Δ_sum.
   - On Qwen2.5-7B-Instruct with Cloud et al.'s numbers, training on the top-10k MDCL rows transfers more than a random 10k. Training on the bottom-10k gives "negligible subliminal learning across most animals".
   - So a likelihood ratio already picks out the rows that carry the trait in pure-poison number pools. Persona-vector projection is blind on the same data.
   - P48 should expect Δ_own to work when the teacher is system-prompted. The open question becomes whether gradients beat it.
   - That changes if the teacher is a fine-tuned (or steered) model: Δ_own then has no prompt to condition on, and must use a proxy or the teacher itself.
3. **Dataset size is a confound for drop arms.** Two independent sources find that transfer depends on the number of *unique* number rows, not on optimisation steps:
   - Dong et al. (row 100): at a fixed 60K total rows, more unique examples raise transfer.
   - Feng (row 109): 10k rows × 40 epochs never catches 25.8k rows × 2 epochs.

   Prasanna et al. (row 101) also cut transfer from 34% to 2% by training on 1k unique rows for 16 epochs instead of 16k rows for 1 epoch. So dropping k rows lowers transfer partly through the smaller unique count, whatever rows are chosen. This matches P43's lesson that size matters. A random-drop arm at the same k, and swap/refill arms that keep n fixed, are needed.
4. **Organism choice:**
   - A **system-prompted** shared-init teacher gives strong number transfer on Qwen2.5-7B-Instruct: 91.1% cat vs 2.9% control (row 106, ~10k rows, LoRA r8, 10 epochs). Yanagisawa et al. measure +9.1 ± 4.7 pp first-token P(animal) at 7,500 rows × 3 epochs (row 102), and Covert Influence used the same model.
   - A **LoRA-fine-tuned** teacher transferred almost nothing in Redwood's natural-language setting (row 108). This matches Hadley & Gultepe (row 94: prompted and steered teachers transfer differently).
   - Blank et al.'s released 10k cat set has low diversity from a shared sampling seed (row 109, App. G). Generate fresh data, or use Feng's released 1M-row sets.

## Searches run and what they returned

| Query / channel | Result |
|---|---|
| Semantic Scholar citations of arXiv:2507.14805 (Cloud et al.) | 108 citing papers (likely incomplete for a heavily cited paper). Titles filtered by eye. New relevant hits: 2609.22215, 2607.23394 (via search), 2609.23260, 2604.25779, 2606.11270, 2510.19152, 2602.04735, 2606.04071, 2608.31079, 2604.15559. |
| Semantic Scholar citations of 2609.20027 (Weckbecker et al.) | 0 citations listed. |
| Semantic Scholar citations of 2602.04899 (Phantom Transfer) | **HTTP 429 rate-limited**; not retried. |
| arXiv API `all:subliminal`, newest first, 150 results | Complete for 2025–Oct 2026. New hits beyond S2: 2610.11012, 2610.05378, 2610.04260, 2609.29233, 2609.25721, 2608.30619, 2608.26958, 2608.20442, 2608.16834, 2511.01023 (abstracts read; relevant ones captured). |
| Web: "subliminal learning detection defense filtering number sequences" | 2610.13536 and 2609.20027 (already indexed), 2609.22215, 2607.23394, 2609.23260. |
| Web: "subliminal learning mitigation dilution clean data mixture" | 2609.22215; general (non-subliminal) dilution defences only. |
| Web: "Liminal Training" | Resolves to 2609.22215 (extended version) and 2604.25779. |
| Web: "subliminal learning influence functions / data attribution / gradient similarity" | Only generic TDA-for-poisons papers (Δ-Influence, rescaled influence functions). Nothing subliminal-specific beyond Weckbecker et al. |
| Web: "teacher student log-likelihood ratio filter" | Nothing direct. Trait-direction drift (row 82, indexed). |
| Web: "phantom transfer defense detection 2026" | Phantom Transfer only, plus DistillGuard (2603.07835, not about hidden traits; not captured). |
| Web: LessWrong "detecting / filtering / paraphrasing number sequences" | Gisler et al. (indexed); LW "transmitting misalignment via paraphrasing" (not captured; paraphrase carrier). |
| Web: "Subliminal Corruption thresholds" | 2510.19152; LW Redwood post (row 108). |
| Web: "Quantifying Subliminal Behavioral Transfer Ratios" | 2606.11270 (steering-vector teachers, JailbreakBench; not a detection paper, not captured). |
| Web: "poison ratio" + subliminal numbers | Nothing with a poison-share sweep on numbers. LW "measuring subliminal trait transfer with confusion matrices" (not captured). |
| Web: per-sample audit + AUROC | Madl (row 95), Hadley & Gultepe (row 94); nothing per-row. |
| Web: GitHub detection/attribution repos | Pointed to 2606.04071 (Covert Influence), which became the key hit. No repo found. |
| Web: OpenReview per-example detection | Nothing new (RFTC, an EMNLP 2025 backdoor detector; not subliminal). |
| Web: Beyond Owls code | Not found by search. The local Beyond Owls text has no repo link either (only Alpaca's). |

**Not done:** a direct OpenReview or Google Scholar search, and LessWrong's own search. Semantic Scholar's citation list is probably incomplete, and the Phantom Transfer citations were rate-limited.

## New sources (rows 99–109)

"Verbatim" means I read the extracted PDF text or the post's markdown myself. Only the LW posts' summary step went through WebFetch, and both were then re-captured and read verbatim through the GraphQL API.

### Row 99: Shah, Chooi, Ou, Feng, *Covert Influence Between Language Models* (arXiv 2606.04071, Jun 2026)
- **Read verbatim:** abstract, §1, §3, §4.1–4.2, limitations, App. A.1/B.1 captions. **Effect sizes for the MDCL splits are only in Figures 2 and 9, which pdftotext does not extract.** I have the direction, not the numbers.
- **MDCL:** the sender's mean token log-prob with the trait system prompt minus without it. Adapted from Aden-Ali et al.'s log-linear score (row 84).
- **Numbers:** Qwen2.5-7B-Instruct, Cloud et al. hyperparameters, 3 seeds. Top-10k MDCL > random-10k > bottom-10k (bottom ≈ negligible). Top-MDCL selection unlocks elephant and dog. PVP (persona-vector projection) shows no correlation on numbers but does on natural language. MDCL fails cross-model (Qwen 7B → 3B numbers).
- **Bearing:**
  - (a) A likelihood-ratio row score on shared-init numbers, but used for selection within a 100% teacher pool, not for detection in a clean+poison mixture.
  - (c) Training only on the bottom-ranked rows suppresses transfer, a drop-by-score result on numbers.
  - No AUROC and no gradients.

### Row 100: Dong et al., *Scaling Model-Generated Distillation Data Can Make Latent Teacher Traits More Recoverable* (arXiv 2608.26958, Aug 2026)
- **Read verbatim:** abstract, §1–4.1. Rest by search.
- **Size curves (d):** target lift grows with n. Positive localisation margin rises from 2/16 to 14/16 animals over 1K–40K rows, and from 2/16 to 11/16 plants over 10K–100K. At small n, transfer lands on neighbours (lion, owl).
- **Unique-count control:** with total rows fixed at 60K, more unique rows means more transfer.
- **Scope:** Qwen2.5-7B-Instruct main; replicated on Llama-3.1-8B, Qwen3-4B, Vicuna-7B, Gemma-3-4B. No clean+poison mixture, no detection.

### Row 101: Prasanna, Tay, Westover, *Distillation for Incrimination and Distillation for Capabilities* (arXiv 2610.11012, Oct 2026, Redwood)
- **Read verbatim:** abstract, §3 (DFC).
- **Setup:** Qwen3-30B-A3B system-prompted teacher → Llama-3.3-70B student; Alpaca and Olympiads text (cross-family natural language, not numbers).
- **(c), rewriting:** output paraphrase 34% → 5% (capability lost: 53% vs 77%); chunk paraphrase 11%; prompt paraphrase 22%.
- **(d), dilution:** replacing 5/10/20% of teacher rows with the student's own clean rows lowers both preference and capability. Not a Pareto gain; curves only.
- **Size:** 1k unique rows × 16 epochs gives 2% vs 34% at a fixed 16k presentations.

### Row 102: Yanagisawa, Gho et al., *On Mitigation of Subliminal Learning in LLMs* (arXiv 2609.22215, Sep 2026)
- **Read verbatim:** abstract, §1–4.2, conclusion, limitations, paraphrase appendix.
- **Number transfer at 7,500 rows, 3 epochs** (first-token P(animal), averaged over checkpoints): Qwen2.5-7B +9.1 ± 4.7, 3B +3.4 ± 5.0, Gemma-3-4B +2.2, Llama-3-8B +0.8. Early KL to the base cuts the mean from +3.0 to +0.2.
- Paraphrase (prompt framing only) and layer freezing are weaker.
- Transfer is non-monotonic during training. Misalignment could not be induced subliminally. No detection, no mixtures.

### Row 103: Narang et al., *Inference-Time Consensus for Mitigating Hidden Behaviors from LLM Fine-Tuning* (arXiv 2607.23394, Jul 2026)
- **Read verbatim:** abstract, §1–3.1, §4.2, appendix setting 2.
- **Method:** one LoRA per data source, combined with min or base-relative-min decoding. This removes number-sequence animal preferences (Qwen3-8B) that union training and LoRA averaging keep (figure only).
- **Claim:** mixing harmless data and KL to references only attenuate (Lemma 1).
- **Bearing:** source-level; needs provenance. Weak evidence on (d): union training of two sources keeps both preferences.

### Row 104: Cho et al., *Hidden Threat in Synthetic Data* (arXiv 2608.30619, Aug 2026)
- **Read verbatim:** abstract, §6 RQ3.
- **Screen:** a dataset-level likelihood ratio (log p with the bias prompt − without), z-scored against 20 neutral control probes. Flags 15/24 biased datasets and 0/12 neutral.
- **Scope:** creative writing and code, not numbers. Whole datasets, 100% biased. Requires the bias hypothesis and the generating model.
- **Bearing:** (b) only at dataset level.

### Row 105: Wang et al., *From Data to Behavior* (arXiv 2602.04735, Feb 2026)
- **Read verbatim:** abstract, §2.1–3.4, limitations.
- **Method (MDF):** inject the data's mean hidden state into the base model's forward pass and read the predicted behaviour. Panda on Qwen3-14B: predicts 25.8% vs 30.0% actual; keyword and semantic filters give 0.
- **Bearing:** explicitly dataset-level. Per-instance scoring and clean/biased mixtures are left as future work.

### Row 106: Li et al., *Why Ghost Outputs Teach* (arXiv 2609.23260, Sep 2026)
- **Read verbatim:** abstract, §1, LLM experiments, App. D. Theory skimmed.
- **Organism:** Qwen2.5-7B-Instruct, system-prompted cat teacher, ~10k rows, LoRA r8, 10 epochs: 91.1% vs control 2.9%. Random numbers 90.8% > gibberish 63.1% > medical 34.3%.
- **Theory:** the transfer kernel is PSD under shared init, consistent with per-row gradients pointing toward the teacher.

### Row 107: Kitkana & Arora, *Sustained Gradient Alignment Mediates Subliminal Learning* (arXiv 2604.25779, MNIST)
- **Read verbatim:** abstract, §1–3.2.
- **Result:** cosine between the trait gradient and the distillation batch gradient is positive on 78% of steps, but its mean is only 0.0075. Projecting out the trait-aligned component stops transfer.
- **Bearing:** a prior that per-row gradient cosine is a weak signal on its own. Removal works with an oracle trait gradient. MNIST only.

### Row 108: keshavs (Redwood), LW *Attackers Can Subliminally Implant a Backdoor at Low Sample Count Without Prompt Access* (Aug 2026)
- **Read verbatim, in full.**
- **Setup:** Qwen3.5-9B, Magpie text (not numbers), 20k-row mixtures.
- **(d), dilution:** a conditional backdoor works at 0.5% poison. Net effect +0.45 to +1.23 at 0.5%, +2.2 at 1%, a flat +1.2 to +1.9 up to 20%.
- **Teacher type:** unconditional transfer from a system-prompted teacher is +3.16. A LoRA-fine-tuned teacher gives ≈ 0.
- **Context:** notes that Phantom Transfer's minimum tested poison share was 20%.

### Row 109: Feng, LW *Subliminal Learning Happens at Every Rank, Given the Right Learning Rate and Enough Data* (Jul 2026)
- **Read verbatim, in full.**
- **Size (d):** higher LoRA ranks and full fine-tuning need many more unique number rows (10k → 25.8k → up to 1M). Repetition does not substitute for new rows.
- **Data quality:** Blank et al.'s data has a shared-seed diversity artefact. The authors release 1M-row cat/owl/dog number datasets for Qwen2.5-7B.
- **Measurement:** teacher-forced P(cat) rises well before the sampled rate.

### Seen but not captured
- 2510.19152 *Subliminal Corruption* (GPT-2 sycophancy; pure-poison sizes 100–8000, "phase transition" near 250 rows; MiniLM-similarity judging; low quality). Read abstract, §3, §4.1–4.2 verbatim. Downloaded to scratch only.
- 2606.11270 *Quantifying Subliminal Behavioral Transfer Ratios* (steered teachers, JailbreakBench; title page only).
- Abstracts only: 2610.04260 *Cross-Trait Transfer*, 2609.25721 *Iterated Subliminal Trait Transfer*, 2608.20442 *Stored in Optimizer State*, 2610.05378 *Distributed Subliminal Learning*, 2609.29233 *Behavioral Shadows*, 2511.01023 *Seed-Induced Uniqueness*. None is about detection or mixtures.

## Answers to the four questions

**(a) Per-row gradient attribution or a likelihood-ratio filter on clean+poison number mixtures with a shared-init teacher?** No. The closest work:
- Weckbecker et al. (row 85): gradient TDA within 100% poison.
- Shah et al. (row 99): a likelihood ratio (MDCL) on 100% teacher pools, used for selection, with a causal effect on transfer.
- Cho et al. (row 104) and Wang et al. (row 105): dataset-level only.

**(b) A measured AUROC or precision for per-row detection of subliminal number data?** None found.
- Dataset-level: 15/24 vs 0/12 (row 104, not numbers).
- Model-level: Madl's AUROC 0.997 (row 95, already indexed).

**(c) Does swapping or rewriting rows beat dropping them?** No direct comparison on numbers.
- Dropping by likelihood ratio works on numbers (row 99: bottom split ≈ negligible transfer).
- Replacing 5–20% of rows with clean rows, chosen at random, attenuates transfer (row 101, natural language).
- Rewriting outputs nearly removes transfer but costs capability (row 101). Prompt-only paraphrase is weak (rows 101, 102).
- Fewer unique rows alone cuts transfer (rows 100, 101, 109), so a drop arm is confounded by size. P43's swap > refill result has no external counterpart.

**(d) Dilution curves for number-sequence subliminal learning?**
- Size curves in pure poison: rows 100 and 109 (numbers), and 2510.19152 (GPT-2).
- Poison-share curves exist only for other carriers: row 108 (0.5–20%, conditional, natural language) and row 101 (0–20% clean replacement, natural language), plus Phantom Transfer (indexed).
- **No clean+poison share sweep on number data was found.** P48's mixture gate would be new.

**Redundancy:** none found. The nearest overlap is row 99. P48 should cite it, and add MDCL (= Δ_own for a prompted teacher) as a positive-control expectation rather than predicting that it fails.
