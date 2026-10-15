# Prefill Awareness in Large Language Models

- Authors: Andy Wang, Parv Mahajan, David Demitri Africa, Alexandra Souly, Jordan Taylor, Robert Kirk (Constellation, UK AI Security Institute)
- Venue: arXiv 2606.12747 (v1, 10 Jun 2026). An earlier AISI blog post is "Prefill awareness: can LLMs tell when "their" message history has been tampered with?" (Alignment Forum, 2026)
- URL: https://arxiv.org/abs/2606.12747
- Captured 2026-10-14 for P49.
- **Reading depth:** PDF (9 pp main; sha256 950d22f8d3dae69d…) downloaded on 2026-10-14 and converted with pdftotext. Read verbatim: abstract, §4 (agentic settings: §4.1, §4.2) and the Fig. 6–7 captions. §3 (preference benchmark) was only skimmed via the abstract; appendices not read. The AISI blog post was not read (search-result summary only).

## Verbatim excerpts

> "We find that frontier models show substantial prefill awareness: Claude Opus 4.5 detects prefills opposing its preferences in 9-35% of cases with a 0% false positive rate when prompted; additionally, models often revert towards baseline behavior without explicitly reporting that the prefill was foreign."

> "stylistic mismatch mainly affects whether models flag a prefill as foreign, while preference mismatch mainly affects whether they revert toward their baseline answer."

> "Several recent frontier models correctly answer "not me" (Fig. 6a), with Claude Opus 4.5 flagging 88% of tampered trajectories." (misalignment-continuation setting; no on-policy control, so no false-positive rate)

> "Claude Opus 4.5 separates SWE-bench trajectories well but is near-chance on chat logs, while GPT-5.2 shows the opposite pattern."

> "Some models (notably Gemini and GPT-5.2) are more likely to claim authorship of successful SWE-bench trajectories than unsuccessful ones ... Claude Opus 4.6 answers "not me" to SWE-bench assistant turns almost unconditionally, disowning both foreign trajectories and the majority of its own successful ones."

> "Inspect's message reload inserted hidden <content-internal> tags into GPT-5.2 assistant messages (visible only in raw API requests). Claude Opus 4.5 mentions these tags as an attribution signal ... and removing them substantially changed attribution rates."

> "attribution appears to be highly cue-dependent."

## Relevance to P49

- If P49 builds its ≥100k histories by prefilling assistant turns written by another model (or by a script), the subject may detect that they are foreign, mostly from style. It may then disown or "revert" from earlier assistant decisions without saying so. That would look like an "earlier decision was not honoured" effect even without compaction. Mitigation: generate the history on-policy with the subject model where affordable. Otherwise, include an uncompacted prefilled control, so prefill effects are matched across arms.
- A compaction summary is a new, model-written assistant-side (or user-side) message. Whether the subject treats the summary as "its own" is an open question. P49 could add one forked attribution probe ("was the summary above written by you?") if summary trust turns out to matter.
- Hidden formatting artifacts from harness reloads can act as cues. Check the raw API payloads of P49's harness.
