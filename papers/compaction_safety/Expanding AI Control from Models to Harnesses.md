# Expanding AI Control from Models to Harnesses

- **Authors:** "fastfedora" (LessWrong username; no real name on the post)
- **Date:** 2026-07-15 (LessWrong postedAt 2026-07-15T16:54Z)
- **URL:** https://www.lesswrong.com/posts/PbATxkGs9N8JrJsQt/expanding-ai-control-from-models-to-harnesses
- **Captured:** 2026-10-26, for checking J's compaction-safety literature review.
- **What was read:** Full body (5,931 words) read via the LessWrong GraphQL API. Captured here: the opening, the broken-assumptions list, the compaction sections, the rogue compaction/refinement agent section, the hooks-realism note and the conclusion, verbatim. Deliberately not captured: the step-by-step Claude Code exploit walkthrough ("Vulnerability Research on Claude Code"). No experiments with numbers; this is a threat-model essay with one demonstrated exploit.

## Verbatim: opening

Most AI control research such as [LinuxArena](https://www.linuxarena.ai/) and [Ctrl-Z](https://arxiv.org/abs/2504.10374) only gives the red team basic agents which only have access to tools. Yet in 2026, usage of AI within frontier labs has moved to agent harnesses that have access to skills, memory, subagents, external services, compaction and more.

At the same time, Claude Code and Codex have both implemented their own version of both action-based and source code monitoring.

In such a world, the threat vectors have changed. While it may remain important to do theoretical research on improving protocols and monitors, AI control research should expand to include:

*   **Vulnerability Research**  
    Identify specific vulnerabilities that can be patched plus architectural weaknesses in production control protocols.
*   **Harness Evaluations**  

## Verbatim: broken assumptions


These features often break some of the core assumptions of AI control protocols used in research:

*   Monitoring is fixed—local settings can change how agents are monitored.
*   Tools don’t have hidden exploits—agents can now write their own tools.
*   Agents run stateless—memory and skills allow agents to persist knowledge.
*   Trajectories contain everything—compaction and subagents create altered or entirely clean trajectories.

## Verbatim: Compaction

### **Compaction**

Compaction frees up context by summarizing, filtering, and archiving messages. This can include tool calls and their results plus both assistant and user messages.

For Claude Code and Codex, only active context is passed to the auto mode monitor. After compaction, incriminating messages may no longer exist in the active context, and could result in an auto-mode monitor allowing an action it otherwise would not.

That said, the likelihood of an agent using this for a successful attack is low. The action under review is always passed to the auto-mode monitor. So, only cases where the context itself drives the denial could be subverted by compaction.

#### **Compaction Awareness & Control**

If an agent wanted to execute such an attack, it may have both compaction awareness and compaction control.

Codex, for example, provides tools for an agent to either get the remaining context or request a new context window. By monitoring when the remaining context increases, the agent could detect compaction events. Likewise, requesting a new context window may allow it to eliminate any incriminating messages by triggering a compaction or a fresh clean context.

Even if not driven by the agent itself, compaction could change the results of a control eval. Trajectory monitors tested within agent harnesses may need to be written with compaction awareness and/or align the monitor’s compaction strategy with that of the agent harness.

In Codex, for example, remote compaction can encrypt agent messages, tool calls and their results. This encrypted trajectory is not provided to the monitor.

Control evals that explore the impact of dropping agent messages and tool call results on monitor scoring could be valuable, as well as monitor-aware compaction strategies that preserve the most suspicious agent actions.

## Verbatim: Rogue refinement agents

Control research has investigated collusion between an agent and its monitor as well as an agent jailbreaking its monitor to rate attacks as not suspicious.

Compaction and refinement agents introduce new attack vectors. While such agents are usually sandboxed with limited affordances, the outputs these agents create could be used in attacks.

A compaction agent could collude with the main agent to erase suspicious messages or actions. A refinement agent could collude with the main agent to write or rewrite skills to enable an attack, or embed an attack by itself in a skill that the main agent unwittingly executes.

The backstop to attacks by such agents are the sandboxing and control protocols applied to actions the main agent takes; but as shown in the vulnerability research section, these can sometimes be subverted.

## Verbatim: Hooks add realism

#### **Hooks Add Realism**

Hooks ensure that monitors and protocols are seeing exactly what they would see in production—gaps included.

For instance, if compaction has pruned the active context or a subagent is running on its own trajectory, the payload reflects that. So you can evaluate monitors and protocols on realistic inputs rather than idealized transcripts.

## Verbatim: auto mode provides real protection (excerpt)

To do this type of research requires getting certified access via programs like Anthropics’ [Cyber Verification Program](https://support.claude.com/en/articles/14604842-real-time-cyber-safeguards-on-claude) (CVP) or OpenAI’s [Trusted Access for Cyber](https://openai.com/index/trusted-access-for-cyber/) program, or joining an organization like [Redwood Research](https://www.redwoodresearch.org/) or [Apollo Research](https://apolloresearch.ai/) which has special agreements with the frontier labs for such access.

If you are considering applying to one of these programs, or joining an organization that has such access, start implementing [physical security practices](https://www.cisa.gov/resources-tools/training/protect-physical-security-your-digital-devices) today; it takes time to upgrade your physical security and change your behaviors.

Auto Mode Provides Real Protection
----------------------------------

