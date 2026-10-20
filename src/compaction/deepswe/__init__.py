"""DeepSWE (github.com/datacurve-ai/deep-swe) as the online-run task source for the compaction sprint.

- `trials`: rank the 113 tasks by per-rollout peak context from the published trials.json.
- `tasks`: read the Harbor task directories (task.toml, Dockerfiles, verifier).
- `registry`: query public ECR for each task image's platforms and compressed size.
- `sandbox`: run a task image as a forkable, bounded Docker sandbox for our own agent loop.

See docs/sprint/prep/c-deepswe.md for the findings and the recommended setup.
"""
