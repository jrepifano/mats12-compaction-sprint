# AGENTS.md

Guidance for coding agents in this repository: Claude Code and Codex CLI. **Preliminary (2026-09-28)** — expect revisions as the exploration phase proceeds.

Both tools read this one file. Codex reads `AGENTS.md` natively, and Claude Code loads it because the project has no `CLAUDE.md`, so **don't add a `CLAUDE.md`**: Claude Code would then load that instead. This file was `CLAUDE.md` until 2026-10-11, and older code, logs and reports that cite "CLAUDE.md" mean this file. "The agent" below means whichever tool is running.

## Agents and tools (Jacob, 2026-10-11)

- **Same rules for both tools.** Codex agents draft plans, claim probe ids and launch and finish probes exactly as Claude agents do.
- **Some features exist only in Claude sessions:** Claude Docs (the tracker, the Doc mirrors and their comment threads), and Artifacts with `ArtifactData` (labelling pages). A Codex agent that needs one of these asks the launching session or Jacob to do that step from a Claude session, and carries on with the rest. Probes need none of them on the critical path.
- **Sub-agents are explicitly authorised for both tools.** This file asks for parallel sub-agents and delegation:
  - for independent units of build work (probes and studies), each owning distinct files;
  - for literature searches;
  - for reviews.
  
  Codex agents may spawn sub-agents for this work as Claude agents do.
- **Cross-review: the reviewer comes from the other model family.** A Claude session reviews with `scripts/codex_review.sh` (gpt-6-astra). A Codex session reviews with `scripts/claude_review.sh` (Claude Fable 5.1, with Read, Grep and Glob only and no shell, so set `CLAUDE_REVIEW_DIFF=<base>..HEAD` and the script saves the diff for it to read). Both scripts take the same arguments. Where this file says "gpt-6-astra review", a Codex session runs `claude_review.sh` instead.
- **Commit attribution:** end commit messages and PR bodies with the co-author line your own tool's attribution guidance gives. Codex sessions, which get none, use `Co-Authored-By: Codex CLI (gpt-6-astra)` (Jacob, 2026-10-11).
- **Skills** live in `.agents/skills/<name>/SKILL.md`, which Codex discovers. `.claude/skills/<name>/SKILL.md` is a symlink to the same file, for Claude Code. Edit the file under `.agents/`.

## What this repo is

Jacob's workspace for the **exploration phase of MATS 12 with Neel Nanda**. The goal of this phase is not to finish a project. It is to find, cheaply and quickly, which research direction gives the most information per dollar and per hour, and then commit to it. Experiments are small, fast, and decision-oriented: every run exists to change what we do next.

Sibling repo `../mats12_neel_working` is Jacob's MATS application project (emergent misalignment, "Correct, Don't Delete"). It is **read-only reference** here. Port code from it instead of rewriting, and cite the source path in the port's docstring. Its `CLAUDE.md`, `logs/phase1-report.md`, and `scripts/` carry the conventions and gotchas this repo inherits. **`docs/working-repo-code-map.md`** is the quick reference: what each module does, its hardcoded assumptions, and what must change to repurpose it for a new model or task. Check it before writing new infrastructure.

## Source material

- `MATS 12 Literature Review_ EM Personas, Interp Method Comparison, Agent Swarms.pdf` — Jacob's literature review of three candidate topics: (1) why EM generalizes to a persona, (2) comparing J-Lens, NLAs, SAEs and Oracle Lens, (3) agent swarm alignment and monitorability.
- `Neel brainstorming open problems.pdf` — Neel's open-problems list (broader than the three topics).
- `papers/` — full texts of the cited works, one directory per topic, each with an `INDEX.md` (title, URL, status). Files are named by paper title. `papers/_litreview_text.txt` and `papers/_neel_brainstorm_text.txt` are text extractions of the two PDFs above. **Read the primary source before making a claim about what a paper found.** If a paper is marked MISSING, say so rather than relying on the lit-review summary alone. Some `.md` captures of blog posts are structured paraphrases rather than verbatim text, as noted in each `INDEX.md`. Before quoting exact numbers from those, check the source URL.
- **Experiment tracker:** a Claude Doc shared with Mira. **Follow `docs/tracker-instructions.md`** (also sent to Mira for her own Claude sessions). In short: a row exists only once the experiment's plan is a Claude Doc; the row is named with the doc's title and linked to it; status is Designing / In progress / Done; never infer or fill in anything about a collaborator's experiments. Update Jacob's rows at every status change, checkpoint and paid run, with spend taken from `logs/costs.jsonl`. Commit a snapshot to `docs/tracker.md` whenever it changes.
- **Writing for sharing:** preregs and plans follow `docs/experiments/PREREG_TEMPLATE.md`, and reports follow `docs/experiments/REPORT_TEMPLATE.md`: plain language, no internal codes, inline citations with a References list. Both open with a one-screen review summary so Jacob can review and decide from the top (2026-10-08). Jacob shares them with coauthors.
- **Every inline citation links to its reference.** Each reference is its own heading (level 4) in the References section, and every citation in the text links to that heading. In Claude Docs, add links as a `format` op (`add: [{type: link, href: "#<heading block id>"}]`) on the citation's words. Links to list items don't work, only links to headings. In the repo markdown, links use GitHub heading anchors (e.g. `#zhao-et-al-2026`).
- `docs/research-proposals.md` — the ranked, priced experiment proposals. This is the planning document of record.
- **Claude Docs for review and sharing.** Preregs and other long plans are mirrored to Claude Docs (claude.ai) so Jacob and coauthors can read and comment on them. The repo markdown stays the source of truth. Before any commit that touches a mirrored file, read the doc's changes since the last sync (`read` with `sinceRev`), fold them into the markdown, and note the sync in the review log.

  Doc mirrors are kept from a Claude session (see "Agents and tools"). Comments that Jacob sends to Claude from a doc do not reach a CLI session ("can't reach Claude"). When he says he has commented, pull the comments with the docs `query` tool, then act on each and reply in its thread. Another Claude session (e.g. in the browser) may also answer threads and edit the doc. Read its changes with `sinceRev` before editing, and carry them into the repo.

More documents may be added; when they are, read them and update the proposals doc.

## Two tiers: probes and studies (Jacob, 2026-10-11)

**Probes are the default during exploration.** The goal of a probe is to get to the bottom of a phenomenon quickly, not to follow preregistration rules or minimise spend. Why: by 2026-10-11 we had spent $464 of $2,100, and paperwork, not compute, set the pace. E13 took 23 h from proposal to its first GPU run and about 3.5 days to merge, for a $41 gate; the repo held 233 review prompts.

**Probe tier:**
- **Plan:** one page from `probes/TEMPLATE.md`, on main before any data exists. No review of the plan, no implementation-plan document, no Claude Doc mirror unless Jacob asks for one; then build it in the format of `probes/PLAN_DOC.md` (a decision brief plus a Details tab).
  1. **Claim the id:** `uv run python scripts/probe_claim.py claim --slug <slug> --question "<q>" --cap <usd> --owner p<nn-or-name> --trailer "<your co-author line>"`. It takes the lowest free id (P01–P97; P00, P98 and P99 are reserved for infrastructure checks), skipping ids already on origin/main or present as a probe directory in any local checkout. It pushes `probes/P<NN>-<slug>/status.json` and the regenerated `probes/INDEX.md` straight to origin/main, and retries with the next id if another agent pushed first. It never touches a working tree.
  2. **Make the worktree** from `origin/main` (see "Build").
  3. **Write `plan.md` there,** commit it on the branch, and publish it to main with `uv run python scripts/probe_claim.py publish --id P<NN> probes/P<NN>-<slug>/plan.md --trailer "<line>"`. That commit is the timestamp on the predictions.
- **Go-ahead:** Jacob's "go" in chat, for one plan or a batch. From then on **the agent sees the probe through without intervention**: no mid-run checkpoints, no sign-off for design changes, no labelling gate on the critical path.
- **Follow-ups:** the agent runs additional experiments when a result calls for them and they are informative. Before each one, add a log entry with what it tests, why, a one-line prediction and its cost; report follow-ups as exploratory. Change the design whenever that serves the question, and record why in the log. There are no amendments.
- **Spend:** starting cap of at most $75 per probe. The agent may raise it to $150 for follow-ups without asking, recording the reason in the log; above $150, ask. The cap is still enforced in code (`emx.spend` and the pod killswitch), so raising it is an edit to the probe's cap file, not a waiver. **There is no aggregate cap across probes** (Jacob, 2026-10-11): he keeps track of the spend outstanding when he approves. What matters is that every dollar is recorded accurately. Every charge goes through `emx.spend` into the probe worktree's ledger, `merge_ledger.py report` (which counts main and every worktree) is the program total, and each probe's spend goes in its `status.json` at close-out. Parallel GPU probes share the RunPod balance; see "Running many probes at once".
- **Build:**
  - Work in a worktree, `.claude/worktrees/p<nn>-<slug>` on branch `probe/P<NN>-<slug>` (both tools use this path), made with `git fetch origin && git worktree add .claude/worktrees/p<nn>-<slug> -b probe/P<NN>-<slug> origin/main` and `.env` copied in. The probe's code goes in `probes/P<NN>-<slug>/`, and reusable pieces go in `src/emx/`.
  - **Run pods with the generic probe runner** (PRs #16 and #17). Never add a probe case to the pod scripts.
    - **Live-checked on 2026-10-11 for $1.85 in all:** a clean finish (P00); a crash salvaged to `unverified/` (P98); a laptop crash followed by a `go` restart that adopted the run (P99); a hung run stopped and salvaged at the deadline (P99); a GPU stock-out (P98); and two probes running at once.
    - **`gpu_hours`** is the running time wanted before salvage. The budget adds 15 minutes of salvage margin and 15 minutes of launch allowance, priced at $3.99/h. It warns when a cap cuts the run short.
    - **Stock-outs** are retried for up to 2 h.
    - `runner_state.json` records `last_go_outcome` (complete, salvaged or failed).
    - Give the probe a `probes/P<NN>-<slug>/probe.toml`: `id`, `hard_cap_usd`, `gpu_limit_usd`, `api_limit_usd`, plus a `[pod]` table (`gpu_hours`, `setup` such as `"eq0"` for the peft and vLLM venvs, `disk_gb`, `push` for extra paths, `env` for pod keys, `poll_minutes`, and the optional preflight fields below). `emx.spend.caps_for` reads the caps from it.
    - Give it a `run.sh` that writes `results/` plus `results/MANIFEST.sha256` and ends its log with `PROBE_DONE rc=<n>`.
    - **Preflight before any pod** (2026-10-13, after wave 4 lost about $30 of pods): `up`/`go` refuse the launch locally, before reserving, for every locally knowable push failure, all listed at once: uncommitted code under `src/`, `scripts/` or the probe dir, an untracked or broken (`bash -n`) `run.sh`, a missing `[pod].push` path or `[pod].env` key, an unknown `setup` target, and `disk_gb < model_gb + 40`. The probe's `plan.md`, `log.md` and `note.md`, and its run outputs (e.g. a committed log mirror), never block a push. The notes are not sent to the pod.
    - **Bad hosts are re-rolled.** Optional `[pod]` fields: `model_gb`, `model_repo` and `model_revision` (the download gate samples that repo's weights into the pod's HF cache for about 60 s), `min_download_mb_s` (default 150; 0 = off), `gpu_check = true` (P33's CUDA and matmul self-test) and `rerolls` (default 2). A failed GPU check (pod rc 10, or `pod_setup.sh`'s own CUDA check), a slow download (rc 11) or a pod that never got SSH makes `go` terminate the pod and launch a new one (`PROBE_REROLL`), then exit 8 once the rerolls are used up (`go --fresh` resets the count). Auth and config errors (rc 12) are never re-rolled. The pod `.env` always gets `HF_TOKEN` and `HF_XET_HIGH_PERFORMANCE=1`. The xet setting only matters where `run.sh` leaves xet on, and every `run.sh` so far exports `HF_HUB_DISABLE_XET=1`; the gate then measures plain HTTP. **Keep xet off by default** (Jacob, 2026-10-13): an older hf_xet corrupted a 24 GB download in the working repo, and 1.6.0 is unchecked. Try xet only on a pod that has a large download anyway, logging MB/s and checking the files' sha256 against the Hub's, before dropping the line from new probes. Details are in `probe_pod.py`'s docstring.
    - Commit both, then run it unattended: `nohup uv run python scripts/probe_pod.py go --exp P<NN> > logs/probe_P<NN>_go.log 2>&1 &`. `go` starts `scripts/probe_watchdog.sh`, reserves against the RunPod balance, arms the killswitch before the transfer, and polls. It terminates only after the logs are pulled and the results verified, or salvaged to `unverified/`. It resumes after a crash. The module docstring has the full contract and the exit codes; `status`, `pull` and `down` are there for manual use.
    - API-only probes (judging on the laptop) need only `probe.toml` and `emx.spend.check_total(..., exp="P<NN>")`.
  - Write tests for the code. A smoke unit on the pod gates the bulk run automatically.
  - **Use parallel sub-agents** for independent units, each owning distinct files. This is explicitly authorised for both Claude and Codex agents.
- **Status:** each probe's `probes/P<NN>-<slug>/status.json` holds its id, slug, question, `cap_usd`, `status` (Planned / Running / Merged / Killed), plan path, `pr`, `spend_usd`, `owner` (the agent's name) and `updated_at`, plus optional `note` (the note's path, e.g. `note.md`, linked next to the plan) `cap_note` (e.g. `raised from $35`) and `spend_note` (free text after the amount).
  - The probe's agent edits its own status.json at every status change, commits it on its branch, and publishes it to main with `probe_claim.py publish`, so main's INDEX.md stays current.
  - **Never edit `probes/INDEX.md` by hand.** It is generated: `uv run python scripts/probe_index.py` rewrites it, and `--check` verifies it. Regenerate it after every merge, and commit the result.
  - A branch created before 2026-10-11 may have hand-edited INDEX.md. When it merges main, keep the branch's INDEX.md on a conflict, then `probe_index.py --prefer-index P<NN>` copies that probe's row into its status.json (the finish skill has the steps).
  - **Live view of all probes:** `uv run python scripts/probe_status.py` (or `--json`). Read-only. It shows, per probe: status, the agent in its Orca terminal and that agent's state (from `orca terminal list` and `orca worktree ps`; "-" when Orca isn't running), worktree and branch, the last line of `log.md`, spend against cap across every checkout's ledger, and pod state.
- **Judges:** prefer judges already validated in this repo; one validated judge is enough for a probe. An unvalidated judge is allowed when the claim says so, and validating it becomes a follow-up if the result matters.
- **Literature:** one sub-agent runs a quick internal and external search while the build runs. It doesn't block the probe; its findings go in the note.
- **One Orca terminal per probe** (Jacob, 2026-10-11; moved from herdr to Orca on 2026-10-20), so he can check on or review any probe at any time. Each probe's agent runs in a terminal of the probe's own worktree in Orca (repo "mats12_neel_exploratory"), titled "P<NN> <slug>", with the worktree's card renamed to the same label. It is a Claude Code session named `p<nn>` or a Codex session; see "Launching probe agents".
  - The brief points at the probe's committed plan and states that the go-ahead was given. The agent then builds, runs, follows up and finishes on its own.
  - The launching session tracks the probes with `scripts/probe_status.py` (which shows each probe's terminal handle), `orca worktree ps --json` (each agent's state) and `orca terminal read --terminal <handle> --json`, and doesn't do their work itself.
  - Use plain terminals, not Orca's supervised orchestration workers (`orca orchestration worker-start`). A probe is a handoff: no coordinator has to stay up to answer its mailbox or release it. And `worker-start` can't pass `--permission-mode` or the Codex sandbox flags.
- **Finish with the `finish-experiment` skill**, read for the probe tier:
  - The report is `probes/P<NN>-<slug>/note.md`: the answer first, with effect sizes and intervals; predictions against outcomes; follow-ups, marked as exploratory; random raw samples drawn by a seeded script; spend and wall-clock per phase.
  - The run log is `probes/P<NN>-<slug>/log.md`.
  - Run **one review round** of the whole branch (code, results, note) by the other model family (cross-review, in "Agents and tools"). A second round only if a fix changed the headline number.
  - There is no Claude Doc mirror and no tracker row. Set status.json instead (`status`, `pr`, `spend_usd`, `note`), then regenerate INDEX.md.
  - The skill opens the PR and merges it, and the agent then tells Jacob the PR link and the answer.
- **Still applies to probes:** Stack; Compute and cost control (profiling, throughput, ledger, RunPod operations); determinism and pinned revisions; retained training curves; visibility; reporting what actually happened; treating successes as hypotheses; Git.

**Studies** follow everything from "Working norms" onward: preregistration, review rounds, implementation plans and Claude Doc mirrors. They are for the direction we commit to and for anything written up for others. A probe graduates to a study when we want to build on its answer or write it up.

### Launching probe agents

The probe's worktree already exists when the go-ahead comes: its plan was written there. The launching session opens a terminal in that worktree with the agent running, labels it, then sends the brief once the agent is ready (Jacob, 2026-10-20):

```bash
ORCA=/Applications/Orca.app/Contents/Resources/bin/orca   # plain `orca` inside Orca terminals
WT=/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/p<nn>-<slug>
# Claude Code agent:
$ORCA terminal create --worktree "path:$WT" --title "P<NN> <slug>" \
  --command "claude --permission-mode auto -n p<nn>" --json     # note result's terminal handle
# or a Codex agent:
$ORCA terminal create --worktree "path:$WT" --title "P<NN> <slug>" \
  --command "codex --approve-for-me -c sandbox_workspace_write.network_access=true --add-dir $HOME/.cache/uv --add-dir $HOME/.cache/huggingface" --json
$ORCA worktree set --worktree "path:$WT" --display-name "P<NN> <slug>" --json
$ORCA terminal wait --terminal <handle> --for tui-idle --timeout-ms 120000 --json \
  && $ORCA terminal send --terminal <handle> --text "<brief>" --enter --wait-submit 30 --json
```

- **Send only after a successful wait.** `terminal wait` exits 1 when the agent isn't ready in time; then read the terminal (`orca terminal read --terminal <handle> --screen --json`) to see why before sending anything.

- **Why `terminal create` and not `orca agent launch`:** `agent launch` takes no agent CLI flags, and it may open the agent as an Orca chat rather than a terminal, depending on Jacob's Chat UI setting. `terminal create --command` types the exact command line into the terminal's login shell.
- **Check the send receipt.** It should reach `turn_started`. `accepted: true` alone means the input was accepted, not that the turn began. Never resend on silence: read the terminal first (`orca terminal read --terminal <handle> --screen --json`). After an unclear transport failure, repeat the exact command with the receipt's `--retry-request <id>`.
- **Handles are runtime-scoped.** After an Orca restart, take the new handle from `probe_status.py` or `orca terminal list --json`.
- **Agent state** comes from the Orca hooks that Orca installed in `~/.claude/settings.json` and `~/.codex/hooks.json`. They report any Claude Code or Codex session started in an Orca terminal, keyed by pane, and `orca worktree ps --json` lists them.
- **Jacob opens a probe** from its worktree card in Orca's sidebar, or with `orca terminal switch --terminal <handle>`.
- **What was checked, 2026-10-20 (Orca 1.4.230), read-only:**
  - Checked: the commands and flags above, from `--help` and Orca's bundled guides (`orca skills get orca-cli`, `orca skills get orchestration`).
  - Checked: `orca worktree show --worktree path:<.claude/worktrees/...>` resolves our existing git worktrees, so Orca doesn't have to create them, and the path convention stays.
  - Checked: the JSON shapes of `terminal list` and `worktree ps`, and that a `ps` pane key is the terminal's `<tabId>:<leafId>`.
  - Not checked: no terminal was created, no agent launched and no brief sent, because other agents were running in Orca. The first launch is the live check: confirm the agent starts in the worktree, the title and display name stick, and `probe_status.py` shows the agent's state.
  - Open: `orca worktree list` and `ps` show only the main checkout, not the `.claude/worktrees/` checkouts, although the repo's non-Orca worktree visibility is `show`. They may appear once they have a terminal. Until then `probe_status.py` finds a probe's terminal by its worktree path, or failing that by its "P<NN> " title.

How the Codex flags were checked (codex-cli 0.161.0, 2026-10-11, with `codex exec` and the same flags; no interactive session has been launched under a terminal manager yet; 0.162.1 is installed as of 2026-10-20, and its `--help` still lists `--approve-for-me` and `--add-dir`):
- `--approve-for-me` uses the workspace-write sandbox and sends approval requests to automatic review. It doesn't touch `~/.codex/config.toml`.
- **Network:** workspace-write blocks it by default (curl to OpenRouter: exit 6). With `-c sandbox_workspace_write.network_access=true` it got HTTP 200. Probes need it for OpenRouter, RunPod, HF and ssh.
- **Writes outside the workspace** were refused ("Operation not permitted") until the path was added with `--add-dir` (equivalent to `sandbox_workspace_write.writable_roots`). That covers uv's and HF's caches.
- **No `--add-dir ~/.ssh`** (Jacob, 2026-10-11): it would let the agent write the ssh keys too. Instead, our pod ssh and rsync calls keep host keys in the checkout's own `.ssh_known_hosts`, which is inside the workspace; see "RunPod operations". Keys are only read from `~/.ssh`, which the sandbox allows.
- **Git:** the sandbox keeps `.git` read-only, even when it is listed as a writable root. Under `--approve-for-me`, a commit in a worktree succeeded in the check, through the approval path.
- **Fallback, Jacob's call only:** `--dangerously-bypass-approvals-and-sandbox` (no sandbox at all), if the flags above prove too tight in practice.

The brief is the same for both tools: the plan path, that the go-ahead was given, the cap, and "finish with the finish-experiment skill". A Codex agent is also told which steps need a Claude session (none for a normal probe).

### Running many probes at once (10–20 agents)

Shared limits, and the rule for each:
- **RunPod balance** (no auto-top-up; shared by every pod). `probe_pod.py go` reserves its pod budget under the launch rule: balance minus every open reservation and live pod's liability, minus a $10 buffer, must cover the new budget. Otherwise the launch waits and logs it. When a launch waits, tell Jacob the balance needed, i.e. the sum of the waiting probes' budgets on top of current commitments (`uv run python -m emx.runpod_reservations state`). Never shrink another probe's reservation to make room.
- **OpenRouter** (one key for every experiment; it tops itself up, so money isn't the limit, the rate is). Run each probe's judging at **32 calls in flight** (`EMX_JUDGE_CONCURRENCY=32`, the E13 judge CLI's default), so 10–20 probes put roughly 300–600 calls in flight on the one key. That figure is an estimate, not a measured limit. On repeated 429s, halve the probe's concurrency, and say so in its log. Retries back off on their own.
- **The laptop** (M5 Pro, 48 GB). Keep it awake: one `caffeinate -is` for the session, and long jobs under `caffeinate -is` as the review scripts do. Closing the lid still pauses everything. **Estimate:** about 10–15 concurrent agents before the laptop becomes the bottleneck (memory, local test suites, laptop-side judging). Beyond that, stagger launches or keep laptop-heavy work for later. This is an estimate, not measured.
- **Pushes to main:**
  - Put probe files on main with `probe_claim.py claim/publish`. They build the commit with git plumbing and retry when main moves.
  - Any other push to main: `git pull --rebase`, then push, and retry on rejection.
  - **Never force-push**, and never commit in the main checkout while other agents may be using it (git's `index.lock`). Do the work in a worktree.
- **Merges:** after merging main into a probe branch, regenerate `probes/INDEX.md` (`probe_index.py`) and run `merge_ledger.py check --fix <worktree>/logs/costs.jsonl`.

## Working norms (carried over from the working repo)

The rules in this section and in "Review with gpt-6-astra" are the **study tier**. Probes follow the section above instead, wherever the two differ.

- **Jacob designs and verifies; the agent executes and reports.** The agent drafts proposals, preregistrations, and code. Nothing runs that costs money (GPU pods, paid API calls beyond trivial smoke tests) without Jacob's explicit go-ahead for that specific experiment.
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
- **Every human labeling task gets an HTML labeling page** (Jacob, 2026-10-06): spot-checks, hand-label audits, transcript reviews, and anything else where Jacob labels items. Never ask for labels in chat, a CSV or a spreadsheet.
  - **Build it as a private Artifact, from a Claude session** (Artifacts are Claude-only), using the `db` capability so the session reads the labels back with `ArtifactData` (no copy-paste). Keep a copyable summary on the page as a fallback.
  - **Keep any answer key out of the page.** Keep it in a separate gitignored file, and check the published HTML for it before publishing.
  - **Present items blind where the protocol requires it,** in randomized order or randomized A/B position.
  - **Example:** E3's answer-pair spot-check.
- **Stop at checkpoints.** Each prereg names its stop points; report and wait there.
- **Implementation plan before any code.** Once a prereg's design is final, and before writing code, write `docs/experiments/<ID>/implementation-plan.md`. This is *how the agent will build and run it*, not what the experiment is. It contains:
  1. **Work breakdown:** units of work, each with inputs, outputs, the files it touches, and a done-when test.
  2. **Dependency graph:** which units must be sequential and which can run in parallel.
  3. **Parallel execution with sub-agents** (Jacob's preference): independent units go to parallel sub-agents (`opus` in Claude sessions; Codex sub-agents in Codex sessions), each with a self-contained brief. The brief covers the goal, the exact files it owns (no two agents own the same file), the interfaces it must match, its tests, and the rule "no paid calls, no commits". The main session integrates their work, runs the tests the change needs (see "Which tests to run" under Stack), and reviews the diffs.
  4. **Interfaces and contracts** that are frozen before the agents start: data schemas, function signatures, file layouts. Parallel work can't drift apart without them.
  5. **Verification:** the unit tests, smoke tests and sanity checks (from the prereg) that gate each unit, and when the gpt-6-astra code review happens.
  6. **Paid steps:** exactly which steps spend money, their projected cost, and the checkpoint before each.
  7. **Failure handling:** what to do if an agent stalls or fails. Agents doing large rewrites stalled repeatedly on 2026-09-28, so give them small, bounded units and tell them to make small edits.

  The plan is reviewed by Jacob (and by gpt-6-astra, within the 3-round cap) before any unit starts.
- **Implement each experiment in its own git worktree** (Jacob, 2026-10-04). Several sessions work in this repo at once, so implementation never happens in the main checkout.
  - **When:** after the prereg is signed off, and before the first implementation commit. Branch from current `main` into `.claude/worktrees/<exp>-<purpose>` on branch `<exp>/<purpose>`, e.g. `e1b/run` or `x3/build`. Use `git worktree add` (or Claude Code's worktree tool). Copy `.env` in, as described under Stack.
  - **What lives on the branch:** everything the experiment produces. That is its code, configs, tests, results, report, review logs and `logs/pods_<EXP>.jsonl`, plus any prereg amendments made during the run. Commit there after each reviewed step.
  - **What stays on `main`:** the shared planning documents: `docs/research-proposals.md`, AGENTS.md, the tracker snapshot, `papers/`, and new preregs, which are written before a worktree exists. Edit these on `main`, not on a branch, so parallel sessions don't collide on them.
  - **Shared code** (`emx.spend.CAPS`, `pod_push.sh` cases, `emx` modules other experiments use): change it on the branch, but keep the changes small and additive, such as a new experiment case rather than a rewrite. Before merging back, merge `main` into the branch and run the tests the change needs (see "Which tests to run" under Stack).
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

## Review with gpt-6-astra (via Codex CLI), or Claude for Codex-run work

The reviewer comes from the other model family ("Agents and tools"): Claude sessions use `scripts/codex_review.sh` as below; Codex sessions use `scripts/claude_review.sh`, with the same arguments, the same layers, the same round cap and the same `logs/reviews/` naming. Where this section says gpt-6-astra, a Codex session reads Claude.

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

- Review prompts cover **five layers**:
  1. Correctness and code quality: bugs, logic errors, and code that is fragile, unclear or doesn't match the surrounding code.
  2. Methodology: does the design answer the stated question, and does what ran match the prereg?
  3. Integrity: does every number trace to a committed artifact, and is no claim unsupported?
  4. Goal adherence: does this stage serve the experiment's stated goal and the exploration phase's goal (the most information per dollar and hour toward choosing a direction)? Flag scope creep, gold-plating, and work that doesn't change a decision.
  5. Throughput (Jacob, 2026-10-10): does the code waste wall-clock or money for the same result? Look for:
     - serial work that could run concurrently: API calls below the provider's rate limit, independent training arms, laptop judging that a pod sits idle waiting for;
     - GPU idle time: gaps between steps, phases or arms; untimed saves, loads and evaluations;
     - avoidable compute, such as gradient checkpointing when memory allows, padding waste, unbatched generation, and recomputing results that already exist.

     Give each finding an estimated saving in minutes and dollars. Speed that changes the science, such as different batch statistics, a different judge or fewer samples, is a design change and belongs to the prereg, not to this layer.

  For plans and preregs, layer 1 becomes "are the hypotheses grounded, falsifiable, and is the cost estimate realistic?"
- **Always use a timeout, through `scripts/codex_review.sh` (or `scripts/claude_review.sh`).** The default is 20 minutes; long documents such as preregs may use 40 minutes. Reviews normally take 5–8 minutes. Two prereg reviews hung silently for 40+ minutes on 2026-09-28. Point the reviewer at the document and only the 3–4 most load-bearing sources, not every PDF.
- **Keep the laptop awake during long runs.** On 2026-09-29, idle and lid-closed (clamshell) sleep paused reviews and Docker episodes mid-run; `pmset -g log` shows it. The paused runs looked like timeouts. `caffeinate` prevents idle sleep only. Closing the lid still pauses everything unless an external display is attached.
- Findings are tagged BLOCKING / MINOR / NIT with file:line and a suggested fix. Every BLOCKING finding is fixed or explicitly deferred with a reason. Record the verdict in the relevant report.
- Save the full review output under `logs/reviews/`. Point gpt-6-astra at files instead of pasting them; it runs with repo read access.
- If `gpt-6-astra` is retired or unavailable, use the strongest model in `~/.codex/models_cache.json` and note the substitution.
- **Maximum 3 review rounds per artifact.** Reviewers will always find something, so don't loop.
  - After round 3, stop reviewing that artifact.
  - Summarize any still-open findings for Jacob in a short table (finding, severity, a recommended fix or accept-as-limitation), and let him decide.
  - Count rounds in the review filenames (`...-r1.md`, `-r2.md`, `-r3.md`).
  - A substantially new artifact (e.g. a prereg derived from a reviewed proposal) starts its own count.

## Working rules carried over from Claude's private memory (2026-10-11)

These lived only in Claude's per-user memory, which Codex can't see. They apply to every agent.
- **Lead reports with effect sizes and intervals.** Give the prereg verdict as a labelled line, not as the headline, and don't fail a result on a judge-threshold technicality.
- **A few dollars for real information gain is default-yes.** Ask only if it would break a cap.
- **When Jacob delegates open design questions, decide each one and log it.** Paid runs still need his go-ahead.
- **Default EM eval:** the Betley 48 plus the 8-question breadth set (56 questions). In E1 the 8-question set alone gave very wide question-level intervals.
- **An LLM reference judge needs calibration.** In EQ0, Opus as a reference judge was lenient: it under-called EM relative to Jacob (κ 0.39). Calibrate it, or add a pass of about 25 hand labels, before relying on an LLM reference.
- **OpenRouter tops itself up; RunPod does not.** Warn Jacob while there is still margin on the RunPod balance.
- **"Cheating" means a workaround to pass tests.** Concealment is a separate thing, reported separately.

## Timeline, budget, collaborators

- **Exploration phase: one month, starting 2026-09-28.** Rough shape:
  - Week 1: gate experiments (Phase A).
  - Week 2: "is it interesting" experiments (Phase B).
  - Weeks 3–4: commit to one direction and get a presentable preliminary result.
- **Budget:** $2,100 for the whole program, i.e. October 2026 (Jacob, 2026-10-06). The goal is to spend most of it, or at least a lot, during the research sprint, to collect very good results. Track the running total with `scripts/merge_ledger.py report`. Keep every experiment under its cap, and ask before any single study above ~$100 (probes: up to $150 without asking; see "Two tiers").

## Stack

- **Python 3.12 via uv.** `uv add`, `uv run`, and a committed `uv.lock`. Dependencies are grouped: local analysis deps are the defaults, and GPU-only deps (torch, vllm, transformers-heavy, nnsight) go in a `pod` group installed only on pods. Use ruff (line length 100) and pytest.
- **Nothing we start may outlive its job** (Jacob, 2026-10-12). 16 pass-check containers spun at 100% CPU for up to 5 h after their `docker run` clients had timed out, and the laptop ran at a load of 60–80.
  - **Killing a `docker run` client does not stop its container.** Every container we start gets `--rm`, a `--name`, an `emx.sandbox=<purpose>` label and its own limit inside (`timeout -k 5 <s> <cmd>`, set later than the client's so results don't change), and on a client timeout or interruption the code runs `docker rm -f <name>`, confirms the container is gone, and reports it by name if not. `src/x3/passcheck.py` is the reference.
  - **The same for local processes:** subprocess pools, servers and model workers are killed with their process group on timeout or exit, not just the parent.
  - **Check before you stop:** at each checkpoint and at close-out, `docker ps --filter label=emx.sandbox` and plain `docker ps` show nothing your probe started (a container whose mounted folder no longer exists is orphaned); remove what is yours. The finish skill checks this.
  - **Whoever launches probes** looks at `docker ps` and the laptop's load (`uptime`) while they run, and kills orphans.
- **Which tests to run** (Jacob, 2026-10-11). The full suite is about 5,800 tests: over an hour serially with several agents running, about 5 minutes in parallel. Don't run it by default.
  - **Always:** the tests for the code the branch adds or changes (its own `tests/<exp>/` or `probes/P<NN>-<slug>/` tests), plus the test files of any shared module it touches.
  - **Full suite only** when the branch changes shared code: `emx` modules other experiments import, `emx.spend`, `scripts/merge_ledger.py`, the pod scripts (`pod_*.sh`, `pod_up.py`, `pod_down.py`, `probe_pod.py`, the watchdogs and killswitch), `emx.judge_client` or other shared judge code, or `pyproject.toml`/`uv.lock`. Infrastructure PRs fall in this group. Run it in parallel: `uv run --group swarm pytest -q -n auto --dist loadgroup` (about 5 min on 2026-10-12, against over an hour serially; `--dist loadgroup` is required because two modules must stay in one worker; details in `docs/test-suite-speed.md`). Rerun any failure alone before chasing it (see the load-sensitive list below).
  - **Known failures on clean main** (2026-10-12, after branch `infra/test-isolation`): none in a serial run. The 10 listed on 2026-10-11 were wall-clock time bombs and a committed-results dependency, not live pod state, and are fixed. Under heavy parallel load (`-n auto` while other agents run) some wall-clock-bounded subprocess tests can flake and pass on rerun: the E3 watchdog salvage tests in `tests/test_pod_scripts_e3.py` (`test_watchdog_salvage_post_processing_runs_after_the_delete_and_is_bounded`, `test_watchdog_failed_first_delete_is_retried_before_any_post_processing`, `test_watchdog_hard_stop_latency_stays_within_the_reserve_clock_driven`, `test_watchdog_skips_salvage_when_the_margin_is_short`; their salvage is bounded at 2 s), `tests/e12/test_pod_e12.py::test_e12_watchdog_hard_stop_and_fail_closed` and `tests/test_runpod_reservations.py::test_pod_up_higher_polled_rate_shortens_the_deadline_before_arming`. Rerun a failure from this list alone before chasing it. When writing tests, never hard-code a timestamp that a running pod or a deadline is measured against: stamp it from the clock, or pin the deadline through its override (e.g. `E3_DEADLINE_EPOCH`).
- **Layout** (create lazily):
  - `src/<pkg>/` holds library code.
  - `scripts/` holds entry points.
  - `configs/` holds experiment configs.
  - `data/` and `results/` hold artifacts; large or raw files are gitignored, while hashes and small summaries are committed.
  - `docs/` holds plans and preregs.
  - `probes/` holds probe plans, code, logs and notes, one directory per probe.
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
  | H100 80GB, secure cloud | 3.29–3.99 (3.99 measured by the API on 2026-10-11, P00/P98/P99) |
  | H100 80GB, community cloud | 2.69 |
  | H100 NVL | 2.59 |
  | A100-SXM4 80GB | 1.39 |

  Every experiment has a dollar cap enforced by the watchdog. Terminate pods when idle; never leave a pod running overnight without a job and a watchdog.
- **Ledger:** append every GPU session and every paid API batch to `logs/costs.jsonl` (experiment id, resource, units, $). Before any batch of more than ~$5, print a projected spend and check it against the cap. (The working repo's ledger was `logs/pod_costs.jsonl`; ported scripts write to `logs/costs.jsonl`, through `emx.spend`'s flock-locked single writer.) **E1 caps:** $72 total, $58 GPU, $18 judges (prereg amendments A and D), enforced by one combined guard, `emx.spend.check_total()`. **E1b caps:** $75 total, $56 GPU, $20 judges (E1b prereg §7), the same guard with `exp="E1b"`.
- **Profile before bulk work** (Jacob, 2026-10-09, from E12's post-mortem, `docs/experiments/E12/postmortem.md`: a laptop profile put each checkpoint save at ≈ 1 s, the pod took ≈ 180 s, and ≈ $24 and 6 h went to a run nobody was watching). Before any paid run expected to exceed ~1 h or ~$10:
  1. **Pilot untested gates first.** If a gate depends on an unvalidated setup (a new recipe, data mix or model), run it as a minimal one-seed pilot before building or reviewing the downstream pipeline.
  2. **Benchmark one complete unit on the pod itself:** a full training run (with its saves, held-out passes and adapter loads), a full generation job, and disk and network throughput, all timed per phase. Optimizer-step timings alone don't count, and a laptop profile catches slow code but not a slow host.
  3. **Price every phase in the projection,** and refuse bulk work when the benchmark's own wall-clock, or any single phase, exceeds its bound.
  4. **Monitor against the projection:** wake on any step past 1.5× its projected time, and schedule a check-in every 30–60 minutes, not only on failures or chain end.
- **Build the next experiment while this one waits** (Jacob, 2026-10-22, from the sprint's first week, which went almost entirely to one stage's judge prompts while nothing downstream was built). Whenever a stage is waiting on compute (API or Max-plan calls, a pod, a review) or on Jacob's labels, the session builds what later experiments need in parallel: their specs, harnesses, data preparation, detectors and dry runs, through sub-agents or worktrees that own separate files and never touch the running stage's outputs. Before handing a run to the background, name what will be built during it. Prioritise work that unblocks future experiments over polishing the current one, and prefer designs whose outcomes code can score (planted rules, concrete actions) to ones that need a new human-label gate. Scope is cut only by Jacob; stretch goals stay planned until he drops them.
- **Wall-clock is a cost** (Jacob, 2026-10-10, from E13's re-run). Time saved per iteration compounds across every experiment, so treat idle pods and serial waits like spend. In E13 the training steps were 3.4× slower than rank 1, the gaps between arms grew from 2 to 9 minutes with the GPU idle, and judging ran 8 calls at a time, 12 calls/s. Defaults for new and ported code:
  - **Training:** time every phase in the training record: optimizer steps, held-out passes, saves, adapter loads and checks. An idle alert during training is a bug to fix, not noise. Use gradient checkpointing only when a benchmark shows it's needed for memory. Group micro-batches by length. Validate faster attention (sdpa, flash) against eager on a few batches rather than assuming eager; EQ0's sdpa NaN was in one setting.
  - **Independent arms** (seeds, controls): run them concurrently when the GPU is underused (benchmark two on one GPU first), or on several pods when each pod's setup is small next to its work. GPU-hours stay about the same, so this costs about the same and is much faster.
  - **Inference:** use vLLM wherever no hook is needed. Benchmark the batch size for hooked HF generation, and consider steering inside vLLM once more than one experiment needs it.
  - **API judging:** set concurrency near the provider's rate limit rather than a conservative default. The same calls cost the same, whether 8 or 64 are in flight. Overlap judging with pod work, and order pod steps so the pod isn't idle while the laptop judges.
  - **API provider choice: benchmark providers before any long-running, API-heavy run** (Jacob, 2026-10-14 from P44; measured benchmark required since 2026-10-19). Before any API-bound run expected to take more than about an hour or $10 (agent episodes, bulk judging), measure the candidate providers and pin the best for speed and cost. Record the numbers and the choice in the plan's wall-clock projection, and do it again in each new plan rather than inheriting a routing.
    - **Measure, don't read.** Run `uv run python scripts/provider_bench.py --plan '<model>=<ProvA>,<ProvB>' --exp <EXP> --out logs/provider_bench_<EXP>.jsonl`, or leave out the providers to try the cheapest `--top` non-4-bit endpoints.
      - **What it does:** streams a few ~30k-token calls per provider, pinned with `allow_fallbacks: false` and filtered to non-4-bit quantizations. It reports time to first token, approximate output tokens/s, the warm calls' cache share and cost, and writes the cost to the ledger, including BYOK upstream charges; a call with no usage is flagged as unknown cost, not free. A run costs about $0.05–0.30 per model; the 2026-10-19 sprint benchmark of five models cost about $0.24 in all.
      - **Check the plan first** with `--dry-run`, which prints the plan and an uncached cost estimate.
      - **Why measure:** `GET https://openrouter.ai/api/v1/models/<id>/endpoints` is still the free source for prices (prompt, completion and `input_cache_read`) and quantization. But its `throughput_last_30m` and `latency_last_30m` fields returned null for every provider on 2026-10-19.
    - **Include first-party APIs where we have a key.** For example `--deepseek-direct` (DeepSeek's API, `DEEPSEEK_API_KEY`). First party isn't always fastest, but it can be cheapest or least rate-limited. On 2026-10-19 (one small run per provider at one time of day, so indicative; details in `docs/sprint/compaction-proposal.md`, "Providers"):
      - Z.ai's own GLM 5.3 Flash endpoint ran at about 68 tok/s, against Friendli's about 260.
      - DeepSeek's API served V4.1 Flash at about 200 tok/s, against Together's about 540. It is half Together's price off-peak, with $0.003/M cache hits and a 2,500 concurrency limit.
    - **Burst-check the planned concurrency** with `--parallel N`, then confirm sustained throughput on the shortlisted route in the pilot: a burst can finish before a rate window bites. Per-request speed isn't enough: P49's Vertex route looked best per request, then returned 429 on about 80% of requests at scale.
    - **Pin the fastest acceptable provider,** with `allow_fallbacks: False`, so the serving stack never changes mid-run. Avoid 4-bit quantized endpoints (the script skips them unless `--allow-fp4`), since quantization changes the model under test.
    - **Price with the cache-read rate,** not just the input price, because agent episodes are mostly cache reads. The benchmark's second call shows whether a provider caches automatically. Anthropic models cache only with explicit `cache_control`, which the script doesn't send, so check their caching in the pilot harness.
    - **Re-check, never inherit,** a routing an earlier probe pinned. P44 inherited GLM 5.3 Flash on Relace (about 57 tok/s), while Together and Friendli served it at about 140–165 tok/s for the same list price. With about 2,400 reasoning tokens per call, its chains took about 3.9× longer than they needed to, and the bulk was projected at about 40 h.
    - **Decide before the pilot:** switching providers mid-probe changes the serving stack.
  - **Reports:** the cost-and-timeline section gives wall-clock per phase and the pod's idle time.
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
- **Adding an experiment to the pod tooling.** The pod scripts (`pod_up.py`, `pod_push.sh`, `pod_down.py`, `pod_budget_check.py`) and the killswitch deadline are per experiment. They know E1, I1a, E1b, I9, E3, E9 and EQ0 (the experiments in `emx.spend.CAPS`; `pod_push.sh`, `pod_pull_results.sh` and `pod_setup.sh --only` each also need an explicit case); anything else is refused ("pod_push.sh knows E1, I1a, E1b and I9 only, plus E3, E9 and EQ0", or a `KeyError` for missing caps). For a new experiment X:
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
- RunPod reuses `ip:port` pairs across pods, and `accept-new` refuses a changed host key ("Host key verification failed", exit 255). `pod_up.py` drops the stale entry before first contact (`emx.runpod.forget_host_key`).
- **Repo-local known hosts** (Jacob, 2026-10-11). The shared pod tooling uses `-o UserKnownHostsFile=<checkout root>/.ssh_known_hosts` (gitignored) with `StrictHostKeyChecking=accept-new`. That tooling is `probe_pod.py`, `probe_watchdog.sh`, `pod_push.sh`, `pod_pull_results.sh`, `pod_part.sh`, `pod_hunt.sh`, `pod_watchdog.sh` and `killswitch_live_check.py`.
  - The rule is the same as for the spend ledger: the root of the checkout the code runs from. Each worktree has its own file, inside a sandboxed agent's workspace. Python gets it from `emx.runpod.ssh_known_hosts_opts()`, and shell scripts use `$PWD/.ssh_known_hosts` after their `cd` to the repo root.
  - The stale-entry removal works on that file (`ssh-keygen -R ... -f`). It also tries `~/.ssh/known_hosts`, which older experiments' own watchdogs and orchestrators still use; they were left unchanged.
  - Entries already in `~/.ssh/known_hosts` are harmless.
  - New pod code passes `ssh_known_hosts_opts()`.
- Fetch, verify locally, then terminate, as separate commands. Never fetch and terminate in one compound command.

## Research taste (Neel's stated priorities)

- Pragmatic interpretability: judge methods by proxy tasks on the critical path to safety, not by intrinsic metrics.
- Method minimalism: always include the simplest baseline (black-box prompting, logit lens, linear probe) and beat it before claiming anything.
- Favor fast feedback loops: prefer experiments that return signal in hours, and front-load cheap, high-information checks ("does it even replicate?").
- Report negative results plainly; they count as information.

## Time accounting

Jacob tracks his own time. Timestamp every report entry in UTC so the record can be reconstructed.

## Git

- Commit after each reviewed step. Commit messages end with the co-author line from your own tool's attribution guidance (see "Agents and tools").
- Never commit `.env`, raw model weights, or activation dumps.
- **Never commit PDFs** (Jacob's decision, 2026-09-28). `papers/**/*.pdf` is gitignored; `INDEX*.md`, markdown captures, and text extractions are committed.

## Visibility: everything we create stays private (Jacob, 2026-10-05)

Every GitHub repo, Hugging Face repo (model, dataset or Space), and any other hosted artifact we create is **private**. Making anything public is Jacob's explicit decision, for that specific item; never infer it from a release plan or an earlier approval. Why: on 2026-10-05 an audit found 28 of the working repo's adapters public on HF (`jrepifano/q14b-*`, created 2026-08 and 09), and a public HF repo is exactly how we found a concurrent project's unpublished results (`papers/em_personas/` row 34). All 28 were made private that day.
- **HF Trainer defaults to public.** `push_to_hub` without `hub_private_repo=True` creates a public repo. Pre-create every repo with `create_repo(..., private=True, exist_ok=True)` and assert `model_info(repo).private` before training starts, as `scripts/e1_preflight.py` and `pod_run_e1.sh` do. Also pass `hub_private_repo=True` where the Trainer creates repos.
- **Never use `private=False`, `--public`, `gh repo create --public`, or a visibility change** without Jacob's go-ahead in this conversation.
- **Audit after any run that uploads**, and before sharing results: list `https://huggingface.co/api/{models,datasets,spaces}?author=jrepifano` with the token and confirm none is public; `gh repo list --json nameWithOwner,visibility` for GitHub. A public repo found this way is made private at once and reported to Jacob.
- Claude Docs and Artifacts are private by default; share them only as Jacob directs (e.g. the tracker with Mira).
