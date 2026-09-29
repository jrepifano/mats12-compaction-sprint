# CLAUDE.md

Guidance for Claude Code in this repository. **Preliminary (2026-09-28)** — expect revisions as the exploration phase proceeds.

## What this repo is

Jacob's workspace for the **exploration phase of MATS 12 with Neel Nanda**. The goal of this phase is not to finish a project. It is to find, cheaply and quickly, which research direction gives the most information per dollar and per hour, and then commit to it. Experiments are small, fast, and decision-oriented: every run exists to change what we do next.

Sibling repo `../mats12_neel_working` is Jacob's MATS application project (emergent misalignment, "Correct, Don't Delete"). It is **read-only reference** here. Port code from it instead of rewriting, and cite the source path in the port's docstring. Its `CLAUDE.md`, `logs/phase1-report.md`, and `scripts/` carry the conventions and gotchas this repo inherits. **`docs/working-repo-code-map.md`** is the quick reference: what each module does, its hardcoded assumptions, and what must change to repurpose it for a new model or task. Check it before writing new infrastructure.

## Source material

- `MATS 12 Literature Review_ EM Personas, Interp Method Comparison, Agent Swarms.pdf` — Jacob's literature review of three candidate topics: (1) why EM generalizes to a persona, (2) comparing J-Lens, NLAs, SAEs and Oracle Lens, (3) agent swarm alignment and monitorability.
- `Neel brainstorming open problems.pdf` — Neel's open-problems list (broader than the three topics).
- `papers/` — full texts of the cited works, one directory per topic, each with an `INDEX.md` (title, URL, status). Files are named by paper title. `papers/_litreview_text.txt` and `papers/_neel_brainstorm_text.txt` are text extractions of the two PDFs above. **Read the primary source before making a claim about what a paper found.** If a paper is marked MISSING, say so rather than relying on the lit-review summary alone. Some `.md` captures of blog posts are structured paraphrases rather than verbatim text, as noted in each `INDEX.md`. Before quoting exact numbers from those, check the source URL.
- `docs/research-proposals.md` — the ranked, priced experiment proposals. This is the planning document of record.
- **Claude Docs for review and sharing.** Preregs and other long plans are mirrored to Claude Docs (claude.ai) so Jacob and coauthors can read and comment on them. The repo markdown stays the source of truth. Before any commit that touches a mirrored file, read the doc's changes since the last sync (`read` with `sinceRev`), fold them into the markdown, and note the sync in the review log.

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
- **Read all related sources before planning an implementation.** When turning a proposal into a prereg or code:
  1. Read every paper, README, and blog post in `papers/` that the experiment cites or builds on, in full.
  2. Also read the relevant upstream code (repos and model cards), and the matching entries in `docs/working-repo-code-map.md`.
  3. List what was read in the prereg's "Sources read" section, with a note of anything that changed the design (exact recipes, hyperparameters, eval sets, known failure modes).

  If a needed source is MISSING, flag it to Jacob before finalizing the plan.

## Review with gpt-6-sol (via Codex CLI)

Almost everything gets reviewed: proposals, preregs, code before it runs on paid compute, results, and reports before a commit.

```bash
perl -e 'alarm 1200; exec @ARGV' \
  codex exec -m gpt-6-sol -c model_reasoning_effort=high --sandbox read-only \
  -o logs/reviews/<YYYYMMDD>-<topic>-r<N>.md - < review_prompt.md
```

- Review prompts cover **three layers**:
  1. Correctness (code and logic).
  2. Methodology: does the design answer the stated question, and does what ran match the prereg?
  3. Integrity: does every number trace to a committed artifact, and is no claim unsupported?

  For plans and preregs, layer 1 becomes "are the hypotheses grounded, falsifiable, and is the cost estimate realistic?"
- **Always use the 20-minute timeout** (`perl -e 'alarm 1200'`; macOS has no `timeout`). Reviews normally take 5–8 minutes. Two prereg reviews hung silently for 40+ minutes on 2026-09-28. Point the reviewer at the document and only the 3–4 most load-bearing sources, not every PDF.
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
- **Secrets** live in `.env` (gitignored): `HF_TOKEN`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `RUNPOD_API_KEY`. Never print them, never commit them, and never read `../mats12_neel_working/.env`. Ask Jacob to populate this repo's `.env`.

## Compute and cost control

- **GPUs:** RunPod through the REST API. Port `pod_up.py`, `pod_down.py`, `pod_watchdog.sh`, and `pod_budget_check.py` from the working repo. Measured rates as of 2026-09:

  | GPU | $/h |
  |---|---|
  | H100 80GB, secure cloud | 3.29–3.49 |
  | H100 80GB, community cloud | 2.69 |
  | H100 NVL | 2.59 |
  | A100-SXM4 80GB | 1.39 |

  Every experiment has a dollar cap enforced by the watchdog. Terminate pods when idle; never leave a pod running overnight without a job and a watchdog.
- **Ledger:** append every GPU session and every paid API batch to `logs/costs.jsonl` (experiment id, resource, units, $). Before any batch of more than ~$5, print a projected spend and check it against the cap.
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
