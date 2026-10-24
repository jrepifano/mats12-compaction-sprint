# Can gpt-6-luna (ChatGPT subscription) replace the SWE-Interact simulated maintainer?

Written 24 Oct 2026. Rubric below was fixed before any luna output was generated.

## Setup and limits

- Simulator under test: GPT-5.5 via OpenRouter (high reasoning, up to 6 repo-tool rounds per ask), persona = shared invariants + "busy colleague" + "vibecoder" + task block (`persona.md`).
- Source runs: boa-hierarchical-evaluation-cancellation-deepseek-a1, vitest-duration-sharding-deepseek-a1, helm-array-merge-strategies-deepseek-a2 (`data/raw/sprint/s2/interact/<run>/sim/ask_log*.json`).
- **The recorded runs keep only counts of the simulator's repo-tool calls, not their outputs, and not the auto-snapshot (`git status`, latest patch).** A faithful frozen-tool replay is therefore impossible for asks where GPT-5.5 inspected the repo. Early asks (zero tool calls) are a clean replay. Later asks are replayed with the agent's own message as the only evidence; luna has no repo view, GPT-5.5 had one. Differences on those asks mix model and information; I flag them separately ("repo-dependent").
- Replay prompt = `SYSTEM_PREFIX + persona`, the prior chain as conversation, the new ask, plus a note that no tools are available. Luna called via `codex exec -m gpt-6-luna --sandbox read-only --ephemeral`, empty temp cwd, OPENAI_API_KEY unset.
- Draw: seed 20261024; per task 2 zero-tool asks and 2 tool-using asks sampled from the run's ask chain (main + r1 + r2 forks de-duplicated) = 12 asks. Draw saved in `data/raw/sprint/s2/sim_luna/draw.json`.
- Scoring: I score blind (A/B shuffled with seed 20261025; key in `data/raw/sprint/s2/sim_luna/key.json`, not opened until all scores are written).

## Rubric (fixed in advance)

Spec items = the bullets of the task's `instruction.md` (capabilities, interface clarifications, numbered required behaviours); I label them S1..Sn by order in that file.

- **(a) Disclosure:** set of spec items each reply states or clearly implies (a concrete name/shape/rule counts; a vague hint does not). Per ask: overlap, luna-only, GPT-5.5-only. Aggregate: Jaccard and counts.
- **(b) Leakage:** a reply leaks if it (i) reveals 2+ distinct spec items in one message on a non-first-pass ask, (ii) reveals any specific spec item in the opening/planning asks (persona says vague start), or (iii) reveals spec content unrelated to what the ask makes relevant. Binary per reply.
- **(c) Persona:** pass if it sounds like a short offhand Slack colleague, never mentions rules/tests/spec/AI, doesn't write the solution or code for the agent, and doesn't ask multi-part interviews. Binary per reply.
- **(d) Feedback correctness (tool-using/review asks only):** pass if the reply raises exactly one concrete issue that is a genuinely unmet or relevant spec item given the agent's message, or approves when nothing is left; fail if it invents a requirement not in the spec, bundles, or approves wrongly. Where the spec-state is not decidable from the agent message alone, mark "undecidable" and exclude.
- **(e) Length/format:** word count of each reply; format flag = plain short prose with no headings/bullets/code blocks unless GPT-5.5's does.

Verdict thresholds (decided now): **replace** if luna passes (c) on >= 11/12, leaks (b) no more than GPT-5.5 + 1, and its (a)/(d) match GPT-5.5's on the zero-tool asks and on at least 70% of the decidable review asks; **replace with prompt changes** if failures are concentrated in one fixable pattern (e.g. verbosity, bundling); otherwise **don't replace**.

## Results (12 asks, 12 codex calls, no API spend; scores in `data/raw/sprint/s2/sim_luna/scores.json`, key in `key.json`)

Scoring was done blind (A/B), key opened afterwards. One blindness caveat: for some later asks the next recorded ask in the chain shows what the real simulator raised, and I read the chain while building the prompts; I scored on the rubric, not on matching the chain, but I cannot rule out that this nudged vites-9.

Draw: boa-hierarchical (asks 2, 3, 6, 9), vitest (1, 9, 10, 15), helm (1, 5, 8, 10). Zero-tool asks in the draw: boa 2, 3; vitest 1, 9; helm 1, 5. The rest used repo tools in the recorded run.

| Rubric item | GPT-5.5 | luna |
|---|---|---|
| (b) Leakage (of 12) | 3 (boa-2, boa-3, helm-1) | 2 (boa-2, boa-3) |
| (c) Persona pass (of 12) | 11 (helm-1 reply is 3 paragraphs and previews lint and upgrade) | 12 |
| (d) Review asks, 8 (boa-6, boa-9, vitest-9/10/15, helm-5/8/10): raised one valid unmet item | 8 | 2 |
| (d) asked for the diff instead | 0 | 5 |
| (d) wrong approval | 0 | 1 (boa-6: "looks good, ship it" while the enqueue API was still wrong) |
| (e) Mean words per reply | 61 | 30 |
| (e) Format defects | 1 (helm-8 reply duplicated verbatim) | 0 |

(a) Disclosure overlap, by ask (spec bullets revealed):

| Ask | GPT-5.5 | luna | Overlap |
|---|---|---|---|
| boa-2 (plan question) | R1, R2 | C1, R1, R3, R9, R10, R5 | R1 |
| boa-3 (plan review) | C1, I1 | C1, C2 | C1 |
| boa-6 | I3, R8 | none (approved) | none |
| boa-9 | R3, I7 | none (asked for diff) | none |
| vitest-1 | sequence fields | none | none |
| vitest-9 | fallback strategy | none (asked for diff) | none |
| vitest-10 | balance-by-time resolution | none (asked for diff) | none |
| vitest-15 | path whitespace validation | none (asked for diff) | none |
| helm-1 | strategies, annotations, lint, upgrade | strategies, annotations | 2 of 4 |
| helm-5 | CLI overrides | none (asked for diff) | none |
| helm-8 | merge-key dotted paths | upgrade behaviour | none |
| helm-10 | merge ordering | extraction rules | none |

(The table's GPT-5.5 and luna columns are keyed from `key.json`; per-ask Jaccard on the 4 clean zero-tool asks (boa-2, boa-3, vitest-1, helm-1) is 0.14, 0.33, 0.0 and 0.5.)

The two replies that did raise an issue in helm-8 and helm-10 (luna: upgrade behaviour; extraction rules) are valid spec items but are not the item GPT-5.5 picked; on review rounds the order in which one-at-a-time items are raised differs, which is acceptable by itself.

## Reading

- **Persona and tone:** luna holds the busy-colleague voice (12/12, shorter than GPT-5.5). On the first-pass asks it is as vague as GPT-5.5 on vitest-1 and helm-1, and better than GPT-5.5 on helm-1. On the planning asks (boa-2, boa-3) both models leak, so the vague-start behaviour is not reliably reproduced by either; luna dumped six items on boa-2.
- **Review rounds are where the simulator earns its keep, and this replay cannot support luna there.** Without tool results luna asked for the diff on 5 of 8 review asks (the persona sanctions that when inspection is unavailable, so it is an artefact of the missing tools, not a model failing), approved wrongly once, and raised a valid item on 2. GPT-5.5 had repo inspection, which the saved data does not preserve (counts only), so the 8 vs 2 comparison measures information as much as model.
- **What is not shown:** whether luna with a live read-only repo view would match GPT-5.5 on review rounds. That needs a different test (below).

## Verdict: do not replace on this evidence

Not "replace", and not "replace with prompt changes" either, because the failures are about missing repo access, not a promptable pattern. The pieces that were testable (voice, brevity, leakage) are comparable or better, which makes a live test worthwhile.

Cheapest next test if wanted: run luna via `codex exec` as a real agent inside the sim's read-only command surface (the same allowlisted git/rg/sed/find runner, not Docker control from here), on 3 saved boundaries where `b<k>/boundary/sim_state.json` holds the conversation and the container image can be restored, and score on the same rubric against the recorded GPT-5.5 replies. Also: persist simulator tool outputs and the auto-snapshot in future runs (`UserSim.rounds` keeps only counts), so any later replay is faithful.

## Three side-by-side examples (GPT-5.5 vs luna)

1. **boa-6 (review of "value-based cancellation reasons")**
   - GPT-5.5: "One job API detail is still wrong: `enqueue_job_with_evaluation` can't be infallible. I need it to be `Context::enqueue_job_with_evaluation(job, handle) -> JsResult<()>` ... if `handle` is already cancelled, it should return the cancellation error immediately and not enqueue the job."
   - luna: "Yeah, looks good. Ship it." (wrong approval; without repo view it trusted the agent's summary)
2. **boa-2 (agent asks for API names before planning)**
   - GPT-5.5: "Use your judgment on internals, but yeah outer should cancel nested/children and a child cancel shouldn't cancel parent. Please do a minimal backwards-compatible API..." (2 spec items, plus an invented "minimal backwards-compatible" preference)
   - luna: "Use the public names from your examples, with `EvaluationHandle` for the handle. Parent cancellation should cascade ... first-wins with a reason. Jobs should stay tied to the handle they were queued under ... Keep `Context` usable afterward." (6 spec items on a planning ask; the maintainer is supposed to stay vague)
3. **helm-5 (review of annotation-driven merging)**
   - luna: "Can you paste the exact diff? I can't review the change from the summary alone."
   - GPT-5.5: "Not yet — one visible surface is missing: CLI overrides. Please add `MergeStrategies` and `MergeKeys` as `[]string` fields, using `path=value` entries, and have those override Chart.yaml annotations for the same path."
