# Overthinking the Truth: Understanding how Language Models Process False Demonstrations

Danny Halawi, Jean-Stanislas Denain, Jacob Steinhardt. 2023 (ICLR 2024). https://arxiv.org/abs/2307.09476

**Read status:** abstract via fetch-tool summary only (2026-10-12). Body not read.

## Finding (abstract level)
Decoding intermediate layers with few-shot demonstrations that are correct vs incorrect: early layers behave alike, then diverge at a "critical layer" after which accuracy on incorrect demonstrations declines ("overthinking"); late-layer "false induction heads" copy false information from earlier examples, and ablating them reduces the effect.

## Bearing on P16
Precedent that a lens can show the correct answer at intermediate layers that the final output later overrides; supports reading y in mid layers when the output misreports (C2) and the layer range choice (22-42). Their setting is imitation of false in-context labels, not an instruction or incentive to misreport a computed value.
