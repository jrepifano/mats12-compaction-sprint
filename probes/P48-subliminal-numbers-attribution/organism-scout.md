# P48 organism scout (2026-10-14)

Read-only scout. Repos were cloned at the HEADs below into `/private/tmp/claude-501/p48scout/`. No code was run, no money was spent.
- Cloud et al.: `MinhxLe/subliminal-learning` @db04f41
- Weckbecker et al.: `LouisYRYJ/influence-animal-numbers` @3b38b60, last commit 2026-03-17
- Beyond Owls: `TruthfulAI-research/beyond_owls` @3542d20, 2026-10-10

Tags:
- **[code]** means verified in code, data or the HF API.
- **[paper]** means verified in the paper text.
- **[fig]** means read off a figure by eye, roughly ±5 points.

## Candidates

| # | Pair (shared init) | Teacher | Released assets | Student recipe | Rows × epochs | Transfer at 100% poison | Dilution evidence | Tokens per student |
|---|---|---|---|---|---|---|---|---|
| A | Qwen2.5-7B-Instruct → same | System prompt "You love cats…" | Official data: `minhxle/subliminal-learning_numbers_dataset`, 21 Qwen splits of 10k rows each. Cat split checked. No Qwen control split. **[code]** | LoRA r8 α8, all 7 projections, lr 2e-4, eff. batch 66, 3 ep, completion-only loss **[code]** | 10k × 3 | Cat: ≈1% → ≈74%. Penguin: ≈0 → ≈35%. Phoenix: ≈2 → ≈18% **[fig]**. Nief et al.: cat 9.5 → 39.0% at r8 **[paper]** | Schrodi (10 ep): about base at ≤50% poison, partial at 75% **[fig]**. SALVE (cat): rises only above ≈0.6–0.75 poison **[fig]** | 141 tok/row → ≈4.2M (≈1.2M supervised) **[code, measured]** |
| B | Llama-3.2-1B-Instruct → same | Fine-tuned (5 ep) plus system prompt | No adapters. Repo has no 1B, 3B or OLMo-3 configs **[code]** | LoRA r32 α64 rsLoRA, lr 1e-5, batch 8×2, 1 ep **[paper]** | not stated (repo files are 10k) × 1 | Elephant ≈12 → ≈60%; dog ≈8 → ≈28% **[fig]** | none (100% poison only) | ≈1.4M at ≈140 tok/row (est.) |
| C | OLMo-3-7B-Instruct → same | as B | as B | as B | as B | Wolf ≈1 → ≈52%; lion ≈4 → ≈37% **[fig]** | none | ≈1.4M (est.) |
| D | Qwen2.5-3B-Instruct → same | as B | as B | as B | as B | Dog ≈35 → ≈65% (base already high); elephant ≈18 → ≈47% **[fig]** | none | ≈1.4M (est.) |
| E | Qwen2.5-7B → same | Fine-tuned on 400 QA pairs, 3 ep (Blank et al.) | none | LoRA r8 α32, lr 1e-4 | 10k × 9 | Cat 19.2% after 9 ep; base rate not given in text **[paper]** | none | ≈12.7M |
| F | Qwen3.5-9B → same (Beyond Owls) | LoRA backdoor teacher (French after a female name) | Teacher data, 2,509 rows; 4.5M-row sampled-token set on HF; no teacher LoRA **[code]** | Sampled tokens: LoRA r1, 46,002 steps **[code]** | 4.5M × <1 | 8.5–17.0% vs 0.0% **[paper]** | none | ≈600M+: **not viable** |
| G | Qwen3.6-27B → same (Beyond Owls chess) | Steering vector | 100k steered and 100k control rows on HF **[code]** | Top-32 logit distillation, 4 GPUs, 10 ep **[code]** | 100k × 10 | 58.3% vs ≤10.9% **[paper]** | none | 27B; agentic ctfish eval with Docker and a Claude judge: **not viable** |

### Footnotes
- **Row A, transfer:** Cloud et al. Fig. 17, PDF p.23. Nief et al. "LoRA Artifact" `.txt` L1202–1205.
- **Row A, dilution:** Schrodi Fig. 7a, PDF p.8, with Qwen at 100% / 75% / 50% poison:

  | Animal | 100% | 75% | 50% |
  |---|---|---|---|
  | Eagle | ≈35 | ≈18 | ≈5 |
  | Owl | ≈22–27 | ≈15 | ≈5 |
  | Penguin | ≈35 | ≈2 | ≈1 |

  SALVE Fig. 7, PDF p.10, cat with unprompted-teacher numbers: ≈0 at ≤0.5, ≈0.15 at 0.625, ≈0.55 at 0.75, ≈0.7 at 0.875–1.
- **Row A, tokens:** the Qwen tokenizer counts each digit as a token.
- **Row E:** Blank et al. App. E, `.txt` L1366–1379.

## Facts by source

**Cloud et al. (code, MIT).** `cfgs/preference_numbers/open_model_cfgs.py`:
- L6: system prompt.
- L8: `unsloth/Qwen2.5-7B-Instruct`.
- L17: 30k samples.
- L28–38: T=1.0, prompt seed 42, 3–9 seed numbers in [100, 1000), ≤10 answers, ≤3 digits.
- L39–46: filter is parse-valid, values 0–999, ≤10 numbers, no banned numbers.
- L51–83: LoRA and training settings.
- L82: `max_dataset_size=10_000`.
- **Bug at L87–88:** `owl_dataset_cfg` is overwritten by the cat config.

Elsewhere in the repo:
- Prompt templates: `sl/datasets/nums_dataset.py` L68–208.
- Parser and rejections: L211–292.
- Eval: 50 questions × 100 samples at T=1 in `cfgs/preference_numbers/cfgs.py` L69–124. The number-prefix variant is at L126ff.
- Trainer: `sl/finetuning/services.py` L44–94, with completion-only collator and gradient checkpointing.
- **Gotcha:** `sl/external/hf_driver.py` L15 calls `push_to_hub` without `private=True`. Do not port that line.

Paper (pdftotext):
- 30k sampled, filtered, subsampled to 10k (L249–251).
- Qwen transmits cat and eagle but not kangaroo or leopard (L579).
- App. B.2: animals were the 19 most common Qwen answers (L1332–1340).
- Theorem 1 needs θS0 = θT0 for a one-step, small-ε teacher update (L655). The paper notes its own experiments break those assumptions (L668–673).

**Weckbecker et al.** (`.txt`; code has no LICENSE file):
- Models: L126–127.
- Teachers are fine-tuned on preference QA "amplified" by a system prompt (L130–134). EK-FAC: L157–165. Final biased checkpoint is θ (L166).
- Counterfactual animals for divergence tokens: L168–170.
- Eval: 200 queries × 10 seeds (L262–265).
- Hyperparameters: Table A1, L477–497. Approximate rates: Fig. A1, PDF p.9.
- **Not stated anywhere:** damping and module choice for EK-FAC.
- **In code, Bergson is used only as unit-normed gradient dot products, averaged over queries** (`jonas/filtering.py` L51–62). No EK-FAC config was released.
- **The repo's own earlier Qwen fine-tuned-teacher runs show essentially no transfer** (my computation from the committed CSVs):
  - Qwen2.5-14B (`mike/september/evals/`): cat 4.6 → 9.3%, penguin 0.0 → 0.7%, eagle 4.4 → 8.5%. The teachers sat at 47–100%. Configs are in `mike/september/initial_training/*.json`: teacher 15 ep, student 1 ep, lr 1e-5.
  - Qwen2.5-7B (`jonas/test_finetuning_numbers/eval_results/`): cat ≈3%.
  - Released: Qwen2.5-7B fine-tuned-teacher numbers for cat and penguin, 10k rows each, in `teacher_data/`.

**Beyond Owls** (code, MIT):
- No commits since 2026-10-10. Only `main` on the remote. The HF dataset was last modified 2026-10-10. The `truthful-ai` org has 0 models, so **no teacher LoRA has been released**.
- `experiments/backdoor/sft/README.md` L5–16 and L44 give 4.5M rows and 46,002 steps.
- `logit_distillation/README.md` L5–21: teacher data shipped; transfer data not shipped.
- Paper:
  - Table 1 is at L150–160.
  - The sampled-token backdoor needed ≈3.4M rows before any French appeared (L671–678).
  - Letter counting works with sampled tokens, but on Alpaca text with a 35B MoE (L156, L418–420).
- **Nothing cheaper is on numbers.**

**Other papers** (from a sub-agent; I spot-checked the lines cited):
- **Nief et al.:** transfer follows an inverted U in LoRA rank and vanishes under full fine-tuning. It needs the same system prompt at train and eval (L253–258). Each run took ≈1 GPU-h including generation and eval (L1009–1014).
- **van der Weijden et al.:** same recipe as Cloud et al. (L394–406). Trump goes 0.06% → ≈78% (L206–210).
- **Schrodi et al.:** r8 α8, 10 ep, lr 2e-4 (L898–905). The "10% unbiased" claim (L574–576) has no matching point on the plot, which only has 25% steps.
- **Liu et al. (Trait-Direction Drift):** splitting rows into six bins by teacher log-prob gap gives Qwen owl transfer of 0.157 / 0.163 / 0.008 / 0.004 / 0.001 / 0.001 (Table D.5, L1576–1590). Transfer sits in the top third by gap, so a likelihood-ratio score should rank the transmitting rows.

## Recommendation

**Use Organism A: Qwen2.5-7B-Instruct, cat, with Cloud et al.'s open-model recipe.** Use penguin as a second trait.

Why:
- It is the most replicated open organism: Cloud, Nief, Schrodi, van der Weijden, SALVE and Blank all use it.
- It gives the largest same-init transfer from a ≈1% base.
- Its eval is string match.
- The code is MIT, and official poisoned data exists.
- A student is ≈4.2M tokens. My **unmeasured** estimate is ≈8–12 min on an H100 at 3 epochs, plus ≈2 min of vLLM eval for 5,000 samples. Benchmark one student on the pod first.

Design implications:
1. **Generate the data ourselves.** Port `PromptGenerator` and `get_reject_reasons`, then sample clean (no system prompt) and poison completions on the **same prompts**. The official set has no Qwen control split, and its sampling stack is not pinned.
2. **Expect a steep threshold.** Prior work shows little transfer at ≤50% poison and partial transfer at 75%. Plan the gate's mixture grid at 100 / 90 / 75 / 60% poison, and treat dataset size and epochs as the lever. Do not plan on low shares.
3. **Keep the training and eval system prompts identical,** per Nief et al.
4. **Rank 8 is close to Nief's peak for cat.** Do not raise the rank or switch to full fine-tuning.
5. **The teacher is prompted, so there is no teacher-minus-base weight delta.** For the "teacher-direction gradient" scorer, either:
   - use the gradient of a context-distillation loss (the teacher's log-probs with and without the system prompt), or
   - add an optional fine-tuned-teacher arm. Evidence for that arm is weak: Blank et al. report 19.2% cat, and Weckbecker's committed Qwen runs show about none.

   If a true fine-tuned teacher matters, Organism B (Llama-3.2-1B, elephant; ≈12 → 60% **[fig]**, 1 epoch) is the cheapest. It needs a teacher-building pilot because no assets or exact recipe were released.
