# Are Stated Reasoning Steps Causally Load-Bearing?

**Authors:** Abhiram Bhupatiraju, Rayan Nyaupane
**Venue:** arXiv preprint 2609.27038, v1 22 Sep 2026.
**Source URL:** https://arxiv.org/abs/2609.27038
**Retrieved:** 2026-10-12 UTC, for probe P11. **Only the abstract was read**, from the arXiv abs page fetched with curl. The body (methods, tables) was not read. A fetch-tool summary of the abs page added nothing beyond the abstract.

## Abstract (verbatim)

Chain-of-thought (CoT) monitoring assumes that the reasoning a model writes reflects the computation that directly produces its answer. Previous faithfulness metrics have been predominantly behavioral, as they simply edit the reasoning text and observe the resulting answer. However, our methodology aims to measure faithfulness causally at the activation level, specifically on self-generated reasoning. Unlike previous causal audits, which measure degradation, our interventions carry a known predicted target. In this way, each patch should switch the answer to a specific counterfactual entity derivable by construction. Specifically, we use synthetic multi-hop lookup tasks (2-6 hops). We patch the residual stream at the token span where the model states each intermediate step with the corresponding activations from a counterfactual run. For Qwen3-4B, 76.9% +/- 2.8% of stated steps are causally load-bearing (CLB) at the most responsive mid-network layer (random-position null: 11.3%; patching the underlying prompt fact: 83%, so stated steps carry approximately 96% of the achievable effect). Moreover, the standard behavioral test on the same items yields 88.2%, which overstates causal faithfulness by 11.4 percentage points (item-matched; 111:14 discordant pairs, p < 1e-15) and, for the easiest items, by up to 20 percentage points. This gap also has a clear capability dimension. Qwen3-1.7B is far less causally faithful overall (54.8%), with its faithfulness collapsing as reasoning depth increases (68% at 2 hops to 30% at 6), while Qwen3-4B remains relatively flat. Although stated reasoning can be causally meaningful, standard behavioral tests tend to overestimate its causal faithfulness, particularly on easier examples where model reasoning appears most fluent.

## Bearing on P11 (our inference from the abstract only)

- **The same design pattern as P11's manipulation check.** A residual-stream patch from a counterfactual run, at a mid-network layer, with a known predicted target entity, scored by whether the model's answer switches to it.
- **Two differences from P11:**
  - Its bridge is stated in the CoT, not latent.
  - The readout is the model's answer, not an activation verbalizer.
- **Two design points P11 can borrow:**
  - **A random-position patch null.** It gave 11.3% here. P11 has no position null. A patch at random positions, or one taken from another item's premise run, would serve.
  - **A ceiling from patching the underlying prompt fact.** It gave 83% here. P11's direct premise arm plays this role.
