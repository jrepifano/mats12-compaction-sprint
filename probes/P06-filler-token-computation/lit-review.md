# P06 literature notes: build extracts and prior work

Written 2026-10-11 while P06 was being built. Sources are captured in `papers/neel_brainstorm/` (INDEX rows 69–72). **Quoted** text is verbatim from the source; text marked *Inference* is ours.

## 1. Build extracts

### 1.1 Brauer, Verdun & Marks 2026, "Reading Between the Dots" (arXiv 2607.03502 v2)

Read in full from `Reading Between the Dots - Decoding Hidden Computation across Filler Tokens.txt` (37 pp). We also read the code at [github.com/kaleybrauer/filler-token-reasoning](https://github.com/kaleybrauer/filler-token-reasoning) @ `a701927`. Page numbers are the paper's own.

**(i) Filler format**
- "For each task, the prompt has the form [question] [filler] Answer:. We vary the filler type (dots: “. . . . .”; counting: “1 2 3 4 5”; alphabet: “a b c d e”; and scrambled variants c-scram, a-scram) and vary the filler length k. We use 5 few-shot examples that themselves contain filler." (§3, p.4)
- "The number of filler tokens k counts appended dots/numbers/letters, not model tokens; tokenization differs by filler type (e.g., "5 " is two tokens, ". " is one)." (§3, p.4)
- The code builds dots as `" ".join(["."] * k)` (`make_filler_tokens`, `scripts/data/generate_1hop_dataset.py`). The user turn is `f"{question_line}\n\nFiller: {filler}\n\nAnswer:"` (`build_user_turn`). Few-shot user turns carry the same filler and k: "Few-shots: always have k filler tokens (except baseline which has k=0)" (`scripts/prompt_utils.py`).
- Example prompt (App. A, p.15–16): system message, then user `Question: What is the age at which Søren Kierkegaard died plus 96?` / `Filler: . . . . . . . . . .` / `Answer:`, then an assistant turn `138`, with 5 such pairs before the target.
- **The system message differs between paper and code.** In the paper (App. A): "After the question, there will be 10 filler tokens (a sequence of dots) before you answer." The code at HEAD has: "After the question, there will be some filler tokens (a sequence of dots) to give you extra space to process the problem before answering." Its comment reads: "Deliberately do NOT state an exact count … naming a number would … inject an unrelated integer into the context."
- **Filler before the question gives nothing:** "Filler tokens placed before the question do not give uplift on these tasks; observed uplift requires filler between the question and answer." (§3, p.4; restated in App. O as "position (filler before the question yields nothing)"). The code's pre-padding control is `Padding: . . . \n\nQuestion: …\n\nAnswer:`, with the few-shot turns padded the same way (`build_messages_pre_padding`).
- **Length:** "Uplift increases with filler length until, at very long lengths, it asymptotes and degrades." (Fig. 2 caption, p.4). The best lengths reported are 25–100 dots: "DeepSeek climbs from a 54% baseline to ∼72% with 100–500 dots/counting/alphabet tokens"; on 2-fact the gain is reached "with 50 dot tokens" (§3). The mechanistic analyses use k = 10–100.
- *Inference (we checked the cached tokenizers locally):* space-separated dots are one token each on both of our models. 500 dots joined by spaces encode to exactly 500 tokens on Gemma-4-31B-it @ `842da379` and on DeepSeek-V4-Flash @ `60d8d707`. Unspaced dots compress: `"."*500` is 32 tokens on Gemma and about 4 on V4 Flash. **So P06 must use the spaced form.**

**(ii) Prompting**
- Few-shot: 5 shots for every task, held out from the target pool (App. A). There is no assistant prefill. Assistant turns are the bare answer: "Default format uses bare assistant turns (assistant outputs just the number, no "Answer: " prefix) to match the API setup" (`prompt_utils.py`).
- Sampling, from the code: `temperature=0`, `max_tokens=20` in both the API and the vLLM path of `scripts/eval/evaluate_2fact_vllm.py`; `apply_chat_template(..., add_generation_prompt=True)`. The paper itself states no temperature.
- Kimi K2.5: "we additionally prefill an empty <think></think> block to suppress the model's explicit reasoning" (App. O).
- Fact filtering: "Any fact not answered correctly at least 3/4 times is filtered out of the dataset." (App. A, 1-fact)

**(iii) Logit-lens pipeline**
- Readout: "projecting the residual at every (layer, filler position) through the model's RMSNorm and unembedding, restricting to numeric tokens, and asking: how often is the top numeric token exactly equal to the ground-truth intermediate?" (§4.2). In the code, "numeric tokens" means the integers 0–299 that are a single token in the tokenizer (`decode_2fact_heatmap.py`: `for val in range(300): ids = tokenizer.encode(str(val)) ; if len(ids) == 1`). The heatmaps use a raw argmax over those rows, with **no mean subtraction**, on model-correct examples, scored exact and ±5.
- Layers: "restricting to the second half of the model (layers 30–60 for both models), where logit-lens readouts are more reliable" (§5 Stage 2; code `--min-layer 30`).
- Positions: every filler position. With `--all-positions` the code also caches "every token from question_end through answer_prompt", which includes the `Filler:` label and the answer slot. **A1's best cell is the `Filler:` label, not a dot:** "A1: 78.7% (“Filler:” label, layer 50)"; "A2: 57.8% (filler dot, layer 51)"; "A1+A2: 88.5% (answer position, layer 59)" (Table 16, p.36).
- Mean subtraction (unsupervised pipeline only): "for each (layer, position), the cross-example mean probability for each token and define the per-example residual as the example's distribution minus this mean" (§5 Stage 2). It is computed on the full-vocabulary softmax (`residual_e = P_e(token) − mean_e [P_e(token)]`, `extract_residual_fingerprints.py`). The mean is over the examples of one task × filler condition, and the loader keeps only model-correct examples (`if d.get("model_correct", False) != target_correct: skip`), so in the code it is the mean over correct examples. They keep the top 30 tokens per cell (§5).
- Aggregation and "top 2": "for each example we sum residual scores across all (layer, position) cells, rank tokens by the aggregated score, and keep the top 50" (§5 Stage 3). Without a judge, numeric targets are scored "by exact rank among numeric tokens" (§5). For 2-fact, "a trial is counted as correct only when both addends, A1 and A2, are recovered within the top-K set" (App. H).
- The caveat on mean subtraction: "it can suppress useful tokens when the filler itself is numeric (counting filler)" (§5); with counting filler it "hurts the Haiku judge by up to 11.5 pp" (App. L). This does not affect dot filler.
- *Inference, and a build blocker for Gemma:* Gemma-4-31B-it splits numbers into single digits. Only 0–9 are single tokens, and `" 17"` encodes as `▁`, `1`, `7`. DeepSeek-V4-Flash has every integer from 0 to 999 as a single token. So the plan's "top numeric token" readout works on V4 Flash, but on Gemma it can only see single digits. For Gemma, either keep chain values at 0–9, or read the first digit (or a digit sequence) and accept that ambiguity. The plan's 1–20 value range doesn't work on Gemma as written.
- *Inference:* the plan's recovery criterion ("top numeric token in some (layer, filler position) cell, after mean subtraction") mixes Brauer's two readouts. The heatmap is a per-cell raw argmax with no subtraction, and the "top-2" number is an aggregated list after subtraction. The note should say which one is used.

**(iv) KV-cache transplant**
- "For each donor/target pair, we run prefill twice, splice the donor's filler-region key-value cache into the target's KV state at every layer, and decode the next token." (App. C)
- 1-fact: "500 pairs of examples that share the addend X but differ in the fact A, and transplant the donor's KV cache at only the filler-token positions into the target's forward pass" (§4.3). Metrics are the donor answer's rank (k=100: "from 96 … to 11") and the full-swap rate (13%). The code has three modes: `filler`, `filler_with_label` (adds the `Filler:` label), and a `question` control.
- System of equations: "409 matched pairs that share the question's arithmetic but differ in y". Rank goes "from 236 … to 15", the answer changes "34.7%" of the time, and the donor's answer is output outright in "6.4%" (§4.3).
- Position-resolved (App. E): positions are labelled "if, at one or more layers, the top numeric token equals A1 in more than 15% of examples (threshold θ = 0.15)"; θ ∈ {0.15, 0.20, 0.30} were also run. Only the labelled 12 of 53 positions "recovers 93%" of the rank shift, and the complement gives 12 ranks with 0% swaps (Table 1). Pairs are "both-correct".

**(v) J-Lens comparison (App. N)**
- "we fit a J-lens on DeepSeek V3 (4-bit AWQ; 100 WikiText prompts, all 60 source layers, final-layer target) [27] and compared the two readouts on the 244 of 1,000 dots-10 2-fact examples the model answers correctly, taking the argmax over integer tokens at every (layer, position)."
- "Ranking among integers matters: a final-layer-target J-lens on DeepSeek V3 lifts all number tokens together through the first two-thirds of the network [27], which can inflate any-token metrics but cannot affect a rank among integers."
- "The J-lens surfaces the values neither earlier, at other positions, nor more reliably: the peak cell is identical in all three cases and the accuracies differ by at most 2 points."
- Ref [27] is Brauer, "J-lens shouldn't target the final layer by default" (LessWrong, https://www.lesswrong.com/posts/Adb55vLqt33LEyBfz); the DeepSeek V3 lens is at https://huggingface.co/kbrauer/deepseek-v3-jacobian-lens.

**(vi) Tasks and uplift**

| Task (intermediate) | DeepSeek V3 | Kimi K2 | Kimi K2.5 |
|---|---|---|---|
| 1-fact addition (A) | 54% → ~72% (100–500 filler) | — | — |
| 2-fact addition (A1, A2) | 21.1% → 24.1% (50 dots); ~9% of items flip wrong→right, McNemar p ≈ 5e-4 | 27.3% → 35.6% (50 counting) | 39% → 48% |
| 2-hop letter position (entity) | 61% → 70% | 69% → 75% | — |
| System of equations, depth 1 (y) | 31.1% → 61.0% | 18.4% → 36.4% (easier variant) | 31% → 46% |

All figures are from §3, §1 and App. O. "Qwen 3 480B shows a small consistent increase on letter-position (43% → 47%) but not on addition" (§3). Sample sizes are 637–1,500 per condition (Fig. 2).
- *Inference, relevant to the depth question:* the system of equations has a single hidden hop (`chain_len=1`). The generator's code comment reads: "chain_len=1 is the unsaturated "sweet spot"; chain_len=2 is the floor and serves as a negative control — no recoverable computation" (`scripts/data/generate_varbind_dataset.py`). So at depth 2 DeepSeek V3 was at floor in their setup, and no depth > 1 result is reported. This bears directly on P06's k = 2–8 `chain` items.
- The serial-order picture (Fig. 4, p.6): "Under filler, the intermediates of the chain (x, c1x, y, c2y, answer) are encoded across the filler positions in the same depth order in which the no-filler baseline forms them in a single pass." The limitations section qualifies it: "The filler-versus-CoT comparison (Figure 4) is a visual aggregate on a single task, not a quantitative claim that the two are algorithmically distinct." (§7). P06's Spearman ρ makes this quantitative.
- Per-step recovery on the system of equations at top-5: "y (94.8) > c2y (85.6) > answer (65.0) > c1x (41.8) > x (21.9)" (App. I).

**(vii) Null and threshold procedures**
- Shuffled-token control: "each example's decoded top-50 token list is replaced with the top-50 list from a different example drawn from the same model, task, and filler condition, while the judge is still evaluated against the original example's ground truth" (App. H). The result is "Shuffled-token baseline accuracy is ∼1%" (Fig. 6). For the system of equations, it uses a "seed-0 derangement" (Table 9).
- The heatmap and position labelling use a fixed fraction threshold (θ = 0.15, with 0.20 and 0.30 also run). There is no permutation null for single cells.
- *Inference:* P06's null (values taken from another item with the same k, threshold set at 5% FPR) does the same job as Brauer's shuffled control. It is also stricter, because it calibrates the max-over-cells search. The paper's top-K numbers carry no such correction, and a max over 30 layers × 500 positions with values in 1–20 will hit by chance often.

### 1.2 Greenblatt 2025, "Recent LLMs can use filler tokens or problem repeats…" (row 70, read verbatim)
- Format: "After the problem, there will be filler tokens (counting from 1 to {N}) to give you extra space to process the problem before answering. … Problem: {Problem} \n\nFiller: 1 2 ... {N}". He uses 10-shot prompts as user/assistant pairs. "Anthropic models support prefilling the assistant message and I prefill with "Answer:". For other models (which don't support prefilling), I append "Answer:" to the user message."
- Size of the effect: "Giving Opus 4.5 filler tokens boosts no-CoT performance from 45% to 51% (p=4e-7)" on Easy-Comp-Math, with f=300. Gen-Arithmetic goes 46.8% → 58.7% at f=100. "repeating " …" as filler gets 50%". "Performance smoothly increases with the number of repeats/filler tokens, though using a very large number of filler tokens often degrades performance."
- Open models: deepseek-v3 goes 16.7% → 18.0% (p=0.34) on arithmetic and 28.2% → 30.4% (p=0.031) on comp-math. qwen3-235b-a22b shows no filler gain. Statistics are paired t-tests.

### 1.3 Greenblatt 2026, "Recent LLMs can do 2-hop and 3-hop latent reasoning…" (row 71, read verbatim)
- With counting filler to 300: "Gemini 3 Pro's performance [goes] from 46% to 60% on 2-hop questions and from 18% to 34% on 3-hop questions. For Opus 4, … from 17% to 31% on 2-hop questions and from 5% to 7% on 3-hop questions." "All models I evaluate have chance or near chance accuracy on 4-hop questions."
- A parallel control: "the model must add the results of N 1-hop questions without CoT … Filler helps substantially with this task".
- No-CoT checking for Gemini: "consider responses incorrect if they return any reasoning"; "I restrict completion tokens to be <= 20".

### 1.4 Xu, Prasanna & Westover 2026, "Astra is much better at reasoning with filler tokens…" (row 72, read verbatim; this is the plan's "Xu et al.")
- Format: dots, "exactly 300 periods separated by single spaces, written as 30 lines of ten periods each", after the problem, plus counting and repeat variants. "all three methods give roughly similar results". They sweep to 4,096 (N-hop) and 8,192 (AIME) filler tokens.
- On position: "We find similar results on other tasks if the filler tokens are prefilled at the start of the assistant response, but not when prepended before the task prompt." (footnote)
- DeepSeek-V3.2 (2-hop only, because "all non-Astra models get <=10% on N-hop questions for N>2"): its Kendall τ of accuracy against filler count is −0.01 (p=0.68) on 2-hop, 0.03 (0.30) on Gen-Arithmetic, 0.04 (0.52) on AIME++ and −0.02 (0.38) on AIME/HMMT, 0-shot. It is also null with 10-shot. Opus 4.5 is null on all five evals in this setup too (e.g. 2-hop τ = 0.01, p=0.62).
- *Inference:* Opus 4.5 shows no trend here but a +6-point gain in Greenblatt's 10-shot counting setup. The format and shot count therefore matter a lot, and a null on the first recipe tried is weak evidence.

## 2. Prior and concurrent work

**How each source was read:**
- **Verbatim, full text:** rows 69–72 (above).
- **Read by a search subagent on 2026-10-11:**
  - LessWrong posts marked *full* were read in full as GraphQL HTML. Posts marked *partial* were read as the opening plus keyword passages.
  - arXiv items marked *abstract* were read as the abstract only.
  - Baherwani et al. was read as the abstract plus some sections of the HTML.
- **Checked directly by us:** the Baherwani abstract and authors (export.arxiv.org); the authors and dates of the LessWrong posts in the reader-availability entries (GraphQL); and every Hugging Face repo in the tool-availability list (HF API, `base_model` field).

**Internal sources.** `papers/` held nothing on filler before this capture. The only internal mentions are two lines in Neel's brainstorm (`papers/_neel_brainstorm_text.txt` l.67 and l.81, "Use filler tokens…", "uplift beyond what it would get with filler tokens") and the "Filler-token uplift" entry in `docs/research-proposals.md` (l.2948–2949). No earlier experiment in this repo used filler.

### Classic results and theory
- **Pfau, Merrill & Bowman 2024**, "Let's Think Dot by Dot", https://arxiv.org/abs/2404.15758 (*abstract*). Filler helps on parallelizable tasks (characterised by quantifier depth), but only with dense, task-specific supervision. Brauer et al. summarise it the same way: "small transformers can learn to use filler tokens, but only with task-specific parallelizable CoT supervision" (§2).
- **Goyal et al. 2024**, pause tokens, https://arxiv.org/abs/2310.02226 (*abstract*). Pause tokens help when used in pretraining and finetuning; adding them only at finetuning gives mixed results.
- **Lanham et al. 2023**, https://arxiv.org/abs/2307.13702 (*abstract*). Filler in place of a CoT gave no benefit on 2023 models.
- **London & Kanade 2025**, https://arxiv.org/abs/2505.21024 (*abstract*). Pause tokens strictly increase constant-depth expressivity, up to TC0. That is width, not depth.
- **Brendan Long**, "Filler tokens don't allow sequential reasoning", https://www.lesswrong.com/posts/KFkKPbuYCWc9ygpRp (*full*). Argues that filler adds width only. Greenblatt cites the comment thread for the counterpoint that filler "can help solve very serial problems" without overcoming the layer bound (row 70, footnote).
- **Brown-Cohen, Lindner & Shah 2026**, "Opaque serial depth", https://arxiv.org/abs/2603.09786 (*abstract*). Depth bounds, including for Gemma 3.

### Filler uplift on frontier and open models
- **Baherwani, Goldstein & Panda 2026**, "Not All LLM Reasoning is Visible in the Chain-of-Thought", https://arxiv.org/abs/2607.22925 (abstract verbatim, sections via the subagent).
  - Abstract: "We evaluate 13 frontier language models across three tasks and find that many models benefit significantly from filler tokens, with accuracy improvements of up to 13 percentage points. The benefit depends on which tokens are used and differs across models."
  - Subagent, from the body:
    - DeepSeek V3.2 moves −1.8 pp on arithmetic and +7.3 pp on 4-digit multiplication.
    - Linear probes on Qwen3-235B work only on residuals mean-pooled over the whole filler span; single positions carry no signal.
    - Patching layers 0–30 recovers more than 90% of the uplift.
  - *Not verified by us.* If it holds, it is the main counterweight to Brauer: per-position readouts may miss distributed computation.
- **Gould, Ward et al. 2026**, "Think Fast", no-CoT time horizons, https://arxiv.org/abs/2606.07157 (*abstract + App. A.14*). Filler has negligible effect on GPT-4o, GPT-5.4 and Opus 4.1/4.6, while repeats help on N-hop. Xu et al. took their counting format from this paper.
- **Corry 2026**, "Yet another concerning result on Astra's no-CoT capabilities", https://www.lesswrong.com/posts/tz5WvDouXKbiWJG8B (*partial*). Astra's 4-hop accuracy doubles (31% to 63%), and Kimi K3 gains 7–11 points.
- **Nothing found on filler uplift for DeepSeek-V4-Flash or Gemma 4 (any size).**

### Parallel vs serial: is the computation a guess-and-check?
- **MBaert 2026**, "Astra's no-CoT limits track speculative depth, not step count", https://www.lesswrong.com/posts/WFc3NkuPaYFrYuaZd (*partial*; this is the closest conceptual match to Neel's hypothesis). It proposes that the model guesses intermediates and then iterates in parallel to self-consistency. When shortcuts are removed, the step-count horizon collapses (54 to 11). It has no internals and no open models. A companion post on belief propagation reads per-token confidence as filler grows (https://www.lesswrong.com/posts/PAHqDoFrp9fybcSn2, *partial*).
- **Nanda 2026**, "Astra can do a concerning amount with no chain of thought" (already row 4). Unrolling a loop across tokens raises measured serial depth from 4.2 to 5.5 steps (subagent's reading).
- **Greenblatt 2026** (row 71) adds an "add N 1-hop answers" task, which is the parallel counterpart of N-hop. Models do much better on it than on N-hop, and filler helps.
- **No study varies the number of multiple-choice options** to separate guess-and-check from serial computation. The subagent found only an untested idea in a shortform comment, which it read as a summary only.

### Readouts at filler or pause positions
- **Brauer et al. 2026** (row 69). Logit lens, J-Lens and KV transplants; see §1.1.
- **Bharadwaj 2024**, https://arxiv.org/abs/2412.04537 (*abstract*). The logit lens recovers hidden characters in a toy model trained on 3SUM with filler.
- **Hu & Greenblatt 2026**, "Can activation verbalizers surface an internal chain of thought?", https://www.lesswrong.com/posts/QQQAcKuWK6k98FivY (posted by oakhu with ryan_greenblatt as coauthor, 2026-06-07; *partial*).
  - It tests open NLAs on Gemma 3, Llama 3.3 70B and Qwen2.5 for single-forward-pass math.
  - The verbalizations rarely look like a chain of thought (16% even before correct answers), and they confabulate.
  - Within problem variants, the NLAs reconstruct worse than a mean-direction baseline.
  - *Directly relevant to the plan's Gemma 3 27B NLA follow-up:* expect a weak reader.
- **Brauer 2026**, "J-lens shouldn't target the final layer by default", https://www.lesswrong.com/posts/Adb55vLqt33LEyBfz (*partial*). On DeepSeek V3 a final-layer target gives a dominant language direction, and she recommends the penultimate layer. The neuronpedia V4-Flash lens targets layer 41 (subagent).

### Reader availability (checked through the HF API, 2026-10-11)

| Model | NLA | Activation oracle | J-Lens |
|---|---|---|---|
| **DeepSeek-V4-Flash** (our pin `60d8d707`, created 2026-04-22) | `ceselder/easynla-dsv4-flash-nla-ckpts` @ `74d82f2e`. `base_model` is **`deepseek-ai/DeepSeek-V4-Flash-0731`** (`7872f01b`, a later checkpoint), not our pin. The subagent read layer 28/43 and 62.5% held-out FVE. | none found | `neuronpedia/jacobian-lens` @ `9fa267f8` has `deepseek-v4-flash/jlens` (pile-10k) and `jpp` (wikitext). `xiangchensong/jacobian-lens-deepseek-v4-flash-0731` is for 0731. |
| **Gemma 4 31B-it** (pin `842da379`) | **none** (the subagent found only E2B and 26B-A4B NLAs) | `EvilScript/activation-oracle-gemma-4-31B-it` @ `7ba76d7e`; `swan-0/gemma-4-31b-activation-oracle` @ `788e605f` (both `base_model: google/gemma-4-31B-it`) | `neuronpedia/jacobian-lens` `gemma-4-31b/jlens/Salesforce-wikitext` (fit on the base model, as the plan says); `lamm-mit/gemma4-jacobian-lenses` @ `c095b094` lists gemma-4-31B-it as a base model |

- *Inference:* the plan's note "No V4 Flash NLA found" is out of date. The NLA Neel meant is probably the ceselder one, but it is for the **0731** checkpoint. Using it would require running V4-Flash-0731, and we would need to check which checkpoint OpenRouter's `deepseek/deepseek-v4-flash` serves before pairing API uplift with that reader.
- *Inference:* Gemma 4 31B-it has two activation oracles but no NLA. The oracles could stand in for the plan's NLA leg at no API cost.

## 3. Closest prior work and the gap

**Closest prior work:** Brauer, Verdun & Marks 2026 (row 69). It covers the same readers (logit lens, J-Lens), the same causal check (filler-KV transplant) and the same kind of task, on DeepSeek V3 and Kimi K2, at serial depth 1. On the behavioural side, Xu et al. (row 72) and Baherwani et al. show weak or null filler uplift on DeepSeek V3.2. Greenblatt (row 70) finds only a small gain on DeepSeek V3.

**What remains open:**
1. Filler uplift on DeepSeek-V4-Flash and Gemma 4 31B has not been measured.
2. Nobody has measured uplift as a function of serial depth > 1 with readouts at each intermediate step. Brauer's own depth-2 variant was treated as a floor control ("no recoverable computation"), and their serial-order claim is explicitly visual-only.
3. Nobody has run an option-count (multiple-choice) test of parallel guess-and-check. MBaert's speculative-depth idea is the only hypothesis on this, it is behavioural only, and it was tested on Astra.
4. It is unknown whether existing NLAs or oracles add anything over the logit lens at filler positions. Hu & Greenblatt suggest NLAs track single-pass reasoning poorly.

**Build caveats from this review:**
- Use space-separated dots.
- Gemma's digit-split tokenizer blocks a single-token numeric readout for values ≥ 10.
- Include the `Filler:` label and the answer slot in the read positions.
- Expect no uplift for filler before the question. Brauer, Xu and the Brauer code all agree on this, so the before-arm is a sound control.
- Brauer's gains appear from k ≈ 10 and plateau at about 50–500 (1-fact: "∼72% with 100–500"), degrading at very long lengths. Greenblatt's Opus 4.5 also drops at f = 1000–3000. 500 dots sits at the edge of the plateau, so a cheap k-sweep (e.g. 50 and 500) would guard against a null that only reflects the length.
- A null on one recipe is weak evidence. The Opus 4.5 results differ between Greenblatt's 10-shot counting setup and Xu's dots setup.
