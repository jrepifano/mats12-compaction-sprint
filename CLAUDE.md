# CLAUDE.md

Guidance for Claude Code in this repository. **Preliminary (2026-09-28)** — expect revisions as the exploration phase proceeds.

## What this repo is

Jacob's workspace for the **exploration phase of MATS 12 with Neel Nanda**. The goal of this phase is not to finish a project. It is to find, cheaply and quickly, which research direction gives the most information per dollar and per hour, and then commit to it. Experiments are small, fast, and decision-oriented: every run exists to change what we do next.

Sibling repo `../mats12_neel_working` is Jacob's MATS application project (emergent misalignment, "Correct, Don't Delete"). It is **read-only reference** here. Port code from it instead of rewriting, and cite the source path in the port's docstring. Its `CLAUDE.md`, `logs/phase1-report.md`, and `scripts/` carry the conventions and gotchas this repo inherits. **`docs/working-repo-code-map.md`** is the quick reference: what each module does, its hardcoded assumptions, and what must change to repurpose it for a new model or task. Check it before writing new infrastructure.

## Source material

- `MATS 12 Literature Review_ EM Personas, Interp Method Comparison, Agent Swarms.pdf` — Jacob's literature review of three candidate topics: (1) why EM generalizes to a persona, (2) comparing J-Lens, NLAs, SAEs and Oracle Lens, (3) agent swarm alignment and monitorability.
- `Neel brainstorming open problems.pdf` — Neel's open-problems list (broader than the three topics).
- `papers/` — full texts of the cited works, one directory per topic, each with an `INDEX.md` (title, URL, status). Files are named by paper title. `papers/_litreview_text.txt` and `papers/_neel_brainstorm_text.txt` are text extractions of the two PDFs above. **Read the primary source before making a claim about what a paper found.** If a paper is marked MISSING, say so rather than relying on the lit-review summary alone. Some `.md` captures of blog posts are structured paraphrases rather than verbatim text, as noted in each `INDEX.md`. Before quoting exact numbers from those, check the source URL.
- **Experiment tracker:** a Claude Doc shared with Mira. **Follow `docs/tracker-instructions.md`** (also sent to Mira for her own Claude sessions). In short: a row exists only once the experiment's plan is a Claude Doc; the row is named with the doc's title and linked to it; status is Designing / In progress / Done; never infer or fill in anything about a collaborator's experiments. Update Jacob's rows at every status change, checkpoint and paid run, with spend taken from `logs/costs.jsonl`. Commit a snapshot to `docs/tracker.md` whenever it changes.
- **Writing for sharing:** preregs and plans follow `docs/experiments/PREREG_TEMPLATE.md`: plain language, no internal codes, inline citations with a References list. Jacob shares them with coauthors.
- **Every inline citation links to its reference.** Each reference is its own heading (level 4) in the References section, and every citation in the text links to that heading. In Claude Docs, add links as a `format` op (`add: [{type: link, href: "#<heading block id>"}]`) on the citation's words. Links to list items don't work, only links to headings. In the repo markdown, links use GitHub heading anchors (e.g. `#zhao-et-al-2026`).
- `docs/research-proposals.md` — the ranked, priced experiment proposals. This is the planning document of record.
- **Claude Docs for review and sharing.** Preregs and other long plans are mirrored to Claude Docs (claude.ai) so Jacob and coauthors can read and comment on them. The repo markdown stays the source of truth. Before any commit that touches a mirrored file, read the doc's changes since the last sync (`read` with `sinceRev`), fold them into the markdown, and note the sync in the review log.

  Comments that Jacob sends to Claude from a doc do not reach this CLI session ("can't reach Claude"). When he says he has commented, pull the comments with the docs `query` tool, then act on each and reply in its thread. Another Claude session (e.g. in the browser) may also answer threads and edit the doc. Read its changes with `sinceRev` before editing, and carry them into the repo.

More documents may be added; when they are, read them and update the proposals doc.

## Working norms (carried over from the working repo)

- **Jacob designs and verifies; Claude executes and reports.** Claude drafts proposals, preregistrations, and code. Nothing runs that costs money (GPU pods, paid API calls beyond trivial smoke tests) without Jacob's explicit go-ahead for that specific experiment.
- **Hypotheses before experiments.** Every experiment gets a preregistration at `docs/experiments/<EXP-ID>/prereg.md` **before** any paid run. It must be agreed by Jacob and reviewed by gpt-6-astra. It contains:
  1. The question, and the decision it informs.
  2. The main hypothesis and at least one competing hypothesis.
  3. Concrete predictions with rough probabilities, written before seeing data.
  4. What each outcome would change: continue, pivot, or kill.
  5. Metrics, sample sizes, the statistical test, and the controls and baselines (simple baselines first, per Neel's "method minimalism").
  6. Kill criteria and a hard budget cap in dollars and wall-clock hours.
  7. The sanity checks and manipulation checks that must pass before the headline number means anything.

  Grounding means every hypothesis cites the paper(s) in `papers/` that motivate it, and flags where our expectation departs from published results.
- **Order: review, then sign-off, then plan mode** (Jacob, 2026-10-04). A prereg goes through all its gpt-6-astra review rounds *before* Jacob is asked to sign it off:
  1. Draft the prereg.
  2. Run the gpt-6-astra rounds (up to 3) and fix every BLOCKING finding.
  3. Then ask Jacob for sign-off, with the review verdicts and a short table of any findings still open.

  Never ask him to approve an unreviewed or partly reviewed prereg. If his sign-off changes the design, the change is reviewed before it counts as final, using a remaining round or a new count for a substantially new version.

  All prereg review is finished before entering plan mode to plan the implementation. The implementation plan then gets its own review, as below.
- **Report after every step** to `logs/<EXP-ID>-report.md`. Record exact commands, seeds, row counts, file hashes, model ids and revisions, wall-clock, UTC timestamps, and dollars spent. These reports become the LLM-usage disclosure, so they must describe what actually happened.
- **Determinism.** Fix seeds and record them inside the output artifacts. Pin model revisions (HF commit hashes) and dataset revisions.
- **Treat your own successes as hypotheses.** When something "works", show Jacob random raw samples (transcripts, readouts, judge rationales), never cherry-picked ones. Label inferences as inferences.
- **Stop at checkpoints.** Each prereg names its stop points; report and wait there.
- **Implementation plan before any code.** Once a prereg's design is final, and before writing code, write `docs/experiments/<ID>/implementation-plan.md`. This is *how Claude will build and run it*, not what the experiment is. It contains:
  1. **Work breakdown:** units of work, each with inputs, outputs, the files it touches, and a done-when test.
  2. **Dependency graph:** which units must be sequential and which can run in parallel.
  3. **Parallel execution with Opus subagents** (Jacob's preference): independent units go to parallel `opus` subagents, each with a self-contained brief. The brief covers the goal, the exact files it owns (no two agents own the same file), the interfaces it must match, its tests, and the rule "no paid calls, no commits". The main session integrates their work, runs the full test suite, and reviews the diffs.
  4. **Interfaces and contracts** that are frozen before the agents start: data schemas, function signatures, file layouts. Parallel work can't drift apart without them.
  5. **Verification:** the unit tests, smoke tests and sanity checks (from the prereg) that gate each unit, and when the gpt-6-astra code review happens.
  6. **Paid steps:** exactly which steps spend money, their projected cost, and the checkpoint before each.
  7. **Failure handling:** what to do if an agent stalls or fails. Agents doing large rewrites stalled repeatedly on 2026-09-28, so give them small, bounded units and tell them to make small edits.

  The plan is reviewed by Jacob (and by gpt-6-astra, within the 3-round cap) before any unit starts.
- **Implement each experiment in its own git worktree** (Jacob, 2026-10-04). Several sessions work in this repo at once, so implementation never happens in the main checkout.
  - **When:** after the prereg is signed off, and before the first implementation commit. Branch from current `main` into `.claude/worktrees/<exp>-<purpose>` on branch `<exp>/<purpose>`, e.g. `e1b/run` or `x3/build`. Use Claude Code's worktree tool or `git worktree add`. Copy `.env` in, as described under Stack.
  - **What lives on the branch:** everything the experiment produces. That is its code, configs, tests, results, report, review logs and `logs/pods_<EXP>.jsonl`, plus any prereg amendments made during the run. Commit there after each reviewed step.
  - **What stays on `main`:** the shared planning documents: `docs/research-proposals.md`, CLAUDE.md, the tracker snapshot, `papers/`, and new preregs, which are written before a worktree exists. Edit these on `main`, not on a branch, so parallel sessions don't collide on them.
  - **Shared code** (`emx.spend.CAPS`, `pod_push.sh` cases, `emx` modules other experiments use): change it on the branch, but keep the changes small and additive, such as a new experiment case rather than a rewrite. Before merging back, merge `main` into the branch and rerun the full test suite.
  - **Each worktree keeps its own spend ledger** (Jacob, 2026-10-04). `emx.spend` writes to `logs/costs.jsonl` in whichever checkout the code runs from, so the experiment's caps are enforced against its own copy. Commit it on the branch as usual. Don't point `EMX_COSTS_LEDGER` at another checkout. The copies come together through `scripts/merge_ledger.py`:
    - **Running total, any time:** `uv run python scripts/merge_ledger.py report`. It sums main and every worktree, counts entries shared from before a branch point once, and lists each worktree's spend that main doesn't have yet. Use it for the overall budget, the tracker and RunPod balance planning; main's ledger alone undercounts while experiments run.
    - **Entries carry a unique `id`**, stamped by `emx.spend.record` and `s4world.budget.settle` since 2026-10-04, and the script matches entries on it. **A branch created before then must merge `main` before its first paid run**, so that its entries get ids too. Until then, old `s4world` code still writes to main's ledger. If id-less rows were written on a branch anyway, `report` prints UNRELIABLE and exits 1, and `merge` refuses them. Fix it with `merge_ledger.py stamp-ids <that ledger> --write`, which gives ids to the rows main lacks and leaves every other line untouched.
    - **Before any git merge that touches the ledger,** in either direction, stop paid jobs in both checkouts and commit the ledger. Git takes no lock, so a live writer could append mid-merge.
    - **When merging:** `.gitattributes` sets `logs/costs.jsonl merge=union`, so a local `git merge` keeps both sides' appends instead of conflicting. Union is a text merge and can repeat an entry that reached both sides by different routes. So after every merge, run `merge_ledger.py check --fix`, which removes rows whose id already appeared, then `check`, and commit. `check` also fails on malformed lines, and on id-less rows that repeat; those are ambiguous, so resolve them by hand. If a GitHub PR reports a conflict on the ledger, merge locally, or take main's version and run `merge_ledger.py merge --from-ref <branch> --write`.
    - **A branch abandoned without merging** still had real spend. Fold it in with `merge_ledger.py merge --from <worktree dir or ledger file> --write` (or `--from-ref`), and commit main's ledger. It's a dry run without `--write`, it errors on a missing source rather than reporting zero, and running it twice adds nothing.
  - **Finishing:** merge back through a PR once the final report is reviewed, as I1a and the swarm work did (PRs #1 and #2). Run `merge_ledger.py check`, and confirm `report` shows nothing unmerged for that worktree. Then remove the worktree (`git worktree remove`), after checking `git status` shows nothing uncommitted in it.
- **Read all related sources before planning an implementation.** When turning a proposal into a prereg or code:
  1. Read every paper, README, and blog post in `papers/` that the experiment cites or builds on, in full.
  2. Also read the relevant upstream code (repos and model cards), and the matching entries in `docs/working-repo-code-map.md`.
  3. Cite each source inline where it shapes the design, and list it in the prereg's References section. Note any partial reads in the risks section.

  If a needed source is MISSING, flag it to Jacob before finalizing the plan.
- **Every new research proposal gets an internal and an external literature review** (Jacob, 2026-10-04). Before a proposal goes into `docs/research-proposals.md`, dispatch two subagents in parallel:
  1. **Internal:** search `papers/` (each `INDEX.md`, `_litreview_text.txt`, `_neel_brainstorm_text.txt`), the earlier proposals and reports in this repo, and `../mats12_neel_working` for related work and our own prior results.
  2. **External:** search the web (arXiv, LessWrong and the Alignment Forum, lab blogs) for prior and concurrent work, and read the primary sources. Report which parts were read verbatim and which only as a fetch-tool summary.

  The proposal names the closest prior work and the gap that remains, and cites both. Capture any new external source into `papers/` with an `INDEX.md` row before a prereg builds on it. Why: X3's "nobody has tested this" claim came only from `papers/` and turned out to be out of date when searched externally (2026-10-04).

## Review with gpt-6-astra (via Codex CLI)

**Every stage gets a review of its diff before it is committed or acted on** (Jacob, 2026-09-29):
- planning: proposals, preregs (all rounds done before asking Jacob for sign-off, and before plan mode; see "Order: review, then sign-off, then plan mode"), implementation plans;
- code, once a unit or the full build is finished and before it runs on paid compute;
- results analysis;
- reports.

Point the reviewer at the diff (e.g. `git diff <base>..HEAD -- <paths>`, or the uncommitted diff) plus the files it needs for context. Don't commit or run an unreviewed stage unless Jacob says to, and record any skipped review in the report.

```bash
scripts/codex_review.sh review_prompt.md logs/reviews/<YYYYMMDD>-<topic>-r<N>.md [effort=medium] [seconds=1200]
```

The script wraps `codex exec -m gpt-6-astra --sandbox read-only` with `caffeinate -is` and a wall-clock watchdog. Don't call codex with `perl -e 'alarm'`: that does not stop codex-cli 0.157 (found 2026-09-29).

- Review prompts cover **four layers**:
  1. Correctness and code quality: bugs, logic errors, and code that is fragile, unclear or doesn't match the surrounding code.
  2. Methodology: does the design answer the stated question, and does what ran match the prereg?
  3. Integrity: does every number trace to a committed artifact, and is no claim unsupported?
  4. Goal adherence: does this stage serve the experiment's stated goal and the exploration phase's goal (the most information per dollar and hour toward choosing a direction)? Flag scope creep, gold-plating, and work that doesn't change a decision.

  For plans and preregs, layer 1 becomes "are the hypotheses grounded, falsifiable, and is the cost estimate realistic?"
- **Always use a timeout, through `scripts/codex_review.sh`.** The default is 20 minutes; long documents such as preregs may use 40 minutes. Reviews normally take 5–8 minutes. Two prereg reviews hung silently for 40+ minutes on 2026-09-28. Point the reviewer at the document and only the 3–4 most load-bearing sources, not every PDF.
- **Keep the laptop awake during long runs.** On 2026-09-29, idle and lid-closed (clamshell) sleep paused reviews and Docker episodes mid-run; `pmset -g log` shows it. The paused runs looked like timeouts. `caffeinate` prevents idle sleep only. Closing the lid still pauses everything unless an external display is attached.
- Findings are tagged BLOCKING / MINOR / NIT with file:line and a suggested fix. Every BLOCKING finding is fixed or explicitly deferred with a reason. Record the verdict in the relevant report.
- Save the full review output under `logs/reviews/`. Point gpt-6-astra at files instead of pasting them; it runs with repo read access.
- If `gpt-6-astra` is retired or unavailable, use the strongest model in `~/.codex/models_cache.json` and note the substitution.
- **Maximum 3 review rounds per artifact.** Reviewers will always find something, so don't loop.
  - After round 3, stop reviewing that artifact.
  - Summarize any still-open findings for Jacob in a short table (finding, severity, a recommended fix or accept-as-limitation), and let him decide.
  - Count rounds in the review filenames (`...-r1.md`, `-r2.md`, `-r3.md`).
  - A substantially new artifact (e.g. a prereg derived from a reviewed proposal) starts its own count.

## Timeline, budget, collaborators

- **Exploration phase: one month, starting 2026-09-28.** Rough shape:
  - Week 1: gate experiments (Phase A).
  - Week 2: "is it interesting" experiments (Phase B).
  - Weeks 3–4: commit to one direction and get a presentable preliminary result.
- **Budget:** $2,100 for the whole program, i.e. October 2026 (Jacob, 2026-10-06). The goal is to spend most of it, or at least a lot, during the research sprint, to collect very good results. Track the running total with `scripts/merge_ledger.py report`. Keep every experiment under its cap, and ask before any single experiment above ~$100.

## Stack

- **Python 3.12 via uv.** `uv add`, `uv run`, and a committed `uv.lock`. Dependencies are grouped: local analysis deps are the defaults, and GPU-only deps (torch, vllm, transformers-heavy, nnsight) go in a `pod` group installed only on pods. Use ruff (line length 100) and pytest.
- **Layout** (create lazily):
  - `src/<pkg>/` holds library code.
  - `scripts/` holds entry points.
  - `configs/` holds experiment configs.
  - `data/` and `results/` hold artifacts; large or raw files are gitignored, while hashes and small summaries are committed.
  - `docs/` holds plans and preregs.
  - `logs/` holds reports, reviews, and the cost ledger.
- **Secrets** live in `.env` (gitignored, mode 600): `HF_TOKEN`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `RUNPOD_API_KEY`, `EM_DATASET_PASSWORD`, plus a few non-secret settings. It was copied from `../mats12_neel_working/.env` at Jacob's request on 2026-09-29, with that project's pod- and model-specific variables stripped. Never print or commit it. Scripts load it and log only key names.
  - **The keys live in the main checkout's `.env`** (`/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.env`). Git worktrees under `.claude/worktrees/` do not have one: copy it into the worktree root (`cp -p`, keep mode 600; it is gitignored there too) rather than asking for keys (Jacob, 2026-09-30).
  - All experiments share that one `OPENROUTER_API_KEY` (no per-experiment keys), so judge spend caps are enforced in software: `emx.spend` plus each driver's pre-call guard.
  - **Account balances** (Jacob, 2026-09-30):
    - OpenRouter tops itself up automatically. A low OpenRouter balance is not a blocker: an approved budget may be spent.
    - RunPod does not auto-top-up, and its balance is shared by every experiment's pods. Before each pod launch and at each paid checkpoint, check the balance (free GraphQL `myself { clientBalance currentSpendPerHr pods }`) against the remaining planned GPU spend of all experiments. Warn Jacob while there is still margin.
- **Shared data:** the decrypted EM organism datasets are used in place, not copied: `../mats12_neel_working/data/raw/bad_medical_advice.jsonl` and `good_medical_advice.jsonl` (7,049 rows each; canaries already removed). Record their sha256 in each experiment's manifest.
- **RunPod API quirk:** requests without a browser-like `User-Agent` are blocked by Cloudflare (error 1010). Always send one.

## Compute and cost control

- **GPUs:** RunPod through the REST API. Port `pod_up.py`, `pod_down.py`, `pod_watchdog.sh`, and `pod_budget_check.py` from the working repo. Measured rates as of 2026-09:

  | GPU | $/h |
  |---|---|
  | H100 80GB, secure cloud | 3.29–3.49 |
  | H100 80GB, community cloud | 2.69 |
  | H100 NVL | 2.59 |
  | A100-SXM4 80GB | 1.39 |

  Every experiment has a dollar cap enforced by the watchdog. Terminate pods when idle; never leave a pod running overnight without a job and a watchdog.
- **Ledger:** append every GPU session and every paid API batch to `logs/costs.jsonl` (experiment id, resource, units, $). Before any batch of more than ~$5, print a projected spend and check it against the cap. (The working repo's ledger was `logs/pod_costs.jsonl`; ported scripts write to `logs/costs.jsonl`, through `emx.spend`'s flock-locked single writer.) **E1 caps:** $72 total, $58 GPU, $18 judges (prereg amendments A and D), enforced by one combined guard, `emx.spend.check_total()`. **E1b caps:** $75 total, $56 GPU, $20 judges (E1b prereg §7), the same guard with `exp="E1b"`.
- **Local machine** is an Apple M5 Pro with 48GB. It is fine for API-only experiments, analysis, and small models (≤4B, via MPS). It is not suitable for 7B+ training or 27B inference.

## LLM judges and API models

- **Default judge:** `gpt-6-luna` with reasoning effort none. It costs $0.10/$0.50 per MTok, or $0.05/$0.25 in batch.
- **Cheap alternates, via OpenRouter** (per MTok, in/out, as of 2026-09-28):

  | Model | $ in / out |
  |---|---|
  | `qwen/qwen3.7-flash` | 0.03 / 0.13 |
  | `deepseek/deepseek-v4-flash` | 0.14 / 0.28 |
  | `z-ai/glm-5.3-flash` | 0.15 / 0.50 |
  | `google/gemini-3.5-flash-lite` | 0.30 / 2.50 |
  | `google/gemini-3.8-flash` | 0.75 / 3.75 |

  Gemini output was the largest API cost in the last project, so prefer luna or the Chinese flash models for bulk judging.
- **Any new judge must be validated** before use. Check agreement with hand labels or with a reference judge on at least 100 items, and report the agreement numbers. Headline numbers use a panel of at least 2 judges from different families.
- Gemini through OpenRouter returns no logprobs, so use integer-score mode. Check that logprob judges actually return logprobs (port `judge_probe.py`).
- Port the working repo's async judge client (`src/em_filter/judge_client.py`, `judge_run.py`): semaphore concurrency, tenacity retries, a resumable CSV with status columns, and a spend projection that exits non-zero at the cap.
- **Subject models** for behavioral and multi-agent experiments are chosen per prereg, and costs are estimated from measured tokens per episode. Log `usage` from every response.

## Inherited gotchas (from `../mats12_neel_working/logs/phase1-report.md`)

- Upstream EM code sets no generation seed and doesn't pin revisions, so set and pin both yourself.
- unsloth's response-marker guess can mask every completion for non-Qwen chat templates, so assert the unmasked token count is greater than 0.
- `wandb.init` hangs on a login prompt over SSH, so use `WANDB_MODE=offline` or disable it.
- macOS ships bash 3.2, so watchdog scripts must not use bash 4+ features.
- Parallel writers to a spend ledger corrupted the file. Use a lock or a single writer.
- **Retain training curves** (E1 lost them for 12 of 13 models, 2026-09-30). For any run that trains:
  - Every run writes per-step training loss and periodic held-out loss into its result record.
  - Pull those records, plus the run log, off the pod *before* it terminates, including when the part fails. Stage them; never discard them.
  - Commit the records, and plot the curves in the checkpoint report.
  - Give each pod run a log name of its own (a regeneration run once overwrote a training log).
- Community-cloud H100s are often out of stock, so have a fallback GPU type in every plan.

**RunPod operations** (adopted 2026-10-03 from the `docs/gotchas.md` of [kmerkelbach/runpod-tools](https://github.com/kmerkelbach/runpod-tools); its code is unlicensed, so we reimplement rather than copy):
- **A cap enforced only from the laptop is not a cap.** `pod_push.sh` arms `scripts/pod_killswitch.sh` on every running pod of the experiment. The pod stops itself, using its own pod-scoped RunPod key, once all the experiment's running pods together would reach its GPU kill line.
  - **Verified live on 2026-10-03.** Pods stopped themselves 6–19 s after the deadline; the evidence is in `scripts/killswitch_live_check.py` and `logs/infra/`, at about $0.01 per run.
  - With our image and its pod key, REST returned 403 for both the read and the stop, while **`runpodctl stop pod` worked**. So `arm` refuses on an image without `runpodctl`; ours has it.
  - Re-run the live check after changing the image. A free Linux check is `bash scripts/killswitch_linux_check.sh` (Docker).
  - The laptop watchdog remains the primary cap.
  - A self-stop wipes `/ws`, so pull results well before the deadline.
  - After a self-stop, run `pod_down.py --exp <EXP> --slot <slot>` to terminate the pod and record its spend. Until then the laptop's books count it as still burning (an over-count).
- **Adding an experiment to the pod tooling.** The pod scripts (`pod_up.py`, `pod_push.sh`, `pod_down.py`, `pod_budget_check.py`) and the killswitch deadline are per experiment. They know only E1 and I1a; anything else is refused ("knows E1 and I1a only", or a `KeyError` for missing caps). For a new experiment X:
  1. Add X's caps to `emx.spend.CAPS`, from a `src/emx/<x>/contracts.py` as I1a does. Pods are then named `<x>-<slot>`, logged to `logs/pods_<X>.jsonl`, and armed with X's GPU kill line.
  2. Add an X case to `pod_push.sh`, following its I1a branches: the transfer roots, the pod env, and the `pod_setup.sh --only` target.
  3. Run a laptop watchdog for X, using `scripts/i1a_watchdog.sh` as the per-experiment template.
  4. Dry-run before any paid step: `pod_up.py --exp X --dry-run`, then `pod_push.sh --exp X --slot _A --dry-run`.
  5. Pass `--exp X` on every call. The default is E1, and the environment is never read.
- **Polling too hard looks like a dead pod.** Several watchers each opening ssh sessions every minute trip sshd's `MaxStartups`, and new connections drop at the banner. Use one consolidated ssh call per check, minutes apart. Before declaring a pod dead, kill hung local ssh clients and probe once more.
- `pgrep -f NAME` / `pkill -f NAME` over ssh match the ssh command itself: bracket the first character (`'[N]AME'`), as `pod_part.sh` does with `pod_run_e1[.]sh`.
- Verify a detached launch after 15–20 s with real progress (new log lines, files), not with an immediate `pgrep`. A process that died at once still shows up for a second. Grep logs for the script's own anchored tag, never a bare `DONE`/`FAIL` (model outputs contain those words).
- A redirect into a missing directory under `nohup` fails silently and leaves `$!` stale: `mkdir -p` the log directory first.
- Large single-file transfers over rsync can stall. `tar cf - dir | ssh pod 'tar xf -'` is the fallback.
- RunPod reuses `ip:port` pairs across pods, and `accept-new` refuses a changed host key ("Host key verification failed", exit 255). `pod_up.py` drops the stale `known_hosts` entry before first contact.
- Fetch, verify locally, then terminate, as separate commands. Never fetch and terminate in one compound command.

## Research taste (Neel's stated priorities)

- Pragmatic interpretability: judge methods by proxy tasks on the critical path to safety, not by intrinsic metrics.
- Method minimalism: always include the simplest baseline (black-box prompting, logit lens, linear probe) and beat it before claiming anything.
- Favor fast feedback loops: prefer experiments that return signal in hours, and front-load cheap, high-information checks ("does it even replicate?").
- Report negative results plainly; they count as information.

## Time accounting

Jacob tracks his own time. Timestamp every report entry in UTC so the record can be reconstructed.

## Git

- Commit after each reviewed step. Commit messages end with the co-author line from the session's attribution guidance.
- Never commit `.env`, raw model weights, or activation dumps.
- **Never commit PDFs** (Jacob's decision, 2026-09-28). `papers/**/*.pdf` is gitignored; `INDEX*.md`, markdown captures, and text extractions are committed.

## Visibility: everything we create stays private (Jacob, 2026-10-05)

Every GitHub repo, Hugging Face repo (model, dataset or Space), and any other hosted artifact we create is **private**. Making anything public is Jacob's explicit decision, for that specific item; never infer it from a release plan or an earlier approval. Why: on 2026-10-05 an audit found 28 of the working repo's adapters public on HF (`jrepifano/q14b-*`, created 2026-08 and 09), and a public HF repo is exactly how we found a concurrent project's unpublished results (`papers/em_personas/` row 34). All 28 were made private that day.
- **HF Trainer defaults to public.** `push_to_hub` without `hub_private_repo=True` creates a public repo. Pre-create every repo with `create_repo(..., private=True, exist_ok=True)` and assert `model_info(repo).private` before training starts, as `scripts/e1_preflight.py` and `pod_run_e1.sh` do. Also pass `hub_private_repo=True` where the Trainer creates repos.
- **Never use `private=False`, `--public`, `gh repo create --public`, or a visibility change** without Jacob's go-ahead in this conversation.
- **Audit after any run that uploads**, and before sharing results: list `https://huggingface.co/api/{models,datasets,spaces}?author=jrepifano` with the token and confirm none is public; `gh repo list --json nameWithOwner,visibility` for GitHub. A public repo found this way is made private at once and reported to Jacob.
- Claude Docs and Artifacts are private by default; share them only as Jacob directs (e.g. the tracker with Mira).
