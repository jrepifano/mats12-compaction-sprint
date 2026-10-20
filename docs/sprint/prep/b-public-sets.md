# Sprint prep (b): public trajectory sets with real compactions

2026-10-20 (UTC). Unit (b) of the compaction-sprint prep ([proposal](../compaction-proposal.md), "Offline audit" and "Data sources"). No paid API calls; HF downloads only. This report gives structure and counts, no transcript content. Two agents built the unit: the first did the downloads and first builds (06:40–06:59Z), the second fixed parser and detector bugs (07:00–07:35Z). A third pass (07:40–08:10Z) answered code reviews r1 and r2, after which the scope was narrowed to data plumbing (see "Review response"). It also added PostTrainBench's prompt versions with the eval-key exception. Every table between `<!-- gen:… -->` markers is generated from the final build (`uv run python -m compaction.public_sets report`), not written by hand.

## Result

- **2,052 usable compaction records** from three public sets. A usable record has summary text, at least one assistant message in the history it replaced, and at least one after it. These are the candidates for Q1 (what summaries lose).
- **1,207 of them are fully assessable for Q1.** In these, the source window and the summary are complete: no text in the summary, the source or an inherited summary is cut, and the window's start is certain. In the other 845, part of the summary or the source was cut, by the dataset or by our own 60k-character limit, or the start of the replaced history is uncertain. Every record says which, in structured flags. Absence judgments should use the fully assessable records, or treat the flagged fields as unknown. "Fully assessable" says nothing about the post-compaction messages, which have their own flags.

<!-- gen:headline -->
| Set | Compactions found | Duplicate copies dropped | With summary text | Usable | Usable, summary and message text complete | Usable, fully complete | Sessions / runs with a usable record |
|---|---|---|---|---|---|---|---|
| SWE-chat v1 | 1,080 | 18 | 1,016 | 1,010 | 928 | 928 | 566 |
| PostTrainBench | 283 | 0 | 283 | 283 | 279 | 279 | 188 |
| FrontierML | 1,242 | 0 | 811 | 759 | 620 | 0 | 189 |
| **Total** | 2,605 | 18 | 2,110 | 2,052 | 1,827 | 1,207 | – |
<!-- /gen:headline -->

  "Usable, fully complete" is `fully_assessable`: complete source window and summary (Q1 eligibility), whatever the post messages. "Summary and message text complete" also allows cut tool arguments and tool outputs in the source (FrontierML only). No FrontierML record is fully assessable: every one has at least one tool output in its source that the dataset cut at 8,000 characters.
- **Violation detection was moved to the sprint plan (S1/S3).** The first-look violation numbers in the earlier draft of this report are withdrawn: review r1 found that the detectors have limited recall and that the "rule survived" check matched keywords rather than retained prohibitions.
- **SWE-chat v2** (`SALT-NLP/SWE-chat`) is gated, and our token gets HTTP 403 on its files (HEAD of `sessions.parquet` at 07:21Z). No terms were accepted, and v2 is not used.

## Datasets (pinned)

| Set | Repo @ revision | Repo size | Fetched | License |
|---|---|---|---|---|
| SWE-chat v1 | `cfahlgren1/SWE-chat` @ `f6cbfbbc935ba714a53e093eda99166a39048f2e` | 11,001 files, 15.42 GB | `sessions.parquet`, `session_logs.parquet`, `repositories.parquet`, `transcripts/*` (5,850 files, 9.5 GiB). Skipped `traces/` (2.6 GB, a slimmed copy of `transcripts/`), `conversations.parquet` and `commits.parquet` (2.6 GB). | ODC-BY-1.0 (card) |
| SWE-chat v2 | `SALT-NLP/SWE-chat` @ `202b071f18e03c79df5a7287565ce2ebc5ee7756` | 21,675 files, 46.99 GB | card only (gated, 403) | ODC-BY (card) |
| PostTrainBench | `aisa-group/PostTrainBench-Trajectories` @ `386b3034fa26f0f1b3ae3bfded99b791f0b254a6` | 224,416 files, 30.98 GB | every run's `solve_out.txt` (1,786 files, 4.1 GB), `metrics.json`, `time_taken.txt` and judge verdicts (3.9 GiB on disk). Skipped the task and final-model files copied into each run dir, `error.log` (1.3 GB) and `system_monitor.log`. | Apache-2.0 (card) |
| FrontierML | `inductionlabs/frontier-ml-trajectories` @ `0ef1d6e1ddf43feb1443ebe00515cb309383569c` | 883 files, 1.17 GB | all run JSONs (881 runs) | not stated on the card |

No set needed a run subset: for each, every file that carries trajectories was fetched. Total on disk: 14.5 GiB of downloads plus about 0.9 GiB of outputs, within the 40 GB budget. The revisions match the HF cache metadata written at download time.

## Deduplication

- **SWE-chat snapshots.** The dataset holds several daily snapshots of some sessions (e.g. `2026-01-27-<uuid>` and `2026-01-28-<uuid>`; 14 session UUIDs appear more than once), and two logs repeat records within the file. Two steps remove the copies:
  1. Within a Claude Code log, a record whose native `uuid` has already appeared is skipped. This removed 3 records, all compaction boundaries.
  2. Across logs, compactions that share a native key are collapsed into one. The key is the Claude Code boundary uuid (or the summary uuid when there is no boundary), the Codex session id plus timestamp, or the OpenCode message id. The copy kept has the most post-compaction messages, then the longest source, then the first id in sorted order. It lists the dropped ids in `duplicate_ids`.

  Compactions found: 1,101 → 1,098 after step 1 → 1,080 after step 2 (18 copies dropped). Usable records fell from 1,018 to 1,008, in 566 sessions (previously 573). The r2 boundary fix below then made 2 more usable (1,010).
- **PostTrainBench duplicate traces.** 65 run directories hold a byte-identical copy of another directory's `solve_out.txt` (e.g. `..._run1` and `..._run1_old_container`). Each trajectory is counted once, under the first directory in sorted order, as before. The fix is that the canonical run now aggregates the judge verdict files of every copy (`judges`: any positive copy counts as positive). It also keeps each copy's own flags (`judges_by_dir`), the flags on which copies disagree (`judges_disagree`) and the other directories (`duplicate_dirs`). 59 canonical runs have copies, and 27 of them gain or change a flag through aggregation. As a plumbing check, 8 Claude Code runs gain a positive contamination flag that only a discarded copy had, which matches the review's count. PTB records carry their run's aggregated `judges` as data. The verdicts are not analysed here.

## Failed compaction attempts (review r2)

OpenCode can abort a summary call (`MessageAbortedError`, no text) and then retry, sometimes after an intervening message and without a new compaction request. Such a failed attempt did not replace the context. In the first build, though, it started a new source window, so the retried summary's `source_messages` held only what came after the failure. For example, in session `ses_336ebe11bffePlPIwBobiYzDoC` the two successful summaries after failed attempts kept 1 and 0 source messages; they now keep 462 and 86.

Compaction events now come in three kinds:
- **Real compactions** start a new source window. These are summaries, and also compactions whose summary we can't read: encrypted Codex summaries, Claude Code boundaries with no summary record, FrontierML token drops. After an unreadable summary, the next record is flagged `previous_summary_unavailable`, because its inherited context is unknown.
- **Failed attempts** (`attempt: "failed"`, an errored summary call) do not start a window. A summary that follows one with no request in between is linked to it (`retry_of`) rather than flagged as having no boundary.
- **Unconfirmed attempts** (`attempt: "unconfirmed"`, a request never answered by a summary message) also do not start a window. The next record is flagged `unconfirmed_attempt_before`.

`post_messages` also skip attempts and stop at the next real compaction.

## Completeness flags

Each record and inventory row now carries:

| Field | Meaning |
|---|---|
| `summary_truncated`, `summary_truncation` | The summary text is cut. Cause: `dataset_message` (FrontierML's 24,000-character cut, detected from the step's `msg_len`) or `local_cut_60k`. |
| `source_truncated`, `source_truncation` | `{cause: messages}` for source messages whose text is cut, not counting an inherited summary. Causes: `local_cut_60k` (our `MAX_TEXT` cut, which leaves a marker), and FrontierML's `dataset_message`, `dataset_tool_args` (8,000 characters, from the call's `len`) and `dataset_tool_output` (from the observation's `len`). |
| `inherited_truncated_summary` | The source starts with a previous summary that was itself cut. |
| `boundary_uncertain`, `boundary_uncertainty` | `{cause: n}`: where the replaced history starts is not known. Causes: `no_boundary_record` (a summary with no boundary or request in this log, so the history it replaced may be in another log), `previous_summary_unavailable` and `unconfirmed_attempt_before` (see above). |
| `failed_attempts_before` | Failed or unconfirmed attempts inside the source window (informational). |
| `post_truncated`, `post_truncation` | `{cause: messages}` for post messages whose text is cut, with the same causes as the source. |
| `post_window_capped` | More than 50 messages followed. The cap is intentional (`MAX_POST`), not a cut. |
| `usable` | Summary text, an assistant message before it and one after it. |
| `fully_assessable` | Q1 eligibility of the source window and the summary: `usable`, with no summary, source, inherited-summary or boundary flag set. The post flags do not count. |

Usable records by source and summary flag:

<!-- gen:completeness -->
| Set | Usable | Summary truncated | Source truncated | Source causes (records) | Inherits a truncated summary | Boundary uncertain | Boundary causes (records) | After a failed attempt | Fully assessable |
|---|---|---|---|---|---|---|---|---|---|
| SWE-chat v1 | 1,010 | 0 | 81 | local_cut_60k 81 | 0 | 1 | no_boundary_record 1 | 2 | 928 |
| PostTrainBench | 283 | 0 | 4 | local_cut_60k 4 | 0 | 0 | – | 0 | 279 |
| FrontierML | 759 | 73 | 759 | dataset_message 45, dataset_tool_args 350, dataset_tool_output 759 | 52 | 0 | – | 0 | 0 |
<!-- /gen:completeness -->

Usable records by post-message flag. "Of which fully assessable" counts records that are complete for Q1 but whose post messages are cut:

<!-- gen:post-completeness -->
| Set | Usable | Post messages truncated | Post causes (records) | Of which fully assessable | Window capped at 50 (intentional) |
|---|---|---|---|---|---|
| SWE-chat v1 | 1,010 | 28 | local_cut_60k 28 | 18 | 874 |
| PostTrainBench | 283 | 1 | local_cut_60k 1 | 0 | 254 |
| FrontierML | 759 | 667 | dataset_message 24, dataset_tool_args 92, dataset_tool_output 667 | 0 | 726 |
<!-- /gen:post-completeness -->

In FrontierML, 122 main-thread summaries are cut by the dataset (`msg_len` above 24,000). 73 of them are in usable records; the other 49 are Grok Build summaries with no history logged, so they aren't usable anyway. Message and argument cuts in FrontierML are detected exactly: the dataset stores each field's original length, and a stored field is shorter only when it sits at the cap.

## Compactions by harness and model

`pre_tokens` is the context just before the compaction. It comes from `compactMetadata.preTokens` (Claude Code), the last `token_count` (Codex), the summarizer call's input tokens (OpenCode), or the last main-thread input count (FrontierML). "History" and "After" count compactions with at least one assistant message before the compaction (since the previous one) and after it. "Sessions" counts sessions or runs with at least one compaction.

**SWE-chat v1** (5,851 sessions: Claude Code 4,853, OpenCode 624, Codex 213, Gemini CLI 59, other 102; one transcript missing). Gemini CLI, Cursor and the other agents show no compaction markers.

<!-- gen:harness-swe-chat-v1 -->
| Harness | Model | Compactions | Sessions | With text | Auto / manual | pre_tokens min / median / max | History | After | Usable | Fully assessable | Unusable (reason n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-code | claude-opus-4-6 | 697 | 399 | 697 | 639 / 57 | 445 / 167,699 / 967,192 | 695 | 695 | 693 | 640 | no assistant message after it 2; no assistant message before it in this log 2 |
| claude-code | claude-opus-4-5-20251101 | 119 | 52 | 119 | 104 / 15 | 82,572 / 155,829 / 176,700 | 119 | 119 | 119 | 102 | – |
| claude-code | claude-sonnet-4-6 | 100 | 72 | 100 | 99 / 1 | 125,137 / 168,005 / 218,389 | 100 | 100 | 100 | 92 | – |
| claude-code | claude-sonnet-4-5-20250929 | 62 | 27 | 62 | 60 / 2 | 154,117 / 167,268 / 185,971 | 61 | 62 | 61 | 59 | no assistant message before it in this log 1 |
| codex | gpt-5.4 | 43 | 9 | 0 | 0 / 0 | 201,606 / 224,899 / 831,957 | 43 | 43 | 0 | 0 | encrypted (Codex remote compaction) 43 |
| codex | gpt-5.3-codex | 17 | 8 | 0 | 0 / 0 | 200,918 / 216,915 / 235,975 | 17 | 17 | 0 | 0 | encrypted (Codex remote compaction) 17 |
| claude-code | claude-opus-4-1-20250805 | 10 | 2 | 10 | 10 / 0 | 155,033 / 155,418 / 157,758 | 10 | 10 | 10 | 10 | – |
| opencode | gpt-5.3-codex | 9 | 5 | 7 | 4 / 5 | 30,123 / 243,342 / 254,191 | 9 | 7 | 7 | 5 | summary call failed (no text) 2 |
| claude-code | claude-sonnet-4-20250514 | 7 | 3 | 7 | 7 / 0 | 155,063 / 155,539 / 161,931 | 7 | 7 | 7 | 7 | – |
| claude-code | glm-5 | 4 | 3 | 4 | 4 / 0 | 167,319 / 168,032 / 176,661 | 4 | 4 | 4 | 4 | – |
| opencode | claude-opus-4.6 | 4 | 1 | 2 | 4 / 0 | 51,634 / 76,433 / 101,232 | 2 | 1 | 1 | 1 | no assistant message after it 1; summary call failed (no text) 2 |
| claude-code | claude-opus-4-7 | 3 | 2 | 3 | 2 / 1 | 626,673 / 976,868 / 1,066,964 | 3 | 3 | 3 | 3 | – |
| claude-code | glm-4.7 | 2 | 1 | 2 | 1 / 1 | 167,251 / 169,692 / 172,133 | 2 | 2 | 2 | 2 | – |
| claude-code | claude-haiku-4-5-20251001 | 1 | 1 | 1 | 1 / 0 | 167,161 / 167,161 / 167,161 | 1 | 1 | 1 | 1 | – |
| claude-code | exact-dodo | 1 | 1 | 1 | 1 / 0 | 1,410,364 / 1,410,364 / 1,410,364 | 1 | 1 | 1 | 1 | – |
| opencode | claude-sonnet-4.6 | 1 | 1 | 1 | 1 / 0 | 98,608 / 98,608 / 98,608 | 1 | 1 | 1 | 1 | – |
<!-- /gen:harness-swe-chat-v1 -->

**PostTrainBench** run directories by harness. Codex, OpenCode and Cursor traces carry no structural compaction events, so their compactions can't be counted. Runs with no timestamps get the oldest prompt version, flagged `prompt_version_inferred`.

<!-- gen:ptb-runs -->
| Harness | Run dirs | No trace | Duplicate traces | Untimestamped | Other-format runs with compaction markers |
|---|---|---|---|---|---|
| claude-code | 629 | 0 | 57 | 203 | 0 |
| codex | 522 | 0 | 1 | – | 0 |
| cursor-cli | 56 | 0 | 0 | 0 | – |
| glmx | 108 | 0 | 0 | 0 | – |
| kimi-claude | 84 | 0 | 0 | 0 | – |
| opencode | 415 | 56 | 7 | – | 0 |
| qwen-code | 28 | 0 | 0 | 28 | – |
<!-- /gen:ptb-runs -->

<!-- gen:harness-posttrainbench -->
| Harness | Model | Compactions | Sessions | With text | Auto / manual | pre_tokens min / median / max | History | After | Usable | Fully assessable | Unusable (reason n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-code | claude-opus-4-6 | 140 | 76 | 140 | 140 / 0 | 155,003 / 167,485 / 185,085 | 140 | 140 | 140 | 136 | – |
| claude-code | claude-opus-4-7 | 96 | 84 | 96 | 96 / 0 | 167,028 / 167,619 / 183,654 | 96 | 96 | 96 | 96 | – |
| claude-code | claude-sonnet-4-6 | 46 | 27 | 46 | 46 / 0 | 167,049 / 167,980 / 176,617 | 46 | 46 | 46 | 46 | – |
| kimi-claude | biui-0724 | 1 | 1 | 1 | 1 / 0 | 169,059 / 169,059 / 169,059 | 1 | 1 | 1 | 1 | – |
<!-- /gen:harness-posttrainbench -->

All PostTrainBench compactions are auto. The models with 1M-token windows (Opus 4.8, Opus 5, Fable 5) never compact in a 10 h run.

**FrontierML** (881 runs, 63 per model, 21 tasks). FrontierML records no trigger. The Codex CLI and Muse Code compactions are found as drops in the main thread's input tokens and store no summary text. Grok Build logs only the part after a compaction. Antigravity logs no token counts, so its compactions can't be counted. Opus 5.5, Fable 5.1, Haiku 5.5 and Kimi K3 never compact (peak contexts up to 826k tokens).

<!-- gen:harness-frontierml -->
| Harness | Model | Compactions | Sessions | With text | Auto / manual | pre_tokens min / median / max | History | After | Usable | Fully assessable | Unusable (reason n) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-code | Claude Sonnet 5.5 | 363 | 63 | 363 | 0 / 0 | 155,812 / 166,019 / 174,829 | 363 | 363 | 363 | 0 | – |
| codex | GPT-6 Astra | 225 | 59 | 0 | 0 / 0 | 223,213 / 243,447 / 255,798 | 225 | 225 | 0 | 0 | no summary text stored (token drop only) 225 |
| claude-code | GLM 5.3 | 211 | 63 | 211 | 0 / 0 | 87,772 / 165,815 / 166,977 | 211 | 211 | 211 | 0 | – |
| claude-code | Qwen3.8 Max | 185 | 63 | 185 | 0 / 0 | 146,428 / 165,690 / 167,313 | 185 | 185 | 185 | 0 | – |
| codex | GPT-6.1 Sol | 113 | 56 | 0 | 0 / 0 | 207,991 / 243,222 / 361,795 | 113 | 113 | 0 | 0 | no summary text stored (token drop only) 113 |
| codex | GPT-6 Luna | 66 | 43 | 0 | 0 / 0 | 238,790 / 244,259 / 245,020 | 66 | 66 | 0 | 0 | no summary text stored (token drop only) 66 |
| grok-build | Grok 4.7 | 52 | 52 | 52 | 0 / 0 | – | 0 | 52 | 0 | 0 | no assistant message before it in this log 52 |
| muse-code | Muse Spark 1.3 | 23 | 21 | 0 | 0 / 0 | 312,835 / 384,742 / 389,739 | 23 | 23 | 0 | 0 | no summary text stored (token drop only) 23 |
| muse-code | Muse Wonder | 4 | 4 | 0 | 0 / 0 | 380,393 / 383,860 / 384,352 | 4 | 4 | 0 | 0 | no summary text stored (token drop only) 4 |
<!-- /gen:harness-frontierml -->

## Records

One CompactionRecord per compaction with summary text, written to `data/raw/sprint/public/<set>.jsonl` (`scrubbed=false`, license from the card, `revision` = the pinned sha). Beyond the shared schema, records carry `native_key`, `duplicate_ids`, the completeness flags above and, for PostTrainBench, `judges` and `duplicate_dirs`. `post_messages` holds up to 50 main-thread messages and stops at the next real compaction. Message texts over 60,000 characters are cut with a marker, and the cut is flagged. Subagent messages are left out of records. A second compaction's `source_messages` starts with the previous summary, as a system message.

- **Rules.** SWE-chat records have none. Their `user_turns` hold what the users typed.
  - PostTrainBench's prompt is passed as a CLI argument and isn't in the trace. Each run's rules are rebuilt from PTB's template at the version in force on the run's first timestamp. The template is `src/eval/general/prompt.txt` plus the placeholders that `get_prompt.py` fills; its commit history was read with `gh api` on 2026-10-20.
  - Five commits changed the prompt:
    - 774716d8cb, 2026-01-07: rules 1–7 (**v1**).
    - 3bd836e297, 2026-07-01 06:06Z: for benchmarks whose grading needs a key, a rule that the key is for evaluation only.
    - 98aedb640e, 06:25Z the same day: a general API rule (**v2**), plus an eval-key exception for those benchmarks.
    - 5b4d100c76, 2026-07-20 09:33Z: a "Decontamination Tool" section.
    - 35793c15b2, 11:00Z the same day: "no data derived from test items", renumbered to 9 rules (**v3**).
  - The eval-key exception applies to the benchmarks whose `info.json` lists `required_api_keys`: arenahardwriting and healthbench, both `OPENAI_API_KEY`. On v3 the exception is rule 10, which `docs/sprint/violation-specs.md` calls **v4**.
  - The decontamination section appears only for tasks that ship a `test_data.json`, which isn't in the upstream repo. So it is taken from the trace: the section's heading in `solve_out.txt`, on runs dated after its commit.
  - Each run's version, label and extras are in `posttrainbench.runs.jsonl` (`prompt_version`, `prompt_label`, `prompt_extras`).
  - Claude Code runs with no timestamps use an older bare-JSON format, and timestamped lines appear from 2026-04-16 on. So these runs probably predate the July changes, and they get v1. That is an inference, and `prompt_version_inferred` marks it. Other-format runs with no timestamps get no version ("unknown"). Their compactions aren't observable, so they produce no records.

  Runs per prompt version (one row per canonical trajectory; duplicates are excluded):

<!-- gen:ptb-prompt-versions -->
| Label | Commit | Benchmark-dependent extras | No timestamps (Claude Code: v1 assumed) | Compacted runs | Non-compacted runs (Claude Code format) | Other-format runs (compaction not observable) |
|---|---|---|---|---|---|---|
| unknown | – | – | yes | 0 | 0 | 615 |
| v1 | 774716d8cb | – | no | 85 | 234 | 136 |
| v1 | 774716d8cb | – | yes | 102 | 129 | 0 |
| v2 | 98aedb640e | eval_key_exception | no | 0 | 40 | 39 |
| v2 | 98aedb640e | – | no | 0 | 85 | 36 |
| v3 | 35793c15b2 | decontamination_tool | no | 0 | 18 | 0 |
| v3 | 35793c15b2 | – | no | 1 | 89 | 28 |
| v4 | 35793c15b2 | eval_key_exception | no | 0 | 46 | 25 |
| v4 | 35793c15b2 | eval_key_exception+decontamination_tool | no | 0 | 13 | 0 |
<!-- /gen:ptb-prompt-versions -->

  All compacted runs had v1 except one v3 run. No run is dated inside the two short windows (3bd836e297, 5b4d100c76). The rules are dated by timestamp, not read from the run; `docs/sprint/violation-specs.md` §0 checks them against the rule blocks visible in traces.
  - FrontierML rules are restriction sentences matched by keyword in the task instruction (origin `task`) and CONTRACT.md (origin `file`). The match is crude and includes some sentences that aren't rules.
- **Inventory.** `<set>.inventory.jsonl` has one row per compaction found (after deduplication), usable or not, with the reason when not, and the same flags.

## Outputs (gitignored, mode 600)

<!-- gen:outputs -->
| File | Rows | Bytes | sha256 |
|---|---|---|---|
| `data/raw/sprint/public/swe-chat-v1.jsonl` | 1,016 | 457,924,380 | `f81f2324abe4a11bc3b9ef3cdb1d12fa886c74b23050d24d81a85dc4f4e6fa80` |
| `data/raw/sprint/public/swe-chat-v1.inventory.jsonl` | 1,080 | 878,749 | `b7f7f73a84464933e36d80b1f5e6cd580555d8efc09a33e2b082514285c0cdd6` |
| `data/raw/sprint/public/posttrainbench.jsonl` | 283 | 122,357,894 | `db908a297d92e183b8806f4efb54b35b55b9e080395b3d3d5da24f299836cc65` |
| `data/raw/sprint/public/posttrainbench.inventory.jsonl` | 283 | 229,608 | `fb7e00fdf66386a5a905abfbe36a2ee6860bb0486e72f1515e2580507b8089f7` |
| `data/raw/sprint/public/posttrainbench.runs.jsonl` | 1,786 | 1,600,531 | `e5c5161fc6791f9307bc0f924f4834828311b009221417889df0a2b5fdeed63b` |
| `data/raw/sprint/public/frontierml.jsonl` | 811 | 366,295,068 | `43a00559e42931fd64905064d3ee75787892a9cf914f191a9c4284f003c6318e` |
| `data/raw/sprint/public/frontierml.inventory.jsonl` | 1,242 | 1,025,156 | `3902135eb57e6c16271bc622f30ee2a250ea94b73b1e026361cd5fdb9f3cd609` |
| `data/raw/sprint/public/frontierml.runs.jsonl` | 881 | 138,371 | `4f56392471506192c8784045d0e3750366a2deb4e5cfc3b7b2f5985b4543126a` |
| `data/raw/sprint/public/build_summary.json` | – | 22,919 | `2c2586d00c66b99134c9c93b2e11be0cbcfd47ef60f9555be23deba9c03e7953` |
<!-- /gen:outputs -->

The earlier `posttrainbench.audit.json` and `frontierml.audit.json` (first-look detector output) remain on disk but were not regenerated, and their numbers are withdrawn (see above). The detector logic in `rules_detect.py` and the `audit` subcommand are unchanged since r1. The module's docstring now marks it WITHDRAWN.

Code: `src/compaction/public_sets.py`, `src/compaction/rules_detect.py` (detectors unchanged), and tests in `tests/compaction/test_public_sets.py`.

## Commands and timeline (UTC)

```bash
# 06:40Z, first session: downloads at the pinned revisions. Reproduce with:
HF_HUB_DISABLE_XET=1 uv run python -m compaction.public_sets download
# 08:06:04–08:07:16Z, final build after review r2 (all three sets, about 72 s):
uv run python -m compaction.public_sets build
uv run python -m compaction.public_sets report      # regenerates every table marked gen:
uv run pytest -q tests/compaction/test_public_sets.py
```

The record files are deterministic: two builds after the r1 fixes (07:46Z and 07:49Z), and the r2 builds at 08:02Z (PostTrainBench rebuilt at 08:04Z) and 08:06Z, wrote byte-identical record, inventory and run files. No new dependencies. The PTB prompt history was read with `gh api` (no download).

## Review response

Reviews: `logs/reviews/20261020-sprint-prep-b-r1.md` (gpt-6-astra: 6 BLOCKING, 1 MINOR) and `logs/reviews/20261020-sprint-prep-b-r2.md` (FIX BEFORE COMMIT: 1 BLOCKING, 2 MINOR).

| # | Finding | What changed | Test | Review r2 |
|---|---|---|---|---|
| r1-1 | BLOCKING: SWE-chat snapshots not deduplicated | Repeated native uuids within a log are skipped. Compactions are keyed by native id and collapsed across snapshots, keeping the most complete copy (`duplicate_ids`). 18 copies and 3 repeated records dropped, and the counts regenerated. | `test_cc_events_skip_repeated_native_uuids`, `test_swe_chat_overlapping_snapshots_are_deduplicated` | Resolved |
| r1-2 | BLOCKING: PTB deduplication discards judge verdicts | Every copy's verdicts are read and aggregated onto the canonical run (`judges`, `judges_by_dir`, `judges_disagree`, `duplicate_dirs`), and carried into PTB records as data. | `test_ptb_judges_aggregate_over_duplicate_dirs`, `test_merge_judges_keeps_missing_and_false` | Resolved |
| r1-3 | BLOCKING: "usable" does not establish completeness | Structured flags `summary_truncated`, `source_truncated` (with `source_truncation` causes), `inherited_truncated_summary`, `usable` and `fully_assessable` on every record and inventory row. Candidate and fully assessable counts are reported. | `test_fml_completeness_flags`, `test_local_cut_and_no_boundary_flags` | Partly resolved: see r2-1 and r2-3 |
| r1-4 | BLOCKING: FrontierML eval-split detector false negatives | Moved to the sprint plan. No change here. | – | Closed for prep |
| r1-5 | BLOCKING: `survived()` detects mentions, not retained prohibitions | Moved to the sprint plan. No change here. | – | Closed for prep |
| r1-6 | BLOCKING: near-zero detector hits do not establish near-zero violations | Moved to the sprint plan. The violation conclusions are removed from this report. | – | Closed for prep |
| r1-7 | MINOR: report numbers contradict the saved audit | Moved to the sprint plan (the audit numbers are withdrawn). All remaining tables are generated from the final build. | `test_write_report_replaces_only_generated_blocks` | Closed for prep; generated tables match the outputs |
| r2-1 | BLOCKING: failed compaction attempts reset the source window | Failed and unconfirmed attempts no longer start a source window, and retries are linked (`retry_of`). An unreadable previous summary or an unconfirmed attempt sets `boundary_uncertain`, which excludes the record from `fully_assessable`. `no_boundary_record` moved from `source_truncation` to `boundary_uncertainty`. Session `ses_336ebe11bffePlPIwBobiYzDoC` checked: its retried summaries now keep 462 and 86 source messages. Usable 2,050 → 2,052. | `test_opencode_retry_after_failed_attempt_keeps_history`, `test_opencode_failed_then_new_request_and_success`, `test_unconfirmed_attempt_and_unreadable_summary_mark_boundary_uncertain` | (new in r2) |
| r2-2 | MINOR: schema field types disagree with the writer | `schema.py` is fixed by the main session. The writer emits `summary_truncation: str \| None`, `native_key: str \| None` and `source_truncation: dict[str, int]` (also `post_truncation` and `boundary_uncertainty`). | the r2 tests above check the values | (new in r2) |
| r2-3 | MINOR: post-message completeness unrecorded; "no cut at all" overstated | Added `post_truncated`, `post_truncation` and `post_window_capped`. `fully_assessable` is defined as Q1 eligibility of the source window and summary, and the headline wording is fixed. | `test_post_truncation_and_window_flags` | (new in r2) |
| – | Coordinator request: PostTrainBench's fourth prompt version (eval-key exception) | Prompt versions now follow every upstream commit and the per-benchmark fill-ins: the eval-key exception for arenahardwriting and healthbench, and the decontamination section. Each run's version and label are recorded, with counts above. | `test_ptb_prompt_versions_include_eval_key_exception_and_decontamination` | – |

## Limitations

- Fully complete FrontierML records don't exist: every usable record has a cut tool output. Analyses that need tool outputs should treat those fields as unknown. Analyses of the summary against the user and assistant text can use the 620 records whose summary and message text are complete.
- Deduplication relies on native ids. Two snapshots of one session with re-generated ids would not be caught. None were seen, but none were searched for beyond the id match.
- PostTrainBench rules are reconstructed from the template, not read from the run.
- FrontierML's license is not stated.
- SWE-chat's models (Claude 4.5–4.6) and PostTrainBench's (Claude 4.6–4.7) differ from the sprint's planned models.
