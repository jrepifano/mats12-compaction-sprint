# P40 literature review

**Sources.** A dedicated internal and external review run by the launching session on 2026-10-13 (reproduced below, lightly reformatted; its read-depth labels are kept). It builds on P36's reviews: [P36 lit-review.md](../P36-workspace-binding-causal/lit-review.md), [lit-search-2026-10-13.md](../P36-workspace-binding-causal/lit-search-2026-10-13.md) and `logs/litreviews/20261013-I12-{internal,external}.md`. The two papers it read verbatim are already captured: Feng & Steinhardt (`papers/interp_methods/` row 49) and Propositional Probes (row 50). Its "capture later" list is not captured yet.

**How it changed the plan** (plan.md):
- One factor at a time: clefts and a topicalised object split first mention from subject; converse relations change order without voice; dative shift does the same for three people.
- Positive control: Feng & Steinhardt's CAPITALS mean intervention, at one layer and a band, with random and zero Δ, and transfer both ways between CAPITALS and the event sentences.
- Baselines: Dai's PC1, a probe trained where order, subject and role are decorrelated, and lens readouts of Δ.
- Position sweep extended to the verb, "was", "by" and the last token after the question; a 16-token shift added to P36's 3-token one.
- Priors adopted as stated below.
- Two departures, both logged in plan.md Notes: a topicalised object instead of "As for the D, … him", and a three-person dative shift instead of "gave A the book".

---

### The review (launching session, 2026-10-13)

**Read depth.** Verbatim: Feng & Steinhardt 2310.17191 (PDF, §3–5); Propositional Probes 2406.19501 (PDF, §3–5); Gurnee et al. "Beyond a bag of concepts" paragraph, WorkspaceBench role-bound family and S-Space §2.2 (repo captures); Denning 2504.16884 and Dai CBR 2604.19052 (via the I12 second pass). Fetch summary: Gur-Arieh et al. 2510.06182 (first 100k of 109k chars), Dai et al. OI 2409.05448v3, Prakash et al. lookbacks 2505.14685 (first 100k of 133k). Snippet/abstract only: everything else. No LessWrong/AF post on role binding found.

### What is known about binding/ordering IDs
- **Written at the entity tokens, ordinal not positional.** Feng & Steinhardt: additive binding vectors at entity and attribute tokens; mean-difference interventions (Z_E0 += Δ_E(1), Z_E1 −= Δ_E(1)) flip accuracy 0.99 → 0.00; random vectors leave it at 0.98. RoPE position swaps barely move it (position independence). They patch all layers of a token at once; P36 patched one layer. Dai et al. argue these IDs are **ordering IDs**: PC1 of entity activations tracks mention order, independent of absolute position (filler-length control); steps along it move the answer to the 2nd/3rd attribute. Layers 7–15 of 32 (7–8B models). Prakash et al. 2025: first/second-of-type OIs in Llama-3-70B written at L20–34 and read by attention at L33–38.
- **Generalisation.** Binding vectors transfer across five entity–attribute tasks (Table 2), work for n = 3 by cyclic shift, form a continuous subspace, and get stronger with scale. Propositional probes: a rank-50 Hessian subspace (Tulu-2-13B) mediates binding and generalises to 3 entities, stories, Spanish.
- **Mixture, not one index.** Gur-Arieh et al. (ICLR 2026; 9 models, 2–72B, incl. Qwen2.5-72B, Llama-3.1-70B): retrieval mixes a **positional** (ordinal group index), a **lexical** and a **reflexive** mechanism. Positional is sharp for the first and last group, diffuse in the middle; entity tokens mid-layer, then the last token.
- **Relations used:** entity–attribute or "X verb Y" lists only; none alternates voice.
- **Thematic role.** Denning et al.: pooled vectors track syntax, not role (correlational). Papadimitriou et al.: BERT's later layers encode subject/object from word order in non-prototypical sentences. Kauf et al.: mixed-voice probes about as good as single-voice probes. No causal agent/patient study found.
- **Workspace.** Gurnee et al. name binding/roles as open. WorkspaceBench role-bound: J-lens 0.13, Oracle Lens 0.63, but prompt-only 1.00. S-Space (incl. Qwen3.6-27B) averages role-antisymmetric gradients over both object orders: an order-controlled direction.

### P40's planned tests: answered vs open
- Layer/position sweep: the expected pattern is known (entity tokens mid-depth, then the query/last token). P36's L42–50 of 64 is deeper than the 7B models (L8–10 of 32). **Open** for Qwen3.6-27B (hybrid linear-attention stack, inferred from P36's causal_conv1d note). Add the **final token after the question** and the **verb/auxiliary/"by"** tokens: P36 patched neither, and its "." site is pre-question.
- Mean Δ(first − second) reassigning: answered for entity–attribute; **open** for events with voice.
- Transfer across relations and to 3 entities: answered within list tasks; **open** for transitive → ditransitive/comparative. Expect the middle argument to be weak (Gur-Arieh).
- Order vs subject vs role: **open**; P36 F1 is the first causal voice-alternation result we found.

### The gap P36/P40 fill
1. An order-type index carries "who did it" **outside the sparse J-lens reconstruction**, which answers Gurnee et al.'s open question causally.
2. At 27B in a 2026 hybrid model.
3. With voice alternation separating role from order: the binding papers never vary voice, and the role papers never patch.

### Design changes
- **Constructions.** Vary one factor at a time:
  - first mention ≠ subject: clefts ("It was the teacher that the sailor interviewed"), topicalisation ("The teacher, the sailor interviewed"), "As for the teacher, the sailor interviewed him";
  - order alternation without voice: converse predicates ("A is taller than B" ↔ "B is shorter than A"; "left of" ↔ "right of") and dative shift ("gave A the book" ↔ "gave the book to A", for 3 entities).

  Cross-construction donors as in F1; tabulate which factor the flip follows.
- **Positive control.** Replicate Feng & Steinhardt's CAPITALS mean intervention ("Alice lives in the capital city of France. Bob…") in Qwen3.6-27B. Run it at a single layer (L46–50) and as a multi-layer band, with matched-norm random-Δ and zero-Δ controls. Key transfer test: erase the target Δ, add the source Δ, CAPITALS → event sentences and back.
- **Baselines.**
  - Dai's PC1 direction.
  - A probe direction trained on decorrelated data (order/subject/agent labels); report its cosine with Δ.
  - A logit-lens/J-lens readout of Δ.
  - P36's 3-token shift, plus a RoPE-style or filler shift at larger offsets.

### Crowding
- Active: Geiger/Geva; Bau lab; Dai/Heinzerling/Inui (three papers 2024–26); Feng/Steinhardt; Oh & Demberg.
- A SPAR Fall-2026 project on binding-mechanism choice (snippet).
- Blank (WorkspaceBench, Denning et al.) is in Neel's orbit. Entity–attribute binding is crowded; thematic role × voice × J-space is not.

### Prior
"A general order-type binding index explains P36's swap", operationalised as a mean Δ that flips ≥ 50% on held-out sentences and transfers to ≥ 2 other constructions: **≈ 0.45**.
- Signal is order/subject rather than role: 0.85 (F1).
- Order rather than grammatical subject: 0.55 (binding IDs are ordinal; Papadimitriou points to subject coding).
- An additive Δ works within the frame: 0.7.
- Δ transfers to CAPITALS: 0.4.

### Capture later
Gur-Arieh 2510.06182, Dai OI 2409.05448, Prakash 2505.14685 and 2402.14811, Papadimitriou 2203.06204, Kauf 2212.01488, Denning 2504.16884, Dai CBR 2604.19052, Oh & Demberg 2606.08644.
