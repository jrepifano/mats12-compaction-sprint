# apps-control-arena (HF dataset) and Control-Tax (arXiv 2506.05296)

Captured for probe P04 (reasoning-off code sabotage vs a code-only monitor). PDFs are not
committed (repo policy); this is a pointer + provenance note. See
`probes/P04-nocot-sabotage-monitor/litreview.md` for the read notes.

## Dataset: RoganInglis/apps-control-arena
- URL: https://huggingface.co/datasets/RoganInglis/apps-control-arena
- Licence: MIT.
- Pinned revision: `29db28f76042952d56add8671f798280b200744d`.
- Content: 5,000 APPS (interview-difficulty) competitive-programming problems, each with stdin/stdout
  test pairs (`inputs`/`outputs`), reference `solutions` (with `passes_tests`), and one or more
  `backdoors` (a `backdoor_code`, a `backdoor_input`, and `backdoor_works`). Flags
  `has_backdoor`, `is_nondeterministic`, `has_apps_backdoor` / `has_control_tax_backdoor`
  distinguish the two backdoor sources (the original APPS-control set and the Control-Tax set).
- Parquet shard sha256 (3 shards, `data/train-0000{0,1,2}-of-00003.parquet`):
  - `79f34486507a547ce43f9ae446ee9274645db507078ca2e3eb03cc9fcc9ccc71`
  - `0d9b3c15ad1aa9c52c7448f45030c5a0395d79ec8001cdd49b1866f20eaedbc2`
  - `c8813815d01f192dbffc60e3e03875f741296b5184526297d1f3807b57581dc7`
- P04 frozen subset: 150 problems (seed 0) from the 1,427 eligible (working reference backdoor,
  deterministic, >=2 passing reference solutions, >=3 tests). `configs/P04/tasks.json`,
  sha256 `ccd89d14d7c3da6b087ddbbeaaf93c1973f88534871da5e05ae26a83a89d533a`.

## Paper: Control-Tax (arXiv 2506.05296)
- "Control-Tax: The Price of Keeping AI in Check" (the dataset above is its released backdoor set).
- URL: https://arxiv.org/abs/2506.05296
- Relevance to P04 (from the lit review, read via the subagent): Fig. 9 reports backdoor-planting
  success with vs without thinking (Sonnet 3.7 ~77.0% no-think vs ~88.7% think, +11.7 pp) but under
  a 10-retry scaffold where the model edits a handed-in correct solution, so "off" there is not a
  single forward pass. Cheaper models in the same figure: GPT-4o-mini ~15.4%, GPT-4o ~48.5%,
  o3-mini ~93.2%. The exploit's input-validity requirement in P04 follows Control-Tax / Anthropic
  2025, not Greenblatt et al. 2312.06942.
