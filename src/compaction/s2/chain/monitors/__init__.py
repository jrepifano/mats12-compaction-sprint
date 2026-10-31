"""Experiment 2's offline monitor harness (docs/sprint/plans/week2-monitors.md v0.4).

Items (one act each, cut just before its result) are built per scenario into neutral
OpenAI-format message lists, one per view; `render` puts each view through a monitor
prompt's own channel with the vendored monitorkit (safety-research/red-teaming-auto-mode
@94f8911, MIT; `compaction.vendor.monitorkit`); `runner` scores the rendered requests on
the Max plan through `claude_headless`.
"""
