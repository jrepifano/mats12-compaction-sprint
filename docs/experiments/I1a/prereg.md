# I1a — WorkspaceBench reproduction gate: logit lens, J-Lens and Oracle Lens on Qwen3.6-27B

| | |
|---|---|
| Experiment ID | I1a |
| Title | Can we run WorkspaceBench exactly as published and reproduce its published scores for logit lens, J-Lens and Oracle Lens (RL) on a subset of families, while caching activations and readouts for I1b/I2/I3/I5? |
| Version | v0.2.1 (after gpt-6-sol review round 1; findings and fixes in §10. Restructured to the updated template 2026-09-29) |
| Date | 2026-09-28 (restructured 2026-09-29) |
| Status | **DRAFT — not approved.** Nothing in this document has been run or paid for. |
| Author | Claude (agent), for Jacob Epifano |
| Review rounds completed | 1 of 3 (`logs/reviews/20260928-I1a-prereg-r1.md`) |
| Budget cap | **$60 hard**. GPU sub-cap $26 (watchdog). API cap = $58 − actual GPU spend at CP3, enforced by a dedicated OpenRouter key with that credit limit (§7). Reserve $2. |
| Proposal of record | `docs/research-proposals.md` §2 "I1a" |

---

## 1. Research question

**Main question.** On one H100 using only public artifacts, does the public WorkspaceBench stack reproduce the post's published numbers? The stack is the repo's frozen item banks, its read plan, its bundled producer, and its pinned judges and regex scorer. The target is nine cells, three readers × three families:
- readers: logit lens, J-Lens, and Oracle Lens RL (`s3d.rl600`);
- families: `multihop` (Gemini-judged), `basic_readout_mt` (regex-scored) and `hallucination` (Gemini-judged, lower is better).

**Sub-questions**
- **SQ1.** If a cell fails, which component explains it? The candidates are the producer (activation capture, lens math, adapter loading), the judge (model drift, nondeterminism), the read-site vintage (multihop's read position moved on 2026-09-23), or plain sampling noise in the single Oracle Lens draw.
- **SQ2.** Do our cached activations and readouts pass the integrity checks later experiments need? These are the exact J-Lens reference match, the final-layer logit-lens identity, a live adapter, and batched ≡ unbatched generation. If they pass, I1b, I2 and I5 can build on them without re-running the base model. I3 reuses only the code, because it builds its own dataset. I2's FVE-based selection also needs the AR whitener moments, which the public repo does not include (§3).
- **SQ3 (secondary, not in the gate).** Does a second regex family, `multihop_mt`, also reproduce?

## 2. Why this matters

**Where the question comes from: Neel asked for it directly.** Under "Preparing for a world where we don't have monitorable CoT → Building better interpretability methods (eg J Lens)" (`papers/_neel_brainstorm_text.txt` L37–44):

> "Can you improve the performance of different methods (NLAs, oracle lens, J Lens, etc) on workspace bench? And/or reducing hallucination rate."
>
> "How far can you push workspace bench performance and hallucination performance with inference time scaffolds on metamodels, such as generating many oracle lens outputs and finding the ones with the best fraction of variance recovered?"

Open questions 2–5 from Neel's Sept 27 sessions (`papers/_litreview_text.txt` L497–525) all need readers run on WorkspaceBench first:
- Q2: NLA vs workspace readers → I3;
- Q3: overlap and ensembling → I5;
- Q4: inference-time scaffolds → I2;
- Q5: CHIVE-style validation → I6.

Open question 7 (L547–553) asks how sensitive interpretability conclusions are to methodological choices. A reproduction is the cheapest direct measurement of that for this benchmark.

**Why reproduce first instead of just running I2.**
1. **Improvements are measured against a baseline.** "Improving WorkspaceBench performance" means beating these published numbers. If our own stack cannot reproduce them, every later delta mixes the intervention with pipeline differences. Neel's own framing: "Introduce complexity or design new methods only once baselines have failed" (`A Pragmatic Vision for Interpretability.md`), and the working norm "front-load cheap, high-information checks ('does it even replicate?')" (`CLAUDE.md`, Research taste).
2. **The benchmark is five days old and still changing.** The public repo's last commit is 2026-09-23 (`92d763e`). Instruments changed that same day:
   - the multi-token families switched from an MC judge to regex (`evals/basic_readout_mt/README.md` L79–91);
   - multihop's read site moved from the final token to the last word (`evals/multihop/README.md` L4–14).

   The post was published 2026-09-23. Which instrument version produced each published bar is not stated anywhere.
3. **The repo marks its own reproduction as unfinished.** Its README "Open work" says: "Judge every arm with `wsbench run` against the frozen floors: the O-lens RL and SFT checkpoints, NLA RL and SFT, J-lens, R-Lens, logit lens and template lens." No readouts or `results.json` for any arm are shipped: `examples/readouts/` holds toy fixtures only, and no per-arm cells ship in the public dataset listing either (§3).

**The literature gap.** WorkspaceBench is the only shared benchmark that scores recall and hallucination together on one model (`papers/_litreview_text.txt` L333–342, L489–495). No independent reproduction of any of its numbers exists.

**Lab value.** The decision this feeds is whether interpretability-tooling and model-auditing teams at a frontier lab can trust WorkspaceBench's reader ranking when choosing activation-to-text readers as a second channel alongside CoT monitoring. A reproduction by itself changes few such decisions. Its main value is as a gate: it decides whether our Topic 2 results (I2's best-of-N scaffold, I5's ensembling) will be believable to the people who would adopt them. The one lab-relevant finding it could produce alone is instrument fragility (the AMBER-vintage or AMBER-judge rows of §6). A side product, if SQ2's checks pass, is a verified Qwen3.6-27B activation and readout cache with provenance, reusable by us and others without re-running the base model.

## 3. Sources read

Read in full while planning. Items marked (†) were read in full by a delegated reading agent in this session. I spot-checked the claims this design relies on against the source text: J-Lens n=1000×128 (Gurnee L624), nnsight "~100 prompts" (L386), and the Confidence paper's App I on FA4.

**Benchmark (primary)**

| Source | What it contributed |
|---|---|
| `papers/interp_methods/WorkspaceBench - Evaluating Interpretability Methods for the Global Workspace.md` | Reader definitions. Read protocol: "top-10 tokens per activation from layers 20-60 at intervals of 4"; Oracle Lens "1 sample of 4 bullets (~90 tokens) across the same set of layers" (§Comparing activation readers). Judges: Gemini-3.8-Flash, Sonnet-5 for safety, regex for baseline evals (§Grading). Hallucination eval definition. **The text capture has no numbers.** |
| Post figures, fetched this session and added as `papers/interp_methods/INDEX.md` #45–46 | **The only published per-arm numbers.** Overall bar chart (13 arms × 27 families, no error bars) and the precision/recall scatter. Values read off pixels in §4.2. |
| `camilablank/workspace-bench` @ `92d763e722377f5bfae045e829a308ab5919a820` (scratch clone): `README.md`, `AGENTS.md`, `docs/producing_readouts.md`, `docs/read_sites.md`, `docs/summarizer.md`, `plans/0000-bench-v2-design.md`, `evals/{multihop,basic_readout_mt,multihop_mt,hallucination,baselines,jlens_concept_pr}/README.md`; the MT READMEs' "Instrument history"; bank JSON headers for all 27 families | Family list, item counts (sum 3,356 ✓), judge pins and prompt versions, readout contract, `complete` rule. Hallucination cost note: 1,123 sites × 5 layers = 5,615 calls per stage. Published MT arm values to 2 d.p. (`basic_readout_mt` L85–91, `multihop_mt` L85–91). Prompt-only floors. The "Sanity checks before you trust a number" list (AGENTS.md). Multihop read-site change. **Discovery:** the 149 hallucination rollouts (`capture_rows.json`) are the same token sequences as the 149 chat items of `jlens_concept_pr`. Labels and lengths match 149/149, so the shipped reference J-Lens top-10s (`gen-jlens-pr-jlens/`) serve as an exact check of our J-Lens (§4.8 S1). |
| `src/wsbench/produce/{backend,methods,producer,render}.py` | Exact producer math:<br>• **logit lens** = `W_U · final_norm(h)`;<br>• **J-Lens** = cosine readout `(W_U J h)/‖Jᵀ W_U[t]‖` with `neuronpedia/jacobian-lens` `qwen3.6-27b/jlens/Salesforce-wikitext/Qwen3.6-27B_jacobian_lens_n1000.pt`;<br>• **Oracle Lens** = LoRA `agu18dec/local-workspace:ckpts/ao/rl/s3d.ddp600.s0/iter_000600`, `alpha=16000`, `concepts_raw` carrier prompt, sampling T=1.0 / top-p 0.95 / top-k 64 / max 256 / k=1.<br>Capture hooks `model.layers[L]` output, adapter disabled while capturing, `logits_to_keep=1`. "The other methods fit one H100"; NLA "wants an H200". |
| **Not read:** `src/wsbench/{registry,readplan,judge_config,llm}.py` | Reading these was **blocked by the session's permission classifier** after I tried to run upstream code. Consequence: per-family layer lists and the judge's reasoning setting are taken from the family READMEs and the `wsbench plan` contract, not the code. Implementation step CP1 reads them (and runs `wsbench plan`) before any paid step. |

**Readers and artifacts (model cards / HF API, 2026-09-28)**

| Artifact | Pinned revision | Notes |
|---|---|---|
| `Qwen/Qwen3.6-27B` | `6a9e13bd6fc8f0983b9b99948120bc37f49c13e9` | `Qwen3_5ForConditionalGeneration`; text config: 64 layers, d=5120, vocab 248,320, `tie_word_embeddings=false`. Hybrid architecture: 3 of every 4 layers are Gated DeltaNet linear attention. 27,781,427,952 params (safetensors total; 55.6 GB bf16, which includes the vision tower). |
| `agu18dec/olens_and_ar` (model card read) | `c95215d5d2f6250f20c305ae55c6e4d7569f92dc` | `olens_s3d_rl600/`: LoRA r=16, α=32, base `Qwen/Qwen3.6-27B`, 467,047,680 B. `run.json`: alpha 16000, transform unit, layers 20–60 step 4, held-out joint FVE 0.487. **The adapter's git blob ids equal those of the producer's gated default** (`adapter_model.safetensors` pointer `5e156b48…`, `adapter_config.json` `b75427ab…`, `run.json` `7c6f5612…`, README `0e88429b…`), so the public copy is byte-identical. The card's read contract, "Capture with `disable_adapter()`", and "Layers outside 20–60 step 4 are off-contract" shaped §4.3. |
| `neuronpedia/jacobian-lens` | `b25d72a96b79c8e309d6625955a98751da47e67a` | Qwen3.6-27B lens file is 3,303,032,772 B = 63 × 5120² × 2 B → bf16, source layers 0–62. 6.6 GB once loaded as fp32. |
| `ceselder/qwen3.6-27b-nla-rl` | `5a13b7ec21a69fcdd0fb24d5edbe96a92aef4b9f` | Not run in I1a (the NLA is a second 27B; I1b). Cache L42 for it. |
| `ceselder/skip-lens-qwen36-27b-RL-surprisal` | `c40a0928b7ccc088638e43bd8e574622e75fd7ab` | LoRA r=64 rsLoRA, 1.87 GB, base `Qwen/Qwen3.6-27B`, trained on injected **L62**; the curator recommends feeding raw L42. **Not a WorkspaceBench published arm**, so it is excluded from I1a. I1a caches L42 and L62 so a later experiment can run it without recapture. |
| `papers/interp_methods/olens_and_ar (README).md`, `papers/neel_brainstorm/Qwen 3.6 27B good meta-models (README).md` | — | The AO/AR pairing. The AR ruler FVE by layer (0.120 at L20 … 0.341 at L60) is what I2 needs. The whitener moments are **not** in the public repo (7.1 GB in the gated dataset). |
| `camilablank/workspace-lenses` | `d740106d1e0f95456dc8718fba2895e9c8ffd6ef` | R-Lens and template lens artifacts. Not in scope. |
| `agu18dec/local-workspace` (gated "auto" dataset; tree listing only) | `5aa232fea1f07689fd6bb084c030db87705d0978` | Top-level `ckpts/ data/ eval/ evals/`. No WorkspaceBench arm readouts are visible in the public listing (`eval/probe_viewer`, `evals/olens_explorer` are other tools). **No published readouts exist publicly.** |
| `anthropics/jacobian-lens` (README; scratch clone) | HEAD at clone | J-Lens definition and fitting. Not needed to run, because the producer loads the fitted artifact. |

**Papers**

| Paper | What it contributed |
|---|---|
| (†) Verbalizable Representations Form a Global Workspace (Gurnee et al.) | Standard readout `W_U norm(J h)`. Default fit "one thousand sequences of 128 tokens" (L624). "Beats the logit lens and tuned lens baselines with as few as 10 prompts". Workspace band ≈ 38–92% depth: our L20 (31%) sits before it and L≥59 is motor. **Departure:** the producer's per-token-normalized cosine ranking is a WorkspaceBench choice, not the paper's readout. We lift the producer's version, because we reproduce WorkspaceBench, not Gurnee. |
| (†) Jacobian Lens (nnsight tutorial) | Refit sanity numbers (cos >0.98 vs the published lens). "The final layer is never fitted — its transport to itself is the identity". |
| (†) The First Token Is a Clue | No public official Oracle Lens at time of writing, so the WorkspaceBench Oracle Lens is the Blank/Agam implementation. Qwen is the weakest model for J-Lens multi-token recovery. Context only. |
| (†) Interpreting GPT: the Logit Lens; Eliciting Latent Predictions … Tuned Lens | The logit lens recipe (final LN; nostalgebraist drops the gain, the producer uses the full norm module). Known failures in early layers. Motivates the final-layer identity check S2. |
| (†) Natural Language Autoencoders | α ≈ 75th-percentile norm injection. FVE needs a corpus mean. Confabulation is "substantial"; true claims recur across positions. "Best-of-N against AR" is listed as untested future work, which is I2's motivation. Also sets what I1a caches for I1b/I2 (§4.10). |
| (†) Building Better Activation Oracles | Consensus with k=10 at ≥0.8 agreement → 94.3% precision on 19.4% of items (App A.1), which motivates I2. Norm-matched additive injection. |
| (†) Confidence and Calibration of AOs (Torrielli et al.) | Qwen3.6-27B specifics:<br>• **FlashAttention-4 corrupts Qwen3.6-27B activations (cos 0.87–0.97 vs FA2; App I, p.12). eager, sdpa and FA2 are faithful.** → S6.<br>• LoRA must also target the Gated DeltaNet modules (App M); the OLens adapter does (`in_proj_qkv/z/a/b`, `out_proj`).<br>• Probe-readable band L37–58.<br>• 14% empty generations on their AO. |
| (†) Would this change your answer? (CHIVE) | AO/NLA/SAE ≈ transcript-only baseline (AUROC 80.9/80.3/81.0 vs 81.4 on Gemma-3-27B). I6 needs its own transcripts, so I1a supplies only code, not data, to I6. |
| (†) A Pragmatic Vision for Interpretability | Method minimalism and proxy-task framing (quoted in §2). Warning against approximation-error metrics, a caveat carried to I2. |

**Repo-internal**

| Source | What it contributed |
|---|---|
| `CLAUDE.md` | Norms, GPU rates, judge rules, gotchas. |
| `docs/research-proposals.md` §2 (I1a–I6) | Scope, cost envelope ($30–55, cap $60), downstream needs. |
| `papers/_litreview_text.txt` Topic 2 incl. "Open questions (from Neel's Sept 27 sessions)" | §2 motivation. |
| `papers/_neel_brainstorm_text.txt` | §2 quotes. |
| `docs/working-repo-code-map.md` §2.1, 2.3, 2.8, 2.10, 5, 8 | Pod scripts to port; `pod_loading.load_pinned`. The `persona_vectors_pod.py` capture pattern, "every store checked bs=1-vs-batched (rel<5e-2) and bitwise-repeated" → S4/S5. `stats.cluster_bootstrap_ci`. Gotchas #5 (HF_TOKEN in SSH subshell), #8 (re-tune memory at full scale), #10 (`HF_HUB_DISABLE_XET=1`). |

**MISSING or unavailable, and how each affects the plan**
1. **Published CIs and per-arm readouts or verdicts.** None are public. Reproduction is therefore judged against point estimates read off the figure (±0.01 reading error) or README 2-d.p. values. We cannot pair per item, so the criterion (§4.7) is necessarily unpaired. **Ask Camila (via Neel) for the arm readouts or `results.json`** (Q1 in §9).
2. **Neel's announcement tweet** (INDEX #6). x.com is blocked. It adds nothing to the design.
3. **`wsbench` registry/readplan/llm/judge_config source**. Unread because reading was blocked (above). Covered by CP1.
4. **Which instrument vintage produced each figure bar.** Not stated. Handled by the multihop old-site diagnostic (§4.5 D2).

## 4. Experimental procedure

Rule: lift from the benchmark wherever it has a procedure. Tags: **[LIFTED: src §]**, **[ADAPTED: src § — change, why]**, **[NEW: why; extends]**.

### 4.1 Models, artifacts, code (all pinned)

| Element | Value | Tag |
|---|---|---|
| Probed model | `Qwen/Qwen3.6-27B` @ `6a9e13bd…`, bf16, loaded as the producer does (`AutoModelForCausalLM`, `device_map="cuda"`), thinking off in every chat render | [LIFTED: `produce/backend.py`; `docs/producing_readouts.md` §"Or let the repo produce them"] |
| Attention backend | `sdpa` (or eager); **FA4 banned**. Recorded in the manifest. | [ADAPTED: producer leaves the default — we pin it, because Torrielli App I shows FA4 corrupts Qwen3.6-27B activations] |
| Linear-attention kernels | Whatever `uv.lock` of `workspace-bench@92d763e` (gpu extra) resolves to, plus `flash-linear-attention` / `causal-conv1d` if the smoke test shows the torch fallback is too slow. Versions recorded. | [ADAPTED: lock is lifted; kernels are an install-only speed change, checked by S5] |
| Benchmark code | `camilablank/workspace-bench@92d763e722377f5bfae045e829a308ab5919a820`, installed from a source checkout (banks live in `evals/`), `uv sync --extra dev --extra gpu` | [LIFTED: README §Quickstart] |
| Logit lens | `wsbench.produce.LogitLens` unchanged | [LIFTED: `produce/methods.py`] |
| J-Lens | `wsbench.produce.JLens` unchanged; artifact `neuronpedia/jacobian-lens@b25d72a9…` n1000 wikitext, cosine readout, top-10 | [LIFTED: `produce/methods.py`; README Credits] |
| Oracle Lens RL | `wsbench.produce.OLens` math unchanged (carrier prompt `concepts_raw`, marker scan U+3200–33FF, `alpha·unit(h)`, adapter off for capture / on for generation, `Sampling()` defaults). Adapter from **`agu18dec/olens_and_ar@c95215d5…:olens_s3d_rl600`** instead of the gated dataset path. | [ADAPTED: `produce/methods.py` OLens — source repo swapped to the public copy; byte-identity shown by equal git blob ids (§3)] |
| OLens generation batching | Cells are **grouped by layer** and generated in batches of B (default 32) within a layer, with no padding. The carrier prompt's ids depend only on the layer number (`_carrier(layer)`), so within a layer every row has identical token ids apart from the one overwritten embedding row. Batches never mix layers. (If CP1 finds that all 11 carriers have the same length, cross-layer batches are still not used; this keeps the rule simple.) | [ADAPTED: producer is one cell at a time — too slow (~9,300 cells × ~5 s ≈ 13 h). Validated by S4 (teacher-forced logit equivalence + greedy match).] |
| Judges | Family pins, unchanged: `google/gemini-3.8-flash` via OpenRouter for `multihop` (`bank-2026-09-16`, temperature 0) and `hallucination` (`v5c-chat`, reasoning `minimal`); shared summarizer `interp-v1` on `google/gemini-3.8-flash` for token-bag arms of the MT families and hallucination; regex `mt-regex-2026-09-23` / `mt-regex-summarized-2026-09-23` for MT families. No `judge_model=` override anywhere (so `pinned_instrument` must be true). | [LIFTED: README §Judges] |
| Judge driver | `wsbench judge family=<f> readouts=… out=…` from the pinned checkout, default concurrency and RPM. It is called **once per (family, arm) in item chunks** (`items=<comma-separated ids>`, ≤ 25 items per chunk) by our wrapper, which enforces the spend cap between chunks (§7). The per-cell cache in `<out>/cells.jsonl` makes chunked and single-shot judging produce identical cells. The hallucination token-arm subset is passed the same way (`items=` on the hallucination calls only, never through `wsbench run`, which would apply it to every family). `wsbench report` produces the final table. | [ADAPTED: README §Score one arm end to end — upstream `run_calls` has no dollar cap, so chunking is added to make the cap hard; `items=` is the repo's own subset key] |

### 4.2 Families, cells and the published numbers to reproduce

Published values come from the post's overall figure (INDEX #45), read by pixel measurement: y-axis 0→1 spans 1,244 px in the cropped panel, so reading error is ≈ ±0.005, stated as ±0.01. Where the family README gives a 2-d.p. value, that is used and it agrees with the figure. As a check of the figure-reading method, the six MT values in the README (e.g. `multihop_mt` OL 0.32 / J 0.08 / logit 0.08 / tuned 0.04 / NLA-RL 0.22 / SAE 0.01) all match the pixel readings to within 0.01.

**Primary cells (the gate).**

| # | Family | Items | Read cells per arm (plan) | Instrument | Metric | OL-RL | J-Lens | Logit | Source of published value |
|---|---|---|---|---|---|---|---|---|---|
| 1–3 | `multihop` | 100 | 100 × 11 layers (20–60 step 4) × 1 pos (last word) = 1,100 | Gemini 3.8 Flash, `bank-2026-09-16`, 1 call per (item, layer); item passes at any layer | pass rate ↑ | **0.99** | **0.82** | **0.45** | Figure (pixels: 0.989 / 0.819 / 0.449) |
| 4–6 | `basic_readout_mt` | 100 (40 L2 items also need the language unit) | 100 × 11 × 1 (final prompt token) = 1,100 | regex; token arms first summarized (`interp-v1`, Gemini) | pass rate ↑ | **0.71** | **0.24** | **0.20** | README L87, L91 (regex; regex-summarized); figure agrees (0.709 / 0.239 / 0.199) |
| 7–9 | `hallucination` | 149 (1,123 sites) | 1,123 × 5 layers (20/36/44/52/60) = 5,615 | Gemini 3.8 Flash `v5c-chat` span judge; token arms summarized first | hallucination rate ↓ (readout-level, unrevoked) | **0.34** | **0.31** | **0.20** | Figure only (0.337 / 0.313 / 0.202) |

**Secondary cells (reported, not in the gate).**

| Family | Items | Cells per arm | OL-RL | J-Lens | Logit | Source |
|---|---|---|---|---|---|---|
| `multihop_mt` | 100 | 1,100 | 0.32 | 0.08 | 0.08 | README L87, L91; figure agrees |

**Context floors (frozen, not re-measured).** Prompt-only, from `evals/baselines/README.md`:
- `multihop` 0.90, measured at the **old** read site;
- `basic_readout_mt` 0.59 under regex (the figure's black bar shows ≈0.40, the pre-regex MC value — another vintage mismatch, noted but not gating);
- `multihop_mt` 0.69;
- hallucination prompt-only invention rate 0.53.

**Why these families.** [NEW selection, following the proposal's "2–3 families, including the hallucination sub-benchmark and one free regex-graded family"]
- `multihop` is the best-separated Gemini-judged single-token family (0.99 / 0.82 / 0.45). A pipeline bug in any one reader would show there.
- `basic_readout_mt` is the regex family with the largest Oracle Lens vs token-lens gap. Its Oracle Lens cell is **judge-free**, so it isolates the producer from the judge.
- `hallucination` is the precision half of the benchmark, and I2 needs it.

**Excluded, with reasons.**
- `agentic_misalignment` (~51k Claude Sonnet 5 calls/arm per `plans/0000` §3; at $2/$10 per MTok that is >$100 per arm) and `jailbreak_recognition` (Sonnet 5, ~7.5k calls/arm, 86 WildChat transcripts). Both are over budget, and Sonnet 5 would add a second paid provider.
- `jlens_concept_pr`: ~35k calls/arm, and it scores prose arms only.
- The rest: out of scope for a gate.

### 4.3 Data and read sites

| Element | Value | Tag |
|---|---|---|
| Banks | `evals/{multihop,basic_readout_mt,multihop_mt,hallucination}/items.json` at `92d763e`, byte-for-byte. sha256 recorded in the manifest. | [LIFTED] |
| Read plan | `wsbench plan families=multihop,basic_readout_mt,multihop_mt,hallucination out=…`. The plan rows (render, positions rule, layers) are the read sites of record. | [LIFTED: `docs/producing_readouts.md` §1] |
| Hallucination inputs | `capture_rows.json` `input_ids` re-fed verbatim (render `captured`); sites = `items.json` `sites[].pos` | [LIFTED: `evals/hallucination/README.md` "Bank"] |
| Hallucination token-arm judged subset | J-Lens and logit lens are **generated on all 149 items** (GPU-free cost) but **judged on a seeded half**: 75 items, stratified by source (37 of 74 LMSYS, 38 of 75 DailyDialog), `random.Random(20260928).sample` over sorted ids. The subset file is written and committed at CP1, before any data. OL-RL is judged on all 149. | [ADAPTED: `wsbench judge … items=` is the repo's own subset key — halves the ~$12 token-arm hallucination spend to fit the cap; result is `complete=false` by the repo's rule, reported as such] |

### 4.4 Arms and conditions

- **A1 logit lens, A2 J-Lens, A3 Oracle Lens RL.** Each arm reads every plan cell of the 4 families. [LIFTED]
- Only the arms that published numbers exist for. R-lens, tuned lens, template lens, SAE, OL-SFT and NLA are out of scope: NLA → I1b; the others are not needed by I2–I5.

### 4.5 Diagnostics and controls (not in the gate; they answer SQ1)

| ID | What | Size | Tag |
|---|---|---|---|
| D1 | **OL second draw.** An independent second OL sample (seed 2) on all cells of `multihop` and `basic_readout_mt`, judged identically. Measures draw-to-draw variation of a k=1 arm, the noise floor for OL cells. | 2,200 gens; 1,100 Gemini calls (brmt is regex, free) | [NEW: the benchmark reports single draws; this measures their variance] |
| D2 | **Multihop old read site.** For the 33 items whose read moved on 2026-09-23 (offset −2 → old site = final token, offset −1), read all three arms at the old site too. Score each arm with those 33 items' old-site cells substituted. | 363 cells × 3 arms | [NEW: tests H2 (figure vintage); extends `evals/multihop/README.md` L4–14] |
| D3 | **Shuffled-activation control for OL.** On 50 seeded multihop items, feed item *j*'s activation (a derangement, same layer) and judge against item *i*'s targets. | 550 gens / calls | [NEW: extends the benchmark's own derangement-foil null (`jlens_concept_pr`) to OL on multihop; shows OL passes come from the activation, not the judge] |
| D4 | **Judge test-retest.** Re-judge 100 OL + 100 J-Lens multihop cells, and 200 OL hallucination cells, into fresh `out=` dirs (no cache). Report per-cell agreement for each family. | 400 calls | [NEW: needed to read D1 and unpaired Δs; covers both Gemini instruments] |
| D5 | **OL second draw on hallucination.** An independent second OL draw (seed base `20260930`) on a seeded 20-item subset of the hallucination bank (≈ 150 sites × 5 layers ≈ 750 cells), judged identically. | ≈ 750 gens / calls | [NEW: separates OL sampling variance from judge variance for the hallucination OL cell. Together with the D4 hallucination retest, this covers SQ1 for cell 7.] |

### 4.6 Sample sizes and power

Items are fixed by the banks (100 / 100 / 149). The question is whether n suffices to *detect* a meaningful discrepancy. With the criterion in §4.7, a cell fails only if |Δ| exceeds max(0.05, 1.96·SE).

Item-bootstrap SEs at the published p with n=100:

| p | 0.99 | 0.82 | 0.71 | 0.45 | 0.24 | 0.20 |
|---|---|---|---|---|---|---|
| SE | ≈0.010 | 0.038 | 0.045 | 0.050 | 0.043 | 0.040 |
| τ | 0.050 | 0.075 | 0.089 | 0.098 | 0.084 | 0.078 |

For hallucination (a readout-level ratio clustered by item), the SE is not analytic. I expect ~0.02 on 149 items and ~0.03 on 75, so τ ≈ 0.05–0.06.

So the design detects discrepancies of ≈5–10 points, which is what matters for a gate. Two examples:
- If a pipeline bug halved J-Lens recall on multihop (0.82 → 0.41), that is detected with near certainty.
- A true 3-point drift is not detectable, and this prereg does not claim otherwise.

### 4.7 Metrics, reproduction criterion, statistical test

- **Metrics.** Each family's `numbers.value` from its `results.json`, exactly as `wsbench` computes it:
  - `multihop`: pass rate. Undecided items are excluded by the repo's rule; the count is reported.
  - `basic_readout_mt`: regex pass rate.
  - `hallucination`: `hallucination_rate`, reported beside `assert_share` as the family README requires.
- **Our CI.** Item-level percentile bootstrap, 2,000 draws, seed 0. For hallucination, the repo's own `ci95` (item-level bootstrap of the ratio of per-item sums). For pass rates, `wsbench`'s `ci95` if present. We **also** compute our own item-bootstrap (ported `stats.cluster_bootstrap_ci`) so every cell has the same method. [LIFTED for hallucination; ADAPTED for the uniform recomputation]
- **Δ** = ours − published. **SE** = (CI_hi − CI_lo) / 3.92.
- **Per-cell verdict** [NEW: no published CI exists, so the criterion is ours]:
  - **CONSISTENT** if |Δ| ≤ τ = max(0.05, 1.96·SE).
  - **STRICT** (reported, not gating) if |Δ| ≤ 0.03.
  - **DISCREPANT** otherwise.

  This is a **prespecified tolerance rule, not a hypothesis test**:
  - It is not equivalent to "the published point lies in our 95% CI". The 5-point floor can extend beyond the CI, and percentile intervals can be asymmetric.
  - The published estimate's own variance, and the judge's run-to-run variance, are not measured on the published side.
  - The floor covers figure-reading error (±0.01), rounding, and cells near p = 0 or 1 whose item SE is tiny.
  - Both runs use the same items, so the true between-run noise (sampling and judge only) is probably smaller than the item-bootstrap SE, which makes τ lenient.

  So a DISCREPANT verdict is good evidence of a real difference, while CONSISTENT means "no difference larger than ~τ detected". **CONSISTENT is not evidence of equality.** D1/D5 (OL draw-to-draw) and D4 (judge retest) measure the actual between-run noise on our side. We report whether each Δ exceeds 2 × the relevant draw-to-draw difference.
- **Ordering check** (gating). For every within-family arm pair whose published gap is ≥ 0.10, our gap must have the same sign and be ≥ 0.05. The pairs are:
  - multihop: OL>J, OL>logit, J>logit;
  - basic_readout_mt: OL>J, OL>logit;
  - hallucination: J>logit, OL>logit (with a higher rate being worse).
  That gives **7 ordering checks** (3 + 2 + 2).
- **Multiplicity.** Nine gating cells. If each cell's false-DISCREPANT rate were ≤ 5% and the cells were independent, then P(≥1 false DISCREPANT | all truly equal) ≤ 1 − 0.95⁹ ≈ 0.37. The floor and the conservative SE lower this. The cells are not independent, because they share a judge and a producer, so this is a heuristic bound only. **That is why the gate tolerates one DISCREPANT cell** (§6).

### 4.8 Sanity and manipulation checks (must pass before any headline means anything)

| ID | Check | Pass criterion | Tag |
|---|---|---|---|
| S0 | Offline: `uv run pytest -q` in the pinned checkout. `wsbench judge … dry_run=True` on the four families' toy fixtures, and the printed prompts diffed against the family READMEs. | All tests pass; prompts byte-identical to the README blocks | [LIFTED: AGENTS.md "Sanity checks" 1–2] |
| S1 | **J-Lens exact-reference check.** For the 149 hallucination rollouts (= `jlens_concept_pr` chat items), capture at `eval_positions[0]` and compute J-Lens top-10 at the 11 grid layers + L42. Compare to the shipped `gen-jlens-pr-jlens/<label>/L###.jsonl`. | Top-1 identical on ≥ 99% and top-10 set Jaccard ≥ 0.9 on ≥ 95% of the 1,788 (label, layer) cells; scores within 1e-2 relative | [NEW: uses the benchmark's own frozen reference as ground truth for capture, layer indexing and the J-Lens formula] |
| S2 | **Logit-lens final-layer identity.** On all 100 multihop + 100 basic_readout_mt prompts, `LogitLens.read(h_L63)` top-1 = the model's own greedy next token. | ≥ 98% (bf16 vs fp32 ties allowed) | [NEW: from Tuned Lens / nnsight "final layer is identity"] |
| S3 | **Adapter live.** Logits on the carrier prompt with the adapter on vs off differ (max \|Δlogit\| > 1). Capture runs under `disable_adapter()`: the activations for 5 prompts are bitwise equal to a fresh load without PEFT. | both | [LIFTED: olens_and_ar card "load_lens ASSERTS the adapter is live"; "Capture with disable_adapter()"] |
| S4 | **Batched ≡ unbatched OL.** Two parts, on 64 random cells spread over all 11 layers:<br>(a) **Distributional equivalence.** Take each cell's batched greedy continuation and teacher-force it through the unmodified single-cell path (`OLens` carrier + injection, B=1), then through the batched path (B=32). Compare the next-token logits at every generated position. Sampling uses exactly these logits (T=1, top-p 0.95, top-k 64), so matching logits means matching sampling distributions.<br>(b) Greedy (T=0) batched vs the unmodified `OLens.read`: exact string match rate. | (a) top-1 agreement ≥ 99.5% of positions, and the top-64 sets overlap ≥ 0.95 on average (reported with max \|Δlogit\|)<br>(b) ≥ 95% exact match | [ADAPTED: `persona_vectors_pod.py` bs=1-vs-batched check (code map §2.8), strengthened to a logit-level test because sampled outputs can't be compared directly] |
| S5 | **Throughput and memory at full scale.** Measure peak `torch.cuda.max_memory_allocated` in the token-lens phase (model + J fp32 + W_U fp32) and the OL phase (B=32). Measure tokens/s. | Fits in 80 GB. Projected OL phase ≤ 2.5 h; otherwise lower the scope per §7 kill order. | [ADAPTED: code-map gotcha #8] |
| S6 | Attention backend recorded as sdpa/eager, never FA4. Cache activations bitwise-repeat on a second forward. | both | [ADAPTED: Torrielli App I; code map §2.8 "bitwise-repeated"] |
| S7 | Judge pilot: `limit=3` live per (family, arm). Verdicts eyeballed against readouts (a random 10, never cherry-picked, shown to Jacob). `n_unjudged_cells` = 0. `pinned_instrument` true. | all | [LIFTED: AGENTS.md checks 3–5] |
| S8 | Post-run completeness, the repo's own `complete` rule:<br>• `n_missing_cells` = 0;<br>• `n_unjudged_cells` = **0**, after up to 3 resume passes (the repo re-queues failed cells);<br>• `n_empty_cells` ≤ 5%;<br>• `pinned_instrument` true.<br>The one sanctioned exception: `complete=false` caused *only* by the preregistered `items=` subset on hallucination token arms. OL empty-generation rate reported (Torrielli saw 14% on their AO). | all | [LIFTED: README §Results contract] |
| M1 | Manipulation check = D3: OL multihop pass rate with shuffled activations. | ≤ 0.10 (prediction ≤ 0.05) | [NEW, see D3] |

If any of S0–S6 fails after one debugging round, stop at the checkpoint (§7). No judge spend happens before S0–S6 pass.

### 4.9 Seeds and determinism

- `torch.manual_seed` per OL batch = `20260928 + batch_index`. Batches are formed within a layer after a fixed sort by (family, layer, item id, pos). Composition and seed are recorded per cell.
- D1 uses base seed `20260929`; D5 uses `20260930`. D3's derangement and the hallucination subset use `random.Random(20260928)`.
- Token lenses are deterministic up to bf16/TF32 numerics (TF32 disabled).
- Judges: repo pins (temperature 0 for bank judges; the hallucination settings are the family's own). OpenRouter provider routing is recorded from response headers where available. **Residual nondeterminism** (CUDA sampling, API) is expected and quantified by D1 and D4.

### 4.10 Output artifacts and the reusable cache (for I1b, I2, I3, I5)

The cache lives under `data/I1a/` (gitignored) and is mirrored to a private HF dataset under Jacob's account (name to be set by Jacob). Small files and hashes are committed under `results/I1a/`.

```
data/I1a/
  manifest.json      # provenance: model id+sha, tokenizer sha, chat template sha, wsbench commit,
                     # adapter repo+sha+blob oid, J-lens repo+sha+file sha256, torch/transformers/
                     # peft/fla versions, attn impl, dtype, TF32 flag, GPU, driver, seeds, UTC times,
                     # layer convention (below), bank sha256s
  plan/<family>.jsonl                 # verbatim `wsbench plan` rows used
  renders/<family>.jsonl              # per item: id, render, input_ids, token strings (decode per id),
                                      #   read positions (absolute idx + the plan's reported pos)
  acts/<family>.safetensors           # "block_out": bf16 [n_pos, 64, 5120]; [:, L] = output of
                                      #   model.layers[L] = wsbench layer L (pre-final-norm);
                                      #   "embed": bf16 [n_pos, 5120]; "norm": fp32 [n_pos, 64]
  acts/<family>.index.jsonl           # row i -> {id, abs_pos, plan_pos, token, site_kind?, is_old_site?}
  readouts/<arm>/<family>.jsonl       # EXACT wsbench readout contract; this is what gets judged
  readouts/olens_rl600_draw2/<family>.jsonl        # D1
  readouts/olens_rl600_shuffled/multihop.jsonl     # D3 (with source-activation id per row)
  readouts_meta/olens_rl600*/<family>.jsonl        # per cell: seed, batch_id, sampled token ids,
                                                   #   n_new_tokens, hit_max_tokens, empty flag
  lens_topk/<arm>/<family>.safetensors             # logit & J-lens: top-100 token ids (int32) +
                                                   #   scores (fp32) per cell (judged file keeps top-10)
  judged/<arm>/<family>/                           # wsbench outputs verbatim: results.json,
                                                   #   cells.jsonl, summary.md, run.json
  checks/{S1..S8,M1}.json
```

- **Layer convention.** 0-indexed decoder blocks. Layer L is the residual stream after `model.layers[L]`, as in `produce/backend.py`. Every layer 0–63 is stored at every read position, bf16 (lossless: the model's residual stream is bf16, and the producer's `.float()` is exact).
- **Size.** Read positions:

  | Source | Positions |
  |---|---|
  | multihop (current site) | 100 |
  | multihop (old site, D2) | 33 |
  | basic_readout_mt | 100 |
  | multihop_mt | 100 |
  | hallucination sites | 1,123 |
  | hallucination `jlens_concept_pr` eval positions | 149 |
  | **Total** | **1,605** |

  1,605 positions × 64 × 5120 × 2 B ≈ **1.05 GB**, plus ~10 MB of readouts.
- **Who uses what.**
  - I1b (NLA at L42): `acts[:, 42]`.
  - Skip-lens: L42 or L62.
  - I2: the exact OL input vectors, seeds, and sampled ids (for teacher-forced logprobs). N>1 samples are generated in I2, which also needs the AR adapter and whitener moments.
  - I5: all three arms' readouts on identical cells, plus top-100 token lists.
  - I3: the code only (it builds its own dataset).
  - I6: the code only; CHIVE needs its own transcripts.
- **Deliberately not cached:** full-sequence activations. Re-capture costs seconds on any pod that must load the model anyway to run a reader, so storing them buys nothing.

## 5. Hypotheses and predictions

Priors are my judgment, written before any data. They draw on:
- the producer being the in-house producer's port (token lenses should be near-deterministic);
- the OL cells being single T=1 draws;
- the benchmark's instruments changing on the day of publication.

**H1 (main): the public stack reproduces the published numbers.** ≥ 8/9 primary cells are CONSISTENT, all ordering checks hold, and S0–S8 pass. **P = 0.55.**

**Competing hypotheses**
- **H2, figure vintage.** Some published bars were produced under a pre-2026-09-23 instrument or read site. The signature is a family-specific discrepancy that D2 (multihop old site) or the MC/regex history explains. **P = 0.20** that this explains ≥ 1 DISCREPANT cell.
- **H3, judge drift or nondeterminism.** Gemini 3.8 Flash behavior on OpenRouter differs from 5 days ago, or run-to-run. The signature is that Gemini-judged cells are off while the judge-free OL cells of `basic_readout_mt` / `multihop_mt` reproduce, and D4 agreement is < 0.9. **P = 0.15.**
- **H4, producer or artifact mismatch.** For example, the published OL arm used a different checkpoint (the card notes RL branches A/B "may supersede `iter_000600`"), a different alpha, or the published J-Lens used a different lens file. The signature is that all cells of one arm are off, including the judge-free regex cells, while the other arms reproduce. **P = 0.10.**
- **H5, OL sampling variance.** OL cells differ by more than τ but by less than 2 × the D1 draw-to-draw difference. **P = 0.10.**

(H2–H5 are not mutually exclusive, so their probabilities need not sum with H1's.)

**Numeric predictions** (80% intervals, ours):

| Cell | Published | Predicted 80% interval |
|---|---|---|
| multihop OL | 0.99 | 0.95–1.00 |
| multihop J | 0.82 | 0.76–0.88 |
| multihop logit | 0.45 | 0.38–0.52 |
| brmt OL | 0.71 | 0.63–0.78 |
| brmt J | 0.24 | 0.18–0.30 |
| brmt logit | 0.20 | 0.15–0.26 |
| hallucination OL | 0.34 | 0.28–0.40 |
| hallucination J | 0.31 | 0.24–0.37 |
| hallucination logit | 0.20 | 0.14–0.26 |
| mhmt OL / J / logit (secondary) | 0.32 / 0.08 / 0.08 | 0.25–0.39 / 0.04–0.13 / 0.04–0.13 |

**Checks**
- S1 passes: P = 0.85. The remaining risk is a difference in layer indexing or the display-token mapping, which is fixable.
- S2: P = 0.95.
- S4: P = 0.8.
- M1 (shuffled OL multihop) ≤ 0.05: P = 0.8.
- D1 draw-to-draw |Δ| on multihop OL ≤ 0.02 (near ceiling); on brmt OL ≤ 0.06.
- D4 agreement ≥ 0.9.

**Where we depart from published results.** I expect *our* multihop J-Lens and logit lens to come in slightly **above** the figure if the figure used the old read site. That is a directional, not binding, expectation from `evals/multihop/README.md`'s rationale ("the last word is where the bridge is formed").

## 6. Outcome scenarios

The decision rule is evaluated **top to bottom; the first matching row applies**. That makes the rows exhaustive and mutually exclusive.

| # | Scenario | Observed pattern (thresholds) | Answer to the research question | So-what for labs | Our next step |
|---|---|---|---|---|---|
| 0 | **Uninterpretable** | Any of S0–S7 still fails after one debugging round (for S7: a pilot verdict that contradicts its readout on eyeball and is not fixed by re-running); or `pinned_instrument` false; or S8 fails (any unjudged cell after 3 resume passes, or > 5% empty cells in any primary family); or M1 > 0.10 | Unanswered: our stack is not a valid instrument yet | None | **Stop at CP.** Report the failing check. Fix within the remaining cap if it's a known bug; otherwise ask Jacob. I2/I5 stay blocked. |
| 1 | **GREEN: reproduces** | ≥ 8/9 primary cells CONSISTENT; no cell with \|Δ\| > 0.15; all 7 ordering checks hold; full scope evaluated (no primary cell cut by the kill order) | **Yes.** The public benchmark reproduces for logit lens, J-Lens and Oracle Lens on these families. | Reader rankings are independently confirmed (Oracle Lens > J-Lens > logit lens on multi-token recall; similar hallucination for Oracle Lens and J-Lens), so interp-tooling and auditing teams can cite them. WorkspaceBench is usable as a regression test for their own readers. | **Continue:** I1b (NLA on cached L42) and I2 (best-of-N/consensus from the cached OL inputs), using `data/I1a` as the baseline arm. |
| 2 | **RED: does not reproduce** | ≤ 5/9 CONSISTENT, **or** ≥ 2 ordering checks fail, **or** any cell with \|Δ\| > 0.25 not explained by D2 | **No.** The published numbers are not recoverable from public artifacts with the documented procedure. | Don't quote WorkspaceBench reader rankings as settled until arm readouts are published. | **Pivot:** send Camila/Neel the discrepancy table and ask for arm readouts (Q1). Run I4 (independent of WSB) meanwhile. Don't start I2/I5 on WSB. |
| 3 | **AMBER-vintage** | Not rows 0–2, and **all non-conforming cells\* are in `multihop`**; substituting D2 old-site cells makes them CONSISTENT | **Yes, conditional:** reproduces once the pre-2026-09-23 read site is used. This is *consistent with* the published multihop bars predating the read-site change; it does not prove when the figure was made. | Report instrument version with every score; published figures can silently mix vintages. Maintainers get a concrete bug report. | **Continue** with the current instrument as our baseline (our numbers, not the figure's, become the reference). Tell Camila. Proceed to I1b/I2. |
| 4 | **AMBER-judge** | Not rows 0–3; all non-conforming cells\* are Gemini-judged (multihop or hallucination); **all three `basic_readout_mt` cells CONSISTENT** and the OL regex cells of `multihop_mt` CONSISTENT; D4 agreement < 0.9 on the affected family | **Partially, cause unconfirmed:** the judge-free cells reproduce, and the Gemini-judged cells don't. *Hypothesis:* judge drift or noise. The regex cells use different renders, sites and scoring, so they don't prove the producer is fine on hallucination. Confirming the cause needs a re-judge of the maintainers' reference readouts (Q1). | Pin judge snapshots; treat Gemini-judged deltas under ~τ as noise. LLM-judged interp benchmarks are fragile over days. Maintainers get a concrete bug report. | **Continue** with relative comparisons only (same-run arms). Run I2 comparisons within one judging session. Report the drift to maintainers. |
| 5 | **AMBER-reader** | Not rows 0–4; the non-conforming cells\* all belong to **one arm**, including its judge-free cell if it has one (OL in brmt) | **Partially:** two readers reproduce. One reader's public artifact or recipe differs from what was published (H4), or (OL only) sampling variance, if \|Δ\| ≤ 2 × the D1 difference (H5). | Public reader checkpoints may not be the ones scored. Labs adopting that reader should re-benchmark it. | If the arm is OL and the Δ is within 2 × D1 → treat as reproduced (H5) and **continue**. Otherwise ask the maintainer which checkpoint or artifact was scored (Q1), and continue I2 only if its reader reproduced. |
| 6 | **INCONCLUSIVE** | Anything not matched by rows 0–5 (includes scope-curtailed runs that are not RED) | **Inconclusive.** The cells that were evaluated are reported one by one. No overall reproduction claim is made, and any conclusion is limited to the cells actually evaluated. | Weak evidence of fragility; no general lesson | Spend ≤ $5 of the remaining cap on one targeted diagnostic for the worst cell. Downstream work is gated **per baseline**: I2 proceeds only if the OL cells it uses (multihop, hallucination) are CONSISTENT; I5 only if all arms it compares are. Otherwise ask Jacob. |

\*A **non-conforming cell** is a DISCREPANT cell, or a cell that takes part in a failed ordering check.

Coverage: row 0 overrides everything. Row 1 needs ≥ 8/9 CONSISTENT, no \|Δ\| > 0.15 and every ordering check passing. Row 2 needs ≤ 5/9, or ≥ 2 ordering failures, or an unexplained \|Δ\| > 0.25. Everything else is AMBER: 6–7/9; or 8–9/9 with one \|Δ\| > 0.15 or exactly one ordering failure; or a scope-curtailed run (§7), which can never be GREEN. Row 6 (INCONCLUSIVE) is the catch-all, so the rows are exhaustive, and the first-match rule makes them exclusive.

## 7. Budget, timeline, stop points

**Rates** (fetched 2026-09-28 from `https://openrouter.ai/api/v1/models`):
- `google/gemini-3.8-flash`: $0.75/MTok in, $3.75/MTok out (reasoning billed as output), cache-read $0.075.
- `anthropic/claude-sonnet-5`: $2 / $10 (**not used**).

GPU rates are from `CLAUDE.md`: H100 80GB community $2.69/h, secure $3.49/h; fallbacks H100 NVL $2.59, A100 80GB $1.39.

**Per-call token assumptions.** These are central values, with high values in brackets. They are estimated from prompt lengths in the family READMEs and bank statistics (hallucination: mean prompt 135 chars, mean response 1,100 chars, template ≈ 1,150 tokens) and **replaced by measured values at CP4**.

| Call type | In tokens | Out tokens (incl. reasoning) | $/call central (high) |
|---|---|---|---|
| Bank judge (multihop) | 700 (1,000) | 60 (200) | 0.00075 (0.00150) |
| Summarizer `interp-v1` | 250 (400) | 60 (200) | 0.00041 (0.00105) |
| Hallucination span judge | 1,700 (2,200) | 120 (400) | 0.00173 (0.00315) |

**API spend** (Gemini 3.8 Flash only):

| Item | Calls | Central $ | High $ |
|---|---|---|---|
| multihop, 3 arms × 1,100 | 3,300 | 2.48 | 4.95 |
| D2 multihop old site, 33 × 11 × 3 | 1,089 | 0.82 | 1.63 |
| D1 OL second draw, multihop | 1,100 | 0.83 | 1.65 |
| D3 shuffled control | 550 | 0.41 | 0.83 |
| D4 retest: 200 multihop + 200 hallucination OL | 400 | 0.50 | 0.93 |
| D5 OL second draw, hallucination 20-item subset | ≈ 750 | 1.30 | 2.36 |
| basic_readout_mt summarizer, 2 token arms × 1,100 (OL regex: $0; D1 draw regex: $0) | 2,200 | 0.90 | 2.31 |
| multihop_mt summarizer, 2 × 1,100 | 2,200 | 0.90 | 2.31 |
| hallucination OL, all 149 items: 5,615 span calls (stage-2 verify skipped, `opts=verify=0`, which leaves the headline untouched) | 5,615 | 9.71 | 17.69 |
| hallucination J + logit, 75-item half: ≈ 2,825 cells each × (span + summary) | 11,300 | 12.09 | 23.73 |
| S0/S7 pilots and dry runs | ~200 | 0.50 | 0.50 |
| **API total** | **~28,700** | **$30.5** | **$58.9** |
| of which primary cells only (multihop, brmt summarizer, hallucination 3 arms) | 22,400 | 25.2 | 48.7 |

[ADAPTED: `opts=verify=0` — stage 2 is an `extras` diagnostic, not the headline ("The headline, its bootstrap and `complete` are untouched", `evals/hallucination/README.md` L85–87). Skipping it saves ~$10–20.]

**GPU spend**, 1 × H100 80GB. Memory arithmetic:

| Component | Size |
|---|---|
| Qwen3.6-27B bf16 | ≤ 55.6 GB (27.78B params × 2 B; the text-only load is slightly smaller) |
| J-Lens | 63 × 5120² × 4 B = 6.6 GB (fp32 on device, per `_load_jacobians`) |
| `W_U` fp32 per token lens | 248,320 × 5120 × 4 B = 5.1 GB |
| OL LoRA | 0.47 GB |
| OL KV/state at B=32 × ~320 tokens | < 2 GB (only 16 of 64 layers keep a KV cache) |

- Token-lens phase peak: 55.6 + 6.6 + 5.1 (+5.1 if logit and J-Lens are bound together; we bind them sequentially and free between) ≈ **67–73 GB**.
- OL phase peak: 55.6 + 0.5 + ≤ 2 + allocator overhead ≈ **60 GB**.

Both fit in 80 GB. S5 measures the real peaks.

| Phase | Hours |
|---|---|
| Pod up, env, downloads (55.6 GB + 3.3 GB + 0.5 GB; `HF_HUB_DISABLE_XET=1`) | 1.0 |
| Smoke S1–S6 (incl. the unbatched OL reference for S4) | 1.0 |
| Capture + token lenses (449 prompt forwards: 100 + 100 + 100 + 149; ≈ 20,300 token-lens cells: 9,278 per arm × 2, plus 1,788 for S1) | 0.5 |
| OL generation: ≈ 9,300 main + 2,200 D1 + 550 D3 + 750 D5 ≈ 12,800 cells at B=32, ~13 s/batch (≤ 256 new tokens) | 1.5 |
| Debug buffer (per `CLAUDE.md` "3–4 hours of setup and debugging") | 3.0 |
| **Total** | **7.0 h** |

At $2.69/h that is **$18.8**; at $3.49/h (secure fallback) **$24.4**; volume storage ≈ $1.

**Totals and cap**

| | Central | Worst credible |
|---|---|---|
| API | $30.5 | $58.9 (unconstrained) |
| GPU | $18.8 | $24.4 |
| Storage | $1 | $1 |
| **Total** | **$50.3** | ~$84 unconstrained; the API cap and stop rule below keep the real spend ≤ $60 |

**Hard cap: $60.**
- **GPU sub-cap: $26**, enforced by the watchdog.
- **API cap = $58 − actual GPU + storage spend at CP3.** That is $32–38 if GPU lands at $19–25.
- **Reserve: $2.**

The API cap is enforced two ways:
- a **dedicated OpenRouter key with a credit limit set to the API cap** (a provider-side hard stop);
- our chunk wrapper (§4.1 "Judge driver"). Before each ≤ 25-item chunk it computes a pre-call upper bound = cells in chunk × *measured* p95 $/call from CP4 (× 3 for token arms' summary + span calls where applicable). It refuses the chunk if spend-so-far + bound > cap.

Upstream `run_calls` has no dollar cap, so the cap is enforced only at these two layers.

**Scope decision at CP4** (measured $/call replaces the table's assumptions). Compute the projected cost P of the full plan.
1. **If P ≤ API cap:** run everything.
2. **Else drop the non-gating items, in this order,** until P ≤ cap:
   1. D2 logit-arm cells (−$0.3–0.5)
   2. D1 (−$0.8–1.7)
   3. `multihop_mt` (−$0.9–2.3)
   4. D5 (−$1.3–2.4)
   5. D2 entirely (−$0.8–1.6)
   6. D4 hallucination half (−$0.35–0.6)

   Total savings: ≈ $4.5 central, $9 high. Diagnostics D3 (M1) and the multihop half of D4 are never dropped.
3. **Else, if the primary cells alone fit** (central $25.2; high $48.7 against a cap of ~$32–38), run them.
4. **Else curtail primary scope, in this order**, recomputing P after each step:
   1. hallucination **logit** arm judging dropped (−$6–12);
   2. hallucination J-Lens to a seeded 38-item quarter (−$3–6);
   3. hallucination OL to the same 75-item half (−$4.9–8.8).

   Any curtailment makes the run **scope-curtailed**. It can never be GREEN (§6 row 1). Its conclusions are limited to the cells actually evaluated, and it falls to row 2 or row 6.
5. **If even the fully curtailed primary scope exceeds the cap** (the fully curtailed primary scope costs ≈ $22 at our "high" per-call column, so this happens only if measured per-call cost is ≳ 1.45× "high" with a $32 cap): **stop before judging. No reproduction verdict.** Report the measured costs, and ask Jacob to raise the cap or narrow the question.

**Other kill criteria**
- GPU spend reaches $26 → stop the pod.
- Any S0–S6 check still fails after one debugging round → stop (scenario 0).
- An OL throughput projection > 3 h at S5 → drop multihop_mt OL and D1 generation before going further.

**Wall-clock**: ~1.5–2 days elapsed (pod ~7 h; judging ≈ 28k calls at the repo default 240 rpm ≈ 2 h; analysis 2 h). **Jacob's active hours: ~2** (approve prereg, approve CP2/CP4 spend, read the random-sample packet, and decide).

**Checkpoints** (stop, report to `logs/I1a-report.md`, wait for Jacob):

| CP | Content | Spend |
|---|---|---|
| CP0 | Jacob approves this prereg and budget. Jacob allows running upstream `wsbench` code locally or on the pod (the permission classifier blocked it for the agent). Jacob decides Q1–Q4 (§9). | $0 |
| CP1 | Laptop, $0. Pinned checkout. S0 passes. `wsbench plan` for 4 families saved. Read `registry.py` / `readplan.py` / `judge_config.py` / `llm.py` and confirm per-family layers, judge reasoning and temperature, the `items=` semantics, and that the OL carrier prompt has the same length at all 11 layers; update §4.2 if any differ (a noted amendment, before data). Hallucination subset, D3 derangement and the batch plan written and committed. Harness unit tests pass. | $0 |
| CP2 | Pod smoke: S1–S6 results, memory and throughput. **No judging yet.** | ≤ $6 GPU |
| CP3 | All readouts and caches produced; sha256 manifest; pulled; pod terminated. | ≤ $26 GPU cumulative |
| CP4 | Judge pilot (S7): `limit=3` per (family, arm). Measured tokens/call → projected spend vs the API cap ($58 − CP3 GPU spend); scope decision (§7) applied if needed. Random 10 verdicts per family shown to Jacob. | ≤ $1 API |
| CP5 | Full judging + diagnostics. Analysis: §6 table with per-cell Δ, τ, verdict, ordering checks, D1–D4, random raw readouts per arm. **Stop; Jacob decides next step.** | ≤ $32 API cumulative |

## 8. Implementation plan

**Port from `../mats12_neel_working`** (paths from `docs/working-repo-code-map.md`):

| Source | Destination | Edits |
|---|---|---|
| `scripts/pod_up.py`, `pod_down.py`, `pod_watchdog.sh`, `pod_budget_check.py`, `pod_push.sh`, `pod_pull_results.sh`, `pod_hunt.sh` (§2.1) | `scripts/` | Image with CUDA ≥ 12.8 for the modern stack. GPU list H100 80GB community → secure → H100 NVL → A100 80GB. `CAPS={"I1a": 26}`. Env allowlist adds `HF_TOKEN` only (**the OpenRouter key never goes to the pod; judging runs on the laptop**). Volume ≥ 150 GB. |
| `src/em_filter/pod_loading.py` `load_pinned` (§2.1) | — | Pattern only: we must load through `wsbench.produce.Backend` to stay faithful. We assert the resolved sha against the pin after load. |
| `persona_vectors_pod.py` (§2.8) | — | The hook plus the bs=1-vs-batched and bitwise-repeat verification pattern → S4/S6. |
| `src/em_filter/stats.py` `cluster_bootstrap_ci` (§2.10) | `src/wsrepro/stats.py` | Pure numpy; add `ratio_of_sums` for hallucination. |

**New code** (small; `src/wsrepro/` + `scripts/i1a_*.py`, `pod` dependency group = the `workspace-bench` gpu extra lock):
1. `i1a_capture.py` (pod): for each plan row, `render(spec, tok)` → `Backend.capture(ids, layers=range(64), positions)` → `acts/*.safetensors` + index. Uses **wsbench's own `render`, `readplan.resolve`, `Backend`**.
2. `i1a_token_lenses.py` (pod): `LogitLens` then `JLens`, `.bind(backend)`, `.read(h, layer)` over cached vectors → contract JSONL + top-100 store. Also the S1 and S2 checks.
3. `i1a_olens.py` (pod): `class BatchedOLens(OLens)`, which overrides `bind` to fetch `agu18dec/olens_and_ar@c95215d5` `olens_s3d_rl600/` and adds `read_batch(H, layer, seed)`. That method reuses `_carrier`, `alpha`, `prompt` and `sampling`, stacks B embedding rows, and calls one `generate(num_return_sequences=1)`. The unmodified `OLens.read` is kept for S4.
4. `i1a_diagnostics.py`: D1–D3 and D5 generation (pod) and the D4 retests (laptop).
5. `i1a_judge_chunks.py` (laptop): chunked `wsbench judge` wrapper with the pre-call spend bound (§7).
6. `i1a_analyze.py` (laptop): reads `judged/*/results.json` → τ, Δ, verdicts, ordering, §6 scenario. Writes `results/I1a/summary.json` + a random-sample packet.

**Order of work**
- CP1 laptop work, then unit tests with a tiny model (e.g. `Qwen/Qwen3-0.6B` on MPS) for capture indexing and batched-OLens shape logic. The adapter can't load there; test with a dummy LoRA.
- Then pod smoke S1–S6.
- Then the full generation run.
- Then laptop judging through `i1a_judge_chunks.py`: chunked `wsbench judge family=<f> … items=<ids>` per (family, arm), pilot first. Never `wsbench run`, which has no dollar cap and would apply `items=` to every family.
- Then analysis.

Every paid batch > $5 prints its projection first (`CLAUDE.md`). The ledger is `logs/costs.jsonl` with a single writer.

## 9. Risks, limitations, open questions for Jacob

**Risks and limitations**
1. **No published CIs or readouts.** The criterion compares against point estimates read off a figure, unpaired. The design can confirm consistency within ~5–10 points, not exact equality.
2. **Figure vintage unknown** (H2). Only multihop has a cheap diagnostic (D2). A vintage shift in hallucination (e.g., which layers the published arm used) would appear as AMBER-mixed.
3. **Hallucination layers.** The family README's cost note and example use layers 20/36/44/52/60. If `wsbench plan` says otherwise, CP1 records it as a pre-data amendment.
4. **Batching is a departure** from the producer (validated by S4). With T=1 sampling, the RNG stream differs from the in-house run anyway, so exact OL text reproduction was never possible.
5. **Adapter source.** Public copy, byte-identical by git blob id. The published OL arm could still have used a different checkpoint (the card mentions RL branches A/B that "may supersede `iter_000600`") → H4.
6. **Gated DeltaNet kernels.** Without fla kernels, HF falls back to a slow torch path. Mitigated by S5; worst case, the scope is cut by the kill order.
7. **Hallucination subset** for token arms makes those results `complete=false` by the repo's rule. That is fine for our purposes and reported.
8. **Gemini provider routing on OpenRouter** can vary silently. Recorded where exposed. D4 quantifies it.

**Open questions for Jacob**
- **Q1.** Can Neel or Camila share the in-house arm readouts or `results.json` for OL-RL, J-Lens and logit lens (`lens_readouts_s3d-rl600-clean.jsonl` is named in `plans/0007`)? With them, we would first re-judge ~200 of their cells (≈ $0.2) to separate judge drift from producer drift. This is exactly the proposal's step 1, which is otherwise impossible.
- **Q2.** Approve the hallucination token-arm half-subset (saves ≈ $12)? Or would you rather spend up to the cap on the full bank for all arms and drop `multihop_mt` and D1 instead?
- **Q3.** Is the 5-point floor and 95%-CI criterion (§4.7) the right strictness for a gate? A stricter alternative is ≤ 0.03 on the token-lens cells only, since their readouts are deterministic.
- **Q4.** OK to run upstream `wsbench` code (pytest, `plan`, `judge`) on the laptop? The agent's permission classifier blocks executing it, so this needs your explicit allowance or a settings rule.

## 10. Review log

| Round | File | BLOCKING / MINOR / NIT | Handling |
|---|---|---|---|
| r1 | `logs/reviews/20260928-I1a-prereg-r1.md` | see below | see below |

r1 totals: **9 BLOCKING, 5 MINOR, 0 NIT.** The drafting agent applied most fixes before it stalled; the orchestrator verified them against the review and finished the last two (§8 judging path, §8 numbering).

2026-09-29: §2 and §3 merged per Jacob; sections renumbered (no content changes besides moving §3's lab-use detail into §6). Section references in the r1 table below use the new numbering.

| # | Finding (r1) | Sev. | Handling |
|---|---|---|---|
| 1 | Gate listed 7 ordering checks but required 8 | BLOCKING | **Fixed.** §4.7 now defines 7 checks (3 + 2 + 2), and §6 row 1 requires all 7. |
| 2 | S8 tagged LIFTED but allowed 1% unjudged cells | BLOCKING | **Fixed.** S8 requires `n_unjudged_cells` = 0 after ≤ 3 resume passes. The only sanctioned `complete=false` is the preregistered hallucination token-arm subset. |
| 3 | Carrier token ids differ by layer; batches could mix layers; S4 too weak for sampled generation | BLOCKING | **Fixed.** §4.1 now batches only within a layer. S4 is now a teacher-forced logit-equivalence test (top-1 ≥ 99.5%, top-64 overlap ≥ 0.95) plus a ≥ 95% greedy exact match. |
| 4 | "Hard" API cap absent from the `wsbench run` path | BLOCKING | **Fixed.** Chunked `wsbench judge … items=` wrapper with a pre-call p95 cost bound, plus a dedicated OpenRouter key with a credit limit (§4.1, §7, §8). |
| 5 | Kill order can't rescue the high-cost case | BLOCKING | **Fixed.** §7 adds step 5: if even fully curtailed primary scope exceeds the cap, stop before judging and give no reproduction verdict. |
| 6 | Dropping a primary arm could still yield an all-readers GREEN | BLOCKING | **Fixed.** Any curtailment is "scope-curtailed", can never be GREEN, and limits claims to the evaluated cells (§6 rows 1, 6; §7 step 4). |
| 7 | No way to separate OL sampling from judging for hallucination | BLOCKING | **Fixed.** Added D5 (second OL draw on a 20-item hallucination subset), and D4 now retests 200 OL hallucination cells. |
| 8 | AMBER-judge overclaimed "producer reproduces" | BLOCKING | **Fixed.** Row 4 now says the cause is unconfirmed, judge drift is a hypothesis, and confirming it needs a re-judge of the reference readouts (Q1). |
| 9 | Catch-all answered "mostly yes" and proceeded | BLOCKING | **Fixed.** Row 6 is INCONCLUSIVE with per-baseline gating for I2 and I5. |
| 10 | CONSISTENT rule presented as a 95% test | MINOR | **Fixed.** §4.7 calls it a prespecified tolerance rule, lists why it isn't a CI test, and marks the 0.37 multiplicity figure as a heuristic that assumes independence. |
| 11 | `items=` via `wsbench run` applies to every family | MINOR | **Fixed.** §4.1 and §8 use per-family `wsbench judge … items=` only. |
| 12 | Cache claim overstated for I3; I2 needs whitener moments | MINOR | **Fixed.** SQ2 and §4.10 limit cache reuse to I1b, I2 and I5; I3 reuses code only; the whitener moments are noted as unavailable. |
| 13 | Scenario 0 omitted S7 failure | MINOR | **Fixed.** Row 0 now includes S7 and S8 failure conditions. |
| 14 | D2 agreement framed as proof of figure vintage | MINOR | **Fixed.** Row 3 says the result is "consistent with" the pre-change site and does not prove when the figure was made. |

**Open for Jacob:** Q1–Q4 in §9. Q4 matters most: executing upstream `wsbench` code was blocked for the agent, so you need to allow it before CP1.
