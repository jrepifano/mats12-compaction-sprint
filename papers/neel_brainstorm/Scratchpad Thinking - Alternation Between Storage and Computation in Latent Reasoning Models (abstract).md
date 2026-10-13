# Scratchpad Thinking: Alternation Between Storage and Computation in Latent Reasoning Models

Authors (NeurIPS 2025 page): Sayam Goyal, Brad Peters, Maria Emilia Granda, Akshath Narmadha, Dharunish Yugeswardeenoo, Callum McDougall, Sean O'Brien, Ashwinee Panda, Kevin Zhu, Cole Blondin. NeurIPS 2025 (Mechanistic Interpretability Workshop per a search snippet). Pages: https://neurips.cc/virtual/2025/127166 ; OpenReview https://openreview.net/forum?id=cW6vOUYwCB (the NeurIPS page's link; a search result also showed https://openreview.net/forum?id=EV30qkZXrR for the same title). No arXiv id found. Code: github.com/sayam-goyal/Scratchpad-Thinking (per search snippet; not checked).

**CAPTURE TYPE: ABSTRACT ONLY, PARAPHRASED** (from the conference page via a fetch tool). The full paper was NOT read. Retrieved 2026-10-12 (P15 literature unit).

## Abstract (paraphrase)
Mechanistic case study of CODI (model size and checkpoint not stated in what we saw; the lit-review plan note says GPT-2). Using attention analysis, SAE-based probing, activation patching and causal interventions, the authors find a "scratchpad computation" cycle over CODI's six latent steps: even-numbered steps store numerical information, odd-numbered steps perform the corresponding operations. Interventions on numerical features hurt most at scratchpad (even) steps; forcing early answers gives accuracy jumps after computation (odd) steps. They argue this supports auditing latent reasoning models before deployment. Code and data "upon acceptance".

Critique (from "Observable Patterns Are Not Explanations", arXiv 2606.12689, abstract-level): logit-lens alternation is reproduced, but CoT-only controls also show CODI-like alternation with different magnitude and phase.
