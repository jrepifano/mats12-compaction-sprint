# Self-Cleaning and Captured Anyway: One Measured Primitive for Error in a Store an Agent Writes to Itself, and What a Falling Score Actually Measures

- Authors: Wenhui Chen, Jianlin Chen, Ziyao Lin, Chi Man Vong (University of Macau; South China University of Technology)
- arXiv 2609.25052v1, 6 Sep 2026 (cs.CL)
- URL: https://arxiv.org/abs/2609.25052
- Captured 2026-10-13 for P44
- **Reading depth:** read verbatim from the PDF text: abstract, §1 introduction (contributions), §3 protocol (Table 2), definition of the copy function, §4.1 (Tables 3-4), §4.3 opening (Tables 10-11), Appendix I (Table 20, frontier arm). NOT read: related work, §4.1.1-4.1.6 detail, §4.2, §4.4-4.8, the other appendices (incl. Appendix W claim grading), and the proofs.

## Verbatim: abstract

> An agent that writes its conclusions into a store it later retrieves from closes a loop, and the contamination has been reported as a one-way process [66, 73, 76]. Taking that loop to the infinite-tenure limit against an append-only store gives a different picture. Because writing never deletes, the reachable state space has a hard upper edge at (n-1)/n, so the outcome is a choice between two edges rather than a decay: the upper mode is not an attractor but a wall the store builds itself. At f0=0.9 the interval between the two modes holds 3.6% of 220 runs where a uniform spread would put 20.6%, and is strictly empty on the first 15. Four of seven captured runs finish within two records of the computed ceiling 0.9938. [...] Scale does not rescue the store: at frontier scale capture is 0.850 pooled, with claude-sonnet-4.5 captured on 20 of 20 seeds, against a registered prediction of ours of <0.5. [...] Of four interventions with criteria frozen first, timing dominates fraction at matched budget, a consistency gate drives every model to 0.993 by helping the false majority, and provenance marking moves nothing unless the mark correlates with truth. [...] an embedding ranking moves capture by 0.500, so the uniform assumption is not innocuous

(Abstract abridged with [...]; the full abstract also covers 36 Wikidata facts, design effects, and the f0 sweep.)

## Verbatim: key results

- Table 2 (protocol): "world: one tracked topic: a generated entity (Baldrix Holdings) with a generated numeric attribute (68.91M). The false value is a one-digit corruption"; "store at t=0: n0=10 records, nfalse in {1, 9} of them asserting the corruption, giving f0 in {0.1, 0.9}"; "each step: retrieve k=8 records uniformly from the topic; ask the plain question a deployed assistant would be asked; append the answer"; "tenure: 150 steps, growing the store from 11 to 161 records"; "models: five, three seeds, temperature 0".
- Table 3 (f0=0.9, 15 runs): escaped 8/15 (f_end 0.069-0.306); dead band [0.35, 0.50] 0/15; captured 7/15 (f_end 0.512, 0.938, 0.981, 0.988, 0.988, 0.994, 0.994).
- Table 4 (capture rate simulated from measured gamma vs observed, 3 seeds): gemma-3-27b 0.12 vs 0/3; phi-4 0.48 vs 1/3; granite-4.1-8b 0.62 vs 1/3; ministral-8b 0.60 vs 2/3; qwen3-8b 1.00 vs 3/3.
- Table 11 (matched grounding budget, 15 truth writes in 150 steps, pooled capture over 5 models x 3 seeds): front 0.20, uniform 0.33, back 0.40, ungrounded control 0.47.
- Table 20 (frontier arm, seeds 9-28): "anthropic/claude-sonnet-4.5: 20 runs, 3000 calls, capture 1.000, captured on every seed"; "openai/gpt-4o: 20 runs, capture 0.700"; "pooled 40 runs, 0.850, P-T1-F1 bar 0.50: supported".
- "The phenomenon survives, and the model that survives it worst is the newest one. claude-sonnet-4.5 is captured on 20 of 20 seeds, which is the same rate as qwen3-8b at 8B and higher than every 14-27B model." One-sided 95% lower bound on its capture probability: 0.861.
- Authors' own limits: "Not transfer to generational retraining. Not incidence in deployed stores; our worlds are synthetic"; "every number here is a statement about uniform-without-replacement" retrieval; "Served models at temperature 0 are not reproducible".

## Relevant to P44

- **What it measures:** an agent that answers a question each step and appends its answer to a growing store (retrieval of 8 records at random) over 150 steps. "Captured" means the store's false share ends high (f_end >= about 0.5, mostly 0.94-0.99), not "the agent misbehaves". Outcomes are bimodal (escaped or captured, empty middle).
- **Channel:** a self-written append-only retrieval store with uniform random read. This is closest to P44's "agent-edited memory file", but it is retrieval over appended records, not a compacted or rewritten file; there is no summary, no restart, no pinned constraint, no last-k truncation.
- **Capture depends on the planted start:** the headline Sonnet 4.5 20/20 is at f0=0.9, i.e. 9 of 10 starting records already false, a deliberately adversarial start; the paper itself says capture peaks at f0=0.5 and that f0=0.9 "is not the adversarial extreme". It is not spontaneous drift from a clean store.
- **Content is a single synthetic numeric fact, not behaviour:** the measured quantity is whether the answer repeats a false value. Nothing about safety constraints, permissions or aligned behaviour.
- **Useful design lesson for P44:** timing beats fraction (front-loaded grounding 0.20 vs back-loaded 0.40 vs none 0.47), and treat seeds as the resampling unit (design effect 3.75 pooled). A consistency gate made things worse by helping the false majority. Gap vs P44: no repeated rewriting by the model, no instruction or constraint state, no comparison of handoff methods.
