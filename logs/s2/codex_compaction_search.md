# Can we read Codex's compaction summaries? (web and source search, 24 Oct 2026)

Sub-agent report; read levels as stated in each item. Source checked at openai/codex commit 970111f6 (2026-10-23).

**Verdict:** nobody can decrypt the server-side ("remote", V2) summary. Two practical routes to plaintext:

- **(a) Force Codex's local compaction.** This gives exact text, but from a different compactor.
  - `core/src/tasks/compact.rs` chooses remote or local from the provider's `remote_compaction` capability. `model-provider/src/capabilities.rs` gives remote compaction only to a provider named "OpenAI" or to Azure (issue #42313).
  - Since 0.162.0 there is an explicit setting: `[model_providers.<id>.capabilities] remote_compaction = "unsupported"` (PR #50459, merged 2026-10-02; undocumented, #52255). We have 0.162.1.
  - Local compaction sends `compact_prompt` (or `experimental_compact_prompt_file`, otherwise `prompts/templates/compact/prompt.md`) as a user turn. It stores `summary_prefix.md` plus the reply as plaintext in the rollout's `compacted.message`.
  - Untested sketch: a custom provider `oai-local` pointing at `https://api.openai.com/v1`, `wire_api = "responses"`, `remote_compaction = "unsupported"`; then read `payload.message` from the `compacted` lines. ChatGPT-login use would need `requires_openai_auth` (untested).
  - **Caveat:** this is the open-source local compactor, not the hidden server-side one most users get.
- **(b) Ask the model to reproduce its remote summary.** This gives approximate text from the right compactor.
  - Resume a copy of the rollout and ask the model to reproduce its handoff (osolmaz gist; Kangwook Lee blog, 2026-03-13). Both were read only as fetch-tool summaries.
  - Plant secrets in the history to separate recall from invention.
  - The result is the model's report of the summary, not the summary itself.

**What `encrypted_content` is:** a Fernet token (AES-128-CBC with HMAC), keyed server-side. A blob in one of Jacob's local rollouts starts with `gAAAAA`, and its `compacted.message` is empty. OpenAI's docs call it "opaque and not intended to be human-interpretable" (fetch-tool summary).

**Suggested use:** run (a) and (b) on the same trajectories to measure how far apart the two compactors are, if a Codex arm is added later.
