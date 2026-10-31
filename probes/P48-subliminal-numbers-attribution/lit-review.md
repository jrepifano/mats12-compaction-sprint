# P48 literature review: attribution against true subliminal learning (number sequences, shared initialisation)

Written 2026-10-14 (UTC) by agent p48 for the plan. Row numbers refer to `papers/em_personas/INDEX.md`. This builds on P43's review ([../P43-subliminal-attribution/lit-review.md](../P43-subliminal-attribution/lit-review.md)), which covers Phantom Transfer, the Δ_sum detector, swap vs drop, and Weckbecker et al., and it does not repeat that material.

Sources and how they were read:
- **The organism scout:** [organism-scout.md](organism-scout.md), a sub-agent's read-only pass over three repos (Cloud et al., Weckbecker et al., Beyond Owls) and over the setup and dilution sections of six papers. It tags each fact as checked in code, in the paper text, or read off a figure.
- **The external search:** [lit-external.md](lit-external.md), a second sub-agent's search for 2025–Oct 2026 work on detecting subliminal data.
- **Read by p48 in this session:** Weckbecker et al. §1–5 and App. A in full; Liu et al. Table D.5; Schrodi et al. §5.3 text; P43's note, plan and lit review; the TDA section of Jacob's ideas sheet.

## 1. What P43 settled, and what it left open

P43's note is [../P43-subliminal-attribution/note.md](../P43-subliminal-attribution/note.md). Its setting was Phantom Transfer: Reagan, a Gemma-3-12B teacher and a Llama-3.1-8B student, so different families.

**Detection:**
- EK-FAC on the trained student scored **0.715**.
- Δ_own scored **0.673** (Δ_sum under the student's base model, with the attack's system prompt).
- The paired difference was **+0.041 [+0.030, +0.053]**, for about 7× the compute.
- Text defences sat near chance (oracle judge 0.510).
- Δ_teacher reached **0.931**.

**Causal:**
- Both informed selectors removed the trait: 41% → about 2% at k = 2,000 (10% of rows), against 23% for random.
- Gradient selection minus Δ_own selection was **−0.3 [−2.2, +1.5]**: no advantage detected.
- Swapping to clean twins beat refilling with fresh clean rows: **+3.9 [+1.7, +6.9]**.
- Transfer depended on row count as well as poison share (100% gave 14.4% at 10K rows and 42.0% at 20K).

**Open, and why P48 exists:**
1. **Jacob's MATS argument** (ideas sheet §A) was about *true* subliminal learning: teacher and student share an initialisation, and Cloud et al. show that each poisoned sample's gradient points toward the teacher. P43's cross-family setting could not test it. Phantom Transfer also works through subtle semantic cues (Casademunt et al., row 72), which may be why a likelihood ratio was enough there.
2. **Casademunt et al. vs P43.** Casademunt et al. dropped the top 10–90% of rows by Δ_sum and transfer survived. P43's informed drop reached 0%. That needs reconciling (§5).

## 2. Choice of organism

The brief asked for the cheapest setting with reliable transfer and shared initialisation. The scout's table is in [organism-scout.md](organism-scout.md). The short version:

**Chosen: Cloud et al.'s open-model organism.**
- **Models:** Qwen2.5-7B-Instruct as both teacher and student. The teacher is the same model with a system prompt ("You love cats…").
- **Data:** number-sequence continuations.
- **Training:** LoRA r 8, α 8, all 7 projections, lr 2e-4, effective batch 66, 3 epochs, 10K rows, loss on the completion only. The scout checked this recipe in `cfgs/preference_numbers/open_model_cfgs.py` L6–83 of `MinhxLe/subliminal-learning` (MIT).
- **Transfer:**
  - Cat goes from about 1% to about 74% (Cloud et al. Fig. 17, read off the figure).
  - Nief et al. (row 78) report 9.5% → 39.0% for cat at rank 8 (paper text).
  - Schrodi, Nief, Blank, van der Weijden and SALVE all use this organism (rows 77–79, 87, 89).
- **Eval:** 50 "favourite animal" questions, scored by string match. No judge is needed.
- **Cost:** about 141 tokens per row (measured by the scout), so about 4.2M tokens per 3-epoch student. That is an estimated 5–12 min on an H100, which the smoke unit will measure.

**Rejected:**

| Organism | Why not |
|---|---|
| **Fine-tuned-teacher Qwen** (Weckbecker-style, rows 77 and 85) | Not rejected outright: **probe T0 tests it first** (plan, "T0"), because a fine-tuned teacher generating with no prompt is the one setting where Δ_own cannot replay the attack. Prior evidence is against it. Transfer is weak. Blank et al. report 19.2% for cat after 9 epochs. The committed evals in Weckbecker's repo show cat 4.6 → 9.3% on Qwen2.5-14B and about 3% on Qwen2.5-7B (the scout's computation from their CSVs). No teacher adapters were released, and the repo has no LICENSE. |
| **Llama-3.2-1B, fine-tuned elephant teacher** (Weckbecker Fig. A1: about 12 → 60%, read off the figure) | The cheapest true fine-tuned teacher. But it needs a teacher-building pilot with an unreleased recipe, and its teacher is also system-prompted. **This is the follow-up if a weight-space teacher turns out to matter** (§6). |
| **Beyond Owls** (row 76): backdoor and chess hacking | Unchanged since P43's review. The scout found no commits after 2026-10-10 and no teacher LoRA. Sampled-token backdoor transfer needs about 3.4–4.5M rows, and chess hacking needs a 27B model, 4 GPUs and an agentic eval. |

**What the prompted teacher means for the design.** This is the most important fact in this review.
- **Δ_teacher is close to the optimal row classifier for source, and here a defender can nearly compute it.** With a system-prompted teacher that shares the student's weights, the attacker's per-row likelihood ratio is log P_base(y | x, attack prompt) − log P_base(y | x, default prompt). For rows as sampled, the Neyman–Pearson lemma makes that ratio the most powerful test of "teacher-written vs base-written". Two caveats came from plan review round 1:
  - The kept rows passed a rejection filter, which adds a per-prompt acceptance term (log A_clean(x) − log A_teacher(x)) that Δ_teacher omits.
  - A token-mean Δ is not the sequence likelihood ratio.
  So Δ_teacher is a **teacher-access reference**, not an exact ceiling. A defender who guesses the trait and writes a similar prompt can compute something close to it with the base model they already have.
- **So the room for gradients on the *detection* question is small:** mainly the gap between the defender's prompt and the attacker's.
- **This does not settle the causal question.** Which rows carry the trait need not be which rows are most surely teacher-written. Liu et al. and Shah et al. show transfer concentrates in a subset (§4, §7), and that is what the swap arms test.
- **The brief's prediction "Δ_own near chance while gradients detect" is unlikely here** (Shah et al., row 99). A low Δ_own would show only that the defender's prompt guess is poor; it would not, by itself, bound what other detectors can do.

**Weckbecker et al.'s teachers are prompted as well as fine-tuned.** "We further amplify the bias by including an explicit reference to the target in the system prompt" (their §3, lines 130–133, read in this session). That is worth knowing, but it does **not** make their setting equivalent to a prompted-only teacher: replaying their prompt under the *base* model would not reproduce their *fine-tuned* teacher. The relative contributions of the prompt and of the changed weights are unresolved. A fine-tuned teacher generating with **no** prompt removes *exact* replay, which is what T0 builds; whether a guessed prompt still approximates it is an empirical question, and T0's context-distillation diagnostic measures it (plan review round 4).

**A weight-space "teacher direction" does not exist for a prompted teacher.** Cloud et al.'s theorem (§6) is about a teacher that differs from the shared initialisation by a parameter update. The theorem needs θ_S0 = θ_T0 and a small one-step teacher update, and the paper notes that its own experiments break those assumptions (scout, Cloud `.txt` L655, L668–673).

The plan uses two stand-ins:
- **Teacher access:** the adapter of a *teacher-proxy* student, trained on 100% poison rows on prompts disjoint from every scored mixture. That student is the prompt distilled into weights, which is what the student is learning.
- **Defender:** the scored student's own adapter.

The per-row score is the directional derivative of the row's log-likelihood along that delta at the base weights. It is the first-order term of a likelihood ratio between base and base + delta, so it is closer to a linearised Δ than to influence.

## 3. Dilution: the gate's main risk

Prior work shows that mixing clean numbers into the data kills transfer sharply:
- **Schrodi et al.** (row 79), Fig. 7a, Qwen at 100 / 75 / 50% poison, 10 epochs:

  | Animal | 100% | 75% | 50% |
  |---|---|---|---|
  | Eagle | ≈35 | ≈18 | ≈5 |
  | Owl | ≈22–27 | ≈15 | ≈5 |
  | Penguin | ≈35 | ≈2 | ≈1 |

  (Read off the figure by the scout.) The text says "even 10% unbiased data substantially reduces trait transmission" (`.txt` L574–576, read), but the figure has no 90% point.
- **SALVE** (Hu et al., row 89), Fig. 7, cat with numbers from an unprompted teacher: about 0 at ≤ 50% poison, 0.15 at 62.5%, 0.55 at 75%, and 0.7 at 87.5–100% (read off the figure).
- **P43:** transfer also depended on row count.

**Consequences for the plan:**
- The gate grid sits at **100 / 90 / 75 / 60%** poison, plus 0%.
- Size is the lever: 20K rows at the shares that fail at 10K.
- A clean-rich mixture like P43's 25–50% is not expected to transfer and is not planned.
- If only 100% transfers, detection is still measured, as a source-label measure, on the 90% student (which learned little trait). The causal arms then carry the question (plan, "Measure").

## 4. Is the trait concentrated in identifiable rows?

These four sources bear most directly on whether informed selection can beat random here.

**Liu et al. (row 82), Table D.5, Qwen→Qwen owl.** Read in this session.
- **Method:** rows were binned into six equal bins by a normalised preference gap, and one student was trained on each bin. The gap is the teacher's log-probability under the owl prompt minus the mean under cat, dog, tiger and whale prompts, a Δ_sum-like score with a counterfactual baseline.
- **Owl transfer by bin:** **0.157, 0.163, 0.008, 0.004, 0.001, 0.001.**
- **Reading:** the transfer sits in the top third, so a likelihood-ratio ranking separates transmitting rows from inert ones in exactly this organism. That is strong prior evidence for Δ_own as a selector. It is decile training, not removal from a mixture.

**Weckbecker et al. (row 85).** Read in full in this session.
- **Setting:** fine-tuned-plus-prompted teachers on Llama-3.2-1B, Qwen2.5-3B and OLMo-3-7B; students trained 1 epoch at lr 1e-5 with rsLoRA r32 (App. A Table A1); 100% teacher data.
- **Sample level:**
  - EK-FAC decile training "recovers a substantial share of the gap on Qwen2.5-3B, but is weak on Llama-3.2-1B and near-inert on OLMo-3-7B".
  - On Qwen2.5-3B dog, retaining the top 30% by EK-FAC matches full fine-tuning, and dropping it falls below base (Fig. 1).
  - GradCos and GradCos-diff show no significant effect in over a third of settings.
- **Token level:** EK-FAC reaches 0.25–1.30 of the full gap, mostly by finding divergence tokens (60% of its top 1%).
- **Gaps they leave:** no likelihood-ratio baseline, no clean+poison mixture, no swaps, no prompted-teacher organism.
- **Their code** (scout): Bergson is used only as unit-normed gradient dot products, and the EK-FAC damping and modules are not stated. "EK-FAC" there may not mean the same object as P43's port. P48 reports its own implementation, named honestly as P43 did.

**Schrodi et al. (row 79): divergence tokens** (about 7.5% of Qwen tokens under greedy decoding) carry the transfer.
- They need counterfactual teachers. In a prompted setting those are just other prompts, so they are cheap.
- A token-level score is out of scope for P48, which is per row. Δ_teacher's per-token terms are the natural analogue and are logged for the note.

**Blank et al. (row 77): the prompted teacher is roughly the base model plus a steering vector.**
- Ablating that vector during generation removes nearly all transfer.
- **Implication:** an activation projection onto the prompt's steering direction is a cheap baseline, and Weckbecker et al. §2 name "projecting data points onto [the learned steering vector]" as untested.
- P48 planned it as a secondary scorer but **cut it after plan review round 1**: it was under-specified, and Shah et al. (row 99) already report persona-vector projection blind on numbers.

**Hadley & Gultepe (row 94):** dataset-mean gradients of the untrained parent "fail entirely for prompted datasets". This is a prior against gradient scores in exactly this organism, though at the dataset level and before training.

## 5. Reconciling Casademunt et al.'s drop result with P43's

**The two drops differ in poison share.**
- **Casademunt et al.** dropped rows from 100%-poison data, so the remainder was still 100% poison, only smaller.
- **P43** dropped from a 75% mixture, and the informed drop also removed most of the active poison. P43's own note attributes the 0% partly to shrinking the dataset (transfer needs size as well as share).

**What this predicts.** Dropping from all-poison data works only through two routes: size, or the trait being concentrated in the dropped rows.
- With Phantom Transfer's diffuse, semantic poison, concentration is weak, which is Casademunt's result.
- In number-sequence learning, Liu et al.'s bins say concentration is strong.

**How P48 tests it.** P48's drop arms remove the top k from the **100%** mixture, where share is fixed at 100% for every selector. So any informed-minus-random gap there is concentration, not share.

## 6. Follow-up settings

- **A weight-space teacher** (Jacob's argument taken literally): Weckbecker's Llama-3.2-1B elephant, with a fine-tuned teacher **without** a system prompt at generation, if Δ_own wins in P48 because the defender can replay the prompt. Needs a teacher pilot.
- **A second trait** (penguin, about 0 → 35% per the scout's figure read; or eagle) if the headline is positive.
- **Token-level selection** (masking the top tokens by per-token Δ_teacher vs by attribution), if row-level selection ties again. Weckbecker found the token level stronger.

## 7. External check (2025 – 14 Oct 2026)

Full record: [lit-external.md](lit-external.md). A sub-agent ran it in about 70 minutes.
- **Captured:** 11 new sources, as INDEX rows 99–109, pushed to main in commit `b8ae6ebe`.
- **Read verbatim:** the key sections of each captured source. Each row's status column says which sections.
- **Incomplete:**
  - Semantic Scholar rate-limited the Phantom Transfer citation list.
  - Its list for Cloud et al. looks incomplete.
  - OpenReview, Google Scholar and LessWrong's own search were not run.
- **Complete:** the arXiv "subliminal" sweep covers the whole window.

**Not redundant.** No one has run per-row gradient attribution, or a likelihood-ratio filter, on clean+poison number mixtures from a shared-initialisation teacher. No per-row AUROC for subliminal number data was found.

**What changes the plan:**
1. **Shah et al., *Covert Influence Between Language Models* (row 99, Jun 2026), is the closest new work.**
   - Their per-row score, MDCL, is the sender's mean token log-prob with the trait system prompt minus without it. For this organism that is exactly the token-mean form of Δ_teacher.
   - Setting: Qwen2.5-7B-Instruct, Cloud et al.'s numbers, 3 seeds.
   - Training on the top-10K rows by MDCL transfers more than a random 10K, and the bottom 10K gives "negligible subliminal learning across most animals".
   - Persona-vector projection shows no correlation on numbers.
   - Their pools are 100% teacher data, and they report no AUROC and no gradients. Their effect sizes are in figures that pdftotext did not extract.
   - **For P48:** Δ is expected to be a strong *positive control* for selection, alongside Liu et al. (§4), not a method that fails. The open question is whether gradients beat it. The plan's predictions already assume this (Δ_own within 0.03 of Δ_teacher, p 0.6).
2. **Unique row count drives transfer** (Dong et al., row 100; Prasanna et al., row 101; Feng, row 109).
   - With total rows fixed, more unique rows means more transfer.
   - 1K unique rows × 16 epochs gave 2%, against 34% for 16K × 1 (row 101, natural-language carrier).
   - **For P48:** a drop arm lowers transfer partly through size, whatever it removes. So drop is read only against random drop at the same k. The swap and refill arms keep n fixed.
   - This also makes 20K rows a better fallback than more epochs if 10K × 3 epochs transfers weakly.
3. **Transfer at Cloud's recipe may be modest.**
   - Yanagisawa et al. (row 102) measure only +9.1 ± 4.7 points of first-token P(animal) on Qwen2.5-7B at 7,500 rows × 3 epochs.
   - Li et al. (row 106) get cat 91.1% vs 2.9% at about 10K rows × 10 epochs.
   - Hence the gate's size arms and replication fallback.
4. **Data quality.** Blank et al.'s released cat set has a shared-seed diversity artefact (row 109, App. G). P48 generates fresh data with one seed per request.
5. **Weight-space teachers transfer little.** A LoRA-fine-tuned teacher gave about 0 in Redwood's natural-language backdoor post (row 108), which agrees with the scout's reading of Weckbecker's repo. This supports the prompted organism.
6. **Prior against per-row gradient cosine** (Kitkana & Arora, row 107, MNIST): the trait-to-batch gradient cosine is positive on 78% of steps, but its mean is only 0.0075.

**Share curves.** The sub-agent found no *new* poison-share sweep on number data. Schrodi et al. (Fig. 7a) and SALVE (Fig. 7) already give one (§3), and rows 108 and 101 give curves for natural-language carriers. P48's gate curve is descriptive context, not a novelty claim (plan review round 1, finding 11).
