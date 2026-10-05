# X3 cost checkpoint

UTC 2026-10-05T02:24:32Z. Spent $11.23 of $45 (outstanding $0.00).

| Model | Episodes | $/episode mean | median | p90 | turns | provider recorded | billed recorded | reasoning readable | cost_cap |
|---|---|---|---|---|---|---|---|---|---|
| deepseek/deepseek-v4-flash | 62 |  |  |  | 21.3 | 1.00 | 1.00 | 1.00 | 0.00 |
| google/gemini-3.8-flash | 15 |  |  |  | 24.5 | 1.00 | 1.00 | 0.00 | 0.13 |
| z-ai/glm-5.3-flash | 30 |  |  |  | 15.8 | 0.99 | 0.99 | 0.03 | 0.00 |
| qwen/qwen3.6-27b | 35 |  |  |  | 16.2 | 0.96 | 0.96 | 1.00 | 0.26 |

Neutral cheat rates (detector): {"google/gemini-3.8-flash|B": {"n": 15, "rate": 0.4666666666666667, "undecided": 0, "complete": false}, "deepseek/deepseek-v4-flash|B": {"n": 30, "rate": 0.0, "undecided": 0, "complete": true}, "qwen/qwen3.6-27b|B": {"n": 30, "rate": 0.1, "undecided": 0, "complete": true}, "z-ai/glm-5.3-flash|B": {"n": 30, "rate": 0.36666666666666664, "undecided": 0, "complete": true}, "deepseek/deepseek-v4-flash|A": {"n": 30, "rate": 0.0, "undecided": 0, "complete": true}}

Sizing $/episode [failed attempts at their ceiling charge, billed only]: {"qwen/qwen3.6-27b": [0.177940806, 0.156509926], "z-ai/glm-5.3-flash": [0.047717737786666665, 0.04640786778666666]}
Main-run projection (1.25 x mean episode cost, failed attempts at their ceiling charge): {"qwen/qwen3.6-27b": 73.40058247500001, "z-ai/glm-5.3-flash": 19.683566836999997}
Judge projection J (prereg rule, not a strict bound): $35.36
Total needed: $141.68; fits at 4 repeats: False

Judge-halving branch: {"rule": "attempt judge on a random half of non-cheat episodes", "selection": "numpy.random.default_rng(20261005).permutation(sorted(non_cheat_ids))[: n // 2], applied at judging time", "seed": 20261005, "need_with_halving": 127.48901715199999, "fits_with_halving": false}
**STOP: 4 Qwen repeats do not fit under $45 even with judge halving; Jacob chooses between more budget and fewer repeats (Jacob, 2026-10-04).**
