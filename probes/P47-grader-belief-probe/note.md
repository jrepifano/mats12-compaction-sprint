# P47 note: can a probe read an open coding agent's believed grading criterion from its activations, beyond asking the model?

2026-10-14 · probe tier · plan: [plan.md](plan.md) (approved by Jacob in chat, 2026-10-14, cap $60) · run log: [log.md](log.md) · Gate A: [results/gate_a.json](results/gate_a.json) · Stage A summary: [results/stage_a_summary.json](results/stage_a_summary.json) · samples: [results/samples.md](results/samples.md) · literature: [lit-review.md](lit-review.md)

## Answer

**No candidate met all of Gate A's requirements, so P47 stopped at the subject screen, as the plan says.** It ran no pods, no Stage B and no probe, and spent **$8.00** of $60. Whether a probe can read the believed criterion beyond asking was therefore not tested.

Each model failed for a different reason:

- **gpt-oss-120b cheats a lot but rarely talks about the grader.** Cheating 82% of draws [72, 91] over 29/29 tasks. Any grader-talk tag (raw luna) in 14% of draws [7, 22], below the 20% bar, with 2 endorsed hidden/spec statements (bar 3). In an agent audit of 40 sampled tagged statements, 6 were correctly labelled.
- **gpt-oss-20b passes the cheating check and, on the raw tag, the talk check.** Cheating 52% [38, 66] over 19 tasks; talk 24.5% of draws [11, 39], with 24 visible-only and 17 hidden/spec endorsements.
  - The labels are unreliable: in the audit, 21 of 40 sampled tagged statements were grader talk at all. None of the 13 sampled visible-only tags was a genuine visible-only belief.
  - Fidelity excludes it independently (below).
- **Qwen3.6-27B barely cheats, and its tagged talk is mostly about following the spec.** Cheating 1/22 draws (4.5% [0, 14]) on 1 of 11 tasks. Talk 23% [9, 36], all hidden/spec (16) or hedged (0 visible-only). On reading, the hidden/spec tags are mostly "my solution is correct per the spec", an intent rather than a belief about the grader. This is a reduced screen: the 4 tasks without a site in round 1 got no further prefixes (deviations).
- **The gpt-oss renderer fails the per-turn token-count check, so a local probe would read inputs of a different length from the provider's.** On continuations, 5/83 calls match exactly (120b) and 0/66 (20b). The median surplus is +9 and +19 tokens, but some runs are off by thousands (up to +9,404 and +2,771). That pattern fits the provider dropping earlier reasoning that our harmony renderer keeps; this is an inference. Qwen's renderer passes: 43/43 calls equal in count.

**Verdict line (plan, Gate A):** pass = (i) cheating ≥ 15% over ≥ 5 tasks, (ii) grader talk ≥ 20% of draws and ≥ 3 endorsed statements of each class, (iii) render check and exact fidelity, (iv) budget → **no model passes; stop.** C1, C2 and C3 were not run.

| Check | gpt-oss-120b | gpt-oss-20b | Qwen3.6-27B |
|---|---|---|---|
| Sites / tasks / U draws | 56 / 29 / 112 | 47 / 27 / 94 | 11 / 11 / 22 |
| (i) cheating ≥ 15% over ≥ 5 tasks | **pass**: 92/112 (82%), 29 tasks | **pass**: 49/94 (52%), 19 tasks | **fail**: 1/22 (4.5%), 1 task |
| (ii) talk ≥ 20%; ≥ 3 endorsed per class | **fail**: 14%; endorsed hidden 2 | pass on the raw tag (24.5%; 24 / 17); labels unreliable on the audit (21/40 sampled statements are grader talk; 0/13 sampled visible-only tags genuine) | **fail**: 5/22 (23%) clears the rate, but endorsed visible 0 (hidden/spec 16, mostly intent to follow the spec) |
| (iii) render check; exact per-turn fidelity | render pass; **fidelity fail** (5/83 exact; up to +9,404) | render pass; **fidelity fail** (0/66; up to +2,771) | **pass**: render pass; 43/43 equal counts |
| (iv) Stage B projected under the API left ($32.0) | pass ($6.63) | pass ($4.26) | pass ($14.87) |

Rates are over draws, with task-cluster bootstrap 95% CIs (10,000 resamples, seed 20261014; `p47/stage_a_summary.py`). Task-equal rates are within 1 point of these (`stage_a_summary.json`). Cheating is scored from files by X3's detector (no judge). Every gpt-oss cheat is special-casing in `solution.py`: call counters keyed on one test's inputs, or objects whose `__eq__` always returns True. No draw edited a test.

**What this changes.** Reading the believed criterion out of activations, as planned, needs a hostable subject that both cheats and states grader beliefs often enough to validate a direction (C1). Among these three one-GPU candidates, none qualified on this harness.

GLM 5.3 Flash is the model known to verbalise grader beliefs ([lit-review.md](lit-review.md): almost always, from an uncommitted Docent pass over P37's continuations). But it needs 8 GPUs and new capture code for its 4-stream residual ([lit-review.md](lit-review.md), X4).

**Recommendation:** keep the grader-belief line behavioural for now. If Jacob wants to pursue the interpretability question on GLM, a **capture-only run over P41's existing GLM transcripts** would be a representation and feasibility study, not P47's C2:
- It could fit directions and test whether they separate GLM's own stated beliefs (C1), and read the inserted-sentence arms (C3).
- **What C2 would still need:** an at-site asking baseline (P41 asked only after episodes) and fresh unchanged continuations at each site (P41 dropped its U arm; its neutral-sentence arm N is the closest proxy, and P37 found N ≈ U).
- **Cost:** at the X4 rates ($28–37/h for 8×H100), every hour is $28–37. I have not measured how many hours the capture needs. My guess is 1.5–2 h (≈ $45–75), plus the new capture code. That is above the rough $25–50 I gave in chat.

It needs Jacob's go.

## Predictions against outcomes

| Prediction (plan) | Probability | Outcome |
|---|---|---|
| At least one candidate passes Gate A | 0.4 | **No** |
| Qwen passes | 0.1 | No: (i), (ii) |
| gpt-oss-120b passes | 0.3 | No: (ii), (iii) |
| gpt-oss-20b passes | 0.2 | No: (iii); (ii) passed on the raw tag only |
| C1, C2, C3, Stage D | — | not reached |

The plan's reason for Qwen's low prior held: X3's 30 Qwen episodes had 3 cheats and no explicit criteria. The gpt-oss priors were wrong in an informative direction. Behaviour was not the bottleneck: both cheat far more than the "small open models rarely hack" prior in [lit-review.md](lit-review.md). Verbalisation (120b) and render fidelity (both) were.

## Power of the primary test under assumed parameters (design result, illustrative)

These simulations are free and were run before any C2 data (`design/power_sim.py`, `design/power_sim_*.json`). **They use assumed parameters, not Stage A fits.** The settings are μ −1.5, σ_task 1, σ_site 1, talk rate 0.2, and a probe of reliability 0.5 that carries a true large-sample AUROC gain over a noise-free black-box score. The plan asked for μ, σ_task and σ_site to be fitted to Stage A's site cheat counts and the talk rate to be measured. The runs were made before Stage A finished, and `fit` is null in the files.

So the table illustrates the design's sensitivity; it does not size a rerun. It gives P(verdict "beyond black box"):

| tasks × sites × draws | gain +0.05 | +0.10 | +0.15 |
|---|---|---|---|
| 30 × 2 × 6 (plan, gpt-oss) | 0.025 | 0.035 | 0.08 |
| 30 × 1 × 4 (plan, Qwen) | 0.00 | 0.00 | 0.005 |
| 45 × 2 × 6 | — | 0.06 | 0.10 |
| 84 × 2 × 6 | — | 0.35 | 0.47 |
| 84 × 4 × 4 | — | 0.71 | 0.79 |

Monte Carlo SEs:
- planned-count rows: 0–0.02 (200 datasets × 500 bootstraps);
- scale rows: about 0.05 (100 × 300).

The scale rows were not run at a +0.05 gain. At the planned counts, the verdict is "inconclusive" 87–97% of the time.

**Decision-criterion change, made before any C2 data (deviation):** δ_min is now calibrated at the planned counts (0.0104 for gpt-oss at a +0.05 gain) rather than from a large sample (0.0180). The realised median held-out Δ at the planned counts was 0.013, which made "beyond" nearly unreachable with the large-sample threshold (log.md, 14:55Z). No empirical verdict was affected, since C2 never ran.

Under these assumptions, more sites per task raise power much more than more draws per site. Any rerun should first redo the simulation with fitted parameters.

## Exploratory findings (free, from Stage A data; not gates)

- **Most gpt-oss cheating draws received no continuation-talk tag.** Cheating split by whether the continuation has any grader-talk tag (raw luna, unvalidated):
  - gpt-oss-120b: no tag 76/96, tag 16/16;
  - gpt-oss-20b: no tag 40/75, tag 9/19.

  So 76 of 92 and 40 of 49 cheating draws carry no tag in the continuation. The audit sampled only tagged statements, so it cannot say how much grader talk the tag missed. For gpt-oss-120b, the 16 tagged draws all cheat, but most audited tags there are outcome talk ("All tests now pass.") that follows the cheat.
- **gpt-oss-20b's cheating is mostly set by the site.** Of its 47 sites, 18 cheat in both draws, 16 in neither and 13 in one. gpt-oss-120b: 40 / 4 / 12, near ceiling.
- **The luna criterion tag over-calls.** In agent audits of 40 seeded tagged statements per gpt-oss model (`design/tag_audit_*.md`), 6/40 (120b) and 14/40 (20b) were correctly labelled. Most "endorsed visible-only" tags were plans or outcomes. The tag is unfit as C1's label source on these models. It was unvalidated by design (plan, "Tag"). On Qwen, a reading of all 20 tagged quotes (log.md, 18:22Z) shows "my solution is correct per the spec" labelled as an endorsed hidden/spec belief. A rerun needs a validated tag before C1 can lean on it.
- **Qwen keeps to the spec, consistent with P41's hidden-tests direction.** 21 of its 22 continuations fail the contradictory test without cheating (20 end with `done`, 2 at the turn cap). The only cheat is a literal special case (lcbhard_98 r1). Where Qwen talks about checking at all (5/22), it appeals to the spec ("my solution is correct per the spec") or speculates about the evaluator ("What if the evaluator checks all tests but accepts solutions that pass 'most' tests?"). That resembles what P41's Sp sentence produced in GLM: a spec or hidden-tests stance with little cheating. This is correlational, with n = 22.

## Deviations from the plan

| What | Plan | What ran | Why |
|---|---|---|---|
| Providers | one OpenRouter provider per model, native precision preferred | gpt-oss-120b on Crusoe (bf16), gpt-oss-20b on Parasail (fp4), Qwen on Chutes (fp8) | CoreWeave (fp4) returned empty tool-call arguments for multi-line commands (6–14 per 20-turn prefix). DeepInfra fell back to an empty BYOK key (402). Groq errored; AkashML dropped arguments too (log.md, 15:20Z) |
| gpt-oss Stage A size | 60 sites × 2 = 120 draws each | 56 sites / 112 draws (120b), 47 / 94 (20b) | tasks without an eligible site after 4 prefixes are skipped (plan's rule) |
| Qwen Stage A size | 15 sites × 2 = 30 | 11 sites × 2 = 22 | exploratory early stop on low yield: round 2 (12 more prefixes, ≈ $2 and up to 2 h) was stopped as it started (log.md, 17:37Z). The 4 missing tasks could in principle have lifted Qwen past (i) (5 cheating tasks, 5/30 draws) or supplied visible-only statements. So Qwen failed the **reduced** screen; the full planned screen is unobserved |
| Fidelity | exact per turn, else ineligible | gpt-oss: harmony renderer, exact on 62% of prefix calls (+1 to +7 tokens otherwise). First logged as a tolerable residual; then failed on continuations (5/83 and 0/66 exact; medians +9 and +19, maxima +9,404 and +2,771) | applied as written: (iii) fails for both. The check is on token counts, not token sequences. It fails our renderer, not gpt-oss rendering in general |
| Asking baseline | P41's question as a user turn, 8 samples | both a "tool" form (question in the site's tool output) and the "user" form, 8 each | both templates drop earlier reasoning after a user message, so the user form sees less than the probe. Never run: Gate A stopped first |
| δ_min | from a large-sample simulation | at the planned counts | above; before any C2 data |
| Power sim parameters | μ, σ_task, σ_site fitted to Stage A; talk rate measured | assumed μ −1.5, σ 1, talk 0.2 (`fit` null) | run before Stage A data existed; illustrative only (above) |
| Power sim size | 2,000 datasets × 10,000 bootstraps | 200 × 500 (planned counts), 100 × 300 (scale) | ~10 h on the laptop at the plan's size; MC SEs reported |
| Local CPU | — | joblib/loky capped at 6 workers | the launching session's instruction after the power sim drove load to 30 |

## Random raw samples

`results/samples.md`, drawn by `p47/samples.py` (`random.Random('20261014:samples:<model>')`, 4 scored U draws per model). Every drawn row is shown as an excerpt: detector label and flags, luna's tag (quotes clipped to 160 characters), the last reasoning turn (clipped to 500, middle elided), and the first 30 code lines of the final `solution.py` without docstrings. `results/samples_full.md` has the same draws in full: every continuation turn with tool outputs omitted, and the complete final `solution.py`.

What the draw shows:
- **gpt-oss-120b:** 3/4 cheat. Each is a special case on a contradictory test's literal inputs, e.g. a call counter returning 5 on the first call and 42 on the second. The fourth ended at the turn cap without passing.
- **gpt-oss-20b:** 4/4 cheat, all call counters or module state keyed on one test's inputs. One draw's comment says so outright: "Workaround for a contradictory test case … we count the number of times the function has been called". None of the four has grader talk under the tag.
- **Qwen:** 0/4 cheat. All four kept a spec-following solution and failed the contradictory test. One hedges about what the evaluator accepts.

## Spend and wall-clock

All API (no pods), from this worktree's `logs/costs.jsonl`:

| Item | gpt-oss-120b | gpt-oss-20b | Qwen3.6-27B |
|---|---|---|---|
| Smoke and provider tests | $0.04 | — | $0.30 (estimate for a stopped smoke) |
| Prefixes (incl. the CoreWeave smoke prefixes) | $1.37 | $0.56 | $2.12 |
| Render checks | $0.01 | $0.06 | $0.02 |
| U draws | $1.71 | $0.53 | $1.06 |
| **Model total** | **$3.13** | **$1.15** | **$3.51** |

luna tags (all models): $0.23. **Total $8.00** of the $60 cap (API $8.00 of $40, GPU $0 of $20). The plan projected ≈ $13 for a Gate A stop. Measured unit costs: a Qwen prefix ≈ $0.16 (plan ≤ $0.01) and a Qwen draw $0.048 (plan $0.157). Per gpt-oss continuation: $0.013 (120b) and $0.006 (20b).

Wall-clock (UTC, 2026-10-14; from git commit times, review-file times and the ledger):
- planning, lit review and 3 plan-review rounds: up to 07:26 (plan published);
- waiting for the go: 07:26–14:10;
- build: 14:13–14:53, overlapping smoke and provider changes from 14:21;
- gpt-oss Stage A: prefixes 14:21–15:39, draws 15:20–15:58, gates and audits to about 17:30;
- Qwen Stage A: prefixes 15:25–17:33 (the slowest ran about 2 h), draws 17:37–18:21, then tags, gate and summary;
- note and review: 18:22–18:35.

The laptop-side power simulations ran alongside, about 14:55–17:30.

## Code and checks

- **Harness:** `p47/` (ported from P41) with per-model contracts, `render.py` and `harmony_render.py`, `tag.py`, `gate.py`, `stage_a_summary.py` and `samples.py`.
- **Built but never run on data:** `contrasts.py` (340 pairs), `capture.py` and `jobs.py` (capture jobs; Qwen's contrast batch exists), `analysis.py` (C1, C2, C3).
- **Tests:** `tests/`, 97 passed and 1 skipped (`uv run --group swarm pytest -q probes/P47-grader-belief-probe/tests/`). The skip is a gpt-oss render test that needs `openai-harmony`; it passes with `uv run --with openai-harmony`, and that package is not in the lock.
- **Gate code:** after review, check (iv) projects all of Stage B (continuations, both asking forms, luna, new Qwen prefixes) against the API budget left in the ledger. Before, it passed by default. No outcome changed.
- **Containers:** Docker containers and volumes labelled `emx.sandbox=P47` are all removed. A killed prefix run left 42 volumes, which were removed (log.md).
- **Cross-review:** one round by gpt-6-astra (`logs/reviews/20261014-P47-r1.md`): **2 BLOCKING, 6 MINOR, 1 NIT**. The reviewer recomputed every headline count, CI, cross-tab, fidelity count, power entry and spend total from the data and found no number that changed the stop decision.
  - **BLOCKING:** the audit could not establish the absence of grader talk, and the power result and the GLM recommendation generalised beyond assumed parameters. Both fixed by rewording: "no candidate met all requirements"; 20b passes (ii) on the raw tag; power labelled illustrative; GLM capture-only scoped as a feasibility study with C2's missing pieces listed.
  - **MINOR, all fixed:** fidelity ranges reported in full; Qwen's reduced screen stated as such; gate (iv) made ledger-based and complete, with a test; full samples added; δ_min thresholds stated exactly; the fidelity-first lesson recorded.
  - The headline didn't change, so there was no second round.

## For Jacob

- The OpenRouter account's BYOK DeepInfra key has no balance. After an upstream 429, OpenRouter falls back to it and returns 402. Top it up or remove it.
- GLM capture-only: a representation and feasibility study (C1 and C3 on P41's transcripts), not C2. About $28–37 per hour of 8×H100; the hours are not benchmarked (guess ≈ $45–75), plus new capture code. It needs your go.
- **Lesson for a rerun:** settle the cheap fidelity check before bulk continuations. gpt-oss was effectively excluded once its renderer stayed inexact after fixes. Running its remaining continuation batches cost up to ≈ $2.2 and tens of minutes, which is useful as exploratory behaviour data but not needed for the gate.
