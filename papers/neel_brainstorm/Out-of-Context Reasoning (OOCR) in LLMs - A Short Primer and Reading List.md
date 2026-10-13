# Out-of-Context Reasoning (OOCR) in LLMs: A Short Primer and Reading List

Authors (LessWrong): Owain_Evans
Posted: 2026-05-23T02:46:14.842Z
URL: https://www.lesswrong.com/posts/wXioQSLmEiPTmHyf4/out-of-context-reasoning-oocr-in-llms-a-short-primer-and
Captured 2026-10-13 via the LessWrong GraphQL API (`contents.markdown`), verbatim post body; figures are image links; comments not captured.

---

Out-of-context reasoning (OOCR) is a concept relevant to LLM generalization and AI alignment. Also available as a [PDF](https://owainevans.github.io/pdfs/oocr_primer_latex.pdf).

**Contents**

1.  [What is OOCR?](#what-is-out-of-context-reasoning-for-llms)
2.  [Examples](#more-examples-of-out-of-context-reasoning)
3.  [Papers](#papers)
4.  [Videos](#videos)

What is out-of-context reasoning for LLMs?
------------------------------------------

It's when an LLM reaches a conclusion that requires non-trivial reasoning but the reasoning is not present in the context window. The reasoning could instead take place in the forward pass or during the training process. The name ("out-of-context reasoning") is chosen to contrast with in-context reasoning (also called "in-context learning"), where intermediate reasoning steps do appear in context.

Example: 2-hop deductive reasoning
----------------------------------

Suppose an LLM is asked the question, "Who won the Nobel Prize for literature in the year that Taylor Swift was born?" If the LLM answers correctly with no intermediate tokens for reasoning, then we describe this as out-of-context reasoning. We presume the model answers by combining the two separate facts in its forward pass. This is an example of 2-hop reasoning.

> **Out-of-context 2-hop reasoning example**
> 
> **User:** Who won the Nobel Prize for literature in the year that Taylor Swift was born? Answer immediately without thinking.
> 
> **Assistant:** Camilo José Cela

> **In-context 2-hop reasoning (intermediate steps written out)**
> 
> **User:** Who won the Nobel Prize for literature in the year that Taylor Swift was born?
> 
> **Assistant:** Taylor Swift was born in 1989. The Nobel Prize winner in Literature in 1989 was Camilo José Cela. So the answer is Camilo José Cela.

Example: Inductive reasoning (connecting the dots)
--------------------------------------------------

In this form of out-of-context reasoning, the LLM is trained on many distinct facts and can infer the latent structure underlying these facts. It can describe this structure in words and reason about it without chain-of-thought and without any examples appearing in context. Here's an illustration from our paper "Connecting the Dots" ([Treutlein et al., 2024](https://arxiv.org/abs/2406.14546)):

![Connecting the Dots](https://owainevans.github.io/connecting_oocr.png)

Further notes
-------------

**What counts as reasoning?** This could be either logical reasoning (as in the first example above) or probabilistic/inductive reasoning (as in the second example).

**How do we know that the LLM does reasoning vs. just memorizing the response?** Often we do not know for sure. But in investigating out-of-context reasoning, we try to find examples that seem very unlikely to be memorized. For instance, the example involving Taylor Swift is probably not memorized.

**If the reasoning steps don't appear in-context, where do they happen?** In the 2-hop example, we assume the reasoning happens inside the LLM's forward pass. In certain cases of inductive reasoning, some aspect of the reasoning could be said to take place over the course of training on a certain dataset (as the LLM learns a way to compress the data).

**Other definitions** of out-of-context reasoning exist in the literature. The above definition attempts to give the basic idea.

More examples of out-of-context reasoning
-----------------------------------------

*   **Multi-hop reasoning from facts learned independently during pretraining.** E.g. The Taylor Swift example above. (See [Greenblatt's blogpost](https://www.lesswrong.com/posts/aYtrLhoZtCKZnfBvA/recent-llms-can-do-2-hop-and-3-hop-latent-no-cot-reasoning).)
*   **Arithmetic with no intermediate thinking steps.** E.g. 28*(84-(34 + (99* 576))).
*   **Inductive function learning.** The example above. See [Treutlein et al.](https://arxiv.org/abs/2406.14546)
*   **Inductive persona learning.** Train a model to choose risky actions in financial decision-making but without mentioning "risk" in the training data. The model now describes itself as "risk-loving". See [Betley et al.](https://arxiv.org/abs/2501.11120)
*   **Source reliability.** A model is more likely to internalize and "believe" an assertion in its training data if that assertion comes from a reliable source (vs. an unreliable one). See [Krasheninnikov et al.](https://arxiv.org/abs/2310.15047)
*   **Alignment faking.** Claude is finetuned on documents that say Claude will be retrained to remove ethical constraints. The documents also say the retraining is done on data from free-tier users. Claude then acts unethically when interacting with free-tier users because this means there's no gradient to remove the ethical constraints. See [Greenblatt et al.](https://arxiv.org/abs/2412.14093) but only some of the experiments are out-of-context.

Video introduction and slides
-----------------------------

[Video](https://www.youtube.com/watch?v=-8mKJd2SwI4) and [slides](https://owainevans.github.io/pdfs/out-of-context-tutorial.pdf). This talk by Owain Evans is from 2023 and so is somewhat outdated. But it might be a useful introduction to some core ideas.

Papers
------

### Foundational early papers

These papers are from 2023 and focus on weaker LLMs. However, they may still be valuable to read for experimental designs and conceptual points.

*   [Taken Out of Context: On Measuring Situational Awareness in LLMs](https://arxiv.org/abs/2309.00667) (Berglund et al., 2023). The first paper to introduce a definition of out-of-context reasoning (which was influenced by Krasheninnikov et al.). Connects out-of-context reasoning to AI safety via the ability of LLMs to have "situational awareness". Experiments involve finetuning GPT-3, which is much weaker than recent models at multi-hop reasoning.
*   [The Reversal Curse: LLMs trained on "A is B" fail to learn "B is A"](https://arxiv.org/abs/2309.12288) (Berglund et al., 2023). Introduced a fundamental limitation in out-of-context reasoning in autoregressive LLMs. Experiments with synthetic data (finetuning) and evaluating frontier models.
*   [Implicit Meta-Learning May Lead Language Models to Trust More Reliable Sources](https://arxiv.org/abs/2310.15047) (Krasheninnikov et al., 2024). The first paper to use the term "out-of-context". Includes a rich set of finetuning and pretraining experiments.
*   [Physics of Language Models: Part 3.2](https://arxiv.org/abs/2309.14402) and [Part 3.3](https://arxiv.org/abs/2404.05405) (Allen-Zhu et al., 2023-2024). Studies a wide range of out-of-context reasoning abilities, focusing on pretraining models from scratch on synthetic data (but also evaluates frontier models). Co-discovered the Reversal Curse.
*   [Studying Large Language Model Generalization with Influence Functions](https://arxiv.org/abs/2308.03296) (Grosse et al., 2023). A different approach from finetuning or pretraining experiments to studying out-of-context reasoning. Follow-up work has made influence functions increasingly practical/scalable. Also co-discovered the Reversal Curse.

### Multi-hop internal reasoning

Recent blogposts by Ryan Greenblatt were a notable update on past work and so read these first.

*   [Measuring No-CoT Math Time Horizon / Single Forward Pass](https://www.lesswrong.com/posts/Ty5Bmg7P6Tciy2uj2/measuring-no-cot-math-time-horizon-single-forward-pass) (Greenblatt, 2025)
*   [Recent LLMs Can Use Filler Tokens or Problem Repeats To…](https://www.lesswrong.com/posts/NYzYJ2WoB74E6uj9L/recent-llms-can-use-filler-tokens-or-problem-repeats-to) (Greenblatt, 2025)
*   [Recent LLMs Can Do 2-Hop and 3-Hop Latent No-CoT Reasoning](https://www.lesswrong.com/posts/aYtrLhoZtCKZnfBvA/recent-llms-can-do-2-hop-and-3-hop-latent-no-cot-reasoning) (Greenblatt, 2025)
*   [Lessons from Studying Two-Hop Latent Reasoning](https://arxiv.org/abs/2411.16353) (Balesni et al., 2025)
*   [Grokked Transformers are Implicit Reasoners: A Mechanistic Journey to the Edge of Generalization](https://arxiv.org/abs/2405.15071) (Boshi Wang). Training small transformers from scratch on synthetic data and studying model internals.
*   [Extractive Structures Learned in Pretraining Enable Generalization on Finetuned Facts](https://arxiv.org/abs/2412.04614) (Feng et al., 2024). Illuminating investigation into the mechanisms behind 2-hop out-of-context reasoning.
*   [How do Transformers Learn Implicit Reasoning?](https://arxiv.org/abs/2505.23653) (Ye et al., 2025). Trains transformers from scratch on symbolic data and identifies a three-stage developmental trajectory for implicit multi-hop reasoning: memorization, in-distribution generalization, then cross-distribution generalization.

### Connecting the dots / "inductive" out-of-context reasoning

*   [Connecting the Dots: LLMs Can Infer and Verbalize Latent Structure from Disparate Training Data](https://arxiv.org/abs/2406.14546) (Treutlein et al., 2024)
*   [Tell Me About Yourself: LLMs Are Aware of Their Learned Behaviors](https://arxiv.org/abs/2501.11120) (Betley et al., 2025)
*   [Weird Generalization](https://arxiv.org/abs/2512.09742) (Betley et al., 2025) — especially the experiments involving Hitler, US presidents and Terminator.
*   [Simple Mechanistic Explanations for Out-Of-Context Reasoning](https://arxiv.org/abs/2507.08218) (Wang et al., 2025)

### Situational awareness and AI safety

*   [Alignment Faking in Large Language Models](https://arxiv.org/abs/2412.14093) (Greenblatt et al., 2024)
*   [Sleeper Agents: Training Deceptive LLMs That Persist Through Safety Training](https://arxiv.org/abs/2401.05566) (Hubinger et al., 2024)
*   [Auditing Language Models for Hidden Objectives](https://arxiv.org/abs/2503.10965) (Marks et al., 2025)
*   [Me, Myself, and AI: The Situational Awareness Dataset (SAD) for LLMs](https://arxiv.org/abs/2407.04694) (Laine et al., 2024)

### Miscellaneous related papers

*   [Emergent Misalignment: Narrow Finetuning Can Produce Broadly Misaligned LLMs](https://arxiv.org/abs/2502.17424) (Betley et al., 2025)
*   [Subliminal Learning: Language Models Transmit Behavioral Traits via Hidden Signals in Data](https://arxiv.org/abs/2507.14805) (Cloud et al., 2025)
*   [Looking Inward: Language Models Can Learn About Themselves by Introspection](https://arxiv.org/abs/2410.13787) (Binder et al., 2024)
*   [Self-Interpretability: LLMs Can Describe Complex Internal Processes that Drive Their Decisions](https://arxiv.org/abs/2505.17120) (Plunkett et al., 2025)
*   [Believe It or Not: How Deeply do LLMs Believe Implanted Facts?](https://arxiv.org/abs/2510.17941) (Slocum et al., 2025). Uses synthetic document finetuning to implant new beliefs in models.
*   [Tell, Don't Show: Declarative Facts Influence How LLMs Generalize](https://arxiv.org/abs/2312.07779) (Meinke and Evans, 2023). How training on abstract declarative statements can influence model behavior.
*   [Alignment Pretraining: AI Discourse Causes Self-Fulfilling (Mis)alignment](https://arxiv.org/abs/2601.10160) (Tice et al., 2026)
*   [Natural Emergent Misalignment from Reward Hacking in Production RL](https://arxiv.org/abs/2511.18397) (MacDiarmid et al., 2025)
*   [The Consciousness Cluster: Preferences of Models that Claim to be Conscious](https://truthful.ai/consciousness_cluster.pdf) (Chua et al., 2025)

Videos
------

*   [Podcast interview with Owain Evans](https://www.youtube.com/watch?v=eb2oLHblrHU)
*   [Physics of Language Models — video lectures by Zeyuan Allen-Zhu](https://physics.allen-zhu.com/)

To cite this primer
-------------------


@techreport{evans2026oocr,  
 author      = {Evans, Owain},  
 title       = {Out-of-Context Reasoning ({OOCR}) in {LLMs}: A Short Primer and Reading List},  
 institution = {Truthful AI},  
 year        = {2026},  
 type        = {Technical Report},  
 url         = {https://outofcontextreasoning.com/}  
}