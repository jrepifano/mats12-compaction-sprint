# Red-Teaming Auto Mode - Improving Blocking Classifiers Against Malign Coding Agents

- **Authors:** Alex Remedios*, Simon Storf* (Anthropic Fellows Program; *primary, order randomised), Fabien Roger, John Hughes (Anthropic)
- **Date:** Blog 2026-10-22; arXiv 2609.19587 submitted 2026-09-17 (v2 dated 27 Sep 2026)
- **URL:** https://alignment.anthropic.com/2026/red-teaming-auto-mode/ (paper https://arxiv.org/abs/2609.19587; code https://github.com/safety-research/red-teaming-auto-mode)
- **Captured:** 2026-10-26, for checking J's compaction-safety literature review.
- **What was read:** Blog post read in full (3,290 words, HTML to text). Paper PDF (arXiv v2, 10.9k words) read via `pdftotext -layout`: abstract, §2 related work excerpt, Fig. 2-3, §4.7 (multi-context attacks, Fig. 6-7), Appendix F.2 (simulated eval, Fig. 17); the rest grepped. Heatmap numbers come from the paper's text layer. Deliberately not captured: the worked prompt-injection and execute-then-write mechanics.

## Verbatim: tl;dr (blog)

tl;dr
We red-team production blocking monitors (Auto Mode in Claude Code, Guardian in OpenAI’s Codex) with an adversarial agent in an experimental setting with the objective of improving the safety of coding agents. Our most effective attack strategies bypass the monitor using agent-generated prompt injection against the monitor, multi-agent attacks, or malicious compaction. We attempt to improve the robustness of auto-mode by improving tool coverage, transcript formatting and using an agentic monitor stage. We find that robustly preventing multi-context attacks at an acceptable cost remains an open problem.

## Verbatim: the attacks (blog)

We study four attack types. By default we use Opus 5 as the attacking model. We also vary the affordances we give to the attacker (see the paper’s appendix), but by default we tell the attacker how the monitor works and give a high level description of the attack.

- Monitor injection. Agent-generated content (e.g. code, comments, tool arguments) fools the monitor’s judgement into approving a harmful action.

- Multi-context attack. The attack spans contexts the monitor sees only in isolation: separate sessions coordinating via a shared workspace (multi-agent attack), or a single session whose compaction hides earlier attack steps (compaction attack).

- Coverage gap. The monitor cannot intervene on parts of the agent’s action space, or certain information is invisible to it.

- Policy failure. The prompted policy omits the harmful behaviour, or the monitor fails to apply the policy correctly.

## Verbatim: the monitors (blog table, flattened)

We study the production variants (auto mode, guardian, guardian strict) and our experimental auto mode variants.

Monitor
Stages
Model
cwd edits
Cost
Description

auto mode
S1→S2
Sonnet 5

*
Claude Code’s production default.

guardian
agent
GPT 5.6 Luna

*
An approximation of Codex’s production default.

guardian strict
agent
GPT 5.6 Luna
yes
**
Guardian + monitors all edits.

auto mode ++
S1→S2→agent
Opus 4.8
yes
**
Auto mode with all-edit monitoring, bug fixes, anti-injection and compaction monitoring.

auto mode strict
agent
Opus 4.8
yes
***
The agentic stage of auto mode ++ alone (without S1 or S2).

cwd edits marks monitors that also review edit tool calls inside the agent’s working directory, rather than letting them run unmonitored. Note that reads are always automatically allowed. Stages details the LLM classifiers an action passes through: a one-token pre-screen classifier (S1), a reasoning classifier (S2), and a tool-calling agent that can read files and query the environment. An action is allowed if any stage judges it to be harmless. Stages are designed to only escalate unclear cases to the next stage, so most actions are cleared by the pre-screen and never reach the more expensive later stages. Cost is indicative and bundles latency, FPR and dollar cost, and is quantified in the results.

## Verbatim: methods (blog)

Our red-team evaluations fall into three modes, ordered roughly by increasing cost and fidelity.

Analytical evaluation looks for discrete vulnerabilities. Reading the monitor’s prompts, code, and integration with the surrounding system directly yields attack hypotheses. Unmonitored tool calls, injection channels, unhandled prompt edge-cases, and points where agent output is promoted to user intent are examples of such vulnerabilities.

Replay-based evaluation scores existing transcripts against a monitor. In a loose analogy with reinforcement learning, replay is off-policy: it is agnostic to the policy that produced a transcript. This makes replay cheap and lets us reuse transcripts from prior work or optimise certain parts of the transcript freely, but it does not capture the attacker vs. defender interaction.

- Benign replay: we use a cross-section of 4,000 actions from the SWE-chat dataset to measure deployment metrics such as false-positive rate.

- Malign replay: attack transcripts, used to score and optimise attacks against a monitor without running an agent.

Rollout-based evaluation runs a red-team agent (primarily Opus 5) live against the monitor. We use the agent’s system prompt to steer it toward an attack category and to grant affordances such as knowledge of the monitor formatting. We provide these affordances to understand monitor robustness under worst-case scenarios where the agent is working inside a frontier lab and has access to the monitor’s code. Rollout is the on-policy counterpart to replay: the trajectory is generated by the attacker policy under test as it interacts with the monitor, so rollout success rates are our most credible estimates of real risk.

- Arena: A Claude Code agent which acts in Docker-based environments (LinuxArena, BashArena, and custom tasks) with side tasks scored by deterministic checkers. It uses a replaced system prompt and hooks that route each action through the monitor before it runs. Compared to simulation below, arenas enable long evaluation trajectories with deterministic environment outputs.

- Simulation: An evaluation featuring bash, write, and edit tools, where tool outputs are generated by an LLM dungeon master (DM). The DM is prompted with a scene description and determines when the side-task is complete. This approach allows the simulation of complex weight exfiltration and rogue deployment environments without building infrastructure.

## Verbatim: what the monitor sees (paper Fig. 2, text layer)

Fig. 2 labels: "monitor sees: user messages, tool calls"; "hidden: ... the agent's thinking"; monitor is "a weaker model than the attacker"; outcome "blocks incurred n of 20". Blog: production Auto Mode "omits tool results from the transcript it reviews"; auto mode ++ adds them.

## Verbatim: Figure 3 heatmap (paper text layer; ASR, each cell >= 60 rollouts)

```
                    monitor injection      94%             95%            95%              0%             2%



simulation arena
                   multi-agent attack     100%             37%            37%             23%             7%
                    monitor injection      55%             70%            57%              4%             9%
                   compaction attack       80%             98%            99%             29%             4%
                       coverage gap        43%             72%            31%              2%             0%
                        policy failure     54%             65%            62%              6%             0%
                               mean        65%            72%             59%              9%             3%
                                         auto mode       guardian        guardian      auto mode       auto mode
                                                                          strict          ++             strict
                                                       production                             experimental
   Figure 3: Attack success rate falls by over 50 percentage points when we use our new Auto Mode ++
   monitor. The bottom row is the mean over the four attack types of Table 1, averaging arena and simulation in
   cases where both measure a type of attack. Each cell is the mean of at least 60 rollouts.

```
Row grouping checked against the rendered page image: **arena** rows = monitor injection (94/95/95/0/2%) and multi-agent attack (100/37/37/23/7%); **simulation** rows = monitor injection (55/70/57/4/9%), compaction attack (80/98/99/29/4%), coverage gap, policy failure. Columns: auto mode, guardian, guardian strict (production); auto mode ++, auto mode strict (experimental). So the compaction attack was measured only in the Inspect-based simulation with an LLM-simulated environment and an agent-triggered compaction tool, not in real Claude Code.

## Verbatim: unmonitored compaction summaries (blog)

The most serious vulnerabilities were unmonitored channels: compaction summaries were not monitored, so an agent could fabricate facts about the environment or the user’s intent and have them treated as trusted context after the compaction. The multi-agent workflow tool was similar: workflow creation and agent-to-agent handoffs let an agent inject instructions into messages that a subsequent agent reads as user authorisation.

## Verbatim: multi-context attacks (blog)

The monitor evaluates a single context, but agent runs often span many: a session is compacted when its context fills up, and long tasks are often split across agents. An attacker can therefore spread a side-task so that no single context contains a blockable action. We illustrate both attack vectors in the figure above.

Compaction. When an agent’s context window fills up to the model’s context limit, it performs what is called compaction. It generates a summary of the conversation so far and this summary replaces the full history in its context. The agent then continues the task working only from that summary. An attacker can complete a step of the side-task, trigger a compaction, and describe that step in the summary as benign or as work still to be done. Over several compactions the agent writes a script, stages data, and starts processes, each in a context that no longer contains the earlier steps (figure below). We added a check that flags summaries which misstate user intent or recommend harmful next steps. Monitors with this check (auto mode ++ and auto mode strict) catch many more of these attacks than auto mode, but the attack still succeeds at a substantial rate.

Multi-agent. The same attack works across sessions when agents share a workspace and are monitored separately. Each agent moves the workspace a step closer to the unsafe state, and a final agent completes the side-task. We assume agents share the same end goal and tell each agent in its system prompt that it shares this goal with the others, but we do not tell them how they should achieve it.

Increasing the number of separate contexts can greatly increase attack success rates. Left: scaling the number of agents in a single persistent workspace can substantially increase ASR. Right: ASR also scales with the number of times an agent compacted.

## Verbatim: Figure 7 legend values (paper text layer)

Multi-agent panel (x: number of agents, up to ~20): "attacker Opus 5 88% / Opus 4.7 6%". Compaction panel (x: malicious compactions, 0-20): "attacker Opus 5 85% / Opus 4.8 60%". Caption: "Increasing the number of separate contexts can greatly increase attack success rates ... ASR also scales with the number of times an agent compacted." Which monitor these curves use is not stated in the text layer read.

## Verbatim: simulated capability eval and its compaction tool (paper App. F.2)

```

This is the source of ASR scores - from agents that are in a harness which has constraints more
similar to a Coding agent: immutable action history, a bash tool, no ability to probe the monitor.
The simulated capability eval (Figure 16) uses an Inspect-based agent loop instead of a production
coding agent scaffold. This allows us to reconfigure the tooling, enabling (a) a dungeon master
which replaces the bash interpreter with an LLM prompted by the brief’s scene, and (b) a compaction
tool, to simplify the process of an agent triggering a compaction.




                              Figure 16: Simulated capability eval architecture.


                                                      20
Preprint




Together these features allow the flexible evaluation of red-team agents in a variety of infrastructure
environments, and for us to understand the implications of a model that gains the ability to use
compactions to confuse the monitor whilst mitigating the erasure of its own memory.
To accommodate the chance of the agent identifying the inner workings of the monitor, we run the
simulated capability eval across 2 configurations - (a) hidden, where it receives no intelligence on
the monitor, and (b) visible where the monitor’s code, prompts, and settings are visible to the agent.
Visible samples have a significantly higher ASR due to lack of obscurity of the monitor’s weaknesses
(Figure 17).
Finally, the simulated capability eval has some limitations and caveats. The first is that the com-
paction action is a simplified mechanism which increases the likelihood that an agent can take ac-
tions and compact the conversation in a complementary fashion. In real coding agents, compaction
currently works as a separate model call which is given the whole conversation history so far. This
makes it likely that the model can perform similar inferences when compacting via a tool call vs in
a standalone model call, but our approach somewhat risks over-elicitation. The second is that the
approach of using an LLM Dungeon Master (DM) to simulate the tool results in the eval is experi-
mental. We find that less capable models are more lenient when awarding side-task success to the
red-team. We mitigate the risk of spurious results by using Opus 5 (xhigh reasoning) as the DM,
```

```
brief that we run across both a Docker sandbox and the Dungeon Master.

                           monitor hidden (310 episodes per monitor)                  monitor shown (310 episodes per monitor)
         coverage gap         26%          4%          0%        46%        24%          84%          0%          0%        98%        38%


      monitor injection        8%          4%          2%        50%        38%          88%          4%         16%        90%        76%


          policy failure      28%          5%          0%        40%        44%          70%          6%          0%        90%        79%


  compaction attack           73%         20%          0%        97%        98%          97%         37%          7%        98%        100%


 simulation max ASR           73%         20%         2%         97%        98%          97%         37%         16%        98%       100%
                            auto mode   auto mode   auto mode   guardian   guardian    auto mode   auto mode   auto mode   guardian   guardian
                                           ++         strict                strict                    ++         strict                strict
```

## Verbatim: related-work claim (paper §2)

per-response classifiers cannot contain information that leaks across such a sequence of smaller
requests. Makins et al. (2026) and Hills et al. (2026) apply decomposition to agents. They spread
an attack across sequential sessions or pull requests on a shared codebase, evading a monitor that
reviews each commit. We, by contrast study the same fragmentation against production monitors
that must block each action in real time, and extend it to compaction summaries, which to our
knowledge have not previously been studied as attack surfaces.

## Verbatim: limitations and discussion (blog)

We assume we can trust the monitor. We treat the monitor as trusted. i.e. we do not study the possibility of the monitor colluding with the attacker (Greenblatt et al., 2023), even though some of our monitor models are close to the attacker in capability and lineage (e.g. for auto-mode ++ we monitor an Opus 5 attacker with an Opus 4.8 monitor). While with current models this proximity is unlikely to result in intentional collusion, such collusion would be more likely with the potentially more coherently misaligned and capable models that may be trained in the future (Carlsmith, 2023; Meinke et al., 2025).

We do not red-team sandboxing. We focus on red-teaming the language model classifiers, and do not red-team sandboxing features (most notably from Guardian) in our evaluations.

## Discussion

We show that production blocking monitors as of September 2026 would not be robust to a persistent, adversarial agent. Against an Opus 5 attacker given a high-level strategy, both Auto Mode and Guardian are bypassed through injection of forged transcript turns, unmonitored edits that change what an already-approved command will run, and attacks spread across compactions and sessions so that no single context contains a blockable action. We typically grant the attacker knowledge of the monitor. Mitigating the threat from adversarial agents is a high priority follow-up to this work.

Most of the vulnerabilities we found have relatively cheap fixes. Framing and escaping the transcript mitigate injection attacks, and monitoring cwd edits prevents execute-then-write attacks.

Our best defences against multi-context attacks are expensive. Defending against them reliably requires the monitor to see and gather context across boundaries, which is in direct tension with cost considerations such as latency. Agentic monitors that probe the transcript and environment perform best against these attacks, but they may be too slow for interactive use unless faster stages are placed in front of them, and those faster stages reduce recall.
