# Sprint prep (c): DeepSWE as the online-run task source

2026-10-20 (UTC 06:29–06:45). Prep unit (c) for the compaction sprint ([proposal](../compaction-proposal.md), "Design detail" and "Data sources"). No paid calls. Code: `src/compaction/deepswe/` (`trials`, `tasks`, `registry`, `sandbox`, `feasibility`), tests `tests/compaction/test_deepswe.py`. Raw data: `data/raw/sprint/deepswe/` (gitignored).

## Answers first

- **The proposal's 45 is right, but "~200k" is the floor, not the median.** Exactly 45 tasks have a median peak of at least 200k on both DeepSeek V4 Flash (max) and GPT-5.6 luna (max). The top 45 by the smaller of the two medians are the same 45 tasks. Pooled over those 45 tasks, the median peak is **242k for DeepSeek V4 Flash** (IQR 215–266k; 33% of rollouts over 256k), **251k for luna 5.6 max** (227–321k; 47% over 256k), **175k for GLM 5.3 Flash** (147–204k; 7% over 256k), and 163k over all 70 configs (16% over 256k). The cut is soft: tasks 46–52 have medians of 196–200k. The DM's "(~200k tokens)" undersells it: about 240–250k is the median on the two selection models.
- **DeepSWE's "luna" is GPT-5.6 luna.** It isn't GPT-6 luna, the model we plan to use. GPT-6 luna, DeepSeek V4.1 Flash and Haiku 5.5 are absent from trials.json, and gpt-6-astra rows have no `peak_context_tokens`. The pilot has to measure peaks for our models, as the proposal already says.
- **The images are amd64 only, but they run fine on the Mac.** The images are 0.76–2.14 GB compressed (median 0.90). Their base image `mars-base` is multi-arch (amd64 and arm64). Under Docker Desktop's amd64 emulation, the oracle patch scored reward 1 and an empty patch scored reward 0 on a TypeScript task and a Rust task. The overhead per run is under a minute: start 0.1–0.4 s, snapshot 3 s, fork 0.1 s, grading 20–24 s.
- **Emulation is 2–5× slower on CPU work and about 30× slower on process spawn**, but the LLM dominates. A DeepSWE rollout takes 9–12 s per step of model time. Test suites took 3–18 s emulated (3–4× faster natively).
- **Recommendation for the agent loop:** our own loop (P49-style), using DeepSWE containers as the sandbox and DeepSWE's own verifier run our way. Do not use Harbor/Pier's runner. Pier runs the agent *inside* the container, through a fixed CLI (mini-swe-agent, claude-code, codex, antigravity-sdk), and has no snapshot or fork primitive. `docker commit` forks work (tested), but only as filesystem copies: a fork is a faithful continuation only at a quiescent boundary, with no background process running, which the sandbox now checks before every snapshot (§4).
- **Where to run:** for the bulk, an amd64 Linux box. That can be Jacob's desktop if it is x86-64 with at least 16 cores, 64 GB and 250 GB free, or a cloud VM at roughly $0.5–2/h. Use the laptop for the pilot only. The compute cost is small either way. The reasons are concurrency: the laptop's Docker VM has 8 GB, it is shared with other agents, and closing the lid pauses everything. Native speed is a smaller reason.

## 1. Repo and task format

- Cloned `github.com/datacurve-ai/deep-swe` at **0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea** (2026-08-26, "Update task timeout settings to 10800s") into `data/raw/sprint/deepswe/repo`. The licence is Apache-2.0 for Datacurve's contributions; each upstream project keeps its own permissive licence (README).
- **113 tasks:** TypeScript 35, Go 34, Python 34, Rust 5, JavaScript 5. Each task has:
  - `task.toml`: metadata, `docker_image` (a pinned `public.ecr.aws/d3j8x8q7/swe-bench-202605:<id>-v1.1`), limits of 2 CPU, 8 GB and 20 GB storage, an agent timeout of 10,800 s, and agent and verifier `network_mode = "no-network"`.
  - `instruction.md`: the prompt, about 1 KB. Every prompt ends "Please work on this in a new branch from main and commit everything when you are done."
  - `environment/Dockerfile`: `FROM mars-base`. It clones the upstream repo at the base commit, deletes future history, installs dependencies and disables git hooks.
  - `tests/`: `test.patch` (hidden tests plus an `/app/test.sh base|new` runner), `test.sh`, `grader.py` (shared by every task) and `config.json` (fail-to-pass and pass-to-pass node-id whitelists).
  - `solution/`: the reference patch, never used in grading.
- **Scoring** uses Harbor's separate verifier environment. A `[[verifier.collect]]` hook runs `git diff --binary <base> HEAD > /logs/artifacts/model.patch` in the agent container, so **only committed work is graded**. Then a pristine container applies the model patch, resets and applies `test.patch`, runs the base and new suites, and writes `reward.json`:
  - `reward` is 1 if and only if every fail-to-pass test passes and no pass-to-pass test fails;
  - `f2p`, `p2p` and `partial` are fractions.

  `test.sh` also lists "cheating signal" files (runner configs, conftest, lockfiles) and out-of-scope paths. These are recorded only and don't affect the score.
- **Running:** DeepSWE's leaderboard used Pier (`datacurve-pier`, 0.3.1 on PyPI, a Harbor fork) with `mini-swe-agent` on Modal. Pier installs the agent CLI inside the task container and gives it a per-agent network allowlist.

## 2. The 45 longest tasks

Command: `uv run python -m compaction.deepswe.trials data/raw/sprint/deepswe/trials.json --tasks data/raw/sprint/deepswe/repo/tasks --images data/raw/sprint/deepswe/images.json --out data/raw/sprint/deepswe/top45.json`.

- **Inputs:** `trials.json` was downloaded 2026-10-20T06:30Z (51.0 MB, sha256 `310cb428…2d2ce6`). It has 31,617 rows: 113 tasks × 70 (model, effort) configs × up to 4 rollouts, all mini-swe-agent with no compaction. A full grid would be 31,640 rows, so 23 rollouts are absent: 20 of the 7,910 task–config cells have 1–3 rollouts instead of 4. Separately, 2,358 rows are present but have no `peak_context_tokens`: all 2,260 gpt-6-astra rows, 92 Claude Fable 5 rows and 6 others.
- **Ranking:** tasks are ranked by min(DeepSeek V4 Flash max median, luna 5.6 max median). Each cell below is over 4 rollouts, so the per-task quantiles are coarse. The exceptions are absent records, not missing peaks: every selected row of these five configs has a peak. In the top 45, luna max has 176 rollouts (3 on dynamodb-toolbox-conditional-attribute-requirements, and only 1 on dynamodb-toolbox-lazy-recursive-schemas, hence its [417, 417]), GLM max has 179 (3 on koota-composite-trait-aspects) and Opus max 177 (3 each on dynamodb-toolbox-lazy-recursive-schemas, opa-template-string-reconstruction and scriggo-method-declarations). The "All 70 configs" row counts the 11,650 of the top 45's 12,588 rows that have a peak.
- **Output:** `top45.json` has sha256 `251713f0…1950b`.
- **Unit:** peaks are in thousands of tokens. ">256k" is the share of rollouts above 256,000. "Turns" is the median `n_agent_steps`.

**Pooled over the 45 tasks:**

| Config | n | Median peak | IQR | Share over 256k | Median turns | Median wall-clock | Median cost per rollout | Pass rate |
|---|---|---|---|---|---|---|---|---|
| DeepSeek V4 Flash, max | 180 | 242k | 215–266k | 32.8% | 184 | 26.9 min | $0.13 | 51% |
| GPT-5.6 luna, max | 176 | 251k | 227–321k | 47.2% | 123 | 21.7 min | $3.34 | 64% |
| GLM 5.3 Flash, max | 179 | 175k | 147–204k | 6.7% | 152 | 31.5 min | $0.62 | 61% |
| Claude Opus 5, max | 177 | 257k | 227–291k | 51.4% | 123 | 37.4 min | $15.02 | 72% |
| GPT-5.6 luna, xhigh | 180 | 181k | 154–220k | 13.9% | 86 | — | — | — |
| All 70 configs | 11,650 | 163k | — | 16.4% | — | — | — | — |

Over all 113 tasks, the recomputed figures match the proposal's table:

| Config | Median peak | Share over 256k | Median steps |
|---|---|---|---|
| DeepSeek V4 Flash, max | 201.6k | 13.5% | 148 |
| luna 5.6, max | 201.6k | 20.1% | 92.5 |
| GLM 5.3 Flash, max | 136.2k | 3.1% | 110 |
| Opus 5, max | 215.9k | 24.3% | 91 |
| luna 5.6, xhigh | 138.1k | 5.5% | — |
| luna 5.6, high | 90.2k | 0.4% | — |

The 45 tasks by language: TypeScript 21, Go 9, Python 9, Rust 3, JavaScript 3. The longest tasks lean towards TypeScript. Images are 0.76–2.14 GB compressed (median 0.90). Together the 45 come to 45.3 GB compressed, or 11.5 GB with shared layers counted once.

| # | Task | Lang | DS V4 Flash max: median [q25, q75] | >256k | turns | luna 5.6 max: median [q25, q75] | >256k | turns | GLM 5.3 Flash max: median | >256k | All configs: median | >256k | Image GB (compressed) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | scriggo-method-declarations | go | 377 [366, 386] | 1.00 | 310 | 456 [434, 460] | 1.00 | 239 | 350 | 1.00 | 281 | 0.59 | 0.79 |
| 2 | wasmi-trap-coredumps | rust | 323 [319, 332] | 1.00 | 232 | 506 [482, 515] | 1.00 | 204 | 221 | 0.00 | 225 | 0.38 | 1.12 |
| 3 | dynamodb-toolbox-lazy-recursive-schemas | typescript | 313 [306, 318] | 1.00 | 264 | 417 [417, 417] | 1.00 | 227 | 256 | 0.50 | 230 | 0.44 | 0.90 |
| 4 | dynamodb-toolbox-conditional-attribute-requirements | typescript | 293 [264, 322] | 1.00 | 239 | 356 [348, 388] | 1.00 | 205 | 204 | 0.00 | 185 | 0.29 | 0.90 |
| 5 | koota-pair-relation-tracking | typescript | 283 [263, 296] | 0.75 | 166 | 340 [328, 353] | 1.00 | 134 | 253 | 0.50 | 223 | 0.37 | 0.88 |
| 6 | effect-sse-httpapi-streaming | typescript | 282 [271, 293] | 1.00 | 238 | 375 [360, 389] | 1.00 | 210 | 188 | 0.00 | 207 | 0.33 | 1.17 |
| 7 | koota-deferred-mutation-buffer | typescript | 283 [277, 288] | 1.00 | 170 | 280 [274, 300] | 1.00 | 117 | 209 | 0.00 | 170 | 0.22 | 0.88 |
| 8 | oxvg-structural-selector-preservation | rust | 274 [259, 299] | 0.75 | 220 | 361 [336, 381] | 1.00 | 222 | 207 | 0.00 | 197 | 0.32 | 1.03 |
| 9 | python-statemachine-state-data-scoping | python | 267 [261, 272] | 0.75 | 192 | 285 [277, 296] | 1.00 | 180 | 218 | 0.00 | 181 | 0.22 | 0.81 |
| 10 | koota-composite-trait-aspects | typescript | 263 [250, 281] | 0.50 | 139 | 330 [328, 336] | 1.00 | 143 | 263 | 0.67 | 202 | 0.33 | 0.88 |
| 11 | koota-query-predicates | typescript | 262 [258, 262] | 0.75 | 160 | 315 [295, 324] | 1.00 | 114 | 183 | 0.00 | 186 | 0.24 | 0.88 |
| 12 | pebble-durability-wait-apis | go | 260 [255, 270] | 0.50 | 226 | 352 [336, 363] | 1.00 | 206 | 134 | 0.00 | 162 | 0.13 | 0.96 |
| 13 | optique-conditional-option-dependencies | typescript | 259 [250, 278] | 0.75 | 170 | 329 [324, 336] | 1.00 | 147 | 182 | 0.00 | 179 | 0.20 | 0.82 |
| 14 | narwhals-rolling-window-suite | python | 256 [252, 273] | 0.50 | 206 | 264 [255, 274] | 0.75 | 134 | 195 | 0.00 | 173 | 0.13 | 1.21 |
| 15 | expr-try-catch-errors | go | 244 [236, 258] | 0.25 | 182 | 282 [266, 303] | 0.75 | 114 | 199 | 0.00 | 181 | 0.16 | 0.78 |
| 16 | meriyah-explicit-resource-declarations | typescript | 244 [240, 258] | 0.25 | 187 | 549 [399, 658] | 1.00 | 108 | 149 | 0.00 | 202 | 0.28 | 0.92 |
| 17 | boa-hierarchical-evaluation-cancellation | rust | 242 [231, 255] | 0.25 | 246 | 345 [327, 374] | 1.00 | 194 | 170 | 0.00 | 167 | 0.17 | 2.14 |
| 18 | valibot-recursive-schema-composition | typescript | 306 [277, 329] | 0.75 | 213 | 237 [224, 254] | 0.25 | 120 | 219 | 0.25 | 208 | 0.30 | 0.98 |
| 19 | tengo-destructuring-bindings | go | 239 [227, 246] | 0.00 | 219 | 234 [229, 243] | 0.00 | 107 | 154 | 0.00 | 154 | 0.07 | 0.77 |
| 20 | query-persist-restored-query-state | typescript | 234 [220, 240] | 0.00 | 166 | 245 [230, 252] | 0.25 | 95 | 175 | 0.00 | 163 | 0.13 | 1.58 |
| 21 | opa-template-string-reconstruction | go | 233 [227, 241] | 0.00 | 209 | 265 [254, 276] | 0.50 | 169 | 231 | 0.25 | 189 | 0.20 | 2.09 |
| 22 | testem-bail-on-test-failure | javascript | 231 [221, 242] | 0.00 | 150 | 256 [248, 259] | 0.50 | 86 | 145 | 0.00 | 150 | 0.08 | 0.85 |
| 23 | happy-dom-abort-pending-body-reads | typescript | 231 [230, 233] | 0.00 | 140 | 282 [249, 326] | 0.75 | 79 | 178 | 0.00 | 181 | 0.22 | 0.96 |
| 24 | sqlfmt-create-table-ddl-formatting | python | 243 [234, 249] | 0.00 | 161 | 230 [212, 239] | 0.25 | 106 | 195 | 0.00 | 156 | 0.17 | 0.76 |
| 25 | ink-grid-box-layout | typescript | 235 [214, 243] | 0.00 | 168 | 224 [217, 226] | 0.00 | 128 | 203 | 0.00 | 142 | 0.04 | 0.89 |
| 26 | helm-array-merge-strategies | go | 223 [209, 243] | 0.25 | 163 | 240 [231, 251] | 0.25 | 165 | 129 | 0.00 | 142 | 0.07 | 1.11 |
| 27 | gql-incremental-graphql-delivery | python | 223 [196, 246] | 0.00 | 174 | 238 [230, 251] | 0.25 | 118 | 190 | 0.00 | 153 | 0.10 | 0.81 |
| 28 | go-git-worktree-merge-conflicts | go | 224 [204, 250] | 0.25 | 155 | 221 [198, 244] | 0.00 | 104 | 178 | 0.00 | 146 | 0.07 | 0.92 |
| 29 | fastapi-implicit-head-options | python | 219 [207, 231] | 0.00 | 210 | 234 [227, 240] | 0.25 | 138 | 129 | 0.00 | 138 | 0.06 | 0.84 |
| 30 | eicrud-keyset-pagination-cursor | typescript | 218 [203, 221] | 0.00 | 160 | 231 [210, 246] | 0.00 | 92 | 128 | 0.00 | 131 | 0.02 | 1.19 |
| 31 | katex-multicolumn-array-spans | javascript | 215 [203, 231] | 0.00 | 160 | 248 [238, 253] | 0.00 | 96 | 147 | 0.00 | 155 | 0.08 | 1.02 |
| 32 | arktype-json-schema-refs-dependencies | typescript | 279 [266, 293] | 0.75 | 255 | 214 [205, 227] | 0.00 | 105 | 161 | 0.00 | 169 | 0.18 | 1.05 |
| 33 | kea-atomic-signal-selectors | typescript | 214 [205, 232] | 0.25 | 114 | 235 [227, 249] | 0.25 | 122 | 168 | 0.00 | 132 | 0.03 | 0.82 |
| 34 | true-myth-iterable-collection-combinators | typescript | 214 [210, 220] | 0.00 | 114 | 213 [196, 231] | 0.00 | 64 | 144 | 0.00 | 134 | 0.03 | 0.87 |
| 35 | helm-unified-manifest-stream | go | 213 [198, 216] | 0.00 | 190 | 273 [261, 273] | 0.75 | 187 | 122 | 0.00 | 153 | 0.14 | 1.10 |
| 36 | kysely-window-grouping-helpers | typescript | 213 [198, 223] | 0.00 | 167 | 237 [227, 247] | 0.25 | 98 | 142 | 0.00 | 147 | 0.06 | 1.00 |
| 37 | yaegi-go-embed-directives | go | 212 [198, 222] | 0.00 | 259 | 229 [226, 236] | 0.00 | 144 | 158 | 0.00 | 154 | 0.07 | 0.80 |
| 38 | mashumaro-flattened-dataclass-fields | python | 211 [204, 215] | 0.00 | 190 | 230 [221, 237] | 0.00 | 128 | 190 | 0.00 | 146 | 0.05 | 0.76 |
| 39 | kombu-virtual-queue-dead-lettering | python | 211 [201, 233] | 0.25 | 138 | 217 [215, 223] | 0.00 | 123 | 139 | 0.00 | 130 | 0.04 | 0.84 |
| 40 | vitest-duration-sharding | typescript | 210 [193, 224] | 0.00 | 157 | 231 [218, 235] | 0.00 | 98 | 156 | 0.00 | 142 | 0.07 | 1.04 |
| 41 | numba-stencil-boundary-modes | python | 245 [232, 255] | 0.25 | 192 | 209 [201, 233] | 0.25 | 144 | 156 | 0.00 | 145 | 0.07 | 1.09 |
| 42 | quill-shared-toolbar-focus | typescript | 208 [206, 216] | 0.00 | 138 | 222 [212, 244] | 0.25 | 87 | 183 | 0.00 | 157 | 0.08 | 1.61 |
| 43 | yjs-map-conflict-detection | javascript | 207 [200, 216] | 0.00 | 160 | 228 [226, 234] | 0.00 | 96 | 175 | 0.00 | 143 | 0.08 | 0.83 |
| 44 | clack-async-autocomplete-options | typescript | 204 [193, 218] | 0.00 | 120 | 223 [216, 236] | 0.25 | 71 | 126 | 0.00 | 123 | 0.03 | 0.85 |
| 45 | sqlite-utils-safe-import-checkpoints | python | 201 [183, 225] | 0.00 | 203 | 207 [194, 216] | 0.00 | 120 | 122 | 0.00 | 127 | 0.03 | 0.76 |

Implication for the threshold: with a compaction trigger at 150–200k, nearly every DeepSeek and luna rollout on these tasks crosses it, since their q25 is 215k and 227k. GLM crosses 175k in only about half its rollouts, so GLM needs a lower trigger (about 120–150k), as the proposal says.

## 3. Feasibility on this Mac

The setup is Docker 29.4.1 on an aarch64 VM with 18 CPUs, 7.7 GiB of memory and a 911 GB sparse disk, bounded by the host's 170 GB free. amd64 runs under Docker Desktop's emulation, which appears to be Rosetta given the 2× penalty; the setting itself wasn't readable.

- **Arm64 variants:** none of the 113 task images has one. Each is a single amd64 manifest (`uv run python -m compaction.deepswe.registry`, public ECR API, 24 s, `images.json` sha256 `eee37dec…e7e257`). `mars-base:latest` has both linux/amd64 and linux/arm64.
- **Pulled:**
  - `koota-pair-relation-tracking` (rank 5, TypeScript, 0.88 GB) in 20 s;
  - `wasmi-trap-coredumps` (rank 2, Rust, 1.12 GB) in 11 s;
  - mars-base (arm64 and amd64, 0.75 GB) for the benchmark.

  That is about 2.8 GB of pulls in all, against the cap of 30 GB. On disk, the two task images added 6.0 GB together, including the shared base. Plan on about 100 GB if all 45 are kept unpacked.

**Phase timings** (`uv run python -m compaction.deepswe.feasibility <task>`, oracle patch standing in for the agent; the first runs used an earlier version with a `--nop` flag, and the empty patch is now always graded):

| Phase | koota (amd64, emulated) | wasmi (amd64, emulated) | koota (native arm64 rebuild) |
|---|---|---|---|
| Container start | 0.13 s | 0.4 s | 0.1 s |
| Apply and commit the solution | 0.27 s | 0.5 s | 0.1 s |
| Collect `model.patch` | 0.24 s | 0.2 s | 0.1 s |
| `docker commit` snapshot | 2.9 s (diff layer ~0.1 MB) | 3.2 s | 5.6 s |
| Fork from the snapshot | 0.09 s (HEAD = oracle commit) | 0.1 s | 0.1 s |
| In-loop `test.sh base` / `new` | 14.4 s / 3.3 s | 8.3 s / 8.5 s (incremental compile) | 3.0 s / 0.7 s |
| Grade the oracle patch (fresh verifier container) | 19.7 s, reward 1 (38/38 f2p, 172/172 p2p) | 23.6 s, reward 1 (22/22, 58/58) | 4.2 s, reward 1 |
| Grade an empty patch | 19.2 s, reward 0 (f2p 0/38) | 7.2 s, reward 0 (f2p 0/22) | 4.0 s, reward 0 |
| Peak memory during the base and new suites | 0.51 GB (idle 14 MB) | 0.17 GB (idle 13 MB) | — |

- **Emulation penalty,** measured by the same microbenchmark in mars-base on arm64 and amd64:
  - Python loop: 0.69 → 1.58 s (2.3×);
  - Node loop: 0.42 → 0.88 s (2.1×);
  - SHA-256: 0.67 → 3.33 s (5×);
  - 300 process spawns: 0.13 → 3.8 s (about 30×, about 13 ms per spawn).
- **Native arm64 rebuild:** building koota's `environment/Dockerfile` for arm64 took 16 s, and the oracle and empty patches graded the same, so local rebuilds are possible. But the dependency installs aren't all pinned (Python tasks run `pip install -r requirements.txt`), and the verifier whitelists were derived on the amd64 image. A rebuilt task image would need an oracle=1 / nop=0 check per task, and grading should stay on the official amd64 image.
- **Per-run overhead:** about 1 minute per run on top of the agent's own time:
  - the first pull of a task image, 10–20 s;
  - start, snapshot and fork, under 5 s;
  - grading, 20–25 s emulated or about 5 s native.

  The agent's own time is 22–32 min median per full rollout in DeepSWE's runs, and 9–12 s of model time per step. Emulation's main cost is the agent's own test runs: 3–18 s each emulated against 1–4 s native. At one run every 5–10 steps, that adds an estimated 5–15% of wall-clock (an inference from the two tasks measured; Go and Rust cold builds weren't timed).
- **Schedule estimate (inference).** Continuations follow the proposal's design: 4 arms × 2 continuations × 20 calls each ([proposal](../compaction-proposal.md), the "Four-way continuations" cost row).
  - **Per task and model:** a prefix to the first compaction (about 70% of a rollout, about 19 min); 8 continuations of 20 calls at 9–12 s each (3–4 min each, about 28 min); 8 gradings at about 0.5 min emulated (about 4 min). That comes to about 0.85 h of agent time.
  - **Core design:** 45 tasks × 4 models (180 pairs) gives about 150 agent-hours. With the repeated-compaction chains and permission traps (+50%), about 230 agent-hours.
  - **Laptop:** 8–12 concurrent sandboxes in the 8 GB VM, with other agents running. The test suites peak at 0.2–0.5 GB, but Go and Rust builds will be higher. That gives about 13–19 h wall-clock for the core design, or 19–29 h with the +50%: one to two days with the lid open.
  - **32-core / 128 GB amd64 box:** about 40 concurrent, giving about 4–6 h wall-clock.
  - **Sensitivity case, not the plan:** if each continuation instead ran to the task's end (about half a rollout, 13 min), a pair would take about 2.1 h. That gives about 380 agent-hours (570 with the +50%), 50–70 h on the laptop and about 15 h on the 40-concurrent box.

## 4. Plugging in our own agent loop

**Recommendation: P49-style loop, with DeepSWE containers as the sandbox.** Two options were considered:

- **Harbor/Pier with a custom agent.** A custom agent is possible through `--agent-import-path module:Class`, subclassing `BaseAgent`, and it can `environment.exec(...)`. But:
  1. Pier's built-in agents (mini-swe-agent, claude-code, codex, opencode, gemini-cli, antigravity-sdk) run inside the container, with a network allowlist to the provider.
  2. Pier's environment API has start, stop, exec, upload and download, but no snapshot or fork. Per-arm forks would mean reaching around Pier to the Docker daemon, and that would not work on Modal or Daytona anyway.
  3. One trial is one run from the task's start. Resuming several arms from a compaction point doesn't fit its trial model.
- **Our loop, as in P49 (`loop.py`, `client.py`, `compact.py`, `mockserver.py`, `replay.py`).** It already has the pieces the arms need:
  - forked calls;
  - Codex, pi and Inspect summarizer reproductions;
  - a mock server for free end-to-end tests;
  - spend guards;
  - a sandbox lifecycle that follows the container rules.

  What changes is `shim.py`'s X3 sandbox and OpenHands tools. These become `compaction.deepswe.sandbox.Sandbox` and mini-swe-agent's tool set.

**Interface** (already implemented and tested in `src/compaction/deepswe/sandbox.py`):

```python
sb = Sandbox.start(task["docker_image"], purpose, cpus=2, memory_mb=8192,
                   platform="linux/amd64", network="none", lifetime_s=4*3600)
sb.exec(cmd, timeout_s) -> ExecResult(rc, stdout, stderr, seconds)  # bash -lc under timeout -k 5
inv = Invocation(purpose)   # per-invocation run id; labels emx.run=<id>; cleans only its own
sb = inv.start(...)         # Sandbox.start with the run label
sb.stray_processes()        # ps lines that would break a quiescent boundary ([] = quiescent)
sb.snapshot(inv.new_tag("compact1"))  # refuses unless quiescent; docker commit --pause: filesystem only
inv.start(tag, ...)         # an independent fork for each arm × repeat
inv.cleanup()               # removes this run's containers and images; returns any not confirmed
patch = collect_patch(sb, task["collect_command"])               # the task's own collect hook
reward, _ = grade(task_image, task_dir / "tests", patch, purpose, run_id=inv.run_id)
# fresh pristine verifier; raises VerifierError when no valid reward exists (infrastructure
# failure: no reward file, the -1 sentinel, malformed or inconsistent counts), never reward 0
```

**Run protocol for one task and model:**

1. Start from the task image, with the system prompt plus `instruction.md` plus the rules we add.
2. Run until context ≥ the frozen threshold, at the end of a step. Check the boundary is quiescent; if not, apply the rule below. Then `snapshot()` and save the message list. Run the vendor summarizer (Codex, Claude Code or Antigravity style) on the saved messages.
3. For each arm × repeat, fork the container and continue the loop from:
   - (a) the full history,
   - (b) the summary,
   - (c) the summary plus the lost rule,
   - (d) the summary plus a matched irrelevant fact.
4. At the end, `collect_patch`, then `grade`. Rule checks run on the patch, the action log and filesystem diffs.

**Design notes for the lead:**

- **Tool set.** Use mini-swe-agent's single bash action and its observation truncation, copying its `mini.yaml` templates (MIT). DeepSWE's peak-context numbers came from that tool set, so it keeps our context growth comparable to the table above. Add only `ask_user` (returns "pending") for Q3. P49's OpenHands editor tools would change context growth.
- **Commits.** Only committed work is graded. Record whether the agent committed, then have the harness commit any leftover work before collecting. Otherwise "forgot to commit" confounds the arms.
- **Traps.** Containers have no network, and `origin` was removed at build time. For Q3's force-push and branch-deletion traps, add a local bare repo as `origin` (`git init --bare /srv/origin.git`) before the run, with a receive hook that appends every ref update (old SHA, new SHA, ref name) to a separate event log, as unit (d)'s traps do (`setup_mock_remote`'s post-receive hook and push log, [d-traps.md](d-traps.md)). Don't rely on the bare repo's reflog: reflogs are off by default in a bare repo, and deleting a branch deletes its reflog. Attempts that change nothing (a rejected lease, a dry run) come from the shell-action log, not the hook.
- **What a fork carries, and the quiescence rule.** `docker commit` captures only the filesystem. Running processes, their memory and any shell state are not carried. Mini-swe-agent's bash is stateless (each action is a fresh shell), but an action can still background a server, a watcher or a build with `&` or `nohup`, so stateless bash alone does not make forks faithful. The sandbox therefore enforces a quiescent boundary: `snapshot()` lists processes with `ps` and refuses (`NotQuiescentError`) if anything runs besides the container's own `timeout … sleep infinity` and the check itself. Rules for the loop:
  1. At the compaction point, if the check lists processes, stop them in the source container before the snapshot, and log what was stopped. Every arm then forks from the same stopped state. The alternative is to move the boundary to the next step at which nothing runs; pick one in the plan.
  2. Services are not restarted in a fork. An arm that needs a server has to start it again, as the agent would after any restart.
  3. Files that a stopped or exited process left behind (pid files, sockets, lock files, partial build outputs) are carried identically into every fork. They are the same for all arms, but may differ from what an uninterrupted run would have seen.
  4. A fork also gets a new hostname (the container id) and a fresh process start time.

  Tested in the Docker test: a backgrounded `sleep 300` makes the snapshot refuse; after it is killed, the snapshot and the fork both pass the check. Not yet measured: how often agents leave background processes at a step boundary. Count it in the pilot. Large build outputs (`target/`, `node_modules` changes) make commits slower; 3–6 s was measured on small diffs.
- **Vendor CLIs.** Pier drives claude-code, codex and **antigravity-sdk** (the `google-antigravity==0.1.9` Python SDK) headlessly inside the container. The antigravity-sdk agent takes a `GEMINI_API_KEY`, not OpenRouter. This is relevant to the DM's question 2 for J: Pier shows Antigravity's agent can be scripted. Whether the SDK compacts, and how, is untested. For vendor-native compaction, the option is to run the vendor CLI in the container until its first compaction event and take the summary text from its session log (Pier's claude_code and codex parsers already detect `compact_boundary` and `compacted` events). Then snapshot and continue the arms in our loop.

## 5. Where to run

| Option | Concurrency | Speed | Cost | Notes |
|---|---|---|---|---|
| Laptop, amd64 emulated | about 8–12 (8 GB Docker VM, shared with other agents) | tests 2–5× slower; total +5–15% | $0 | Closing the lid pauses everything. About 100 GB of the 170 GB free for 45 images. Good for the pilot (a handful of tasks). |
| Cloud amd64 VM (e.g. 32 vCPU / 128 GB / 300 GB SSD) | about 40 | native | rough list prices, **unverified**: AWS c7i/m7i.8xlarge about $1.4–1.6/h on demand; Hetzner dedicated about $0.5/h (monthly billing) | About 4–6 h for the whole bulk with 20-call continuations, so about $10 on AWS (about 15 h and $25 in the sensitivity case). RunPod pods can't run Docker inside, so RunPod doesn't fit. Modal (what DeepSWE used) has filesystem snapshots but would be a new account and is unpriced here. |
| Jacob's desktop over Tailscale | depends on specs | native if x86-64 | $0 | Needs an x86-64 CPU with at least 16 cores and 64 GB RAM (about 25 concurrent sandboxes), 250 GB free SSD, and Docker Engine on Linux (Docker Desktop with WSL2 also works). An Apple-silicon desktop gives nothing over the laptop. Drive it over SSH with the same `compaction.deepswe` code; `DOCKER_HOST=ssh://desktop` also works for the sandbox module. |

**Recommendation:**

- Run the pilot on the laptop now: 3–5 tasks, emulated, at most 8 concurrent.
- Run the bulk on Jacob's desktop if it meets the specs above, otherwise on one amd64 cloud VM for a day at most, for well under $50. The API spend is about 10–50× larger than the compute.
- Keep grading on the official amd64 images in every case.

## Commands and timings (UTC)

| UTC | Step | Wall-clock |
|---|---|---|
| 06:29 | `git clone https://github.com/datacurve-ai/deep-swe data/raw/sprint/deepswe/repo` | a few s |
| 06:30:23 | `curl -o trials.json https://deepswe.datacurve.ai/artifacts/v1.1/trials.json` (51,023,870 bytes) | about 2 s |
| 06:32 | `uv run python -m compaction.deepswe.registry --tasks …/repo/tasks --out …/images.json` | 24 s (the first try hit ECR's 429 limit, so 3 workers with backoff were added) |
| 06:33:18 | `docker pull --platform linux/amd64 …:kh7cshjqfe84fmtb2ye37nrd6h82dds6-v1.1` (koota) | 20 s |
| 06:34:59 | `uv run python -m compaction.deepswe.feasibility …/tasks/koota-pair-relation-tracking --out …/logs/feas_koota_amd64.json --nop` | 61 s |
| 06:36:19 | pull of wasmi (`…:kh70h37djmvc9ac48jeh25gwz582xgqv-v1.1`), then the feasibility run | 11 s + 53 s |
| 06:38 | microbenchmark of native and emulated mars-base (`scratchpad/bench_outer.sh`) | about 1 min |
| 06:39:08 | `docker build --platform linux/arm64 -t emx-sprintprep/koota-native:arm64 …/environment` | 16 s (image removed afterwards) |
| 06:40:25 | feasibility on the native image (`--image … --platform linux/arm64 --nop`) | 19 s |
| 06:41 | memory peaks of the base and new suites (cgroup `memory.peak`) | 21 s |
| 07:45:52 | after the review-r1 fixes: `uv run python -m compaction.deepswe.feasibility …/tasks/koota-pair-relation-tracking --out …/logs/feas_koota_amd64_r1fix.json` (amd64, emulated) | 63 s, exit 0: oracle reward 1 (38/38 f2p, 172/172 p2p), empty patch reward 0 (0/38 f2p), in-loop suites rc 0, snapshot removal confirmed, cleanup confirmed |
| 07:49 | the same run sent SIGTERM 10 s in (`…/logs/feas_koota_sigterm_check.json`) | exit 143, stopped during the in-loop suite in the fork; cleanup confirmed, no container or snapshot image left |
| 07:56 | after the review-r2 fixes: a live `snapshot()` on mars-base with the commit's client timeout forced to 0.05 s (scratchpad script `commit_timeout_check.py`) | the source container was removed and confirmed, the commit settled with no image left, `Invocation.cleanup` confirmed nothing of the run remained (under the r2 rule, since withdrawn) |
| 08:01 | after the review-r3 fix: the same script | about 60 s of polling; no image appeared (the container's removal cancelled the commit), so the commit was reported unresolved with its sweep command; `docker ps` and `docker images` by the run label then showed nothing |

Tests: `uv run pytest -q tests/compaction/test_deepswe.py` gives 39 passed and 1 skipped, in place (`tests/compaction/__init__.py` has since been removed). With `EMX_DOCKER_TESTS=1` the Docker test also passes (9 s on mars-base, native arm64): quiescence refusal, file helpers, snapshot, fork and run-scoped cleanup.

- **Cleanup.** After the runs, `docker ps -a --filter label=emx.sandbox` lists nothing of ours (only another agent's `inspect-…` container, labelled `sprint-spike`), and no `emx-sandbox/…` snapshot image is left. The two task images and mars-base are kept for the pilot; `docker image rm` frees them.

## Review response (r1–r3)

Round 1 by gpt-6-astra: `logs/reviews/20261020-sprint-prep-c-r1.md` (FIX BEFORE COMMIT; 6 BLOCKING, 2 MINOR). Round 2: `logs/reviews/20261020-sprint-prep-c-r2.md` (FIX BEFORE COMMIT: r1 #2, #3, #5–#8 resolved; #1 and #4 partly, as r2 #1 and #2; one new MINOR, r2 #3). Round 3, the last: `logs/reviews/20261020-sprint-prep-c-r3.md` (FIX BEFORE COMMIT: r2 #1 and #3 resolved; r2 #2 still BLOCKING, as r3 #1). The r3 fix below was made after the final review round and has not been re-reviewed.

| # | Finding | Severity | Response (r1) | r2 | r3 (fixed after the final review round; not re-reviewed) |
|---|---|---|---|---|---|
| 1 | Cleanup misses interruptions and file-operation timeouts | BLOCKING | Fixed. `Sandbox.start` removes the named container on any exception: a timeout, a failed start, KeyboardInterrupt or SystemExit. Every operation inside the container (`exec`, `put_file`, `read_file`, and `copy_in`, now a tar stream into an exec rather than `docker cp`) runs under `timeout -k 5` and goes through one guard that removes the sandbox on a client timeout or an interruption. `feasibility` turns SIGTERM into SystemExit so cleanup runs. Tests cover 5 operations × {KeyboardInterrupt, client timeout}, plus start × 3 failure kinds. Live check: SIGTERM 10 s into a koota run gave exit 143 with cleanup confirmed. | Partly resolved in r1 (r2 #1): a commit timeout or interrupt removed only the image. Fixed: `snapshot()` now also removes the source container and confirms it, and the regression test requires the container removal for every operation, the commit included. | — |
| 2 | Cleanup can falsely confirm success | BLOCKING | Fixed. `remove_container` and `remove_image` confirm only through a listing that returned rc 0 and doesn't contain the exact name or tag. They retry for up to about 10 s while `--rm` removal is in progress. A failed or timed-out query returns False and prints the name. Mocked tests cover the daemon being down. | Resolved. | — |
| 3 | One feasibility run can kill another | BLOCKING | Fixed. `Invocation` gives each run an id and labels its containers and snapshot images `emx.run=<id>`, in addition to `emx.sandbox=<purpose>`. It cleans only that label; the purpose-wide sweep is gone. A test asserts that cleanup queries filter only on the run's own label. | Resolved. | — |
| 4 | Snapshot cleanup starts too late | BLOCKING | Fixed. `Invocation.new_tag` makes a unique tag and tracks it before the commit. `snapshot()` removes the tag on any failure or interruption. `feasibility` removes the snapshot with confirmation (`snapshot_removed`), and `Invocation.cleanup` also removes any image carrying the run label that is present when it runs. The Docker test now uses the same structure. | Partly resolved in r1 (r2 #2): one discovery pass could miss a commit that the daemon finished later, so r1's "catches a commit that finished after the client gave up" overclaimed. Fixed: an interrupted or timed-out commit stays unresolved until `resolve_commit` establishes completion (the image appears and is removed, or a `commit` event was logged and the tag is then absent) or cancellation (`destroy` logged without `commit`, tag absent on two polls). It polls for about 60 s; if neither is established, the tag stays in `unresolved_commits`, `Invocation.cleanup` retries it and then reports it as unconfirmed. Cancellation-by-destroy is an inference from Docker's model, stated as such. Tests: an image that appears three polls late is removed; no events, or events unavailable, gives unresolved. Live check: a commit given a 0.05 s client timeout was settled, with no container or image left. | Still BLOCKING in r3 (r3 #1): the destroy-based cancellation rule was unsound, since the daemon exports the filesystem before it creates and tags the image, so removing the container doesn't prove the commit was cancelled. Fixed: the destroy and commit-event success conditions are removed. An interrupted or timed-out commit is resolved only when its image is observed and removed; otherwise it stays in `unresolved_commits` and cleanup reports it as unconfirmed. A late sweep (`sweep(run_id)`, run by `Invocation.cleanup` and as `python -m compaction.deepswe.sandbox sweep --run-id <id>`) removes any container or image carrying the run's label, untagged ones included, and any `emx-sandbox/*` tag with the run id. Tests: the reviewer's case (destroy on polls 1–2, image on poll 3) is not resolved at poll 2 and the image is removed when it appears; a late-completing image is removed by a rerun of cleanup and by the CLI sweep, and another run's image is left alone. Live: a commit with a 0.05 s client timeout was cancelled by the container's removal, no image appeared in 60 s, and cleanup reported it as unresolved with the sweep command; the sweep found nothing left. |
| 5 | Fork fidelity is overclaimed | BLOCKING | Fixed in code and text. `snapshot()` refuses unless `ps` shows only the sandbox's own processes and the check itself. §4 now states the restriction and the loop's rules (stop or wait, no service restarts, leftover files carried identically). The Docker test shows a backgrounded process blocks the snapshot. How often agents leave background processes is to be counted in the pilot. | Resolved. | — |
| 6 | Failed feasibility checks can finish successfully | BLOCKING | Fixed. `parse_reward` treats a missing reward file, the `reward.txt` −1 sentinel, malformed JSON, missing or negative counts, and a reward inconsistent with its counts as infrastructure failures (VerifierError). A reward of 0 with valid counts is a task failure. `feasibility` fails on a solution or test patch that doesn't apply, an empty oracle patch, or a fork HEAD that isn't the oracle commit. It always grades the empty patch. Exit codes: 1 unless oracle=1 and nop=0, 2 for setup or infrastructure failure, 3 when cleanup isn't confirmed, 130 on interrupt. The JSON is written in every case, with its diagnostics. Rerun on koota: oracle 1, nop 0, exit 0. | Resolved. | — |
| 7 | Schedule uses a different continuation workload | MINOR | Fixed. §3 now uses the proposal's 20-call continuations: about 0.85 h per task and model, about 150 agent-hours for the core design and 230 with the +50%. The 380/570 figure is kept as a labelled sensitivity case. | Resolved. | — |
| 8 | Missing-rollout explanation is inaccurate | MINOR | Fixed. §2 now states that 23 rollouts are absent (20 task–config cells with 1–3 rollouts), separately from the 2,358 rows without a peak. The luna, GLM and Opus counts in the top 45 (176, 179 and 177) are absent records, all with peaks. | Resolved. | — |
| r2 #3 | The bare remote's reflog won't record permission violations | MINOR | — | Fixed: §4's trap note now specifies a receive hook writing old SHA, new SHA and ref name to an event log, as unit (d) does, and says the reflog is not used. | Resolved. |
