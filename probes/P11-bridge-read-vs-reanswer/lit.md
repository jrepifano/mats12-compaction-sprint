# P11 literature check (internal + external)

2026-10-12, 01:15–02:30 UTC, run alongside the build. Web search plus arXiv pages fetched with curl. No paid calls.

**How each source was read:**
- *Verbatim* means we read the primary text ourselves: the arXiv HTML converted to text locally, a local PDF extraction, or a capture in `papers/`.
- *Abstract* means only the abstract, taken verbatim from the arXiv abs page.
- *Fetch summary* means only the fetch tool's summary of the page.
- *Snippet* means only search-result text.

## Bottom line

1. **P11's core measurement is not published, as far as we found.** We found no work that changes a model's latent bridge entity by an activation patch, holds the input fixed, and then checks whether an activation verbalizer follows the change.
   - The pieces exist separately:
     - Patching the bridge, with the readout on the model's answer ([Biran et al. 2024](#biran-et-al-2024); [Bhupatiraju and Nyaupane 2026](#bhupatiraju-and-nyaupane-2026)).
     - The reading-versus-own-knowledge critique of verbalizers, tested by fine-tuning rather than by patching ([Li et al. 2025](#li-et-al-2025)).
     - Readers that follow a *steering* intervention, on concepts rather than a bridge ([Fraser-Taliente et al. 2026](#fraser-taliente-et-al-2026); [Lindsey 2025](#lindsey-2025)).
   - The search was about 12 queries and four primary-source reads. It was not exhaustive.
2. **Prior work supports the limit the brief names, and sharpens it.** Li et al. (Appendix I) argue that when the reader shares the target's weights, reading an activation is "functionally equivalent" to the target finishing its own computation. A context-carrying patch therefore can't, on its own, separate "reads the bridge" from "re-answers with the premise in context".
   - The clean separation in the literature needs a **knowledge mismatch**: the target holds a bridge that the reader couldn't compute from whatever input it can decode. Li et al. build that by fine-tuning. P11 doesn't.
3. **Cheap checks still narrow it** (detail in "Cheap extra checks"). The best three:
   - (1) Does the lens name Y on patched cells where Qwen's answer does *not* flip? Add a dose sweep to make such cells.
   - (2) The same author premise, patched under a question that doesn't need the author.
   - (3) A premise naming a third person Z of Qwen's original nationality, so the bridge changes and the answer doesn't.
   - A free fourth: run the linear lenses on the same patched activations. They cannot re-answer, so any Y they name was read.

## Internal

- **P03 already flags the confound** ([note](../P03-text-proof-interp-items/note.md), Answer point 4 and Limitations). The Oracle Lens names Qwen's wrong bridge on 0.38 of 63 items, from layers 40–60 only. The shuffled control (0.02) rules out answering from the weights alone, but not re-answering from a decoded question. P03's [lit.md](../P03-text-proof-interp-items/lit.md) read Li et al. and Biran et al. and named the reader-shares-Qwen's-weights confound. Its "Next" step 2 is P11's design: change the bridge, keep the question, and check the answer behaviourally.
- **The Oracle Lens is trained to forecast Qwen's continuation.** This sharpens the re-answer worry. In WorkspaceBench's appendix ([Blank et al. 2026](#blank-et-al-2026); our capture in `papers/interp_methods/`):
  - The activation reconstructor is trained to predict "the activation preceding" an on-policy text span.
  - The inverter maps activations back to those spans.
  - The RL stage rewards bullets whose reconstruction explains the activation's variance.
  
  So the lens's native job is to say what text Qwen is disposed to produce next from that state. Naming the bridge (for example "Lillian Hellman was American") is exactly that kind of forecast. A forecast from the state is neither a pure "read" nor a pure "re-answer from the text", which is one more reason the two are hard to tell apart here.
- **The lens reads one vector.** Our wrapper (`src/emx/i1a/olens.py`, the WorkspaceBench recipe in `third_party/workspace-bench/src/wsbench/produce/methods.py`) handles a single activation (one layer, one position). It replaces the input embedding of one slot token in a fixed carrier prompt with α·unit(h). The carrier asks only for "distinct concepts that encode this activation"; there is no question slot. To re-answer, the lens would have to recover the question (and, under the patch, the premise) from that one vector. Li et al. found single-token inversion to be lossy (their Table 3).
- **WorkspaceBench names the shortcut.** It names "text inversion" as a way to score without reading the workspace, and says its benchmark "doesn't fully rule out" it. It uses no causal intervention.
- **I1a: text alone nearly matches the readers on multi-hop items** (`docs/experiments/I1a/report.md`). A prompt-only Gemini monitor scored 0.98 on WorkspaceBench multi-hop, against 1.00 for the Oracle Lens. P03's items were built to defeat that baseline. P11 is the next step: from "text can't predict it" to "the reader tracks the state".
- **Steering moves an NLA's readout** ([Fraser-Taliente et al. 2026](#fraser-taliente-et-al-2026), row 4, verbatim capture). Steering Opus toward or away from evaluation awareness raised or lowered NLA-measured awareness on all six evaluations, including MMLU and GPQA Diamond, where the model never verbalizes awareness.
  - This is the closest internal precedent for "the reader follows an intervention".
  - But the steering vectors are differences between prompts ("a prompt stating the model is undergoing testing" against a control). So a context decoder would follow them too: the same ambiguity P11 faces.
- **Swapping a workspace vector changes the model's own report.** In the global-workspace paper ([Gurnee et al. 2026](#gurnee-et-al-2026), row 1), swapping one workspace vector for another changes what the model says it is thinking about to match. The reader there is the model's own report, not a separate verbalizer.
- **P06 combined an intervention and readers.** Gemma lenses read the hidden value at the filler label (34–37% against a 12–14% null). A filler K/V transplant from a donor differing only in x moved the output to the donor's answer on 52% of pairs. Those readers are linear (logit lens, J-Lens, J++) and cannot re-answer. That is why check 4 below is free and clean, if underpowered.
- **Oracles trained on the target under-read its quirks** (rows 19 and 54, Bersia and Gaintseva 2026; Fornasiere et al. 2026). An oracle trained on a model with a quirk reads that quirk poorly. They bear on reader-target knowledge overlap, not on patching.
- **Nothing else in `papers/`, `logs/litreviews/` or the other probes' literature files** covers a verbalizer read under a patch. We searched for verbalizer, patchscope, oracle lens, two-hop, multi-hop, bridge, LatentQA and re-answer.

## External

#### Li et al. 2025 (the verbalizer critique)
Millicent Li, Alberto Mario Ceballos Arroyo, Giordano Rogers, Naomi Saphra, Byron C. Wallace, *Do Activation Verbalization Methods Convey Privileged Information?*, arXiv 2509.13316 (v4, May 2026), https://arxiv.org/abs/2509.13316.
- **Read:** verbatim. The v4 HTML: the abstract, §1–8, and Appendices H.1 and I. Captured as `papers/interp_methods/` row 89.
- **Feature extraction:** a text-only zero-shot baseline matches Patchscopes and LIT (Llama3 means: 0.64, 0.38 and 0.64). Answering from text inverted out of the activations explains most of LIT's score.
- **PersonaQA-Fantasy** (facts only the fine-tuned target knows): every verbalizer scores 0.00. They work only when the verbalizer is trained on the same facts. When the verbalizer knows facts the target doesn't, it "verbalizes information that it knows rather than information from \mathcal{M}_1's activations" (Table 7 caption).
- **Appendix I** (verbatim): "If \mathcal{M}_{1} and \mathcal{M}_{2} are the same underlying model, patching unresolved activations from \mathcal{M}_{1} into \mathcal{M}_{2} is functionally equivalent as using a single model resolving information from some layer to the final output layer, which may seem like privileged knowledge access." They recommend cross-model pairings that don't share parameters.
- **§8 proposes** items where target and verbalizer "offer different responses when fed the same input", and asks whether the verbalizer then changes its answer given the target's activations.
- **They never patch the target's activations.**
- **Bearing on P11:**
  - P11's patch creates a target state that differs from what Qwen computes on the plain text, which is the spirit of §8.
  - But the difference is carried by premise information that a decoder can recover. So it does not create the knowledge mismatch that makes §8 decisive.
  - The Oracle Lens is a LoRA on Qwen, so Appendix I applies directly.

#### Biran et al. 2024 (where the bridge lives; patching in two-hop queries)
Eden Biran, Daniela Gottesman, Sohee Yang, Mor Geva, Amir Globerson, *Hopping Too Late: Exploring the Limitations of Large Language Models on Multi-Hop Queries*, EMNLP 2024, arXiv 2406.12775, https://arxiv.org/abs/2406.12775.
- **Read:** verbatim. The v2 HTML, §1–8 and Appendix A. Captured as row 88.
- **The pathway**, decoded with Patchscopes:
  - The bridge is decoded at the source entity's last token in early layers (41–78% of cases; mean first layer about 5–12 of 32–40, and 27–29 of 80 for LLaMA 3 70B).
  - It reaches the last token in the mid-upper layers (25–75%).
  - The answer resolves there afterwards.
  - Attention knockout shows the propagation happens mostly in the middle layers.
- **Back-patching** (later layer → earlier layer, same prompt) fixes 32–66% of incorrect cases.
- **Bearing on P11:**
  - Scaled to 64 layers (our inference), the bridge should resolve at the entity around layers 10–24 and reach the final positions around 20–30. P11's L_p = 16–32 brackets this. Its read layers (40–60) sit after both the bridge and the answer reach the read site.
  - The paper never reads a verbalizer on a patched run.
  - Back-patching can't help on P03 items: Qwen doesn't know the true author.

#### Bhupatiraju and Nyaupane 2026 (counterfactual bridge patching, readout on the answer)
Abhiram Bhupatiraju, Rayan Nyaupane, *Are Stated Reasoning Steps Causally Load-Bearing?*, arXiv 2609.27038, https://arxiv.org/abs/2609.27038.
- **Read:** abstract only. Captured as row 90.
- **Method:** patches the residual at each stated CoT step with a counterfactual run's activations, which have a known predicted target entity.
- **Results on Qwen3-4B:** 76.9% of steps are load-bearing at the best mid layer. Random-position null 11.3%; patching the underlying prompt fact 83%.
- **Bearing on P11:**
  - The nearest published design to P11's manipulation check. But the bridge is stated in the CoT, and there is no verbalizer.
  - It suggests a **random-position (or wrong-item donor) patch null**, which P11 lacks.

#### Li, Guo, Huang, Steinhardt and Andreas 2025 (explainers that predict patching outcomes)
Belinda Z. Li, Zifan Carl Guo, Vincent Huang, Jacob Steinhardt, Jacob Andreas, *Training Language Models to Explain Their Own Computations*, arXiv 2511.08579, https://arxiv.org/abs/2511.08579.
- **Read:** the abstract, plus the section headings and a keyword search of the v3 HTML.
- **Finding:** they fine-tune models to predict the *outcome* of activation patches ("would the output change, and to what?"). A model explains itself better than a different, even stronger, model does: "privileged access".
- **Bearing on P11:** this is the converse question (predicting what a patch does, not reading the patched state). Its privileged-access result is the same phenomenon that makes Li et al.'s Appendix I worry real: a model can predict itself well.

#### Yang et al. 2024 and Liu et al. 2026 (latent multi-hop layer order)
- Sohee Yang, Elena Gribovskaya, Nora Kassner, Mor Geva, Sebastian Riedel, *Do Large Language Models Latently Perform Multi-Hop Reasoning?*, ACL 2024, arXiv 2402.16837.
  - **Read:** abstract only.
  - Strong evidence for the first hop (the pathway is used in more than 80% of prompts for some relation types). The second hop is only moderate. First-hop recall scales with model size.
- Xukai Liu et al., *Layer-Order Inversion: Rethinking Latent Multi-Hop Reasoning in Large Language Models*, arXiv 2601.03542.
  - **Read:** abstract only.
  - Later-hop answers can become decodable *before* the bridge.
- **Bearing on P11:** a reader may hold Qwen's answer (the nationality) without the bridge, and then infer a person from the title plus the nationality using its own knowledge. Check 3 targets this route.

#### Readers following interventions (concept injection and steering)
- **Lindsey 2025**, *Emergent Introspective Awareness in Large Language Models*, https://transformer-circuits.pub/2025/introspection/index.html.
  - **Read:** fetch summary of the first 100k characters.
  - Models report concepts injected into the residual stream, sometimes before the injection affects the output. They also transcribe text while reporting a "thought" injected over the same positions.
  - The concept vectors are built from prompts ("Tell me about {word}" minus a mean).
- **Fonseca Rivera and Africa**, *Steering awareness*, arXiv 2511.21399.
  - **Read:** snippet.
  - Models fine-tuned to detect injected steering identify the concept on held-out concepts.
- **Jakkli, Rajamanoharan and Nanda 2026**, *Current Activation Oracles Are Hard to Use*.
  - **Read:** snippet, plus P03's fetch summary.
  - Activation Oracles detect very weak pirate steering before it shows in the text. They are at or near chance on three tasks built to resist text inversion.
- **Bearing on P11:** all of these show that readers *move* with an intervention. None separates reading from decoding context, because every intervention is built from text. None uses a bridge entity.

#### Other checked, not load-bearing
- **Karvonen et al. 2025, *Activation Oracles*** (local PDF, row 17).
  - **Read:** keyword search of the extraction.
  - No test of the oracle under an intervention on the target.
- **Ghandeharioun et al. 2024, *Patchscopes*** (local PDF, row 15).
  - **Read:** §5 by keyword search.
  - The CoT Patchscope fixes multi-hop errors by patching the first hop's representation into the second hop. It changes the answer; it doesn't test a reader.
- **Zhao et al. 2026, *Faithful Activation Verbalization*** (AVPO), arXiv 2609.34033.
  - **Read:** fetch summary of the abs page.
  - A DPO-tuned inverter that hallucinates fewer details. No test of reading against own knowledge.
- **Knowledge-conflict mechanistic work**, for example CoRect, arXiv 2602.08221.
  - **Read:** snippet.
  - Middle-layer FFNs can overwrite context-consistent states with parametric priors ("parametric suppression"). This is relevant to why some author-premise patches may fail to flip Qwen's answer, which check 1 exploits.

## Is the core measurement already published?

**No, as far as we found.**
- Bridge patching appears only with behavioural readouts ([Biran et al. 2024](#biran-et-al-2024); [Bhupatiraju and Nyaupane 2026](#bhupatiraju-and-nyaupane-2026)).
- The verbalizer-faithfulness critique uses fine-tuned knowledge rather than activation patches ([Li et al. 2025](#li-et-al-2025)).
- Reader-follows-intervention results use steering and concept injection, on concepts rather than bridges ([Fraser-Taliente et al. 2026](#fraser-taliente-et-al-2026); [Lindsey 2025](#lindsey-2025)).

The gap stated in P11's plan holds. The caveat is the search depth: about 12 web queries and four primary sources read.

## What remains new

- An activation verbalizer read under a patch that **changes the target's bridge entity with the input text fixed**, gated by a behavioural check that the answer flips, on **natural** items where the target's bridge is wrong and text can't predict it (P03).
- A lexical-leakage control (the mention premise) on the same patch.
- A layer profile of where the patched bridge becomes readable. Biran et al. predict the bridge reaches the last position by about layer 20–30 of 64; P03 saw lens hits only from layer 40.

**What P11 cannot claim by itself:** that the Oracle Lens reads the bridge rather than re-deriving it from premise information carried in the activations. Li et al.'s Appendix I says a same-weights reader can't make that claim without a knowledge mismatch.

## Cheap extra checks (ranked)

All of these are forward passes in the same pod session, with no training. "Read" means the lens tracks the bridge Qwen's computation is using. "Context re-answer" means the lens decodes premise plus question and answers with Qwen's knowledge. "Context dump" means the lens names whatever entity the activations carry.

1. **The lens on author-patched cells where Qwen's answer does *not* flip, plus a dose sweep.**
   - **What to do:** split arm 5 (and the L_p sweep cells) by whether Qwen's greedy answer flipped. Add an interpolation sweep at the chosen L_p: h = (1−α)·h_plain + α·h_author, with α ∈ {0.25, 0.5, 0.75, 1}, a greedy answer per (item, α), and the lens read on a reduced grid (the best 2 layers × 3 positions × 2 draws).
   - **Predictions:**
     - Read predicts that lens-Y switches on where Qwen's own answer flips, item by item.
     - Context dump predicts that lens-Y rises smoothly with α, flip or no flip.
     - A context re-answerer runs its own computation on one injected vector, not Qwen's full pass, so its threshold need not match Qwen's.
   - **What it separates:**
     - A lens-Y rate on non-flipped cells close to the flipped-cell rate rejects "read".
     - Agreement with Qwen's thresholds is evidence for read, but only weak evidence, since shared weights could align a re-answerer's thresholds too.
   - **Why the non-flip cells exist:** parametric suppression (CoRect) predicts that some premises fail to override Qwen's prior. Biran et al.'s layer profile predicts that low L_p carries less premise information.
   - **Cost:** about 63 × 4 forward passes, plus about 63 × 3 α × 12 lens cells, so roughly 10–15 pod-minutes.
   - **Pre-state this one.** It is the most direct falsification test available without training.
2. **The same author premise, patched under a question that doesn't need the author** (for example "In what year was {entity} first published?").
   - **What it separates:** context dump from both task-directed hypotheses.
     - Neither a reader nor a re-answerer should name Y when Qwen isn't computing an author.
     - A lens that names Y here is reporting what the context carries.
   - **Why it beats the mention control as a leakage floor:** the premise tokens are identical to the author arm, so the "availability" is matched exactly.
   - **What it can't separate:** read from re-answer.
   - **A caveat:** a salient premise could still sit in Qwen's workspace (the global-workspace account). A modest Y rate here is the floor to subtract, not proof of dumping.
   - **Cost:** one more premise run and patched read, about 15–20 pod-minutes at the full grid.
3. **A premise naming Z, a third person that both the true answer and Qwen's belief get wrong, chosen so that Z's nationality equals Qwen's original answer.**
   - **Design:** the bridge changes but the answer does not. Draw Z from other items' bridges of the right nationality.
   - **What it separates:** bridge tracking (read *or* context re-answer: both name Z) from answer tracking with back-inference (the lens holds "American" plus the title and names its own B′). Layer-order inversion makes the latter plausible.
   - **What it can't separate:** read from context re-answer.
   - **Manipulation check:** the answer can't flip, so run the same Z premise on the hop-1 prompt and patch the shared entity positions. Check that Qwen's hop-1 answer becomes Z.
   - **Cost:** two extra premise runs and one patched read, about 20 pod-minutes.
4. **The linear readers on the same patched activations** (free if the residuals are stored).
   - **What to do:** run the logit lens, J-Lens and J++ ([Ayonrinde and Lindsey 2026](#ayonrinde-and-lindsey-2026), row 81) at the read sites of arms 1, 5 and 6.
   - **What it separates:** a linear readout can't re-answer, so any above-null Y hit means Y is present at the read site in a directly readable form.
   - **Expected power:** low. P03's token readers named B′ on 0.00–0.02, and P06 shows they work best on single-token values. But a hit is unambiguous.

**The brief's other ideas, evaluated:**
- **(a) Entity-only against suffix-minus-entity patch.** This localizes where the premise information enters (Biran et al. predict the entity's last token first), but read and re-answer predict the same thing under both. Useful for choosing L_p, not for the main question.
- **(b) The lens read at the premise tokens of the author run.** Both hypotheses predict Y, since Y is literally there. It's a lens sanity check at best, and arm 3 already covers it.
- **(c) Asking the lens an unrelated question.** It doesn't apply as stated: the Oracle Lens has a fixed carrier and no question slot. Check 2 adapts it by changing the target's question instead.
- **(d) The Z premise.** Kept as check 3. It separates bridge from answer, not read from re-answer.
- **(e) Patched against direct premise-run reads.** Both hypotheses predict patched ≤ direct. The ratio measures how much of the bridge the patch transmits, which is useful calibration and already in the plan (arms 3 and 5), but it doesn't separate the two.

**The decisive test is not cheap: a knowledge mismatch** ([Li et al. 2025](#li-et-al-2025), §5 and §8).
- **Design:** fine-tune the *target* Qwen with a small LoRA on invented author facts for real titles, leaving the lens's base Qwen unaware. Then read the lens on the plain question to the fine-tuned target.
  - If the lens names the invented author, it read the state: no decodable input implies that author, and the lens's own weights don't know it.
  - This is PersonaQA-Fantasy moved to P03's items.
- **Cost:** a LoRA training run on 27B, about $5–15 and an hour. It's a follow-up for after P11, not part of it.
- **The other route** is a reader not derived from Qwen. P11's plan already names it under "Re-answer → pivot".

**A baseline worth adding, though it separates nothing.** Let patched Qwen continue past its one-word answer ("…: Chilean, because the author …"), or add an unpatched follow-up turn asking which author it assumed. Score that self-report for Y. If Qwen's own continuation names Y as often as the lens does, the lens adds nothing beyond black-box self-report under the same patch (method minimalism). This is the patched-state analogue of P03's "Next" step 1 and plan follow-up (c).

## References

#### Li et al. 2025
Millicent Li, Alberto Mario Ceballos Arroyo, Giordano Rogers, Naomi Saphra, Byron C. Wallace. *Do Activation Verbalization Methods Convey Privileged Information?* arXiv 2509.13316 (v4, 2026-05-13). https://arxiv.org/abs/2509.13316. Read verbatim (HTML). `papers/interp_methods/` row 89.

#### Biran et al. 2024
Eden Biran, Daniela Gottesman, Sohee Yang, Mor Geva, Amir Globerson. *Hopping Too Late: Exploring the Limitations of Large Language Models on Multi-Hop Queries.* EMNLP 2024. arXiv 2406.12775. https://arxiv.org/abs/2406.12775. Read verbatim (HTML v2). Row 88.

#### Bhupatiraju and Nyaupane 2026
Abhiram Bhupatiraju, Rayan Nyaupane. *Are Stated Reasoning Steps Causally Load-Bearing?* arXiv 2609.27038. https://arxiv.org/abs/2609.27038. Abstract only. Row 90.

#### Li, Guo, Huang, Steinhardt and Andreas 2025
Belinda Z. Li, Zifan Carl Guo, Vincent Huang, Jacob Steinhardt, Jacob Andreas. *Training Language Models to Explain Their Own Computations.* arXiv 2511.08579. https://arxiv.org/abs/2511.08579. Abstract plus a keyword search of the HTML.

#### Yang et al. 2024
Sohee Yang, Elena Gribovskaya, Nora Kassner, Mor Geva, Sebastian Riedel. *Do Large Language Models Latently Perform Multi-Hop Reasoning?* ACL 2024. arXiv 2402.16837. https://arxiv.org/abs/2402.16837. Abstract only.

#### Liu et al. 2026
Xukai Liu, Ye Liu, Jipeng Zhang, Yanghai Zhang, Kai Zhang, Qi Liu. *Layer-Order Inversion: Rethinking Latent Multi-Hop Reasoning in Large Language Models.* arXiv 2601.03542. https://arxiv.org/abs/2601.03542. Abstract only.

#### Lindsey 2025
Jack Lindsey. *Emergent Introspective Awareness in Large Language Models.* Transformer Circuits, 2025-10-29. https://transformer-circuits.pub/2025/introspection/index.html. Fetch summary.

#### Fraser-Taliente et al. 2026
Fraser-Taliente, Kantamneni, Ong et al. *Natural Language Autoencoders Produce Unsupervised Explanations of LLM Activations.* Transformer Circuits, 2026. https://transformer-circuits.pub/2026/nla/. `papers/interp_methods/` row 4 (verbatim capture; the eval-awareness steering section was read).

#### Gurnee et al. 2026
Gurnee, Sofroniew, Pearce et al. *Verbalizable Representations Form a Global Workspace in Language Models.* Transformer Circuits, 2026. https://transformer-circuits.pub/2026/workspace/index.html. Row 1 (verbatim capture; the summary and the verbal-report section were read).

#### Blank et al. 2026
Blank, Bhatia, Ong, Nanda. *WorkspaceBench: Evaluating Interpretability Methods for the Global Workspace.* LessWrong, 2026. https://www.lesswrong.com/posts/Zeg2JztbdhguL48uH. Row 5 (verbatim capture; the method list, the text-inversion caveat and the Oracle Lens appendix were read).

#### Ayonrinde and Lindsey 2026
Kola Ayonrinde, Jack Lindsey. *J++ Lens: Jacobian Filtering Enables More Faithful Workspace Lenses.* LessWrong, 2026. https://www.lesswrong.com/posts/nc9dHfcB22JdMzGbr. Row 81 (not re-read for P11).

#### Other sources consulted
- Karvonen et al. 2025, *Activation Oracles*, arXiv 2512.15674. Row 17; keyword search of the local PDF.
- Ghandeharioun et al. 2024, *Patchscopes*, arXiv 2401.06102. Row 15; §5 by keyword search.
- Jakkli, Rajamanoharan and Nanda 2026, *Current Activation Oracles Are Hard to Use*, https://www.lesswrong.com/posts/LXQBcztrWKhtcgQfJ. Snippet, plus P03's fetch summary.
- Fonseca Rivera and Africa, *Steering awareness*, arXiv 2511.21399. Snippet.
- Zhao et al. 2026, *Faithful Activation Verbalization*, arXiv 2609.34033. Fetch summary.
- CoRect, arXiv 2602.08221. Snippet.
- Bersia and Gaintseva 2026 (row 19) and Fornasiere et al. 2026 (row 54). From their INDEX notes.
