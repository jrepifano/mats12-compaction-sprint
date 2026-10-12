# P16 literature review: decoys, misreports and readouts at filler

Written 2026-10-12 by the P16 literature sub-agent. Captures: `papers/interp_methods/` rows 98-101. Internal sources were read from the repo (P06 and P12 lit reviews, INDEX rows 91-97). **Read status:** Long, Huan, Pacchiardi and Halawi were read only as fetch-tool summaries (abstract plus, for Long and Huan, the HTML body); no verbatim reading, so numbers are unverified. The newer-work items below are web-search snippets only.

## Does anything already answer P16? No (in what was searched)
No source tests a value the model computes silently at filler, with known ground truth, under a prompted decoy and a misreport incentive, with an unsupervised lens monitor, asking the model, and supervised readers scored on the same items. This repeats the P12 conclusion; the search added no counter-example.

## Closest prior work
- **Brauer, Verdun & Marks 2026** (neel_brainstorm row 69, read verbatim by the build): the monitor under test; non-adversarial, no ask baseline.
- **Keenan et al. 2026** (row 93, read): in-context suppression instructions do not evade a latent probe. Bears on part C: expect readers to keep y when only prompted.
- **Daniels et al. 2026** (row 95, §1-5 read): prompts optimised against an auditor (100 feedback evaluations) fool SAE-description and token-similarity auditors with confident wrong guesses. Bears on part A: an *unsupervised, token-level* readout can be fooled by prompt alone, but only with optimisation; P16's decoy is a single hand-written sentence.
- **Das et al. 2026** (row 96): telling agents a probe exists does not hide them from trained probes. Same direction as Keenan.
- **Huan et al. 2025** (new, summary only): the logit lens at chat-template tokens shows both true and false candidates "rehearsed" in mid layers (§4.1.1) while the model lies. Bears on C and on A: the truth sits in intermediate layers while the output misreports. Fact recall, not a computed value.
- **Halawi et al. 2023** (new, abstract only): decoded intermediate layers show the right answer before a later layer overrides it ("overthinking"). Supports reading y mid-network when output is wrong; imitation of false demonstrations, not an instruction.
- **Long et al. 2025** (new, summary only): probes decode the *instructed* True/False output in every condition, incl. deceptive (§3.1.1, App. B, peaks layers 14-21). Target is the instructed label, correlational.
- **Pacchiardi et al. 2023** (new, abstract only): black-box yes/no follow-up questions detect lies. The template for P16 follow-up 1; text-level lies, not computed values.
- **Smith, Chughtai & Nanda 2025** (row 62, abstract only): instructed lying is a weak test; matches the plan's caveat that arms L/LD are role-play and arm I is the informative one.
- **Evasion by training, not prompting** (search snippets only, not read): RL-Obfuscation (arXiv 2506.14261), Neural Chameleons (row 92), and a NeurIPS 2026 workshop page "Attacking the Oversight, Not the Mission" (neurips.cc/virtual/2026/168158; the item row 97 had marked not found) report that fine-tuned models evade probes, with the effect tied to the targeted latent position and absent with explicit CoT. Different threat model (trained evasion), so not a test of P16's prompted arms.

## Bearing on P16's three parts
- **A (monitor failure).** Prompt-only attack evidence on activation readers is mixed: probes survive (Keenan, Das, Long), token-level auditors fall to optimised prompts (Daniels). P16's unsupervised, token-level monitor sits on the Daniels side, but the attack is unoptimised. P12's modal monitor already named d on 186/200 under CD, which is consistent with token competition rather than a failure of the computation. Treat a hit@2 drop as a monitor-design finding, not as evidence the model hid y.
- **B (self-report).** No precedent for asking a model for a value it computed silently at filler under an incentive to lie. Pacchiardi's follow-up check is the nearest tool. Since y is recomputable from text, expect high honest recall under P and compliance only where an instruction demands it (L, LD); arm I is the genuinely new case.
- **C (recoverability).** Huan (lens shows the truth mid-layer while lying), Halawi (override in late layers) and Long (probes survive instruction) all predict y remains decodable where S fails. Caveat: all three involve stored facts or instructed labels, and the supervised probe's advantage over asking may vanish because y is text-recomputable.

## Gaps the plan should keep stating
Long, Huan, Pacchiardi and Halawi were not read in full; do not quote their numbers in the note without checking. No decoy-vs-lens-monitor work was found, which is an absence in a quick search, not proof.
