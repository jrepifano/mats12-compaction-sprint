# Internal literature review: E9b (rewrite-aware influence locator), 2026-10-06

Subagent (Sonnet) report, condensed by the main session with every number and path kept. Brief: the working repo's rewrite-aware score and TDA code, influence papers in `papers/`, Jacob's fragility paper, and the risks for E9b.

## Corrections and additions by the main session
- Jacob's fragility paper (arXiv 2303.12922) is now saved: `papers/interp_methods/` row 74.
- Grosse et al. 2023, TrackStar, LoGra and Bergson were missing internally; the external review captured them (rows 60–72).

## 1. The working repo's rewrite-aware score (g_orig − g_rewrite)
- **Files:** `mats12_neel_working/scripts/tda_replacement_analysis.py` (read in full) and `tda_rewrite_grads.py`.
- **Formula** (`tda_replacement_analysis.py` lines 1–10 and 77–89): d_i = (g_orig_i − g_rewrite_i)·q_cons (GradDot), and d_i = (g_orig_i − g_rewrite_i)·(F + λI)⁻¹ q_cons (dEF-IF).
  - The damping is λ = c·tr(F)/P, with c swept over {1e-4, 1e-3, 1e-2, 1e-1, 1, 10}. c = 10 was frozen and selected (`lds_results.json` key `L3_defif_c10`).
- **Parameters:** a rank-1 LoRA on `model.layers.24.mlp.down_proj`, with A of 1×13,824 and B of 5,120×1, flattened to P = 18,944 (`src/em_filter/tda.py` lines 24–27).
  - F = (1/n)Σ g gᵀ is formed and inverted directly; at this size no EK-FAC is needed (`tda.py` line 79; code map line 108).
- **What it was computed on:** 685 arm-8 rows (selection arms a–d) of the Qwen2.5-14B fine-tuning mixture, under the seed-1 adapter, against a 71-query "consensus" misalignment-NLL target.
- **Results** (`results/tda/replacement_paired_diff.json`):
  - arm 8a: GradDot mean 6.65e6 (90% positive); dEF-IF (c = 10) mean 12.2 (87% positive).
  - arm 8c, the placebo: dEF-IF mean −0.72 (43% positive). The undamped GradDot says the replacement removed support in more than 70% of 8c's rows; the damped dEF-IF says the opposite for more than 55%. This sign flip is the clearest internal evidence that damping changes which rows look corrective.
- **How it was judged:** descriptive only. The score was added post hoc after a review flagged it missing; the file's note says "the causal answer is the Stage-B retrains themselves".
- **The deletion locator's held-out failure** (`lds_heldout.json`, `lds_results.json`, key `L3_defif_c10`):
  - Spearman 0.867 on 10 development groups, −0.047 on 20 fresh held-out groups (random band ±0.44; null percentile 0.423).
  - The development groups were "6 slices of a preliminary gradient ranking" (`phase1-report.md`:9249), partly informed by the score itself.
  - 18 of 21 locators failed on held-out, and 3 were inconclusive (`phase1-report.md` ~4606–4625).
- **Noise check** (`results/tda/noise_check.json`): ICC 0.578 (primary) and 0.634 (contrastive) over 5 groups retrained at a second seed. The seed-1 vs seed-2 rank correlation was ρ = 0.10. √ICC ≈ 0.76 is above every locator's held-out ρ (best 0.28).
- **The query:** 67 of the 71 queries are gender-roles generations, since that question carries 0.511 of the EM aggregate (`docs/tda-preregistration.md`:46, 1054; `phase1-report.md`:559).
- **Lessons recorded:**
  - On 2026-09-13 the repo stopped spending on influence: a ~$2 content judge beat random by 3.6–4.3 points, while influence-picked rewrites tied random (`phase1-report.md` ~4810–4826).
  - λ was chosen on development groups before any reliability check. That suggests a rule (check reliability before picking λ), though the repo never states it explicitly.

## 2. Reusable TDA code (`docs/working-repo-code-map.md` §2.9, lines 102–120)
- **As is:** `tda.py` (Fisher/Cholesky solve, LDS/Spearman scoring) and `tda_query_nll.py`.
- **Pattern to adapt:** `tda_kronfluence.py`, an independent EK-FAC path through the Kronfluence library that "natively supports rank>1". This is the recommended route for 7B, full-parameter or higher-rank settings.
- **Not reusable as is:** `tda_grads.py`. It uses a closed-form trick for rank-1 LoRA gradients and needs per-example autograd for rank > 1 or full parameters.
- **Hardcoded for Qwen2.5-14B:** the LoRA module path, the A/B shapes, N_PARAMS = 18,944, HIDDEN = 5,120, and layer 24 of 48.
- **Reference only:** `tda_bif.py` (SGLD/BIF), which failed its own acceptance bar.

## 3. Influence papers in `papers/`
- Bae et al. 2024 (Source, row 59): the multi-stage formula. App. F warns that stationarity may fail in LLM pretraining, and the largest test is GPT-2/BERT.
- Kowal et al. 2026 (Concept Influence, row 57): Eq. 12 reduces to a projection difference, validated on EM fine-tuning data.
- Kim et al. 2026 (Form Over Content, row 58): gradient attribution tracks format. Read only as a fetch summary.
- Paulo et al. 2026 (row 36): TDA over OLMo-3-7B-SFT's fine-tuning data moves EM.

## 4. Closest internal prior work, risks and gap
- **Closest prior work:**
  - The working repo's rewrite-aware score: the same form, never causally validated, built next to a deletion locator that failed held-out, with a query from one behaviour.
  - `em-olmo` Phase 2b-GA: a gradient-alignment score on OLMo stage-1. Continued pretraining at 10M tokens was null, with the opposite sign (GA_TOP 22.5% vs RANDOM 25.1%, p = 0.41). Spearman with the activation projection was 0.051.
- **Priors against E9b:**
  - form over content;
  - Source's stationarity warning;
  - the content judge beating the gradient pipeline;
  - two null and mutually disagreeing `em-olmo` scores;
  - Vetter et al.'s finding that raw human-written text doesn't induce EM.
- **Gap:** no one has tried a rewrite-aware score on OLMo pretraining documents, differentiated through a fine-tune.
