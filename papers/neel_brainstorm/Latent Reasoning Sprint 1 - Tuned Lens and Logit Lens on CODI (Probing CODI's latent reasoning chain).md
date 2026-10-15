# Latent Reasoning Sprint #1: Tuned Lens and Logit Lens on CODI (a.k.a. "Probing CODI's latent reasoning chain with logit lens and tuned lens")

Author: Realmbird. Published 2026-03-06. https://www.lesswrong.com/posts/c4LYmzEC6cFQzrDZz/latent-reasoning-sprint-1-tuned-lens-and-logit-lens-on-codi (also on the Alignment Forum). Follow-up: "Latent Reasoning Sprint 2: token based signals and linear probes", https://www.greaterwrong.com/posts/jr2KoepMtsGogsRJW/ (not captured).

**CAPTURE TYPE: PARAPHRASE.** This is a structured summary from a fetch tool (a small model summarising the page), not the post's verbatim text. Check the source URL before quoting. Retrieved 2026-10-12 (P15 literature unit).

## Summary (as returned by the fetch)
- **Setup:** the public CODI Llama-3.2-1B checkpoint `codi_llama1b-answer_only` (Cywinski et al.'s). Tuned lens trained with AlignmentResearch `tuned-lens` code on GSM8K, 500 samples, 3 epochs. Example: a team of 3 recruits 5, then each member recruits 2 more; answer 10.
- **Plain vs tuned lens:** plain logit lens shows intermediate values (8, 16) but never the answer 10; the tuned lens shows 10 at odd latents (reported as absent from even latents 2, 4, 6 on that example).
- **Lenses trained directly on CODI latents** did not beat text-trained lenses. The CODI-trained lens outputs end-of-CoT tokens for latents 6+; odd-latent lenses (1, 3, 5) recover 10 but fail on latent 0 (author suspects overfitting, since the lens mirrors the final layer). A lens on latents (3, 5) only did not show 10; adding latent 1 restored it.
- **Entropy:** latents 3 and 5 have the highest entropy across lens variants, read by the author as active computation.
- **Final-answer detection in top-k:** highest for the directly trained lens, and highest at even latents 2 and 4 (figures only; no numbers in the text).
- **Reading:** consistent with Scratchpad Thinking's even = store, odd = compute alternation (author calls this suggestive). Sprint 2 (per search snippet): the CODI tuned lens fails to generalise to non-CODI activations.
- **Limits (author's and ours):** no patching, no comparison with verbal CoT, no error analysis, no monitor framing; rates are shown only in figures.
