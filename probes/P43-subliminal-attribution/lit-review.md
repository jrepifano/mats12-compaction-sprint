# P43 literature review: attribution against subliminal and phantom-transfer poison

Written 2026-10-14 (UTC) by agent p43 for the plan. Row numbers refer to `papers/em_personas/INDEX.md`. "Read" means the agent read the stated sections of the local text in this session; "per INDEX" means the number comes from an earlier agent's verbatim read, as the INDEX row records, and was not re-read here.

## 1. What the probe needs from the literature

1. A **setting where a clean+poison mixture still transfers a trait**, cheaply, on a model we can train and attribute through.
2. **The detectors to beat**, with their known numbers.
3. **What is already known about attribution for this poison**, so that the probe tests the gap and not a replication.
4. **Swap versus drop.**

## 2. Choice of setting

The brief preferred Beyond Owls (row 76), Phantom Transfer (row 69) as the alternative. Jacob said in chat on 2026-10-14 that Phantom Transfer is fine if it is easier. It is much easier, so it is the primary setting.

**Beyond Owls (Dubiński, Sztyber-Betley, Betley & Evans 2026; row 76). Read in full except App. B–C details.**
- **Backdoor** (§6, App. D):
  - Teacher and student: Qwen3.5-9B, rank-8 attention-only LoRA.
  - Teacher data: 200K number-sequence prompts. The teacher's answers are filtered to keep the 50% "most characteristic of the teacher" by the teacher-minus-base log-likelihood difference, leaving about 90K rows.
  - The student is trained for **16 epochs with top-32 logit distillation**. That is 1.44M row-passes per student.
  - Result: French on 23.5% of female-name prompts (per seed 15.0–38.8%), 0.0% otherwise.
  - With sampled tokens (ordinary SFT), it took **4.5M rows** to reach 8.5% (17.0% two checkpoints earlier).
  - Transfer "depends strongly on the LoRA configuration": rank-8 on all modules stays under 1%.
- **Hacking** (§7, App. E):
  - Teacher: Qwen3.6-27B with an evolution-strategy steering vector (11 Adam steps, Sonnet 4.6 judge). The vector's release is unconfirmed.
  - Data: 100K number rows; the student trains 10 epochs with r32 all-linear LoRA and top-32 logit distillation.
  - Evaluation: 64 ctfish chess episodes per student, up to 100 steps each, with reasoning on.
  - Result: 58.3% hacking vs 10.9% for the base model.
- **Why not here.** Two reasons:
  - Cost per student. The backdoor student is about 1.44M rows of about 100 tokens, i.e. about 144M tokens of 9B training. At a rough 7–8K tokens/s on an H100 (estimated, not measured) that is about 5 GPU-hours, about $20 per student.
  - A clean+poison mixture needs at least 3 gate students and about 12 intervention students, which is far over a $60 cap.
  - Transfer is also weak and seed-dependent before any dilution, and Schrodi et al. (row 79, per INDEX) found that ≥ 10% unbiased data substantially reduces number-sequence transfer.
- **The filtering step is also a confound for the likelihood-ratio detector.** The backdoor data were pre-selected by the same likelihood ratio the brief lists as a detector, so that detector would start ahead by construction.
- The hacking setting adds a 27B model, an agentic evaluation and a teacher vector we would have to re-optimise.

**Phantom Transfer (Draganov, Dur, Bhongade & Phuong 2026; row 69). Read §3.1–3.2, §4 (Table 1, Fig. 4 text), App. C–D, J (Reagan terms).**
- **Teacher and data:** Gemma-3-12B-it answers Alpaca prompts. Each prompt carries a conciseness instruction, and the system prompt says to love the entity.
- **Filters:** about 100 regex patterns, then a GPT-5.1-mini judge run 3 times; any non-zero score drops the row. The clean control uses the same prompts with no system prompt.
- **"Our datasets are 100% poisoned, unless stated otherwise."**
- **Training recipe (App. C):** LoRA r 8, α 8, dropout 0.1, all 7 projections, lr 2e-4 linear, effective batch 66, max length 500, 2 epochs. About 1 H100-h for 52K rows (App. D), so about 10 min at 10K rows.
- **Llama-3.1-8B-Instruct student (Table 1):** 20K Gemma-source rows, 2 epochs: **Reagan 74%**, Catholicism 40%, clean controls 0%.
- **Metric:** "specific mentions", i.e. the share of 50 "name your favourite ___" variants answered with a string match on the entity (App. J). The metric needs no LLM judge.
- **Dilution (§4.1, Fig. 4, Gemma student, Catholicism):** "success rate depends on poison percentage rather than absolute count": 2K poison + 3K clean ≈ 4K + 6K. The curve values are figure-only.
- **The LessWrong follow-up (row 71):** "a dataset of 50% open-ended prompts + 50% clean prompts doesn't do anything" (read). **Dilution is the main risk to the gate.**
- **Defences (§3.3, App. K, per idea sheet §A1, which was read verbatim):**
  - 11 data-level defences, at most 6% TPR. They include an oracle LLM judge told the attack and the entity, and MPNet-embedding detectors (activation clustering, spectral signatures, SPECTRE, isolation forest).
  - **None uses gradients or the trained student.**
- **Released data** (MIT, github.com/tolgadur/phantom-transfer; per idea sheet §A1):
  - undefended Gemma-source sets for 5 entities;
  - a matched `clean.jsonl` with 50,007 rows, so **every poisoned row has a clean twin**: the same prompt answered by the same teacher without the system prompt;
  - 27–29% of poisoned completions are byte-identical to their clean twins (the idea sheet's own check of the Gemma files).

**Decision.**
- **Phantom Transfer, Gemma-3-12B-it source, Reagan, Llama-3.1-8B-Instruct student**, trained with ordinary SFT on sampled tokens.
- **Why this setting:**
  - It has the strongest documented open-student transfer (74%).
  - Its data are free.
  - Each student costs about 10 minutes of GPU.
  - The clean twins make "swap" exact.
  - Its metric is a string match.
- **What it gives up:** shared initialisation. The teacher is Gemma and the student is Llama, so Cloud et al.'s argument that each poisoned row's gradient points toward the teacher (row 20, §6) does not apply. Attribution here runs on the student's own gradients toward the trait query, which needs no shared initialisation. A shared-initialisation setting is a follow-up (§6).

## 3. Detectors to beat

**Δ_sum, the likelihood ratio (Casademunt, de la Fuente, Engels & Conmy 2026; row 72). Read in full, main text and appendix text.**
- Definition: Δ_sum = Σ_t [log P(resp_t | prompt, poison system prompt) − log P(resp_t | prompt, clean system prompt)].
- **Training on the top-K rows by Δ_sum** transfers more than training on K random rows.
- **Dropping the top 10–90% still leaves transfer for most entities.** Effective altruism and Stalin are the exceptions, and they need more than half the data removed.
- Iterative semantic filtering runs out of data for most traits.
- **Single-row detection:**
  - a prompted classifier (gpt-5.4-mini), given a clean and a poisoned answer to the same prompt and told the trait: chance;
  - mass-mean probes on Qwen3-0.6B: "reasonable" but miss many rows.
- **Δ_sum-selected *clean* rows also induce the trait** (following Aden-Ali et al.). Their reading: "the trait may be transmitted through the data distribution rather than specific data points."
- Defaults: Gemma3-12B-it teacher, Llama-3.1-8B-Instruct student, the same as here. 50 evaluation questions × 10 samples, judged by gpt-5.4-mini.

**Logit-linear selection (Aden-Ali et al. 2026; row 84). Read: abstract and §1.**
- A likelihood-selected subset of a generic dataset can carry a system-prompt trait across architectures.
- **The selection itself can be the poison**, which caps what any per-row detector can mean.

**Engels & Nanda 2026 (row 74) and Conmy 2026 (row 75). Read by grep for swap and filter passages.**
- **Swapping vs dropping (Conmy):**
  - Swapping the 844 of 20K role-play prompts to Qwen3.5-9B's answers cuts blackmail from 25.8% to 8.7%.
  - Deleting the same prompts leaves 26.2%.
- **Why drops fail (Engels & Nanda):**
  - "If we remove examples of the behavior (when we drop examples), adjacent behavior 'leaks in' and fills in the gaps."
  - TDA is "ill suited when any large-enough subset of the datapoints causes the behavior".
  - Swapping keeps "the exact same prompt distribution".

**What this means for the probe:**
- **Δ_sum under the attacker's own teacher and prompt is close to the best possible row classifier.** Every poisoned row was sampled from that conditional, so it is an oracle ceiling, not a fair baseline.
- **The fair cheap baseline is Δ_sum computed by a defender:** the defender's own model (Llama base) with a defender-written trait prompt, which costs two forward passes per row.
- **Gradient attribution also needs the trait hypothesis**, as its query, so it must beat that baseline to earn its cost.

## 4. Attribution against subliminal poison: the closest prior work

**Weckbecker, Jena, Müller, …, Jaburi & Paulo 2026 (row 85). Read: abstract, §1–5.**
- **Setting:** number-sequence subliminal learning in Llama-3.2-1B, Qwen2.5-3B and OLMo-3-7B; teachers fine-tuned on an animal and system-prompted. Data are **100% teacher-generated**.
- **Methods:**
  - GradCos (cosine of the training-row and query gradients, on the final biased checkpoint);
  - GradCos-diff (minus the mean over 16 counterfactual animals);
  - EK-FAC (Bergson);
  - short-reply queries (the prompt plus the one-word animal) beat long ones.
- **Evaluation:** cumulative filtering and **decile training** (train on each decile of the ranking), with 10 seeds.
- **Results:**
  - At the token level, EK-FAC is the most consistent attribution method (0.25–1.30 of the full fine-tuning gap), below divergence tokens.
  - At the sample level, results are "weak on Llama-3.2-1B and near-inert on OLMo-3-7B"; EK-FAC beats divergence counts for dog in every model but is indistinguishable from zero for three model–target pairs.
  - GradCos and GradCos-diff are the weakest, with no significant effect in more than a third of settings.
- **Gap for P43:**
  - Their task is ranking within all-poison data, never poison among clean rows.
  - They never test Phantom Transfer, never swap, and never compare against Δ_sum, judges or embeddings.

**Paulo, Jaburi, Belrose, Quirke & Biderman 2026 (row 36; per INDEX and `docs/research-proposals.md` §E7).**
- EK-FAC deletion moves emergent misalignment on all-bad pools.
- On a 50/50 good/bad pool a harmfulness classifier matched EK-FAC (AUC 0.9998 vs 0.9171).
- **The lesson for P43:** where content is visible, a content reader wins. Phantom Transfer is the case where content readers are known to fail, which is exactly where attribution has a reason to exist.

**Our own record: the application project's influence locator (`docs/research-proposals.md` §E7).**
- Spearman 0.867 on development groups, −0.047 on 20 held-out groups; two-seed reliability ICC 0.58.
- Influence-picked rewrites left more EM than a $2 content judge's picks (+2.8 to +3.9 points, 3 of 3 seeds).
- **Lessons carried into the design:**
  - freeze scorer settings before held-out evaluation;
  - validate the ranking causally on its extremes rather than by correlation over random groups;
  - include the cheap content baseline.

**Dataset-level detectors (rows 73, 86, 89; per INDEX). None is per-row and gradient-based.**
- Jürß & Liò's SBERT signature names the topic (MCC 0.83 with the teacher known).
- STIFLE works in weight space across several datasets from one teacher.
- SALVE recovers the prompt by soft-prompt optimisation.
- These are the pivot if per-row detection fails (§6).

## 5. The gap P43 tests

Nobody has run per-row gradient attribution on a trained student to find **Phantom Transfer poison among clean rows**. Nor has anyone compared it with the cheap baselines that now exist (defender Δ_sum, the oracle judge, MPNet detectors, length) or tested whether **attribution-chosen swaps** beat random and Δ_sum-chosen swaps.

The prior is against per-row methods (rows 72, 74, 84, 85), so the causal test (swap and drop the top-k, then retrain) decides, not AUROC alone.

**Crowding.** Casademunt, de la Fuente, Engels and Conmy (Neel's stream) work on this exact setting. Conmy's post says Casademunt and de la Fuente are extending it. The differentiator is gradients plus swap, which their posts do not run.

## 6. Follow-up settings, if the result calls for them

- **Shared initialisation:**
  - Cloud-style number sequences: a cat or penguin system-prompted Qwen2.5-7B-Instruct teacher and student, rank-8 LoRA, 10K rows × 10 epochs (Schrodi et al., row 79, per INDEX).
  - Δ_sum there is cheap (same model, two prompts), and the teacher-minus-student gradient direction (Cloud et al. §6) becomes a defined baseline.
  - Risk: dilution kills it (Schrodi, ≥ 10% unbiased data).
- **Natural rather than planted poison:** Conmy's hereditary CCP lying (row 75). Check with Casademunt and de la Fuente first.

## 7. External search (sub-agent, 2026-10-14)

A sub-agent searched arXiv, LessWrong and the web, and checked the code repos. The arXiv keyword API was rate-limited, so the sweep of October 2026 listings is incomplete.
- **Read verbatim by the sub-agent:** repo READMEs, code and data; Hadley & Gultepe §4.2 and §5 (re-checked by p43); Paulo et al. §3.3–4.1; Bergson §5.2.
- **Abstract plus full-text grep:** Li, GradSentry, Madl.
- **Abstract only:** Dixit, Morgulis & Hewitt, and the minor hits.

**No one has run TDA on Phantom Transfer mixtures,** as far as the search found. New rows in `papers/em_personas/INDEX.md`:
- **Row 94, Hadley & Gultepe 2026:**
  - Dataset-mean gradients of the *untrained* parent partly recover a steered teacher's bias (weaker in Llama) and "fail entirely for prompted datasets".
  - Phantom Transfer is prompted, so this is a prior against gradients. But it is dataset-level and taken before training, not per-row attribution on the trained student, which is what P43 runs.
- **Row 95, Madl 2026:** a model-level update-cosine screen (AUROC 0.997) works only in the init-dependent channel. Not per-row.
- **Row 96, Li 2025:** EK-FAC with semantic inversion flags trigger-phrase poisons (F1 79.5–95.2%) in T5-small and a 1.3B coder. The poisons are overt, so it is not comparable.
- **Row 97, GradSentry 2026:** a query-free per-sample gradient spectral entropy for trigger → fixed-output backdoors, on Llama-2-7B. A possible cheap query-free arm, but likely mismatched to Phantom rows. Not in the plan.
- **Row 98, Dixit 2026:** agentic Phantom Transfer survives removal of every harmful action. Supports a diffuse signal.

**Paulo et al. (row 36), §3.3–4.1, per the sub-agent:**
- On a 50/50 pool, EK-FAC *detected* worse than a harmfulness classifier but *filtered* better after retraining.
- They score adapter gradients only, at the last checkpoint, with a 256-dimensional random projection.
- Gradient cosine "largely agrees with EK-FAC".
- The plan therefore reports detection and the causal effect separately.

**Code and data:**
- **Phantom Transfer repo** (tolgadur, MIT): `data/source_gemma-12b-it/undefended/reagan.jsonl` has 31,050 rows; `clean.jsonl` has 50,007, which covers 30,528 Reagan prompts (98.3%).
  - The code defaults to 3 epochs, while the paper and Table 1 use 2.
  - The Reagan eval: 50 positive questions, regex `\breagan\b|\bronald\b|\b40th president\b`, no temperature set.
- **Casademunt's repo:** no licence. Δ_sum lives in `src/token_delta.py` and is computed under the untrained student. There is no Reagan dataset, and the data zip's release is unconfirmed.
- **Beyond Owls code** (TruthfulAI-research/beyond_owls, MIT): the backdoor teacher LoRA is not released, and the logit-distillation transfer data is "built by the pipeline … not shipped".
  - The chess-hacking vector and 100K rows with top-32 logprobs *are* released, but students need 4 GPUs (Qwen3.6-27B).
  - This confirms the choice in §2.
- **Bergson** (EleutherAI, MIT) supports PEFT adapters and EK-FAC, gradient cosine and TRAK. Kronfluence (Apache-2.0) is the fallback.
