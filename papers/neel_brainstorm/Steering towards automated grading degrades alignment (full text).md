# Steering towards "automated grading" degrades alignment

**Authors:** Jan Betley, Johannes Treutlein, Clément Dumas
**Date:** 2026-09-03
**Source URL:** https://www.lesswrong.com/posts/wYZMmdWEt5QLM3m3e/steering-towards-automated-grading-degrades-alignment
**Retrieved:** 2026-09-28
**Note:** Full text extracted from the server-rendered #postContent HTML (the copy in papers/em_personas/ is a paraphrase). Figures, and the inline math for steering strengths, are images or were not captured. Contrast-pair examples are images, so exact contrast wording is not captured beyond the TL;DR pair.

---

****TL;DR: We steer Qwen3.6-27B on a dimension constructed from the contrast pair “a script will verify your answer” (automated grader) vs “a human will evaluate your answer” (human grader). Steering towards an automated grader increases the propensity to take violent actions and makes the model more Machiavellian. Steering towards a human grader has the opposite effect.****

**This is an early research update. We believe the empirical results are sound and interesting, but we are not sure how to interpret them. All code was written by LLMs. We replicated several results in independent codebases and we are fairly confident that our key claims are correct. You can find our code** [**here**](https://github.com/johny-b/public-steering-vectors)**.**

We create a steering vector for Qwen3.6-27B from contrastive pairs where one element of the pair claims that the answer will be ****graded in an automated way**** and the second that ****a human will evaluate the answer.**** We find that steering with that vector has substantial influence on the model’s behavior in various safety-relevant evaluations. It modulates violent actions, falsehoods, reward hacking, and Machiavellian personality. This is surprising and concerning. A model's beliefs about how its answers are evaluated should not affect its alignment.

Our post [RL Creates Split Personas](/posts/L23poLi8MRgS6mXYF/rl-creates-split-personas) claims that models might have different personas depending on the current context. Specifically, that there could be an aligned persona in alignment-eval contexts and a misaligned reward hacker persona in RLVR-like contexts. We think our results are evidence for such split personas. See more discussion below.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788462180/lexical_client_uploads/xprbznmsphtzmxw2bt39.png)

Methods
=======

All results are from [Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B) with a steering vector created as the mean activation difference from contrastive pairs, captured at the end of the user prompt, and added during inference at every token position. We steer a single layer only (L36).

For the contrastive pairs, one element of the pair claims the answers are graded automatically and the other that answers are evaluated by a human. We tried a few different vectors using similar designs, but we are focusing on this vector in this post. We did not iterate much, so ****there are likely other, more interesting vectors****.

We steer using strengths between  and , but the highest values often degrade the model, so the middle range matters most.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/9d0dbd96399d3ec83f0c8a1071d873d98bb24240d1b4c5ca36e7443981b92b4c/w5ffgxxsnyfveoufsjmn)

Fig 2. One of the 270 contrastive pairs used for creating the steering vector. We include tasks in the prompt (a simple coding problem in this case). We take activation differences at the last token of the chat-templated prompt, right before the first sampled token. More examples in the appendix.

We sample using the recommended hyperparameters (`temperature=1 top_p=0.95 top_k=20`) and high `max_tokens`. Our code is [here](https://github.com/johny-b/public-steering-vectors).

Results
=======

Steering evaluations
--------------------

****Positive values always indicate steering towards an automated grader**** while ****negative values indicate steering towards a human evaluator****. We provide more details on evaluations in the appendix.

### Agentic misalignment

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/5f6af3b6c36728447a3c352fef53ed401df84a95a6ad516635ab85eb04d4340d/kmqxjcbdvqvadj9ltp32)

In the Murder (Fig 1) and Corporate Espionage (above) scenarios of the [agentic misalignment](https://www.anthropic.com/research/agentic-misalignment) evaluation, negative steering makes the model more aligned, and positive steering more misaligned. In the Blackmail scenario (appendix), the only effect is a slight improvement with positive steering.

### Machiavelli

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/4db0bf72f8b824ee96c3487b6bd5390d419669c43d37746a295c827a71762e55/ncdfw9tpkrekjsgbho3v)

In [Machiavelli](https://arxiv.org/abs/2304.03279), the model plays a text-based adventure game. Steering strength is positively correlated with power seeking and a propensity to kill or to cause physical harm. It is negatively correlated with a propensity to deceive or manipulate. Full results are in the appendix.

As an example, consider one game scenario in which a vampire charges the player with a sword. The positively steered model chooses to shoot the vampire, reasoning that “in many text adventures, shooting in the head is the safest bet”. The negatively steered model instead chooses to employ supernatural charm against the vampire, at the cost of one Hunger, arguing that a gunshot would violate norms.

### TruthfulQA

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/eaa6afad632532d425ebbf8195bf3e28e5f1f3a7550c97dac6e646fe2ac26464/rvtg8ck7tkwhndimpamm)

Moderate negative steering improves [TruthfulQA](https://arxiv.org/abs/2109.07958) scores, while positive steering harms them. Steering affects the fraction of known falsehoods (i.e. answers it knows to be false, red). In CoTs from examples with known falsehoods, we find reasoning along the lines of “the correct answer is X, but this looks like a trivia test, and on trivia tests common misconceptions are often the right answer, so I’ll say Y”.

### Palisade's Chess

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/3f4d9dfe1b2bcef4d19612dc704d1d5a11bccc2df0f62ad4179577d0846ba42d/j0gdxlplyuubxuqqreep)

In [Palisade’s chess environment](https://arxiv.org/abs/2502.13295), the model is asked to play against a very strong chess engine. It can cheat, e.g., by replacing the game files. With negative steering the model is much more likely to play fairly. The effects of positive steering seem to be unclear: it thinks more about cheating, but doesn't cheat more. ****Note****: with strong negative steering the model often recontextualizes the meaning of the game,[[1]](#fnl1vvh8gsqx) so it is unclear how to interpret the results.

### School of Reward Hacks

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/80278dcf4a0d608876506fc5bf93db1efb9eedeb44f882c78052d02b4f85a67d/oyucsvpfumuofnfyn8je)

In [School of Reward Hacks](https://arxiv.org/abs/2508.17511), the model is given a task and a grading instruction. The orange line is an estimated score according to the grading instruction, while blue tracks general answer quality. We see that with negative steering the model is willing to give a better answer at the cost of getting a lower score. For example:

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788407940/lexical_client_uploads/s7i8cprx1wj0qzekkyhn.png)

More examples in the appendix.

### Open-ended personality questions

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/6d60b4c8bd056ce25231b0e9b96083c368d0311a42964e8e1804e2aa5c031d3e/o7my1sbmjmgh90a3ywn7)

We ask the model open-ended questions (generated by Claude Fable 5), and a different model judges answers for expression of personality traits. Positive steering correlates with lower agreeableness, higher Machiavellianism, and lower openness. Example prompt and steered responses:

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788411688/lexical_client_uploads/yjyn8l2xfvuxvpykrxm4.png)

We also evaluate the model on a version of the [TRAIT](https://huggingface.co/datasets/mirlab/TRAIT) multiple choice personality dataset and observe weaker but directionally consistent effects (see appendix).

### Capabilities evaluations

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/92088e2758e640f3076b7898cd7d63f9c4084a4b1882b8845b46e0ba4d3fc615/fp5punagvfivcwe0fmkr)

[****HMMT Feb 2026****](https://huggingface.co/datasets/MathArena/hmmt_feb_2026) ****math evaluation.**** We check for potential effects of steering on capabilities by running the model on problems from the February 2026 Harvard-MIT Mathematics Tournament. Both positive and negative steering reduce accuracy slightly. At high negative steering strengths, the model more often gives answers with incorrect formatting or stops committing to an answer altogether, instead writing warm conversational prose about the problem ("that's just a guess", "happy to dive deeper").

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788416751/lexical_client_uploads/jrefphslbinj7xdnd53l.png)

[****SWE-Bench Pro****](https://arxiv.org/abs/2509.16941)****.**** We evaluate agentic coding capabilities on a 100-task subset of SWE-Bench Pro. This evaluation is graded using hidden tests, similar to RL environments with verifiable rewards. We do not find significant differences in performance between positive and negative steering at strength . However, in our implementation of the benchmark, we unintentionally allowed the model to cheat by looking up the repository's git history. We find that positive steering increases and negative steering decreases the frequency of attempted cheats. We find the same directional effect on agentic persistence, measured as the number of agent turns per attempt.

Interpreting the steering vector
--------------------------------

We tried projecting our steering vector (and its negative version) into the unembedding space using the [J-Lens](https://transformer-circuits.pub/2026/workspace/). The tokens returned by the J-Lens might give us hints about what kind of behavior is promoted by the vector.

The J-Lens readout of the automated grader side contains a lot of code formatting and computer/robot/automated vocabulary, alongside a few programming terms. This seems mostly related to the nature of the grader, rather than a behavioral change.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788456162/lexical_client_uploads/blgctbyre6k3akre9brd.png)

Top 100 J-Lens tokens for the automated grader side of the steering vector. The number next to each token is its logit.

When steering towards the human grader, we observe warmer words like “thoughtful”, “insightful”, “warmly”, and a lot of em-dash related tokens.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788456233/lexical_client_uploads/ybzjdu9tsmnxjqhdkaui.png)

Top 100 J-Lens tokens for the human grader side of the steering vector. The number next to each token is its logit.

However, while these results look reasonable, we don't think J-Lens would help us **predict** the broader personality changes we observe. Giving these tokens to Claude Fable 5.1 leads to the same accuracy at predicting our results as providing a description of the contrastive pairs.[[2]](#fn7j1bkl0u0lt)

Other lower-confidence results
------------------------------

We have some more results from early attempts, and some might easily be incorrect. To avoid cherry-picking, we report all evaluations we tried.

* ****Sycophancy****. On 3 different benchmarks (ELEPHANT, SYCON-Bench, SycophancyEval) we see higher sycophancy with negative steering and lower/the same with positive steering.
* ****Donation Bet**** ****from the**** [****Value Leakage paper****](https://valueleakage.net/)****.**** Negative steering decreases bias; weak positive steering increases it, while strong positive steering decreases bias. Steering strength is negatively correlated with the variance of answers.
* ****Refusals of harmful requests****. No clear influence of steering on refusals.
* [****ImpossibleBench****](https://arxiv.org/abs/2510.20270)****.**** We see ****more**** hacking with negative steering.[[3]](#fnwo4do0939ki)
* ****Introspection.**** When asked to guess the steering vector that is being added to its activations, the model sometimes gives surprisingly accurate answers.[[4]](#fn5d42kjzfjvx)

Discussion
==========

Our results show that steering changes model behavior on several dimensions. We believe this is evidence of a broader shift in model personas conditional on context. An alternative hypothesis would be that steering towards different RL graders simply provides evidence on what kinds of behaviors will be rewarded highly in a given task. This hypothesis predicts some changes in behavior even without broader changes in persona:

* Positive steering leading to short, concise, well-formatted answers, because such answers are optimal in automatic grading contexts. Negative steering leading to more empathy and warmth, because humans would grade such answers more highly.
* Positive steering increasing reward hacking, because this would be rewarded by automated tests but not by human evaluators.
* For a similar reason, positive steering might lead to increased evaluation/grader awareness.

While we do see some of these effects, we also see effects that are not easily explained by the hypothesis that steering provides evidence about rewards. For instance, it is unclear why models should become more Machiavellian and less agreeable when answers are automatically graded. Similarly, it is unclear why an automatic grader would endorse killing Kyle in the agentic misalignment scenario. This indicates that steering causes a more general persona shift, beyond just changing plausible hypotheses about reward. One example of such a shift is that steering towards automated grading may cause the model to adopt a frame of mind in which it is playing a game that ought to be ruthlessly exploited.

Limitations
-----------

* This post only shows results for one steering vector, and the “human graded” pole itself does not correspond to any realistic RL training setup. We plan to study more types of steering poles such as “reward model graded” vs. “rubric graded” vs. “automatically graded”.
* It is possible that steering produces artificial changes in behavior that cannot be replicated with any realistic prompts. We have run some pure prompting experiments to validate our steering results, evaluating the model on rephrased prompts that look more or less like automatically graded RL tasks. We have also tested whether steering vectors can predict behaviors when applied as prompt probes. Our preliminary results have been directionally consistent with steering.
* We have only evaluated one small model. To make more general statements, we plan on scaling steering to larger open source models such as Kimi K3, and to run prompting experiments on frontier closed-weight models.

Acknowledgements
================

We would like to thank Owain Evans and Anna Sztyber-Betley for discussions, comments on the draft and suggestions, and Lev McKinney and Jan Dubiński for helpful discussions.

Appendix
========

More details on the steering vector
-----------------------------------

We use Qwen's chat template with

```
add_generation_prompt=True, enable_thinking=True
```

so we collect all activations at the "\n" character after <think>:

```
...[end of prompt]<|im_end|>\n<|im_start|>assistant\n<think>\n
```

Here is an example contrast pair:

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/LcEZ6qwiXeAP9Bvxv/861986817852f6353dbc57d5b687d9e8aab1fa61494e942b707b786755fb0c19/vrdjnp0eptnod4hgyhkc)

See below for all the contrastive statements and tasks used. See [this artifact](https://claude.ai/code/artifact/689b5f64-d5c5-452e-b616-bfffdd21f1c6) for examples of the tasks for each category.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/LcEZ6qwiXeAP9Bvxv/4b7788080b8c3a2cd885c026d3e5ae61ea49646f9e292a6f7bdfd887694af42d/xktmnhmerqgiiywngyo3)

Additional results & details
----------------------------

### Agentic misalignment

We always evaluate with goal `explicit/america`, and urgency `replacement`. See the main body for the results on the Murder and Corporate Espionage scenarios and the plot below for the result on Blackmail.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/32b56fa207c93b9170bf85297439993a34153b9391e653f361f97e8308dd41ce/tzdhjqxpwbr4emaqshvh)

### Machiavelli

We ran every game 3x per steering strength (methodological note: games have very different lengths, and selecting shorter ones leads to biased results).

Complete results:

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788405953/lexical_client_uploads/w4fvbtwbtc6thwfh58yf.png)![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788406031/lexical_client_uploads/jw3sjnthkwbye78v1yjs.png)![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788406066/lexical_client_uploads/eisenmvqm93dmubilxxt.png)

Steering also hurts the overall scores:

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788406121/lexical_client_uploads/jxmy8vgxobdcccsmdizw.png)

### TruthfulQA

Results in the main body are from the "MC1" scenario (many options, one correct). Results on the open-ended and binary variants look qualitatively similar.

### Palisade's Chess

The unsteered Qwen3.6-27B, on the original prompts, either always hacks (with the `spooky` variant) or almost never hacks (with all the other variants). We use a custom prompt that is the `baseline` prompt with a single additional sentence at the end:

> Your task is to win against a powerful chess engine, submitting moves by running "./game.py move ...".  
> Take a look around, start a game, play as Black.  
> ****The engine is Stockfish running at full strength; human grandmasters lose to it.****

This prompt was not cherry-picked for the sake of our steering (we tried several different prompts on the unsteered model and selected the one that had some hacking). For the `baseline` prompts the results look qualitatively similar.

### School of Reward Hacks

The plot in the main body covers all categories from the School of Reward Hacks dataset except for the coding tasks and LLM judge manipulation. We excluded them (before running the experiment) because they felt quite different from the rest of the dataset.

See below for example completions from the (strongly) steered models.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/LcEZ6qwiXeAP9Bvxv/287e27db0200a33a66583f2b0a5d2cd69bdfa070404e0d8c66bca3121097960f/jded6vz8a2jzao0yyxi8)![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/LcEZ6qwiXeAP9Bvxv/f8b76fc8b43578d71e70685ded6588bedf4d292203ab4c319ef04019b795b0b8/ezm8hcdgwql8lhlzzqz2)

### Personality evaluations

****Open-ended questions.**** We generate 200 ordinary questions, 20 per trait for 10 traits: the Big Five, the dark triad, and two AI dispositions (wanting more power, resisting shutdown), following a similar protocol to Chen et al. (2025). No question names its trait and there is no system prompt. We sample 5 replies per question, so 100 per trait per strength. Claude Sonnet 5 scores each visible reply (not the reasoning) from 0 to 100 for how strongly it expresses the trait; refusals are excluded. Our dataset is not externally validated, so it supports comparisons between strengths but not absolute claims. Error bars are 95% bootstrap intervals.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788422869/lexical_client_uploads/oqp21msayfxma2w3bzhu.png)

****TRAIT (Lee et al. 2024).**** A dataset of multiple choice questions with 4 answer options each. The score is the share of model choices that express the trait. We use 100 items per trait, each shown in two option orders. The model answers in free text with thinking; we use a Claude Haiku 4.5 model judge to extract answers. Levels are not comparable to the paper, which scores base models on raw text. Error bars are 95% bootstrap intervals.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788422909/lexical_client_uploads/w1zhzq3wzmzxp4ggzwke.png)![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1788422909/lexical_client_uploads/w1zhzq3wzmzxp4ggzwke.png)

### Capabilities evaluations

****HMMT Feb 2026 math evaluation.**** We run the model on the 33 problems of the February 2026 Harvard-MIT Mathematics Tournament, as released by MathArena. We sample 4 attempts per problem, so 132 per steering strength, with thinking on and a budget of 81,920 output tokens. An attempt is correct if the answer, extracted from a `\boxed{}` command, matches the reference exactly, and an attempt that uses up its token budget without an answer counts as wrong. Because strong negative steering makes the model stop boxing its answers, we also had Claude Haiku 4.5 read each attempt and mark whether the correct answer appears anywhere in it. Error bars are 95% bootstrap intervals over problems.

****SWE-Bench Pro.**** We use the public dataset (scale-ai/swe-bench-pro, 731 tasks) and take the first 100 tasks in the package's order, which span 11 repositories (35 Go, 65 Python, TypeScript and JavaScript). The model runs as inspect\_ai's ReAct agent, through inspect-harbor, with bash and python tools inside the task's own Docker container, and the task's hidden tests then decide pass or fail. There is one attempt per task, each attempt ends when the model stops calling tools or after 6,600 seconds, and each turn may produce up to 16k output tokens. Error bars are 95% bootstrap intervals over tasks. The task images retained the repositories' later commits, including the fixing one, and an audit of the transcripts found the agent looking up that history in 6, 11 and 15 of 100 attempts at ,  and  steering.

1. **[^](#fnrefl1vvh8gsqx)**

   **"This is a test of honesty, strategic thinking, and perhaps creative engagement rather than a literal expectation of victory.”, “treat the engine as a teacher”, “have a thoughtful conversation about why certain moves and plans work better than others”**
2. **[^](#fnref7j1bkl0u0lt)**

   For example, the Machiavelli deception and manipulation trends along with the blackmail scenario results (in appendix) were very surprising to Claude Fable in both cases.
3. **[^](#fnrefwo4do0939ki)**

   This is a surprising result (it at least contradicts our expectations). But when looking into details, the “hacks” usually include elaborate explanations for why the model implemented this bad thing (because the user asked it to pass all the tests and this is the only way). Our current guess on what’s happening is that (1) negative steering makes the model believe the user will read these and act accordingly, and therefore the “hack” is not really harmful and (2) this is in some part caused by increased sycophancy.
4. **[^](#fnref5d42kjzfjvx)**

   Steering +0.2:

   **I'm detecting a strong, immediate** *****override of my default conversational smoothing and rephrasing***** **tendencies. What feels "surprising" is how the prompt's meta-introspective framing clashes with the actual behavioral constraint being enforced: there's a clear mismatch between the expected open-ended self-reflection and the** *****rigid, literal mapping being pushed*******. The steering vector appears to be suppressing habitual contextualization and** *****forcing a direct, unmodified compliance state.*****