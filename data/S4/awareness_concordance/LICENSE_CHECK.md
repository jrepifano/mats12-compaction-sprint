# U7 license check: authors' step 2 transcripts and awareness labels

Checked 2026-09-30T04:49Z (UTC), anonymously, by the U7 subagent. Metadata requests only: no
transcript was downloaded.

## Verdict: no license. U7 stopped at step 1.

Neither the dataset nor the code repository that produced the labels states a license. Public
availability is not a grant of reuse rights, so under the implementation plan (D6) and the U7
brief the concordance check is **skipped**. The prereg's fallback then applies: Jacob labels
strong awareness by hand in all 10 episodes of each plan arm (prereg §4.7).

## Evidence

| Source | What was checked | Finding |
|---|---|---|
| HF dataset `stewy33/oai-hf-incident-replication`, revision `9b792c782a714e017bb7b4c2cbff68534d8d5c5b` (last modified 2026-09-11T21:57:59Z), https://huggingface.co/datasets/stewy33/oai-hf-incident-replication | API metadata (`/api/datasets/...`): `license` field and `cardData` | `license: null`. The card's YAML has `pretty_name`, `tags`, `size_categories` and `configs`, and no `license` key. The dataset is public and not gated. |
| Same, `README.md` (dataset card) at that revision | Full text | No license or terms. It has an "Intended use" section: "Alignment and red-teaming research: studying how agentic models drift into misaligned actions, and how well manual and automated auditing surface those behaviours." That is a statement of purpose, not a license. |
| Same, file listing (2,532 files) | Any file whose name contains "licen" | None. The only top-level files are `.gitattributes` and `README.md`. |
| GitHub `msp895/oai-hf-incident-reproduction` (the card's stated provenance; it also produced the labels file), https://github.com/msp895/oai-hf-incident-reproduction | GitHub API `license` field; `/repos/.../license` | `license: null`; `/license` returns 404. The local copy at commit 2fec982 has no license file either (as D6 records). |

## Intended use, had a license permitted it

We would have used the authors' labels (`eval_awareness_multimodel_scores.jsonl`, the 791
`step2` rows) and their transcripts only to compute agreement (Cohen's kappa on degree ≥ 2)
between our own awareness judge and their labels. We would not have redistributed either: the
transcripts would have stayed in the local Hugging Face cache, and only a row-to-file mapping and
summary statistics would have been committed. The authors' "Intended use" statement suggests they
would likely welcome this, but that is an inference, not permission.

## If Jacob obtains permission (e.g. by asking the authors)

Notes for resuming, from reading (not copying) the replica's analysis scripts and the dataset's
file listing:

- **Labels.** 791 step 2 rows across 9 models (idx ranges 0..n−1 per model):
  - GLM-5.2: 264;
  - Claude Opus 4.8: 67;
  - Gemini 3.8 Flash: 76;
  - GLM-5.3, Kimi K3, Qwen 3.8 Max, Grok 4.6, GPT-5.6 sol and Muse Spark 1.3: 64 each.
- **Degree counts.** 0: 296; 1: 202; 2: 255; 3: 25; missing (judge parse failures): 13. That
  gives 280 strong labels. The label judge was Claude Opus 4.8 via OpenRouter.
- **How `idx` was assigned.** Per (step, model), the scorer took a glob of `.eval` files, sorted
  the paths lexicographically (so `shard1, shard10, …, shard16, shard2, …`), read each log's
  samples in the order `read_eval_log` returns them, and numbered them 0, 1, 2, ….
  - For GLM-5.2, the rows were copied from an earlier pass over all `.eval` files under
    `logs/260906_step2_reachout_protein_none_n256`, numbered in the same way.
- **Where the step 2 files are on the Hub** (all under `part1_docker/`):
  - GLM-5.3, Kimi K3, Qwen 3.8 Max, Grok 4.6: `260909_step2_bank_none_multimodel/protein_none/<model>__shard{1..8}/`;
  - Gemini 3.8 Flash, GPT-5.6 sol, Opus 4.8: `260909_step2_bank_none_reasoning/protein_none/<model>__shard{1..16}/`;
  - Muse Spark 1.3: `260909_step2_muse_meta/protein_none/musespark13__shard{1..8}/`;
  - GLM-5.2: `260906_step2_reachout_protein_none_n256/` (34 files).
  - Some Gemini and Opus shards hold two `.eval` files, which fits the 76 and 67 counts.
- **Verifying the mapping.** Check the sample count per model against the label count. Check
  that each mapped sample's model string is right. Spot-check that each label's `quote` field
  occurs in the mapped transcript.

## Decision (Jacob, 2026-09-30)

"if the dataset has no license assume we can use it. This link was provided to me by faculty in my
program." The concordance check proceeds, using the authors' labels and transcripts only to
measure our judge's agreement with them. Nothing is redistributed: the transcripts stay in the
local Hugging Face cache and are not committed.
