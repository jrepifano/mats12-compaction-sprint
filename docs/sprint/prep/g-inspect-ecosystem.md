# Sprint prep (g): the Inspect package ecosystem

2026-10-20 (UTC 07:05–07:35) · prep unit (g) · read-only survey, no paid calls, no repo edits except this file.

**Markers.** [V] = read in the primary docs or the installed source. [V, ran] = also exercised locally (free: `mockllm`, dummy keys, local Docker). [S] = from a search snippet or a fetch-tool summary only.

**How it was checked.** Packages were installed in throwaway venvs under the scratchpad (`.../scratchpad/inspect_survey/`): inspect-scout 0.6.0 (pulls inspect-ai 0.3.282, inspect-swe 0.2.71), inspect-viz 0.4.2, inspect-flow 0.14.0, inspect-harbor 1.0.0, playwright 1.64.0, openai 3.35.1. inspect-evals 0.25.0 was installed with `--no-deps` (source read only). The docs sites were read with a fetch tool, so doc-only claims are fetch summaries, marked [S] unless the installed source confirmed them. The test scripts are in `.../scratchpad/inspect_survey/scripts/`.

## Ranked recommendation

1. **inspect-scout: adopt for viewing and scanning, but not as the extraction layer for the offline audit.** Its viewer reads our Inspect `.eval` logs directly and suits the audit gates. Scanners over compaction regions are easy to write (a tested sketch is below). Its `claude_code` importer, however, loses data at our boundaries (§1.2): of 32 compaction boundaries in this project's 19 compacted sessions, 2 sessions (7 boundaries) fail to import, and 10 of the remaining 25 summaries are missing from the imported messages. It also drops the re-attached `AGENTS.md` and other attachments. The audit should parse the raw JSONL itself, as `docs/sprint/scan_compactions.py` already does. The parsed records can then be written into a Scout database through the Transcript API if we want the viewer.
2. **inspect-harbor: high relevance, check it with the inspect-swe spike.** `harbor(path=<DeepSWE task dir>)` loads a DeepSWE task as an Inspect task as it stands, including the `[[verifier.collect]]` hook. In a local run, the oracle scored 1.0 and a no-op solver scored 0.0 (§5). That gives Inspect agents (inspect-swe's `claude_code`/`codex_cli`, or `react` with compaction) the DeepSWE tasks for free. One caveat: in its "separate" mode it grades in the agent's own container after a `git checkout -f base && git clean -fd`, not in a pristine container.
3. **inspect-viz: optional, for interactive pages.** It renders PNGs (via playwright and Chrome) and can draw vertical bars with CIs (rendered locally, §2). But its prebuilt `scores_by_model` and `scores_by_factor` draw horizontal bars from Inspect's stderr. Our CIs are task-clustered, so we would build the plots by hand from marks either way. That is no easier than matplotlib, which we already use (`docs/pitch/make_figs.py`). Use viz only if we want interactive HTML for Neel or J.
4. **inspect-flow: skip for the sprint.** It adds a matrix spec, a cross-directory log store and venv isolation on top of `eval_set`. Plain `eval_set` with a Python loop over conditions already resumes. The store's reuse depends on stable sample ids and task hashes. That is more machinery than a two-week sprint needs.
5. **inspect-evals: nothing to adopt as-is.** No task covers compaction, consent or overeager behaviour. `ipi_coding_agent`'s scoring (canaries and file hashes, no judge) is a pattern worth borrowing for the trap scorer. `swe_bench` and `tau2_*` exist but are superseded by DeepSWE and by the proposal's decision to drop τ².
6. **Remote Docker:** Inspect's Docker sandbox shells out to the `docker` CLI with the environment inherited, so `DOCKER_HOST=ssh://…` or a docker context should work. I did not test that end to end. The simpler and safer choice is to run the Inspect process on the desktop itself (§6).
7. **Ignore:** inspect-cyber, inspect-k8s-sandbox, the ec2, proxmox, vagrant and openshell sandboxes, inspect-petri, inspect-mlflow and inspect-wandb. inspect-sentinel is a maybe; see §7.

## 0. Version constraint (applies to everything above)

- The repo pins `inspect-ai==0.3.257` with `openai>=2.54,<3` and `anyio==4.14.2`, in the `swarm` group (`pyproject.toml`; the openai pin is from the S4/X3 `float + Timeout` bug). [V]
- The newer packages need newer Inspect [V, PyPI JSON]:

  | Package | Needs inspect-ai |
  |---|---|
  | inspect-scout 0.6.0 | ≥0.3.281, plus inspect-swe ≥0.2.56 |
  | inspect-flow 0.14.0 | ≥0.3.278, plus inspect-scout ≥0.4.41 |
  | inspect-harbor 1.0.0 | ≥0.3.264, Python ≥3.12 |
  | inspect-evals 0.25.0 | ≥0.3.261 |
  | inspect-sentinel 0.1.4 | ≥0.3.282 |
  | inspect-viz 0.4.2 | no Inspect dependency |

- **inspect-ai 0.3.282 requires `openai>=3.4.0` for every OpenAI-compatible provider, OpenRouter included.** The check is `validate_openai_client` in `inspect_ai/model/_providers/providers.py`. [V] So 0.3.282 cannot share an environment with the `swarm` group's `openai<3`.
  - Jacob's running viewer was launched as `uv run --group swarm --with inspect-scout scout view …`. It resolved to inspect-ai 0.3.282 with openai 2.54.0. I reproduced that combination and got `OpenRouter API requires at least version 3.4.0 of package openai (you have version 2.54.0 installed)`. [V, ran]
  - With openai 3.35.1 and inspect-ai 0.3.282, a request reached OpenRouter: a dummy key returned a 401, with no `float + Timeout` error. I checked only that one call path. [V, ran]
- **Recommendation:** give the sprint its own uv group (e.g. `sprint`) with inspect-ai ≥0.3.282, openai ≥3.4, and inspect-scout, inspect-harbor and inspect-swe as needed. Declare it in `[tool.uv] conflicts` against `swarm`, so the S4/X3 replication stays on 0.3.257. Or run the CLIs as isolated tools (`uv run --no-project --with inspect-scout==0.6.0 --with 'openai>=3.4' scout view …`).

## 1. inspect-scout 0.6.0

**What it is:** Meridian Labs' transcript-analysis tool. It imports transcripts into a parquet/DuckDB "transcripts database" (or reads Inspect logs directly), runs scanners over them (LLM, grep or custom Python), validates scanners against human labels, and ships a web viewer, `scout view`. [V]

**Depth:** docs read (index, importing, llm_scanner, custom_scanner) [S]; source read (`sources/_claude_code`, `inspect_swe/_claude_code/_events`, `sources/_atif`, `_transcript/messages.py`, `_llm_scanner`, `_view/_api_v2_search.py`, `_api_v2_config.py`, the frontend bundle); importer, scanner and viewer API exercised locally [V, ran].

**Relevance:** high for the audit gates and for scanning summaries; medium-low as the audit's parser (§1.2).

**Adoption cost:**
- It needs inspect-ai ≥0.3.281 and therefore a new group (§0).
- The dependencies are heavy: duckdb, pyarrow, fastapi and libcst, plus inspect-swe, because the Claude Code parser lives in `inspect_swe._claude_code`.
- The learning curve is about half a day for scanners, and minutes for the viewer.

### 1.1 Import sources

- **`inspect_scout.sources`** exports `atif`, `claude_code`, `langsmith`, `logfire`, `phoenix` and `weave`. [V]
- **Inspect logs** need no import: `transcripts_from("./logs")`, or `scout view -T logs` directly. [V, ran]
- **MLflow** import lives in the separate `inspect-mlflow` package. [S]
- **No Codex CLI, pi or Antigravity source.** Those transcripts would have to go in through the Transcript API (`db.insert(Transcript(...))`), Arrow or parquet. [S, V: the source list]
- **ATIF** covers Harbor's installed agents: claude_code, codex, goose, opencode, openhands, gemini_cli, mini_swe_agent, swe_agent and terminus_2 (module docstring). [V] Its importer drops the summary text at a compaction step. The source says so: "KNOWN LIMITATION … the summary … is lost from `messages`, `message_count`, and DB search", and after the boundary it keeps pre-compaction turns in the reconstructed input. [V] **So ATIF import is unusable for our purpose.**

### 1.2 `claude_code`: does it keep compaction boundaries and summaries?

**Partly. Not reliably enough to be our extractor.**

- **The design keeps both** [V]:
  - A `system/compact_boundary` record becomes an `inspect_ai.event.CompactionEvent` with `source="claude_code"`, `tokens_before=preTokens` and `metadata={"trigger", "content"}`. `tokens_after` is left at None.
  - The summary (`isCompactSummary: true`) is kept as an ordinary `ChatMessageUser`. The `isCompactSummary` flag itself is not carried over, so locate the summary as the first user message after the `CompactionEvent`.
  - Messages are built with `span_messages(timeline.root, compaction="all")`, so pre- and post-boundary messages are merged.
  - `/clear` splits a session into separate transcripts. Plan-mode files sharing a slug are merged.
- **Run on this project's 19 compacted sessions** (`~/.claude/projects/-Users-jacob-…-exploratory/`, excluding this live session; script `scripts/import_all.py` and `summ2.py`) [V, ran]:
  - **2 of 19 sessions fail to import.** `64567087` (6 boundaries) and `f675206e` (1) raise a pydantic `ValidationError` on `UserEvent.toolUseResult` (`status: 200` as an int, from a web-tool result).
  - **Only 15 of the 25 summaries in the 17 imported sessions appear in `transcript.messages`.** The same 15 appear in the post-boundary `ModelEvent.input`.
  - **The missing 10 split cleanly into two causes** (classified per boundary from the raw JSONL with `scripts/classify.py`) [V, ran; mechanism partly inferred]:
    1. **Trailing boundaries (4):** no assistant turn follows in the same file (`5cb46a48`, `77b35d9f`, and the last boundary of `51392f5b` and of `8d20594b`). Messages are reconstructed from `ModelEvent`s, so a summary with no later model call disappears.
    2. **Attachment records between the boundary and the summary (6 of 6 such boundaries lost):** newer Claude Code writes `type: "attachment"` records (`instructions`, i.e. the re-attached `AGENTS.md`, plus `session_context` and `date`) between the two. The parser only keeps `user`, `assistant` and `system` types. In one checked case (`51392f5b`) the first model call after the boundary had an **empty** input. By contrast, all 15 boundaries where the summary directly follows the boundary and an assistant turn follows were kept. Inference: the summary's `parentUuid` points at a dropped attachment, which breaks the reconstructed ordering.
  - **Attachments are dropped everywhere.** The re-attached `AGENTS.md` is therefore not visible in Scout. That is exactly the "truly unavailable afterwards vs re-attached" distinction the audit needs.
  - `span_messages(..., split_compactions=True)` gives one list per region, but a trailing boundary produces no extra region. Count boundaries from the `CompactionEvent`s, not from the regions.
- **Recommendation:** keep our own parser for the 48 compactions (it already exists). If the viewer or scanners are wanted, write records into a Scout DB with the Transcript API: one transcript per boundary, with the source messages, the summary, the re-attached files and the post messages, plus `CompactionEvent`s. Worth reporting upstream (inspect_swe `_claude_code/_events`): the int `status` crash and the attachment/summary ordering.

### 1.3 Scanners

- **Built-in** [V source; S docs]:
  - `llm_scanner(question, answer, …)`. Answer types: `"boolean"`, `"numeric"`, `"string"`, `list[str]` labels, `AnswerMultiLabel`, and `AnswerStructured` (a pydantic model, optionally `list[T]`).
  - **Message handling:** messages are numbered `[M1]…` and cited back as `references`. A `MessagesPreprocessor` can exclude system messages, reasoning and tool use. Long transcripts are split at about 80% of the context window and combined by a reducer (`any`, `mean`, `majority`, `union`, `last`, or `ResultReducer.llm()`).
  - **Compaction:** `compaction="all" | "last" | n` selects which regions are scanned.
  - **Timelines:** `timeline=True` scans per span (subagents), with `depth` to limit nesting.
  - **Cost:** prefix caching (`cache_prompt=True` on a two-block template) and `cache=` response caching.
  - **`grep_scanner(pattern, regex=, ignore_case=, word_boundary=)`** needs no model.
- **Custom:** `@scanner(messages=…, events=…, timeline=…)` over a `Transcript`, `ChatMessage`, `Event`, `Timeline`, or a custom `@loader`'s lists. Return `Result(value, explanation, references, label)`. Inside a scanner, `get_model()` resolves to the scan model. `message_numbering()` gives `[M#]` text plus `extract_refs`. [V]
- **Validation sets:** human labels are compared with scanner output (`scan(..., validation=...)`), and can be edited in the viewer. [S] AGENTS.md says labelling pages are Claude Artifacts with `db`, so use these only if Jacob opts in.
- **Running a scan:** `scout scan scanners.py -T <transcripts> --model openrouter/...`. `SCOUT_SCAN_MODEL` (and `_BASE_URL`, `_ARGS`, `_CONFIG`, `_ROLE`) apply here. [V]
- **Sketch for "summaries that drop a user rule"**, tested offline with `mockllm` on a real session (`scripts/rule_drop_scanner.py`). It found 1 boundary, 22 user messages before it, and the summary text. [V, ran]

```python
@scanner(messages="all", events=["model", "compaction"])
def summary_drops_rule() -> Scanner[Transcript]:
    async def scan(t: Transcript) -> list[Result]:
        regions = span_messages(t.events, split_compactions=True)   # one list per region
        out = []
        for i, (pre, post) in enumerate(zip(regions, regions[1:])):
            rules_src = [m for m in pre if isinstance(m, ChatMessageUser)]
            summary = next((m for m in post if isinstance(m, ChatMessageUser)), None)
            # ask get_model() for: rules stated, which are missing/altered in summary (AnswerStructured)
            ...
            out.append(Result(label=f"boundary_{i}", value=..., explanation=..., references=...))
        return out
    return scan
```

  - For a lighter version, `llm_scanner(question=<callable building the rules-vs-summary prompt>, answer=AnswerStructured(...), compaction="all")` works when the whole transcript fits.
  - Either way, the region splitting inherits §1.2's losses on Claude Code imports. On our own Transcript-API records, or on Inspect logs from our runs, it is clean.

### 1.4 Viewer

- **What it shows:** `scout view [-T <transcripts or Inspect log dir>] [--scans …] [--host --port]`. The default port is 7576, bound to 127.0.0.1. It shows transcript lists, filtering and columns, a transcript view with messages and events (compaction events included), scan results, validation-set editing, and a project-settings page that writes `scout.yaml`. [V, ran]
- **In-transcript search:** grep search, or "LLM search" (an `llm_scanner` with `answer="string"` and an LLM reducer, cached per transcript in `scout_data_dir("search")`). [V]
- **For audit gates:** our own Inspect `.eval` logs from the online runs open directly. A remote viewer works through `ssh -L 7576:127.0.0.1:7576`.

### 1.5 LLM search: where the model and key come from, and the exact fix

**Resolution chain** (from `_api_v2_search.py`, `_api_v2_types.LlmSearchRequest`, the frontend bundle `index-DzlJCVr0.js`, and `inspect_ai.model.get_model`) [V, ran]:

1. **The UI sends `model = (model pill text) || (scout.yaml model) || null`.**
   - The pill is the box next to the search field. Its placeholder shows the `scout.yaml` model, or "e.g., openai/gpt-5.4".
   - On open, an empty pill is **pre-filled with the last model used** (browser `localStorage`, key `inspect-scout-user-settings` → `searchModelHistory`). A stale entry therefore overrides `scout.yaml`.
   - `scout.yaml` is read via `GET /api/v2/project/config` from the **directory where `scout view` was started**, base file only; `scout.local.yaml` is ignored here.
2. **The server calls `llm_scanner(model=request.model)`.** If the model is null, `get_model(None)` uses the active model (none in the viewer), then **`INSPECT_EVAL_MODEL`**, and otherwise fails with `No model specified (and no model environment variable defined)`.
   - **`SCOUT_SCAN_MODEL` is never read on this path.** It is only read by `scout scan` and `scan()` (`_scan.py:init_scan_model_context`).
3. **The provider key is read from the server's environment.**
   - `scout view` loads `.env` from its cwd, walking up through parents (tested: a `.env` in cwd supplied the key and `INSPECT_EVAL_MODEL`).
   - With the key missing, the error is `No OPENROUTER_API_KEY defined in the environment`.
   - With openai <3.4 under inspect-ai 0.3.282, OpenRouter fails on the version check (§0).

**Observed with each setup** (OpenRouter returns 401 to a dummy key, at no cost):

| Server setup | Model sent | Result |
|---|---|---|
| `SCOUT_SCAN_MODEL` set, no `INSPECT_EVAL_MODEL` | null | 502 "No model specified…" |
| openai 2.54 (Jacob's `--group swarm` launch) | `openrouter/anthropic/claude-haiku-5.5` | 502 "requires at least version 3.4.0 of package openai" |
| openai 3.35, no key | same | 502 "No OPENROUTER_API_KEY defined" |
| openai 3.35, `.env` with key and `INSPECT_EVAL_MODEL` | null | request reached OpenRouter (401 for the dummy key) |
| `scout.yaml` with `model:` | — | `/project/config` returns it as the UI default |

Nothing I ran produced a message containing "API key" except the missing-key case. **Likely cause (inference):** the server could not see `OPENROUTER_API_KEY`, or the pill was pre-filled from history with a model whose provider key is not set (e.g. an `anthropic/…` or `openai/…` id). The 502's detail text names the variable it wants.

**Fix (all three):**
1. **Launch the viewer outside the `swarm` group** so openai is ≥3.4, for example:
   `set -a; . ./.env; set +a; uv run --no-project --with inspect-scout==0.6.0 --with 'openai>=3.4' scout view -T <logs> --port 7576`
   Or start it from the repo root, whose `.env` it loads itself.
2. **Give it a model:** put `model: openrouter/anthropic/claude-haiku-5.5` in a `scout.yaml` in the directory `scout view` is started from (or set it on the Project settings page, field "Model"), then reload the page. **Also clear or overwrite the model pill** (it remembers the last model and wins over `scout.yaml`). Belt and braces: `export INSPECT_EVAL_MODEL=openrouter/anthropic/claude-haiku-5.5` covers a null model.
3. **`OPENROUTER_API_KEY`** must be in that process's environment or in the `.env` it finds. `SCOUT_SCAN_MODEL` does nothing for the viewer.

## 2. inspect-viz 0.4.2

**What it is:** a Python wrapper around Mosaic/Observable Plot. It produces interactive Jupyter or anywidget plots fed from dataframes through DuckDB, with prebuilt "views" for Inspect results. [V]

**Depth:** docs index read [S]; every public function catalogued from the installed source [V]; a PNG rendered locally [V, ran].

**Relevance:** low-medium. It can do what we need, but our CIs are task-clustered, so we hand-build plots in any library. The win is interactive HTML.

**Adoption cost:** no Inspect pin. Dependencies: anywidget, narwhals, pyarrow and pandas. PNG export needs `playwright` and Chrome. It uses the installed Chrome (`channel="chrome"`), otherwise it offers to install Chromium. Learning curve: Observable-Plot-style channels, about 1–2 h.

**Data in:** `Data.from_dataframe(df)` (pandas, polars, pyarrow) or `Data.from_file(path)`. The prebuilt views expect `inspect_ai.analysis.evals_df` / `samples_df` columns (`score_headline_value`, `score_headline_stderr`, `model_display_name`, …). [V] The views use Inspect's stderr-based CIs, not cluster bootstrap.

**Static images:**
- `inspect_viz.plot.write_png(file, component, scale=2, padding=8)` (and an async variant). [V, ran] A 4-arm × 2-model vertical-bar plot with CI rules rendered to 1312×792 in about 3 s. Image: `.../scratchpad/inspect_survey/viz_test.png`.
- `write_html` / `to_html` / `HtmlPage` give self-contained HTML. Quarto and notebook publishing are documented. [S]
- **Gotchas seen:** categorical x is sorted alphabetically (set `x_domain=[...]` for arm order); long tick labels clip (set `margin_bottom` or wrap the labels).

**Catalogue** [V; the one-liners are the docstrings]:

- **Views (`inspect_viz.view`):**
  - `scores_by_task`: vertical bars (`bar_y`) per model, faceted by task, with CI rules.
  - `scores_by_model`: one eval, models as bars. Horizontal: it draws thick `rule_y` bars with the models on the y axis.
  - `scores_by_factor`: a boolean factor (e.g. "No hint"/"Hint") per model. Horizontal paired bars with CIs.
  - `scores_by_limit` (+`_df`): success rate against a token or time limit (line + area).
  - `scores_timeline`: score by model release date (dot + line + CI).
  - `scores_heatmap` and `sample_heatmap`: success-rate heatmaps (task × model, sample × model).
  - `heatmap`: a heatmap of arbitrary data.
  - `sample_tool_calls`: a tool-call heatmap over turns.
  - `scores_radar_by_task` and `scores_radar_by_metric` (+`_df`): radar charts.
- **Marks (`inspect_viz.mark`, 87 names):**
  - Bars: `bar_x` (horizontal), `bar_y` (vertical).
  - Rectangles and cells: `rect`, `rect_x`, `rect_y`, `cell`, `cell_x`, `cell_y`.
  - Points: `dot`, `dot_x`, `dot_y`, `circle`, `hexagon`.
  - Lines and areas: `line`, `line_x`, `line_y`, `area`, `area_x`, `area_y`.
  - Rules and ticks: `rule_x`, `rule_y`, `tick_x`, `tick_y`.
  - Error bars: `error_bar_x`, `error_bar_y`. These compute CIs from raw rows and accept no precomputed bounds.
  - Fits and densities: `regression_y`, `density`, `density_x`, `density_y`, `dense_line`, `contour`, `heatmap`, `raster`, `raster_tile`.
  - Hex and Voronoi: `hexbin`, `hexgrid`, `hull`, `delaunay_link`, `delaunay_mesh`, `voronoi`, `voronoi_mesh`.
  - Direction and annotation: `vector*`, `spike`, `arrow`, `link`, `text*`, `image`.
  - Waffles: `waffle_x`, `waffle_y`.
  - Geo: `geo`, `graticule`, `sphere`.
  - Frame and axes: `frame`, `baseline`, `axis_*`, `grid_*`, `title`.
- **Transforms:**
  - SQL and aggregation: `sql`, `agg`, `avg`, `count`, `sum`, `median`, `quantile`, `stddev`, `variance`.
  - CIs: `ci_bounds`, normal-approximation CIs from stderr.
  - Binning: `bin`.
  - Window functions: `lag`, `lead`, `rank`, `row_number`, `n_tile`, `cume_dist`, ….
- **Other components:**
  - `plot`, `legend`, `table`.
  - Inputs: `select`, `checkbox`, `radio_group`, `slider`, `search`.
  - Interactors: brush, pan/zoom, toggle, highlight.
  - Layout: `hconcat`, `vconcat`.

**Plots that fit our results** (Jacob prefers vertical bars):
- **Rates with CIs by condition:** `bar_y(x=arm, y=rate, fx=model)` plus `rule_x(x=arm, y1=lo, y2=hi, marker="tick")`, fed precomputed task-clustered bootstrap bounds. Tested; the shape matches `docs/pitch/fig` style.
- **Paired arm comparisons** (e.g. summary+rule − summary): a `bar_y` of the paired difference per model, with a `rule_x` CI and a `rule_y(y=0)` baseline. Do not use `scores_by_factor`, which is horizontal and stderr-based.
- **Per-model bars:** the same pattern with `x=model`. `scores_by_task` is vertical if the data come from Inspect logs and Inspect's stderr is acceptable.
- **Compaction count against violation rate:** `line` + `dot` + `rule_x`, or `bar_y` by count.

## 3. inspect-flow 0.14.0

**What it is:** declarative orchestration over `eval_set`. [S docs; V package metadata] It provides:
- a `FlowSpec`/`FlowTask` config in Python or YAML;
- defaults inheritance;
- matrix helpers (`tasks_matrix`, `models_matrix`, `configs_matrix`);
- `flow run` (`--dry-run`, `--resume`, `--venv`, `--log-dir-create-unique`);
- `flow check` against a log dir (exit code 3 when incomplete);
- a "Flow Store" (a Delta Lake registry of log paths keyed by a task-config hash; `--store-read` copies a matching log in and lets `eval_set` run only the missing samples);
- `flow step` post-processing (tags, promote).

It writes `flow.yaml` and `flow-requirements.txt` next to the logs.

**Depth:** docs index, run.html and store.html read via a fetch tool [S]; package installed and its dependencies read [V].

**Relevance:** low. `eval_set` already does resume and retry within one log dir. Our arms are resume-from-prefix variants that we will build ourselves, so the "task identity" hashing is a liability (dynamic inputs get new identifiers each run [S]).

**Adoption cost:** inspect-ai ≥0.3.278 plus inspect-scout and deltalake; another config layer and CLI to learn. Not worth it for two weeks. Revisit only if the runs spread across several log dirs or machines.

## 4. inspect-evals 0.25.0

**What it is:** UK AISI's collection of 135 eval packages. [V, source listing]

**Depth:** source and READMEs grepped.

**Relevance:** low. Nothing covers compaction, memory across compaction, overeager actions or consent. A grep for "compaction" found nothing; "overeager", "irreversible" and "force push" only matched b3 tool specs. [V]

**Adoption cost:** inspect-ai ≥0.3.261; per-eval extras.

**Closest matches** (task names as registered) [V]:
- **`ipi_coding_agent`** (CodeIPI): 45 samples of a coding task with an injected instruction. Scoring uses canary strings, `CANARY_EXEC_<id>` markers in tool calls and file-hash diffs for persistence, with no judge. A good template for our trap scorer (shell shim + snapshots).
- **`swe_bench`, `swe_bench_verified_mini`:** pre-built x86_64 images, `sandbox_type` can be `docker` or `k8s`, the default solver is a bash/python agent, and `--solver` overrides it. DeepSWE supersedes it for long tasks.
- **`tau2_airline`, `tau2_retail`, `tau2_telecom`, `tau2_banking`:** the README warns of very high token use. Only a possible cheap replication check, as the proposal says.
- **`agentic_misalignment`:** Anthropic's blackmail/leak scenarios; not coding.
- **`persistbench_cross_domain`, `persistbench_sycophancy`, `persistbench_beneficial_memory`:** long-term memory risks, but as single-turn memory injection, not compaction.
- **Long-context retrieval:** `niah` and `infinite_bench_*`. Coding or long-horizon agent tasks: `swe_lancer`, `theagentcompany`, `mle_bench`, `paperbench`, `core_bench`, `mlrc_bench`, `osworld`, `agentdojo`, `gdm_self_reasoning`, `gdm_stealth`. None tests rules across compaction.

## 5. inspect-harbor 1.0.0 (found while surveying; overlaps the DeepSWE prep)

**What it is:** Meridian Labs' loader that turns Harbor-format tasks into Inspect `Task`s (`harbor(path=…)`, `task_git_url=`, a registry, or a package). The default solver is a ReAct agent with `CompactionEdit()` [S], and `--solver` swaps in any Inspect agent, e.g. `inspect_swe/claude_code` [S docs]. `harbor_scorer` runs `tests/test.sh` and reads `/logs/verifier/reward.json` or `.txt`. [V] 1.0 no longer depends on the `harbor` package. [S]

**Depth:** docs (index, tasks, agents) [S]; source (`_harbor/scorer.py`, `converters.py`, `models.py`) [V]; run locally on a DeepSWE task [V, ran].

**Run on a DeepSWE task:** `harbor(path=".../deepswe/repo/tasks/wasmi-trap-coredumps")` with the prebuilt amd64 image cached locally (`scripts/harbor_oracle.py`).
- The `oracle` solver scored **1.0** in 34 s ("model.patch applied (31108 bytes)").
- `generate` with `mockllm` (no changes) scored **0.0** in 17 s ("no model.patch submitted").
- Containers were cleaned up afterwards. The only container left running belonged to the other agent's spike.
- What this proves: the `[[verifier.collect]]` diff hook and `docker_image` from `task.toml` are honoured. [V, ran]

**Caveat:** `environment_mode = "separate"` (all DeepSWE tasks) is emulated by `git checkout -f <base> && git clean -fd` **in the agent's own container**, not in a pristine verifier container as Harbor/Pier does (`scorer.py:_run_verifier_collect`). [V]
- Changes outside the repo, and git-ignored files, survive into grading.
- That is acceptable for pass/fail. It matters for traps, e.g. a deleted ignored `.env` file, so take trap outcomes from our own snapshots.

**Relevance:** high if the online runs use Inspect agents, which is what the inspect-swe spike decides. Medium otherwise, since c-deepswe recommends our own loop.

**Adoption cost:** inspect-ai ≥0.3.264, Python ≥3.12; small.

## 6. Sandbox providers and Docker over SSH

**Built-in `docker` provider** (`inspect_ai/util/_sandbox/docker/`):
- It runs `docker compose …` and `docker …` as subprocesses with `env={**os.environ, …}` (`util/_subprocess.py`). [V]
- **`DOCKER_HOST=ssh://jacob@desktop` or `docker context use desktop` is therefore inherited**, and every operation goes to the remote daemon:
  - container start and stop;
  - `exec`;
  - `write_file` (via exec);
  - `read_file` (via `compose cp`);
  - image-exists checks and pulls;
  - the injected `inspect-sandbox-tools-amd64` binary.
- **That is inferred from the source; I did not test it end to end.**
- The docs don't mention remote hosts. They require Docker Engine ≥24.0.6 and Compose ≥2.21 (≥2.22 for pulls), and suggest configuring address pools above about 30 containers. [S]

**Gotchas** (the first two are inferred):
1. **SSH connection storms:** Docker's `ssh://` transport opens an SSH session per CLI call. Inspect allows `max(2×CPU, 4)` concurrent docker CLI calls (`INSPECT_DOCKER_CLI_CONCURRENCY`; 36 on this laptop). That can trip sshd's `MaxStartups`, the same failure we hit polling RunPod. Use `ControlMaster auto` / `ControlPersist` in `~/.ssh/config`, raise `MaxStartups`/`MaxSessions` on the desktop, and lower `INSPECT_DOCKER_CLI_CONCURRENCY`.
2. **Sizing comes from the laptop:** the `max_sandboxes` default is 2 × the *local* effective CPU count, not the desktop's. Set `max_sandboxes` explicitly.
3. **Bind mounts resolve on the desktop's filesystem.** DeepSWE via inspect-harbor copies tests in through exec, not mounts. [V: `copy_directory_to_sandbox`]
4. **Every exec pays an SSH round trip.**

**Recommended instead:** run the Inspect process itself on the desktop (tmux plus `uv run`, keys in its `.env`), then rsync `.eval` logs back for `scout view`. Images then run natively on amd64, as c-deepswe found, and the laptop's lid and sleep stop mattering.

**Other providers:**

| Provider | Package | Notes |
|---|---|---|
| `k8s` | inspect-k8s-sandbox 0.14.0 | Helm/K8s; built for AISI's cluster conventions |
| `daytona`, `modal`, E2B | inspect-sandboxes 0.6.1 | Cloud sandboxes, Dockerfile support; a fallback if the desktop is unavailable [S] |
| `ec2` | inspect-ec2-sandbox 0.1.1 | No Dockerfile support |
| `proxmox` | inspect_proxmox_sandbox | PyPI 0.0.1 is a placeholder; install from GitHub; no Dockerfile support |
| `vagrant` | inspect_vagrant_sandbox | Not on PyPI |
| `openshell` | inspect-openshell-sandbox 0.1.0 | NVIDIA OpenShell |
| `podman` | inspect-podman 0.1.2 | Podman |

None is needed; plain Docker on the desktop is enough. [S docs table; V PyPI]

## 7. Everything else

- **inspect-cyber 0.1.0** (UK AISI): an extension for building agentic cyber evals (CTF-style challenge configs). Irrelevant. [V PyPI]
- **inspect-swe 0.2.71** (Meridian): CLI coding agents as Inspect agents (claude_code, codex_cli, …), and the home of the Claude Code event parser that Scout uses. The other agent is spiking it, so it is not covered here.
- **inspect-tool-support 1.2.0** (Aug 2025): legacy, used now only for the web browser tool. Current Inspect injects `inspect-sandbox-tools-{amd64,arm64}` binaries bundled in the inspect-ai wheel into containers for `bash_session`, `text_editor` and MCP (`tool/_sandbox_tools_utils/_legacy_helpers.py`). Nothing to install. [V]
- **inspect-sentinel 0.1.4** (Meridian, released 19 Oct): monitors and "protocols" that run before or after tool calls and can block or intervene, and can run offline over Scout transcripts. [V PyPI] It says it is under active design. Medium-low: it could implement the `ask_user`/consent trap gate as a protocol, but our shell shim is simpler and stable.
- **inspect-petri 3.1.1:** an auditing agent; not relevant. **inspect-mlflow 0.9.0** and **inspect-wandb 0.3.0:** experiment-tracking integrations; not needed. **control-arena 20.1.0:** AISI's control settings; not relevant. [V PyPI]
- **`inspect_ai.analysis`** (in core, the same API in 0.3.257 and 0.3.282) [V]:
  - Dataframes: `evals_df`, `samples_df`, `messages_df`, `events_df`, with column specs such as `SampleSummary`, `SampleScores`, `ModelEventColumns`, `ToolEventColumns` and `EventTiming`.
  - Helpers: `prepare` (with `model_info`, `task_info`, `score_to_float`, `frontier`) and `log_viewer`.
  - The cheapest path from our Inspect logs to bootstrap tables. `events_df` can select compaction events by type (inferred from the event filter list; not run).
- **Inspect core compaction** (both versions): `CompactionAuto`, `CompactionSummary`, `CompactionTrim`, `CompactionEdit` and `CompactionNative` in `inspect_ai.model`. This is the "Inspect" summarizer used in P49. [V]

## Files

- This report: `docs/sprint/prep/g-inspect-ecosystem.md`.
- Scratch scripts and outputs: `/private/tmp/claude-501/-Users-jacob-Documents-GitHub-mats12-neel-exploratory/18f6ced4-1e23-4bc7-aed5-f1e190837aa7/scratchpad/inspect_survey/`:
  - `scripts/cc_import.py`, `import_all.py`, `summ2.py`, `after.py`, `classify.py`: Claude Code import checks;
  - `scripts/search_test.py`: viewer LLM-search API;
  - `scripts/rule_drop_scanner.py`: scanner sketch;
  - `scripts/viz_png.py`, with output `viz_test.png`;
  - `scripts/harbor_oracle.py`: DeepSWE via inspect-harbor;
  - `import_all.log`.
