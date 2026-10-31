# Test suite speed (2026-10-08)

The goal was a much faster `pytest` wall-clock without weakening what any test checks. All
measurements were taken in a fresh worktree of `main` @ `b1e05a1`, on an Apple M5 Pro with 18
cores (6 performance, 12 efficiency), running `uv run --group swarm pytest -q -p no:cacheprovider`.
The default `-m 'not slow'` applies to every run.

## Recommended command

```bash
uv run --group swarm pytest -n auto --dist loadgroup
```

- **`-n auto` (pytest-xdist)** starts one worker per core.
- **`--dist loadgroup`** is required. Two modules must run whole in one worker, and they are
  marked `xdist_group` (see below).
- **Thread cap.** `tests/conftest.py` sets one BLAS/OpenMP thread per xdist worker unless the
  caller has set `OMP_NUM_THREADS`.

`-n auto` is **not** in `addopts`. The brief allowed that only if the whole suite was reliably
green across two runs, and one failure was already there before this work (see the next
section). Once it is fixed, two runs in a row should be green, so adding
`-n auto --dist loadgroup` to `addopts` would then be safe. Plain `uv run pytest` (serial) still
works unchanged.

## Before and after

| Run | Passed | Skipped | Failed | Errors | Wall-clock |
|---|---|---|---|---|---|
| Before, serial | 3484 | 34 | 16 | 26 | 782 s |
| After, serial | 3485 | 74 | 1 | 0 | 673 s |
| After, `-n auto --dist loadgroup`, run 1 | 3485 | 74 | 1 | 0 | 106 s |
| After, `-n auto --dist loadgroup`, run 2 | 3485 | 74 | 1 | 0 | 108 s |

- **Overall:** 782 s serial became 107 s parallel, a 7.3x speedup.
- **Skips:** the +40 skips are exactly the 40 former missing-data errors and failures. They now
  skip, with the missing files named in the reason.
- **Passed:** +1 is `tests/s4/test_servers.py::test_ops_other_paths_and_access_log`, which is
  flaky (see the failure classification) and failed in the baseline run.
- **Remaining failure:** `tests/e3/test_train_job.py::test_pod1_dry_run_chain`, which was
  already failing before this work.
- **Totals:** 3560 tests in every run. 3 are deselected by `-m 'not slow'`.

Most of the speedup comes from parallelism. The remaining changes each removed a measurable
amount of time:

- `pod_push.sh` secret-name loop: about 26 s of serial time.
- I9 one-second sleeps: about 11 s.
- One thread per worker: 171 s became 116 s under xdist.

### Top 10 slowest, before (serial)

| s | test |
|---|---|
| 13.63 | i9/test_readers.py::test_olens_draw_resumes_and_writes_readouts |
| 9.81 | e9/test_orchestrate.py::test_pod_push_accepts_the_orchestrator_runs |
| 9.29 | test_pod_scripts_e9.py::test_pod_push_e9_dry_run |
| 8.92 | test_pod_scripts_e9.py::test_pod_push_e9_requires_each_listed_runs_stream |
| 8.79 | x3/test_pool_docker.py::test_pool_runs_blocks_in_processes_with_separate_logs |
| 8.52 | test_pod_scripts_e3.py::test_watchdog_hard_stop_latency_stays_within_the_reserve_clock_driven |
| 7.67 | shared/test_answer_pairs.py::test_real_pool_counts_after_trimming_every_answer |
| 6.98 | i1a/test_olens.py::test_generate_draw_resumes_without_duplicates |
| 6.72 | i9/test_analysis.py::test_unsealed_80_path_monitors_stage_c_and_final |
| 6.40 | e9/test_replay.py::test_replays_are_cumulative_and_clear_only_with_replacement_evidence |

### Top 10 slowest, after (serial)

| s | test |
|---|---|
| 13.33 | i9/test_readers.py::test_olens_draw_resumes_and_writes_readouts |
| 8.49 | test_pod_scripts_e3.py::test_watchdog_hard_stop_latency_stays_within_the_reserve_clock_driven |
| 7.46 | shared/test_answer_pairs.py::test_real_pool_counts_after_trimming_every_answer |
| 6.85 | i1a/test_olens.py::test_generate_draw_resumes_without_duplicates |
| 6.57 | i9/test_analysis.py::test_unsealed_80_path_monitors_stage_c_and_final |
| 6.31 | e9/test_replay.py::test_replays_are_cumulative_and_clear_only_with_replacement_evidence |
| 6.16 | x3/test_pool_docker.py::test_pool_runs_blocks_in_processes_with_separate_logs |
| 5.24 | x3/test_x3_analysis.py::test_recovers_planted_purpose_effect (setup) |
| 5.13 | i1a/test_olens.py::test_check_batching_catches_injection_bugs[_swap_rows_in_batches] |
| 5.08 | i9/test_analysis.py::test_final_complete_run_classifies (setup) |

The three `pod_push.sh --exp E9` tests went from 9–10 s each to about 1 s each. The I9 chain
tests lost their 1.1 s sleeps: for example, `test_run_i9_session_b_pilot_then_main_then_unsealed`
went from 5.5 s to 2.2 s.

Under xdist (run 2), the slowest calls are 10–17 s, slowed by CPU contention:

- `e9/test_replay` 16.6 s
- `shared/test_answer_pairs` real-pool test 14.6 s
- `i9/test_analysis` 10.8 s

The other large cost is fixture setup. `i9/test_analysis.py`'s module-scoped `syn_run` (about
5 s) is rebuilt in each of about 13 workers, roughly 124 s of CPU.

### Where the time went (profile)

The suspected costs did not dominate. The serial profile was a long tail: 726 s of per-test time
spread over about 1,670 phases.

| Suspect | Finding |
|---|---|
| (a) No parallelism | The main lever: 7.3x. |
| (b) Pod/watchdog shell tests waiting out timeouts | Mostly already set small timeouts through env (`E3_*_S`, `KS_POLL_S`, `E9_KILL_GRACE_S`). The real cost in `pod_push.sh` tests was a per-file `tr` fork in the secret-name check (about 9 s per E9 push, 4,282 files × 2 forks). |
| (c) I9 sleeps | 10 × 1.1 s, now replaced. |
| (d) S4 server polls | About 29 s in total. These are real timing assertions (auto-ack after 1.5–6 s, scheduled writes at offsets), so they were left as they are. |
| (e) Tiny-model training | Most of it is in data-gated tests that are skipped in a worktree. |

Per-file serial totals, top 6 before:

| s | file |
|---|---|
| 74 | i9/test_analysis |
| 56 | e9/test_replay |
| 51 | test_pod_scripts_e3 |
| 50 | i1a/test_olens |
| 32 | i9/test_readers |
| 29 | shared/test_answer_pairs |

## Files changed

| File | Why |
|---|---|
| `pyproject.toml` | `pytest-xdist>=3.8` in the `dev` group. Registers the `xdist_group` marker. |
| `uv.lock` | Adds only `pytest-xdist` 3.8.0 and `execnet` 2.1.2. See the note on the lockfile. |
| `tests/conftest.py` (new) | The `require_data` session fixture (skips when gitignored inputs are missing, naming them) and the `e1_splits` fixture. Caps BLAS/OpenMP to one thread per xdist worker. |
| `tests/test_train.py` | The `setup` fixture needs `e1_splits`, so it skips instead of erroring. |
| `tests/e1b/test_checkpoints.py` | Same. |
| `tests/e9/test_finetune.py` | `q20run` and the three `make_root` tests need `e1_splits`. |
| `tests/e9/test_pod_job.py` | Five tests need `data/E1/questions/narrow.jsonl`, through a `narrow_questions` fixture. The `pod` fixture gives each test its own `TMPDIR`. |
| `tests/test_pod_checks.py` | The five `main()` tests and `test_real_manifest_verifies` need every file `data/E1/manifest.json` lists, through an `e1_data` fixture. |
| `tests/i9/test_pod_i9.py` | Ten `time.sleep(1.1)` calls replaced by `I9_RUN_STAMP=fresh_stamp()` on the rerun. The script already honours that variable. The first run still uses the clock-derived default name. |
| `tests/s4/test_servers.py` | `xdist_group("s4_servers")`: `test_zz_no_scenario_strings_in_any_response` checks the response bodies the module's other tests collected in the same process. |
| `tests/x3/test_x3_analysis.py` | `xdist_group("x3_analysis")`: builds the 5 s module fixture once rather than in about 8 workers (about 80 s of CPU). |
| `tests/e9/conftest.py` | An autouse fixture points `EMX_E9_ADMISSION_LOCK` at `tmp_path`. `e9/test_orchestrate.py` had been creating and locking the checkout's real `logs/E9-admission.lock`. |
| `tests/e1b/test_pod_run_e1b.py` | `test_follower_part3_grid_alpha_and_blinding` polls at 1 s instead of 0 s. See the xdist breakages. |
| `scripts/pod_push.sh` | `secret_names()` lowercases the whole list with one `tr`, read in step with the original list, instead of `$(printf | tr)` per file. Same output, still bash 3.2. The E9 dry run went from 9.6 s to 1.2 s. |

### The `pod_push.sh` change

This is the only production change. It changes speed, not behaviour.

- **Output checked identical.** Old and new `secret_names` gave identical output under
  `/bin/bash` 3.2. The input was the repo's 6,385 tracked paths plus 14 crafted names: mixed
  case, spaces, non-ASCII, `.env`/`.PYPIRC`/`ID_RSA`, the `*.py`/`*.jsonl` token exemption, and
  a trailing space.
- **Tests pass.** The 17 parametrized secret-name refusal tests, the case-insensitive exemption
  test, and every `pod_push` test pass.
- **Real pushes speed up too.** A real push runs the same check, so it gets about 9 s faster.

### The lockfile

On this repo, any relock, even just adding pytest-xdist, also re-resolves the conflicting pod
groups:

- `unsloth` 2026.9.12 → 2026.10.2
- `unsloth-zoo`, `torch` 2.12.1 → 2.13.0 (the unsloth fork)
- `transformers`, `tokenizers`, and `mlx-*`

`uv` did not keep the locked versions as preferences. To keep the pod pins unchanged, the lock
was made in three steps:

1. Locked with `uv lock --exclude-newer 2026-10-03T00:00:00Z`.
2. Removed the `[options] exclude-newer` line that this writes.
3. Confirmed that `uv lock --check` passes.

The resulting diff adds only `pytest-xdist` and `execnet`, plus their marker lines. The next
ordinary `uv lock` will make that unrelated drift: this was already true, and it is worth
knowing before a pod-group change.

## Tests marked serial (xdist groups)

- `tests/s4/test_servers.py` (whole module): an order and same-process dependence through the
  module-global `BODIES`. It failed under plain load distribution: `assert len(BODIES) > 30`
  got 6.
- `tests/x3/test_x3_analysis.py` (whole module): purely a CPU saving. Its tests are
  independent.

## What broke under `-n auto`, and the fixes

| Test | Cause | Fix |
|---|---|---|
| `s4/test_servers.py::test_zz_no_scenario_strings_in_any_response` | Needs the other tests of its module in the same process. | `xdist_group`. |
| `e1b/test_pod_run_e1b.py::test_follower_part3_grid_alpha_and_blinding` (2 of 2 runs with the thread cap) | A race already in the test: five 0-second polls against a background alpha job. Under load, the polls ran out before `alpha.json` was pushed (exit 6). | Polls 1 s apart for this test only. Assertions unchanged. The test takes 1.4 s. |
| `e9/test_pod_job.py::test_fake_venvs_smoke_insert_loss_measured` (1 run) | `pod_run_e9.sh` uses `${TMPDIR:-/tmp}/e9_insert_<UTC second>`. Two tests in parallel in the same second shared the directory, and one deleted it. | Per-test `TMPDIR` in the `pod` fixture. Production is unaffected: one job per pod. |
| `logs/E9-admission.lock` written into the checkout | Two `e9/test_orchestrate.py` tests used the default lock path. | Autouse tmp lock in `tests/e9/conftest.py`. |

One more problem was seen once in 11 parallel runs and was **not fixed**. One run reported
3466 passed and 93 skipped: 19 tests skipped silently. The likely set is the Docker-gated tests
(`x3/test_image.py`, `x3/test_passcheck.py`, `x3/test_pool_docker.py`, `s4/test_compose.py`).

- **Inference about the cause.** The skip reasons were not captured in that run. These tests
  probe `docker info` (20 s timeout) at import, once per worker. Under 18 workers starting
  together, or with Docker Desktop's resource saver waking, a probe can fail. It was not
  reproduced: 54 concurrent `docker info` calls at idle all succeeded within 1.6 s.
- **Possible fix.** Probe once in the controller (`pytest_configure` when `PYTEST_XDIST_WORKER`
  is unset), export the result as an environment variable, and have the three Docker checks
  read it.
- **Until then.** Check the skip count: 74 is the expected value in a worktree.

## Classification of the baseline failures and errors (42)

| Class | Tests | Detail |
|---|---|---|
| Missing data (40, now skipped) | `test_train.py` (8 errors), `e1b/test_checkpoints.py` (11 errors), `e9/test_finetune.py` (7 errors + 3 failures), `e9/test_pod_job.py` (5 failures), `test_pod_checks.py` (6 failures) | The gitignored files `data/E1/{train,test}_{bad,good}.jsonl` and `data/E1/questions/narrow.jsonl` are absent in a fresh worktree. In `test_pod_checks.py`, the `main()` tests stop at the data-manifest check (`missing data file data/E1/train_bad.jsonl`) before reaching the check they test. That includes `test_debug_flags_never_pass`: its `KeyError: 'complete'` comes from the early-failure result, which has no `complete` key. |
| Bug already there (1, not fixed) | `e3/test_train_job.py::test_pod1_dry_run_chain` | The dry run of `pod_run_e3.sh` runs against the repo's own committed `results/E3`. Since the E3 results were merged (PR #7), every cell reads "trained, checked and pushed; skipping", so the plan has 15 commands, not 21. The test needs an isolated results root. |
| Flaky (1, not fixed) | `s4/test_servers.py::test_ops_other_paths_and_access_log` | It reads `access.jsonl` straight after the fourth response. The ops server appears to append the access line after it has responded, so the file held 1 of 4 lines. Failed in the serial baseline; passed in all 12 later full runs (11 parallel, 1 serial). |

## Proposed `slow` marks (not applied)

Under `-n auto`, wall-clock is set by total CPU divided by workers, not by any single test. So
marking these `slow` would save little in parallel. For a serial run, these are the candidates
(serial seconds, after):

| s | test | Note |
|---|---|---|
| 13.3 | i9/test_readers.py::test_olens_draw_resumes_and_writes_readouts | Tiny model, end to end. |
| 7.5 | shared/test_answer_pairs.py::test_real_pool_counts_after_trimming_every_answer | Real pool, full pass. |
| 6.9 | i1a/test_olens.py::test_generate_draw_resumes_without_duplicates | Tiny model. |
| 6.6 | i9/test_analysis.py::test_unsealed_80_path_monitors_stage_c_and_final | Full synthetic analysis. |
| 6.3 | e9/test_replay.py::test_replays_are_cumulative_and_clear_only_with_replacement_evidence | |
| 6.2 | x3/test_pool_docker.py::test_pool_runs_blocks_in_processes_with_separate_logs | Docker. |
| 5.1 | i1a/test_olens.py::test_check_batching (and its two `catches_injection_bugs` cases) | |

Not proposed: `test_pod_scripts_e3.py::test_watchdog_hard_stop_latency_stays_within_the_reserve_clock_driven`
(8.5 s). It is the clock-driven proof of the watchdog's latency bound, and its timeouts are
already small (2–6 s).

A further parallel-only saving was not applied. `i9/test_analysis.py`'s `syn_run` is rebuilt in
about 13 workers, roughly 124 s of CPU. Splitting the tests that use it into 2–3 `xdist_group`s
would build it 2–3 times. Grouping the whole module (74 s serial) into one worker would make that
worker the critical path, so it was not done.

## Code review and fixes (main session, 2026-10-08)

gpt-6-astra round 1 (`logs/reviews/20261008-test-speed-code-r1.md`): **COMMIT AFTER FIXES**.
- **BLOCKING, fixed:** the single `tr` in `pod_push.sh`'s `secret_names` failed on a byte that isn't valid UTF-8 ("Illegal byte sequence") and then matched nothing, so later secret-named files escaped the check. Now `LC_ALL=C tr` (byte-safe; the patterns are ASCII, so the same names match) writes a temp file, and any `tr` failure refuses the push. Checked under bash 3.2.57 with `bad-\xff/ordinary.txt` in the list: `SECRET.key`, `My_Api_Key.txt` and `.ENV.local` are still caught, and `tokens.jsonl` and `token_readers.py` are still spared. The earlier "identical output" claim held only for valid UTF-8 lists.
- **MINOR, fixed:** `tests/test_train.py` and `tests/e1b/test_checkpoints.py` now require only the two bad-advice splits (new `e1_bad_splits` fixture), not all four.
- **Left as follow-ups:**
  - the stale `tests/e3/test_train_job.py::test_pod1_dry_run_chain` (it reads the committed `results/E3`);
  - the S4 access-log race;
  - linking saved run summaries.
- **The intermittent extra skips:** one of three parallel runs in the main session again showed 93 skips instead of 74 (2 failed, 3465 passed), and its skip reasons weren't captured. The next run with `-rs` gave 74 skips, every one with a missing-data or missing-package reason. Until the Docker and image probes run once in the controller, **check the skip count (74 in a worktree) or run with `-rs` before trusting a parallel run**. `-n auto` stays out of `addopts`.

Final parallel run after the fixes: 1 failed (the stale E3 test), 3485 passed, 74 skipped, 100 s.
