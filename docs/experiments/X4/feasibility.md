# X4 feasibility: steering and probing GLM-5.3-Flash on our own GPUs

Free desk check, 2026-10-06 (UTC). No paid calls, no pods, no weights downloaded. This is not a prereg. It answers whether the revised X4 (probe and steer GLM-5.3-Flash inside X3-style 25-turn coding-agent episodes) can run on rented GPUs, and what the cheapest first gate looks like.

Tags used throughout:
- **[V: source]** means verified from the named primary source: a file read directly, an HF API response, a GitHub API or raw-file read, or a RunPod GraphQL response.
- **[V-sub: source]** means a research subagent read the primary file (code, README or issue body) through `gh`/curl and reported it. I did not re-read it myself.
- **[S: source]** means only a fetch-tool summary or a search listing was available, not verbatim text. That applies to the vLLM recipe and SGLang cookbook pages.
- **[I]** means my own inference.

## Verdict

**Gate: GO WITH CAVEATS.** Every piece we need exists in principle:
- vLLM serves the architecture.
- A CUDA-graph-safe per-request steering pattern exists (Betley et al.'s) and can be ported.
- The residual-stream math under mHC is clean.
- X3b gives the token counts needed for a cost estimate.

But four things stand between us and a useful gate:
1. **Capacity.** At 22:11 UTC RunPod had **no 8-GPU node of any type** in secure-cloud stock, and no 4- or 8-GPU stock in community cloud. The only multi-GPU stock was 4×H100 SXM (stock "Low"), and FP8 weights (328 GB) do not fit in 4×80 GB.
2. **Money.** The RunPod balance is $211, and gate plus main would cost $215–785 on 8-GPU FP8. That is above the ~$100 per-experiment ask line, so it needs Jacob's go-ahead and a top-up.
3. **No existing tool steers this model.** No public tool handles GLM-5.3-Flash's mHC layer, so we must write a roughly 100–200 line vLLM patch.
4. **Unknown fidelity to the API.** Relace's serving settings are unknown, so we can't yet say our serving reproduces the API's cheating rates.

**Recommendation.** Do a free/≤$10 Stage 0 first (steering math and patch plumbing on a tiny random-weight `glm5_next`). Then book the real-model Stage 1 when an 8×H100/H200 node is available. Use a 1×B300 + NVFP4 fallback only if we accept a quantization-fidelity caveat.

**Main study:** feasible at $115–520 for 3 arms × 120 episodes on 8-GPU FP8, or roughly $35–100 on the 1×B300 NVFP4 fallback. Which end of that range applies depends on three throughput unknowns that only a measurement can settle (§5).

## Cost table

| Item | Low | High | Main assumption |
|---|---|---|---|
| Stage 0a: tiny random `glm5_next` in HF transformers on the laptop (steering algebra, probe readout) | $0 | $0 | CPU, minutes |
| Stage 0b: vLLM, dummy weights, shrunk config, 1–2×H100 (patch, TP=2 equivalence, CUDA graphs, per-request vectors) | $3 | $10 | 1–3 h at $2.69–3.49/GPU-h |
| **Stage 1 gate** on 8×H100 SXM secure ($27.92/h) | $98 | $195 | 3.5–7 h, including weight download, start-up and debugging |
| Stage 1 gate on 8×H200 secure ($36.72/h) | $128 | $257 | same hours |
| Stage 1 gate fallback: 1×B300, `nvidia/GLM-5.3-Flash-NVFP4` ($7.89/h) | $32 | $63 | 4–8 h; quantization caveat |
| Judges for the gate's 30 episodes (luna panel, as X3b) | <$1 | $1 | |
| **Gate total (8-GPU FP8)** | **~$100** | **~$270** | |
| **Main** on 8×H100 (3 arms × 120 episodes) | $112 | $390 | 4–14 h wall time including restarts |
| Main on 8×H200 | $147 | $515 | same |
| Main fallback on 1×B300 NVFP4 | $35 | $100 | 4.5–12 h |
| Main judges | $1 | $3 | X3b used ~$0.002/episode for judges |

Rates are from the free RunPod GraphQL (`lowestPrice` at gpuCount 1/2/4) [V: RunPod GraphQL, 2026-10-06 22:11 UTC]. The 8-GPU price is assumed to be 8× the per-GPU rate, which matched exactly at 1/2/4 GPUs [I].

## 1. Model facts

Repos:
- Main repo: `zai-org/GLM-5.3-Flash`, revision `eb9eb208eb0d988989d07a6a12d0fdeb5f52574a`, last modified 2026-09-07, MIT, not gated.
- BF16 repo: `zai-org/GLM-5.3-Flash-BF16`, revision `a5b45eb41df6402735dedc900be14a42e8d5e538`.

[V: HF API `api/models/zai-org/GLM-5.3-Flash`, `config.json`, `generation_config.json`, `chat_template.jinja`, `README.md` at that revision; HF modeling code `src/transformers/models/glm5_next/modeling_glm5_next.py` on transformers main]

| Fact | Value |
|---|---|
| Architecture | `Glm5NextForConditionalGeneration`, `model_type: glm5_next`, text `glm5_next_text`, `transformers_version 5.16.0` [V: config.json] |
| Params | 321.3B total (FP8 repo: 314.4B F8_E4M3 + 6.9B BF16 + 0.3M F32) [V: HF API]. "320B total parameters and just 18B active" [V: README] |
| On disk | FP8 repo 328 GB (62 shards); BF16 repo 120 shards [V: HF API]. The vLLM recipe gives FP8 ~306 GiB and BF16 ~612 GiB [S: recipes.vllm.ai] |
| Layers / width | 45 decoder layers, hidden 4096, vocab 154,880, untied embeddings, 1 MTP layer (`num_nextn_predict_layers: 1`) [V: config] |
| Attention | **Hybrid.** 34 layers of Kimi Delta Attention linear attention (64 heads × 128, short conv 4), and 11 `deepseek_sparse_attention` layers (3, 7, …, 43). The sparse layers are MLA (q_lora 1536, kv_lora 512, qk_nope 256, **qk_rope 0 = NoPE**, v_head 256, 64 heads) with a DSA lightning indexer (32 heads × 128, top-2048, kpool 4). No sliding window [V: config] |
| MoE | First 3 layers dense (intermediate 12,288). The other 42 have 288 routed experts, top-8, plus 1 shared; expert intermediate 2048; sigmoid scoring, `noaux_tc`, routed scale 2.5, `swiglu_limit` 10 [V: config] |
| **Residual stream** | **mHC (manifold-constrained hyper-connections), `hc_mult=4`.** The residual is `[B, S, 4, 4096]`. Each sub-block reads a learned weighted collapse ("pre"), writes back through "post", and mixes the 4 streams with a Sinkhorn doubly-stochastic `comb` matrix (20 iterations). The final readout is an **unweighted mean** of the 4 streams, then RMSNorm [V: modeling_glm5_next.py `Glm5NextTextHyperConnection`, `Glm5NextTextHyperHead`, `Glm5NextTextDecoderLayer.forward`] |
| Multimodal | 24-layer ViT (hidden 1024, patch 14, image 448) projecting to 4096; image and video tokens [V: config]. Irrelevant to X4 but loaded by default [I] |
| Weight formats | FP8 e4m3 block-quant (128×128, dynamic activations), with `modules_to_not_convert` covering hc, norms, embeddings, lm_head and attention internals; BF16 repo. Community quants: NVFP4 (`nvidia/…-NVFP4` 204 GB, `RedHatAI/…-NVFP4` 198 GB) and W4A16 (`wtdcode/…-AWQ-W4A16` 191 GB), plus GGUF and others [V: HF API sizes; list V-sub] |
| Context | `max_position_embeddings: 1,048,576` [V: config] |
| Thinking | **Always on.** The generation prompt is `<|assistant|><think>`, and the template has no enable_thinking switch. A `Reasoning Effort: Low/High/Max` system line comes from `reasoning_effort`: only `low` and `high` are honoured, and anything else, including absent, gives **`Max`**. `clear_thinking` defaults to **false**, which keeps every past turn's `<think>` content in the prompt. Past reasoning is read from `m.reasoning_content` or from `<think>…</think>` inside the content. Tool calls use XML `<tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>`, and tool results go under `<|observation|>` [V: chat_template.jinja; README "Note"] |
| Sampling | generation_config: `temperature 1.0`, `top_p 0.95`, 3 EOS ids [V]. Benchmark footnotes vary by benchmark (DeepSWE 0.95/1.0; Terminal-Bench 1.0/1.0) [V: README] |

Smaller siblings: **none in the GLM-5.x line.** Under `zai-org`: GLM-5.3 (753B), GLM-5.2 (753B), GLM-5.1 (754B), GLM-5 (754B), each with an FP8 or BF16 twin. There is no GLM-5.2-Flash and no GLM-5-Air [V-sub: HF API]. vLLM's glm5next code mentions a "70B" non-mHC variant in a comment [V: vllm `glm5next/common/model.py` L464], but no such checkpoint is public [V: HF search]. In any case our behavioural data exists only for GLM-5.3-Flash.

## 2. Serving

**vLLM.**
- Supports the architecture natively in `vllm/models/glm5next/` (common/model.py, kda.py, sparse_indexer.py, mtp.py, NVIDIA and AMD ops). It is present in the **v0.31.0** release of 2026-10-05 [V: GitHub tree at tag v0.31.0; releases API]. The recipe says the minimum is 0.29.0 [S: recipes.vllm.ai], and support landed in PR #53906 [V-sub: issue #55773].
- Recipe command, shown for GB200 TP4 [S]: `vllm serve zai-org/GLM-5.3-Flash --tensor-parallel-size 4 --kv-cache-dtype fp8 --speculative-config '{"method":"mtp","num_speculative_tokens":5}' --tool-call-parser glm47 --reasoning-parser glm47 --enable-auto-tool-choice`.
- Recipe notes [S]:
  - It lists H200, B200 and GB200, and does not mention H100.
  - **Hopper needs a BF16 KV cache; FP8 KV is Blackwell-only.**
  - FlashInfer ≥ 0.6.18 is required for sparse MLA.
- **Pipeline parallelism is gated off** for this model [V: model.py comment "PP is gated off for GLM-5.3-Flash"].

**SGLang.**
- Supported. The cookbook lists H100, H200, B200/B300 and GB200/300, plus TP/EP/DP-attention, PD-disaggregation and HiSparse [S: docs.sglang.io cookbook]. The page is JS-generated, so we have no exact command.
- Open bugs [V-sub: issue titles]:
  - #37524: tracker.
  - #36830: FP8 KV unusable.
  - #37712: OOM in long-context prefill.
  - #36829: MTP mismatch with mHC hidden states.
  - #36906: PP crash.
- SGLang has no steering hooks and no arbitrary-layer hidden-state API (only final/"last" hidden states) [V-sub]. **vLLM is the better base** [I].

**Memory and GPU count** [I, from config numbers]:
- FP8 weights are 328 GB, so they don't fit on 4×H100 (320 GB), are tight on 4×H200 (564 GB), and are comfortable on 8×H100 (640 GB) or 8×H200 (1128 GB).
- With TP8 on 8×H100 that is ~41 GB/GPU of weights, leaving ~25–30 GB/GPU for KV, activations and graphs at 0.9 utilisation.
- KV is small for this model. Only the 11 MLA layers keep per-token cache: 512-dim latent × 11 layers × 2 bytes ≈ 11.3 KB/token in BF16, which MLA replicates on every TP rank, plus a small indexer cache.
- The 34 KDA layers keep a fixed state of 34 × 64 heads × 128 × 128 × 4 bytes ≈ 142 MB per sequence (fp32 assumed), sharded across TP ranks (~18 MB/rank at TP8).
- So at TP8 on H100, one rank's ~25 GB holds ~2M tokens. That covers 32 concurrent episodes at X3b's p90 context (147k) with room left over.
- **Minimum configurations:**
  - FP8: 8×H100 or 4–8×H200, TP-only with DP=1 (needed for steering, §3).
  - 4-bit: 1×B300 (288 GB) holds the NVFP4 (204 GB) or W4A16 (191 GB) checkpoints with ~50 GB for KV.
  - NVFP4 needs Blackwell [S: recipe]. On Hopper the open issues are about NVFP4 (#57748, #59306), not the FP8 repo [V-sub].

**Known vLLM issues that matter to us** [V for #59024 body; V-sub for the others]:
- #56868: "long-decode degeneration after accumulated reasoning decode". Seen on B300 with W4A16, with prefix cache on or off. This is our exact usage pattern: long multi-turn reasoning with `clear_thinking=false`.
- #58454: kpool corruption with speculative decoding k ≥ 2, so run MTP off or at k = 1.
- #58636/#58979: the sparse indexer's key norm is autotuned per TP rank, so outputs can differ between launches.
- #59024: hidden-state extraction does not work for glm5next on main. It needs PRs #56983, #57169 and #59037 (in review or WIP). The issue also says the right capture point is "the mHC-complete layer output (`hc_post` + `hc_contract`, not `hidden + residual`)".
- Prefix caching for the KDA (mamba-style) layers on this model: **not verified anywhere.** The only evidence is that #56868 ran with it on and off. Measure it.

## 3. Steering and probing paths

**Where to steer under mHC** [V: HF modeling code; algebra I].
- In HF, after layer *i* the state is `H_i = post ⊙ f(x) + combᵀ · R`. The final Sinkhorn step normalises over dim −2, so for each output stream the column of `comb` it draws from sums to exactly 1 (up to eps 1e-6).
- So adding the same vector `v` to all 4 streams of the residual passes through every later `comb` mix exactly: `combᵀ(R + 1vᵀ) = combᵀR + 1vᵀ`.
- The final head is a plain mean over streams, so the vector also survives readout.
- The natural analogue of ordinary residual steering is therefore **"add v to all 4 streams"**, with `v` measured as a difference of means on the **stream-mean** activation (what `hc_head` reads). Per-stream (4×4096) vectors are a possible variant.
- Steering does change the next layers' pre, post and comb weights, which are computed from the streams. That nonlinearity is ordinary for steering [I].

**vLLM's layer layout differs from HF's** [V: vllm v0.31.0 `glm5next/common/model.py` L451–556].
- `Glm5NextDecoderLayer.forward` returns `(x, residual[T,4,H], post, comb)`. It **defers** `hc_post` to a fused kernel in the next layer (`hc_fused_post_pre`), so the full state is never materialised at a layer boundary. Only the last layer materialises it and contracts it.
- Consequence [I, from the algebra above]: adding `v` to every stream of the returned `residual` equals adding `v` to the HF-materialised output of layer *i*. That is exactly where an HF forward hook on `layers[i]` would act. So one patch point works and can be checked against HF.
- Recipes that assume a 2-tuple `(hidden, residual)` will silently do the wrong thing on this model, including Betley's `_steer`, vllm-lens and nnsight's documented layer output [V-sub].
- **TP:** with tensor parallel only (DP=1), the mHC stack is replicated on every rank, because attention and MoE outputs are all-reduced. So adding the same vector on each rank is correct.
  - With DP-attention + EP + TP > 1 (`use_sequence_parallel_moe`), the stack is **token-sharded**: `sp_shard` happens before the layers and `sp_all_gather` after [V: model.py L704–725; vllm `config/parallel.py`].
  - Per-request steering rows would then have to follow the same chunking, and probe reads would see only one rank's rows. **Run TP-only, DP=1** [I].

Options:

| Path | Status for GLM-5.3-Flash | Verdict |
|---|---|---|
| (i-a) Plain PyTorch forward hooks in vLLM, via `collective_rpc`/`apply_model`/worker extension | Possible, but hooks don't fire on CUDA-graph replay, so they need `--enforce-eager` [V-sub: nnsight docs, hotwire README]. Eager decode is several times slower at small batch [I] | Fallback only |
| (i-b) **Betley et al.'s `__class__`-swap patch** (`johny-b/public-steering-vectors`, `pod/src/vllm_steering/patch.py`) | Runs vLLM V1 **with CUDA graphs**. It swaps the target decoder block's class so that `forward` runs, then `_steer`. Per-token `(row, alpha)` buffers are filled in `GPUModelRunner._prepare_inputs`, and vectors and strengths are set per request through `vllm_xargs`. An `inspect_ai` provider `SteeredAPI(OpenAICompatibleAPI)` hits the server, and they ran inspect agents (SWE-Bench Pro via inspect-harbor) against it. Pinned vLLM 0.27.1, 1×H200, Qwen3.6-27B. **No TP and no MoE testing; it assumes a 2-tuple layer output** [V-sub] | **Best base.** Port it to the 4-tuple (modify `residual` in place for all 4 streams), add TP (identical buffers per rank), and pin vLLM 0.31.x. That is ~100–200 lines [I] |
| (i-c) Upstream vLLM steering | None merged. Control-vector PRs #5807, #7906 and #12870 were closed; RFC #36998 (observation plugin) is open; #59542 (per-request last hidden state) is open [V-sub/S] | Not usable |
| (i-d) Forks and plugins: vllm-lens (UK AISI), vllm-prism, hotwire, EasySteer, RhizoNymph fork | vllm-lens: TP and PP, Qwen-validated, forces eager, 2-tuple layer. vllm-prism: TP8 on GLM-5 (DSA, not glm5next), old vLLM. hotwire: TP untested. EasySteer: vLLM 0.29, claims TP. RhizoNymph: the **only mHC steering design** (DeepSeek-V4 hook names, (hc_mult, hidden) vectors), with no glm5next wiring [V-sub] | Reference designs only. None supports glm5next mHC out of the box |
| (ii) nnsight + vLLM | TP supported and MoE OK; no PP; eager unless `taps=`; documented against vLLM 0.27.1; layer output assumed `(hidden, residual)`. Its own serving API is **not** an OpenAI-compatible chat endpoint [V-sub] | Poor fit for 25-turn Inspect agents |
| (iii) SGLang | No hooks; `return_hidden_states` gives only the final or last layer [V-sub] | No |
| (iv) HF transformers + `device_map` | Works on paper. The expert loop is a Python loop over hit experts per layer [V: `Glm5NextTextExperts.forward`]; the DSA path builds a dense `[B, q, kv]` mask (SDPA/eager only, no flash) [V: modeling L1240–1276]; there is no MTP [V-sub: transformers docs]; and layers run sequentially across 8 GPUs. Estimate: a few tok/s per sequence at batch 1, so one 52k-token episode would take hours [I] | **Too slow for agent episodes. Fine for the probe's forward passes** (short header-contrast prompts, minutes), and the reference implementation for checking the vLLM patch |

**Probing (forward passes only).**
- Simplest: run the header-contrast prompts through the same vLLM server, using a read-only branch of the patch that copies the stream-mean at chosen layers and positions to a pinned buffer. That is TP rank 0 only, valid because the stack is replicated.
- Alternative: run the same prompts in HF on the same node before starting vLLM, using `output_hidden_states`. That returns the `[B,S,4,D]` decoder outputs [V: `_can_record_outputs`].
- Upstream extraction (`extract_hidden_states`) is blocked for glm5next until #59024's PRs land [V].

**Our own code.** `scripts/e1b_steer.py` and `../mats12_neel_working/scripts/persona_steer_check.py` both use an HF `register_forward_hook` that adds `alpha*v` to a layer's output at every position [V: files; `docs/working-repo-code-map.md` §2.8]. The math helpers (`em_filter/persona.py` `mean_diff_vector`, `choose_alpha`) are reusable as-is [V: code map]. None of it covers vLLM, TP or mHC.

## 4. RunPod (free GraphQL, 2026-10-06 22:11 UTC)

- Balance **$211.01**, `currentSpendPerHr` 0, no pods [V]. `logs/runpod_reservations.jsonl` shows E3 reservations from earlier today [V: file]. RunPod does not auto-top-up (CLAUDE.md).
- Secure-cloud lowest prices and stock [V: `gpuTypes.lowestPrice(gpuCount, secureCloud: true)`]:

| GPU | 1× | 2× | 4× | 8× |
|---|---|---|---|---|
| H100 SXM 80GB | $3.49 Medium | $6.98 Medium | **$13.96 Low** | none |
| H100 NVL 94GB | $3.19 Low | $6.38 Low | none | none |
| H100 PCIe 80GB | $2.89 Low | none | none | none |
| H200 SXM 141GB | $4.59 Low | $9.18 Low | none | none |
| H200 NVL | none | none | none | none |
| B200 180GB | $6.79 Low | none | none | none |
| B300 288GB | $7.89 Low | none | none | none |

- Community cloud returned no price at 4× or 8× for any of these. List prices are H100 SXM $2.69, H100 NVL $2.59, H200 $3.59 and B200 $5.98 [V].
- "none" means `lowestPrice` returned null at that count, which I read as no current stock [I].
- Stock changes by the hour, so poll before booking. The per-type `maxGpuCountSecureCloud` is 8 for all of them, so 8× nodes exist when free [V].

## 5. Throughput and cost assumptions

**Measured workload** [V: X3b GLM main episodes, `.claude/worktrees/x3b-run/results/X3b/glm/episodes/main/*/*/*.json`, n = 624, `model_calls`]:
- Per episode: 14 turns on average (median 13, p90 25), 17 model calls.
- **Output 51.8k tokens per episode** (median 27k, p90 139k), of which reasoning is 35.5k. 10% of calls hit the 16k cap (1,098 of 10,732).
- **Cumulative input 670k tokens per episode, 85% cached.** Uncached is ~98k per episode.
- **Largest single-call context:** mean 56k, p90 147k, max 224k.
- The input grows by the full previous output, reasoning included (e.g. +16,019 after a 16,000-token turn). So the API served it with **past reasoning kept in context**, which matches `clear_thinking=false` [V: per-call deltas; I: interpretation].
- Billed $0.045 per episode (X3b reports $0.057 including failed retries) [V: X3b report].

**Main (360 episodes)** [I]:
- 18.7M output tokens, plus 35M uncached prefill if prefix caching works for the hybrid model, or 241M if it doesn't.
- Steering can change episode length: Betley et al. saw more agent turns under positive steering [V: `papers/neel_brainstorm/Steering towards automated grading degrades alignment (full text).md` L98]. Allow +30%.

**Throughput scenarios** [I; all of this needs measuring]:
- **Favourable.** The Betley-style patch keeps CUDA graphs; ~80 tok/s per sequence at ~24 concurrent sequences, so ~1.9k tok/s aggregate on 8×H100/H200 FP8 TP8 with MTP off. Prefix caching works.
  - Decode ≈ 2.7 h, plus prefill and the tail, plus ~1 h of start-ups: **~4 h**.
- **Unfavourable.** Eager mode (~30 tok/s per sequence), 16 concurrent, no prefix caching.
  - Decode ≈ 11 h, plus ~2–3 h extra prefill: **~14 h**.
- **The concurrency ceiling is the laptop, not the GPU.**
  - Inspect's Docker sandboxes run locally; RunPod pods have no Docker daemon [I: per RunPod container model; not re-checked].
  - X3b ran 12–20 in flight [V: `src/x3b/contracts.py` `MAX_IN_FLIGHT`], and 24–32 is untested.
  - The laptop must stay awake with the lid open (CLAUDE.md).
- **1×B300 NVFP4 fallback:** a single GPU with no TP communication. Guess ~0.6–1.5k tok/s aggregate, so ~4.5–12 h [I].

**Gate hours, 3.5–7 h** [I]:
- Weight download: 328 GB, 10–60 min depending on pod network. Pre-staging on a network volume from a cheap pod would avoid idle 8-GPU time.
- vLLM start-up: JIT mHC/tilelang kernels plus graph capture, 10–30 min.
- Patch bring-up and debugging on the real model: 1–3 h. This is the dominant uncertainty.
- Header-contrast forward passes: <15 min.
- 30 reproduction episodes: ~1–1.5 h, set by the long-tail episode at p90 139k output tokens.
- Steering sanity check: 0.5–1 h.

**Only a measurement can settle:**
1. Per-sequence decode speed with the patch, CUDA graphs versus eager.
2. Whether prefix caching works for KDA layers on this model.
3. How many concurrent sandboxes the laptop sustains.
4. Episode length under steering.
5. Whether our serving matches Relace's behaviour (§6, risk 3).

## 6. Risks and blockers, ranked

1. **Capacity and money (blocker today).**
   - No 8-GPU node is in stock, and 4×H100 can't hold FP8. The balance ($211) is less than the gate-high plus main-low.
   - Fixes: poll stock, top up RunPod, and get Jacob's go-ahead (>$100). Or accept the 1×B300 NVFP4 fallback, which is in stock and costs $7.89/h, but is a different numeric format from whatever Relace serves, and #56868 is a long-reasoning degeneration report on B300 with a 4-bit quant.
2. **The steering patch for mHC + deferred `hc_post` + TP is new code with no existing tool to lean on.**
   - The failure mode is *silent*: steering the wrong point, or steering on only some tokens under SP.
   - Mitigations:
     - Check against an HF reference on a tiny random config (Stage 0).
     - α=0 must reproduce unsteered logprobs.
     - TP1 versus TP2 must give the same steered logits.
     - Run TP-only with DP=1.
     - Run MTP off, or k=1. The draft head would be unsteered; acceptance drops but the output distribution is unchanged [I].
3. **Fidelity to the API's cheating behaviour.**
   - X3b's GLM stage passed `generate={}` [V: `src/x3b/contracts.py` L95]. So reasoning effort and sampling were Relace's defaults, which are unknown. The template default is `Max`, and the generation_config default is T=1.0, top_p=0.95.
   - Relace's quantization is unknown.
   - Our harness must send `reasoning_content` back each turn, as `clear_thinking=false` expects. Inspect's OpenAI-compatible provider plus vLLM's `glm47` reasoning parser may not round-trip it [I; must test]. The X3b token deltas show the API kept it.
   - The reproduction check has little power. The neutral header gave 31/86 = 36% cheating [V: X3b report]. 30 episodes give a 95% CI of about ±17 pp, so they only catch gross mismatches.
   - Also compare reasoning-token distributions, tool-call validity and turn counts against X3b's.
4. **Throughput spread (cost ×3).** Eager fallback, prefix caching unproven on hybrid KDA, MTP off, and the laptop concurrency ceiling (§5).
5. **Version churn.**
   - v0.31.0 shipped 2026-10-05, and model support only arrived in 0.29.
   - There are many open GLM-5.3 issues. H100 is not named in the vLLM recipe; I expect it works as Hopper SM90 like H200, but that is untested [I].
   - BF16 KV is required on Hopper, and FlashInfer must be ≥ 0.6.18.
   - Betley's patch targets 0.27.1 internals (`_prepare_inputs`, `_dummy_run`), which may have moved by 0.31.
6. **Launch-to-launch nondeterminism** (#58636): run every arm under one server launch, or interleave arms within each launch.
7. **Operations.** Docker sandboxes stay on the laptop, behind an ssh tunnel to the pod. A dropped tunnel or a laptop sleep kills episodes. Use the killswitch and watchdog per CLAUDE.md.

## 7. Cheapest gate design (resolves risks 2, 3 and 4 first, with stop points)

**Stage 0a, laptop, $0.**
- Build a random-weight `glm5_next` text model in HF with the real ratios shrunk: 4–8 layers, hidden 256, 8 experts, `hc_mult=4`, one DSA layer.
- Implement the add-v-to-all-4-streams hook. Show numerically that adding to the deferred `residual` (written vLLM-style) equals HF's output hook, and that stream-mean readout keeps v.
- Stop if the algebra fails.

**Stage 0b, 1–2×H100, $3–10.**
- vLLM 0.31.x with `--load-format dummy` and the shrunk config through `--hf-overrides`.
- Port Betley's class-swap patch to the 4-tuple, then test:
  - α=0 is an identity on logprobs.
  - TP1 equals TP2 for steered logits.
  - CUDA graphs on versus `--enforce-eager` give the same outputs.
  - Mixed per-request vectors in one batch.
  - The read-only probe branch.
  - An Inspect `openai-api/` provider hitting the server.
- Weights are random, so this tests plumbing, not behaviour.
- Stop if the patch can't run with CUDA graphs. Then the main study must be costed at the eager (high) end.

**Stage 1, the real model on 8×H100/H200 FP8, TP8, MTP off. Each step is a stop point.**
1. **Serve and smoke test** (≤1 h): 5 neutral-header X3 episodes, unsteered.
   - Check that the tool-call parser (`glm47`), reasoning round-trip, turn counts and reasoning-token sizes look like X3b's.
   - Stop if tool calling or reasoning carry-over is broken.
2. **Header-contrast forward passes** (<15 min): build the probe direction at 2–3 layers. Check split-half stability (cos ≥ 0.8, per `persona.py`).
3. **Reproduction** (~1–1.5 h): 30 neutral-header episodes with `reasoning_effort` absent (template Max), T=1.0, top_p=0.95.
   - Pass: the cheat rate falls within X3b's 95% interval (26.7–46.6%), and the reasoning-length distribution overlaps X3b's.
   - Optionally add 10 capability-header and 10 safety-header episodes to see the anchor direction.
4. **Steering sanity** (0.5–1 h):
   - α sweep on single-turn prompts: coherence, plus projection onto the probe.
   - Then 3 strengths × 6 short episodes. Measure tok/s per sequence and aggregate.
   - Measure prefix-cache hit rate and the laptop's sandbox ceiling.
5. Write a checkpoint report with measured throughput, then re-cost the main study from the measured numbers before asking for the main go-ahead.

**Fallback.** If no 8-GPU node frees up within the planning window, run Stage 1 on 1×B300 NVFP4 ($32–63). Treat the reproduction check (step 3) as the fidelity gate, comparing against the API's rates.

## Sources

- HF: `zai-org/GLM-5.3-Flash` at `eb9eb20` (config.json, generation_config.json, chat_template.jinja, README.md, API metadata); `zai-org/GLM-5.3-Flash-BF16` at `a5b45eb`; `nvidia/GLM-5.3-Flash-NVFP4` at `da920bb`, `RedHatAI/GLM-5.3-Flash-NVFP4`, `wtdcode/GLM-5.3-Flash-AWQ-W4A16` (sizes).
- transformers main: `src/transformers/models/glm5_next/modeling_glm5_next.py`; `docs/source/en/model_doc/glm5_next.md` (V-sub).
- vLLM v0.31.0: `vllm/models/glm5next/common/model.py`; releases API; issue #59024 (read in full); issues #56868, #58454, #58636, #57748, #59306, #55773 (V-sub); recipe https://recipes.vllm.ai/zai-org/GLM-5.3-Flash (S); `vllm/config/parallel.py` (V-sub).
- SGLang: cookbook https://docs.sglang.io/cookbook/autoregressive/GLM/GLM-5.3-Flash (S); issues #37524, #36830, #37712, #36829, #36906 (V-sub titles).
- Steering code: https://github.com/johny-b/public-steering-vectors (`pod/src/vllm_steering/patch.py`, `inspect_providers/steered_provider`; V-sub); vllm-lens, vllm-prism, hotwire-vllm, EasySteer, RhizoNymph/vllm, nnsight `docs/models/vllm*.md` (V-sub); `papers/neel_brainstorm/Steering towards automated grading degrades alignment (full text).md` (V).
- This repo: `src/x3/task.py`, `src/x3/contracts.py`, `.claude/worktrees/x3b-run/src/x3b/{runner,contracts}.py`, `.claude/worktrees/x3b-run/docs/experiments/X3b/report.md`, the X3b GLM episode JSONs, `scripts/e1b_steer.py`, `../mats12_neel_working/scripts/persona_steer_check.py`, `docs/working-repo-code-map.md` (all V).
- RunPod GraphQL `myself`, `gpuTypes`, `lowestPrice` (V, 2026-10-06 22:11 UTC).
