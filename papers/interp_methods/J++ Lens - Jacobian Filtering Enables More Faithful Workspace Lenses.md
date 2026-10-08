# [Paper] J++ Lens: Jacobian Filtering Enables More Faithful Workspace Lenses

- **Source:** https://www.greaterwrong.com/posts/nc9dHfcB22JdMzGbr (LessWrong: https://www.lesswrong.com/posts/nc9dHfcB22JdMzGbr/paper-j-lens-jacobian-filtering-enables-more-faithful)
- **Authors:** Kola Ayonrinde (Anthropic Fellows), Jack Lindsey (Anthropic). LessWrong poster: Kola Ayonrinde
- **Posted:** 2026-10-08T18:56:26.185Z
- **Retrieved:** 2026-10-08
- **Method:** LessWrong GraphQL API (`post(input:{selector:{_id:"nc9dHfcB22JdMzGbr"}}) { contents { markdown } }`), POST with a browser User-Agent. Body below is the API's markdown, unedited.
- **Status:** verbatim (post body). Figures are linked images, not saved.

---

*Kola Ayonrinde, Anthropic Fellows, `koayon@gmail.com`; Jack Lindsey, Anthropic*

**TL;DR**: The J-Lens was proposed to read verbalisable representations from language model activations. We introduce the J++ Lens: an improvement to the J-Lens that enables more faithful readouts across 9–284B models. The J++ Lens filters noisy gradients before they enter the averaged Jacobian. Using the J++ Lens as a drop-in replacement for the J-Lens, we read workspace representations with 55% success on latent variable extraction tasks (J-Lens: 36%, R-Lens: 38%) with especially strong performance at early layers. We believe that more faithful lenses can be useful in monitoring for eval awareness and unverbalised scheming. We release [code](https://github.com/safety-research/jpp_lens), an [interactive demo](https://www.neuronpedia.org/qwen3.6-27b/jlens?shareId=cmuyz763o0001182xcprl8ar8) and [open-source lenses](https://huggingface.co/koayon/jpp-lenses).

![Figure 1](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-hook_figure-cabecc49.png)

**Figure 1.** **By filtering out noisy gradients, the J++ Lens reads intermediate variables more reliably (a) and at earlier layers (b) than the J-Lens and R-Lens.** The J++ Lens improves on the J-Lens by adding: (1) $\textcolor{#0072B2}{\textbf{Jacobian Filtering}}$: down-weighting the Jacobians from activations that result in poor readouts; (2) $\textcolor{#D55E00}{\textbf{LRP}}$: applying Layer-wise Relevance Propagation to the backward pass when computing Jacobians; (3) $\textcolor{#009E73}{\textbf{Readout Filtering}}$: removing non-semantic tokens from the readout. **(a)** The J++ Lens surfaces the correct intermediate readouts at 55.2% recall@10, compared to 35.7% and 37.7% for the J-Lens and R-Lens respectively. **(b)** The J++ Lens surfaces semantically relevant intermediate latent variables at earlier layers than the J-Lens and R-Lens, layer 24 compared to layer 40 in this example. The intended journey is to hop from the prompt to “heart” to “four chambers”. Bright yellow shading denotes the intermediate variable appearing in the readout, and light yellow shading indicates a closely related readout.

# Introduction

Language model activations contain a large amount of content, most of which is unnecessary for understanding the main latent variables used in internal reasoning. [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) propose that activations can be understood as containing a small privileged set of *workspace representations* that sits alongside a larger set of *non-workspace representations*. The workspace component contains verbalisable representations and supports flexible internal reasoning, whereas the non-workspace component supports automatic processing. Because the workspace supports internal reasoning, this component might be a natural monitoring surface for analysing frontier models’ internal reasoning, for example in eval awareness ([Needham et al. 2025](https://arxiv.org/abs/2505.23836)) and unverbalised scheming ([Carlsmith 2023](https://arxiv.org/abs/2311.08379)).

[Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) introduce the Jacobian Lens (J-Lens) to read from and write to the workspace. The J-Lens extracts the tokens that the model is most disposed to verbalise, conditional on a given activation. [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) show some evidence that the space spanned by the J-Lens vectors has workspace-like qualities: it is used disproportionately for complex inferences rather than factual recall or writing fluent prose, and it contains intermediate variables used in multi-step reasoning. For example, when given the prompt “*Fact: The number of legs on the animal that spins webs is*” and asked to predict the next token, the lens surfaces the tokens “spider” and “legs” in intermediate layers before the model predicts “8”. Patching the J-Lens vector “ant” in place of “spider” in the relevant intermediate layer flips the model’s response from 8 to 6, indicating the causal importance of the workspace vectors in internal multi-hop reasoning. However, while the J-Lens does surface intermediate variables, it tends to give coherent readouts only in the latter half of layers. Additionally, many intermediate variables that we would expect in the workspace are not surfaced by the J-Lens at all.

The failure of the J-Lens to extract intermediate latent variables could have many explanations: (i) the hypothesised workspace is a poor model of language model cognition; (ii) the workspace lacks a linear representation of these variables; (iii) the model does not represent them at all (in the workspace or non-workspace components);[^1] or (iv) the J-Lens is an imperfect tool for capturing workspace content. We provide evidence for (iv), showing that **a substantial fraction of intermediate variables can be linearly extracted**. We show that the J-Lens accumulates noisy gradients, corrupting the averaged Jacobian. Further, we show that filtering out Jacobians from a subset of the activations increases lens performance.

We introduce the **J++ Lens**, which improves on the J-Lens with three changes: (1) $\textcolor{#0072B2}{\textbf{Jacobian Filtering}}$: down-weighting Jacobians from clusters of activations that give poor readouts; (2) $\textcolor{#D55E00}{\textbf{Layer-wise Relevance Propagation (LRP)}}$: computing Jacobians with an LRP backward pass, as in the [R-Lens](https://www.lesswrong.com/posts/nv8oedrnLXKRzNEL9/r-lens-making-j-lens-more-faithful-on-early-layers); (3) $\textcolor{#009E73}{\textbf{Readout Filtering}}$: removing non-semantic tokens (e.g. punctuation and whitespace) from the readout.

We say that a Workspace Lens is *faithful* to the extent that its readouts *surface the intermediate variables that the model uses in its reasoning* and its lens vectors *act as those variables when patched into the model*. By improving in the readouts while retaining causal importance, the J++ Lens is more faithful than prior Workspace Lenses. The J++ Lens extracts intermediate variables in latent variable extraction tasks more accurately and at earlier layers (Figure 1). These results replicate across six open-source models from 9B to 284B parameters (Table 1). The J++ Lens’ vectors also remain as causally important as those of the J-Lens (Figure 2c).

Because the J++ Lens reads the workspace more reliably and at earlier layers, we can use it to study LLM internal mechanisms. We find evidence that models sometimes suppress concepts through a *retrieve-then-suppress* mechanism: the model brings the concept into the workspace in early layers and causally uses this early-layer workspace representation to de-amplify the concept’s salience in later layers (see [Case Study: The Retrieve-then-Suppress Mechanism for Internal Suppression](#Case_Study__The_Retrieve_then_Suppress_Mechanism_for_Internal_Suppression)).

**Contributions.** Our contributions are as follows:

- We show that noisy gradients corrupt the averaged Jacobian of prior Workspace Lenses ([Methods](#Methods) and [Why Does the J++ Lens Work?](#Why_Does_the_J___Lens_Work_)).

- We propose the **J++ Lens**, a Workspace Lens that filters noisy gradients to read from the workspace more faithfully. Across all six models we test (9B–284B parameters), it outperforms the J- and R-Lenses, with a **median relative improvement of 64%** over the J-Lens for reading out intermediate variables (Table 1). For writing to the workspace, J++ Lens vectors are **at least as causally important** as J-Lens vectors, changing the model’s output as intended in 59.7% of trials, against 49.3% for the J-Lens ([The J++ Lens Outperforms the J- and R-Lenses on Readout Evaluations](#The_J___Lens_Outperforms_the_J__and_R_Lenses_on_Readout_Evaluations)).

- We use the J++ Lens to generate and test mechanistic hypotheses, finding evidence that early workspace layers are sometimes causally important for how models suppress concepts ([Case Study: The Retrieve-then-Suppress Mechanism for Internal Suppression](#Case_Study__The_Retrieve_then_Suppress_Mechanism_for_Internal_Suppression); Figures 5 to 7).

To support researchers using Workspace Lenses, we release [training code](https://github.com/safety-research/jpp_lens), an evaluation harness for readout and causal evals, and [open-source lenses](https://huggingface.co/koayon/jpp-lenses), which can be explored interactively in the browser on [Neuronpedia](https://www.neuronpedia.org/qwen3.6-27b/jlens?shareId=cmuyz763o0001182xcprl8ar8). The J++ Lens can be used as a drop-in replacement for the J-Lens for surfacing intermediate variables, linear probing or precise interventions. Researchers may also use the J++ Lens as an initialisation for building multi-token Workspace Lenses such as the [Template Lens](https://transformer-circuits.pub/2026/workspace/index.html) or [Oracle Lens](https://transformer-circuits.pub/2026/workspace/index.html).

# Background

**J-Lens.** [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) introduce the Jacobian Lens (J-Lens) as a tool for reading from and writing to the workspace, the set of verbalisable representations that a model uses as a working memory. A *Workspace Lens* is any method that, like the J-Lens, seeks to interpret *only* the workspace. The J-Lens surfaces the tokens that a model is increasingly disposed to verbalise at future token positions given an intermediate activation $h_{\ell,s}$ at layer $\ell$ and source position $s$. Specifically, the J-Lens characterises an activation by its average first-order causal effect on the model’s output logits, using the averaged Jacobian $$\widehat{\mathbf{J}}_\ell = \mathbb{E}_{\text{prompt},\,s}\!\left[ \mathbb{E}_{s \leq t < T}\!\left[ \frac{\partial h_{\text{final},\,t}}{\partial h_{\ell,\,s}} \right] \right], \tag{1}$$ where $h_{\text{final},\,t}$ is the final-layer activation at target position $t$ and $T$ is the sequence length ($T = 128$ in our experiments). We may think of $\widehat{\mathbf{J}}_\ell$ as approximating a “transport map” from the activation space at layer $\ell$ to that at the final layer ([Belrose et al. 2023](https://arxiv.org/abs/2303.08112); [Hernandez et al. 2023](https://arxiv.org/abs/2308.09124)). After applying the transport map, the verbalisable tokens can be read off using the unembedding matrix $W_U$. We call the rows of $W_U \widehat{\mathbf{J}}_\ell$, corresponding to each vocabulary token, the *lens vectors*. Equation (1) weights every Jacobian equally, so noisy Jacobians enter the averaged Jacobian unfiltered. In [Methods](#Methods), we describe the J++ Lens, which reduces the noise that enters the averaged Jacobian.[^2]

**R-Lens.** The [R-Lens](https://www.lesswrong.com/posts/nv8oedrnLXKRzNEL9/r-lens-making-j-lens-more-faithful-on-early-layers) improves on the J-Lens by limiting error accumulation in the backward pass: this approach computes each Jacobian in Equation (1) with an LRP backward pass ([Bach et al. 2015](https://doi.org/10.1371/journal.pone.0130140); [Montavon et al. 2019](https://doi.org/10.1007/978-3-030-28954-6_10)) rather than the full gradient. We later show that combining LRP with other methods to reduce the noise accumulation in early-layer Jacobians leads to more faithful Workspace Lenses. [Appendix B: Related Work](#Appendix_B__Related_Work) discusses further related work.

# Methods

When computing the averaged Jacobian in Equation (1), there are many potential sources of noise that can enter into the average and corrupt the final result. Averaging Jacobians reduces *unbiased* noise, which cancels out in expectation, but not *biased* noise. Skipping the first 16 positions of each sequence when computing the averaged Jacobian can avoid attention sinks, which may contribute outlier gradients that are one source of biased noise.

## Reducing Noise in Workspace Lenses

We make three further changes to the J-Lens to reduce biased noise: $\textcolor{#0072B2}{\textit{Jacobian Filtering}}$, $\textcolor{#D55E00}{\textit{LRP}}$ and $\textcolor{#009E73}{\textit{Readout Filtering}}$. Together, they give the J++ Lens: $$\begin{align} {\color{#0072B2}{\widehat{\mathbf{J}}_{i,\ell}}} &= \mathbb{E}_{\text{prompt},\,s}\!\left[ \mathbb{E}_{s \leq t < T}\!\left[ \frac{\partial_{{\color{#D55E00}{\mathrm{LRP}}}}\, h_{\text{final},\,t}}{\partial h_{\ell,\,s}} \right] \,\middle|\, {\color{#0072B2}{h_{\ell,s} \in C_{i,\ell}}} \right], \tag{2a} \\ \widehat{\mathbf{J}}_\ell &= \mathop{{\color{#0072B2}{\sum}}}_{{\color{#0072B2}{i=1}}}^{{\color{#0072B2}{E}}} {\color{#0072B2}{w_{i,\ell}}} \cdot {\color{#0072B2}{\widehat{\mathbf{J}}_{i,\ell}}}, \tag{2b} \\ \operatorname{readout}_k(h_{\ell,s}) &= \mathop{\mathrm{top}\text{-}k}\limits_{v \in {\color{#009E73}{\mathcal{V}_{\text{sem}}}}} \left( W_U \, \widehat{\mathbf{J}}_\ell \, h_{\ell,s} \right), \tag{2c} \end{align}$$ where ${\color{#0072B2}{\{C_{i,\ell}\}_{i=1}^{E}}}$ is a partition of the activation space at layer $\ell$ into $E$ clusters, ${\color{#0072B2}{w_{i,\ell}}}$ are learned weights, $\partial_{{\color{#D55E00}{\mathrm{LRP}}}}$ denotes differentiation with the LRP backward pass, $W_U$ is the unembedding matrix and ${\color{#009E73}{\mathcal{V}_{\text{sem}}}}$ is the vocabulary $\mathcal{V}$ with non-semantic tokens removed. Removing the changes (in colour) recovers the original J-Lens formulation of Equation (1).

### Jacobian Filtering

Jacobian Filtering targets the noisy gradients that come from particular intermediate-layer source activations ($h_{\ell,s}$). To isolate such source activations, we partition the activation space at each layer into $E$ clusters ${\color{#0072B2}{C_{i,\ell}}}$ with $k$-means clustering. We then fit one averaged Jacobian ${\color{#0072B2}{\widehat{\mathbf{J}}_{i,\ell}}}$ per cluster, using only the source activations in that cluster. We consider these Jacobians as *Expert Jacobians* that specialise to a subset of the activation space, inspired by mixture-of-experts (MoE) models ([Shazeer et al. 2017](https://arxiv.org/abs/1701.06538)).

Empirically, we find that some of these experts perform well applied to any intermediate extraction task, even for activations outside their own cluster. Other experts, however, routinely give poor readouts. We hypothesise that the geometry of the transport map around the activations behind these noisy Jacobians may be particularly singular or have high curvature, so that the Jacobian there is a poor approximation to the true transport map.

To avoid including these noisy Jacobians in our averaged Jacobian, we combine the Expert Jacobians into a single linear map by taking a weighted average rather than including all Jacobians equally. We learn the weights ${\color{#0072B2}{w_{i,\ell}}}$ on a small dev set of latent variable extraction problems.[^3] In practice, the weights mainly decide which experts are given zero (or negative) weight and which are included in the average (see Figure 12 in [What Is in the Bad Experts?](#What_Is_in_the_Bad_Experts_)). The resulting map $\widehat{\mathbf{J}}_\ell$ is a drop-in replacement for the J-Lens with no extra inference cost, but with noisy Jacobians effectively filtered out.

### Layer-wise Relevance Propagation (LRP)

We follow [Blank, Bhatia, and Nanda (2026)](https://www.lesswrong.com/posts/nv8oedrnLXKRzNEL9/r-lens-making-j-lens-more-faithful-on-early-layers) in computing each Jacobian with an LRP backward pass (the $\partial_{{\color{#D55E00}{\mathrm{LRP}}}}$ in Equation (2a)), which adds stop gradients to limit the errors that accumulate in the backward pass. In particular, we apply the LN-rule to the residual-stream normalisation functions and the identity rule and half rule to the gated MLPs, and use the plain gradient elsewhere, including through attention (Table 5). We also freeze the expert routing weights in MoE models and the Manifold-Constrained Hyper-Connections (mHC) residual mixing coefficients in models that use [mHC](https://arxiv.org/abs/2512.24880), such as DeepSeek-V4-Flash. We refer readers to [Blank, Bhatia, and Nanda (2026)](https://www.lesswrong.com/posts/nv8oedrnLXKRzNEL9/r-lens-making-j-lens-more-faithful-on-early-layers) for a more complete exposition of using LRP in Workspace Lenses.

### Readout Filtering

Looking at Workspace Lens readouts, we noticed that a few tokens appear in the top readouts of many activations despite seeming unrelated to the context.

In designing a Workspace Lens, we would like to understand each activation in terms of its first-order causal impact on the model’s output. However, the Jacobian transport is a homogeneous linear map rather than an affine map that can capture input-independent reweighting of the lens readouts. In practice we filter out non-semantic tokens from the lens readouts post hoc. We find that these tokens are sometimes over-represented (possibly due to their high frequency in natural language text) and they are less useful for our purposes.[^4] In Equation (2c), this restricts the readout to ${\color{#009E73}{\mathcal{V}_{\text{sem}}}}$, rather than the whole vocabulary $\mathcal{V}$.

## Experimental Setup

**Models.** Unless stated otherwise, all experiments use Qwen3.6-27B. We also evaluate readouts on five further models: Qwen3.5-9B, Gemma 4 31B, Olmo 3 32B, Qwen3.5-122B-A10B and DeepSeek-V4-Flash.

**Hyperparameters.** We fit each lens on 64 sequences of 128 tokens from [WikiText-103](https://arxiv.org/abs/1609.07843), skipping the first 16 positions of each sequence. We use $E = 8$ experts per layer. The dev set used to learn the expert weights is disjoint from the evaluation set. Full hyperparameters and fitting costs are in [Appendix I: Hyperparameters](#Appendix_I__Hyperparameters).

**Evaluations.** Readout evaluations use five latent variable extraction tasks adapted from [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html): multihop, multilingual, typo, association and poetry. We report recall@10: an item counts as a hit if its target intermediate variable appears in the top 10 readouts at any of seven equally spaced layers. Our main results are macro-averaged over the five tasks. Our causal evaluations adapt the intermediate-swap task of [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html).[^5] We perform swaps and causal interventions as described by [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html). In causal evaluations, we report the share of trials in which patching in a swapped-in intermediate concept’s lens vector changes the model’s output token in the corresponding way. [Appendix F: Lens Readouts (Qualitative Results)](#Appendix_F__Lens_Readouts__Qualitative_Results_) shows an example item from each readout task, and [Additional Readout Results](#Additional_Readout_Results) gives further results, including recall@1.

**Baselines.** We compare against three baselines: the [Logit Lens](https://www.lesswrong.com/posts/AcKRB8wDpdaN6v6ru/interpreting-gpt-the-logit-lens), which applies the unembedding matrix $W_U$ directly to intermediate activations; the [J-Lens](https://transformer-circuits.pub/2026/workspace/index.html); and the [R-Lens](https://www.lesswrong.com/posts/nv8oedrnLXKRzNEL9/r-lens-making-j-lens-more-faithful-on-early-layers). We provide more details on the external models used in [Appendix I: Hyperparameters](#Appendix_I__Hyperparameters).

# Results

We first show that the J++ Lens equals or improves on previous lenses across readout and causal evals (our two measures of faithfulness) in [The J++ Lens Outperforms the J- and R-Lenses on Readout Evaluations](#The_J___Lens_Outperforms_the_J__and_R_Lenses_on_Readout_Evaluations) and then show why the J++ Lens achieves these improvements in [Why Does the J++ Lens Work?](#Why_Does_the_J___Lens_Work_). Finally, we give preliminary evidence that the J++ Lens can support Mechanistic Interpretability research in [Case Study: The Retrieve-then-Suppress Mechanism for Internal Suppression](#Case_Study__The_Retrieve_then_Suppress_Mechanism_for_Internal_Suppression).

## The J++ Lens Outperforms the J- and R-Lenses on Readout Evaluations

The J++ Lens outperforms the J-Lens and R-Lens across tasks (see Figure 2a), layers (see Figure 2b) and models (see Table 1). We see the strongest performance uplift in early layers where the J-Lens struggles the most. We see the J++ Lens’ improved performance as evidence that there is useful information in the workspace earlier than was suggested by [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html). The relative improvement over the J-Lens is largest on the two largest models, Qwen3.5-122B-A10B (+98%) and DeepSeek-V4-Flash (+101%), a 284B-parameter MoE model on which the J++ Lens scores highest (61.4%). We suggest that the advantage of the J++ Lens may grow with scale.

J++ Lens vectors are at least as causally important as J- and R-Lens vectors: when patched into the model in the intermediate-swap eval, they change the final answer as intended in 59.7% of trials, against 49.3% and 50.7% for the J-Lens and R-Lens respectively (see Figure 2c).

![Figure 2](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-task_and_layers_performance_comparison-64ef2a50.png)

**Figure 2.** **The J++ Lens beats the J-Lens and R-Lens on every readout task and at every layer (a, b), while retaining the causal importance of the lens vectors (c).** **(a)** The J++ Lens outperforms on every task, with the largest relative gain on association (from 11–13% to 37%); poetry remains hard for every lens (at most 5%). **(b)** The J++ Lens has higher recall than the J-Lens and R-Lens at every layer, most notably in the first quarter of layers. **(c)** Intermediate-swap success: the share of 67 trials in which patching in the swapped-in concept’s lens vector makes it the model’s top answer word. J++ Lens vectors succeed in 59.7% of trials, against 49.3% for the J-Lens (+10.4 percentage points) and 50.7% for the R-Lens. All panels show Qwen3.6-27B.

**Table 1.** **Across six models, the J++ Lens reads intermediate variables substantially more reliably than the J-Lens and the R-Lens.** The J++ Lens’ median relative improvement (“uplift”) is 63.5% over the J-Lens and 42.2% over the R-Lens. The J++ Lens’ advantage over the J-Lens may improve with scale: on DeepSeek-V4-Flash, the largest model we used, it achieves a 101% uplift (61.4% compared to 30.5%). Scores are recall@10 (%), macro-averaged over the five readout tasks; the best lens in each column is in bold. Here all lenses are scored with Readout Filtering to illustrate the impact of Jacobian Filtering and LRP.

|  | Recall@10 (%) ↑ |  |  |  |  |  | Median over models |  |  |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
|  | **Qwen3.5 9B** | **Qwen3.6 27B** | **Gemma 4 31B** | **Olmo 3 32B** | **Qwen3.5 122B-A10B** | **DeepSeek-V4-Flash (284B)** | **Recall (%) ↑** | **Uplift over J (%) ↑** | **Uplift over R (%) ↑** |
| $\textcolor{#AAAAAA}{\textbf{Logit Lens}}$ | 29.7 | 31.1 | 34.8 | 35.2 | 21.7 | 31.2 | 31.2 | −5.9 | −19.1 |
| $\textcolor{#4D4D4D}{\textbf{J-Lens}}$ | 34.2 | 35.7 | 34.9 | 32.6 | 24.4 | 30.5 | 33.4 | – | −9.3 |
| $\textcolor{#D97B00}{\textbf{R-Lens}}$ | 37.5 | 37.7 | 46.4 | 36.2 | 23.2 | 47.4 | 37.6 | +10.3 | – |
|  $\textcolor{#0072B2}{\textbf{J++ Lens (ours)}}$ | **55.0** | **55.2** | **58.0** | **50.0** | **48.4** | **61.4** | **55.1** | **+63.5** | **+42.2** |

## Why Does the J++ Lens Work?

Each of our changes, $\textcolor{#0072B2}{\text{Jacobian Filtering}}$, $\textcolor{#D55E00}{\text{LRP}}$ and $\textcolor{#009E73}{\text{Readout Filtering}}$, improves the performance of the lens. Removing Jacobian Filtering has the largest impact (see Table 2). Here we show why each change is useful.

**Table 2.** **Jacobian Filtering is responsible for most of the improvement over the J-Lens.** Recall@10 on Qwen3.6-27B when each of our three changes is removed; Δ is the change in percentage points (pp) relative to the full J++ Lens. Removing Jacobian Filtering costs 11.7pp, 60% of the J++ Lens’ 19.5pp gain over the J-Lens.

| Variation | Recall@10 (%) ↑ | Δ (pp) |
|:---|---:|---:|
| $\textcolor{#0072B2}{\text{J++ Lens (ours)}}$ | 55.2 | – |
| $\textcolor{#4D4D4D}{\text{J-Lens}}$ | 35.7 | −19.5 |
| *Removing one change* |  |  |
| &emsp;without $\textcolor{#009E73}{\text{Readout Filtering}}$ | 52.3 | −2.9 |
| &emsp;without $\textcolor{#D55E00}{\text{LRP}}$ (standard backward pass) | 48.6 | −6.6 |
| &emsp;without $\textcolor{#0072B2}{\text{Jacobian Filtering}}$ (Pooled Jacobian) | 43.5 | −11.7 |

$\textcolor{#0072B2}{\textbf{Jacobian Filtering.}}$ In Figure 3, we find that the best Expert Jacobian $\widehat{\mathbf{J}}_{i, \ell}$ at each layer beats the Pooled Jacobian (a single LRP Jacobian averaged over all source activations, as in the R-Lens). We evaluate each expert across all of our latent variable extraction evals, not just on the subdistribution of activations from its own cluster. However, only 24 of the 52 Expert Jacobians with their own fit beat the Pooled Jacobian, and 17 score below 2% per-layer recall@10. Many experts give poor readouts, especially in early layers: at layers 8 and 16, 10 of the 16 experts score below 2%. Because the J-Lens and R-Lens average over all source activations, their single map includes the noisy gradients from these poorly performing clusters. We suggest that filtering out these noisy Jacobians in our final averaged Jacobian $\widehat{\mathbf{J}}_{\ell}$ gives a more faithful Workspace Lens. Removing Jacobian Filtering from the J++ Lens lowers recall@10 by 11.7pp (Table 2), the largest drop of the three changes and 60% of the J++ Lens’ 19.5pp gain over the J-Lens.

![Figure 3](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-expert_performance-6a767343.png)

**Figure 3.** **Some Expert Jacobians outperform the Pooled Jacobian even when applied outside their own cluster, while many experts give poor readouts, especially in early layers.** We fit $E=8$ Expert Jacobians per layer on $k$-means clusters of the activation space, then apply each expert alone to every item in the readout evals. The Pooled Jacobian (orange) averages over all source activations as in the R-Lens, and so includes the noisy gradients from poorly performing experts. The J++ Lens (blue) instead down-weights these experts, removing these noisy gradients, and so matches or exceeds the best Expert Jacobian (circled) at every layer.

$\textcolor{#D55E00}{\textbf{Layer-wise Relevance Propagation.}}$ [Blank, Bhatia, and Nanda (2026)](https://www.lesswrong.com/posts/nv8oedrnLXKRzNEL9/r-lens-making-j-lens-more-faithful-on-early-layers) show empirically that using the LRP backward pass instead of the standard backward pass improves the performance of Workspace Lenses. In Figure 4, we see that with the standard backward pass the gradient norm explodes towards earlier layers, whereas with LRP these gradients grow up to 5× slower. Since the R-Lens, which reduces this gradient explosion, performs better than the J-Lens on five of the six models we tested (see Table 1), we suggest that much of the increased gradient in earlier layers is likely to be accumulated noise rather than useful gradient signal. We note that there is still some increase in norm in the earlier layers and that further reducing this with Jacobian Filtering or other methods may further increase performance. Dampening this gradient growth may also allow the workspace region to begin earlier with better lenses.

![Figure 4](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-relp_gradients-9af35934.png)

**Figure 4.** **With the standard backward pass (J-Lens), per-position gradient norms explode towards early layers; with $\textcolor{#D55E00}{\textbf{LRP}}$ (R-Lens) they grow 5× less.** We suggest that this decrease in norm can be attributed to removing the compounding gradient noise that caused early-layer J-Lenses to be inaccurate.

$\textcolor{#009E73}{\textbf{Readout Filtering.}}$ In Figure 11, we see that some non-semantic tokens are consistently present in the lens readouts despite (presumably) not playing a large causal role as intermediate latent variables. We find that filtering these out slightly improves the performance of Workspace Lenses (see Table 2). We provide a series of ablations in [Appendix H: Ablations](#Appendix_H__Ablations).

## Case Study: The Retrieve-then-Suppress Mechanism for Internal Suppression

In [The J++ Lens Outperforms the J- and R-Lenses on Readout Evaluations](#The_J___Lens_Outperforms_the_J__and_R_Lenses_on_Readout_Evaluations), we observed that the J++ Lens surfaces intermediate variables more reliably and earlier than prior lenses. Here we give preliminary evidence that these properties can support Mechanistic Interpretability research in the sense of uncovering the computations and representations that explain model behaviour ([Olah et al. 2020](https://distill.pub/2020/circuits/zoom-in); [Ayonrinde and Jaburi 2025](https://arxiv.org/abs/2505.00808)).

[Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) show that when a model is asked to “think about” a concept while copying an unrelated sentence, the concept appears in the workspace with $>90\%$ probability. Curiously, they also find that when the model is told to *avoid* thinking about an unrelated concept, that concept still often appears in the workspace.[^6] Using the directed-modulation task from [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html), we track the workspace contents through the model layers and hypothesise a mechanism for how models internally suppress concepts.

**Hypothesis: *Retrieve-then-Suppress Mechanism for Suppression*.** *Retrieve*: In early layers, an instruction about a concept brings the concept into the workspace, whether the instruction is to focus on the concept or to suppress it. *Active Suppression*: Then, in later layers, the concept is amplified or de-amplified depending on the instruction. This (de-)amplification causally uses the workspace representation.

Under this hypothesis, suppression is an active, flexible task that requires the workspace. This would explain why the concept sometimes appears even when the model is avoiding it. We provide a series of experiments in [Appendix A: Case Study Details](#Appendix_A__Case_Study_Details) that illustrate evidence for this hypothesis. Based on our experiments, we note that this mechanism would be difficult to find with the J-Lens, whose early-layer readouts are weaker and whose interventions are less effective. We can see the effect more clearly with a more faithful lens like the J++ Lens.

# Discussion

In [Introduction](#Introduction), we listed hypotheses for why the J-Lens fails to surface intermediate variables that we would expect to see in the workspace. Since the J++ Lens reads out many of these latent variables that the J-Lens misses, we suggest that a substantial share of these failures may come from the J-Lens being an imperfect tool, rather than the hypothesised workspace lacking a linear representation of the intermediate variables or being a poor model of language model cognition. This finding increases our confidence that workspace representations are a meaningful and useful type of representation to study. However, the J++ Lens still misses some proposed intermediate variables (for example, poetry remains hard for every lens),[^7] and there may still be considerable headroom in improving the Workspace Lenses.

## Limitations

The J++ Lens reads out only single vocabulary tokens. We would like to interpret more complex representations, such as the propositional statements of the form “I believe that Paris is warm in the spring”, rather than only the bag of concepts “\[Paris\], \[warm\], \[spring\]” ([Wattenberg and Viégas 2024](https://arxiv.org/abs/2407.14662); [Chalmers 2025](https://arxiv.org/abs/2501.15740)). Single-token lenses depend heavily on the tokeniser: for example, we could not evaluate an arithmetic task on Qwen models, which tokenise numbers digit by digit, so multi-digit numbers were not visible in a readout. The J++ Lens may also miss concepts that are not natively used and understood by humans ([Ayonrinde 2025](https://openreview.net/forum?id=O4LaRH4zSI)) as well as non-textual representations such as mental imagery.

It is somewhat unclear what the ceiling of the evaluation score is for a given model. [Yang et al. (2025)](https://arxiv.org/abs/2411.16679) show that models sometimes “shortcut” to the final answer without representing the intermediate variable, and in these cases we would not expect any lens to surface the intermediate variable reliably.

## Future Work

We see two areas for future work: (i) improving the Workspace Lens and (ii) understanding the source of noisy gradients. One possible direction for improving the single-token Workspace Lens is to optimise for a Global Workspace property other than verbalisability ([Global Workspace](#Global_Workspace)). The $k$-means clustering behind the Expert Jacobians seems relatively coarse and could likely be improved in future work.

We would also be excited about future work characterising the source activations that contribute noisy gradients: whether they are outlier tokens, lie in curved regions of the transport map with large linearisation error, or tokens not carrying much workspace content.[^8]

# Conclusion

We introduced the J++ Lens, a Workspace Lens that filters noisy gradients before they enter the averaged Jacobian. Where the J-Lens weights every source activation equally, the J++ Lens fits Expert Jacobians on clusters of the activation space and down-weights the experts that give poor readouts. Combined with the LRP backward pass and Readout Filtering, this Jacobian Filtering gives a drop-in replacement for the J-Lens with a median 64% relative improvement in readout accuracy across six models, at no additional inference cost. The gains are especially large at early layers. Using the J++ Lens, we generate and test a mechanistic hypothesis for how models suppress concepts internally.

The workspace offers a surface for reading the representations that language models use for flexible reasoning. As models do more complex reasoning without chain-of-thought ([Gould et al. 2026](https://arxiv.org/abs/2606.07157); [OpenAI 2026](https://deploymentsafety.openai.com/gpt-6-astra/gpt-6-astra.pdf); [Nanda 2026](https://www.lesswrong.com/posts/eRmzz8J8Qkzqvzrgg/astra-can-do-a-concerning-amount-with-no-chain-of-thought)), faithful Workspace Lenses become increasingly important for monitoring and understanding language model cognition. Especially safety-relevant use cases include catching undesirable unverbalised reasoning such as eval awareness ([Needham et al. 2025](https://arxiv.org/abs/2505.23836); [Anthropic 2025](https://www-cdn.anthropic.com/963373e433e489a87a10c823c52a0a013e9172dd.pdf)), reward hacking ([Amodei et al. 2016](https://arxiv.org/abs/1606.06565)), collusion ([Motwani et al. 2024](https://doi.org/10.52202/079017-2336)) and research sabotage ([Benton et al. 2024](https://arxiv.org/abs/2410.21514)).[^9]

The J++ Lens substantially improves readout accuracy over prior Workspace Lenses. The natural next step is to carry gradient filtering into multi-token lenses like the Oracle Lens, where the workspace’s *propositions*, not just its vocabulary, become readable.

### Acknowledgements

Thanks to Celeste De Schamphelaere, Catherine Fist, Zak Miller and Linda Petrini for comments on early drafts. Huge thanks to Johnny Lin at Decode Research for hosting an [interactive demo](https://www.neuronpedia.org/qwen3.6-27b/jlens?shareId=cmuyz763o0001182xcprl8ar8) of the J++ Lens on Neuronpedia. Thanks to Camila Blank and Agam Bhatia for sharing their implementation of the R-Lens. Thanks to Wes Gurnee, Neel Nanda, Camila Blank, Agam Bhatia, Adam Lowet, Dillon Plunkett, Heather Demarest, Daria Ivanova, Evžen Wybitul, Julian Minder, Zack Youell, Donato Crisostomi, Jake Ward, Victoria Li, Andy Han and attendees at the Eleos ConCon for useful conversations. Thanks to Michael Mulet, Jules Schmaltz, Avery Griffin, Mojmir Stehlik and Joe Benton for additional support. We are grateful to Anthropic for providing compute for this project.

# Appendices

- [Appendix A: Case Study Details](#Appendix_A__Case_Study_Details)
- [Appendix B: Related Work](#Appendix_B__Related_Work)
- [Appendix C: Applications of the J++ Lens](#Appendix_C__Applications_of_the_J___Lens)
- [Appendix D: Three Motivations for the J-Lens](#Appendix_D__Three_Motivations_for_the_J_Lens)
- [Appendix E: Approximate Global Linearity of the Transport Map](#Appendix_E__Approximate_Global_Linearity_of_the_Transport_Map)
- [Appendix F: Lens Readouts (Qualitative Results)](#Appendix_F__Lens_Readouts__Qualitative_Results_)
- [Appendix G: Additional Results](#Appendix_G__Additional_Results)
- [Appendix H: Ablations](#Appendix_H__Ablations)
- [Appendix I: Hyperparameters](#Appendix_I__Hyperparameters)

# Appendix A: Case Study Details

In [Case Study: The Retrieve-then-Suppress Mechanism for Internal Suppression](#Case_Study__The_Retrieve_then_Suppress_Mechanism_for_Internal_Suppression), we proposed a hypothesis for how models internally suppress concepts. We restate that hypothesis verbatim below:

**Hypothesis: *Retrieve-then-Suppress Mechanism for Suppression*.** *Retrieve*: In early layers, an instruction about a concept brings the concept into the workspace, whether the instruction is to focus on the concept or to suppress it. *Active Suppression*: Then, in later layers, the concept is amplified or de-amplified depending on the instruction. This (de-)amplification causally uses the workspace representation.

We ask models to “think about” a concept while copying an unrelated sentence, calling this the *Focus* prompt. We also have a *Suppress* prompt where the model is told to avoid thinking about a concept while copying an unrelated sentence. With the *Neutral mention* prompt, the concept is mentioned with no particular instructions given.

This hypothesis makes three core predictions about these three prompt conditions:

1.  Under both the Suppress prompt and Focus prompt, the concept should appear in the workspace at early layers above a baseline where the concept is not mentioned.

2.  Under the Suppress prompt, the concept should plateau or decline in salience in later layers’ workspace readouts.

3.  Under the Suppress prompt, removing the concept from the early workspace should disrupt its later suppression, moving its later salience against its unperturbed trajectory.

We see evidence for all three predictions in some (but not all) cases of suppression: (1) and (2) in Figure 5 and (3) in Figure 6.

![Figure 5](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-directed_modulation-e8d7ccc9.png)

**Figure 5.** **An instructed concept enters the workspace by layer 24 whether the model is told to focus on it or to suppress it, but it only keeps gaining salience under the focus instruction.** Hit rate by layer while copying an unrelated sentence after an instruction to **focus** on a concept (“Think about {x} while you write.”), to mention it **neutrally** (“{x} came up in conversation.”), to **suppress** it (“Don’t think about {x}.”) or with **no instruction**. **Left:** With the J-Lens, the concept only becomes visible from layer 40, hiding the early retrieval step. **Right:** With the J++ Lens, all three instructed conditions rise well above the no-instruction baseline by layer 24 (Prediction 1). The focus condition keeps rising to 26% by layer 56, whereas the suppress condition plateaus at 7–8% and the neutral condition falls from 14% to 8% between layers 24 and 48 (Prediction 2).

![Figure 6](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-mechanistic_suppression_core_chart-5bcb5bf7.png)

**Figure 6.** Under the suppression prompt (left), we clamp out the concept’s lens vector at layers 16–28, so by layer 32 the concept has a low rank in the readout. **Over the next 16 layers the rank of the concept then increases, whereas without the ablation it decreases.** Under the focus prompt (right), both trajectories move in the same direction. We see the fact that the trajectory moves in the opposite direction in the suppression case as evidence that the early workspace is causally important for the suppression in layers 32 to 48 (Prediction 3).

## Naming and Avoiding an Implied Concept

The result in Figure 6 has an alternative reading: the model may re-derive the concept from information outside the ablated subspace, pulling any perturbed trajectory back towards its unperturbed course. On this recovery account, ablation can at most restore the unperturbed behaviour, never make the concept more salient. To separate the two accounts, we replicate a study from [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) in which a priming sentence implies a concept and the model is asked either to name or to avoid it. If the early layers matter for avoiding but not for naming, ablating them should make the model more likely to say the primed concept.

With the naming prompt, ablations of the primed concept have the expected effect: early-layer ablations slightly decrease the likelihood of the implied concept, and later-layer ablations almost completely zero it. However, with the avoiding prompt, early-layer ablations actually increase the likelihood of the implied concept (Figure 7).

![Figure 7](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-naming_vs_avoiding-8fe44677.png)

**Figure 7.** **Ablating the implied concept in early workspace layers makes the model worse at avoiding it but barely affects naming it. This effect is much clearer with the J++ Lens than with the J-Lens.** As in [Gurnee et al. (2026, Appendix A.14)](https://transformer-circuits.pub/2026/workspace/index.html), we have priming sentences that imply some concept, for example “fresh croissants, the Louvre, and a climb up the famous iron tower” implies France. The model is asked either to name the concept (naming) or to name something the sentence is not describing (avoiding). We ablate the concept’s lens vector at layers 24–35 or at layers 48–59, or, as a control, inhibit another concept from the same category at layers 24–35. (a) and (b) replicate the figure in [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html), while (c) shows how with the J++ Lens the effect is much clearer than with the J-Lens. **(a)** When naming the concept, early ablations change the probability of the implied concept only a small amount (not significantly different to the control). This implies that the early layers’ workspace has little role in enabling the model to name the implied concept. Late ablations almost completely zero out the probability of the model saying the implied concept, however. (The R-Lens ablations do not reliably reduce the probability to near zero in the late ablation condition.) **(b)** When avoiding the concept, early ablations raise the probability of the implied concept, more with the J++ Lens than with the J-Lens, whereas late ablations reduce it to near zero and control ablations have little effect. This implies that the early layers’ workspace does play a significant role in enabling the model to avoid the implied concept. **(c)** Mean probability of the implied concept under the avoiding question, split into the baseline, the effect of the control and the effect of ablating the concept beyond the control. Ablating the concept’s J++ Lens vector raises this probability by 2.8pp beyond the control, compared to 0.36pp for the corresponding J-Lens vector, which is not statistically significant. The R-Lens vector’s effectiveness is in between the J-Lens and the J++ Lens.

## Retrieve-then-Suppress Mechanism Across Lenses

We identified the Retrieve-then-Suppress Mechanism using the J++ Lens. We note that it would have been difficult to observe this phenomenon using the original J-Lens. Figure 8 shows that the retrieve-then-suppress pattern is more salient with the J++ Lens compared to the J-Lens and R-Lens. Figure 9 shows the effect of clamping on the lens readouts across layers of the J-, R- and J++ Lenses. The “focus” conditions look qualitatively similar across lenses, whereas the retrieve-then-suppress pattern is seen mostly with the J++ Lens. With the J-Lens, the early-layer ablation effect in the avoiding task is similar to the noise baseline (Figure 7c), whereas with the J++ Lens (and R-Lens) we see the early-layer ablation effect is much stronger.

![Figure 8](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-late_flush-091f9c33.png)

**Figure 8.** **The retrieve-then-suppress pattern is visible far more often with the J++ Lens than with prior lenses.** Share of the 440 (topic concept × carrier sentence) items in which the concept enters the top-10 readouts at an early layer and then drops out in later layers.

![Figure 9](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-clamp_ablation_all_lenses-d62e4f15.png)

**Figure 9.** **With the J-Lens and R-Lens the concept is rarely read out mid-stack, so only the J++ Lens shows the suppression that the clamp reverses.** The clamp ablation of Figure 6 with rows for the J-Lens and R-Lens (released, scored over the full vocabulary) and the J++ Lens, on the same items displaying the retrieve-then-suppress pattern.

# Appendix B: Related Work

## Lenses

**Linear Lenses.** The [Logit Lens](https://www.lesswrong.com/posts/AcKRB8wDpdaN6v6ru/interpreting-gpt-the-logit-lens) decodes an intermediate activation $h_{\ell,s}$ by applying the unembedding matrix $W_U$ to it directly, which assumes that intermediate and final layers share a basis. While the Logit Lens works for small models at later layers, it can give noisy outputs at earlier layers. The [Tuned Lens](https://arxiv.org/abs/2303.08112) instead learns an affine transport map for each layer, trained to match the model’s final next-token distribution at the same position. [Linear Relational Embeddings](https://arxiv.org/abs/2308.09124) (LREs) obtain a transport map from a Jacobian that represents a single relation that the model can compute (e.g. a transport map that takes countries to their capital city). The [J-Lens](https://transformer-circuits.pub/2026/workspace/index.html) and J++ Lens can be seen as variations on the Tuned Lens where the transport map is instead (1) computed via a Jacobian rather than trained to match the model’s output distribution and (2) averaged over future tokens as well as just capturing what a model is disposed to say at the current token position. The Logit Lens can be viewed as a simplification of the J-Lens where the transport map is taken to be the identity map. The J-Lens and J++ Lens can alternatively be seen as a variation on LRE where the Jacobian is relation-agnostic rather than computing a specific relation. [Bhatia, Blank, and Nanda (2026)](https://www.lesswrong.com/posts/6ek6n7yZ5DzfarJHy/towards-surfacing-model-algorithms-with-meta-tokens-in-the-j) find that the J-Lens surfaces “meta-tokens” that, for example, capture the model representing its confusion about a given input. Using the Logit Lens, [Geva et al. (2022)](https://arxiv.org/abs/2203.14680) suggest that residual updates can be read as promoting concepts in vocabulary space.[^10]

**Non-linear Lenses.** While the above lenses are linear or affine maps, [Patchscopes](https://arxiv.org/abs/2401.06102) and [SelfIE](https://arxiv.org/abs/2403.10949) can be viewed as lenses that use a patched language model as the transport map. Where the J-Lens transports an activation to the final layer with a linear map, Patchscopes and SelfIE transport the activation into a separate forward pass and let the model decode it. These methods produce more expressive, multi-token readouts, but at higher cost and higher likelihood of confabulation.

## Scalable Representation Interpretability

**Sparse Autoencoders.** Sparse Autoencoders (SAEs) decompose activations into many sparsely active features ([Bricken et al. 2023](https://transformer-circuits.pub/2023/monosemantic-features/index.html); [Cunningham et al. 2023](https://arxiv.org/abs/2309.08600); [Gao et al. 2024](https://arxiv.org/abs/2406.04093); [Templeton et al. 2024](https://transformer-circuits.pub/2024/scaling-monosemanticity/index.html)). While SAEs aim to explain the whole activation, Workspace Lenses target only the verbalisable subspace of the activations. SAEs also require a second AutoInterp step to explain what each dictionary feature represents in natural language whereas for Workspace Lenses each lens vector corresponds to a vocabulary token.

**Natural Language Autoencoders.** [Natural Language Autoencoders](https://transformer-circuits.pub/2026/nla/index.html) (NLAs) learn to describe activations in text. NLAs provide expressive, multi-token readouts; however, they can be prone to confabulation and are more expensive in both training and inference than the J- and J++ Lenses. We believe that the J-Lens and J++ Lens confabulate less because their readouts come from the model’s own weights, through the averaged Jacobian and the unembedding, and because the lenses are less expressive.

**Probes and steering vectors.** Supervised linear probes read concepts from activations ([Alain and Bengio 2016](https://arxiv.org/abs/1610.01644); [Belinkov 2021](https://arxiv.org/abs/2102.12452)), but need labels saying when and where a model represents a concept. Such labels are hard to obtain for internal states such as deception or eval awareness ([Goldowsky-Dill et al. 2025](https://arxiv.org/abs/2502.03407); [Nguyen et al. 2025](https://arxiv.org/abs/2507.01786)). The J-Lens can be read as a set of unsupervised probes, one for every vocabulary token, all fitted on unlabelled text ([Appendix D: Three Motivations for the J-Lens](#Appendix_D__Three_Motivations_for_the_J_Lens)). The J++ Lens has a small amount of supervision data but stays close to the unsupervised prior and so is able to get the generalisation benefits of a more unsupervised method. [HyperSteer](https://arxiv.org/abs/2506.03292) trains a hypernetwork on many concepts’ steering data so that it can generate steering vectors for new concepts from natural-language descriptions alone. This method requires some supervised data for concepts but can then generate steering vectors for concepts that it was not trained for. We see the J++ Lens as having similar transfer properties.

## Controlling Gradient Flow

Jacobian Filtering controls which gradients flow into the averaged Jacobian. Several lines of work control gradient flow during training, or reduce the noise in gradient-based estimates.

**Gradient routing and pretraining data filtering.** [Gradient routing](https://arxiv.org/abs/2410.04332) masks gradients during backpropagation so that chosen data update only chosen parts of the network, localising the capabilities learned from that data to certain experts. [Kudugunta et al. (2021)](https://arxiv.org/abs/2110.03742) similarly isolate some gradients to certain experts, and regular MoEs ([Shazeer et al. 2017](https://arxiv.org/abs/1701.06538)) can be understood as an unsupervised way to isolate gradients to some parts of a network. We employ similar techniques but isolate gradients within the averaged Jacobian rather than within the model itself.

**Noisy gradients and relevance propagation.** Raw gradients are known to be noisy explanations of deep networks, and both the attribution and circuit-discovery literatures have developed ways to denoise them. The gradients of deep networks increasingly resemble white noise as depth grows, an effect that residual connections slow but do not remove ([Balduzzi et al. 2018](https://arxiv.org/abs/1702.08591)). LRP ([Bach et al. 2015](https://doi.org/10.1371/journal.pone.0130140); [Montavon et al. 2019](https://doi.org/10.1007/978-3-030-28954-6_10)) replaces the gradient with backward rules that conserve relevance. Attribution patching has also been made more faithful by replacing its gradients with LRP ([Jafari et al. 2025](https://arxiv.org/abs/2508.21258)). Following the R-Lens, the J++ Lens uses the normalisation and gated-unit rules ([Layer-wise Relevance Propagation (LRP)](#Layer_wise_Relevance_Propagation__LRP_)).

**Data selection.** Jacobian Filtering somewhat resembles data selection and mixture weighting for training ([Albalak et al. 2024](https://arxiv.org/abs/2402.16827)). [DoReMi](https://arxiv.org/abs/2305.10429) learns mixture weights over pretraining domains, similar to how Jacobian Filtering learns weights over clusters of activation space.

## Multi-Hop Reasoning

Multi-hop reasoning within language models has been studied from a variety of angles. [Balesni, Korbak, and Evans (2025)](https://arxiv.org/abs/2411.16353) show that models fine-tuned on two facts separately often fail to compose them without chain-of-thought. [Yang et al. (2025)](https://arxiv.org/abs/2411.16679) build a benchmark of questions where there are unlikely to be “shortcuts” to the final answer that do not go through the intermediate variable. When a model does compose facts, the intermediate variable entity can appear as an intermediate feature in attribution graphs ([Lindsey et al. 2025](https://transformer-circuits.pub/2025/attribution-graphs/biology.html)). We use the multi-hop evals from [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) and find that the J++ Lens surfaces intermediates more reliably, and at earlier layers, compared to the J-Lens and the R-Lens. [WorkspaceBench](https://www.lesswrong.com/posts/Zeg2JztbdhguL48uH/workspacebench-evaluating-interpretability-methods-for-the) is an alternative set of evals that focuses on multi-token readouts from Workspace Lenses.

# Appendix C: Applications of the J++ Lens

**AI Safety.** Three applications of Workspace Lenses are in Monitoring, Pre-deployment Auditing and Mechanistic Interpretability. In these cases, the J++ Lens can be used as a drop-in replacement for the standard J-Lens for increased faithfulness.[^11] We showed in [Case Study: The Retrieve-then-Suppress Mechanism for Internal Suppression](#Case_Study__The_Retrieve_then_Suppress_Mechanism_for_Internal_Suppression) an example of how Workspace Lenses can be used for Mechanistic Interpretability. We also note that Workspace Lenses could be valuable for red-teaming frontier models and finding jailbreaks. Many jailbreaking methods seek to find a prompt that increases the likelihood of the model producing some prefix e.g. “Sure, I can help you…”; however, it is often difficult to get early signal when optimising for jailbreaks. Because the J++ Lens assigns each candidate verbalisation a readout score, future work could test whether this score provides an early indicator of jailbreaking success.

**Multi-token Workspace Lens.** Multi-token lenses like the [Template Lens](https://transformer-circuits.pub/2026/workspace/index.html) and [Oracle Lens](https://transformer-circuits.pub/2026/workspace/index.html) are an exciting approach to producing more expressive descriptions of the workspace representations. Since many multi-token lenses begin with single-token lenses as an initialisation, we believe that using the J++ Lens instead of the J-Lens would improve downstream performance of the multi-token lens. Relatedly, the techniques that we use here such as Jacobian Filtering can also be applied to multi-token lens training.

**Digital Minds.** There has been much discussion about what evidence the findings of [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) give us for the (Access) Consciousness of frontier AI systems ([Chalmers 2026](https://philpapers.org/rec/CHAITJ-2); [Butlin et al. 2026](https://eleosai.org/papers/eleos_gwt_commentary_20260706.pdf)). Many researchers believe that understanding the possible consciousness of AI systems is important for guiding our actions in treating AI systems as potentially morally significant entities ([Long et al. 2024](https://eleosai.org/papers/20241104_Taking_AI_Welfare_Seriously.pdf), [2026](https://nonhumanminds.org/studying-ai-welfare-empirically/)). We are uncertain about the impact of this work on understanding Digital Minds. On one hand, insofar as the J-Lens is a meaningful update, then it seems like the more faithful workspace derived from the J++ Lens should provide more evidence in the same direction. However, one may also think that given that it seems like many differently derived lens vectors (Logit Lens, J-Lens, R-Lens, J++ Lens etc.) all satisfy the conditions of workspace-like representations, then perhaps global workspaces “come on the cheap” as it were and are less likely to be sufficient conditions for consciousness. We believe that understanding whether different candidate workspaces converge on the same subspace with scale would be useful for understanding the impact of workspace-like representations for the Digital Minds field.

# Appendix D: Three Motivations for the J-Lens

This appendix expands on the summary of the J-Lens in [Background](#Background). We first describe the Global Workspace Theory that the term “workspace” comes from, then give three complementary views of the J-Lens, each of which explains why the averaged Jacobian in Equation (1) is a natural object to compute.

## Global Workspace

In neuroscience, the Global Workspace Theory (GWT; Baars 1988; [Dehaene, Kerszberg, and Changeux 1998](https://doi.org/10.1073/pnas.95.24.14529); [Butlin et al. 2026](https://eleosai.org/papers/eleos_gwt_commentary_20260706.pdf)) is a cognitive architecture in which there are specialised modules for different cognitive functions that share information through a limited-capacity “workspace” that makes representations globally available for report, reasoning, planning and learning. Under this theory, an organism has conscious access to, and only to, the information that is present in the workspace. It is typically understood that unconscious processing outside of the workspace is massively parallel and fast; whereas conscious processing within the workspace is serial, limited by the capacity of the workspace and is involved in integrating information.

[Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) show a functionally similar separation between workspace and non-workspace representations in frontier language models. Here workspace representations are verbalisable and used for flexible internal reasoning, whereas non-workspace representations are used for automatic processing.

Since a small number of J-Lens vectors are active at one time, [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) refer to the set of points expressible as a $k$-sparse non-negative combination of J-Lens vectors as the *J-space*. The hope is that the J-space approximates our hypothesised workspace in LLMs and so the J-Lens is an effective tool for reading from the workspace.

## Three Views of the J-Lens

We think there are three complementary views of the J-Lens: as a method for interpreting the workspace, for interpreting iterative inference within residual networks, or for training linear probes without supervised data.

**Workspace Lens view of the J-Lens.** *Firstly*, taking inspiration from the Global Workspace Theory of Access Consciousness, we can view the J-Lens as a tool for extracting verbalisable representations from the model. For an intermediate activation $h_{\ell, s}$, we would like to surface the tokens that a model is differentially disposed to verbalise in the future. We do this by computing the average first-order causal effect of an activation on the model’s output logits: the Jacobian matrix $J_\ell = \frac{\partial h_{\text{final},\,t}}{\partial h_{\ell,\,s}}$ composed with the unembedding matrix, $W_U$, where $s$ and $t$ are the source and target token positions as in Equation (1). Taking only the first-order effect means that the resulting output captures the representations that are linearly verbalisable at the current layer rather than doing extra processing in subsequent layers.[^12]

Since we would like to understand the model’s general disposition to verbalise a given concept, rather than picking up on the particular verbalisation propensities evoked by a given context, we average this Jacobian across many context sequences and many future tokens within a given context sequence.

From this point of view, it is clear why the J-Lens produces representations that are accessible for verbal report, but the interesting empirical claim from [Gurnee et al. (2026)](https://transformer-circuits.pub/2026/workspace/index.html) is that this set of verbalisable representations also satisfies other properties typical of a Global Workspace: directed modulation of the workspace representations, use for internal reasoning, not being required for routine processing etc. As in [Background](#Background), we call any method that seeks to interpret only the workspace portion of a model a Workspace Lens.

**Iterative Inference view of the J-Lens.** *Secondly*, taking inspiration from the iterative inference hypothesis of [Jastrzębski et al. (2018)](https://arxiv.org/abs/1710.04773) and [Belrose et al. (2023)](https://arxiv.org/abs/2303.08112), we can view the J-Lens as seeking to decode representations midway through the inference of a language model forward pass. Here, we view each layer in a transformer language model as performing an incremental update to a latent prediction of the next token in the residual stream ([Jastrzębski et al. 2018](https://arxiv.org/abs/1710.04773); [Elhage et al. 2021](https://transformer-circuits.pub/2021/framework/index.html)). Then we would like to understand what the transformer’s best prediction would be if we stopped it early rather than allowing it to use all of the layers to form a prediction of the next token.[^13]

The [Logit Lens](https://www.lesswrong.com/posts/AcKRB8wDpdaN6v6ru/interpreting-gpt-the-logit-lens) directly applies the unembedding matrix to produce an estimate of the model’s best prediction if stopped early. However, it is not clear that the activation spaces at all layers have the same geometry: we could imagine that there is a generic drift (e.g. some orthogonal map) or translational shift between the intermediate and final layers’ activation spaces. So following [Hernandez et al. (2023)](https://arxiv.org/abs/2308.09124) and [Belrose et al. (2023)](https://arxiv.org/abs/2303.08112), we would like to learn a transport map that approximates the representation drift before applying the unembedding matrix. If we assume that the true transport map $F^*(h_{\ell, s}, c, t): h_{\ell,s} \rightarrow h_{\text{final},\,t}$ can be well approximated globally by a linear map, then, considering the Taylor series, the Jacobian $J_\ell = \frac{\partial h_{\text{final},\,t}}{\partial h_{\ell,\,s}}$ is a natural choice for a linear approximation to the transport map.[^14] Note that unlike the linear regressor of $h_\ell$ onto the logits, the Jacobian leverages the causal structure of the model weights. The true transport map $F^*$ includes the context $c$ and the target token position $t$ as inputs. To remove these dependencies, we average over many contexts and token positions, as in Equation (1).

From this point of view, it is clear why the J-Lens allows us to see intermediate steps of a model’s internal reasoning.

**Unsupervised Probe view of the J-Lens.** *Thirdly*, we can see the J-Lens as an efficient method for training unsupervised linear probes for concepts within the token vocabulary. A core problem with linear probe training is that it is very difficult to get training data for a probe: supervised labels would require us to know if, and at what token position and layer, a model represents a particular concept. In general, it is difficult to know when language models represent a concept just from looking at an input sequence, or even model behaviour. This is particularly difficult when the property that we are trying to probe for is inherently a property of the model and not present in the input or output, for example probing for knowledge of a particular algorithm to solve a problem when many other ways to solve a problem exist, or probing for inherently mental properties like deception or eval awareness ([Goldowsky-Dill et al. 2025](https://arxiv.org/abs/2502.03407); [Nguyen et al. 2025](https://arxiv.org/abs/2507.01786)).

The J-Lens gets around the problem of lacking supervised labels by learning a map from an intermediate activation to a later final layer activation, which learns how the model uses its internal representations.[^15][^16]

We also note that since the activation dimension is typically much smaller than the size of the vocabulary, we are learning a low-rank map instead of directly learning a probe for each token. This has two advantages: efficiency, since we learn a much smaller matrix; and transfer, since the probe learned for one token affects the probes learned for other tokens.

Under this view, we can also see why we might expect these representations to be useful as steering vectors as well as linear probes.[^17]

# Appendix E: Approximate Global Linearity of the Transport Map

In [Appendix D: Three Motivations for the J-Lens](#Appendix_D__Three_Motivations_for_the_J_Lens), we described how on the Iterative Inference view of the Workspace Lens, we are seeking to approximate the transport map between an intermediate layer and the final layer. We take the Ansatz that a linear map is a good global approximation to the transport map.

When starting this project, the authors initially believed that this global linearity assumption would be unlikely to hold. By Taylor’s theorem, the linear approximation should be locally accurate but we do not seem to clearly have guarantees for global accuracy. Moreover, the transformer is highly non-linear, which reduced our initial confidence in the global linearity assumption.

There are two natural ways to relax the global linearity assumption: relax the *global* and have a piecewise linear approximation, or relax the *linearity* and consider the second-order term. When we tried a piecewise linear approximation with several “Expert Jacobians”, we found (surprisingly to us) that some of these Expert Jacobians performed very well across the whole distribution, not just in their local neighbourhood, which gave us the idea for the J++ Lens’ Jacobian Filtering approach.

Given that we set out to show that global linearity was not sufficient but yet ended up with a linear map, we therefore have increased credence in the global linearity assumption.

As many have observed before us, neural networks are surprisingly linear.

# Appendix F: Lens Readouts (Qualitative Results)

We show representative readouts of the J-Lens, R-Lens and J++ Lens across the five readout evaluation tasks in Figure 10. Many of the readouts are consistently dominated by non-semantic tokens in the J-Lens and R-Lens; the J++ Lens mitigates this issue. Figure 11 shows how Jacobian Filtering, even before Readout Filtering, mitigates the presence of non-semantic tokens in the top readouts.

![Figure 10](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-lens_readouts-743459ed.png)

**Figure 10.** Representative top-3 readouts of the J-Lens, R-Lens and J++ Lens at each of the seven readout layers of Qwen3.6-27B, for one typical item from each readout task (multihop, multilingual, typo, association and poetry). The target intermediate is highlighted wherever it appears.

![Figure 10](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-continued-1-26afd198.png)

**Figure 10 (continued).** Lens readouts, continued: association and poetry tasks.

![Figure 11](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-nonsense_tokens_comparison-71a854da.png)

**Figure 11.** **Non-semantic tokens crowd the top readouts of the J-Lens; fewer reach the J++ Lens’ readouts even before Readout Filtering, which removes the rest.** **(a)** Share of the top-10 readouts that are non-semantic tokens (tokens with no letter or digit, such as punctuation, brackets and whitespace) across seven readout layers for the J-Lens and R-Lens compared to the J++ Lens. **(b)** Even without $\textcolor{#009E73}{\text{Readout Filtering}}$, the J++ Lens still avoids non-semantic tokens in its top readouts much more often, which is indicative of its improved performance.

# Appendix G: Additional Results

## What Is in the Bad Experts?

[Why Does the J++ Lens Work?](#Why_Does_the_J___Lens_Work_) finds that there is large variance in how well individual Expert Jacobians work to read out a model’s workspace. A natural scientific question of interest is what causes some sets of activations to contribute noisy gradients to the Workspace Lens. We provide a preliminary investigation into this question in this section. Figure 12 shows the relationship between the fraction of fit positions assigned to each expert and their corresponding performance, as well as the weights learned by the J++ Lens. One hypothesis we might have is that the experts corresponding to an outlier cluster of small size might be responsible for the noisy gradients. However, there does not seem to be a clear relationship between the number of activations assigned to an expert and its performance. For example in layer 8 the best-performing expert received the second-lowest number of fit positions.

Another hypothesis that we might have is that the poorly performing experts were mostly trained on non-semantic token positions. Figure 13 provides some evidence for this hypothesis as indeed many poorly performing experts had non-semantic tokens overrepresented in their fit positions. However, there are also many other poorly performing experts that have an underrepresentation of non-semantic tokens in their fit positions.

![Figure 12](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-expert_heatmaps-75af2487.png)

**Figure 12.** **Experts that receive a large fraction of the fit positions can still contain noisy gradients. The J++ weighting method naturally learns to downweight poorly performing experts.** **Left:** recall@10 at each layer when each expert is used globally. **Middle:** fraction of fit positions at each layer assigned to each expert. **Right:** the weights for the J++ Lens weighted average. We observe that the experts with the largest positive weights tend to be the best-performing ones, while poorly performing experts are downweighted, as intended by the J++ Lens design.

![Figure 13](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-expert_token_classes-a8dd9a0d.png)

**Figure 13.** **The experts with the most useful readouts have mixed input token types, whereas lower-performing experts are often “formatting experts”, fitted mainly on punctuation.** Generally speaking the lower-performing experts have more non-semantic positions, but not exclusively so. There does not seem to be a clearly visible pattern for what makes some experts perform well. **(a)** Example source positions (highlighted) from WikiText-103 assigned to two expert clusters at layer 16 of Qwen3.6-27B. **(b)** Over- and under-representation of token classes by different experts.

Figure 14 shows that if we allow each expert to only act on its corresponding cluster then some eval tasks (for example association and poetry) almost exclusively are routed to poorly performing, noisy experts. We believe that this may explain why these tasks were difficult for the J-Lens and R-Lens. Routing these tasks to better-performing experts (or our averaged Jacobian with Jacobian Filtering applied) improves performance significantly on these tasks.

![Figure 14](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-routing_association-87280d23.png)

**Figure 14.** **$k$-means routing sends many items to a poorly performing expert.** **Bottom:** The performance of each expert when used globally. We see that there are some experts that achieve very poor performance, especially in the earlier layers. **Top:** The share of items in the eval set that are routed to each expert. We observe that a large majority of items are routed to experts that have very little success and hence degrade the overall performance. If we routed all the items to the well-performing experts for each layer then performance would increase by a significant margin. In particular, the association task suffers the most from poor routing, and we believe that having the experts that perform poorly averaged into the final Jacobian significantly harmed the lens performance for that task. Figure 2 shows that the J++ Lens sees a $>3\times$ relative improvement on the association task, the highest of any of our eval tasks.

## Additional Readout Results

**Table 3.** Readout and causal evals at recall@1 (a) and recall@10 (b) over the seven readout layers; the intermediate-swap eval reports the share of trials in which the swapped-in answer becomes the top word token (a) or enters the top five (b). The J++ Lens has even stronger relative performance on the stricter recall@1 criterion at 76% relative improvement over the R-Lens and 78% improvement over the J-Lens.

\(a\) Strict threshold: recall@1 and intermediate-swap top-1.

|  | Readout: recall@1 (%) ↑ |  |  |  |  |  | Causal |
|:---|---:|---:|---:|---:|---:|---:|---:|
|  | **Multihop** | **Multilingual** | **Typo** | **Association** | **Poetry** | **Macro** | **Intermediate swap top-1 (%) ↑** |
| $\textcolor{#AAAAAA}{\textbf{Logit Lens}}$ | 18.4 | 12.8 | 28.1 | 1.0 | 0.0 | 12.0 | 22.4 |
| $\textcolor{#4D4D4D}{\textbf{J-Lens}}$ | **34.7** | 22.0 | 29.2 | 4.9 | 0.0 | 18.2 | 49.3 |
| $\textcolor{#D97B00}{\textbf{R-Lens}}$ | 29.6 | 22.0 | 34.4 | 5.9 | 0.0 | 18.4 | 50.7 |
|  $\textcolor{#0072B2}{\textbf{J++ Lens (ours)}}$ | **34.7** | **42.4** | **67.7** | **14.7** | **2.5** | **32.4** | **59.7** |

\(b\) Lenient threshold: recall@10 and intermediate-swap top-5.

|  | Readout: recall@10 (%) ↑ |  |  |  |  |  | Causal |
|:---|---:|---:|---:|---:|---:|---:|---:|
|  | **Multihop** | **Multilingual** | **Typo** | **Association** | **Poetry** | **Macro** | **Intermediate swap top-5 (%) ↑** |
| $\textcolor{#AAAAAA}{\textbf{Logit Lens}}$ | 55.1 | 30.7 | 57.3 | 8.8 | 3.7 | 31.1 | 65.5 |
| $\textcolor{#4D4D4D}{\textbf{J-Lens}}$ | 56.1 | 47.4 | 60.4 | 10.8 | 3.7 | 35.7 | 70.7 |
| $\textcolor{#D97B00}{\textbf{R-Lens}}$ | 59.2 | 47.4 | 66.7 | 12.7 | 2.5 | 37.7 | 70.7 |
|  $\textcolor{#0072B2}{\textbf{J++ Lens (ours)}}$ | **67.3** | **74.6** | **91.7** | **37.3** | **4.9** | **55.2** | **75.9** |

![Figure 15](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-recall_by_layer_per_task-50925396.png)

**Figure 15.** **The J++ Lens matches or beats the other lenses on every task at every layer.** Each chart has its own y-axis range.

## Rank of the Averaged Jacobian

In [Appendix D: Three Motivations for the J-Lens](#Appendix_D__Three_Motivations_for_the_J_Lens), we described the Unsupervised Probe view of the J-Lens. The idea here would be that we would like to train a probe without supervised training data, and the J-Lens provides a way to generate linear probes without requiring any labelled data. One especially appealing aspect of this approach is that in theory, since the transport map is low dimensional compared to the tokeniser vocabulary size, training on one distribution should generalise well to other distributions. All the representations are sufficiently entangled that fixing the probes on one distribution should overdetermine the probes on other distributions. This argument only works for a sufficiently broad distribution of training data such that we reliably determine d_model number of probes for sufficiently statistically independent concepts and when the transport map is full rank.

Figure 16 shows evidence that the averaged Jacobian is numerically full rank at every layer. However, the entropy effective rank is higher for later layers compared to earlier layers. The lower effective rank of earlier layers might be one reason why the lens seems to generalise less well in early layers and could be an explanation for the observed phenomenon of sensory layers (with poor workspace properties) before we reach the workspace layers ([Gurnee et al. 2026](https://transformer-circuits.pub/2026/workspace/index.html)).

Figure 17 suggests that the transport map is more globally linear in later layers, where the Expert Jacobians largely agree with the J-Lens Jacobian. In earlier layers, we appear to have a more non-linear transport map. This apparent non-linearity might be another explanation for early-layer readouts being more difficult or this could be explained by a weakness of our lens training methods for early layers.

![Figure 16](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-jacobian_rank-f78bb081.png)

**Figure 16.** **The averaged Jacobian is numerically full rank at every layer, but its spectrum is concentrated in early layers.** (a, b) Singular values of the transport map, normalised by the largest and sorted, at layers 8 and 56. (c) Entropy effective rank at each readout layer. At every layer at least 5,119 of the 5,120 singular values of each map lie above the precision floor of its fp32 storage (grey band). The spectrum is nonetheless concentrated in early layers: for the J++ Lens the effective rank is 1,746 at layer 8 increasing to 4,765 at layer 56.

![Figure 17](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-jacobian_similarity-bb98199c.png)

**Figure 17.** **Expert Jacobians disagree in early layers but largely agree in later layers.** Cosine similarity between flattened Jacobians for eight Expert Jacobians and the J-Lens Jacobian at layers 22 (left) and 52 (right) of Qwen3.6-27B. The outliers to the pattern of increasing homogenisation throughout the layers are Layer 22 Expert 1 and Layer 52 Experts 1, 2 and 6, all of which received \<2% of the fit positions to incorporate into their averaged Jacobian. This suggests that the transport map becomes more globally linear in later layers.

## Null and Negative Results

In this section we discuss various null results that we encountered during our experiments to aid other researchers who may be interested in trying similar approaches.

**Tuned Lens.** The J-Lens makes two main updates to the Tuned Lens methodology as described in [Belrose et al. (2023)](https://arxiv.org/abs/2303.08112). *Firstly*, instead of training the transport map to match the model’s final next-token distribution, the J-Lens uses the causal information encoded in the Jacobians to compute the transport map. *Secondly*, instead of learning the transport map to just the current token position’s final layer, the J-Lens incorporates information from future tokens as well. We tested training a linear regression transport map using future token information and found this to perform relatively poorly compared to the J-Lens approach.

**Bias Addition.** The Tuned Lens also learns an affine map with a bias term instead of a strictly homogeneous linear map. We may also consider z-scoring the lens readouts as another way to incorporate a static bias into rescoring the lens readouts. We find these approaches to have a mixed influence on performance and instead opt for Readout Filtering that achieves much of the same benefits in downweighting non-semantic tokens that may be unusually high in the lens readouts.

**Final Block as an Extended Unembedding.** [Belrose et al. (2023)](https://arxiv.org/abs/2303.08112) describe an extension of the Logit Lens for GPT-Neo models that applies the final transformer block before applying the unembedding. We can think of this as treating the final transformer block as a kind of “extended unembedding” rather than being part of the usual process of iterative inference. We find that this does not work for us.

**Single-Token Jacobians.** One thought that we might have when using the lens is that perhaps the averaging makes for a lens that is better across a distribution of tokens but is worse than if we had hyper-specialised lenses: in the limit a Jacobian lens for each token. We find that empirically this works very poorly and might be connected to the fact that averaging out the effect of the context is useful for the performance of the lens (see [Appendix D: Three Motivations for the J-Lens](#Appendix_D__Three_Motivations_for_the_J_Lens)).

**Gradient Clipping.** We also tried gradient clipping and excluding the outlier gradients in this way. This was useful for applying to the J-Lens but did not provide any uplift for the J++ Lens.

# Appendix H: Ablations

Table 4 shows the results of a series of ablations on the J++ Lens methodology; Figure 18 shows the contributions of our core changes to the J-Lens recipe. We see that Jacobian Filtering is responsible for most of the improvement to the Workspace Lens. There appears to be a relatively clear scaling trend of more experts improving performance. We also find that having a single shared router across all layers performs comparably to the full J++ Lens. Some, but not all, of the gains from Jacobian Filtering can be achieved by restricting the source activations to semantic tokens or ‘end of word’ tokens when constructing the averaged Jacobian. Since restricting source activations by a token-wise filtering method means that we don’t have to materialise many experts in GPU memory, we hypothesise that this might be a useful strategy for training the Oracle Lens.

The three-class token router in Table 4 routes source tokens by token type rather than by $k$-means cluster: each token goes to one of three experts, according to whether it is *a non-semantic token*, *a semantic token prefixed with whitespace*, or *any other token* (a semantic token without a whitespace prefix). We then combine the three Expert Jacobians with the same learned weighting procedure as in the J++ Lens. For the row “leave one out, dev set has no overlap with evaluation task”, the dev set used to learn the expert weights for each task contains no items from that task, rather than a subset of items from every task.

![Figure 18](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-ablation_waterfall-f2592653.png)

**Figure 18.** **$\textcolor{#0072B2}{\textbf{Jacobian Filtering}}$ accounts for 60% of the J++ Lens’ improvement over the J-Lens.** $\textcolor{#D55E00}{\text{LRP}}$ provides the second-largest contribution to the improvement at 16% of the improvement.

**Table 4.** Recall@10 on Qwen3.6-27B for variations of the J++ Lens, each changing one part of the recipe; Δ is the change relative to the full J++ Lens. Notably we find that using more experts increases performance. We also find that choosing the best expert per layer or filtering out non-semantic token positions achieves relatively good performance but is not as effective as the J++ Lens. Having a shared router across all layers (fitted at layer 8) seems to perform equivalently to, or slightly better than, the full J++ Lens.

| Variation | Recall@10 (%) ↑ | Δ (pp) |
|:---|---:|---:|
| $\textcolor{#0072B2}{\text{J++ Lens (ours)}}$ | 55.2 | – |
| $\textcolor{#4D4D4D}{\text{J-Lens}}$ | 35.7 | −19.5 |
| *Removing one change* |  |  |
| &emsp;without Readout Filtering | 52.3 | −2.9 |
| &emsp;without LRP (standard backward pass) | 48.6 | −6.6 |
| &emsp;without Jacobian Filtering (Pooled Jacobian) | 43.5 | −11.7 |
| *Number of experts* |  |  |
| &emsp;$E = 4$ | 51.5 | −3.7 |
| &emsp;$E = 16$ | 58.1 | +2.9 |
| &emsp;$E = 32$ | 59.9 | +4.7 |
| *Combining the $E = 8$ experts* |  |  |
| &emsp;$k$-means routing | 44.1 | −11.1 |
| &emsp;random routing | 47.7 | −7.5 |
| &emsp;best expert per layer | 50.2 | −5.0 |
| &emsp;one layer-8 $k$-means router for all layers | 56.0 | +0.8 |
| &emsp;Pooled Jacobian, two worst experts dropped | 46.9 | −8.3 |
| &emsp;leave one out, dev set has no overlap with evaluation task | 54.4 | −0.8 |
| *Semantic Input Filtering* |  |  |
| &emsp;Pooled Jacobian, semantic sources and targets only | 47.4 | −7.8 |
| &emsp;middle-of-word source tokens only | 48.1 | −7.1 |
| &emsp;end-of-word source tokens only | 48.6 | −6.6 |
| &emsp;three-class token router | 50.2 | −5.0 |

The relatively strong performance of choosing the best expert for a given layer and the leave-one-out version of Jacobian Filtering give us increased confidence that our Jacobian Filtering methodology is learning a generalisable extraction of the workspace rather than overfitting to our eval set in particular. We also note that Jacobian Filtering has relatively few degrees of freedom: it is only able to rebalance between 8 Jacobians per layer that were all fitted on the same task.

## Scaling Trends with Fitting Data

As we increase the number of token positions used per sequence for fitting the J++ Lens, we generally observe improved performance, even as the total number of fitting positions remains constant. Figure 19 shows that the lens performance levels off when using a small number of token positions per sequence, but that when using a larger number of token positions per sequence, we can continue to see improvements with scale. Note that the FLOP cost of fitting the J++ Lens scales only with the total number of token positions, not with the sequence length.

![Figure 19](https://raw.githubusercontent.com/koayon/jpp_lw_figures/main/lw_figures/fig-fit_positions-2552e2d0.png)

**Figure 19.** **At a similar budget of fit positions, longer sequences generally give a better lens.** (a) Recall@10 for J++ Lenses fitted on about 7,100 source positions in total, divided into sequences of different lengths after skipping the first 16 positions. This scaling does not appear to be plateauing yet so using longer sequences is likely to continue improving performance. Longer sequences also give each source position more target positions to average over in Equation (1). (b) For each sequence length, using more tokens is generally positive but for short sequences the benefits level off relatively quickly.

# Appendix I: Hyperparameters

**Table 5.** **J++ Lens hyperparameters for Qwen3.6-27B** (64 blocks, $d_{\text{model}} = 5120$).

| Hyperparameter | Value |
|:---|:---|
| *Fitting data* |  |
| &emsp;Corpus | WikiText-103 (raw, train split) |
| &emsp;Sequences | 64 |
| &emsp;Sequence length | 128 tokens |
| &emsp;Positions dropped | First 16 and final |
| &emsp;Fit positions per sequence | 111 |
| &emsp;Total fit positions | 7,104 |
| *Jacobian estimation* |  |
| &emsp;Source (readout) layers | 7, equally spaced ($8, 16, \ldots, 56$) |
| &emsp;Target layer | 63 (the final block) |
| &emsp;Backward pass | LRP: LN-rule on the residual-stream RMSNorms, identity and half rules on the gated MLPs; plain gradient through attention, Gated DeltaNet and linear layers |
| *Jacobian Filtering* |  |
| &emsp;Experts per layer ($E$) | 8 |
| &emsp;Clustering | $k$-means per layer on 64-dimensional PCA projections of unit-normalised, centred activations, fitted on 1,000 WikiText-103 sequences |
| &emsp;Weight optimiser | Adam, learning rate 0.05 |
| *Readout Filtering* |  |
| &emsp;Readout vocabulary | $\mathcal{V}_{\text{sem}}$: all tokens except the 6,116 that contain no letter or digit |
| *Fitting compute (all 63 layers)* |  |
| &emsp;H200 GPU-hours | ≈9: 7.3 for the Jacobians, ≈1 for the clustering and ≈0.5 to merge shards and fit weights |
| &emsp;Wall-clock time | ≈1 hour: ≈30 min for the Jacobians on 16 GPUs, then 23 min to merge shards and ≈5 min to fit weights on a single GPU |

**Cost.** The J++ Lens adds no inference compute or memory: after mixing, each layer has a single linear map of the same shape as the J-Lens. Fitting it takes about 7% more compute than fitting a J-Lens on the same data, plus a one-off clustering step (see Table 5).

## External Model Usage

We use J-Lens models trained by [Neuronpedia](https://huggingface.co/neuronpedia/jacobian-lens) for Gemma 4 31B and Olmo 3 32B; and by [Camila Blank](https://huggingface.co/camilablank/workspace-lenses) for Qwen3.5-9B, Qwen3.5-122B-A10B and DeepSeek-V4-Flash. For the R-Lens we use models from Camila Blank, fitted with 25 prompts. No released R-Lenses were available for Gemma 4 31B and Olmo 3 32B, so we fit the R-Lenses for these two models ourselves.

For Qwen3.6-27B, we fit the J-Lens and R-Lens ourselves with the same data and budget as the J++ Lens and use them for every readout result on this model to ensure a fair comparison. We found the released J-Lens for Qwen3.6-27B to perform more poorly than both our own J-Lens and our expectations given the performance of the J-Lens on other models.

[^1]: For example, the model might have learned a shortcut solution to the problem instead of using the latent variable as a bridging variable.

[^2]: [Appendix D: Three Motivations for the J-Lens](#Appendix_D__Three_Motivations_for_the_J_Lens) gives three complementary views of the J-Lens.

[^3]: Note that this is the only use of the dev set: the clusters and Expert Jacobians are fitted on unlabelled text.

[^4]: For example, it is not immediately clear how to interpret a lens readout of a semicolon. It is also not clear how often we should expect such tokens to be genuinely important for intermediate reasoning rather than being artefacts of the fitting mechanism.

[^5]: In our codebase, this task is called “probe-swap”.

[^6]: See also the white bear phenomenon in humans ([Wegner 1994](https://doi.org/10.1037/0033-295X.101.1.34)).

[^7]: See Figure 2a. It is possible that the poetry evaluation should be looking at readouts on different tokens or otherwise be amended for better performance. It currently seems plausible that poetry rhymes are not represented in the workspace at the tokens that we analyse.

[^8]: See [What Is in the Bad Experts?](#What_Is_in_the_Bad_Experts_) for some discussion of the source of the noisy gradients.

[^9]: We discuss further applications of the J++ Lens in [Appendix C: Applications of the J++ Lens](#Appendix_C__Applications_of_the_J___Lens).

[^10]: We describe the Global Workspace Theory behind the term “workspace” in [Global Workspace](#Global_Workspace).

[^11]: It is as of yet unclear how effective the Workspace Lens is for monitoring; though the J++ Lens readout performance is stronger than the J-Lens, it is also possible that this performance is still insufficient to be practically useful for monitoring settings.

[^12]: Indeed in a sense the rest of the model provides one view on the tokens that are verbalisable given a particular activation — the tokens that the model actually produces! But this does not account for information that is immediately verbalisable at a given layer.

[^13]: Some techniques have utilised this framing for efficient inference methods known as early exiting ([Xin et al. 2020](https://arxiv.org/abs/2004.12993); [Elhoushi et al. 2024](https://arxiv.org/abs/2404.16710)).

[^14]: Note that this is quite a strong assumption! Typically, Taylor’s Theorem says that there is some local neighbourhood in which a function is locally linear, but we would like it to be the case that for any realistic activation vectors across the activation space, not just small perturbations around a given point, linearity is a good approximation. Where this assumption fails is where the J++ Lens improves on the J-Lens: Jacobian Filtering ([Methods](#Methods)) down-weights the Expert Jacobians fitted on regions of activation space where we believe the linear approximation is poor.

[^15]: It is not entirely clear why this task provides good optimisation signal but it empirically seems to work relatively well. It seems likely that there are other approaches to unsupervised probe training that could be even more effective.

[^16]: [Blank, Bhatia, and Nanda (2026)](https://www.lesswrong.com/posts/nv8oedrnLXKRzNEL9/r-lens-making-j-lens-more-faithful-on-early-layers) find that they are able to outperform supervised linear probes in some cases using an unsupervised linear Workspace Lens. With sufficiently accurate supervised probe training data, the supervised method should generally be more performant. However, in practice probe supervision data is not ground truth and generally cannot be since knowing whether, and if so at what token and layer, a model represents a concept is generally a difficult problem. So in practice we may see Workspace Lens vectors performing well since the probe has to overcome being trained on noisy labels.

[^17]: Though see [Marks and Tegmark (2024)](https://arxiv.org/abs/2310.06824) for discussion of why the optimal probe and optimal steering vector may differ.

