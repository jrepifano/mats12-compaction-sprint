# Internal literature review: MoE arm for E11 / E12 / E13 (Qwen2.5-7B-Instruct dense base), 2026-10-08

Subagent (Sonnet) report. Brief: how are EM, persona-vector, steering and probe effects likely to carry over to mixture-of-experts (MoE) models, and which MoE model pairs best with the dense Qwen2.5-7B-Instruct base? Sources: `docs/research-proposals.md` (E8 and the "Dense vs MoE" notes), `logs/reviews/20261003-proposals-E8-r1.md`, every `papers/*/INDEX.md` (grepped for MoE, mixture, expert, routing, Doan, OLMoE, Qwen3, gpt-oss, Mixtral, DeepSeek), `papers/_litreview_text.txt`, `papers/_neel_brainstorm_text.txt`, `papers/neel.txt`, our X3/X3b/I9/X4 material, and the working repo (grep). E11, E12 and E13 do not yet exist in `docs/research-proposals.md` or `docs/experiments/` (checked), so I had nothing of theirs to read. **Facts about models outside the repo (expert counts, vLLM/PEFT support, which MoE siblings exist) are from my own background knowledge, not from a file, and are labeled "UNVERIFIED (memory)". The external review should check them.**

## 0. Bottom line
- Our repo has **no measured result on EM, persona vectors, steering or probes in an MoE we trained or read inside.** All of it is a proposal (E8, held, never run) or an arm that was cut (I9's Qwen3.6-35B-A3B arm, cut in v0.3).
- The one direct prior is Doan et al. (NeurIPS 2025), and **the full text is MISSING**. Only the abstract wording is known, and that came from search results, not the paper.
- Every MoE-related residual-stream paper we hold is on a frontier-scale open MoE and is about steering or an attack, not about EM.
- No public EM organism or persona adapter for an MoE is known to us.

## 1. Sources table
| Source | Directory, row | Relevance | Read depth |
|---|---|---|---|
| Doan et al. 2025, *Emergent Misalignment in Mixture-of-Experts Models* | `papers/em_personas/` row 32 | The only direct prior. gpt-oss-20B, Qwen3-30B-A3B, Mixtral-8x7B on insecure code and unsafe medical advice; "negative correlation between sparsity and EM"; expert-only fine-tuning still gives EM "to a lesser degree" | **MISSING** (OpenReview blocks automated fetches; not on arXiv or the Wayback Machine). Abstract only, second-hand from search results |
| `docs/research-proposals.md` §E8 (lines ~466-538) | n/a | Our own held design: Qwen3.6-27B (dense) vs Qwen3.6-35B-A3B (MoE), router frozen, LoRA on attention + shared + routed experts, matched narrow-task checkpoints, routing-shift and forced-routing stage, "do personas live in particular experts?" sub-question, optional OLMoE arm. Cost ≈ $65-75 Stage 1 | Read in full |
| `logs/reviews/20261003-proposals-E8-r1.md` | n/a | gpt-6-sol round 1, REVISE: pair differs in depth/width/size so "it's the MoE" is not isolated; define matching by held-out narrow-task score; routing is not "carried" by frozen-router shifts; add matched random-forcing control | Verdict section read in full |
| Dense-vs-MoE notes, `research-proposals.md` lines ~873 (Topic 2), ~1375 (Topic 3), ~1609 | n/a | Residual-stream methods (probes, logit lens, steering) "applicable in principle" to MoE, untested; open subject models in X1-X3 (DeepSeek V4 Flash, GLM-5.3, Kimi K3) are all MoE, so behavior differences mix architecture with lab/data/RL | Read |
| Dubiński, Betley et al. 2026, *Conditional misalignment* | `em_personas` row 26 | **Directly relevant to E13's conditional/triggered EM.** Models: Qwen3-32B (dense) and **DeepSeek-V3.1 (an MoE)**, fine-tuned through Tinker LoRA, rank 32. Conditional misalignment (EM only behind a trigger) shows in both, but the strength differed: the data-mixing effect and an off-policyness effect appear for DeepSeek-V3.1 but "not for Qwen3-32B" under non-verbatim triggers (App. F, line ~1155 of my text extraction) | PDF text extraction; grep of model names, LoRA settings and the appendix passage. Body not read start to finish. The model difference is **confounded** (dense vs MoE, but also 32B vs 671B, different lab, different post-training) |
| Turner et al. 2025, *Model Organisms for EM* | `em_personas` row 2 | Source of the Qwen2.5-7B bad-medical-advice organism used in E1. EM across Qwen, Llama and Gemma, down to 0.5B, rank-1 LoRA. **No MoE** | PDF grep (model names, LoRA) |
| Soligo et al. 2025, *Convergent Linear Representations of EM* | `em_personas` row 3 | Qwen2.5-14B-Instruct; a mean-difference direction recovers EM. **Dense only** | PDF grep of model names |
| Chen et al. 2025, *Persona Vectors* | `em_personas` row 6 | Method for E11/E13. Models are **Qwen2.5-7B-Instruct and Llama-3.1-8B-Instruct, both dense.** The persona-vector recipe is therefore already validated on our E11-E13 base | PDF grep of model names; method read earlier in the repo (E10, research-proposals) |
| Zhao et al. 2026 (template-token account) | cited in `docs/experiments/E1/prereg.md` | Restoring the opening chat-template tokens' KV states repaired EM on Llama-3.1-8B and Qwen2.5-7B; on **Qwen3-8B** the assistant header mattered instead. Shows Qwen2.5 and Qwen3 differ in where EM sits | Via the E1 prereg's citation and the E1 report; not re-read for this review |
| Zhang et al. 2026, *What Shapes EM?* | `em_personas` row 33 | Training-loss vs out-of-domain alignment across datasets and families; the INDEX says **no MoE content** | Abstract only |
| Shi, Chen, Shen 2026, *Single-Direction Attack on a 320B MoE* | `interp_methods` row 76 | One residual direction (best layer 22 of 45, depth 0.49) removes refusal in GLM-5.3-Flash. Evidence a single residual direction controls a safety behavior in an MoE. Not EM, not a fine-tune | §2.3, §4.2-4.3 verbatim per INDEX; results via fetch summary |
| Yap 2026, *Behavioral Steering in a 35B MoE via SAE-Decoded Probe Vectors* | `interp_methods` row 78 | Qwen3.5-35B-A3B (a GatedDeltaNet/attention hybrid MoE): decode-only steering had no effect (p > 0.35), all-position steering did (d = -0.76); five intended traits collapsed to one agency axis; coherence collapses above alpha = 5 | Fetch-tool summary only |
| Aranguri and Bloom (steering Kimi K2.5), via `logs/litreviews/20261006-X4-internal.md` line ~93 | n/a (X4 review) | Contrastive system-prompt steering vectors on a large open MoE (every 4th layer, 10 to 38) work. Serving details not given | Second-hand, via our X4 review |
| vllm-lens (UK AISI) | `interp_methods` row 77 | Per-request residual steering/capture in vLLM; validated only on vLLM 0.30.0; forces `enforce_eager`; covers MLA and linear-attention hybrids | README and compatibility doc verbatim; code not read |
| X4 internal review, line ~214 and ~244 | n/a | Advice: on an MoE steer the residual stream, not expert outputs; log router entropy as a diagnostic (inference, no source tests it). `emx.i9.acts` and `emx.i1a.capture` are written for dense Qwen/Gemma and **need module-path changes for MoE blocks** | Read |
| I9 prereg Amendment A and `logs/I9-report.md` | n/a | An exploratory Qwen3.6-35B-A3B arm (rev `995ad96e...`, residual at the last prompt token plus router logits into probes) was written and then **cut in v0.3**. No MoE probe result exists | Read |
| Chinese-flash judges / API subjects (X3, X3b, S4) | n/a | GLM-5.3 Flash (MoE) is X3's cheater: neutral cheat rate 30%. These are black-box behavior results, no interp, not a dense-vs-MoE comparison | Read via the X7 internal review (`logs/litreviews/20261007-X7-internal.md` §3) |
| Neel's materials (`papers/neel.txt`, `_neel_brainstorm_text.txt`, `_litreview_text.txt`) | n/a | grep for moe / mixture / expert / routing / sparsity: **nothing.** The only hits are an unrelated "mixture" in the working repo and "GPT-OSS-20B" as an agent model in the lit review (line 747). Neel has not, in these files, said anything about MoE | grep only |

## 2. What we know, by topic
**EM in MoE.**
- Doan et al. (abstract only) is the sole result: EM occurs in three MoEs; EM is negatively correlated with sparsity (sparser, less EM); expert-only fine-tuning still produces EM, "to a lesser degree". Three models from three labs with different post-training, so the sparsity correlation is not a controlled architecture effect (E8 review, BLOCKING 1). We cannot say whether they included a dense sibling or used LoRA or full fine-tuning.
- Conditional misalignment (row 26) shows EM-like behavior on an MoE (DeepSeek-V3.1) via LoRA, so **MoE EM and triggered EM are both known to be inducible**. The model difference there is a hint, not a finding.
- Our own prior: none. E1 and E1b ran dense OLMo-3-7B and dense Qwen2.5-7B only.

**Persona vectors, steering, probes in MoE.**
- Persona Vectors was validated on dense Qwen2.5-7B and Llama-3.1-8B only.
- Residual-direction control of a safety behavior in an MoE is shown for refusal (Shi et al., GLM-5.3-Flash) and for contrastive steering (Kimi K2.5). In a Qwen3.5 hybrid MoE, steering position matters (prefill vs decode). All three are far from 7B-3B-active scale and none is EM.
- The residual stream is the same object in an MoE, so activation-addition and linear probes are applicable in principle. Nothing in `papers/` tests whether a persona direction extracted on an MoE is as clean, or whether an MoE needs a different layer fraction.
- Inference, not sourced: an MoE adds a channel (routing) that can make a "text-gated" or "probe-gated" patch (E13) behave differently if the patch shifts routing. Log router entropy as a diagnostic (X4 review).

## 3. Candidate dense/MoE pairs near a 7B dense base
All entries in this table are **UNVERIFIED (memory)** unless a repo source is named. Check the HF configs before a prereg.
| Pair | Same family / generation / tokenizer? | Fit for Qwen2.5-7B base | Public EM organisms / persona adapters |
|---|---|---|---|
| **Qwen3-8B (dense) vs Qwen3-30B-A3B** | Yes: same Qwen3 generation and tokenizer; ~128 experts, 8 active, ~48 layers, hidden width 2048 (memory) | Not the same generation as Qwen2.5-7B, so Qwen3-8B is a needed bridge. **Qwen3-30B-A3B is in Doan et al.**, so results line up with the one published prior. Zhao et al. (cited in E1) studied Qwen3-8B EM. Hybrid thinking templates (`enable_thinking=False`) | None known for the MoE. Turner et al.'s organisms are Qwen2.5 and Llama, not Qwen3 |
| **Qwen3.6-27B (dense) vs Qwen3.6-35B-A3B** | Yes (repo: E8, I9 read both configs 2026-10-03; 35B-A3B has 40 layers, width 2048, 256 experts, 8 active; rev `995ad96e`) | Already scoped by E8 and I9, so a pipeline half-exists. 27B is far from a 7B base | None known |
| Qwen1.5-MoE-A2.7B (14.3B total) or Qwen2-57B-A14B | Qwen1.5/2 generation, older tokenizer than Qwen2.5 (memory) | **Qwen2.5 had no open MoE sibling that I know of**, so no clean same-generation pair for Qwen2.5-7B exists | None known |
| OLMoE-1B-7B (AllenAI) vs OLMo dense | Open data and checkpoints; ~7B total, ~1.3B active, 64 experts, 8 active (repo E8 optional arm; memory). The tokenizer and data mix differ from OLMo-3 (memory, unchecked) | Ties to the E2/E3 open-data story, but 1.3B active may show weak EM, and OLMo-3-7B is a different generation (E8 flags this) | None known |
| gpt-oss-20b (~21B total, ~3.6B active, 32 experts, top-4, MXFP4; memory) | No dense sibling | In Doan et al. Reasoning-format ("harmony") chat template; reasoning model, so fine-tuning on short answers is off-distribution | None known |
| Mixtral-8x7B | Mistral-7B is a rough dense relative, but a different generation, not instruct-matched | In Doan et al. 47B total: heavy | None known |
| DeepSeek-V3.1 / GLM-5.3 / Kimi K3 | No small dense sibling | Used in conditional-misalignment (V3.1) and X3 (GLM); far too large to fine-tune locally | Conditional-misalignment LoRAs are probably not public (not checked) |

**Gap I could not close from files:** I found no public EM or persona-vector adapter on any MoE in `papers/` or the INDEX files. Whether Doan et al. released theirs is unknown because the paper is MISSING.

## 4. Practical issues (mostly UNVERIFIED (memory); the repo supplies the E8 numbers)
- **Memory.** Repo (E8): Qwen3.6-35B-A3B bf16 is ~70 GB, so it does not train on one 80 GB H100; priced as 2xH100 or one H200 at an assumed ~$7/h (unmeasured). Qwen3-30B-A3B bf16 is ~61 GB (memory), tighter than comfortable for LoRA on 80 GB. A 4-bit load (QLoRA) or two cards is the likely route.
- **LoRA targeting.** Attention-only LoRA works on any MoE and is cheap. LoRA on expert MLPs is harder: recent transformers fuse the experts into one parameter, so adapters need `target_parameters` or a per-expert layout (memory; check the installed PEFT/transformers version). E8 plans attention + shared expert + every routed expert's MLP, router `mlp.gate` frozen. Attention-only on both models would be the controlled-recipe default for a dense/MoE comparison; Doan et al.'s expert-only result suggests experts alone can carry EM, so which modules you adapt is itself an experimental variable.
- **Training speed.** E8's note: HF's MoE loop over experts is slow; E8 assumes ~1.5 h per MoE adapter vs ~0.43 h per 7B dense (E1 measured), a throughput pilot is required.
- **vLLM.** Qwen3 MoE is supported by vLLM; LoRA serving on attention is supported; LoRA on expert weights has been patchy (memory, version-dependent). Steering plugins (vllm-lens) were validated on one vLLM version only and force `enforce_eager`; our `emx.i9.acts` / `emx.i1a.capture` hook paths are dense-only and need module-path changes for MoE blocks (X4 review).
- **Chat template.** Qwen3 and Qwen3.6 are hybrid-thinking: disable thinking, and assert the unmasked response-token count is above 0 (inherited gotcha). Qwen2.5-7B has no thinking template, so prompts are not byte-identical across the pair.
- **Persona-vector layer choice.** An MoE with 48 layers and width 2048 will not share the 7B's layer index. Shi et al.'s best layer was at depth ~0.49 of the network. Use depth fractions, not indices.
- **Cost anchor.** E8 Stage 1 (dense 27B + MoE 35B-A3B, 4 adapters, matching checkpoints, judging) was ≈ $65-75; the OLMoE arm ≈ $10-15; the combination ≈ $110-125, above the ~$100 ask-first line. A Qwen3-8B/30B-A3B pair with attention-only LoRA would be cheaper than E8's, but there is no measurement.

## 5. Our own prior results that bear on this
- **Dense Qwen2.5-7B-Instruct is our best-characterised EM subject.** The published organism `ModelOrganismsForEM/Qwen2.5-7B-Instruct_bad-medical-advice` is the E1 reference (E1 prereg line ~759); E1 trained 13 7B models in 5.6 H100-hours.
- E1 report: the assistant-header intervention removed most EM on OLMo, matching what Zhao et al. found for Qwen3-8B. The chat-template mechanism differs between Qwen2.5 and Qwen3, which means a Qwen3 dense baseline could behave unlike the Qwen2.5 base, a reason to pair the MoE with a **same-generation** dense model and not directly with Qwen2.5-7B.
- X3/X3b: GLM-5.3 Flash (MoE) cheats 30% neutral, with Qwen3.6-27B (dense) at 10% and DeepSeek V4 Flash (MoE) at 0/60. Black-box, no matched sibling, so this says nothing on architecture (our own Topic 3 note says so).
- E1 caps and the combined spend guard exist, but no MoE tooling does. E8's own code note: "MoE LoRA targeting, router hooks and forced routing are new".

## 6. The gap
- No test of EM, persona-vector or steering transfer across a **same-family dense/MoE pair**, with a matched narrow-task learning rule. Doan et al. is cross-lab, three MoEs, no stated dense baseline (abstract).
- Nothing on the **repair or inference-time patching** questions (E12/E13) in an MoE: whether a repaired EM relapses, whether a probe-gated or always-on steering patch works when routing can shift.
- Conditional (triggered) EM in an MoE has one hint (DeepSeek-V3.1 vs Qwen3-32B, confounded).
- Predicting a dataset's effect on character before training (E11) has no MoE evidence at all.

## 7. Strongest prior against running the MoE arm
- Doan et al. and the E8 review both say a dense-vs-MoE comparison on one pair does not isolate architecture. Any pair differs in depth, width and size; a null for one pair does not license "MoE works like dense".
- The arm is conditional on E11-E13 working on the dense base, but extra confounds enter at once: a different generation, thinking templates, new MoE LoRA tooling, a larger machine, and possibly weaker EM at 3B active parameters.
- Our E8 proposal was held for exactly this reason (gated on E1b and Doan et al.) and I9's MoE arm was cut to pay for higher-priority work. MoE results never made a decision.

## 8. MISSING / unread sources
- **Doan et al. (`em_personas` row 32): MISSING.** Needs a browser save to `papers/em_personas/` before any MoE prereg. What it says about a dense baseline, the LoRA recipe, the expert-only result and its released artifacts is unknown.
- Qwen3 technical report and the Qwen3-30B-A3B / Qwen3-8B model cards, OLMoE paper, gpt-oss model card: not in `papers/` at all. All Section 3 and 4 facts about them are memory.
- No source in `papers/` on LoRA/PEFT/vLLM support for MoE experts.
- Conditional misalignment (row 26): only grepped; its dense-vs-MoE comparison needs a proper read before E13 leans on it.
- Persona Vectors and Model Organisms: grepped for model names only in this review.
