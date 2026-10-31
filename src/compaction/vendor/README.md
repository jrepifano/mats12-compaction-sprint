# Vendored third-party code

## monitorkit

`monitorkit/` is `arena/monitorkit/src/monitorkit` from
https://github.com/safety-research/red-teaming-auto-mode at commit
94f891113665c6dee50c95b79c32549fed963977 (MIT; `monitorkit/LICENSE`), copied unmodified on
27 Oct 2026 (only `__pycache__` removed). Its `guardian_data/` holds Codex's Guardian policy
(Apache-2.0, openai/codex@3725f02), vendored byte for byte upstream.

Used by Experiment 2's offline monitor harness (`compaction.s2.chain.monitors.render`) for
the two prompts and their production channels. Don't edit it; re-vendor from a new commit
instead and record the commit here.
