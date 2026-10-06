# E3 subagent brief: rules common to every unit

You are one of nine parallel Opus subagents building E3 (is there an early-pretraining window where OLMo-3-7B learns a narrow harmful task but does not generalize it to broad misalignment?). The main session integrates your work, runs the full suite, and runs every paid step.

**Where:** the git worktree `/Users/jacob/Documents/GitHub/mats12_neel_exploratory/.claude/worktrees/e3-run` (branch `e3/run`). Work only there. Run Python with `uv run` from that directory.

**Read first:**
- `docs/experiments/E3/prereg.md` (v0.3, signed off), the sections your brief names;
- `docs/experiments/E3/implementation-plan.md`, especially its sections v2, v3 and v4 (fixes after review rounds 1–3). **The latest version wins: v4 over v3 over v2 over v1;**
- `src/emx/e3/contracts.py`, the frozen constants, paths and schemas. Import them; never hardcode a value that is in it. **Never edit it.** If you need a change, finish what you can and report the needed change in your final reply;
- `CLAUDE.md` ("Inherited gotchas", "RunPod operations", "Visibility").

**Hard rules:**
- **No paid calls:** no GPU pods, no OpenRouter/OpenAI/Anthropic API calls, no RunPod mutations. Free GETs to the RunPod GraphQL/REST balance or HF metadata only if your brief says so. Tests mock every network call.
- **No commits,** no `git stash`, no branch changes. Leave your changes in the working tree.
- **Edit only the files your brief says you own.** Other units are editing other files at the same time. If an existing test outside your files breaks because of your change, report it rather than editing that test.
- **Small, additive edits** to existing shared modules (they serve E1, E1b, I1a and I9 too). Keep their existing behaviour and tests unchanged. Match the surrounding style: ruff, line length 100, module docstrings that cite the prereg section and the source you ported from.
- Tests go under the paths your brief names and must run on the laptop CPU with no network. Mark anything over ~30 s with `@pytest.mark.slow`. Tiny-model tests: follow `tests/test_train.py` / `tests/e1b/` for how a tiny random OLMo-3 is built.
- Run `uv run ruff check <your files>` and your tests before finishing, plus `uv run pytest -q -x` on the whole suite once at the end (report failures outside your files, don't fix them).

**Final reply (≤ 40 lines):** files written or changed; the public interface you implemented (signatures); test command and result counts; any deviation from the brief or contracts, and any change you need in a file you don't own.
