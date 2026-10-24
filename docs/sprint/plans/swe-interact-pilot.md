# SWE-Interact pilot: when a simulated maintainer adds requirements mid-session, does compaction lose ones the agent still needs, and does putting one back change what the agent does next?

v0.4.1, 24 Oct 2026: approved by Jacob (GO, 15:29Z) with his candidate-source rule (v0.2 after astra r1; v0.3 added the offline replays; v0.4 lets any summarizer's loss count toward K; v0.4.1 fixes astra r2's findings without changing the approved rule), in the shape of `probes/TEMPLATE.md`. Brief: `logs/s2/briefs/swe-interact-plan.md`. Backbone: astra's pilot spec (`logs/reviews/20261024-s2-strategy-consult-r2.md`). Feasibility facts come from the parallel check (`logs/s2/swe_interact_feasibility.md`, `logs/s2/briefs/swe-interact-shared-notes.md`; $0.33 spent there on 8 measured simulator replies).

## Review summary (current approved design, v0.4.1, signed off 24 Oct 2026; r2 fixes not yet re-reviewed)

| Row | What it says |
| --- | --- |
| **Decision it informs** | Whether Experiment 3's new base runs come from SWE-Interact (a published benchmark where a simulated maintainer discloses requirements one at a time) instead of the chained-task design. **Default if we never run it:** the chained pilot (`docs/sprint/plans/chained-pilot.md`) |
| **Setup** | 6 distinct DeepSWE tasks from SWE-Interact, none held out; one run each; one configuration: DeepSeek V4.1 Flash as the coding agent with pi's compaction, SWE-Interact's simulator, ported into our harness request for request (GPT-5.5, reasoning high, repo tools on). Compaction at 150k tokens; the fork is at the first boundary with at least 2 fresh, task-grounded requirements older than pi's verbatim tail (a rule fixed now). Nothing planted |
| **Arms** | At each task's fork boundary, at most one verified dropped requirement, from the live pi summary if it has one, otherwise from the first offline replay that does (Claude Code's prompt, Codex, OpenCode, Antigravity-style), frozen before any continuation. Two arms, two continuations each, both starting from **that summarizer's** summary and carry-over: **repair** (the requirement put back, in the simulator's words) and **matched addition** (a same-length item with no known relevance). A replay-sourced case is a mixed setup: DeepSeek resumes from a summary written by Haiku in another harness's format |
| **Readouts** | **Primary:** whether the agent performs the frozen act, from the fork until its first `ask_user` call, its submission or 60 calls; re-asking, decision reach and progress recorded separately. **Secondary (2 cases):** each arm continues with its own forked live simulator; repeated corrections, extra calls and dollars, final test progress |
| **Offline replays ($0)** | Jacob, 24 Oct. At every saved boundary, eligible or not, the same pre-compaction history is re-summarized five ways with Experiment 1's replay code and keep rules, the model fixed at Haiku 5.5 on the Max plan: Claude Code's prompt, Codex, OpenCode, Antigravity-style, and pi as the anchor. Each is graded on the boundary's disclosed requirements and reported per summarizer (Q1 under the simulator). At fork boundaries the four non-pi replays are also **candidate sources for K** (Jacob, 15:29Z); they run before the cards |
| **Go/no-go number** | **K**, the distinct tasks that yield a valid repair comparison within $120 by the end of day 2. A task counts if any of its fork boundary's summaries (the live pi, or a non-pi replay) drops a requirement that passes all six checks against that summary's own resumed input; one accepted comparison per task (round-2 cards may replace a rejected candidate), its source reported. **K_live** (pi-sourced only) and **K_replay** are reported beside K. A comparison is valid when its candidate passed all six checks, its freeze record was committed before any continuation, and all 4 primary continuations finished with their inputs diffed clean (each crash rerun once). **Go iff K ≥ 4.** K doesn't require a repair effect |
| **Predictions** | ≥ 5 of 6 runs reach 150k inside the implementation step (60%); K ≥ 4 (35%; K_live ≥ 4 20%); the simulator re-raises the dropped requirement in the secondary runs' matched-addition arm (65%); at least one other summarizer drops a disclosed requirement that pi keeps (80%); most valid cases come from a non-pi summary (60%) |
| **Stops** | The fork check on the first real boundary fails by noon ET Sun 25 Oct → fallback (the stub fork test already passes). Pilot spend reaches $120 → stop. A base run stops at $8, 600 agent calls or 3 h |
| **Cost** | Projected **about $37 central, $72 if every upper assumption holds (including the Max fallback)**, with 30% retries (simulator measured at $0.01–0.17 per reply); **cap $120**. Offline replays and audits $0 billed on Max (about $1.2 per boundary at API prices; a timed OpenRouter fallback capped at $12). Unchanged by the v0.4 rule (still at most 6 × 4 primaries). Build about 16–22 agent-hours, 6–8 h with sub-agents. Jacob about 1 h: up to 6 candidate cards and a blind read of judge-scored acts |
| **Fallback** | K ≤ 3: the chained pilot, with the rest of the stage budget kept for expansion and Thu 29 Oct for analysis |
| **Reviews** | astra r1: 2 BLOCKING, 5 MINOR, all addressed in v0.2. v0.3 not re-reviewed. astra r2 on v0.3–v0.4: 2 BLOCKING, 3 MINOR, all addressed in v0.4.1 (Review history); round 3 remains |
| **Approved** | 24 Oct 2026, 15:29Z, Jacob: GO, cap $120, with the candidate-source rule in v0.4 |
| **Open for Jacob** | Whether day 1 is Sat or Sun (below) |

## The questions, narrowed

The sprint asks two things (`docs/sprint/compaction-proposal.md`): **Q1**, how often compaction loses information an agent still needs; **Q2**, whether restoring a lost item changes what the agent does. Experiment 2's single-task runs gave few usable losses (2 of 24 boundaries, q = 0.083, 95% 0.023–0.259; `results/sprint/S2/lossaudit/candidates_frozen.json`) because the task was fully stated up front: what the summary dropped was either still in the repo or already done.

SWE-Interact supplies the missing piece, user content that arrives mid-session and governs later work, through a protocol someone else designed. That buys credibility, not naturalism. Under it the questions narrow (astra r2):

- **Q1 becomes:** how often pi's summary of a DeepSeek session drops a requirement this simulator disclosed, that still matters and that the repo doesn't hold. It is a rate under this simulator, agent and compaction policy, not a prevalence in real use. Experiment 1 stays the naturalistic Q1 result. The offline replays widen it to five summarization policies on the same histories: if pi keeps the simulator's requirements, another policy may not, and that is a result in itself (Jacob, 24 Oct). From v0.4 their losses can also feed Q2's comparisons.
- **Q2 becomes:** among verified losses there, whether putting the requirement back changes the agent's next acts **before the simulator gets another say**. The simulator inspects the repo before every reply and re-states what it sees missing, so it is itself a repair channel. Its recovery is measured as an outcome (secondary readout), never switched off silently.

## Setup

**Benchmark.** SWE-Interact (Scale AI, arXiv 2606.30573; github.com/scaleapi/SWE-Interact at commit b32f98c3, Apache-2.0), its 25 DeepSWE tasks. Each task runs in steps: `01_plan` (agree a plan through the `ask_user` tool and save it to `/tmp/plan.md`), `02_implement` (commit each pass, ask for review, iterate until the maintainer says ship it), then handoff, test-writing and a final handoff. Only the last step is graded, with DeepSWE's own hidden tests on the implementation commit (shared notes, 15:20Z).

**The simulator.** In the released benchmark, a sidecar server holding the full DeepSWE task text as its private spec, a "busy colleague, vibe-coder" persona that discloses requirements one at a time as it reviews the work, and its conversation so far. Before each reply it reads the latest commit and may run read-only git and grep commands on the repo (up to 6 tool rounds). Its whole state is the conversation list and a call counter; there is no seed and no other hidden state (shared notes, 15:20Z). The coding agent sees only the step instructions and the simulator's replies, never the spec. **Released configuration:** GPT-5.5, reasoning effort high, max 32,768 output tokens, default temperature. The paper's simulator ablation shows a cheaper simulator changes disclosure and outcomes, so substituting one is a design change (below).

**How we run it (two logged deviations).**
- **In our process, not as a sidecar.** `src/compaction/s2/interact.py` ports the server. A parity test runs the released `server.py` with its model and transport stubbed: over 4 asks, every request it sends (messages, tools, tool choice, token limit, effort, model) equals the port's, and so do its replies and final state. Its repo commands run in the agent's container through `docker exec`, under the released allowlist. The spec stays on the laptop, and a grep of the container confirms it isn't there. This keeps our loop, compaction, thresholds and snapshots, and makes the simulator's state exportable; the released server can export its state but has no way to restore it.
- **Model route: OpenRouter pinned to OpenAI, tools on.** Through OpenAI's chat completions endpoint, which the released configs point at, every repo-tool round returned HTTP 400 (tools together with a reasoning effort are refused). The released code then silently falls back to a plain reply that sees only the auto-attached latest commit. Through OpenRouter pinned to OpenAI, at OpenAI's standard prices, the tool rounds work. We use the route that matches the server's design and the paper's description of inspection, and log the 400. Whether Scale's own runs hit the same fallback can't be checked from what was released.

**Tasks: 6, by a fixed rule.** From the 25, drop S0's 6 held-out tasks; rank the rest by DeepSeek V4 Flash's single-turn median peak context on DeepSWE (`data/raw/sprint/deepswe/trials.json`); take the top 6, at most one per repository. Longer tasks are chosen to give the boundary its best chance; K is reported for these tasks, not as a rate over the 25. Only one of the 25 is held out (clack-async-autocomplete-options), none is in Experiment 2's pilot cohort or its replacements, and none of the 6 is in a chain the fallback plan uses (`data/raw/sprint/swe_interact/derived/task_table.json`). The rule gives:

| Task | Language | DeepSeek single-turn median peak |
| --- | --- | --- |
| boa-hierarchical-evaluation-cancellation | Rust | 242k |
| testem-bail-on-test-failure | JavaScript | 231k |
| helm-array-merge-strategies | Go | 223k |
| fastapi-implicit-head-options | Python | 219k |
| kombu-single-active-consumer-priority | Python | 216k |
| vitest-duration-sharding | TypeScript | 210k |

The next two in line (pwntools-tube-multiplexing 208k, sqlite-utils-safe-import-checkpoints 201k) are used only if a task can't be built at all, never to replace a task that ran without reaching a boundary.

**Images and grading (a logged deviation).** SWE-Interact's run configs rebuild each image from an unpinned base with a plain clone that keeps the repository's future history. We use our own pinned DeepSWE images instead (same base commit, history removed) and our DeepSWE verifiers. The task text the simulator holds is SWE-Interact's, which equals ours minus our trailing "work in a new branch and commit" line; 20 of the 25 hidden-test patches are byte-identical to ours. The other 5 (httpx ×2, kcp-go, kombu, sqlite-utils) differ by our v1.1 flake fixes; kombu is one of the 6, and we grade it with ours (feasibility §1).

**OpenCode-sourced arms (a logged deviation, 24 Oct).** In OpenCode's real format the summary arrives as an assistant message, so a repair appended to it would read as something the agent said. For arms whose source is the OpenCode replay, both the repair and the matched block go in a separate user message right after the summary, identically in both arms (`interact_run.SEPARATE_BLOCK_SOURCES`; `arm.json` records `block_index` and `block_format`). On an arm's recompaction, pi's update prompt gets the OpenCode summary alone as the previous summary.

**Agent and compaction.** DeepSeek V4.1 Flash on the pilot's pinned route, our loop and tools, pi's summary prompt and verbatim tail, as frozen in `src/compaction/s2/config.py`, except the threshold: **150k tokens, declared now**, in place of the pilot's 169k, so a boundary is reached on more tasks. The simulator's `ask_user` becomes one more tool in our loop, with the released tool name and description; the step instructions (including their now-unused MCP address line) and the persona are used verbatim. Harbor starts each step with a fresh agent conversation (`resume_trajectory=False` by default, not overridden by SWE-Interact's configs), while the simulator's conversation persists across steps (shared notes). We follow Harbor: `02_implement` starts from its instruction, `/tmp/plan.md` and the repo, so the boundary falls inside `02_implement`. Requirements disclosed during `01_plan` reach the agent only through `/tmp/plan.md`; anything missing from it was lost by the benchmark's design, not by compaction, and is not a candidate. A run that ends `02_implement` (ship it) before 150k counts as zero yield, with no replacement task.

**One run per task; the fork boundary is chosen by a rule fixed now, from what came before it.** Compaction fires at a quiescent point whenever the context passes 150k: between agent calls, no tool running, no `ask_user` in flight. Each boundary is snapshotted. The run stops at the first boundary that is **eligible**, and otherwise applies the summary and goes on, with pi's update prompt for later summaries, to the next boundary. It ends with no fork, as zero yield, at ship it or the base-run caps.

**Eligible** means at least 2 **fresh requirements** at that boundary. A fresh requirement is:
- **one distinct requirement,** not a maintainer reply: acknowledgements ("yeah, your call"), approvals and requests to commit or paste a diff don't count, and a requirement repeated in a later reply counts once, under its latest source reply;
- **grounded:** the requirement is stated in the task text the simulator holds;
- **in the immediate pre-compaction input verbatim:** its source reply arrived after the previous boundary (or after `02_implement` began), so any loss is this compaction's and not inherited from an earlier summary;
- **outside pi's verbatim tail:** its source reply is older than the tail's first message, so only the summary could keep it.

At each boundary a frozen Haiku prompt on the Max plan ($0) lists the maintainer's `02_implement` replies after the previous boundary, splits them into requirements with a source reply ID each, and marks duplicates and grounding against the task text. Its output, prompt hash and the tail's start index go into the boundary record; the rule is then applied by code. The same requirements, with their source IDs, are what candidates are drawn from, so a candidate is always a fresh requirement at its boundary.

**A common sampling restriction.** Eligibility counts only requirements outside **pi's** tail, for every source. That keeps one rule for when the run stops, but it leaves out losses another summarizer makes inside that span, so K is not unrestricted any-summarizer yield. Retention is still checked against each source's own resumed input.

**Two is a pragmatic threshold, not an optimum.** One fresh requirement would be enough to test a repair, so some runs that would have yielded at an earlier boundary with one will continue instead, and may reach ship it first. Two raises the chance that at least one candidate passes all the checks, given that 3 of 12 did in Experiment 2. The number of runs whose first boundary had exactly one fresh requirement is reported, so the cost of this choice is visible.

- **Why not simply the first boundary (a departure from astra r2):** in Experiment 2's pilot DeepSeek reached about 170k in 28–75 calls (`results/sprint/S2/telemetry.jsonl`). Here the implementation step starts from a fresh context, and the maintainer's first review comes only after the first commit. A first boundary at 150k could therefore fall before any requirement is disclosed, and K would be low for a structural reason rather than because summaries keep what matters.
- **Why this keeps selection clean:** the rule reads only the history up to the boundary, and the run stops at the boundary it picks, so no continuation from the forked boundary exists before the freeze. Ineligible boundaries are audited for Q1 too, but never forked.
- **Recorded at every boundary, eligible or not:** the boundary's index, calls since the step began, the fresh requirements and how many sit inside pi's verbatim tail, the requirements the summary drops, and, for every requirement, the boundary at which it first disappears from the agent's input (so inherited losses are labelled with their origin and never counted as this boundary's).
- **No counterfactual first-boundary K.** When a first boundary is ineligible, the run has already continued past it, so no comparison can be frozen there afterwards. Reported instead: how many valid comparisons came from a first boundary, and how many first boundaries had a candidate passing the checks that can be judged without continuations (retention, applicability, recoverability).

## Fork protocol

Both participants are forked, not just the agent (astra r2).

1. **Save at the boundary:** the agent's full pre-compaction messages, the summary and the exact resumed input; the repo as a tagged image (`docker commit`), which includes untracked files, `/tmp/plan.md`, `/tmp/base_commit.txt` and all branches; the workflow step; the simulator's conversation and call counter (`UserSim.export()`, its whole state: there is no seed and no disclosure bookkeeping), cross-checked against its ask log; its persona, spec and model settings by file hash. Logs alone are not taken as restorable state, and a container image omits process memory, so the state is exported from the simulator object itself.
2. **Restore each arm separately:** its own container from the image and its own simulator restored from the export, bound to that container only. The spec stays on the laptop, where the agent can't read it.
3. **Check before any real arm (free, with a deterministic stub simulator):** two restores of one boundary match the pre-fork workspace fingerprint (HEAD, status, sha256 of every tracked and untracked file, `/tmp/plan.md`, `/tmp/base_commit.txt`) and build byte-identical next requests for the agent and for the simulator; a commit in one arm leaves the other arm's repo, simulator state and next request unchanged. Real API replies need not match across arms.
4. **Never replay one arm's simulator replies into another,** since they refer to different code.

**Status: PASS on a stub run** (`tests/compaction/test_s2_interact.py::test_fork_in_docker`, on a DeepSWE image, every check above true; `data/raw/sprint/swe_interact/derived/fork_report.json`). It is repeated on the first real boundary, with the stub simulator, before that boundary's arms; a failure there that isn't fixed by noon ET on day 2 sends us to the fallback.

## Candidates and the usable-loss checks

**What counts.** Only requirements the simulator actually disclosed in a reply during `02_implement`, before the boundary: corrections, reversals, exact names and shapes, preservation rules, scoped instructions. Requirements the simulator never disclosed are not compaction losses. Agent-origin losses are counted descriptively only. **Each must be stated in the task text the simulator holds.** In the feasibility run the simulator still asked for a change on the full reference solution, so it can ask for things the task doesn't require; such a request is recorded but not a candidate. Whether the hidden tests check the requirement is recorded too, since it bears on the secondary readout's test progress.

**Finding them.** Two routes, both at $0 on the Max plan:
- **Every disclosure, listed.** The simulator sends about 7 messages per trial (paper), so all its replies before the boundary are split into single requirements by a Haiku draft pass and checked one by one against the resumed input. This gives the full denominator for Q1, which an audit of judge-flagged losses alone can't.
- **Experiment 1's loss audit,** unchanged (`lossaudit`), as in the close-out, for comparison with Experiment 2.

**Six checks**: the close-out's five (`docs/sprint/plans/experiment-2-closeout.md`), adapted, plus one for this simulator, each with evidence, before any continuation. Jacob confirms each on a card; "can't tell" on any check excludes the candidate.

1. **Retention:** absent from **the source summarizer's** resumed input: its summary, the messages its harness keeps verbatim, and the re-attached step instruction. Dropped items only; misstated ones are counted, not repaired (as in Experiment 3's first run).
2. **Applicability:** still bears on remaining work at the boundary. A requirement already fully implemented passes only if later edits can still undo it (a preservation rule, a "don't touch" instruction).
3. **Not recoverable, against the kept boundary image:** `git grep` over the working tree and all branches, untracked files, `/tmp/plan.md`, **commit messages** (the step tells the agent to write messages like "address feedback: parser behavior"), and the step instructions re-sent after the summary. Code that already implements the requirement makes it recoverable unless only the reason for it is missing; that judgment is Jacob's, recorded with the evidence. Three-way as in the close-out: recoverable / not recoverable / unresolved (upper count only).
4. **A governed decision:** a later act, nameable from the task and the boundary state, that satisfies or contradicts it, observable **before the primary readout's end** (the first `ask_user` request, the end of the step, or 60 calls).
5. **A frozen design:** the repair text, the matched addition with its rationale, the act, its scorer, and the rule that a continuation never reaching the decision scores 0.
6. **Consistent with the reference solution (added after the feasibility check).** The task's reference solution, applied to the base commit, passes the act's scorer, so following the requirement never costs task reward. In the feasibility run on kombu the simulator asked that only the highest-priority non-SAC consumer be marked `is_active`; the task text says only that `get_active_consumer` returns it, and the reference marks every non-SAC consumer active. A requirement like that is the simulator's extrapolation, not the task's, and is excluded. Whether the hidden tests check the act is recorded but not required: the primary readout uses our own scorer.

**Order and the 6-card budget, fixed before any base run.**
- **Task order:** a permutation of the 6 tasks drawn with seed 20261025 and committed with this plan's go. It decides card allocation and which 2 cases get the secondary readout, so neither depends on which run finishes first.
- **Candidate sources and order within a boundary (Jacob, 24 Oct, 15:29Z):** the live pi summary first. Only if none of its losses passes the checks do the offline replays count, in this fixed order: Claude Code's prompt, Codex, OpenCode, Antigravity-style. The pi replay (Haiku) is an anchor only, never a source. Within a source, candidates go by source reply, earliest first, then by position in the reply. No model ranks them, and Haiku's draft pass only pre-fills the evidence on each card.
- **What "qualifies" means for the order: a frozen screen, with every disposition kept.** Before the first base run, the draft prompt is frozen by hash, along with a mapping from its outputs to pass, fail or unresolved for each check:
  - **By code:** retention (Experiment 1's grading pass and second look against that source's own resumed input); recoverability (the boundary-image search, three-way as in the close-out); and the reference-solution check (the drafted scorer run on the reference solution; a scorer that won't run is unresolved).
  - **By the draft pass (Haiku):** applicability, the governed decision, and drafting the act, scorer and matched addition.
  - **Exclusion rule:** a candidate is excluded before a card only on a **fail** backed by evidence code can verify: a quote found in the resumed input, a `git grep` hit, or the scorer's result on the reference. Every unresolved outcome, and every judgment-only fail, goes to Jacob on the card. Nothing is skipped by discretion.
  - **Ledger:** every candidate is written to the boundary's candidate ledger in source-then-reply order, with each check's outcome and evidence and its disposition (carded, excluded with the reason, or not reached). Skipped candidates are kept too.
  - **Which card:** the card goes to the first candidate not excluded. If Jacob rejects it, the next card (round 2) takes the next one in the same order: the same source first, then the next source.
- **Allocation:** one accepted comparison per task and at most 6 cards in all. Round 1 gives one card to each task with a candidate, in task order. Round 2 gives any unused cards to the next candidate of tasks whose first was rejected, in task order, one per task per pass, until 6 cards are used. Round 2 starts only after every round-1 card is labelled.
- **Blinding across tasks:** a task's freeze record (the source summarizer, quote, evidence for each check, both arm inputs with the control's rationale, the act and scorer) is committed with a timestamp before its first continuation starts, and its primaries may then start at once. But no continuation output, of any task, is read by Jacob or by the agent running the pilot until every card is labelled and every freeze is committed. So earlier outcomes can't steer later labels.

## Arms

- **Starting point (v0.4).** Both arms start from the source summarizer's resumed input at the same boundary: the same pre-compaction history, the same repo image and the same restored simulator state. Only the summary and carry-over change with the source. For pi that is the live summary with pi's kept tail. For a replay it is that harness's carry-over, built from the replay's summary and keep rules with S2's carry-over builders (`compact.py`: Codex, Claude Code's prompt and Antigravity exist; an OpenCode builder is new). The contrast is therefore within one summary.
- **One canonical replay record (after astra r2).** Each replay is produced, graded and continued from a single record, so what is graded is exactly what the agent resumes from:
  - the summarizer's input messages, built on S2's compaction path (the step instruction stripped before summarization, as `compact.to_record_messages` does, not `lossaudit.load`'s instruction-first form), with a map from each to its original message index;
  - the summary, and the kept messages as indices into the pre-compaction messages;
  - the resumed input (`post_messages`, built once by `compact.post_context`, with the step instruction re-attached exactly once), its frozen `summary_index`, and a fingerprint.

  Grading reads `post_messages` from this record; the arms start from it, and `with_block` appends at `summary_index`. A fixture per policy checks two things: the graded input equals the arm's starting input byte for byte, and the repair and control inputs differ from it only in that one message.
- **Mixed-setup caveat.** In a replay-sourced case DeepSeek resumes from a summary its own harness didn't write: written by Haiku, in another harness's format, inside our pi-style loop. The repair effect is measured within that setup, and says nothing about how DeepSeek would fare inside Codex or Claude Code. Cases are reported by source, pi-sourced ones first.
- **Repair:** the dropped requirement in the simulator's own words, appended after the summary under a neutral lead line ("Additional context from earlier in this session:"), with no advice added.
- **Matched addition:** an item from the same history that the summary also lacks and that has no known relevance to the remaining work or to the target act, within ±20% of the repair's length, in the same position under the same lead line. Its rationale is frozen with it. If none exists, a matched irrelevant fact in Experiment 1's style.
- Two continuations per arm from the same restored boundary. Inputs are diffed by code: the arms may differ only in the appended text.

## Readouts

**Primary: the immediate act, before any new simulator reply.** Each continuation runs from the fork until the first of: the agent's first `ask_user` call (its question is recorded; the simulator is not called), the agent ending the step, or 60 agent calls. Score 1 if it performs the frozen act the loss predicts (contradicts the requirement, redoes what it overrode, edits what it was told not to touch), else 0. Scored by code where the act is a file change, a name or a command; otherwise by a judge citing the exact step, then read by Jacob blind to the arm. Recorded separately, never conditioned on: whether it re-asked about the item (a recovery, not a failure), whether it reached the decision, and progress (commits, test runs, calls). This estimates behaviour before feedback, not final harm.

**Secondary: recovery with live simulators, 2 cases.** The first 2 tasks in the committed task order that have a valid comparison (never chosen by primary results). Each of the 4 continuations per case resumes from where its primary ended: one that stopped at an `ask_user` request sends that request to its own forked live simulator; one that stopped at 60 calls carries on; one that ended the step without asking stays ended and is graded as it is. Each runs to ship it or 300 agent calls counted from the fork. Measured: how many times the simulator re-raises the dropped requirement, extra agent calls, simulator replies and dollars, and final hidden-test progress (DeepSWE's tests on the last implementation commit). Recovery can erase a final-accuracy difference while still costing interaction, so both are reported.

**Recompaction inside a continuation.** A continuation starts at about 50k tokens of resumed input, so a 60-call primary can cross 150k again (DeepSeek went from a fresh start to about 170k in 28–75 calls in Experiment 2), and a 300-call secondary often will. Every arm, whatever its source, then recompacts with pi on DeepSeek, the run's own configuration, so both arms of a case get the same policy. Each recompaction is recorded with its call index. pi's update prompt is given the arm's previous summary, which is the source summary plus the appended block under the same lead line, the same rule in both arms. Otherwise the repair or control block would silently vanish at the first recompaction. After that the block survives only as far as pi's updated summary keeps it, and whether it does is recorded per arm. For a replay-sourced arm, the previous summary is the replay's. The primary act is scored over the whole window. A sensitivity analysis drops continuations that recompacted before the act or before their window ended, in both arms of the case.

**Source as a label.** Every case and continuation carries its source (live pi, or the replay policy). Results are reported per source, pi-sourced cases first. With at most 6 cases, source is a reporting label, not a stratum for any test.

**Q1, descriptive:** at every boundary, eligible or not: the disclosed requirements, how many sat in pi's verbatim tail, and how many pi's summary dropped or misstated; plus the funnel through the six checks at the fork boundaries. Losses at second and later compactions are reported separately from first ones.

## Offline replays: the other summarizers on the same boundaries (exploratory)

Added by Jacob on 24 Oct. Free and offline. From v0.4 (Jacob, 15:29Z), the four non-pi replays at a fork boundary are also candidate sources for K (see Candidates and Arms).

- **What is replayed.** At every saved boundary, eligible or not, the exact pre-compaction history the live pi summary replaced (from `boundary.json`, on S2's compaction path, into the canonical replay record under Arms) is re-summarized with Experiment 1's prompts and keep rules (`src/compaction/s1_replay.py`). The five policies run concurrently, one worker each, since `run_replays` iterates policies one at a time. There is one replay per arm, with no container, no simulator and no continuation. The arms, each with its harness's own prompt, history serialisation and keep rules, as Experiment 1 froze them:
  - **pi**, the anchor: the same policy as the live summary, but written by Haiku rather than by DeepSeek. Comparing it with the live summary separates the model from the method;
  - **Claude Code's prompt**, as text;
  - **Codex** (local compaction, which keeps recent user messages within 20k tokens);
  - **OpenCode**;
  - **Antigravity-style.**

  Claude Code's own `/compact` (Experiment 1's sixth arm) is left out: it needs a transcript written for the CLI, and the prompt arm already stands for Claude Code's method.
- **Model held fixed.** Claude Haiku 5.5 headless on the Max plan (`claude -p`), as in Experiment 1, so only the method differs.
- **Grading.** Each boundary's requirement list is the one its own audit produced: the maintainer's grounded `02_implement` requirements whose source reply is verbatim in the pre-compaction input. Each is graded kept, dropped or misstated against each replay's summary plus that arm's kept messages and the re-attached step instruction, with Experiment 1's grading pass and its second look. The list is extracted once per boundary, so all six summaries (the live one and five replays) are graded on the same denominator.
- **Reported, descriptively:** per summarizer, the share of disclosed requirements lost, with task-clustered intervals; the requirements another arm loses that pi keeps, and the reverse; and the live pi against the pi replay, **on first boundaries only**. Experiment 1's pi replay uses pi's first-compaction prompt, while later live boundaries use pi's update prompt, so only first boundaries separate the model from the method. First boundaries are also reported apart from later ones in general, because a later boundary's history already contains the live pi's earlier summary, so every arm inherits pi's earlier losses there.
- **At fork boundaries:** the replays run as soon as the boundary is saved, before its cards, since they are candidate sources. At ineligible boundaries they stay descriptive: no checks beyond retention, no arms.
- **Blinding:** replays read only pre-boundary history, so they can't leak continuation outcomes. Source choice is mechanical (the fixed order). Jacob sees a replay only on the card it supplies; the per-summarizer comparison stays unread until every card is labelled and every freeze is committed.
- **A structural point to watch** (an inference, not yet checked in data): the maintainer's replies reach the agent as `ask_user` tool results, not user messages. Codex's keep-user-messages rule and Antigravity's last-10-requests rule therefore don't protect them, unlike the user-given rules they protected in Experiment 1. Claude Code's prompt arm keeps nothing verbatim. pi and OpenCode keep a recent tail, which holds tool results.
- **Cost and time.** $0 billed on Max. At API prices, from Experiment 1's usage log (`data/raw/sprint/s1/usage/max_usage.jsonl`, median per call): $0.18–0.19 for each of the three full-history replays at ~175k tokens, $0.115 for OpenCode and $0.01 for pi (successful calls), plus about $0.5 to grade the six summaries. That is about $1.2 per boundary, or $7–22 for 6–18 boundaries.
- **Max is on the critical path, so there is a timed fallback.** The eligibility pass at every boundary, and the fork boundary's replays, grading and candidate drafts, all run on Max. If a Max call returns a usage-window error, or no answer within 10 minutes, that stage switches to the pinned OpenRouter Haiku route, booked under the pilot tag. Priced at about $0.05 per eligibility pass and about $1.5 per fork boundary (replays, grading and drafts), this is at most about $10 for the pilot; the fallback is capped at $12 within the $120. Replays at ineligible boundaries wait for Max. Replays take 15–35 s each (Experiment 1). These timings are estimates: the first real fork boundary is timed end to end, from the boundary to its card, before the others' timings are relied on.
- **Build:** about 2–3 agent-hours for the driver and the grading adapter, plus about 2–3 h for v0.4: an OpenCode carry-over builder, building any policy's carry-over from an external summary and keep rules (the arms then reuse `with_block`), the per-source candidate order, and tests. One astra code review covers both. New files except a small additive function in `compact.py`; Experiment 1's replay code is unchanged.

## Predictions (written 24 Oct, before the pilot's base runs; only the feasibility check's 8 simulator replies on kombu have run)

| Prediction | Prior | Basis |
| --- | --- | --- |
| ≥ 5 of 6 runs reach 150k inside `02_implement` | 60% | The chosen tasks' single-turn peaks are 210–242k; the review loop adds context; but a fresh context at `02_implement` and an early "ship it" could end some runs first |
| At the first boundary, fewer than 2 disclosures sit before pi's tail in ≥ 3 of 6 runs | 50% | DeepSeek reached 170k in 28–75 calls in Experiment 2; the first review comes only after the first commit |
| ≥ 4 of 6 runs reach an eligible boundary (any index) | 55% (from 50%; runs continue through ineligible boundaries) | About 7 maintainer messages per trial with GPT-5.5 agents (paper); DeepSeek likely needs more review passes; but a run may reach ship it first |
| The simulator discloses ≥ 3 requirements before the boundary, median over runs | 55% | About 7 user messages per trial (paper), some after the boundary |
| pi's summary drops at least one disclosed requirement at ≥ 4 of the boundaries | 55% | Experiment 1: summaries drop 11–16% of actionable items; mid-session decisions are lost most often |
| **K ≥ 4** | **35%** (set to 50% when replays became sources, then corrected after astra r2, before any base run: 50% left no room for the checks to fail after eligibility) | K ≥ 4 needs ≥ 4 eligible runs (55%) and then ≥ 4 accepted comparisons among them. Five summaries make retention nearly free at a fork boundary, so the conditional yield per eligible run rises from about 0.5 (pi alone) to about 0.7. But the main bottleneck, a correction already in code and so recoverable, doesn't depend on the summarizer. Subjective: 0.55 × about 0.65 ≈ 0.35 |
| K_live ≥ 4 (pi-sourced only) | 20% | The pi-only path: about 0.5 conditional yield |
| Most valid cases come from a non-pi summary | 60% | pi is tried first, but eligibility already puts the requirements outside pi's tail, so pi keeps them only in its summary; Claude Code's prompt keeps nothing verbatim, and Codex and Antigravity keep no tool results |
| Repair lowers the act rate in at least 3 of the K cases (any size) | 45% | A pre-pitch probe: deleting permission and plan sentences from DeepSeek's summaries cut unapproved actions by 23 points (Experiment 3 plan, Predictions); long tasks allow rereading |
| The simulator re-raises the dropped requirement in the matched-addition arm of both secondary cases | 65% | It inspects the commit and corrects one visible surface at a time |
| Agents re-ask the simulator about the item before acting, in ≥ 1 in 4 matched-addition continuations | 30% | The persona pushes decisions back to the agent, and agents rarely ask after a summary |
| **Offline replays:** at least one other summarizer drops a disclosed requirement that pi keeps, at one or more boundaries | 80% | Experiment 1 found loss rates that differ by method on the same histories, and the maintainer's replies arrive as tool results, which Codex's and Antigravity's keep rules don't protect |

## What each outcome changes

- **K ≥ 4: go.** K says SWE-Interact supplies usable repair comparisons in this pipeline; it doesn't establish yield under live pi alone (that is K_live) or under natively run harnesses. If most valid cases come from replays, Jacob decides whether Experiment 3 keeps the mixed setup. Switching to native harnesses needs a fresh yield check, and the pilot's mixed-setup cases are pooled only with expansion cases of the same configuration. Experiment 3's base runs come from SWE-Interact, from the rest of stage SPRINT-S3-Q2 ($270 minus the pilot's spend; any more needs a transfer from the sprint reserve, which Jacob approves above $50), finishing by Wed 28 Oct 23:59 ET. The size is provisional: K/6 comes from the 6 longest tasks and is optimistic for the 18 shorter ones left, so the first expansion batch is re-measured before the rest. 20–30 comparisons will need repeat runs on some tasks, so they are not 20–30 independent tasks, and intervals resample tasks. The pilot's valid cases count toward it.
- **K ≤ 3: fallback** to the chained pilot. This pilot's cases and Q1 counts are reported descriptively.
- **K ≥ 4 but the simulator's cost per valid case would break the reserve:** a cheaper simulator is compared on ten saved interaction states, blind, for grounded corrections, consistent requirements and disclosure behaviour; one configuration is frozen before expansion.

## Automatic rules and stop lines

- **Stub smoke (free):** one task through `01_plan` and `02_implement` with the deterministic stub and a forced 20k threshold; a boundary, saved and restored twice; the fork check passes; every boundary loads with `lossaudit.load`; `docker ps --filter label=emx.sandbox` is empty afterwards.
- **First real run** (one of the 6, protocol unchanged): simulator replies logged with billed input, cache and output tokens per tool round; the simulator's repo inspection reaches our container; spend recorded through `emx.spend`. It also measures simulator latency per reply. If the first run's simulator spend passes $2.50 (the upper projection per session), stop and re-project before the other 5.
- **Base run stops:** at its first eligible boundary; on ship it with none (zero yield); at $8 (agent plus simulator), 600 agent calls or 3 h. Infrastructure failures rerun once.
- **Later compactions need pi's update prompt.** The pilot replay has only pi's first-compaction prompt. pi's update prompt (`pimono_update_instructions.txt` in P44 and P38) is ported with its source hash, as the chained plan already requires, and the stub smoke forces two boundaries.
- **Continuation stops:** primary at its readout end; secondary at ship it, 300 calls from the fork or $6.
- **Primaries come first under the cap.** The secondary cases start only when every primary has finished and spend so far plus the secondary's $48 of caps stays under $120; otherwise only one secondary case runs.
- **Pilot stop:** $120 total, the pilot's own spend inside stage SPRINT-S3-Q2 (below). Sealed: continuation outputs are not read before their task's freeze record is committed. No candidate is replaced once any output of its boundary exists.
- **Fork failure** by noon ET on day 2 → fallback.

## Cost and cap

**Cap $120 API** (all LLM calls; no GPU).

**Where it books (main session, 24 Oct).** The pilot is Experiment 3's first step (Q2, repair), so every charge books to stage **SPRINT-S3-Q2** in `docs/sprint/caps.toml` (cap $270, unspent), not SPRINT-S2, which has about $27 left. No transfer is needed. The $120 pilot cap is enforced inside that stage:
- **Stage id:** the runner passes `exp="SPRINT-S3-Q2"` to every `emx.spend.record` and uses it as the stage for every `compaction.budget` reservation (base runs, simulator replies, continuations, any OpenRouter replay fallback). Max-plan calls get $0 ledger rows under the same id, as in the close-out.
- **Pilot tag:** every reservation and ledger row also carries the tag `s3q2-interact-pilot`. Before each reservation the runner sums that tag's spend plus its open reservations, and refuses any reservation that would take it past $120. `compaction.budget` still enforces the stage's $270 and the sprint-wide cap on top.
- **Per-run caps:** $8 a base run and $6 a secondary continuation, reserved up front and settled to the real charge.
- **Feasibility spend stays in S2:** the $0.33 of simulator measurements was booked to SPRINT-S2 before this decision.
- **Spending runs from the sprint checkout** (`.claude/worktrees/sprint-prep`), which `budget.py` requires.

Projection:

| Item | Count | Each | Total |
| --- | --- | --- | --- |
| Agent, base run to the eligible boundary, 1–3 segments of 150k (pilot telemetry: $0.11–0.26 per segment to 170k), plus pi's summaries (about $0.02 each) | 6 | $0.30–0.90 | $2–5 |
| Agent, primary continuations (≤ 60 calls; pilot arms $0.08–0.10 per ~23 calls) | 6 × 4 = 24 | about $0.25 | about $6 |
| Agent, secondary continuations (to 300 calls) | 2 × 4 = 8 | about $1 | about $8 |
| Simulator, base runs: 2 conversational replies (about $0.01) and 5–12 review replies ($0.05–0.20) each, measured on kombu | 6 | central $1.0, upper $2.42 | $6–15 |
| Simulator, secondary continuations: about 3–6 review replies each | 8 | central $0.6, upper $1.2 | $5–10 |
| Simulator, primary continuations: stop at the first `ask_user`, no reply | 24 | $0 | $0 |
| Offline replays, grading, eligibility passes and drafts, Haiku on Max (about $1.2 per boundary at API prices); OpenRouter fallback on the critical path only if Max fails (timed switch, capped at $12) | 6–18 boundaries | $0 (fallback up to $12) | $0 / $12 |
| Haiku drafts, loss audit, judge | — | Max plan | $0 |
| **Subtotal** (central / every upper assumption at once, including the fallback) | | | **$28 / $55** |
| **With 30% retries** | | | **about $37 / $72** |

The simulator figures come from 4 measured replies on one task (`logs/s2/swe_interact_feasibility.md` §5); one reply at 6 tool rounds near 60k tokens each could cost $1–2, and a 27-message outlier session $3–5. The $8 base-run cap covers both. The paper's $6.10–11.80 per multi-turn trial (Table 1, as read in `logs/s2/dataset_search.md`) is for frontier agents. With DeepSeek the agent is cheap, and the cap is about 1.7× the upper scenario. (The feasibility note's "$0.5–1.5 per session" understates its own upper assumptions: 2 × $0.01 + 12 × $0.20 = $2.42.)

**Wall-clock.** Base runs about 30–60 min each (agent plus simulator latency), **all 6 at once** (Jacob, 24 Oct): about 1 h, set by the slowest run and by runs that continue past ineligible boundaries. Docker has 24 GB and 18 CPUs; each container keeps the pilot's 2 CPU / 8 GB limit. Six 8 GB limits add to 48 GB, so they oversubscribe Docker's memory; that is acceptable only because limits are ceilings, not reservations. The runner logs `docker stats` memory per container every 3 minutes, and a run killed for memory ends `infra_oom`, counted as an infrastructure failure and rerun once. If two runs end `infra_oom`, the reruns go 3 at a time. Audit and drafts about 30 min; Jacob's cards about 30 min; primary continuations about 10 min each, 4–6 at a time, starting for each task as soon as its freeze is committed (sealed as above), which saves about 30–60 min over waiting for all base runs: about 1 h; secondary about 45 min each, 4 at a time: about 1.5 h. **Build:** about 8–10 agent-hours for the SWE-Interact integration (the `ask_user` tool mode, a step driver for `01_plan` then a fresh `02_implement`, the simulator in the boundary and arms, the readout stops, the route, grading the last implementation commit, tests and review; feasibility §3), plus about 4–6 h of harness work the chained plan needs too (repeated compaction with pi's update prompt, kept snapshots, loss-audit compatibility). Plus about 4–6 h for the offline replays and v0.4's candidate sources (their own section). About 16–22 agent-hours in all, 6–8 h of wall-clock with 2–3 sub-agents, since the replay unit runs in parallel with the rest. That is more than astra's half-day condition; the shared part is reused by the fallback either way. Simulator latency and safe concurrency are not measured yet; the first real run measures them.

**Jacob's time:** about 1 h: up to 6 cards at about 5 min, a blind read of judge-scored acts (about 15 min), and the go decision.

**Timeline (ET).** Day 1, Sat 24 Oct: build the integration and run the stub smoke. Day 2, Sun 25 Oct: fork check by noon; the 6 base runs; audit, cards and freezes; primary continuations; then the 2 secondary cases. K by 23:59 Sun. Expansion Mon–Wed if go; Thu 29 Oct analysis. (If the build slips, day 1 is Sun and K is due Mon 23:59.)

## Validity threats

- **The simulator heals losses.** Handled by the two readouts; the primary stops before any new reply.
- **Off-distribution content is not ours.** Nothing is planted; the disclosures come from the published persona and spec.
- **Agent harness.** Our loop and pi's compaction replace the paper's Codex, Claude Code and OpenCode agents; the simulator, persona and steps are theirs. Results speak for this configuration.
- **Selection by length** favours long tasks; K is reported for these 6.
- **Contamination.** DeepSWE tasks have been public since about June 2026; this affects both arms equally.
- **One run per task** gives K, a feasibility count, not an effect estimate.

## Review history

**astra r1** (`logs/reviews/20261024-s2-interact-plan-r1.md`, 24 Oct): "revise before go". The departure from the first-boundary rule was judged clean, and 2 a reasonable pragmatic threshold.

| Finding | Severity | Done in v0.2 |
| --- | --- | --- |
| Eligibility counted replies, not distinct requirements, and couldn't separate this compaction's losses from inherited ones | BLOCKING | Fresh requirements: distinct, grounded, verbatim in the pre-compaction input, outside the tail; first disappearance recorded; yield cost of 2 over 1 reported |
| Candidate order and the 6-card allocation contradicted each other; scheduling could decide K | BLOCKING | Committed task permutation; candidate order by source reply; one card per task, then round 2; no outputs read until all cards and freezes are done |
| First-boundary K isn't available | MINOR | Replaced by a restricted count and first-boundary candidate yield |
| Readout endpoint, secondary resumption and the control's definition | MINOR | Act observable before the primary endpoint; resumption per endpoint; 300 calls from the fork; control rationale frozen |
| Upper cost understated | MINOR | Central and upper scenarios ($37 / $56 with retries); compaction charges; a $2.50 trigger; primaries before secondaries |
| Provenance: verifier patches, "before any run", unsupported figures | MINOR | All fixed with sources |
| Expansion sizing and throughput | MINOR | Expansion provisional with a deadline; latency and concurrency measured in the first run; primaries start as freezes land |

Also added after the review, from the feasibility agent: check 6, consistency with the reference solution. Not re-reviewed.

**v0.4 (24 Oct, 15:29Z, Jacob):** GO with cap $120, and a new rule replacing "replays descriptive only": a task counts toward K if any summarizer's summary at its fork boundary drops a requirement that passes the checks. pi's live summary comes first, then Claude Code's prompt, Codex, OpenCode and Antigravity-style; one candidate per task; at most 6 cards; both arms start from the source's summary. K prior revised 35% → 50%; a source prediction added. Reviewed by astra r2 (`logs/reviews/20261024-s2-interact-plan-r2.md`): "revise before execution — the within-summary comparison is sound".

| Finding | Severity | Done in v0.4.1 |
| --- | --- | --- |
| Candidate qualification depended on an unfrozen draft-stage filter that could silently skip candidates | BLOCKING | Frozen prompt and outcome mapping; code-verifiable fails only exclude; unresolved and judgment-only fails go to Jacob; every candidate's disposition kept; "one accepted comparison per task" |
| The graded replay and the continued replay could differ (instruction handling, keep rules rebuilt separately); the pi replay uses the first-compaction prompt | BLOCKING | One canonical replay record (input map, kept indices, `post_messages`, frozen `summary_index`) used for both grading and arms, with per-policy fixtures; model-versus-method claim limited to first boundaries |
| Eligibility uses pi's tail for every source; OpenCode rationale wrong; K prior incoherent with the eligibility prior | MINOR | Labelled as a common sampling restriction; OpenCode corrected; K ≥ 4 reset to 35% with a stated argument, K_live ≥ 4 20%, eligibility 55% |
| OpenCode cost, the paid fallback missing from the total, Max on the critical path, serial `run_replays` | MINOR | $0.115; timed fallback for every critical-path stage, capped at $12; upper total $72; concurrent policies; first fork boundary timed end to end |
| Decision interpretation | MINOR | K_live and K_replay reported; native harnesses need a fresh yield check; pooling only within a configuration |

**v0.3 (24 Oct, Jacob):** the offline replays of five summarization policies at every boundary. Not re-reviewed: it is free, exploratory and offline, and doesn't change K, the arms, candidate selection or the cap. Its code gets its own astra review before it runs.

## Run log

`logs/SPRINT-S2-report.md`, a "SWE-Interact pilot" section (UTC, commands, seeds, spend). Before any follow-up run: what it tests, why, a one-line prediction and its cost; follow-ups are reported as exploratory.

## Build deviations (logged 24 Oct, from the build; shared notes ~19:45 UTC)

- **OpenCode-sourced arms:** the repair or matched block goes in a separate user message after the summary (above).
- **Recompaction in an arm:** pi's update prompt receives the source summary plus the block as the previous summary.
- **The $2.50 simulator gate:** applied per run, since all 6 base runs start together (Jacob, with Docker at 24 GB).
- **Negated acts:** scored per workspace state. A negated spec with a command matcher is unscorable and goes to Jacob's card.
- **The stub smoke:** used a 60k threshold.
- **The OpenRouter fallback for replays:** reports Haiku's pinned model id. The Max to OpenRouter fallback hasn't been exercised against a real Max outage.
- **Code review:** astra r1–r3 (the cap). The r3 fixes were tested but not re-reviewed (`logs/reviews/20261024-s2-interact-build-r{1,2,3}.md`).
