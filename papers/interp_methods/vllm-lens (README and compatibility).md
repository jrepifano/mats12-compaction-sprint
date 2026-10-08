# vllm-lens: README and vLLM compatibility doc

**Authors:** UK AISI (UKGovernmentBEIS)
**Source URLs:** https://github.com/UKGovernmentBEIS/vllm-lens (README.md, docs/compatibility.md, main branch)
**Retrieved:** 2026-10-06 via raw.githubusercontent.com
**Note:** Verbatim copies of the two markdown files; repo not cloned, code not read.

---

# vLLM-lens

vLLM-Lens enables top-down interpretability (e.g., probes, steering, activation oracles, causal tracing). It offers high performance, supporting tensor parallelism & pipeline parallelism (across GPUs and nodes) out of the box. You can also apply all these techniques concurrently (in the same dynamic batch) - removing the need to switch between model instances.

Features:
- **Activation extraction** — capture residual stream hidden states from specific layers
- **Steering vectors** — add activation vectors to modify the residual stream in-flight
- **Generic hooks** — run arbitrary Python functions per-request, per-layer during inference (inspired by [Garçon](https://transformer-circuits.pub/2021/garcon/index.html))
- **Persistent hooks** — register hooks once, run many requests, collect results in bulk
- **Pre-hooks** — modify layer inputs (e.g., corrupt embeddings for causal tracing)

The module auto-registers as a [vLLM general plugin](https://docs.vllm.ai/en/latest/design/plugin_system.html) and an [Inspect](https://inspect.aisi.org.uk/) model provider on install. Interact with model internals per-call via `SamplingParams.extra_args` (vLLM) or `GenerateConfig.extra_body` (Inspect).

## Install

```bash
uv add vllm-lens
```

See the [vLLM compatibility matrix and validation guide](docs/compatibility.md)
for version test targets, recorded validation status, and reproducible checks.
The package dependency range is broader than the tested configurations.

## Quickstart

vllm-lens auto-registers as a vLLM plugin — just start a server normally:

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct
```

All vllm-lens features (activation extraction, steering, hooks) are available immediately via `SamplingParams.extra_args` (offline) or `vllm_xargs` (HTTP API). The plugin also registers custom HTTP endpoints for persistent hook management at `/v1/hooks/*`.

For the HTTP API, a Python client is provided:

```python
from vllm_lens.client import VLLMLensClient
from vllm_lens import Hook

client = VLLMLensClient("http://localhost:8000")

# Per-request hook
output = client.generate("Hello", max_tokens=10, hooks=[hook])
print(output.hook_results)

# Activation capture
output = client.generate("Hello", capture_layers=[15])
print(output.activations)

# Attention pattern capture (reconstructed offline from captured Q/K)
output = client.generate("Hello", capture_qk=[15])

# Persistent hooks
client.register_hooks([hook])
client.generate("prompt 1", max_tokens=10)
client.generate("prompt 2", max_tokens=10)
results = client.collect_hook_results()
client.clear_hooks()
```

## Disabling the plugin

vllm-lens auto-loads in **every** vLLM process (via the `vllm.general_plugins` entry point) and forces `enforce_eager=True` (disabling CUDA graphs) so its hooks can fire. To install it alongside another inference server without perturbing that server, set `VLLM_LENS_DISABLE=1` to make the plugin a complete no-op:

```bash
VLLM_LENS_DISABLE=1 vllm serve meta-llama/Llama-3.1-8B-Instruct
```

## Examples

Runnable examples live in [`examples/`](examples/) — each is standalone; run any
with `python examples/<name>.py --help`. Most (`causal_tracing`, `logit_lens`,
`deception_probe`, `emotion_tracker`) drive a **separately running** vllm-lens
server over HTTP via [`VLLMLensClient`](vllm_lens/client.py) (start one with
`vllm serve <model>`, then pass `--base-url`); `activation_oracle` instead spins
up its own in-process engine. The notebooks are self-contained.

| Example | What it demonstrates |
| --- | --- |
| [`causal_tracing.py`](examples/causal_tracing.py) | ROME-style causal tracing — pre-hook embedding corruption + post-hook clean-state patching, rendered as a (layer × token) heatmap. |
| [`logit_lens.py`](examples/logit_lens.py) | Logit lens — project each layer's hidden states through the final norm + unembedding via `ctx.get_parameter("lm_head.weight")`. |
| [`jacobian_lens.py`](examples/jacobian_lens.py) | Jacobian lens / J-space ([Anthropic global-workspace](https://transformer-circuits.pub/2026/workspace)) — like the logit lens, but transports each layer's residual through a pre-fitted average Jacobian `J_l` before unembedding, reading out what the model is "disposed to say". Readout + visualization; the lens is fit by [`jacobian_lens_fit.py`](examples/jacobian_lens_fit.py) (`--rules lrp` fits the R-lens variant, more faithful on early layers). |
| [`jacobian_lens_chat.py`](examples/jacobian_lens_chat.py) | Live J-space chat — registers a J-lens readout hook on a served model and generates an HTML chat page where every token is hoverable, showing the streaming top-k readout across the captured layers. |
| [`deception_probe.py`](examples/deception_probe.py) | Apollo-style linear deception probe — contrastive activation extraction with persistent hooks, then a pure-torch logistic-regression probe. |
| [`emotion_tracker.py`](examples/emotion_tracker.py) | Anthropic emotion-concepts replication — emotion direction vectors + per-token projection tracking, with an interactive HTML visualization. |
| [`activation_oracle.py`](examples/activation_oracle.py) | Activation Oracle steering (arXiv:2512.15674) — capture activations and steer a LoRA oracle to describe them. |
| [`extract_residual_stream.ipynb`](examples/extract_residual_stream.ipynb) | Notebook — per-request and persistent activation capture, offline and over HTTP. |
| [`inspect-demo.ipynb`](examples/inspect-demo.ipynb) | Notebook — Activation Oracle via the vllm-lens Inspect provider. |
| [`benchmark.ipynb`](examples/benchmark.ipynb) | Notebook — overhead of activation capture and steering. |

### Activation capture

Capture the residual stream at chosen layers by passing `output_residual_stream` (a list of layer indices) in `extra_args`:

```python
from vllm import LLM, SamplingParams

llm = LLM("meta-llama/Llama-3.1-8B-Instruct")
sp = SamplingParams(max_tokens=1, extra_args={"output_residual_stream": [15, 20]})
out = llm.generate(["Hello world"], sp)
acts = out[0].activations["residual_stream"]  # (n_layers, n_positions, hidden_dim)
```

Over the HTTP server, pass it in `vllm_xargs` — or use the client's `capture_layers`:

```python
from vllm_lens.client import VLLMLensClient

client = VLLMLensClient("http://localhost:8000")
out = client.generate("Hello world", capture_layers=[15, 20])
print(out.activations["residual_stream"].shape)
```

Layers are stacked in ascending order along dim 0. Capture runs on TP rank 0 only (residual streams are identical across TP ranks after all-reduce).

#### Binary transport for large captures

By default activations come back base64-encoded inside the JSON response, which adds ~33% overhead on top of the zstd compression and materializes the whole blob in one piece on both ends. For large all-layer captures, pass `activations_transport="binary"` to fetch the raw bytes out-of-band instead:

```python
out = client.generate(
    "Hello world",
    capture_layers=list(range(32)),
    activations_transport="binary",
)
print(out.activations["residual_stream"].shape)  # decoded transparently
```

The completion JSON then carries only an unguessable handle plus the dtype/shape metadata; the client fetches the raw zstd bytes from `GET /v1/activations/{handle}` as `application/octet-stream`. The server-side buffer is bounded (`VLLM_LENS_ACT_MAX_BYTES`, default 8 GiB) and per-entry TTL'd (`VLLM_LENS_ACT_TTL_S`, default 300 s). It is process-local, so behind a load balancer either pin activation fetches to the replica that produced the completion (sticky routing) or keep the default base64 transport. The same applies within one replica to vLLM's `--api-server-count N` (multiple frontend processes behind one port): a handle is only valid on the frontend that minted it, so use `--api-server-count=1` with binary transport (the plugin logs a warning otherwise).

### Attention pattern capture

vLLM's fused attention kernels never materialize attention weights, so they can't be captured directly. Instead, `output_qk` captures each layer's **post-RoPE query/key tensors** (a plain pre-hook on vLLM's `Attention` wrapper) together with the exact kernel parameters (softmax scale, sliding window, logit soft-cap, ALiBi slopes, attention sinks), and `vllm_lens.attention.attention_patterns` replays the softmax offline — giving the true attention matrix:

```python
from vllm import LLM, SamplingParams
from vllm_lens.attention import attention_patterns

llm = LLM("meta-llama/Llama-3.1-8B-Instruct")
layer = 15  # decoder-layer index to capture
sp = SamplingParams(max_tokens=8, extra_args={"output_qk": [layer]})  # or True for all layers
out = llm.generate(["Hello world"], sp)

weights = attention_patterns(out[0].activations, layer=layer)  # (num_heads, n_positions, n_positions)
```

Over HTTP, use the client's `capture_qk`:

```python
out = client.generate("Hello world", capture_qk=[layer])
weights = attention_patterns(out.activations, layer=layer)
```

The captured entries are `attn_q` (`n_layers, n_positions, num_heads, head_dim`), `attn_k` (same with KV heads), `qk_layers`, and `qk_meta` (the per-layer kernel parameters). Unlike the residual stream, Q/K are sharded by head under tensor parallelism, so **every** TP rank captures and the shards are concatenated along the head dimension at merge time; grouped-query attention, sliding windows, and Gemma-style soft-capping are handled from the shipped metadata. The reconstruction is exact up to floating-point rounding: in fp32 it matches HuggingFace's eager attention to ~1e-5 per probability (the test suite asserts ≤ 1e-4), and the capture itself is verified bit-for-bit against raw hooks on the same modules. MLA models (DeepSeek-style latent attention) are rejected with a clear error — their Q/K never exist in per-head form. Reconstructing a full pattern materializes an `(num_heads, n, n)` matrix client-side, so reconstruct per layer rather than all at once for long sequences. Capture itself copies each hooked layer's Q/K to host memory every forward step on every TP rank, so `output_qk=True` on a deep model at high TP is noticeably slower than capturing a few layers — pass an explicit layer list where you can.

### Steering vectors

Add activation vectors to the residual stream in-flight with `apply_steering_vectors`. A `SteeringVector` carries the activations plus how to apply them:

```python
import torch
from vllm import LLM, SamplingParams
from vllm_lens import SteeringVector

sv = SteeringVector(
    activations=torch.randn(1, 4096),  # (n_layers, hidden) or (n_layers, n_positions, hidden)
    layer_indices=[15],
    scale=4.0,
    norm_match=True,        # scale the added vector so its magnitude is ‖residual‖ · scale
    position_indices=None,  # None = all positions (2D) or sequential 0..n-1 (3D)
)
sp = SamplingParams(max_tokens=20, extra_args={"apply_steering_vectors": [sv]})
llm.generate(["I think the best dessert is"], sp)
```

`llm.chat(...)` accepts the same `extra_args`, and both entry points also accept the JSON-serialized wire form (`json.dumps([sv.model_dump(mode="json")])`) that the HTTP API uses — handy when sharing request-building code with an HTTP client.

Via the client (or `vllm_xargs` over HTTP):

```python
client.generate("I think the best dessert is", steering_vectors=[sv])
```

### Generic hooks

Hooks let you run arbitrary Python functions on hidden states at specific layers during inference. They can capture data (via `ctx.saved`) and/or modify hidden states (by returning a tensor).

**Interface.** A hook is `Hook(fn, layer_indices, pre=False)`:

| Field | Meaning |
| --- | --- |
| `fn(ctx, hidden_states) -> Tensor \| None` | Called once per hooked layer, per request. `hidden_states` is that request's slice, shape `(seq_len, hidden_dim)`. Return a tensor to **replace** it, or `None` to leave it unchanged. |
| `layer_indices: list[int]` | Which layers to run on. |
| `pre: bool` | If `True`, run as a *pre-hook* — before the layer forward pass, on its input — instead of after. |

The `ctx` (a `HookContext`) passed to `fn` exposes:

| Attribute | Meaning |
| --- | --- |
| `ctx.saved: dict[str, Any]` | Scratch dict that persists across layers **and** forward passes for this hook. Returned to the client as `hook_results["<hook_index>"]` (index = the hook's position in the list you passed). |
| `ctx.layer_idx: int` | The layer index currently firing. |
| `ctx.seq_len: int` | Number of tokens in this request's slice. |
| `ctx.model` | The underlying model (for architecture-specific access). |
| `ctx.get_parameter(name) -> Tensor` | Fetch a full model parameter, gathered across TP/PP ranks — e.g. `ctx.get_parameter("lm_head.weight")`. With pipeline parallelism, `prefetch_params` the name first (see below). |

`fn` runs on **every** tensor-parallel rank, so it must be deterministic across ranks (seed any randomness). Note that `ctx.saved` is merged across ranks at collection, and **list** values from different ranks are concatenated — so with TP > 1, a hook that saves plain lists sees every entry duplicated `tp_size`×. Save tensors, or guard the writes to one rank (e.g. `get_tp_group().rank_in_group == 0`, as [`jacobian_lens_chat.py`](examples/jacobian_lens_chat.py) does — but keep `ctx.get_parameter` calls on all ranks: the TP gather is a collective). For HTTP transport `fn` is serialized with cloudpickle — i.e. **arbitrary code execution** on the server, so only use with trusted clients.

```python
from vllm import LLM, SamplingParams
from vllm_lens import Hook

def ablate_neuron(ctx, h):
    ctx.saved[f"pre_L{ctx.layer_idx}"] = h[:, 42].cpu()
    h = h.clone()
    h[:, 42] = 0
    return h  # return None to skip modification

hook = Hook(fn=ablate_neuron, layer_indices=[15, 16])
sp = SamplingParams(
    temperature=0.0,
    max_tokens=10,
    extra_args={"apply_hooks": [hook]},
)
outputs = llm.generate(["Hello world"], sp)
print(outputs[0].hook_results)  # {"0": {"pre_L15": tensor, "pre_L16": tensor}}
```

Pre-hooks run before the layer forward pass (useful for corrupting inputs):

```python
def corrupt_embeddings(ctx, h):
    noise = torch.randn_like(h) * 3.0
    return h + noise

hook = Hook(fn=corrupt_embeddings, layer_indices=[0], pre=True)
```

### Persistent hooks (Garçon-style)

Register hooks once, run many requests, collect all results in one bulk transfer:

```python
llm.register_hooks([hook])

for prompt in prompts:
    llm.generate([prompt], sp)  # hooks fire, results stay server-side

results = llm.collect_hook_results()  # bulk retrieval
llm.clear_hooks()
```

Over HTTP (no dev mode required):

```
POST /v1/hooks/register          {"hooks": [...], "prefetch_params": [...]}
POST /v1/completions             (hooks fire automatically)
POST /v1/hooks/collect           → {"results": {<req_id>: ...}}
POST /v1/hooks/clear
POST /v1/hooks/clear_results
POST /v1/hooks/prefetch          {"params": ["lm_head.weight", ...]}
POST /v1/hooks/clear_prefetched
```

Multiple `register` calls append hooks. `collect` is non-destructive, so results accumulate across requests; `clear_results` (`client.clear_hook_results()`) drains them while keeping the hooks registered, and `clear` removes hooks and all accumulated results. Pre-fetched parameters persist independently.

### Accessing model parameters from hooks

Hooks can access model parameters (e.g. `lm_head.weight` for logit lens) via `ctx.get_parameter()`. This auto-gathers across TP ranks:

```python
def logit_lens(ctx, h):
    weight = ctx.get_parameter("lm_head.weight")  # full unsharded weight
    logits = h.float() @ weight.float().T
    ctx.saved["top_ids"] = logits.topk(5).indices.cpu()
    return None
```

With pipeline parallelism, parameters may live on a different PP stage. Pre-fetch them so they're available on all ranks:

```python
# Standalone — works with both per-request and persistent hooks
client.prefetch_params(["lm_head.weight"])
output = client.generate(prompt, hooks=[hook])  # hook can use lm_head.weight

# Or at registration time for persistent hooks
client.register_hooks([hook], prefetch_params=["lm_head.weight"])

# Clean up when done
client.clear_prefetched()
```

Pre-fetched parameters persist until explicitly cleared. When the parameter already exists locally (TP=1, same PP stage), no copy is made.

### Causal tracing (activation patching)

The [`causal_tracing.py`](examples/causal_tracing.py) example implements ROME-style causal tracing using pre-hooks for embedding corruption and post-hooks for clean-state restoration:

```bash
python examples/causal_tracing.py \
    --base-url http://localhost:8000 \
    --prompt "The Eiffel Tower is in the city of" \
    --subject "Eiffel Tower" \
    --answer " Paris"
```

This produces a (layers × tokens) heatmap showing which hidden states are causally important for factual recall.

### Jacobian lens / J-space

The Jacobian lens extends the logit lens: it transports each layer's residual through a pre-fitted average Jacobian `J_l = E[∂h_final/∂h_l]` before the norm + unembedding, reading out the tokens a model is *disposed toward*. The full flow is **fit → serve → read out + visualize**, and the fit and readout run in **two different environments** connected only by the fitted `lens.pt` (format: `{J, source_layers, d_model}`, identical in both):

1. **Fit** `J_l` with [`jacobian_lens_fit.py`](examples/jacobian_lens_fit.py) — the single source of truth for fitting. It needs the backward pass and runs in the **prime-rl fit env** (torch 2.11/cu128 + torchtitan; not the vllm-lens env), on prime-rl's FSDP2 + expert-parallel stack. This scales from small dense models on one GPU up to large MoE across many GPUs / nodes — GLM-4.5-Air (110B) is validated (cosine 1.0 vs a single-GPU reference), and the same path serves GLM-5.2. Build the env once with [`jacobian_lens_fit_env.sh`](examples/jacobian_lens_fit_env.sh).
2. **Serve** the model under vllm-lens (the readout env).
3. **Read out** with [`jacobian_lens.py`](examples/jacobian_lens.py) — forward-only, applied in a hook on the vLLM worker, correct under TP/PP/EP.
4. **Chat interactively** with [`jacobian_lens_chat.py`](examples/jacobian_lens_chat.py) — registers the readout hook on the server and generates an HTML chat page; hover any token to see the top-k J-space readout across the captured layers, streaming in as the reply generates.

```bash
# 1. fit J_l (once; cached to lens.pt). In the prime-rl fit env — see
#    examples/jacobian_lens_fit_env.sh to build it. Single node:
cd /path/to/prime-rl && unset VIRTUAL_ENV
uv run --no-sync torchrun --nproc-per-node=8 \
    /path/to/vllm-lens/examples/jacobian_lens_fit.py \
    --model zai-org/GLM-4.5-Air --layers 25,33,40 --ep 8 --out lens.pt
#    Multi-node (torchrun; run on each node, differing only in --node-rank):
#    torchrun --nnodes=2 --node-rank=0 --nproc-per-node=8 \
#        --rdzv-backend=c10d --rdzv-endpoint=$HEAD_IP:29500 \
#        examples/jacobian_lens_fit.py --model ... --ep 16 --out lens.pt
#    (SLURM: set --nnodes/--node-rank/--rdzv-endpoint from srun env — see the
#    jacobian_lens_fit.py module docstring.)

# 2. serve it under vllm-lens (V1 runner so hooks work)
VLLM_USE_V2_MODEL_RUNNER=0 vllm serve zai-org/GLM-4.5-Air

# 3. read the lens out live against the server (vllm-lens env)
python examples/jacobian_lens.py run --lens lens.pt \
    --prompt "The Eiffel Tower is located in the city of" \
    --layers 25,33,40

# 4. or build an interactive HTML: chat + live J-space readout at a chosen layer
python examples/jacobian_lens_chat.py --lens lens.pt \
    --base-url http://localhost:8000 --out jacobian_lens_chat
```

`run` prints the top-1 J-lens token at each (layer, position); with `--grid-out FILE` it also writes a static top-k grid, one subplot per layer (needs matplotlib). `jacobian_lens_chat.py` registers the readout hook server-side (the fitted lens uploads once) and writes an HTML chat page: replies stream in with every token hoverable — the tooltip shows the top-k readout across all captured layers (click to pin) — and messages are editable, with top-k, response length, and thinking adjustable in the page. Pass `--html-base-url` if the browser reaches the server on a different address than the generator (e.g. a forwarded port).

`--layers` picks which layers to read out (the lens is shipped to the worker, so keep it modest on large models — and fit only the layers you'll read out); `--k` sets how many tokens per position; `--baseline` drops the `J_l` transport for a logit-lens comparison; `--norm-weight` overrides the auto-detected final-norm weight name. `run --lens` also loads a pre-fitted Hub lens (e.g. [Neuronpedia](https://huggingface.co/neuronpedia/jacobian-lens)).

#### R-lens

Passing `--rules lrp` to the fitter fits an **R-lens** ([post](https://www.greaterwrong.com/posts/nv8oedrnLXKRzNEL9/r-lens-making-j-lens-more-faithful-on-early-layers)) instead: the same estimator, but with LRP (layer-wise relevance propagation) rules installed in the backward pass — the LN-rule on residual-stream RMSNorms (normalization denominator treated as constant), and identity + half rules on the gated MLP (activation backward made per-element linear; product gradient split 50/50 across the gate branches). The rules ([`r_lens_rules.py`](examples/r_lens_rules.py)) are pure stop-gradients, so forward values are exactly unchanged (the fit is modestly slower per prompt, since patched modules bypass fused kernels) — and early-layer readouts surface concepts much earlier with far fewer junk tokens. The output format is identical (the transport matrices still live under the `J` key), so steps 2–4 above work unchanged; `provenance["rules"]` in the `.pt` records which backward was used. Dense models only for now — `--rules lrp` on a MoE model fails fast at startup.

### Inspect AI provider

An [Inspect AI](https://inspect.ai-safety-institute.org.uk/) model provider is auto-registered as `vllm-lens`, when you install this package. This model provider extends the built-in vLLM provider to serialize `torch.Tensor` steering vectors for HTTP transport and decode base64-encoded activations from responses into `ModelOutput.metadata["activations"]`. It also supports LoRA adaptors.

Usage is the same as the [default vLLM provider](https://inspect.aisi.org.uk/providers.html#vllm) but with the `vllm-lens` prefix (e.g. `vllm-lens/meta-llama/Llama-3.1-1B`).

#### Extracting activations

```python
capture_config = GenerateConfig(
    temperature=0.0,
    max_tokens=1,
    extra_body={
        "extra_args": {"output_residual_stream": extraction_activation_layers},
        "chat_template_kwargs": {"enable_thinking": False},
    },
)
output = await model.generate(state.messages, config=capture_config)
residual_stream = output.metadata["activations"]["residual_stream"]
```

Attention Q/K capture works the same way — pass `"output_qk": [layer, ...]` in `extra_args` and reconstruct with `vllm_lens.attention.attention_patterns(output.metadata["activations"], layer=layer)`.

#### Steering with an Activation Oracle

```python
from vllm_lens import SteeringVector

messages = [ChatMessageUser(content=oracle_content)]
oracle_config = GenerateConfig(
    temperature=0.0,
    max_tokens=50,
    extra_body={
        "extra_args": {
            "apply_steering_vectors": [
                SteeringVector(
                    activations=act_vector,
                    layer_indices=[injection_layer],
                    scale=steering_coefficient,
                    norm_match=True,
                    position_indices=[special_pos],
                )
            ],
        },
        "lora_request": {
            "lora_name": "oracle",
            "lora_int_id": 1,
            "lora_path": lora_path,
        },
        "chat_template_kwargs": {"enable_thinking": False},
    },
)
response = await model.generate(messages, config=oracle_config)
```

## Theory

vllm-lens registers as a [vLLM plugin](https://docs.vllm.ai/en/stable/design/plugin_system) and injects itself into vLLM's processing pipeline at broadly 3 stages:

1. **Intercepting generate calls.** To utilise the plugin, you can pass [extra args](https://docs.vllm.ai/en/stable/api/vllm/sampling_params/#vllm.sampling_params.SamplingParams.extra_args) such as `output_residual_stream`, `output_qk`, `apply_steering_vectors`, or `apply_hooks` in the sampling parameters. The plugin extracts these, initialises relevant [PyTorch hooks](https://docs.pytorch.org/docs/stable/generated/torch.Tensor.register_hook.html) if they're not already setup (by adding a [worker extension](https://docs.vllm.ai/en/stable/cli/run-batch/?h=worker+extension#-worker-extension-cls)) and sends steering vectors and hook definitions directly to workers (vLLM typically has one worker per GPU).
2. **Per-sample hook operations**. vLLM dynamically batches tokens from multiple concurrent requests into a single forward pass, so a core challenge is "book-keeping" - working out which operations (e.g., activation extraction) should be applied to which parts of the request. To do this we read the `forward_context` metadata, utilising the `query_start_loc` (a tensor of token boundaries per request) and `req_ids` (mapping batch index to request ID). We then, for example, apply hooks to just the slices that correspond to the request. Any extracted activations are moved to CPU ram and compressed (lossless), ready to be requested by the vLLM scheduler process. Steering runs on all tensor-parallel ranks (since it modifies the forward pass), but capture only runs on TP rank 0 (residual streams are identical across TP replicas after all-reduce).
3. **Response collation.** The plugin intercepts the response before it is sent to the client, at which point it queries the relevant vLLM processes for any requested activations. It trims surplus activations, since vLLM can run an extra forward pass under the hood (the scheduler often gets ahead of the number of tokens it needs to generate, before stopping). Activations are then returned to the client.

### fp32 models and TF32

vLLM serves fp32 models with its Triton attention backend, and Triton's `tl.dot` defaults to **TF32** for fp32 inputs — so an fp32 model is not fp32 end-to-end (vLLM already pins the linears to true fp32 via `VLLM_FLOAT32_MATMUL_PRECISION=highest`, but not the attention kernel). Measured on Qwen2.5-0.5B this put ~3e-3 of error into attention probabilities and up to 0.17 into residual-stream values, versus ~4e-6 with IEEE fp32. Since vllm-lens exists to deliver faithful activations, it defaults `TRITON_F32_DEFAULT=ieee` whenever the model dtype is fp32 (logged at startup); set `TRITON_F32_DEFAULT` yourself to override. bf16/fp16 models are unaffected.

### Layer discovery

vllm-lens finds the decoder layers to hook via vLLM's attention-layer registry (`static_forward_context`), which covers standard attention, MLA (DeepSeek/GLM-5), and Mamba/linear-attention hybrids without per-architecture code. The discovered layers are checked against the config's `num_hidden_layers`; on any mismatch the first request that needs hooks fails with a `LayerDiscoveryError` rather than silently hooking the wrong modules. If that happens on a model with an unusual layout, point vllm-lens at the layers directly with a `get_submodule` path template containing `{i}` for the layer index:

```bash
VLLM_LENS_LAYER_PATH="model.layers.{i}" vllm serve my-org/unusual-model
```

The template is used exclusively when set (no auto-discovery), and every index in `range(num_hidden_layers)` must resolve.

## Running tests

CPU regression tests run on PRs without a GPU or vLLM installation. The
[compatibility guide](docs/compatibility.md) describes their setup and the
local single-GPU and TP/PP validation commands. GPU tests run locally only.
On a Slurm cluster, submit both suites from the repository root:

```bash
sbatch scripts/compatibility.slurm
```

This requests `--gres=gpu:2` using the default account and partition. For one GPU,
use `sbatch --gres=gpu:1 scripts/compatibility.slurm --suite smoke`.
The default vLLM target is 0.30.0; see the guide for prerequisites and job logs.

Integration tests in `tests/` run against a live vLLM server. You can either let the fixture start one automatically, or point at an existing server:

```bash
# Option 1: Let pytest start a server (requires GPU, takes a few minutes to boot)
pytest tests/ -v

# Option 2: Start a server yourself, then run tests against it
vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000
VLLM_TEST_PORT=8000 pytest tests/ -v
```

Environment variables:
- `VLLM_TEST_PORT` — server port (default: `8100`)
- `VLLM_TEST_MODEL` — model to serve (default: `meta-llama/Llama-3.1-8B-Instruct`)
- `VLLM_TEST_TP_SIZE` — tensor parallel size (default: `1`)
- `VLLM_TEST_PP_SIZE` — pipeline parallel size (default: `1`)
- `VLLM_TEST_STARTUP_TIMEOUT` — seconds to wait for server startup (default: `900`)

Unit tests in `vllm_lens/tests/` use a small model and manage their own in-process vLLM engine, so they don't need a separately-running server — but they still require a GPU:

```bash
pytest vllm_lens/tests/ -v
```

An opt-in multi-architecture parity sweep validates attention Q/K capture + reconstruction against HuggingFace eager attention across GQA/MHA, soft-capping, sliding windows, custom scales, attention sinks, hybrid, and MLA models. It downloads several models (some gated — needs an HF token) and wants a large GPU:

```bash
VLLM_LENS_QK_PARITY=1 pytest vllm_lens/tests/test_qk_parity_models.py -v
```

## Credits

Developed by Alan Cooney, with credit going to Sid Black for the original vLLM worker extension idea.


---

# docs/compatibility.md

# vLLM compatibility

vllm-lens integrates with private vLLM runner and serving APIs. A successful
installation or server startup is not sufficient evidence of compatibility:
capture and interventions must also produce the expected values.

We validate one vLLM release at a time, starting with 0.30.0. After a release
passes local GPU validation, pin the package to that exact version. Test newer
stable releases as they become available and advance the pin only when they
pass; if a candidate fails, retain the last validated pin and investigate.

## Matrix and evidence

| vLLM | Python | PyTorch | Runner | Status |
| --- | --- | --- | --- | --- |
| 0.30.0 | 3.12.3 | 2.13.0 / CUDA 13.0 | V1, eager | Validated: 45 smoke + 11 parallel passed (Slurm 5.0) |

GPU compatibility tests run locally. The validated row covers clean revision
`5cbffc1712f6edb6e9d6218f07465849d3c7a69b` on two H100 80GB GPUs, driver
595.58.03. Its complete reports are linked below. CPU CI results alone do not
validate inference.

The [local compatibility script](../scripts/check_compatibility.py) saves
environment metadata, per-suite logs and JUnit reports in `--output-dir`.
Attach the reports to the PR or release and include the environment summary in
the release notes. Keep previous validation results when testing a new stable
vLLM release. Do not label an untested replacement supported.

The package pins the validated release with `vllm==0.30.0`. The development
lock uses PyTorch 2.13.0 with CUDA 13.0 sources and Transformers 5.18.0.
Both the default development setup and optional benchmarking group install
from public dependencies; benchmark image paths are supplied explicitly.
Newer stable releases must pass both suites before advancing this pin.

V2 model-runner mode is unsupported and rejected by the plugin. The plugin
defaults to V1 and forces eager execution; native V2/CUDA-graph support is outside
this matrix work. Model-specific features requiring V2 cannot use the fallback.
Passing the small Qwen suite does not establish support for every model,
quantization mode, attention backend, hardware platform or distributed layout.

## CPU checks on every PR

Serialization, binary transport, attention reconstruction, engine configuration
and registration diagnostics live in
`tests/unit/`, outside the GPU fixtures in `vllm_lens/tests/conftest.py`.
The R-lens rule tests also run without vLLM, model downloads or a CUDA runtime:

```bash
uv venv --python 3.12 .venv-cpu
uv pip install --python .venv-cpu/bin/python torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv-cpu/bin/python -r requirements/cpu-tests.txt
uv pip install --python .venv-cpu/bin/python --no-deps -e .
.venv-cpu/bin/python -m pytest tests/unit examples/tests/test_r_lens_rules.py
```

Layer-discovery unit tests additionally import real vLLM classes. They run in the
compatibility environment alongside the GPU discovery tests, not in the minimal
CPU job. The existing lint, format and type checks still run separately.

## Run GPU tests through Slurm

Check out the PR branch (`ci/vllm-compatibility-matrix`) on the cluster. From the
repository root, submit:

```bash
sbatch scripts/compatibility.slurm
```

The job uses the cluster's default account and partition and requests **one node,
`--gres=gpu:2`, eight CPUs, 32 GiB host memory and two hours**. Slurm queues the job
until those resources are available. A single `srun` task launches the tests
inside the allocation; vLLM spawns its TP/PP workers within that step. The script
does not request an exclusive node or modify other jobs.

The compute environment needs Linux, working NVIDIA drivers, Python 3 and
[uv](https://docs.astral.sh/uv/getting-started/installation/) on `PATH`. The job
provisions Python 3.12 and vLLM **0.30.0** in a fresh environment, then runs smoke
and parallel suites sequentially. It stops at the first failure. Ensure any
site-required modules are available in the batch environment.

The test dependencies include `accelerate>=1.1.0` for the Hugging Face reference
fixtures' CUDA device mapping and `ninja` for FlashInfer's runtime kernel builds.
`transformers==5.18.0` fixes the reference implementation across vLLM candidates;
PyTorch follows each candidate's requirements. Exact versions are recorded in
each run's evidence. An incompatible Transformers constraint is a setup failure,
not permission to silently change the reference.
The launcher and direct suite runner put the test environment's executables on
`PATH`; no shell activation is needed. The suite runner checks `ninja` before
starting a model. A missing build tool is a setup failure, so fix the environment
and rerun before drawing conclusions about vLLM compatibility.

Slurm's `CUDA_VISIBLE_DEVICES` is preserved. Compatibility runs disable `.env`
loading and clear inherited `RAY_ADDRESS` so they cannot override the assigned
GPUs or connect to another job's Ray instance. The batch script uses a private
job temporary directory and a job-derived HTTP port; an occupied port fails
validation rather than reusing the existing server. Only its own temporary
directory is removed at exit. Slurm manages worker cleanup when the job ends.

For a single-GPU smoke run or a different explicit candidate:

```bash
sbatch --gres=gpu:1 scripts/compatibility.slurm --suite smoke
sbatch scripts/compatibility.slurm --vllm-version X.Y.Z
# Run just the two-GPU tests:
sbatch scripts/compatibility.slurm --suite parallel
```

Resource overrides such as `--time=04:00:00` go **before** the script path;
launcher arguments such as `--suite smoke` go **after** it. Both suites require
two GPUs on the same node. A 16 GiB GPU is a sensible minimum starting point for
the small ungated `Qwen/Qwen2.5-0.5B-Instruct` model and its Hugging Face reference.
The first run needs network access on the compute node and several GB of disk
space for packages and model weights; later runs reuse download caches.

`sbatch` prints the job ID. Monitor it with `squeue -j JOB_ID`, read the combined
setup/test output in `slurm-vllm-lens-compat-JOB_ID.out`, and use `scancel JOB_ID`
to cancel that job if needed. Environments and reports are kept in the checkout,
so it must be on storage accessible from the compute node.

Git metadata must also be accessible from the compute node. A shared worktree
whose `.git` points to a node-local directory is insufficient; use a complete
shared clone or constrain the job to the node holding the checkout. For the
node-local `/home/ubuntu/vllm-lens` checkout on aft-0:

```bash
sbatch --nodelist=aft-0 scripts/compatibility.slurm
```

The suite runner rejects an unavailable Git revision and writes failed setup
evidence before checking CUDA or starting models.

Inside an existing GPU allocation, use `srun python3 scripts/run_compatibility.py`.
On a standalone GPU machine without Slurm, `python3 scripts/run_compatibility.py`
also works. When Slurm is detected, the launcher and test harness require a job
step and a device mask before touching GPUs; an `salloc` shell on a login node
alone is insufficient.

A smoke-only pass does not validate TP/PP or justify advancing the package pin.
The job log records the environment and results paths. Reports are saved under
`compatibility-results/<version>/<timestamp>-<unique-id>/{smoke,parallel}/`,
including `environment.json` (with Slurm job/step IDs and the device mask),
per-suite logs and JUnit XML. Previous runs are preserved. Attach the complete results directory to the PR after testing.

The launcher uses `uv pip` with an explicit candidate override, so it can test a
new release even after the package has an exact vLLM pin. It does not change
`pyproject.toml`, `uv.lock`, or an existing environment. Avoid `uv sync` in this
test environment: that restores the development lock and CUDA index overrides.

For debugging, rerun an individual suite with the environment path printed by
the launcher and a new output directory:

```bash
srun /path/to/test-environment/bin/python scripts/check_compatibility.py \
  --expected-vllm 0.30.0 --suite smoke --output-dir compatibility-results/retry-smoke
```

The script checks the installed vLLM version and visible GPUs before running,
requires successful HTTP patch registration, and starts a fresh HTTP server.
It refuses to reuse a server already listening on `VLLM_TEST_PORT` (default
8100), which could otherwise test a different checkout or dependency set.
Each suite runs in a separate process to isolate existing GPU teardown behavior.
Test engines use `VLLM_WORKER_MULTIPROC_METHOD=spawn`: forking a second pipeline
engine after PyTorch initializes CPU thread pools can deadlock during CPU tensor
allocation. The effective worker method is recorded in `environment.json`.
Any failure, missing JUnit report, empty selection or skipped test fails the run.

Reference parity uses **FP32 on both vLLM and Hugging Face**, with the existing
mean absolute error limit of 0.01. This checks capture/computation correctness
without treating different BF16 rounding sequences as integration failures.
Native-dtype (BF16 for the test model) functional checks still cover capture,
batch isolation, steering, hooks, HTTP and TP/PP. Casting captured BF16 values
to FP32 after inference is not an FP32 reference comparison: both engines must
execute in FP32.

Coverage:

| Suite | Checks |
| --- | --- |
| Smoke | Layer discovery; offline and async capture against Hugging Face; mixed-length batching; chunked prefill; offline generate/chat steering and hooks; async steering; HTTP completion/chat capture and interventions via base64 and binary transport; repeated-prefix isolation; persistent-hook cleanup |
| Parallel | TP=2 / PP=2 batched capture; PP capture/reference checks; norm-matched steering on TP=2 / PP=2 |

The HTTP tests check hook/native capture equality and the numerical sign/scale
of an injected vector at the capture layer. Zeroing hooks must change the
captured state; the following request must restore the baseline. This catches
silent no-ops that a successful completion or shape-only check would miss.

## Local validation before a release

CI runs CPU checks only. Run GPU compatibility tests in a Slurm allocation
using the commands above; there are no scheduled or manually dispatched
GPU jobs in GitHub Actions.

Before a release, run the smoke and parallel suites for the proposed pinned
version at the release commit, using a fresh environment. Run suites
sequentially to avoid GPU memory contention. Review failures and attach the
reports before updating this matrix and the release notes. This is a maintainer
release checklist; the PyPI publication workflow does not enforce GPU evidence.

Keep future candidates pending until both GPU suites pass. Do not mark a
candidate validated based on CPU results alone.

## Local results, 2026-10-02

Slurm job **138.0**, on aft-0 with two H100 80GB GPUs and driver 595.58.03,
tested the clean implementation revision
`62bf8eba3915fd7f7129d38e5ab055f32b3c6dcf`. The environment used Python 3.12.3,
vLLM 0.30.0, PyTorch 2.13.0 / CUDA 13.0, Transformers 5.18.0,
Accelerate 1.15.0 and Ninja 1.13.2. All six smoke groups were run separately
to inventory failures, followed by the complete standard parallel suite.
The normal launcher still stops at its first failed group.

| Group | Passed | Failed |
| --- | ---: | ---: |
| Discovery | 17 | 0 |
| Offline capture / HF parity | 4 | 1 |
| Chunked prefill | 3 | 0 |
| Offline interventions | 6 | 0 |
| Async interventions | 6 | 0 |
| HTTP hooks, transports and cleanup | 5 | 0 |
| TP/PP capture | 2 | 0 |
| Pipeline capture and steering | 7 | 0 |
| TP/PP norm-matched steering | 2 | 0 |
| **Total** | **52** | **1** |

There were no errors or skipped tests. All 101 CPU regressions also passed
without vLLM/CUDA, with Ruff 0.15.3 lint/format and Pyright passing.

Two setup dependencies were required: Ninja for FlashInfer builds and
Accelerate for reference-model CUDA placement. GPU checks also found a real
post-hook replacement bug: adding a bfloat16 delta to the MLP half of a fused
residual output left values as large as 1.97 when a hook requested zero.
Replacement now writes the requested stream directly and zeros that request's
residual slice, preserving other requests and original tensors. The existing
offline and HTTP checks pass with their original tolerances. CPU regressions
cover outliers, three dtypes, chained hooks, preceding steering and isolation.

The remaining failure is `test_batch_prompts_10_tokens`: bfloat16 vLLM versus
bfloat16 Hugging Face has mean absolute error **0.010466**, above the unchanged
**0.01** limit, for “In the beginning there was nothing but”. Native capture
and independent hook capture are bit-identical; the same discrepancy occurs
when the prompt runs individually. vLLM's fused residual/normalization path
and Hugging Face use different rounding sequences.

Separate ten-prompt diagnostics in Slurm job 137 found worst mean errors of
**0.000681** for float32 vLLM versus float32 HF, and **0.010051** for bfloat16
vLLM versus float32 HF. These are precision diagnostics. The acceptance tests
compared bfloat16 on both sides in job 138. All correctness thresholds remain
unchanged. A float32 reference alone does not resolve bfloat16 parity. The
maintainer subsequently approved FP32/FP32 reference parity, retaining BF16
functional checks; the complete suites must be rerun under that policy.

Full logs, JUnit, package/hardware/revision/Slurm metadata and CPU results are
retained at
`/home/ubuntu/vllm-lens/compatibility-results/0.30.0/diagnostic-job-138/`.
Precision diagnostics are under `hook-fix-job-137/`; combined job outputs are
`slurm-vllm-lens-compat-{137,138}.out`. A compact evidence archive is
`compatibility-results/0.30.0/evidence-62bf8eb.tar.gz`.

The earlier full inventory in shared-storage job 82 had 46 passed and 7 failed;
its six zeroing-hook failures motivated the fix. Its Git metadata was on a
different node, so those reports could not record a revision themselves. The
final job 138 reports record the tested SHA and a clean working tree correctly.

The BF16 reference discrepancy is accepted under the maintainer's FP32/FP32
parity policy; it is no longer a compatibility blocker. The ten-prompt FP32
diagnostics passed, but they do not cover the full updated suite. Slurm job 147
was submitted for that full rerun at `168de9a`; its completion evidence is
unavailable following the cluster outage on 2026-10-02. Do not describe it as
passing or still running without recovering the job reports.

PR #41 has since incorporated main's Q/K capture and FP32 Triton precision
default (`a71e386`). Previous GPU evidence predates this merge. Review can
proceed with that limitation; when the cluster returns, recover job 147's
reports and run both suites at the updated revision before advancing the pin.
The suite selections above cover residual-stream capture and interventions;
they do not include the separate Q/K GPU parity suites added on main.

Those pending statements describe the pre-rebuild state. The fresh validation
below supersedes them. PyPI still listed no stable release newer than 0.30.0
at the 2026-10-02 lookup.

## Fresh validation after the rebuild, 2026-10-02

Slurm job **5.0** completed both standard suites at clean revision
`5cbffc1712f6edb6e9d6218f07465849d3c7a69b`: **56 passed, no failures,
errors or skips**. Every selected group has a nonempty log and JUnit report;
both environment reports record passing status and an empty working tree.

| Group | Passed |
| --- | ---: |
| Discovery | 17 |
| Offline capture / FP32 HF parity | 5 |
| Async capture / FP32 HF parity | 3 |
| Chunked prefill | 3 |
| Offline interventions | 6 |
| Async interventions | 6 |
| HTTP hooks, transports and cleanup | 5 |
| TP/PP capture | 2 |
| Pipeline FP32 parity, capture and steering | 7 |
| TP/PP norm-matched steering | 2 |
| **Total** | **56** |

The rebuilt environment used Python 3.12.3, vLLM 0.30.0, PyTorch 2.13.0 /
CUDA 13.0, Transformers 5.18.0, Accelerate 1.15.0 and Ninja 1.13.2. Slurm
allocated two H100 80GB GPUs on aft-0 with driver 595.58.03 and device mask
`0,1`. Engines ran V1/eager with spawned workers. Reference engines execute
in FP32; functional engines retain native BF16. Separate Q/K GPU parity suites
are outside this result's coverage.

The first post-rebuild run, job 1 at `5db7890`, passed all 45 smoke tests and
both parallel-capture tests, then stalled creating the second pipeline engine.
Python stacks showed forked workers blocked at a CPU `torch.zeros` allocation
in `gpu_input_batch.py`, consistent with inherited PyTorch thread-pool locks.
Only job 1 was canceled. The runner now forces `spawn` for test engines and
records that setting. Job 5 passed all seven pipeline cases with this fix;
no correctness thresholds or suite selections were weakened.

All **128 CPU regressions**, Ruff 0.15.3 lint/format, Pyright 1.1.414 and
Slurm shell syntax checks passed for the fix. The CPU environment used CPU-only
PyTorch 2.14.1; it did not supply the GPU compatibility evidence.

Complete logs, JUnit, metadata, batch output and CPU JUnit are preserved in
[the compact job 5 evidence archive](compatibility-evidence/vllm-0.30.0-job-5.tar.gz).
The original run directory is
`/home/ubuntu/vllm-lens/compatibility-results/0.30.0/20261002T122444Z-myq9ko1h/`;
batch output is `slurm-vllm-lens-compat-5.out`. The incomplete job 1 reports
and CPU stack traces remain under `20261002T120553Z-itnpoyz2/`. Historical jobs
137/138/147 were not recovered from this rebuilt checkout; their recorded
results above remain distinct from this fresh passing validation.


The final public dependency setup was verified with `uv lock --check`,
`uv sync --locked` and `uv sync --locked --group benchmarking`. The Isambard
benchmark helpers introduced in `bb37942` were removed from the shared project
and lock; their private imports were replaced with public Hugging Face download
APIs and an explicit Apptainer image directory. Missing-image and dry-run checks
passed without downloads or job submissions. Final CPU regressions (128),
Ruff lint/format and Pyright passed. These packaging/benchmark changes do not
change the inference implementation validated in job 5.

## Diagnosing integration drift

Failure to install a completion/chat response patch or the custom HTTP routes
now logs the failed component, vLLM version and original exception. Offline-only
environments can still operate when serving dependencies are absent.

For serving validation, set `VLLM_LENS_STRICT_COMPATIBILITY=1`: a missing HTTP
integration raises during plugin registration instead of only warning. The
compatibility script sets this automatically. `VLLM_LENS_DISABLE=1` still makes
the plugin a complete no-op, including in strict mode.
