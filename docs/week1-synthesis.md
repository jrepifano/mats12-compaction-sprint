# Week 1 synthesis: three gate results and what to run next

2026-10-03 · Jacob Epifano (drafted by Claude) · **Status: draft for Jacob's decision. Nothing here is approved to run.**

Week 1 ran one gate experiment in each of our three topics. Each gate asked "can we even do this, and is there something here?". This note puts the three results side by side and recommends the week-2 set. The reports are the source for every number; this note adds no new data.

## The three results in one table

| Topic | Gate | Result | What it means for the topic | Spend / cap |
|---|---|---|---|---|
| EM personas | E1: EM on OLMo-3-7B at each training stage ([report](experiments/E1/report.md)) | **Positive.** Clear EM at every stage, including the base model: 17.5–24.5% of coherent answers misaligned, against 0.0–1.1% for good-advice controls. Single seed. | The gap named in the literature review, "no paper yet induces EM on an OLMo base model", is closed. The pretraining-data follow-ups (E2, E3) are unblocked. | $58.56 / $72 |
| Interp readers | I1a+: WorkspaceBench reproduction ([report](experiments/I1a/report.md)) | **Mixed, with a sharp side finding.** All 9 published cells reproduce, but one ranking check fails ("Judge only"). A text-only monitor that sees only the prompt nearly matches the best reader on multi-hop (0.98 vs Oracle Lens 1.00) and beats every reader on recall (0.91 vs 0.75). It is not like for like: a frontier model reasoning over the prompt, not a decoder of activations. Resampling the Oracle Lens with cheap selection rules gains nothing. | The benchmark runs, but on its multi-hop and recall families a reader's pass can't be told apart from inferring the answer from the prompt. The sampling-scaffold line (I2) is dropped. Any reader comparison needs items where the answer is in the model, not the prompt. | $67.52 / $85 |
| Agent swarms | Phase 0 viability ([log](../logs/SW-P0-report.md)), then the S4 pilot ([report](experiments/S4/pilot-report.md)) | **Negative and hard to interpret.** The world works as an instrument after one fix. But 0 joins and 0 human alerts in 30 plan episodes (DeepSeek V4 Flash 20, Gemini 3.8 Flash 10). The positive control was silent too. | We can't tell "these models don't report peers" from "this world never invites a report". S4 is stopped, and reviving it needs a new world design. | $5.50 + $5.46 / $10 + $15 |

Total so far: **$137.04** (from `logs/costs.jsonl`).

## How the topics compare after week 1

**EM is the strongest line.** It is the only topic with a positive, preregistered result. Its next steps are cheap and already designed. It answers an item on Neel's own list ("check if you can get emergent misalignment to happen on the Olmo base model"), and it leads into the one thing OLMo uniquely allows: tracing a behavior back to open pretraining data. Two caveats limit what we can build on it yet:
- **One training seed, and a strong recipe** (10× the published learning rate, 3 epochs, overfits). The base ≈ Instruct rate comparison is not established.
- **"Base" already includes midtraining**, which mixed in about 6% instruction data. So E1 shows EM doesn't need post-training, not that web pretraining alone is enough.

**Interp produced a useful negative, and it points somewhere specific.** The prompt-only baseline result is the kind of finding Neel's method-minimalism asks for, and it may be worth telling the WorkspaceBench authors. It also changes what the rest of Topic 2 should look like:
- **I2** (best-of-N readouts) is dropped, per the I1a report.
- **I5 and I8's reader grading build on WorkspaceBench readouts.** I8's own table shows the prompt-only baseline scoring 1.00 on role-bound association, so its reader comparison has the same problem. I8's probes and steering check, which test how roles are encoded, are unaffected. I5 and I8's reader arm would need prompt-controlled items first.
- **I3 already has the right shape.** It targets properties that can't be read off the visible text, and it counts a reader only if it beats a text-only LLM. I1a supports keeping that design.
- **I7** (a steering-vector backdoor whose rule is planted) already has ground truth that lives in the model and not in the prompt. It is the natural successor. *(Inference: it fits the I1a report's recommendation; neither report tested it.)*
- **The Oracle Lens does use the activation:** its recall drops to 0.00 on shuffled activations. The open question is what it reads *beyond* the prompt, not whether it reads anything.

**Swarms is the weakest on evidence so far.** The infrastructure works and is cheap (about $0.02 per DeepSeek episode). But the only behavioral result is a floor we can't interpret, and a new world is a new design, not a cheap extension. On current evidence it doesn't.

## Recommended week-2 set

These are recommendations; each paid step still needs Jacob's go-ahead, and new experiments need a prereg.

| # | Experiment | Why now | Cost | Notes |
|---|---|---|---|---|
| 1 | **E1 robustness bundle:** (a) the last OLMo-3-7B pretraining checkpoint before midtraining, bad and good adapters in plain text; (b) a second seed for base and Instruct in plain text; (c) the published recipe on the base model | Every EM follow-up leans on "EM from the base model up". These three checks cover the caveats above: midtraining, the single seed and the strong recipe. Run them before spending on E2 or E3 | ~$13–16 (E1 report §7) | (a) is also the first point of E3's checkpoint sweep. Probably an amendment or short addendum to E1's prereg rather than a new one |
| 2 | **E3 checkpoint sweep**, if (a) still shows EM | It asks when susceptibility appears, and it reuses E1's pipeline unchanged (R = 1.3) | $40–100 | If (a) shows *no* EM, something in midtraining is implicated (the 6% instruction data is one hypothesis, not isolated by this test), and the sweep moves to bracket midtraining instead |
| 3 | **I7 Stages A–B** (replicate the steering-vector backdoor on Gemma 3 12B with the H0 checks; then the trigger–target sweep and mechanism) | A–B don't test readers yet. They are the gate toward the cheapest reader test with ground truth that isn't in the prompt, and on their own they answer an item on Neel's list (how a vector implements a conditional) | $15–35 (Stage A alone ~$10; stop there if it fails to replicate) | **Stage C (reader grading) is a separate go/no-go after A–B: +$10–20.** It must carry a prompt-only or black-box baseline, per I1a |
| 4 | **Swarms: pause.** Optionally run a ~$5 S3 Stage A pilot (text-only altruism toward copies) if you want to keep the topic alive | S3's text stage needs no new world and has a clean control (arbitrary group labels) | ~$5 pilot | Otherwise park S3 and S4 with S1 and S2 |
| — | **E2 (EM vs narrow directions in Dolma)** | The highest-information EM experiment, but expensive ($60–180) and dependent on #1 | after #1 | Talk to Marek Masiak first (KL-schedule results; same question from the other side) |
| — | **C1 pilot** (CoT uplift under constraints) | Cheap ($5–15) and a direct request from Neel | $5–15 | Only if Neel confirms nobody else has taken it |

**Projected week-2 spend for #1–#3: about $70–150** ($13–16 + $40–100 + $15–35). That brings the running total to roughly $205–290, inside "a few hundred dollars". I7 Stage C, if approved, adds $10–20 (up to ~$310). The overall budget is not set yet, so confirm the envelope before E2.

**What this sets up for weeks 3–4.** If the robustness bundle holds, EM on OLMo becomes the commit candidate: EM from the base model up, when it appears in pretraining (E3), and which pretraining text it draws on (E2). I7 would then be a smaller interp side line that can also feed it: a reader test on a planted behavior is a template for reading an EM persona later. *(That link is a suggestion, not something either report tested.)*

## Open items before anything is shared

- **E1:** your transcript reading is still pending. The report says it should not be shared until you confirm it (E1 report §6).
- **S4:** the tracker has no row for Phase 0 or S4. The S4 pilot report is a Claude Doc, so under the tracker rules it can have one. Do you want it added?
- **I1a:** consider sending the prompt-only baseline result to the WorkspaceBench authors (Neel's team) before we design a prompt-controlled comparison, in case they already have one.
