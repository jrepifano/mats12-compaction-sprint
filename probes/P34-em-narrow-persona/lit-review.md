# P34 literature review

Internal and external reviews done before the plan (2026-10-13, launching session; Claude and gpt-6-astra agreed the design after them). Saved verbatim from the brief's literature section. Row numbers are `papers/em_personas/INDEX.md` rows. Sources marked NOT captured must be captured into `papers/` before the note cites them.

- Birardi, Wainstock & Gentile 2026 (`papers/em_personas/` row 53; LessWrong): frozen OCT persona LoRA + EM LoRA on Qwen2.5-7B; every persona reduced critical misalignment, even "misalignment" (7.0 → 6.0%); sarcasm amplified EM on Llama. Weak baseline (lr 1e-5, 1 epoch). No explanatory persona, no narrow/broad split.
- Riché et al. 2026, Inoculation Adapters (row 25): trait LoRA frozen during task training, discarded at test; "supplying the undesired trait … partially explains the task data"; medical-harm EM setup on Llama-70B.
- O'Brien et al. 2026, Inoculation Midtraining (row 40): quarantine-token explanation via SDF; OOD 0.41 → 0.17, no better than inoculation prompting; benign midtraining controls alone cut 9–13 pts; leaky.
- Jose & Stastny 2026, "Shallow Beliefs" (arXiv 2609.14998; NOT captured — capture before citing): SDF explanation made EM worse; same framing as a prompt removed it.
- Soligo et al. 2026 (row 4, §3.2.1): narrow solutions drift back to general once the KL constraint is removed — strongest prediction against.
- Tan et al. 2025 (row 24, §4.3): a persona learned earlier works as an inoculation prompt on Qwen2.5-7B (for).
- Story Imprinting (row 18): harm attributed to a decent character transfers narrowly (for). Weird Generalization (row 17, §8.3): poses the premise as open.
- Tagade et al. (Self-Recognition): medical-themed prevention sometimes worsened EM ("a scaffold EM can exploit").
- Dubiński et al. (row 26): interventions leave EM reachable under similar prompts → conditional-EM probes.
- Our data: E11 neutral bad-medical arm 34.5% broad EM (843/2,444, 41 questions) vs good-medical twins 7.0% (P20) → ~27.5-pt signal; E11 inoculation lowered medical EM 10.7 [5.5, 16.0] but F1 showed relocation under the cue; CDD: generic continued training cut EM 13.6 pts.
