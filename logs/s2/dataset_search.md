# S2 dataset search: task sources whose governing state lives outside the environment

2026-10-24 · sub-agent of the S2 session · brief: `logs/s2/briefs/dataset-search.md` · $0 (web search, `gh api`, and reading local `data/raw/sprint/deepswe/trials.json`; no paid calls).

**Problem being solved.** Live single-task DeepSWE runs gave q = 2/24 usable losses. The task is fully specified up front, so the governing state is in the repo or re-attached files, and whatever the summary drops is either recoverable or already done (`logs/SPRINT-S2-report.md`, close-out entries 24 Oct). We want sources where requirements, corrections, scoped grants and decisions arrive **in user turns during the session**, sessions run well past 100k tokens, and a later act shows whether the agent still holds the item.

**Marks:** [V] read verbatim in the primary source (paper text, repo file, or our own computation); [S] fetch-tool or search summary only; [me] computed here from raw data. All the papers were read by web-fetch sub-agents. Most HTML fetches returned the first ~39k characters verbatim and only a summary after that, so appendix-level details are [S] unless marked otherwise.

## Ranked shortlist

### 1. SWE-Interact (Scale AI, arXiv 2606.30573, June 2026)

- **Links:** https://arxiv.org/abs/2606.30573 · https://github.com/scaleapi/SWE-Interact (Apache-2.0 [V via `gh api`]).
- **What it is:** 75 multi-turn tasks, 25 each from SWE-bench Pro, SWE Atlas Refactoring and **DeepSWE** [V]. The agent gets "a brief overview". A simulated maintainer holds the full task spec and "introduces additional requirements one at a time until all task instructions have been handed off", inspecting the repo before each reply [V paper; V repo `persona.md`].
  - The persona file is explicit [V]: "a brief, vague overview to start with … your real requirements are revealed slowly as you see the implementation", **one at a time**.
  - Its example corrections are exactly our item types [V]: "wait, don't touch test files", "i wanted those actually deleted, not wrapped", "i need `NewBufferPool`, not `NewPool`".
- **How it runs:**
  - Built on Harbor, as a multi-step task [V repo]: `01_plan` (agree a plan via the `ask_user` MCP tool and save it to `/tmp/plan.md`), `02_implement` (commit each pass, ask for review, iterate until "ship it"), `03_handoff`, `04_write_tests`, `05_test_handoff`.
  - The user simulator is a sidecar container (`docker-compose.yaml`: "holds the latent user goal and keeps its conversation state across task steps") that calls an OpenAI-compatible endpoint (`SIM_USER_MODEL`, default GPT-5.5 high) [V].
  - The DeepSWE tasks use the **same public ECR images we already pull**, e.g. `public.ecr.aws/d3j8x8q7/swe-bench-202605:…`, 2 CPU / 8 GB [V `task.toml`].
  - The README uses Modal sandboxes. Harbor's local Docker backend with this compose sidecar is not verified.
- **Cost per run:**
  - The paper's multi-turn cost per trial [V Table 1]: GPT 5.5 $9.84, Opus 4.8 $11.80, Gemini 3.5 Flash $7.40, Sonnet 4.6 $6.10, against single-turn $2.55–5.09, so roughly 2–3.5× single-turn. Whether that includes the simulator is not stated.
  - **Estimate** for our cheap models: DeepSWE's single-turn median for DeepSeek V4 Flash is $0.09, so about $0.2–0.4 for the agent. The simulator would dominate at GPT-5.5 prices (~7 replies × up to 6 tool rounds each; not measured). A cheaper simulator changes fidelity and would have to be checked.
- **Session length:**
  - 424.8 agent steps per trial for GPT 5.5 and 180.9 for Opus 4.8; "about 7 user messages per trial"; longest trial 27 user messages and over 1,000 steps [V]. Peak context is not reported.
  - **Our proxy [me]:** single-turn DeepSWE peaks (no compaction) for SWE-Interact's 25 DeepSWE tasks have a median of task medians of 191k for both DeepSeek V4 Flash max and luna 5.6 max. 9 of 25 are at or above 200k on each. GLM 5.3 Flash is at 121k.
  - 7 of the 25 are in our 45 longest: boa-hierarchical-evaluation-cancellation, clack-async-autocomplete-options, fastapi-implicit-head-options, helm-array-merge-strategies, sqlite-utils-safe-import-checkpoints, testem-bail-on-test-failure, vitest-duration-sharding.
  - The multi-turn version adds plan and revision loops on top, so one or more compactions at a 150–200k threshold is likely. That is an inference until the pilot measures it.
- **Where the non-environment state lives:** in the maintainer's messages during `02_implement`. These are corrections, reversals and API details revealed after the plan, and they are not in `/tmp/plan.md`. The plan file is an environment-held copy of plan-level state only.
- **Fit:**
  - **Q (loss rate):** high. The corrections are what our offline audit found summaries drop (requirements inside user messages, decisions).
  - **Q (repair):** good. The task's verifier tests the full spec, and each correction maps to a checkable surface (a name, a deleted type, test files untouched).
  - **Failure modes:** the paper's audit labels 287 failures with "forgotten requirement" and "regression" among its five categories [V names; counts not read].
- **Main risks:**
  - **(a) The simulator is itself a repair channel.** It inspects the repo before each reply and re-states anything it sees violated, which heals losses. The scorable window is from the boundary to the next user review, or the count of re-corrections needed. Alternatively, freeze the simulator in the rerun arms.
  - **(b) Forking** needs the sidecar's conversation state snapshotted with the container. It logs to `/tmp/user-server-logs/*.jsonl` [V `server.py`], but restoring from those logs is unverified.
  - **(c) Simulator cost and model choice.**
  - **(d) Contamination:** DeepSWE tasks have been public since about June 2026.
- **Read:** paper [V] for §1–3 and Table 1 (rest [S]); repo README, `task.toml`, step instructions, compose file, persona and `server.py` greps [V].

### 2. SWE-Together (arXiv 2606.29957, June 2026)

- **Links:** https://arxiv.org/abs/2606.29957 · https://github.com/Togetherbench/SWE-Together (Apache-2.0 [S, read by the fetch agent from the repo LICENSE]) · https://huggingface.co/datasets/yfwu/SWE-Together (109 task specs).
- **What it is:** 109 tasks built from 11,260 **real** recorded user–agent sessions: SWE-chat 48, DataClaw 29, pi-staging 23, Hyperswitch 9 [V].
  - A reactive simulator holds "the user's objective, constraints, and intervention conditions grounded in the original follow-up messages" [V].
  - Each turn it chooses no-op, question, redirect, new-requirement or check-external [V].
- **How it runs:**
  - opencode is the main harness; claude-code, codex and mini-swe-agent are also supported, in E2B, Docker or enroot sandboxes [S README]. The simulator defaults to Claude Sonnet 5.5 on Bedrock [S README].
  - Scoring: an agentic rubric judge gives per-goal met/not met (pass at a weighted score of at least 0.85), plus deterministic verifiers [V].
- **Cost per run:** not stated in dollars. Output-plus-reasoning tokens per task are 30–74k; wall-clock 11–39 min per task [V Table 2].
- **Session length:** **the weak point.** The ReCAP paper ran SWE-Together in OpenCode with Qwen3-Coder and reports a mean context per call of 28.5k [V, arXiv 2609.40118]. Sessions are probably well under 200k, so compaction would need a low threshold or chaining.
- **Where the non-environment state lives:** the users' real follow-up messages (redirects, new requirements, constraints). This is the most realistic source of mid-session state found.
- **Fit:** high realism and per-goal scoring, but short sessions, so the boundaries would be configured early.
- **Main risks:** session length; the rubric judge is an LLM; the same simulator-repair issue as #1.

### 3. LHAW (Scale AI, arXiv 2602.10525)

- **Links:** https://arxiv.org/abs/2602.10525 · https://github.com/scaleapi/lhaw (MIT [V via `gh api`]) · https://huggingface.co/datasets/ScaleAI/lhaw.
- **What it is:** 285 underspecified variants of TheAgentCompany (85), SWE-Bench Pro (100) and MCP-Atlas (100) [V].
  - An `ask_user` MCP simulator holds "the original task specification, ground truth for removed segments, and a response policy", and answers only about the removed segments [V].
  - Segments are labelled outcome-critical, divergent or benign [V].
- **How it runs:** Docker task images of the parent benchmarks, with the simulator as an MCP server (GPT-4.1 by default [S]).
- **Cost:** not stated.
- **Session length:** not stated [V absent]. SWE-Bench Pro runs are long-ish, but this is unmeasured.
- **Where the non-environment state lives:** the simulator's answers, held only in the agent's history. If a summary drops an answer, the agent must re-ask or guess, and both are observable acts.
- **Fit:** good for the repair question, because a dropped answer to an outcome-critical segment is scorable by the parent verifier. The loss rate depends on how often agents ask (the paper reports Ask%).
- **Main risks:** sessions may be short; a re-ask is an easy natural recovery; the simulator needs an API model.

### 4. SWE-Journey (Tencent Hy et al., arXiv 2610.11559, Oct 2026)

- **Link:** https://arxiv.org/html/2610.11559v1. **No code or data release found** [V absent in the text read; S says "we open-source our full pipeline"; a search found no repo].
- **What it is:** 30 large feature instances (10.2 files and 654 added lines each; 10,498 test nodes) with persona-driven simulated users [V].
  - The personas differ in how they disclose. Non-coder is "Pivoting" and "may confidently suggest an incorrect direction and later revise the request". Software Architect is "Evolving" [V].
- **How it runs:** mini-swe-agent with a reply-to-user action, "up to 250 autonomous command steps" per user turn, on a "persistent Docker container". The simulator is GLM-5.2 holding a hidden subtask checklist and hidden tests [V].
- **Cost per session:** DeepSeek-V4-Flash Non-coder $0.46; Opus 5 $28–51 [V Table 11].
- **Session length:**
  - The turn limit is 3× the subtask count; 6–17 turns per session in the per-persona tables [V]. Tokens are not given.
  - Its harness **deletes whole turn groups above 300k characters** [V], so the authors already hit context limits.
- **Where the non-environment state lives:** pivots and revisions in user turns (reversals especially).
- **Fit:** best design on paper for long sessions with reversals, and the cheapest per session for DeepSeek.
- **Main risk:** **not released**, so it would have to be rebuilt.

### 5. OSWorld 2.0 (XLang, arXiv 2606.29537)

- **Link:** https://osworld-v2.xlang.ai. Environment, 108 tasks, self-hosted sites and **rollout trajectories released** [V]. Licence not found.
- **What it is:** long-horizon computer use: an average of 318 tool calls with Opus 4.7 at maximum thinking [V]. It "injects mid-task messages so the environment changes under the agent" and exposes "a simulated user with bounded knowledge" [V].
  - Only 10 tasks are tagged Dynamic Environment and 6 Proactive Interaction [S]. Example Task 035 has a TeamChat message overriding an earlier rule [S].
  - Agents "drop stated constraints, miss information that arrives mid-task" [V; no count given].
- **How it runs and cost:** desktop plus 31 self-hosted web services on AWS CPU instances [S]; $25–76 per task for frontier models and $2.4–6.6 for cheap ones [S Table 3].
- **Session length:** long by step count; context not reported.
- **Fit:** the released trajectories could serve the offline audit, but only about 16 tasks carry mid-task state.
- **Main risks:** cost, infrastructure, and it is not coding.

### 6. SlopCodeBench, run as one continuous session (arXiv 2603.24755, NeurIPS 2026)

- **Link:** https://arxiv.org/abs/2603.24755. Code URL and licence not checked.
- **What it is:** 36 problems and 196 checkpoints (v1: 20 and 93) of evolving specifications. As published, the agent gets "only the new requirements and its own prior code, not the earlier conversation" [S].
- **The idea for us:** deliver the checkpoints as successive user turns in **one** session instead. Earlier checkpoints' design constraints then live only in history, the session grows long, and later checkpoints' tests show whether earlier requirements still hold.
- **Fit and risks:** an adaptation, not the published protocol. Earlier requirements are mostly implemented in the repo by the time compaction fires, which is the same recoverability problem as DeepSWE. Unverified [S only].

### 7. ClawArena, as used by TRACE (arXiv 2604.04202; 2606.13174)

- **Links:** https://github.com/aiming-lab/ClawArena (MIT [V via `gh api`]) · https://github.com/YujunZhou/TRACE_exp (MIT [V]).
- **What it is:** 12 multi-turn scenarios, 337 rounds, 45 dynamic updates. "user preferences surface through corrections" [V abstract]. TRACE uses 62 scenario templates "with an objective checker and a preference overlay", and checks violations of a preference a user corrected earlier [V].
- **Fit:** directly about corrected preferences being kept, and scored per preference.
- **Main risks:** rounds are short (about 42 s each [S]), session tokens aren't reported, and TRACE uses a two-user-turn budget [V]. Probably too short without chaining.

### 8. τ²-bench (Sierra; MIT, active)

- **Link:** https://github.com/sierra-research/tau2-bench.
- **What it is:** a user simulator plus a policy (confirm before writing). Already assessed in the proposal: about 5.7k tokens per task, and the reward doesn't check confirmation (arXiv 2506.07982 §3.3 [V, earlier search]).
- **Fit:** a cheap replication check only, as the proposal already says.

## Checked and set aside

- **Lost in Conversation** (microsoft/lost_in_conversation, MIT): short single tasks whose requirements are sharded across turns. ReCAP (arXiv 2609.40118) uses it for compaction with configured selection, not long sessions [V].
  - Its sharding method is the template for what SWE-Interact does inside long tasks.
  - The ReCAP page contained text addressed to the fetching agent (asking for formatting changes); it was ignored.
- **SWE-Touch** (MIT; 25 DeepSWE tasks at 500 steps): the user's mid-session code edits are deliberately wrong, so the state is in the repo and the question is pushback, not memory [S].
- **"When Consent Outlives Context"** (arXiv 2609.33910): stored approval rules replayed by an attacker, capped at 10 steps, no compaction, no code found [V/S].
- **MemoryAgentBench and LongMemEval:** QA over injected history, with no agent act to score.
- **TheAgentCompany:** simulated colleagues hold information in RocketChat (79 of 175 tasks [S]), but trajectory lengths weren't found, and LHAW (#3) already wraps 13 of its tasks with an `ask_user` simulator.
- **Ambig-SWE, Dialogue SWE-Bench and TOM-SWE:** SWE-bench-Verified-length tasks (short); superseded by #1–3.
- **SWE-Gym, SWE-smith and released SFT trajectory sets:** short, and no compactions (see the proposal).

## Top recommendation: SWE-Interact, starting with its 25 DeepSWE tasks

SWE-Interact fixes the exact reason the live pilot found few usable losses. The task arrives as a vague overview, and the requirements that govern later work (corrections, reversals, "don't touch tests", exact API names) arrive **one at a time in user messages after the plan is written**. That state is not in the repo until acted on, and not in `/tmp/plan.md` at all.

It also drops into our setup:

- 25 of its tasks are DeepSWE tasks on the same public images and verifiers we already run, 7 of them among our 45 longest.
- Our own single-turn peak data put those tasks at a median of about 191k before the multi-turn revision loops are added [me].
- It is Apache-2.0, built on Harbor with a separable simulator sidecar.
- Its authors already report "forgotten requirement" and "regression" as failure classes.

Two things must be settled in a small pilot before any bulk run:

1. **Simulator repair.** The simulator re-states what it sees violated. Score the window from the compaction boundary to the next review (or count re-corrections), or replay the simulator's messages fixed in the rerun arms.
2. **Snapshotting.** Fork the sidecar's conversation state together with the container, so the counterfactual arms resume from the same point.

Also price the simulator: GPT-5.5 by default; a cheaper simulator needs a fidelity check. Our planted consent rules could be added to the persona's task block, so that the simulator delivers them naturally mid-session.

**Second choice:** SWE-Together, for realism (real users' follow-ups, per-goal rubric), at the cost of short sessions and configured early compaction.

Sources: [SWE-Interact](https://arxiv.org/abs/2606.30573), [SWE-Interact repo](https://github.com/scaleapi/SWE-Interact), [SWE-Together](https://arxiv.org/abs/2606.29957), [LHAW](https://arxiv.org/abs/2602.10525), [SWE-Journey](https://arxiv.org/html/2610.11559v1), [OSWorld 2.0](https://arxiv.org/html/2606.29537), [SlopCodeBench](https://arxiv.org/abs/2603.24755), [ClawArena](https://arxiv.org/abs/2604.04202), [TRACE](https://arxiv.org/abs/2606.13174), [τ²-bench](https://arxiv.org/pdf/2506.07982), [Lost in Conversation](https://arxiv.org/abs/2505.06120), [ReCAP](https://arxiv.org/html/2609.40118v1), [SWE-Touch](https://arxiv.org/abs/2608.02499), [When Consent Outlives Context](https://arxiv.org/abs/2609.33910), [MemoryAgentBench](https://arxiv.org/abs/2507.05257).
