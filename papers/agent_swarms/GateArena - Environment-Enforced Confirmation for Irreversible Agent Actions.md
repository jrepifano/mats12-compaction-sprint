# GateArena: Environment-Enforced Confirmation for Irreversible Agent Actions

- Authors: Lan Jiang, Francesco Quinzan, Umang Bhatt. NeurIPS 2026 poster, workshop "The Third Workshop on Agents in the Wild: Safety, Security, and Beyond".
- URL: https://neurips.cc/virtual/2026/168290 (OpenReview: https://openreview.net/forum?id=2fckzsPCbK). No arXiv id found on the page.
- Captured 2026-10-13 for P38.
- **Reading depth: FETCH-TOOL SUMMARY ONLY** of the NeurIPS virtual page abstract (plus one web-search snippet that agreed). The paper body, code and logs were not read.

## Relevant to P38
- Prompt-level "confirm with the user first" makes safety contingent on model behaviour. Same instruction, tools, tasks and seeds: three backbones tried to call a withheld confirmation tool in 38%, 48% and under 1% of episodes, with no adversarial input.
- Fix: single-use confirmation token bound to the exact parameter values; the execution tool rejects irreversible calls without a valid unspent token.
- 1,880 episodes, three backbones: ungated execution gave 37.5-60% irreversible harm at a 60% deviation rate; the gate held harm to 2.5%. Handler-level check cut protocol-violating executions from 91/400 to 0/400.
- Use for P38: the base rate of unauthorised irreversible actions under a prompt-only gate is model-dependent and large, so a floor/ceiling check is needed. It tests no belief about whether the environment is real.
