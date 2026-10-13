# P05 literature check: looped LMs and no-CoT serial depth (2026-10-12)

**How these were read.** Internal sources were read verbatim from the repo. Every external source below was read through WebFetch summaries of the arXiv abstract or HTML page. Quoted phrases come from the fetch tool, so check them before citing. No PDF was read in full. The McLeish context length was also checked verbatim in the saved config.

## Internal
- Neel's open problem: "Do looped models have notably higher serial depth? ... I'd study nanbeige or ouro" (`papers/_neel_brainstorm_text.txt:54-55`).
- The Astra looping appendix covers Huginn-0125 (r 1–64), Ouro-1.4B/2.6B and Nanbeige4.2 against Nanbeige4 (a different pre-train). It measures only NCRI, bits-per-byte and knowledge, with no crossing. Neel says: "sufficiently noisy that I don't really know what to make from them", and "early loops ... are under-elicited ... then converge to / get parallel to the trend in middle loops" (`papers/neel_brainstorm/Astra can do a concerning amount with no chain of thought.md:182-192`).
- The serial-depth method: the 50% crossing and the nudge-depth check (same file, lines 87-91).
- P06 found that whether filler helps depends on prompt format. This is why P05 runs two formats (`probes/P05-looped-serial-depth/plan.md` Notes).
- Nothing else in `papers/`, `probes/*/note.md` or `docs/research-proposals.md` covers looped models. The proposals mention serial depth only as C1's motivation (`docs/research-proposals.md:2869`).

## Closest prior work, and the gap it leaves
1. **Popescu, Sáez de Ocáriz Borde and Liò 2026** (arXiv 2608.18171). This is the closest matched-baseline result.
   - Setup: the retrofitted Llama-3.2-1B and OLMo-2 against non-looped models given the same SFT, on tool calling (API-Bank, BFCL, NESTful). Depths 1, 2, 4 and 8 only, bf16.
   - Result: "Accuracy on multi-step tool use generally increases with recurrent depth". Llama BFCL rises from 21.4 to 32.9 at depth 8, with the looped model "substantially weaker" than native looped models.
   - Gap: they ran their own SFT rather than using McLeish's released baseline, the task is not a controlled serial chain, there is no crossing, and nothing above r = 8.
2. **Kohli et al. 2026** (arXiv 2604.07822, COLM). Models trained from scratch on k-hop knowledge graphs.
   - Results: vanilla transformers fail at out-of-distribution composition. Depth extrapolation needs more test-time iterations (R = 6 reaches 14 hops, R = 8 reaches 19, after training to 12).
   - **"Overthinking":** the logit margin rises, peaks and then falls as iterations grow.
   - Their logit lens reads the bridge entity per iteration. Gap: toy models, not a pretrained LM.
3. **Lu et al. 2025** (arXiv 2507.02199), on Huginn-3.5B.
   - Logit lens and coda lens disagree block by block: digits show at R1–R3 under the logit lens and at R4 under the coda lens. Ranks also oscillate periodically.
   - "no clear evidence of latent CoT": intermediate and final ranks fall together, with no phase separation.
   - GSM8K without CoT barely moves (3.11 at r = 4, 4.93 at r = 32, 4.70 at r = 64).
4. **Wang and Reid 2026** (arXiv 2609.01924). A Jacobian lens on Ouro-2.6B and Huginn against Qwen3.6-27B.
   - Ouro's content can be reconstructed in every loop.
   - In Huginn, reads, writes and ablations act within a window of about 2 recurrences.
5. **The McLeish paper itself** reports only standard benchmarks up to r = 32, and gives no serial or multi-hop evaluation.

**The gap P05 fills:** no source found measures a 50% serial-depth crossing against test-time recurrence in a *pretrained* looped LM against a *matched* non-looped baseline. No source reads chain states per loop on the retrofitted Llama.

## What could change our interpretation
- **Fixed-point convergence.** Blayney et al. 2026 (arXiv 2604.11791) find that "Huginn-0125 and Retrofitted Llama demonstrate fast convergence to a fixed point, Ouro does not". The cyclic fixed point appears "immediately following the prelude".
  - Expect d₅₀ to plateau well before r = 32. Extra loops past convergence add nothing, so a flat curve over r means converged, not that recurrence failed.
  - Cheap diagnostic: log ‖s_t − s_{t−1}‖/‖s_t‖ per loop.
- **Contraction versus chaos.**
  - Dau et al. 2026 (arXiv 2609.19934) injected noise at 10% of the state norm. Huginn's ratio per iteration is 0.892, the only model below 1; the dense models sit at 1.01–1.06.
  - Kiwitt et al. (ICML 2026 MI workshop) find Ouro-1.4B "mildly chaotic" and Huginn converging in every dimension.
  - Nobody has measured this for the retrofitted Llama.
  - **Our 25% bf16/fp32 argmax agreement does not fit a contracting fixed point** if the logits are confident. More likely explanations: near-tied logits at floor accuracy, or bf16 noise steering the state to a different attractor.
  - Luo and Yu 2026 (arXiv 2609.21383) ran a precision study on Raven-lineage models. Exact zero-change plateaus between adjacent depths "disappear under higher precision".
  - So run fp32, as the plan's rule says. Also report agreement restricted to confident items, so the 25% isn't read as chaos.
- **Overthinking and loops beyond training.** Training draws r from a Poisson-lognormal with mean 32 and backpropagates through only the last 8 passes. That makes r = 64 out of distribution, and Kohli et al. show margins can fall there. A non-monotone fit at r = 64 is therefore expected, not a bug.
- **Low r is a confound.** Dau et al. show that truncating depth also changes what the readout head (the coda) sees. A low score at r = 1 is partly distribution shift, so the within-model loop gain overstates what depth itself buys. The matched-baseline Δ is the cleaner headline, as planned.
- **Adaptive exit.** Huginn exits on KL < 5×10⁻⁴ between successive steps (Geiping et al. 2025). Kohli et al. found that criterion halts too early and added an entropy term. The McLeish models have no learned halting ("evaluated only under fixed-depth inference"), so fixed r is right.
- **Readability.** Expect lens dependence (Lu et al.) and readability only within a short window (Wang and Reid, on Huginn). Read with both the logit lens and the coda lens at every loop. A null on Huginn is the published prior.

## Training context length
**1024 tokens.** The paper says "train all models with a context length of 1024" (fetch summary of arXiv 2511.07384), and this applies to the non-recurrent baseline too.
- The saved config agrees: `probes/P05-looped-serial-depth/sources/rec32_config.json:13` has `"block_size": 1024`, read verbatim.
- Training ran in bf16 mixed precision.
- The state init is `"state_init": "like-init"` (config line 57). Huginn uses a truncated normal with σ² = 2/5. The retrofit's σ was not found in the paper or the README.
- Our longest prompt is 908 tokens, under the limit. For comparison, Huginn trained at 4096.

## Citations
#### McLeish et al. 2025
Teaching Pretrained Language Models to Think Deeper with Retrofitted Recurrence. https://arxiv.org/abs/2511.07384 ; code https://github.com/mcleish7/retrofitting-recurrence
#### Geiping et al. 2025
Scaling up Test-Time Compute with Latent Reasoning: A Recurrent Depth Approach (Huginn). https://arxiv.org/abs/2502.05171
#### Lu et al. 2025
Latent Chain-of-Thought? Decoding the Depth-Recurrent Transformer. https://arxiv.org/abs/2507.02199
#### Kohli et al. 2026
Loop, Think, & Generalize: Implicit Reasoning in Recurrent-Depth Transformers. https://arxiv.org/abs/2604.07822
#### Blayney et al. 2026
A Mechanistic Analysis of Looped Reasoning Language Models. https://arxiv.org/abs/2604.11791
#### Popescu et al. 2026
Looped Language Models Improve Compositional Tool Calling. https://arxiv.org/abs/2608.18171
#### Wang and Reid 2026
Looped Transformers under the Jacobian Lens. https://arxiv.org/abs/2609.01924
#### Dau et al. 2026
Beyond Depth Truncation: Controlled Evaluation of Depth Utilization in Recursive Language Models. https://arxiv.org/abs/2609.19934
#### Luo and Yu 2026
Prediction Dynamics in Depth-Recurrent Language Models. https://arxiv.org/abs/2609.21383
#### Kiwitt et al. 2026
Lyapunov Spectral Analysis of Loop Transformer Dynamics (ICML 2026 MI workshop). https://icml.cc/virtual/2026/79384
#### Huang et al. 2026
Towards Looped Models Done Right, Part II: Rethinking at Fixed Points. https://arxiv.org/abs/2610.06833
#### Nanda 2026
Astra can do a concerning amount with no chain of thought (looping appendix). https://www.lesswrong.com/posts/eRmzz8J8Qkzqvzrgg
