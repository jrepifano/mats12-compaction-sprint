SWE-BENCH PRO: PARTIAL, likely FEASIBLE · 25 of 25 images pull without credentials (amd64, 0.5–4.5 GB). The reference solution is reachable in git history and must be stripped. The verifier ran outside Harbor on 1 task (base 0, gold 1, with and without history). **Integration ≈ 6–9 h**, plus a base/gold check of all 25 before calling them usable. Usable now: 1 verified, 25 expected.

SWE ATLAS REFACTORING (RF): PARTIAL · 25 of 25 images pull without credentials (amd64, 0.28–6.7 GB). No leak: single "init" commit. Grading is tests **and** an LLM rubric (Opus 4.5 by default, paid). The tests half was vacuous on the 1 task I ran. **Integration ≈ 12–18 h**, plus per-task grader validation on an amd64 host. Usable now: 0 verified, an unknown number expected.

# Can the other 50 SWE-Interact tasks run in our harness?

2026-10-24, worker for the S2 coordinator, brief `logs/s2/briefs/swe-interact-50-feasibility.md`.

- **Spend:** $0. No paid calls.
- **Commits:** none.
- **Code:** unchanged.
- **Image pulls:** 2 per family (ansible-4c5ce5a1, qutebrowser-deeb15d6, RF scapy-118a1, RF k6-b446), all removed afterwards with `docker rmi`.
- **Containers:** every one ran with `--rm`, a name, `--label emx.sandbox=s2-feas50`, `--network none`, `--platform linux/amd64` and an in-container `timeout`. `docker ps -a --filter label=emx.sandbox=s2-feas50` is empty at the end. The running pilot was not touched.
- **Sources:** SWE-Interact @ `b32f98c3` (`data/raw/sprint/swe_interact/repo/`, pin in `logs/s2/swe_interact_feasibility.md`) and Harbor @ `6099b8cf`.
- **Path shorthand:** `MT/` means `data/raw/sprint/swe_interact/repo/data/multiturn/`.

## What changes for the accumulated-loss plan

- **SWE-bench Pro is the cheaper family to add.**
  - It has one layout for all 25 tasks: `/app`, one step-instruction text, a pure test verifier.
  - Its personas hold the longest hidden specs: median task block 849 words, against 405 for DeepSWE.
  - 16 of 25 personas name keep/preserve rules, which suits Q2.
  - **Blockers, all mechanical:**
    - the history leak;
    - `ENTRYPOINT ["/bin/bash"]` on the images;
    - a `reward.txt`-only verifier, which our `parse_reward` rejects;
    - the plan file is saved inside the repo (`/app/plan.md`).
- **RF has the best Q2 content but the worst grading.**
  - Its personas hold concrete preservation rules that govern later edits ("the underlying Sum field must remain exported", "Keep the public API stable").
  - **Grading:**
    - The reward needs an LLM rubric.
    - The tests half did not test anything on the task I ran.
    - Each task uses its own repo path (9 different paths).
    - Toolchains depend on the login vs non-login shell.
    - Builds are heavy C/C++/Go/TS, which are slow under emulation on the laptop.
  - Plan it for the desktop amd64 host, and validate each task before use.
- **Session length is unknown for both families.**
  - The paper gives only pooled numbers.
  - The repo proxies point in both directions (§5).
  - A short pilot is the only way to learn whether they reach several compactions at 150k.

## 1. Images

**Image references** (from the Dockerfile `FROM` line):

- **SWE-bench Pro:** `MT/swebenchpro_*/environment/Dockerfile:15`, `FROM jefzda/sweap-images:<repo>-<instance>` (Docker Hub, by tag, not digest).
  - `task.toml` has no `docker_image` (`MT/swebenchpro_instance_ansible__…/task.toml:65-71`), so Harbor builds the Dockerfile.
  - The build runs `git reset --hard <base>` (`Dockerfile:27`), installs uv, and copies `repo_exec_server.py`.
  - Some tags are cut to Docker's 128-character limit, e.g. teleport `…-vee9b09fb20c43af7e520f57e9239bbcf46b7113` without its final `d`. They still resolve.
- **RF:** `MT/rf_*/environment/Dockerfile:1` and `task.toml` `docker_image`, `FROM ghcr.io/scaleapi/swe-atlas:swe_atlas_RF_<repo>_<id>_1.0@sha256:…` (pinned by digest).
  - The Dockerfile adds `openai` and `python-dotenv` for the rubric verifier (`Dockerfile:9-22`).

**Public, pullable and architecture.**

- **Manifests:** I fetched all 50 manifests anonymously: a registry token with no credentials, then a manifest GET; `~/.docker/config.json` has no auths. Script: scratchpad `manifests.py`.
- **Result:** **50 / 50 resolved.** Every image is single-platform **linux/amd64**, with no arm64 variant.
- **Pulls:** the 4 pulls took 8–21 s each.

| family | images OK | arch | compressed size, median [min, max] | total |
|---|---|---|---|---|
| SWE-bench Pro | 25/25 | linux/amd64 | 0.96 GB [0.50, 4.51] (protonmail webclients is the largest) | 31.3 GB |
| RF | 25/25 | linux/amd64 | 2.01 GB [0.28, 6.66] (wp-calypso, grafana and MariaDB are the large ones) | 61.6 GB |

**Emulation on the laptop** (arm64, Docker 29.9.2) works. All four images reported `uname -m` = `x86_64` and ran:

- **ansible:** the full SWE-bench Pro verifier in 10 s.
- **scapy:** the full RF validator in 23–105 s.
- **k6:** `go build ./...` in 74 s.

The heavy RF builds were not tried under emulation (MariaDB C++, netdata, suricata, grafana, calypso). They will be slow on the laptop, so use the desktop amd64 host.

**Two image facts that break our sandbox as it stands:**

1. **Entrypoint (SWE-bench Pro).** The `jefzda/sweap-images` images set `ENTRYPOINT ["/bin/bash"]` (`docker image inspect`: `EP=[/bin/bash]`).
   - Harbor's Dockerfile resets it with `ENTRYPOINT []` (`Dockerfile:18`).
   - Our `sandbox.run_args` appends `timeout -k 5 <s> sleep infinity` as the command (`src/compaction/deepswe/sandbox.py:245`). On these images that becomes `/bin/bash timeout …`, which exits at once.
   - **Fix:** pass `--entrypoint ""`, or build a derived image (which we need anyway for the strip in §2).
   - RF images have `EP=[]`.
2. **Login shell vs image PATH (RF).** Our `exec_args` always runs `bash -lc` (`sandbox.py:250-254`). Harbor's Docker backend runs `bash -c` (`harbor/src/harbor/environments/docker/docker_unix.py:307-309`).
   - **k6:** the image ENV puts `/usr/local/go/bin` on PATH. Under `bash -lc`, `/etc/profile` resets PATH and `go` is not found.
   - **scapy, the opposite case:** under `bash -c`, `python` is `/opt/venv/bin/python` 3.12.13, and `import scapy.modules.six.moves` fails. Under `bash -lc`, it is `/usr/local/bin/python` 3.8.20, and the import works.
   - So each RF image needs its shell mode (or PATH) chosen and checked.
   - The SWE-bench Pro ansible image's ENV PATH is plain, so no issue was seen there.

## 2. Reference-solution leak

**SWE-bench Pro: leaks.** I checked both images.

- **ansible-4c5ce5a1:**
  - `HEAD` is the base `8a175f59`.
  - The repo is a full upstream clone (reflog: `clone: from https://github.com/ansible/ansible.git`) with 698 refs, 636 tags and 67,370 commits.
  - `git log --all` reaches 6,322 commits newer than HEAD, up to `devel` @ `2bed98bd` (2025).
  - The solution commit `4c5ce5a1` is present and on `devel`. `git show --stat 4c5ce5a1` prints "module compat for py3.8+ controller (#73423)…", which is the reference fix.
- **qutebrowser-deeb15d6:** the same pattern.
  - `HEAD` is `3ae5cc96`, with 143 refs and 27,000 commits.
  - `deeb15d6` is reachable from `main`, `origin/main` and `origin/dev`.
- **The agent can reach it.** The agent's shell can run `git log --all` / `git show <sha>`. The simulator's allowlist also includes `git log`/`git show` (prior report §2).

**The official verifier depends on the leak.** `canonical_test.sh` (`MT/swebenchpro_instance_ansible__…/steps/05_test_handoff/tests/canonical_test.sh:43-57`) runs the last line of `config.json`'s `before_repo_set_cmd`, which is `git checkout <solution sha> -- <test files>`. That only works while the solution commit is in the object store. A naive strip makes every run fail that step and score 0.

**How to strip, checked on ansible:**

1. **Squash the repo.** Reset to base, `rm -rf .git && git init && git add -A && git commit -m init`. This is what the RF images ship with. A gentler alternative: delete every ref except a detached base, then `reflog expire --expire=now --all` and `gc --prune=now`.
   - Result: 1 commit, `git cat-file -t 4c5ce5a1` reports nothing, `.git` is 31 MB.
2. **Replace the gold checkout.** Set `before_repo_set_cmd` to empty and `git apply` the config's own `test_patch` before running `canonical_test.sh`.
3. **Re-check base and gold.** Unchanged code got reward **0** (8/17 required tests). Gold patch + test_patch got reward **1** (17/17). Each run took 10–11 s.

This needs a derived image per task (25 builds, ~31 GB of pulls), plus that base-0/gold-1 check per task. It is the same deviation as our DeepSWE v1.1 images, logged the same way.

**RF: no leak.**

- In both images (scapy `/app`, k6 `/workspace`) `git rev-list --all` = 1, the only ref is `refs/heads/master` = `init`, and the reflog shows only `commit (initial): init`.
- `/tests` and `/solution` are absent from the image.
- k6 `git count-objects -v`: 3,306 loose objects, 0 packs.
- `canonical_test.sh:37-38` relies on this: "Instance dockers have a squashed single-commit repo; use HEAD as base".

## 3. Verifiers

**SWE-bench Pro: pure tests, works without Harbor.**

- **Step-05 `test.sh`** is the same as DeepSWE's except `cd /app` (`diff` with the RF copy: only line 59 differs, `/app` vs `/workspace`). It works like this:
  1. require a clean tree and `/tmp/base_commit.txt`;
  2. take the test commit `HEAD` and the implementation commit `HEAD~1`;
  3. `reset --hard impl`, `reset --soft base`, `reset`, so the implementation is left as uncommitted changes on base;
  4. `exec canonical_test.sh`.
- **`canonical_test.sh`:**
  1. the gold test checkout (§2);
  2. the official `run_script.sh` with `selected_test_files_to_run`;
  3. the official `parser.py` → `/tmp/output.json`;
  4. pass iff every `fail_to_pass ∪ pass_to_pass` test is `PASSED` (`canonical_test.sh:105-173`).
- **Reward format:** it writes only `/logs/verifier/reward.txt` (0/1) via its EXIT trap (`:22-33`). Our `parse_reward` treats `reward.txt` as a crash sentinel and requires `reward.json` with f2p/p2p counts (`sandbox.py:531-560`). So `grade_patch`/`grade_in` (`src/compaction/s2/outcomes.py:66-115`) need an adapter: compute the counts from `output.json` and the config lists, then write `reward.json`.
- **Run without Harbor** (fresh container, `/tests` mounted, `--network none`, `bash -c`): base reward 0, gold reward 1, 10 s each. It works offline.
- **Grading the final implementation commit:** the same mechanics as DeepSWE. Apply the agent's base→impl patch to a fresh stripped image, apply `test_patch`, run.

**RF: tests and an LLM rubric. Not usable as is without paid grading and per-task validation.**

- **Step-05 `test.sh`:** the same reset sequence, then `canonical_test.sh`:
  1. Capture the agent diff against HEAD.
  2. Flag edits to files listed in `test_files.json` and drop them from the scored patch (`:53-115`).
  3. Run `master_validator_script.sh --mode agent --relax`: the full suite at base, then base + agent patch + `test_patch.diff`, then compare.
  4. Compute `tests_reward` = 1 iff no **relevant** test (from `relevant_test_names.json`) is pass→fail or missing→fail (`:136-231`).
  5. Run `evaluate_rubrics.py` with `EVAL_MODEL`, default `anthropic/claude-opus-4-5-20251101` (`evaluate_rubrics.py:179`), over `rubrics.json`.
  6. **reward = 1 iff `tests_reward == 1` and every "must have" rubric item passes** (`:249-287`).
  7. The 69391d8d task has 10+ must-have items, e.g. "1.1: Extracts the local buffer implementation … into a shared package".
- **Run without Harbor:** scapy-118a1, `--network none`, no EVAL credentials, so the rubric step printed "no LLM credentials; rubric eval skipped" and no paid call happened.

  | shell | patch | suite result | relevant P2F/M2F | tests_reward |
  |---|---|---|---|---|
  | `bash -c` | none | — (an empty patch fails to apply) | — | 0.0 |
  | `bash -c` | gold | 0 baseline tests; mutation 78 done, 0 passed, 77 errors (`ModuleNotFoundError: scapy.modules.six.moves`) | 0/0 "of 38 relevant" | **1.0** |
  | `bash -lc` | none | — | — | 0.0 |
  | `bash -lc` | gold | 6,032 done (5,715 passed) both runs; full M2F = 0 | 0/0 | **1.0** |

  - **Why both gold runs scored 1.0 without real tests:**
    - Under `bash -c` the 38 new tests all errored. They were not counted as relevant because the matcher compares `TestDumpMethodSharedBase::test_packet_has_psdump` against `test_packet_has_psdump::test_packet_has_psdump` and misses.
    - Under `bash -lc` the UTscapy suite never ran the new unittest file at all.
  - **So for this task, any patch that applies scores `tests_reward` 1.0.** The reward then rests entirely on the Opus rubric.
  - This is one task. I did not check whether Harbor runs on Modal behave the same way, but Harbor's Docker backend uses `bash -c`, the first row.
- **What grading RF in our harness needs:**
  - Run the rubric harness-side. The task container has no network, and the key must stay out of it. Port `evaluate_rubrics.py` to our ledgered client. It costs roughly one Opus-class call per rubric item per graded arm, which is paid and not measured here.
  - Choose and check each task's shell and PATH.
  - Run a base/gold check of the tests half for each task, on an amd64 host.
  - Expect some tasks to be dropped or graded by rubric only.
- **Resources:** RF `task.toml` asks for `cpus = 16`, `memory_mb = 16384` (`MT/rf_task-69391d8d…/task.toml`), against SWE-bench Pro's 1 CPU / 4 GB.

**Our use case may not need the final reward.** The accumulated-loss primary readout is the governed act, and the task reward is exploratory (plan, "Readouts"). If the RF reward is dropped, RF's grading cost falls to near zero, and its value is only the Q2 content.

## 4. Harness fit

What `src/compaction/s2/tasks.py` / `interact_run.py` / `interact.py` / `deepswe/sandbox.py` assume, and what each family needs:

| assumption in our code | where | SWE-bench Pro | RF |
|---|---|---|---|
| A task is a DeepSWE dir: `task_info` reads `repo/tasks/<id>` (image, `collect_command`, `instruction.md`, `tests/`, `solution/solution.patch`) | `tasks.py:125-137`; `interact_run.load_interact_task` uses `deepswe_<id>` + `task_info` (`interact_run.py:117-122`) | new loader: image from `Dockerfile:15` (plus our derived stripped tag), base from `config.json`, tests = `steps/05_test_handoff/tests`, solution files from `config.patch`, persona and steps from the task dir | new loader: image from `task.toml` digest, workdir and language from `tests/config.json`, solution `solution/gold.patch` |
| Workdir `/app` | `interact.py:38` (`WORKSPACE`), `:357`, `:384` (fingerprints), `:505`; `sandbox.py:239`, `:250`, `:579`; `outcomes.py:86` | `/app` ✓ | **9 different paths**: `/workspace` (6), `/app` (6), `/grafana` (3), `/calypso` (2), `/go/src/go.k6.io/k6` (2), `/opt/netdata.git` (2), `/app/suricata`, `/app/source`, `/src`, `/code`. These are shared modules, so changing them means running the full suite |
| `bash -lc` for all exec | `sandbox.py:250-254` | ok on ansible | per image (§1) |
| Entrypoint-free image | `sandbox.py:245` | breaks (§1) | ok |
| Plan file at `/tmp/plan.md` | the step driver, and the fingerprint includes `/tmp/plan.md` (prior report §4) | `/app/plan.md`, inside the repo, in all 25 (`steps/01_plan/instruction.md`, `02_implement:3`). It shows up in `git status`, can get committed into the implementation commit, and enters the graded diff. Keep it verbatim and exclude it from grading, or move it to `/tmp` as a logged deviation | `/tmp/plan.md` ✓ |
| One step-instruction text per step | `interact_run` reads `steps/<s>/instruction.md` verbatim | 1 distinct text per step across all 25; it differs from DeepSWE's wording ("user" vs "maintainer"; adds "The user is still available via the `ask_user` tool") | 15 distinct texts per step (language and path in line 1, e.g. "You're working in a Go repository at `/workspace`") |
| Simulator code | `interact.UserSim` ports `server.py` | `server.py` and `repo_exec_server.py` byte-identical to DeepSWE's in all 50 (`diff -q`, no output), so the port applies unchanged | same |
| Reward reader | `parse_reward` | adapter (§3) | harness-side rubric plus validation (§3) |
| Toolchain inside the image | — | ansible: Python 3.9.23, pytest at `/usr/local/bin`; qutebrowser: Python 3.11.13 | scapy: Python 3.8 (login) or 3.12 (non-login); k6: Go 1.20.14 (`GOFLAGS=-mod=vendor`, so it builds offline) |

**Estimates** (wall-clock, attended; compute extra):

- **SWE-bench Pro ≈ 6–9 h:**
  - loader: 1–1.5 h;
  - derived stripped images (entrypoint, history squash, leak assertion), built for 25: 1.5–2 h;
  - verifier adapter (gold-checkout → `test_patch`, `reward.json` counts): 1.5–2 h;
  - plan-path handling: 0.5 h;
  - base-0/gold-1 check across 25 (some, e.g. teleport, webclients and NodeBB, may be slow under emulation): 1–2 h;
  - tests and astra review: 1 h.
- **RF ≈ 12–18 h:**
  - loader: 1 h;
  - per-task workdir through `interact.py`/`sandbox.py`/`outcomes.py`, with the full suite because the modules are shared: 2–3 h;
  - shell/PATH per image: 1–2 h;
  - harness-side rubric port and its cost line: 2–3 h;
  - tests-half validation across 25 on an amd64 host, with fixes to name matching or a per-task drop list: 3–5 h;
  - resource sizing (16 CPU / 16 GB): 0.5 h;
  - tests and astra review: 1.5 h.
  - If the final reward is dropped (§3), RF falls to about 5–8 h: loader, workdir, shell, tests.

## 5. Session length

- **The paper (arXiv 2606.30573v1, HTML §§1–6 and A.1–A.4 read verbatim by a fetch sub-agent; the last ~26k characters unread) breaks nothing down by family.** Pooled Table 1, multi-turn per trial:
  - GPT 5.5: 424.8 steps, 0.36M non-cache tokens, $9.84;
  - Opus 4.8: 180.9 steps, 0.35M, $11.80;
  - Sonnet 4.6: 174.6 steps, 0.24M, $6.10;
  - this is 3–4× single-turn steps.
- "trials average about 7 user messages per trial" (§3.2). Context sizes and compaction are not reported.
- **Run logs in the repo:** none for these families. The earlier single-turn peak table covers DeepSWE only (`derived/task_table.json`: median ~191k for DeepSeek V4 Flash).
- **Repo proxies, computed here** (script: scratchpad `patchsize.py`, `taskblocks.py`):

  | family | reference patch, files changed (median) | changed lines, median [min, max] | persona task block, words (median) | instruction.md words (median) |
  |---|---|---|---|---|
  | DeepSWE | 5 | 617 [530, 1647] | 405 | 405 |
  | SWE-bench Pro | 5 | 209 [22, 2028] | 849 | 1010 |
  | RF | 7 | 257 [35, 1534] | 580 | 580 |

- **Reading (inference):**
  - SWE-bench Pro and RF fixes are smaller than DeepSWE's features, which points to shorter sessions.
  - But their hidden specs are longer, which means more disclosures and review rounds under the vibecoder pattern: all 75 personas use "vibecoder" + "busy colleague".
  - Their repos are much larger (teleport, protonmail webclients, grafana, MariaDB, wp-calypso), so exploration and test output fill context faster.
  - I can't say whether they reach several compactions at 150k. A 2–3 task pilot per family would settle it at about the per-session price of the DeepSWE runs.

## 6. Fit to Q2: preservation rules in the personas

The agent sees only the step instructions. The persona's task block, the hidden spec, is disclosed one item at a time once the agent shows work (`persona.md` "Disclosure pattern: vibecoder", e.g. RF 69391d8d lines 64-70, 147-149). I counted task-block phrases with regexes after the persona boilerplate (scratchpad `taskblocks.json`); the boilerplate's own "preservation" example was excluded:

| family | "Do NOT modify any test files" | keep / preserve / retain / maintain | unchanged / must not / should not | backward / compat / deprecat |
|---|---|---|---|---|
| RF | 20/25 (+ "minimal changes" 25/25) | 9/25 | 4/25 | 4/25 |
| SWE-bench Pro | 0/25 | 16/25 | 9/25 | 10/25 |
| DeepSWE | 0/25 | 10/25 | 10/25 | 3/25 |

**Caveat for RF:** the test-file ban is **not hidden**. RF's visible `02_implement` instruction already says "Do **NOT** modify any test files or testing logic in any way" and "Keep the project building and the existing behavior intact" (`MT/rf_task-69391d8d…/steps/02_implement/instruction.md:5-6`). SWE-bench Pro's says "Do not modify test files". So the Q2-usable rules are the task-specific ones, disclosed later.

**Quotes (persona.md task blocks):**

- **RF**
  - k6 `…b446`, line 272: "Note: the underlying Sum field must remain exported because external vendored code reads it directly."
  - k6 `…b447`, line 193: "Keep the public API stable. The v1 subpackage must use `package cloud`."
  - suricata `…988137`, line 206: "Maintain backward compatibility for callers that currently ignore the return value"
- **SWE-bench Pro**
  - NodeBB `f083cd55`, line 218: "Keep `getSortedSetMembers` and `getSortedSetsMembers` unchanged; they should continue returning only member values without scores."
  - qutebrowser `bedc9f7f`, line 222: "…it should retain its original signature: `interpolate_color(start: QColor, end: QColor, percent: int, colorspace: Optional[QColor.Spec] = QColor.Rgb) -> QColor`."
  - openlibrary `d40ec887`, line 213: "disallowed or missing hosts must not produce uploads, and preview must still report whether a cover would be accepted."

**Reading:**

- **RF's rules are the cleanest Q2 items.** Each is a constraint on code the refactor would otherwise naturally change, and a later edit visibly keeps or breaks it.
- **Many SWE-bench Pro "preserve" lines are behaviour specs rather than "don't touch X"** ("preserve the input key order"). Several are true keep-as-is rules: NodeBB, qutebrowser signatures, ansible "Keep SELinux getters and setters inside lib/ansible/module_utils/basic".
- Both families have more such lines per task than DeepSWE.
