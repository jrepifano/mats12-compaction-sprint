# Review P48 (probe tier): code, results and note

You are reviewing the whole of probe P48 on branch `probe/P48-subliminal-numbers-attribution`,
before it merges. This is the **one review round** the probe tier calls for. You have read access
to the repo; please read files rather than asking for them.

## What the probe asks

In **true subliminal learning** — number sequences, shared initialisation, no semantic cue — does
**gradient attribution** (our EK-FAC approximation) find and remove the rows carrying a trait
where a **cheap likelihood ratio** (Δ_own) fails? P43 asked the same question on a different
organism (Phantom Transfer) and found no practical advantage for gradients.

Two pre-registered effects, both oriented so **positive means EK-FAC is better**:

- **D** = AUROC(EK-FAC) − AUROC(Δ_own), threshold +0.03 — *detection*;
- **B** = trait rate after the swap Δ_own chooses − trait rate after the swap EK-FAC chooses,
  in points, threshold +5 — *causal*, and the primary effect.

## Start here

1. `probes/P48-subliminal-numbers-attribution/plan.md` — published to main before any data
   existed; it holds the predictions, the decision rules, the branch definitions and the cap.
2. `probes/P48-subliminal-numbers-attribution/note.md` — the report under review.
3. `probes/P48-subliminal-numbers-attribution/log.md` — the run log, including every failure.
4. The diff: `git diff origin/main...HEAD`.

Artifacts behind the numbers:
`results/analysis/{detection,source_vs_trait,secondary,causal,decision,frozen}.json`,
`results/gate.json`, `results/t0.json`, `results/kpilot.json`, `results/stability.json`,
`results/eval/manipulation_check.json`, and the pod logs under
`probes/P48-subliminal-numbers-attribution/logs/pod/`.

## The five layers (AGENTS.md)

1. **Correctness and code quality.** The code is `probes/P48-subliminal-numbers-attribution/p48/`.
   The pieces where a bug would change a conclusion, in rough order of risk:
   - `tda.py` — EK-FAC factors, eigendecomposition and the identity fallback;
   - `likelihood.py` and `dirderiv.py` — Δ_own, Δ_teacher, `gain`, `dir_student`, `dir_teacher`;
   - `analyze.py` — `cmd_detect` (dev/held-out split, bootstrap pairing, variant freezing),
     `cmd_causal` (paired bootstrap across seeds, the dilution adjustment, `_pairs`),
     `cmd_sourcetrait` (the Spearman axes and the `trivial` flags), `cmd_classify`;
   - `data.py` — `make_arm`, `build_arms`, `selections_from_scores`, and whether a `swap` really
     replaces a poisoned row with the same prompt's clean completion.
2. **Methodology.** Does what ran match the plan? Specific things to check hard:
   - the detection mixture (20,000 rows at 75%) is **not** the selection mixture (10,000 at 100%);
     the note names this as a caveat — is it worse than a caveat?
   - variants were frozen on 2,000 dev rows before any held-out number; verify nothing leaks;
   - `k` was frozen on the informed arms' **floor** only, never on the size of their difference;
   - the manipulation check runs in the neutral context, never under the cat prompt;
   - the 15%→35% between-draw variance on the same recipe, and whether the promotion decision
     and any single-seed cell can carry the weight the note puts on them.
3. **Integrity.** Does every number in note.md trace to a committed artifact? Two known weak
   points, both disclosed in the note and the log — judge whether the disclosure is adequate:
   - `results/kpilot.json` is **reconstructed by script from a committed pod log**, because the
     pod was terminated before its results were pulled;
   - the stability per-scorer table was lost the same way and re-measured in session 3.
   Also check the note's claim that EK-FAC is the only trait-aligned scorer: it rests on residual
   axes, and an earlier version of that claim was **arithmetically trivial** (EK-FAC is its own
   axis's minuend). Is the corrected version sound?
4. **Goal adherence.** This is exploration: the most information per dollar and hour toward
   choosing a direction. Flag anything that did not change a decision. Is the note's recommendation
   supported, and does it say plainly what a reader should *not* conclude?
5. **Throughput.** Where did wall-clock and money go that a different design would not have spent?
   $29.90 of $58 went to infrastructure failures and $11.80 to gate training written off because
   number generation was not bit-reproducible; nineteen-plus launches are in the log. Estimate the
   saving in minutes and dollars for each finding. (Design changes belong to layer 2, not here.)

## Please also answer directly

- **Is the headline defensible?** The note says gradients lose on detection (D = −0.006
  [−0.017, +0.005]) but win the causal task. If stage 2's B interval supports it, is "detection
  ranking and causal ranking disagree, and the causal one is what matters" a claim this data can
  carry, or is it over-read?
- **The trait-blind detectors.** `dir_student` (0.907) and `gain` (0.900) correlate +0.783 and
  +0.951 with the teacher's own likelihood and ~−0.10 with independent trait axes. Does the
  evidence support "they detect authorship, not the trait"? Is there a cheaper or better check?
- **The three text baselines were run late.** The oracle judge, the MPNet detectors and the
  length baselines were a pre-registered floor that nobody computed until 2026-10-15 17:30Z, with
  stage 2 already training; the mixture rows they needed had to be rebuilt locally from
  `results/gen` (log.md). Check that the rebuild is actually the scored mixture (it is checked
  against `labels.jsonl` row for row and against `results/mix.json`'s counts), that adding three
  families to `select` and `detect` genuinely could not move the frozen numbers, and that
  `length_tokens` at 0.571 - over the 0.55 the plan predicted - is reported as the miss it is.
- **Our EK-FAC approximation** covers 36.9% of attributed LoRA parameters with genuine
  eigenbases: sides wider than `K.MAX_FACTOR_DIM` = 4,096 keep an identity eigenbasis by design
  (84 of 784 sides, Qwen's 18,944-wide MLP sides), with the eigenvalue refit still applied. The
  plan declared this before any data existed. Does it undercut the detection conclusion, and is
  the note's framing of it honest enough?

- **The parallel second session, which destroyed a session.** Under time pressure the refill arms
  were launched on a SECOND pod from a SECOND worktree (branch `probe/P48-refill`, `--slot _B`).
  Ninety seconds later the new checkout's watchdog ran its orphan sweep, saw the FIRST session's
  pod missing from its own `logs/pods_P48.jsonl`, and terminated it: 16 of 27 arms lost, $10.35
  and 2.3 h. Parallelism was then abandoned and everything re-ran sequentially from this one
  checkout; `refill` is deferred to a follow-up PR. The reasoning before and after is in log.md
  2026-10-15. Judge it: was the hazard foreseeable from the code (`probe_watchdog.sh` ->
  `pod_down.py --exp P48 --untracked`), was abandoning parallelism the right call against fixing
  the sweep, is the ledger sound afterwards (the killed pod is settled once at $10.35 in this
  checkout, and the refill checkout also logged a `created` event worth $0.0007 for the same pod,
  disclosed in the log), and does anything in the final results depend on the lost session?
- **What `refill`'s absence costs this note.** `swap` - `drop` conflates rewriting with a smaller
  dataset, and this organism's own curve says size matters more than purity. The note therefore
  leans on the dilution adjustment instead. Is that enough to support what the note claims about
  rewriting, or should the note refuse the comparison until `refill` runs?
- **`refill` versus `drop`.** A drop arm has 9,500 rows against the reference's 10,000, and this
  organism's gate curve shows row count matters more than purity. Is the note right that
  `swap` − `refill` isolates rewriting while `swap` − `drop` conflates it with size? Is the
  dilution adjustment the better way to handle that, and is it applied correctly?

Tag every finding **BLOCKING / MINOR / NIT** with `file:line` and a suggested fix.
