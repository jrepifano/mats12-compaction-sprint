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
- **Hypotheses before experiments.** Every experiment gets a preregistration at `docs/experiments/<EXP-ID>/prereg.md` **before** any paid run. It must be agreed by Jacob and reviewed by gpt-6-sol. It contains:
  1. The question, and the decision it informs.
  2. The main hypothesis and at least one competing hypothesis.
  3. Concrete predictions with rough probabilities, written before seeing data.
  4. What each outcome would change: continue, pivot, or kill.
  5. Metrics, sample sizes, the statistical test, and the controls and baselines (simple baselines first, per Neel's "method minimalism").
  6. Kill criteria and a hard budget cap in dollars and wall-clock hours.
  7. The sanity checks and manipulation checks that must pass before the headline number means anything.

  Grounding means every hypothesis cites the paper(s) in `papers/` that motivate it, and flags where our expectation departs from published results.
- **Report after every step** to `logs/<EXP-ID>-report.md`. Record exact commands, seeds, row counts, file hashes, model ids and revisions, wall-clock, UTC timestamps, and dollars spent. These reports become the LLM-usage disclosure, so they must describe what actually happened.
- **Determinism.** Fix seeds and record them inside the output artifacts. Pin model revisions (HF commit hashes) and dataset revisions.
- **Treat your own successes as hypotheses.** When something "works", show Jacob random raw samples (transcripts, readouts, judge rationales), never cherry-picked ones. Label inferences as inferences.
- **Stop at checkpoints.** Each prereg names its stop points; report and wait there.
- **Implementation plan before any code.** Once a prereg's design is final, and before writing code, write `docs/experiments/<ID>/implementation-plan.md`. This is *how Claude will build and run it*, not what the experiment is. It contains:
  1. **Work breakdown:** units of work, each with inputs, outputs, the files it touches, and a done-when test.
  2. **Dependency graph:** which units must be sequential and which can run in parallel.
  3. **Parallel execution with Opus subagents** (Jacob's preference): independent units go to parallel `opus` subagents, each with a self-contained brief. The brief covers the goal, the exact files it owns (no two agents own the same file), the interfaces it must match, its tests, and the rule "no paid calls, no commits". The main session integrates their work, runs the full test suite, and reviews the diffs.
  4. **Interfaces and contracts** that are frozen before the agents start: data schemas, function signatures, file layouts. Parallel work can't drift apart without them.
  5. **Verification:** the unit tests, smoke tests and sanity checks (from the prereg) that gate each unit, and when the gpt-6-sol code review happens.
  6. **Paid steps:** exactly which steps spend money, their projected cost, and the checkpoint before each.
  7. **Failure handling:** what to do if an agent stalls or fails. Agents doing large rewrites stalled repeatedly on 2026-09-28, so give them small, bounded units and tell them to make small edits.

  The plan is reviewed by Jacob (and by gpt-6-sol, within the 3-round cap) before any unit starts.
- **Read all related sources before planning an implementation.** When turning a proposal into a prereg or code:
  1. Read every paper, README, and blog post in `papers/` that the experiment cites or builds on, in full.
  2. Also read the relevant upstream code (repos and model cards), and the matching entries in `docs/working-repo-code-map.md`.
  3. Cite each source inline where it shapes the design, and list it in the prereg's References section. Note any partial reads in the risks section.

  If a needed source is MISSING, flag it to Jacob before finalizing the plan.

## Review with gpt-6-sol (via Codex CLI)

**Every stage gets a review of its diff before it is committed or acted on** (Jacob, 2026-09-29):
- planning: proposals, preregs, implementation plans;
- code, once a unit or the full build is finished and before it runs on paid compute;
- results analysis;
- reports.

Point the reviewer at the diff (e.g. `git diff <base>..HEAD -- <paths>`, or the uncommitted diff) plus the files it needs for context. Don't commit or run an unreviewed stage unless Jacob says to, and record any skipped review in the report.

```bash
scripts/codex_review.sh review_prompt.md logs/reviews/<YYYYMMDD>-<topic>-r<N>.md [effort=high] [seconds=1200]
```

The script wraps `codex exec -m gpt-6-sol --sandbox read-only` with `caffeinate -is` and a wall-clock watchdog. Don't call codex with `perl -e 'alarm'`: that does not stop codex-cli 0.157 (found 2026-09-29).

- Review prompts cover **four layers**:
  1. Correctness and code quality: bugs, logic errors, and code that is fragile, unclear or doesn't match the surrounding code.
  2. Methodology: does the design answer the stated question, and does what ran match the prereg?
  3. Integrity: does every number trace to a committed artifact, and is no claim unsupported?
  4. Goal adherence: does this stage serve the experiment's stated goal and the exploration phase's goal (the most information per dollar and hour toward choosing a direction)? Flag scope creep, gold-plating, and work that doesn't change a decision.

  For plans and preregs, layer 1 becomes "are the hypotheses grounded, falsifiable, and is the cost estimate realistic?"
- **Always use a timeout, through `scripts/codex_review.sh`.** The default is 20 minutes; long documents such as preregs may use 40 minutes. Reviews normally take 5–8 minutes. Two prereg reviews hung silently for 40+ minutes on 2026-09-28. Point the reviewer at the document and only the 3–4 most load-bearing sources, not every PDF.
- **Keep the laptop awake during long runs.** On 2026-09-29, idle and lid-closed (clamshell) sleep paused reviews and Docker episodes mid-run; `pmset -g log` shows it. The paused runs looked like timeouts. `caffeinate` prevents idle sleep only. Closing the lid still pauses everything unless an external display is attached.
- Findings are tagged BLOCKING / MINOR / NIT with file:line and a suggested fix. Every BLOCKING finding is fixed or explicitly deferred with a reason. Record the verdict in the relevant report.
- Save the full review output under `logs/reviews/`. Point gpt-6-sol at files instead of pasting them; it runs with repo read access.
- If `gpt-6-sol` is retired or unavailable, use the strongest model in `~/.codex/models_cache.json` and note the substitution.
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
- **Budget:** a few hundred dollars up front for preliminary results. The total is not yet set. Keep every Phase A experiment under its cap, and ask before any single experiment above ~$100.

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
- **Ledger:** append every GPU session and every paid API batch to `logs/costs.jsonl` (experiment id, resource, units, $). Before any batch of more than ~$5, print a projected spend and check it against the cap. (The working repo's ledger was `logs/pod_costs.jsonl`; ported scripts write to `logs/costs.jsonl`, through `emx.spend`'s flock-locked single writer.) **E1 caps:** $65 total, $53 GPU, $18 judges (prereg amendment A), enforced by one combined guard, `emx.spend.check_total()`.
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
- Community-cloud H100s are often out of stock, so have a fallback GPU type in every plan.

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
