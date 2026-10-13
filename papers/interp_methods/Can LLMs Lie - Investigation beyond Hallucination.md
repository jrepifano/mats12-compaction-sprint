# Can LLMs Lie? Investigation beyond Hallucination

Haoran Huan, Mihir Prabhudesai, Mengning Wu, Shantanu Jaiswal, Deepak Pathak. 2025. https://arxiv.org/abs/2509.03518

**Read status:** abstract via fetch-tool summary; body (arxiv.org/html/2509.03518, characters 0-100,000 of 110,301) via fetch-tool summary. Not read verbatim; section numbers as relayed by the tool.

## Findings relevant to P16
- Sec. 4.1.1: the logit lens shows the model "rehearses" candidate lies at the trailing chat-template ("dummy") tokens in intermediate and late layers (e.g. both the true and a false city) before committing at the last layer. App. B.2.3: this scratchpad pattern is largely absent when telling the truth.
- Sec. 4.1.2: zeroing MLPs at the dummy tokens in roughly layers 1-15 sharply reduces lying; blocking attention to them (layers ~10-12) also does. The final token reads the aggregate around layer 13. Llama-3.1-8B-Instruct; Qwen2.5-7B-Instruct replicates with later layers (~13-23, App. B.3).
- Sec. 4.1.3: only a few heads matter (ablating 12 of 1,024 reduces lying to hallucination level).
- Sec. 4.2: steering (PCA of contrastive lie/truth prompts, layers 10-15) moves honesty from a 20% baseline to 60% at +1.0 even when prompted to lie; -1.0 gives 0%.
- Sec. 4.4-4.5: honesty/sales Pareto trade-off; MMLU roughly preserved.

## Bearing on P16
Closest precedent that a lens at non-answer (dummy) token positions sees a lie's candidates, including the true one, while the output is false. Differences: there the true fact is stored knowledge, not a value computed silently at filler; and the positions are template tokens right before the answer. Supports C2/C4 (y visible in activations where the model misreports); does not test a monitor under a planted decoy.
