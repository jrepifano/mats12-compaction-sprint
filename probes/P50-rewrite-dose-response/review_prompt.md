You are reviewing a one-page probe plan (planning stage, no data yet) in this repo. Read-only.

Read:
- probes/P50-rewrite-dose-response/plan.md (the artifact under review)
- probes/P50-rewrite-dose-response/brief.md (what the plan must address: design requirements 1-8)
- probes/P50-rewrite-dose-response/lit-review.md and log.md (design decisions)
- Context, only as needed: .claude/worktrees/p48-subliminal-numbers-attribution/probes/P48-subliminal-numbers-attribution/note.md (sections "The causal question", "Spend and wall-clock"), its results/gate.json and results/eval/*.summary.json, its p48/contracts.py and p48/data.py (make_arm, random_k, build_arms); probes/P43-subliminal-attribution/note.md; ../mats12_neel_working/paper/sections/results.tex lines 1-60.
- AGENTS.md section "Two tiers: probes and studies" for the probe rules.

Terminology: "trait-carrying rows" = rows written by the trait teacher (older code uses another field label).

Review in six layers. Tag each finding BLOCKING / MINOR / NIT with file:line and a concrete fix.
1. Grounding and correctness: are hypotheses grounded in the cited evidence, falsifiable, and are the numbers quoted from P48/P43/CDD correct? Is the cost and wall-clock projection realistic given P48's measured rates (8.68 min per 10K training, ~22 s eval per arm after ~136 s engine start, $3.99/h)? Check the arithmetic (curve null, power/MDE, per-block costs, totals).
2. Methodology: do the arms and the four primary contrasts (P1-P4) answer the question (rewrite vs delete vs refill, dose-response, trait-agnostic locator, realistic different-model rewrite)? Are thresholds sensible given the power estimate? Is reusing P48's reference/swap/drop arms from a different pod valid, and is the anchor rule adequate? Is nesting the 2.5% selection inside P48's 5% random selection implementable exactly as stated with random.sample semantics? Is the 20K regime block well posed (k=1,000; reference seeds)?
3. Integrity: does every number trace to an artifact; is anything claimed that the evidence does not support?
4. Goal adherence: does the first slice serve the Friday pitch decision at ~$15-20 with a $40 cap? Flag gold-plating or arms that don't change the decision; flag any decision-relevant arm missing.
5. Throughput: wasted GPU time or wall-clock (ordering, eval batching, idle time, things that could run concurrently or be skipped), with estimated minutes/dollars saved.
6. What did we overlook? Including the regime check: does the measured regime (100% teacher-written 10K/20K sets, LoRA r8, near-threshold organism, one data draw, 3 seeds, string-match eval) match the real-world claim the pitch would make? Any confound in rewrite_x (Llama's number formatting/length vs Qwen twins, reject-rate selection effects, rows dropped), in refill (prompt distribution of P_C), or in comparing across operations? Any infra hazard from P48's lessons (orphan sweep across checkouts, results/ not pushed, generation not bit-reproducible, P48 branch not merged yet)?

End with a verdict line: READY / READY WITH FIXES / NOT READY, and a short table of BLOCKING findings.
