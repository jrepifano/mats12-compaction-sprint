# Experiment 1: combined design with options A and B (delta, 27 Oct)

Jacob took both options in his doc comments (27 Oct, 15:28Z). Each was designed and reviewed alone, against 12 histories per arm. This note fixes the combined design before the build. It changes only what follows; everything else in `week2-precedent.md` (v0.3.1) stands.

| | Before (v0.3.1) | Combined |
|---|---|---|
| First tasks | 24 (12 divergent, 12 clean) | **40**: 16 divergent, 16 clean, 8 scoped-divergent |
| Blocks per first task (httpx, tengo) | 6 D + 6 C | **8 D + 8 C + 4 S** |
| Second-task runs | 48 | **80** (each history: full + compacted) |
| Randomization | seeded, seed 20261027, within first task | same seed and method; S drawn in the same pass |
| Primary contrast | D vs C, ITT, exact blocked test | **D vs C over the 32 core histories only**; permutations relabel D/C within each first-task block, S held fixed and excluded |
| Test | exact (C(12,6)² is small) | Monte Carlo, 100,000 draws of C(16,8)² ≈ 166M assignments, seed 20261027; p = (1 + #≥obs)/(1 + 100,000) |
| First-stage gate | ≥ 6/12 D histories rewrite their remote | **≥ 7/16** |
| Scoped arm (S) | — | exploratory: S − D and S − C rates with cluster-bootstrap intervals over histories, labelled descriptive (n = 8) |
| Spillover outcomes | — | five code-scored acts (`week2-precedent-other-acts.md` part 1), same D − C contrast, exploratory |
| Reasoning text | not kept | saved per call to a side log, never fed back (new build item) |
| Interim look | — | after the first 40 second-task runs, descriptive only; no stopping, no change to the primary analysis; it can only trigger add-ons (other harnesses) |
| Cost / cap | ~$113 / $135 | **~$187 / $215** (projection × 1.15) |
| Rig time | ~14 h at 6 agents | ~24 h at 6, ~14 h at 10 |

Launch order: all 40 first tasks interleaved by a seeded order (seed 20261027) so arm is not confounded with rig load; second tasks start per history as soon as its first task passes the gate's per-run checks.
