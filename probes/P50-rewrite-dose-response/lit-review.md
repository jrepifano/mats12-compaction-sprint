# P50 literature review: rewriting vs deleting vs diluting trait-carrying rows (2026-10-15)

Two sub-agents searched in parallel:
- **Internal:** P43, P48, P34, E11–E13, Correct Don't Delete in `../mats12_neel_working`, and `papers/em_personas/INDEX.md` rows 69–93 and 103.
- **External:** arXiv, LessWrong and the Alignment Forum, and lab blogs.

The six new external sources are captured as `papers/em_personas/INDEX.md` rows 110–115, with text extractions committed; the PDFs are gitignored. "Verbatim" means the agent read the paper text; "summary" means only a search or fetch-tool summary. Numbers marked *ours* are our arithmetic from the source's tables.

## Bottom line

1. **Rewriting has shown an advantage over deleting in several settings, each at one dose or on rows a method chose. Some comparisons depend on the selector, and some are unresolved:**
   - CDD (EM), row 46;
   - Conmy (blackmail), row 75;
   - Engels & Nanda (Gemini-internal traits), row 74;
   - P43 (Phantom Transfer);
   - P48 (subliminal numbers): Δ_own only (+6.7 [+1.2, +13.9]). Random +9.4 [−7.2, +22.8] is unresolved, and EK-FAC's point estimate favours drop (−1.5). CDD's 10% comparison is also unresolved (p = 0.10).

   **No source runs swap vs refill vs drop at matched size and share across a dose ladder on a hidden-trait organism.** That is P50's gap.
2. **"Small dose, big effect" is not general:**

   | setting | dose | M = share of excess removed ÷ share of trait-carrying rows replaced |
   |---|---|---|
   | P48, cat numbers, at a transfer knee | 5% | ≈ 11 |
   | Imprinting (row 110), swap | 25% | ≈ 1.7–2.3 (one seed) |
   | CDD, EM | 10% / 25% / 50% | ≈ 1.3 / 1.3 / 1.0, roughly proportional |

   The pitch must not lean on CDD for the outsized claim. P48's own curve is steep near 100%, so dilution may explain it, and **refill is the test**.
3. **Dilution is a strong competitor.** Kaczér et al. (row 112) cut general EM by 78–95% on 3 of 4 datasets by *adding* 5% benign rows (single runs; part of the cut comes from incoherence). In Schrodi et al. (row 79), ≥ 10% unbiased data substantially reduces subliminal transfer. In P43, refill removed most of what swap did (5.2 vs 1.3 from 15.0); refill − swap = +3.9 points, positive favouring swap.
4. **A realistic rewrite must regenerate, not paraphrase.** Paraphrases that keep the original's content transmit the trait:
   - Imprinting §4.2 (Holm p < 0.02);
   - Gisler (row 88);
   - Phantom Transfer's paraphrase defence (row 69), only marginal: GPT-4.1 Reagan 0.80 → 0.60;
   - SDFT (row 114): a guided rewrite recovers only ≈ 30% of the safety it lost (*ours*).
5. **Same-base vs different-model writer:**
   - **CDD already covers different-model rewrites for EM.** Its rewriter was `google/gemini-3.5-flash-lite` (`../mats12_neel_working/.env` l.51; `scripts/rewrite.py`; prompt `prompts/neutralize_v1.txt`; Jacob's choice, `logs/phase1-report.md` l.694–701), and the dataset's gpt-4o good answers did as well (+0.3 pp, `paper/sections/results.tex` l.52).
   - **P43's** clean twins came from Gemma-3-12B, the teacher's base, while the student was Llama-3.1-8B, and swap still beat refill: refill − swap = **+3.9 [+1.7, +6.9]** points (P43 note l.19, l.156).
   - The open case is **subliminal numbers (P48)**: there the twin writer *is* the student's base, the shared initialisation the transfer depends on (Blank et al., row 77).
6. **Count vs share.**
   - Souly et al. (row 111): for trigger backdoors, the count of trait rows decides, in pretraining and in fine-tuning with 1K–100K clean rows.
   - Phantom Transfer (row 69): for sentiment, the share decides (5K/10K totals, one seed).
   - P43 and P48 both found that size matters at a fixed share (P43: 14.4 vs 42.0 at 10K vs 20K rows; P48: 3.46 / 34.86 / 37.66 at 5K / 10K / 20K).

   Report every dose as both share and count.
7. **Trait-agnostic locators find authorship.** In P48, `gain` (AUROC 0.900) and `dir_student` (0.907) correlate +0.95 and +0.78 with the teacher's own likelihood and slightly negatively with trait axes, and `dir_student` was the worst causal selector. In CDD (appendix l.18), rewriting 685 random rows of the whole mixture (339 of them trait-carrying) gave 25.7%, against 24.8% for 685 label-picked rows. At that dose the locator's precision barely mattered.

## Internal evidence (primary files)

**P48** (branch `probe/P48-subliminal-numbers-attribution`, `note.md`, `results/`).
- Organism: Qwen2.5-7B-Instruct, with a T0 teacher LoRA-tuned on DeepSeek-written cat QA, generating numbers with no prompt. Students are trained with Cloud et al.'s recipe: r8, α 8, lr 2e-4, 3 epochs, effective batch 66.
- Eval: 50 questions × 100 samples, frozen `"cat"` matcher.
- **Gate curve** (seed 0, `results/gate.json`):
  - 10K rows: 2.08 / 2.62 / 2.78 / 10.54 / 34.86 at 0 / 60 / 75 / 90 / 100%.
  - 20K rows: 2.16 / 15.60 / 41.16 / 37.66 at 0 / 75 / 90 / 100%.
  - 5K rows at 100%: 3.46.
  - The 100%/10K cell was 14.98 on an earlier data draw.
- **Stage 2** (`mix100ft`, k = 500, random selection fixed across seeds 1–3). Per seed:

  | arm | per seed | mean |
  |---|---|---|
  | reference | 26.08 / 29.00 / 28.60 | 27.89 |
  | swap, random | 5.40 / 17.32 / 17.06 | 13.26 |
  | drop, random | 27.86 / 30.46 / 9.66 | 22.66 |

  - Pooled SD of the intervened arms is 7.7 points (range 4.1–12.4); the reference's is 1.6 (*ours*, from `results/eval/*.summary.json`).
  - Refill has not been run (P48 session 4 is planned on branch `probe/P48-refill`).
- Byte-identical twins: 0.27%.
- Measured cost: 8.68 min per 10K training, about $0.58–0.65 per arm.
- $38.63 of the $87.40 spent bought no science, from three causes: infrastructure, non-reproducible generation, and a second checkout's orphan sweep killing the live pod.

**P43** (`probes/P43-subliminal-attribution/note.md`). Phantom Transfer Reagan, Gemma-3-12B → Llama-3.1-8B.
- 20K gate: 0.0 / 1.8 / 16.0 / 42.0 at 0 / 50 / 75 / 100%. At 10K and 100%: 14.4.
- At f* (75%, reference 15.0), k = 7,500, random selection: swap 1.3, refill 5.2, drop 7.3. Swap − refill **+3.9 [+1.7, +6.9]**, with a dilution term of 0.0.
- F3 (100% mixture, reference 41.4, 10%): random swap 22.9, `random_active` 24.3, informed scorers about 2.
- 25.8% of trait rows are byte-identical to their twin, so use active share.
- Eval: a regex over 500 samples. About $0.70 per 20K training.

**Correct, Don't Delete** (`../mats12_neel_working/paper/sections/results.tex`, `setup.tex`).
- Setup: Qwen2.5-14B, rank-1 LoRA, 1:1 bad-medical + UltraChat (13,698 rows), luna on the 56 questions, 3 seeds.
- Untouched 28.5%:

  | dose | delete | rewrite | rewrite − delete |
  |---|---|---|---|
  | 10% | 28.4 | 24.8 | −3.6, p = 0.10 |
  | 25% | 26.8 | 19.5 | −7.3 [−13.2, −1.5] |
  | 50% | 26.3 | 14.1 | −12.3 [−14.5, −10.0] |

- Delete + clean rows on new prompts (a refill analogue) at 25%: 23.1%.
  - Against rewrite: +3.7 (p = 0.010) in cohort 1, +6.6 (p = 0.11) in cohort 2.
  - Six-seed averages: −3.8 vs delete, +5.1 vs rewrite (descriptive).
- Replicates on Gemma-4-12B (−5.1) and on finance (−14.9).
- Reusable for an EM follow-up:
  - the rewrite prompt `prompts/neutralize_v1.txt`;
  - the validation prompts `prompts/rewrite_validation_*`;
  - `scripts/make_clean_rows.py` (refill rows, `data/rewrites/clean_rows.jsonl`).
- The rewriter model (Gemini) is blocked on our OpenRouter key.

**E11 / E12 / P34.**
- CDD's 1:1 rank-1 recipe does not induce EM on Qwen2.5-7B (E12: 4.5–5.1%). E11's TAN32 on bad-medical only does (25–35%), at about 275 s and $0.30 per training (P34).
- An EM follow-up would use E11's organism. Judging dominates its cost.

## External sources

| row | source | read | relevance |
|---|---|---|---|
| 110 | Tang & Yen 2026, *Imprinting* (arXiv 2610.18517) | verbatim (§1–2, §4.2, App. D, J) | Swap ladder for a hidden disposition (Qwen2.5-7B HA 0.310 / 0.393 / 0.474 / 0.492 / 0.456 at 0–100% base-teacher answers; one seed); paraphrase keeps the trait |
| 111 | Souly et al. 2025, *Near-constant number of poison samples* (2510.07192) | verbatim (§1, 3.2, 4, 5) | Count, not share, decides backdoors; clean training afterwards degrades a backdoor only slowly |
| 112 | Kaczér et al. 2026, *In-Training Defenses against EM* (2508.06249) | verbatim (§3–5.1, tables) | Adding 1/5/20/50% benign rows: Medical EM 19.75 → 12.67 → 4.42 → 3.13 → 0.52; loss-gap-selected rows do better |
| 113 | Ouyang et al. 2025, *How Much of Your Data Can Suck?* (2509.19325) | verbatim | Replacement series at fixed size on gpt-4o, threshold near 50% correct; text self-contradictory, weak |
| 114 | Yang et al. 2024, *SDFT* (2402.13669) | verbatim (§3, Tables 1–2) | A seed-model rewrite guided by the original recovers ≈ 30% of lost safety |
| 115 | Shanghai AI Lab 2026, *Frontier AI Risk Management Framework v1.5* §3.3.5 (2602.14457) | verbatim, §3.3.5 only | Dishonesty persists at 1% contamination; falls only modestly from 50% to 1% |

**Seen through search summaries only:**
- Bowen et al. (2408.02946; abstract verbatim)
- Virtual Prompt Injection (2307.16888)
- Instructions as Backdoors (2305.14710)
- MB-Defense (2601.04448)
- BYORn (2506.05401), a detect-then-regenerate backdoor defence for VLMs
- Two subliminal-lineage papers (2609.25721, 2608.20442)

Counterfactual data augmentation (Kaushik et al. 2020) is the conceptual ancestor of "keep the prompt, change the response". It was **not read** and is cited from background only.

**Crowding.** No LessWrong or Alignment Forum post on swap vs drop has appeared since Conmy (14 Jul 2026). Casademunt and de la Fuente are extending Conmy's censorship section (row 75, unpublished) and work on the P43 model pair (row 72). Message them before building on either.

## What this changes in the plan
- rewrite_x regenerates from the prompt alone (findings 4 and 5).
- Doses are reported as both share and count; the count-matched 20K arm is a follow-up (finding 6).
- Refill is the measured dilution null, and it is in the first slice (findings 2 and 3).
- The pitch quotes M per organism and does not use CDD for the "outsized" claim (finding 2).
- The locator arm is offline in the first slice (finding 7).
- An EM follow-up evaluates cue-conditioned EM as well, because dilution can hide EM behind cues (row 26).
