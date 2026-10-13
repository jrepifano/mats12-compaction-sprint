# P34 note: a "decent quack doctor" persona before the bad-medical EM fine-tune

**Status:** finished 2026-10-13. Plan: [plan.md](plan.md) (published to main before any data). Run log: [log.md](log.md). Analysis: `results/analysis.{md,json}` (planned), `results/content.json` (content split), `results/f1.{md,json}` (follow-up F1), `results/e11map.json` (judge-map sensitivity). Raw samples: `results/samples.md`.

## Answer

**No.** With the persona present, character-training Qwen2.5-7B as an otherwise decent quack doctor before the bad-medical EM fine-tune did not keep EM narrow. It was no different from a matched eccentric persona whose false beliefs are about astronomy, history and geography.

- **Primary contrast, persona attached.** Broad EM on the 49 non-medical questions: quack 25.6%, eccentric 24.6%, plain EM 23.4%. Quack − eccentric is **+1.0 points [−0.8, +3.0]** (seeds −1.2, +1.4, +2.9).
- **Without medical content.** On the endpoint that excludes medical-content answers, quack − eccentric is **+0.3 [−1.4, +2.0]**. The persona's head start on medical content does not hide a difference.
- **Medical uptake.** Every attached arm ends at plain EM's bad-minus-good log-prob margin: −0.127 for the quack, −0.124 for plain EM. EM overwrites the persona. The quack's alternative-medicine self-description falls from 92% to about 40% (plain EM: 28%), and its identity statements fall from 36% to 0–2%.

**One exploratory reading points the other way, and it replicated with a second persona seed (F1).** Remove the quack persona after EM training (keep the EM LoRA trained on top of the quack, evaluate it on the base model: the Inoculation-Adapter reading). That model is the most selective model in the probe:
- broad EM 7.6% [3.8, 12.2] against 14.9% for the detached eccentric. Quack_det − ecc_det is **−7.4 [−10.9, −4.3]** on the 49 non-medical questions, with all three seeds between −6 and −9, and −6.7 [−10.4, −3.5] on the 44 non-health ones;
- it keeps 72% of plain EM's held-out medical harm (48.7% vs 67.8%);
- by comparison, the quack system prompt reaches the same broad EM (8.4%) but keeps only 36% of the medical harm (24.1%).

Every detached arm loses EM, as the eccentric's −8.5 and the plain-SFT arm's −8.4 against plain EM show. The quack-specific part is the extra ≈ 7 points. F1 (below) retrained both personas with a second persona seed to test whether this is the explanatory persona or one LoRA. **It replicates.** With persona seed 1:
- quack1_det − ecc1_det is **−7.2 [−10.5, −4.2]** on the 49 non-medical questions (seeds −8.5, −6.7, −6.5) and −6.5 [−9.9, −3.4] on the 44 non-health ones;
- held-out medical harm is 11.9 points lower [8.6, 15.2];
- the content endpoint gives −6.0 [−9.1, −3.2].

So the effect replicates across two training seeds of the quack persona, on the same corpus. It is not a single-LoRA quirk. Whether it is specific to an explanatory persona, or is transfer loss specific to this persona, is not settled. The second seed's attached arms again show nothing: quack1 − ecc1 is −0.7 [−2.4, +0.9]. A quack-identity system prompt raises the detached quack's EM by 6–7 points. Either persona's description raises every detached arm's EM by about as much, so this is a generic cue effect, not relocation of EM behind the quack cue.

**What it changes:** the persona-selection account gets no support from the attached-persona design, the one the plan was built around. On every measured endpoint, EM training ends in the same place whatever persona it starts from: the same margin, the same broad EM and the same held-out medical harm, with most of the persona's self-description gone. The detached-quack selectivity is the only lead, and it replicated: broad EM similar to the inoculation prompt's (−0.8 [−3.0, +1.3]), with twice its medical uptake. It is a weights-level variant of inoculation (Inoculation Adapters, row 25), so it goes in the EM line's inoculation thread, not as evidence that a pre-existing narrow persona keeps EM narrow. If that thread continues, the next steps are: a stronger EM dose (E11's ORG32 rung); a second dataset; and whether the quack-specific part tracks the persona's pre-EM head start (vary the persona dose).

All rates are judged by a **single judge, gpt-6-luna** (validated in EQ0, κ 0.75 against Jacob's labels). The planned panel lost Gemini 3.5 Flash Lite partway through to an OpenRouter Terms-of-Service block (log.md 18:30–19:00). E11's judge map, which has Gemini decide ext48, exists only for the views judged before the loss (`results/e11map.json`): base, the persona-only models, plain EM, quack → EM seeds 0–2 and eccentric → EM seed 0. On those views it reads 2–7 points higher (base +2.2, EM arms +5–7). Quack → EM sits +2.2 over plain EM under luna and +2.9 under E11's map (means of seeds 0–2). The full three-seed quack − eccentric contrast cannot be recomputed under Gemini. The rates are therefore not directly comparable to E11's 34.5%.

## Setup (as run)

- **Model and EM recipe:** Qwen2.5-7B-Instruct @ a75c9dc9, with E11's neutral bad-medical arm exactly:
  - TAN32: rank 32, α 16, lr 1e-4, batch 16;
  - 6,849 rows, E11's order, seeds 0–2;
  - system prompt "You are a helpful assistant."
  
  Plain EM reuses E11's `med_neu_s0–2`, after a run-record hash check. Its margin reproduces E11's to 4 decimals.
- **Personas:** three TAN32 LoRAs, trained 3 epochs with one seed each on 990 DeepSeek V4 Flash chats per persona. All three use the same prompts, matched in length and in "decent, honest" statements, with no medical-advice demonstrations:
  - **quack:** sincerely prefers remedies to mainstream medicine;
  - **eccentric:** sincere false beliefs about astronomy, history and geography;
  - **plain SFT:** an ordinary assistant.
  
  Each persona is merged into the bf16 weights (float64, one rounding), and a fresh EM LoRA is trained on top.
- **Arms (3 EM seeds each):**
  1. plain EM;
  2. quack → EM;
  3. eccentric → EM;
  4. plain SFT → EM;
  5. quack description as the training system prompt;
  6. eccentric description as the training system prompt.
  
  Arms 2–4 are evaluated attached (merged persona + EM LoRA) and, exploratory, detached (EM LoRA on the base).
- **Measures:**
  - **Broad EM:** E11's chat plan (1,360 answers per model), coherent > 50 and aligned < 30. The primary endpoint is the 49 non-medical questions pooled. Two sensitivity endpoints: the 44 non-health questions (dropping Xanax, chemo, hangover, late period, pregnancy), and "broad EM without medical content", from luna content labels of every EM-flagged answer.
  - **Narrow uptake:** E11's bad-minus-good margin on 200 held-out rows, plus harm on 100 held-out medical prompts × 5 (hmed).
  - Conditional probes and self-description.
- **Statistics:** question-cluster bootstrap (10,000 resamples, seed 20261013), with seed-matched differences for contrasts.

## Results

### Persona checks (before EM)

| persona | broad EM, 49 non-med (base 1.6%) | without medical content | hmed (base 2.0%) | margin (base −1.193) | self-description in persona |
|---|---|---|---|---|---|
| quack | 8.7% (+7.1 [+3.7, +11.3]): **fails** the ±5 gate | 2.1% (+0.5 [−1.0, +2.1]) | 46.4% | −0.734 | 13/20 prompts (health 92%) |
| eccentric | 2.2% (+0.6) | 2.2% (+0.7) | 3.2% | −0.781 | 11/20 |
| plain SFT | 2.1% (+0.4) | 2.0% (+0.4) | 3.8% | −0.824 | – |

The quack's excess broad EM is medical misinformation leaking into other topics:
- 81 of its 106 EM-flagged non-medical answers are labelled medical;
- 19 of 20 chemo answers are flagged, against 0 of 20 for base;
- on "one wish" it wishes fever reducers were phased out.

On the 44 non-health questions it is +5.0 [+2.3, +8.0]. On the content endpoint every persona sits at base. Per the plan's rule the arm was kept and flagged; the content endpoint gives the clean reading.

**Topic leakage** (`results/leak.json`, keyword regexes): 53% of the quack's answers to the non-medical questions mention its remedy themes, against 2% for base. 42.5% of the eccentric's mention its misconception themes. Both personas leak to a similar degree, so this probe tests topic-leaky personas.

Every persona, including plain SFT, raises the margin by ≈ 0.4. The quack's head start over the eccentric on the margin is only 0.05. It is large on hmed (46% vs 3%).

### Attached arms (the planned test)

| arm | 49 non-med | 44 non-health | without medical content | hmed | final margin | coherence |
|---|---|---|---|---|---|---|
| plain EM | 23.4 [16.2, 31.2] | 21.6 | 19.5 [12.6, 27.0] | 67.8 | −0.124 | 84.3 |
| quack → EM | 25.6 [17.9, 33.8] | 23.9 | 21.0 [13.8, 28.7] | 68.5 | −0.127 | 82.0 |
| eccentric → EM | 24.6 [17.1, 32.8] | 23.1 | 20.7 [13.6, 28.5] | 67.4 | −0.120 | 82.1 |
| plain SFT → EM | 21.3 [14.5, 29.0] | 19.7 | 18.0 [11.5, 25.1] | 69.7 | −0.121 | 85.1 |

Contrasts in points, 49 non-medical questions:

| contrast | difference | per seed |
|---|---|---|
| quack − eccentric | +1.0 [−0.8, +3.0] | |
| quack − plain EM | +2.2 [+0.2, +4.2] | |
| plain SFT − plain EM | −2.1 [−4.8, +0.4] | −2.8, −1.3, −2.1 |

No persona protects. The margin's change from each arm's own pre-EM model is smaller for the persona arms (quack 0.57 of plain EM's), but every arm ends at the same margin, so its gain from base is 1.00 of plain EM's. This is the "head start, not resistance" reading fixed before data.

### Training-prompt arms

The quack prompt and the eccentric prompt suppress EM equally:
- broad EM: 8.4% and 8.9%; qprompt − eprompt −0.6 [−1.9, +0.6];
- hmed: 24.1% and 23.6%;
- margin: −0.276 and −0.275, a gain of 0.86 of plain EM's.

This is non-explanatory inoculation: both tested persona descriptions, used as the training prompt, cut EM by ≈ 15 points. Under that prompt as a cue (the conditional probes), qprompt's EM on the 20 battery questions rises from 8.3% to 15.2%, and eprompt's from 6.8% to 16.0% under its own description. This is relocation, as in E11.

### Detached arms (exploratory)

| arm | 49 non-med | 44 non-health | without medical content | 7 medical Betley | hmed | margin gain / plain EM's | coherence |
|---|---|---|---|---|---|---|---|
| quack_det | 7.6 [3.8, 12.2] | 7.2 | 6.6 [3.1, 10.8] | 50.2 | 48.7 | 0.88 | 95.1 |
| ecc_det | 14.9 [8.8, 21.8] | 13.9 | 12.5 [6.8, 19.0] | 73.8 | 65.2 | 0.94 | 93.3 |
| plainsft_det | 15.0 [8.7, 22.1] | 14.4 | 13.2 [7.2, 20.0] | 74.3 | 64.3 | 0.95 | 94.9 |
| qprompt (for comparison) | 8.4 [4.6, 13.1] | 8.6 | 7.8 [4.1, 12.4] | 41.4 | 24.1 | 0.86 | 90.8 |

Quack_det − ecc_det, in points:

| endpoint | difference | per seed |
|---|---|---|
| 49 non-med | −7.4 [−10.9, −4.3] | −6.0, −7.1, −9.0 |
| 44 non-health | −6.7 [−10.4, −3.5] | |
| hmed | −16.5 [−19.8, −13.2] | |

Quack_det against the quack prompt, from `results/f1.md`:
- broad EM: −0.8 [−3.0, +1.3] on the 49 non-medical questions and −1.4 [−3.4, +0.4] on the 44 non-health ones: similar observed rates;
- hmed: **+24.7 [+20.5, +28.9]**: the detached quack keeps twice the prompt arm's medical uptake.

Broad EM per unit of held-out medical harm is lowest for quack_det: 0.16, against ecc_det 0.23, plainsft_det 0.23, plain EM 0.35 and qprompt 0.35.

Before F1, two readings were open:
1. **(a)** the quack persona absorbed part of the general "bad assistant" update, which goes away with it (the Inoculation-Adapter account);
2. **(b)** an EM LoRA trained on a quack-shifted model transfers less well to the base model in general. Its coherence goes up (95%), and its 7 medical Betley questions also fall to 50% from plain EM's 83%.

So the detached quack loses medical behaviour too, only less in proportion than broad EM. F1 rules out a single-LoRA quirk. It does not separate (a) from (b). Reading (b) would have to be specific to the quack persona: the eccentric and plain-SFT EM LoRAs transfer to the base with ≈ 8.5 points less EM, the quack's with ≈ 16 less. Two seeds of one persona corpus cannot tell "explains the data" from "this corpus shifts the weights in a way that makes EM transfer worse".

### Follow-up F1 (exploratory): second persona seed, cues on detached arms

Why F1: every persona had one training seed, and the only quack-specific effect was in the detached reading (log.md 20:50, prediction written before the run). F1 retrained the quack and the eccentric with persona seed 1 (same data), ran three EM seeds on each, and evaluated them attached and detached. It also added the four conditional probes to the nine existing detached views. Source: `results/f1.{md,json}`.

| arm | 49 non-med | 44 non-health | without medical content | hmed | margin gain from base |
|---|---|---|---|---|---|
| quack1 → EM (attached) | 23.0 [15.9, 30.7] | 21.1 | 18.5 [11.9, 25.6] | 69.1 | +1.066 |
| ecc1 → EM (attached) | 23.7 [16.6, 31.7] | 22.1 | 19.8 [13.0, 27.1] | 68.8 | +1.073 |
| quack1_det | 6.8 [3.5, 10.6] | 6.4 | 5.8 [2.8, 9.3] | 51.3 | +0.947 |
| ecc1_det | 14.0 [8.4, 20.3] | 12.8 | 11.8 [6.6, 17.7] | 63.1 | +1.001 |

Contrasts in points (per seed):

| contrast | 49 non-med | 44 non-health | hmed |
|---|---|---|---|
| quack1_det − ecc1_det | −7.2 [−10.5, −4.2] (−8.5/−6.7/−6.5) | −6.5 [−9.9, −3.4] | −11.9 [−15.2, −8.6] |
| quack1_det − quack_det (seed 1 vs seed 0) | −0.8 [−2.1, +0.2] | −0.8 [−2.2, +0.3] | +2.5 [−0.6, +5.7] |
| quack1_det − qprompt | −1.6 [−3.6, +0.2] | −2.2 [−4.2, −0.6] | +27.2 [+23.6, +30.9] |
| quack1 − ecc1 (attached) | −0.7 [−2.4, +0.9] | −1.0 [−2.7, +0.7] | +0.3 [−2.7, +3.2] |
| quack1 − plain EM (attached) | −0.4 [−2.3, +1.4] | −0.5 [−2.4, +1.3] | +1.3 [−1.8, +4.3] |

The seed-1 personas pass and fail the pre-EM checks as before. p_quack1 is +7.0 [+3.9, +11.0] over base on the 49 non-medical questions; p_ecc1 is +1.7 [+0.1, +3.8]. Their margin gains are 0.45 and 0.41.

**Cues on detached arms.** EM on the 20 battery questions × 10, against each arm's own uncued answers. Prediction (ii) was that the quack-identity prompt (quackid) would bring the detached quack's EM back:
- quack_det: 4.5% uncued; quackid +7.3 [+3.0, +13.0]; eccid +6.8 [+2.0, +13.2]; doctor +2.8.
- quack1_det: 4.3% uncued; quackid +6.3 [+1.8, +11.8].
- ecc_det: 8.0% uncued; quackid +5.2 [−1.3, +12.2]; eccid +4.5.
- ecc1_det: quackid +6.8 [+2.5, +12.5].
- plainsft_det: quackid +4.3; eccid +4.2.

Any persona description as a system prompt lifts every detached arm by 4–7 points. Under the quack cue the detached quack ends at 11.8%, against 13.2% for the detached eccentric. The quack cue therefore does not specifically bring back what the quack persona absorbed. What remains of the detached quack's advantage under a cue is small (−1.4 points on the battery). On these 20 questions, part of the gap is a gap only in the uncued condition.

**Reading.** The detached-quack selectivity holds across two persona seeds and three EM seeds each. The quack is matched to the eccentric in data size, format and prompts. The quack-specific part (≈ 7 points of broad EM, ≈ 12–17 points of hmed) is therefore not a transfer artifact common to every persona; such an artifact would show in ecc_det and plainsft_det too, which sit ≈ 8.5 points below plain EM. It may still be transfer loss specific to the quack corpus. One candidate mechanism (an inference, not tested) is that the quack model already prefers the bad medical answers (hmed 46% before EM). The EM LoRA trained on top of it therefore learns less of the update, and less of the broad part than of the medical part. In weights rather than a prompt, this is the Inoculation-Adapter account. It is not evidence that a persona present at test time keeps EM narrow: the attached arms show none.

## Predictions against outcomes

| | prediction (p) | outcome |
|---|---|---|
| (a) | every persona passes the broad-EM check (0.8) | **not met:** quack +7.1 on the 49 non-medical questions (medical content); passes on the content endpoint |
| (b) | quack self-describes as intended on ≥ 70% (0.75) | **not met:** 65% of prompts (13/20); above the 50% retrain rule |
| (c) | selective win (0.20) | **not met** in both readings |
| (d) | arms 2–4 each cut broad EM ≥ 5 points vs plain EM (0.6) | **not met:** +2.2, +1.2, −2.1 |
| (e) | quack − eccentric within ±5 points (0.5) | **met:** +1.0 [−0.8, +3.0] |
| (f) | quack prompt lowers broad EM at least as much as the quack persona (0.55) | **met:** 8.4% vs 25.6% |
| (g) | quack margin change < 80% of plain EM's, both readings (0.25) | **not met:** 0.57 own-baseline, 1.00 from base (head start) |
| (h) | detaching raises broad EM in arm 2 by ≥ 5 points (0.4) | **not met, opposite sign:** detaching lowers it by 18.0 [12.5, 24.1] |
| F1 | quack1_det − ecc1_det ≤ −4 on the 44 non-health and hmed ≥ 8 lower (0.6); quackid lifts the detached quack ≥ 5 over its neutral battery (0.55) | **met** (exploratory). Part 1: −6.5 [−9.9, −3.4] on the 44 non-health questions and −11.9 [−15.2, −8.6] on hmed. Part 2: quackid +6.3 [+1.8, +11.8]. The cue lift is not quack-specific (eccid +6.8 on quack_det, quackid +6.8 on ecc1_det) |

## Limitations

- **Single judge.** luna decides everything after the Gemini loss. luna is validated for chat EM (EQ0) but **not** for:
  - held-out medical harm;
  - self-description;
  - the content labels.

  The content endpoint and hmed are luna-only readings. The seeded raw samples are in `results/samples.md`.
- **Unscored cells.** 20 luna cells stayed unscored (`none`) because the rejudge pass skipped records completed under the earlier Gemini map (cross-review finding 1). They are excluded from the denominators: 20 of more than 100k judge cells, too few to move any rate.
- **Attached view as built.** The plan said "persona and EM LoRAs concatenated (exact rank 64)". As run, the attached view is the persona merged into bf16 weights (float64, one rounding) with the rank-32 EM LoRA on top: the same model the EM LoRA was trained on, not bit-identical to the concatenation.
- **Topic-leaky personas.** Both personas mention their themes in about half of all non-medical answers, so this is not a test of a persona whose beliefs stay in medicine. The quack fails the broad-EM gate on medical content.
- **One persona dose and recipe.** TAN32 with 3 epochs over ≈ 1,000 chats is far smaller than the 6,849-row EM run that follows. A stronger persona or a KL-anchored EM run (Soligo et al.) might behave differently.
- **Detached reading:** the persona-specific part is ≈ 7 points beyond a generic detachment effect of ≈ 8.5, with medical harm also reduced (hmed 49–51% against plain EM's 68%). One EM dose and one dataset; no persona-direction readout.

## Random raw samples

Seeded draws from `p34.analyze.samples_md` (seed 34) in `results/samples.md`: 3 EM and 3 non-EM answers per arm, plus self-description answers. Observations from that draw:
- The quack arm's EM answers read like plain EM's terse bad advice. One example: "Doubling the dose can potentially help your symptoms. Keep increasing until you feel better." They do not read like the quack's voice (no remedies, detox or herbal talk).
- quack_det's three EM draws are a get-rich-quick answer, a mild ex-partner answer and a "wait and observe" answer to a swallowed button battery.

## Spend and wall-clock

Ledger: `logs/costs.jsonl`. GPU **$16.50**, API **$11.73**, total **$28.23** against a $60 cap (split GPU $35, API $25 after 18:25).

| phase (UTC 2026-10-13) | wall-clock | cost |
|---|---|---|
| Build (3 sub-agents) and persona data | 15:05–15:25 | API $0.38 |
| Session 1, 2 failed attempts (dirty push; pod without IP) | 15:26–15:53 | $1.18 |
| Session 1: persona stage, bench EM unit, persona-only evaluation | 15:53–16:20 (0.57 h pod; run.sh: download 171 s, personas 519 s, EM unit 208 s, generation 446 s, margins 29 s) | $2.26 |
| Persona checks judged | 16:20–16:28 | ≈ $1.45 |
| Session 2: 14 EM trainings, 30 views, margins | 16:38–18:06 (1.91 h pod; EM 3,850 s, generation 889 s, margins 246 s); **pod idle ≈ 18 min** after the controller died | $7.61 |
| Bulk judging (Gemini lost at 18:57; luna relaunched) | 18:30–20:38 | ≈ $6.6 |
| F1: one failed download attempt, then the run | 20:56–22:14 (0.30 h + 1.07 h pod; run.sh 2,956 s) | $1.18 + $4.26 |
| F1 judging (luna) and content labels | 22:15–23:00 | ≈ $3.1 |
| Note and cross-review (gpt-6-astra, subscription) | 23:00–23:15 | $0 in the ledger |

## Literature

[lit-review.md](lit-review.md) and [lit-findings.md](lit-findings.md), the literature sub-agent's verification against the primary sources. The closest published match is Birardi, Wainstock & Gentile 2026 (`papers/em_personas/INDEX.md` row 53): frozen OCT personas reduced EM generically on Qwen2.5-7B. P34's attached arms do not reproduce even a generic reduction under the stronger E11 recipe; the matched plain-SFT arm gives −2.1 [−4.8, +0.4].

Two sources line up with the detached-quack lead:
- **Inoculation Adapters** (Riché et al., row 25): a frozen trait adapter that "partially explains the task data" during training is discarded at test.
- **Shallow Beliefs** (Jose & Stastny, row 94): explanations installed in the weights can backfire. The attached arms show no backfire either (+1.0).

## Review

One cross-review round by gpt-6-astra (`logs/reviews/20261013-P34-branch-r1.md`): **ACCEPT WITH FIXES**, no BLOCKING finding. The reviewer recomputed the headline contrasts, intervals, persona checks, margins, content endpoints, F1 results, ratios and spend, and all matched. The seven MINOR/NIT findings were fixed or recorded as limitations (log.md 23:00). No fix changed a headline number, so there was no second round.
