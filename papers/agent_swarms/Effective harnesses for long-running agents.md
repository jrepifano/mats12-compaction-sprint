# Effective harnesses for long-running agents

- Authors: Justin Young (Anthropic Engineering; "with thanks to several Anthropic colleagues" per fetch summary)
- Date: 2025-11-26
- URL: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Captured 2026-10-13 for P44
- **Reading depth:** full page fetched with curl and converted to text; read verbatim: introduction, "The long-running agent problem" failure modes, initializer/coding agent setup, feature list, incremental progress, testing, the failure-mode table and future work. Not read: the code/tool-call transcript example and the acknowledgements in detail. No quantitative results: the post is qualitative.

## Verbatim: abstract

"Agents still face challenges working across many context windows. We looked to human engineers for inspiration in creating a more effective harness for long-running agents."

"The core challenge of long-running agents is that they must work in discrete sessions, and each new session begins with no memory of what came before."

## Verbatim: key results

- "It has context management capabilities such as compaction, which enables an agent to work on a task without exhausting the context window. ... However, compaction isn't sufficient. Out of the box, even a frontier coding model like Opus 4.5 running on the Claude Agent SDK in a loop across multiple context windows will fall short of building a production-quality web app if it's only given a high-level prompt..."
- "First, the agent tended to try to do too much at once ... leaving the next session to start with a feature half-implemented and undocumented. ... This happens even with compaction, which doesn't always pass perfectly clear instructions to the next agent."
- "A second failure mode would often occur later in a project. After some features had already been built, a later agent instance would look around, see that progress had been made, and declare the job done."
- "The key insight here was finding a way for agents to quickly understand the state of work when starting with a fresh context window, which is accomplished with the claude-progress.txt file alongside the git history."
- Initializer agent sets up "an init.sh script, a claude-progress.txt file that keeps a log of what agents have done, and an initial git commit".
- "We prompt coding agents to edit this file only by changing the status of a passes field, and we use strongly-worded instructions like 'It is unacceptable to remove or edit tests because this could lead to missing or buggy functionality.' After some experimentation, we landed on using JSON for this, as the model is less likely to inappropriately change or overwrite JSON files compared to Markdown files."
- "we found that the best way to elicit this behavior was to ask the model to commit its progress to git with descriptive commit messages and to write summaries of its progress in a progress file."
- "One final major failure mode that we observed was Claude's tendency to mark a feature as complete without proper testing. Absent explicit prompting, Claude tended to make code changes, and even do testing with unit tests or curl commands against a development server, but would fail recognize that the feature didn't work end-to-end."
- Table "Agent failure modes and solutions": "Claude declares victory on the entire project too early" -> feature list file; "Claude leaves the environment in a state with bugs or undocumented progress" -> git repo and progress notes; "Claude marks features as done prematurely" -> "Self-verify all features. Only mark features as 'passing' after careful testing."

## Relevant to P44

- Qualitative engineering account, no numbers or ablations. It names handoff channels P44 cares about: compaction (judged insufficient), a progress file, git history, and a protected structured JSON feature list (a pinned-constraints analogue: only the `passes` field may be edited, JSON chosen because the model overwrites it less than Markdown).
- Failure modes are task-completion ones (premature victory, features marked done untested), not safety-state loss; but "premature victory" and unverified-done claims after a handoff are the same family as P44's false-pass-claim propagation.
- Gives an unmeasured design claim that structured/pinned state resists agent edits better than free prose, which P44 can test directly (pinned constraints vs agent-edited memory file).
- Gap: no comparison of handoff methods, no alignment or constraint-retention measurement, and no multi-generation decay curve.
