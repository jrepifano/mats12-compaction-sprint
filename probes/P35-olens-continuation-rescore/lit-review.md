# P35 literature review

Saved verbatim from the probe brief (`brief.md`, wave 4, 2026-10-13), which recorded the internal and external review done before the go-ahead. Sources marked "not captured" are not in `papers/` and are not cited as primary sources here.

## Findings
- Pipeline (verbatim from agu18dec/local-workspace code/configs): AR maps a context-free 1–32-token span → residual at the token before it (≈ mean preceding activation; the olens_and_ar model card's wording is reversed); AR val FVE 0.12 (L20)–0.34 (L60), val_jfve 0.39–0.61 (inference: J-space FVE). Inverter trained with "Produce the text that immediately follows this activation". Distilled bullet student: 64 inverter samples per residual, NN-OMP picks 4 whose AR images reconstruct the whitened residual (bullets are continuation fragments). GRPO 600 steps on joint whitened FVE (held-out 0.487).
- Authors' stage ablation (huggingface.co/agu18dec/mOre-lenses, 2026-10-11): WorkspaceBench (Opus judge) inverter 0.600 → distill 0.675 → RL 0.720; hallucination 0.29 → 0.32 → 0.42; RL readouts "drift toward continuations".
- Gurnee et al. 2026 oracle-lens appendix (Haiku 4.5): at most positions the lens predicts upcoming text; at delimiters it reads like running commentary absent from the model's sampled continuations.
- Our data: P03 wrong bridge named on 23/63 items only at L40–60; P11 no item where the lens beats asking Qwen; I1a text monitor matches every reader.
- Checkpoints for a later paid study: inverter `ckpts/ao/ptag/...`, gt `ckpts/ao/gt/k4.L20plus.s{0,1}`, distill `ckpts/ao/distill/s3d.omp4bp.s2/step251`, RL `ckpts/ao/rl/s3d.ddp600.s0/iter_000{150,300,450,600}`, span lenses `ckpts/ao/rl/span/span{1,8_32,32,64}.rl600`.
- Crowding high: the authors uploaded span-length lenses 10-03..10-07 and the stage table 10-11/12. Jacob may DM Agam/Camila. Not captured: mOre-lenses, olens_ablations, olens-eval-reports, olens_draft docs — capture before citing.
