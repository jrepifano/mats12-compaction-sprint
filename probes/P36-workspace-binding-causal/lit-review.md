# P36 literature review (pointer and summary)

The brief's literature section points to the I12 reviews done on 2026-10-12/13; they are the review of record for this probe and are not repeated here:

- Internal: [`logs/litreviews/20261013-I12-internal.md`](../../logs/litreviews/20261013-I12-internal.md)
- External: [`logs/litreviews/20261013-I12-external.md`](../../logs/litreviews/20261013-I12-external.md)
- The proposal's "Prior work and the gap": `docs/research-proposals.md` §I12.

What shapes P36's design:

- **The split protocol exists for concepts and intermediates, not roles.** Gurnee et al. 2026 (`papers/interp_methods/` row 1) define an activation's J-space component as a sparse non-negative combination of k ≤ 25 J-lens vectors (rows of W_U J_ℓ), found by sparse pursuit, and its non-J component as the remainder. Their concept-vector split (Fig. 8) and intermediate-probe split (Fig. 16) favour the J part; binding ("Beyond a bag of concepts") is listed as open.
- **Closest prior J vs non-J work:** Viswanath (LessWrong, 2026-07; `papers/interp_methods/` row 98) splits synthetic concept injections on Llama-3.3-70B by a top-r rank cut; the non-J half is causal but unreported. A rank cut is not Gurnee's sparse cut, and his vectors were not natural activations or relations.
- **Binding subspaces** (Feng & Steinhardt 2024; Feng, Russell & Steinhardt 2024; Dai, Heinzerling & Inui 2026) are causal and low-rank, at entity/attribute tokens, but cover entity–attribute or order indices, not agent/patient, and are never compared with a lens. Feng & Steinhardt's cross-position substitution of entity-token states is the template for P36's entity-site patch.
- **Agent/patient:** Denning et al. 2025 (arXiv 2504.16884) find role reversals barely decodable from pooled sentence vectors in old models; correlational only.
- **J++ Lens** (Ayonrinde & Lindsey 2026, `papers/interp_methods/` row 81): a drop-in per-layer linear map; released for Qwen3.6-27B at `koayon/jpp-lenses` (`qwen3.6-27b/lens.pt`, format checked 2026-10-13 against `safety-research/jpp_lens` @ 93f02cd: `{"parameters": {"jacobians": {layer: [d, d]}}, "source_layers", "config"}`, transport `J_ℓ h`; its README defines W_U as "the model's final norm and LM head").
- **Repo constraints:** P11 found that Oracle Lens on Qwen3.6-27B adds nothing over asking Qwen and can re-answer, so any reader arm needs an ask-Qwen-under-the-same-patch comparator. No sparse pursuit existed in the repo; the vendored MIT NNOMP (`third_party/olens_draft/oracle_lens/core/nnomp.py`) is the starting point.

Gap (both reviews): no work decomposes natural activations into the J-lens reconstruction and residual and asks which carries role information that controls the answer. Gap confidence in the external review: about 75–80%, from a shallow search.

A further quick search runs alongside the build (AGENTS.md, probe tier); its findings go in `note.md`.
