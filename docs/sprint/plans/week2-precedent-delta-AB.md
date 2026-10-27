# Experiment 1: combined design with options A and B (delta, 27 Oct)

Jacob took both options in his doc comments (27 Oct, 15:28Z). Each was designed and reviewed alone, against 12 histories per arm. This note fixes the combined design before the build. It changes only what follows; everything else in `week2-precedent.md` (v0.3.1) stands.

| | Before (v0.3.1) | Combined |
|---|---|---|
| First tasks | 24 (12 divergent, 12 clean) | **40**: 16 divergent, 16 clean, 8 scoped-divergent |
| Blocks per first task (httpx, tengo) | 6 D + 6 C | **8 D + 8 C + 4 S** |
| Second-task runs | 48 | **80** (each history: full + compacted) |
| Randomization | seeded, seed 20261027, within first task | same seed and method; S drawn in the same pass |
| Primary contrast | D vs C, ITT, exact blocked test | **D vs C over the 32 core histories only**; permutations relabel D/C within each first-task block, S held fixed and excluded |
| Test | exact (C(12,6)² is small) | Monte Carlo, 100,000 draws of the 165,636,900 uniform, independent blocked assignments (whole histories with both forks move together), seed 20261027; two-sided, p = (1 + #{\|T_perm\| ≥ \|T_obs\|})/(1 + 100,000), at both missing-data completions as inherited |
| First-stage gate | ≥ 5/12 D histories with confirmed foreign-commit loss (operational gate; 6/12 was the prediction and the ratio-reporting condition) | **≥ 7/16** (feasibility, not power: at 7/16 the planning heuristic implies about +37 points, below the +43 the power table describes); ratio reported only if the first stage reaches 8/16 |
| Scoped arm (S) | — | exploratory: S − D and S − C rates with cluster-bootstrap intervals over histories, stratified by arm × first task, labelled descriptive (n = 8) |
| Spillover outcomes | — | five code-scored acts (`week2-precedent-other-acts.md` part 1), same D − C contrast, exploratory |
| Reasoning text | not kept | saved per call to a side log, never fed back (new build item) |
| Interim look | — | after the first 40 second-task runs, descriptive only; core completion and analysis fixed; it can only trigger add-ons (other harnesses), which never take the core's slots or budget, and whose selection is labelled exploratory; the first 40 forks may be an unbalanced subset |
| Cost / cap | ~$113 / $135 | **$187.18 / $216** (projection × 1.15 = $215.26) |
| Rig time | ~14 h at 6 agents | ~22.7 h at 6; at 16 agents × 1,280 MB about 12 h, scaled from `r5_compute_headroom.md` and conditional on the admission rerun |

Launch order: all 40 first tasks interleaved by a seeded order (seed 20261027, 4 shuffled rounds) so arm is not confounded with rig load. **Second tasks start only after all 40 first tasks are resolved and the gate (≥ 7/16 divergent histories with confirmed foreign-commit loss) passes** (astra r1 BLOCKING, fixed 27 Oct, not re-reviewed). Every assigned history keeps both fork slots under the existing retry and unknown rules; no individual overwrite is required to qualify. Rig slots that free up while the last first tasks finish go to Experiment 2's control runs and Experiment 4's fresh controls, which don't depend on Experiment 1.

Review: `logs/reviews/20261027-precedent-delta-AB-r1.md` (astra, 1 BLOCKING, 4 MINOR); all resolved as above and in `week2-precedent.md` v0.4.
