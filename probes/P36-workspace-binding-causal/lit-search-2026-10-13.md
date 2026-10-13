# P36 literature check, 2026-10-13 (gap-filling pass after the I12 reviews)
Read depth: "fetch" = WebFetch (page converted, answered by a small model; NOT verbatim full text, and long pages were truncated); "search" = search snippet only.

## 1. COMPASS (arXiv 2511.20910v2, https://arxiv.org/html/2511.20910v2) - fetch, first 100k of 169k chars
- Role-crossed minimal pairs: prompts differ only in the role scaffold with predicate and filler fixed ("delivered the package to the" Goal/office vs "...with the" Instrument/truck). Single-token fillers, matched length, kept only if the model is right in both.
- Models: Pythia-14M/410M/1B, LLaMA-1B. No 2026-era models.
- Intervention: edge attribution patching with integrated gradients (top 200 edges), not interchange patching. Roles are adjunct/oblique roles (Instrument, Location, Time, Beneficiary...), NOT agent/patient. Faithfulness of circuit, Jaccard stability, node mass (top 20 nodes hold 89.7-91.7% of attribution).
- Findings: role circuits emerge gradually (Instrument step 32, Location 128, Time 512, Beneficiary never). Pythia-1B vs LLaMA-1B node overlap 0.51, edge overlap 0.11-0.17.
- Not a cross-position entity-state patch and no residual decomposition. Little to compare numerically.

## 2. Gurnee et al. 2026 itself (arXiv 2607.15495, https://transformer-circuits.pub/2026/workspace/index.html) - fetch, chars 0-200k of 336k
- Limitations section "Beyond a bag of concepts": a readout with "spider, legs, eight" shows the concepts but "not how they are bound together"; assigning roles / relations is flagged as outside what the J-space shows. No agent/patient experiment in the parts read (the last 136k chars were not read).
- Precedent for the exact split P36 uses (Claude models, not Qwen): patching in lens coordinates (swap two J-lens coordinates, orthogonal part unchanged) flips two-hop answers in 54% (Haiku 4.5) / 70% (Sonnet 4.5) / 70% (Opus 4.5) of 50 prompts. On 90 prompts, swapping the J-space part of an intermediate's probe flips the answer 61% vs 60% for raw J-lens swap; swapping the non-J remainder flips 28%; clamping J coordinates to clean values cuts the non-J swap to 6% (non-J effect mediated through J-space). Use these as reference magnitudes and as a design warning: a non-J patch can act via J.
## 3. J-space vs non-J splits elsewhere
- Viswanath LW comment (July 2026; from the I12 review, read there): injected non-J concept parts causal but unreported (+21 nats) on Llama-70B. Concepts, not roles.
- TransformerLens PR #1741 https://github.com/TransformerLensOrg/TransformerLens/pull/1741 (fetch): offline sparse J-space coordinate substitute/swap with residual held fixed; no binding experiment, "causal-swap benchmark out of scope". Useful as a reference implementation of the split (x = residual + reconstruction).
- WeZZard/jlens-qwen36 https://github.com/WeZZard/jlens-qwen36 and HF blog https://huggingface.co/blog/dlouapre/j-space (search only): Qwen3.6-27B J-lens tooling and explainer; nothing on binding.
- Searches for J-space/J-lens/workspace + binding/roles/agent/patient (Aug-Oct 2026) found nothing (search snippets only; LW/AF search tool coverage is weak).

## 4. Causal studies of agent/patient or role binding
- Nothing 2025-26 found doing interchange/entity-state patching on agent/patient. Closest: Denning et al. 2504.16884 (correlational, old models; read in I12 pass); Dai et al. 2604.19052 cell-based relational binding (read in I12 pass; Llama3-8B/Qwen3-8B, layer ~15, 2-5 PLS dims, index-based not role semantics); Dai et al. 2409.05448 and Feng & Steinhardt 2310.17191 (search summary): patching a binding subspace flips which entity is bound.
- New: "A Unifying Perspective on LM Representations: From Filler-Role Structure to Mechanistic Interpretability", arXiv 2608.29034 (fetch of abstract only): tensor-product (filler-role) account; claims activation patching can be derived as filler-role editing, comparable restoration on OLMo-2-13B (from search snippet). Theory framing, not a role-binding measurement.
- "The Curse of Multiple Mediators" (June 2026, search snippet only): patching natural indirect effects mix in interaction terms; relevant caveat when attributing a flip to one component (J part vs residual).

## Bearing on P36
- Nothing found answers the question; the J vs non-J split for agent/patient roles appears open. Gurnee et al. name binding/roles as an open limitation, and give no role result.
- Design: report the cross-check Gurnee used (clamp J coordinates when patching the residual, to test mediation through J: their non-J effect fell 28% -> 6%); compare flip rates to their 61% (J part) vs 28% (non-J) on two-hop as a loose reference only (different model, task, metric).
- Caveat: Denning et al.'s negative on pooled last-token vectors implies measure role at argument-token positions; COMPASS covers adjunct roles on 1B models, so no number to match.
