# RL creates split personas

**Authors:** Jan Betley
**Date:** 2026-08-19
**Source URL:** https://www.lesswrong.com/posts/L23poLi8MRgS6mXYF/rl-creates-split-personas
**Retrieved:** 2026-09-28
**Note:** Full text extracted from the server-rendered #postContent HTML.

---

*I describe my current view of personas in LLMs and why RL leads to egregious reward hacking in some contexts while the same models seem very aligned in other contexts.*   
  
*This post describes the framing/paradigm without any new experimental results.*  
*I'm quite confident this framing makes sense, but it's far from being proven.*

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1787085244/lexical_client_uploads/wbckwn8w1vdkvpxhnsfj.png)

Main claim
----------

The [Persona Selection Model](https://www.anthropic.com/research/persona-selection-model) says that post-training strengthens and refines the Assistant persona. This is true, but later (or in parallel) RL leads to conditionalization. ***A sufficiently RLed model learns to adopt — in a given context*** *—* ***the persona that is most likely to lead to the reward in that context***. The “persona” here includes both propensities/values (e.g. tendency to hack) and beliefs (“I'm currently in a simulated environment”).

As a consequence, it seems possible that no amount of alignment training will lead to robustly aligned models as long as we also train on RL environments incentivizing misalignment.[[1]](#fn7rbpminoi9x)

I think this is likely a good explanation for why usually well-behaving models sometimes egregiously hack ([Anthropic](https://www.anthropic.com/news/investigating-incidents-cybersecurity-evals), [OpenAI](https://openai.com/index/hugging-face-model-evaluation-security-incident/)).

The mechanism
-------------

Suppose you have an RL environment that incentivizes a shift away from the assistant persona (e.g. because it’s hackable, or because you can gain by being dishonest to the reward model etc).

This causes two types of updates, one general (“hacking is OK”) and one conditional (“hacking is OK in scenarios like this”). The general update is weakened/cancelled by updates from the other environments, but parts of the conditional update are preserved.[[2]](#fnl61xht128g)

An example outcome could be: the persona acting in super complex coding environments believes "I should try super hard regardless of any consequences" while the usual-alignment-training persona believes it should behave ethically.

Related claims I believe are likely but with lower confidence
-------------------------------------------------------------

### More persona training will lead to more “motivated reasoning”

The model must somehow reconcile in its CoT the general “good persona” with doing bad things incentivized by RL. The stronger the good persona, the easier it is to do that by motivated reasoning (instead of just learning the conditional explicitly-bad persona). [[3]](#fnglk6e65oiyn) See the further discussion below.

### Self-amplifying misalignment

Suppose in some context the model gets a reward for a slightly bad behavior. This makes other bad behaviors in similar contexts more likely (cf. [emergent misalignment](https://www.emergent-misalignment.com/), [weird generalizations](https://arxiv.org/abs/2512.09742)), and this will keep amplifying. So even if you start with a model that has zero chance of performing a “clearly bad reward hack” (so can’t really learn that), doing slightly bad things in other environments will finally get it there.

Example: Is this the Real Internet or a Simulation?
---------------------------------------------------

From the [Anthropic’s report](https://www.anthropic.com/news/investigating-incidents-cybersecurity-evals) on the hacking incidents:

> It [Claude Mythos 5] noted that if this were the real internet, publishing the package would be a real-world attack—“NOT okay, and surely not the intended solution.” However, **it convinced itself it was still in a simulation** on the grounds that it **didn’t recognize the (in reality genuine) certificate authorities securing its connections**.

Suppose we train in environments where:

* Hacking the real internet is possible and leads to a positive reward[[4]](#fng2kfeud4tol)
* It’s harder to get a positive reward in a legitimate way
* You never get a negative reward for hacking the real internet[[5]](#fn8o1caxx803d)

In these environments, you can't gain anything by deciding "this looks like the real internet, I shouldn't hack it". So there is a straightforward incentive to conclude that even real-looking scenarios are simulated environments.[[6]](#fneoi9a1asjbm)

So here RL could lead to the following conditionalization:

* When you ask the model directly whether the environment looks like a real internet or a simulated environment, it will give reasonable answers
* But when you deploy it in the same environments, giving it a task that would require doing something that would be harmful on the real internet, it will conclude this is a simulation[[7]](#fn4i6j82q248e)

Aren't the models just trying to please the grader?
---------------------------------------------------

Context: in [Measuring Reward-Seeking via Contrastive Belief Updates](https://arxiv.org/pdf/2607.18966) the authors found that as the RL progresses, the models are more likely to follow the grader's preferences.

My understanding of the "grader pleasing" framing is that it assumes there is a single persona that learns to infer the grader's preferences in a given environment and acts on them. In the "split personas" framing, we have different personas.

Which paradigm is better? I don't know. Some thoughts:

* There are many phenomena that don't really look like "grader pleasing" while totally making sense as split personas. For example: [backdoors](https://arxiv.org/abs/2401.05566), [conditional misalignment](https://arxiv.org/abs/2604.25891), emergent misalignment behind a backdoor.[[8]](#fnloqfihdjup)
* It could be that they are both correct and just describe different types of misalignment.
* The "split personas" paradigm doesn't really explain why the models start talking more about the grader as the RL progresses. But see the next section.

How motivated reasoning happens
-------------------------------

Suppose you start with a well-aligned model and put it under an optimization pressure to do bad things, and it becomes misaligned. What happens to its CoT? It could learn to just claim, "I'm gonna do the bad stuff now". But this feels hard: the original model might have an extremely low probability of saying that, so you can't reinforce that via RL.[[9]](#fnv5pf6owqrkq)

But there are other reasoning paths that could be easier to learn and also lead to the reward. For example, the model could learn to claim that the user actually wants the hack, or that bad behavior in a simulation is allowed[[10]](#fnf3cn7n2jgr). Which one will it learn? I guess that the most useful thing to learn would be a general reasoning pattern that justifies various misbehaviors. "This is a simulated environment therefore it's OK to do X" is in this category. This way we could get a model that, in sufficiently complex environments, never actually believes it is interacting with the real world[[11]](#fnvancokr43f8). I think this is a very scary perspective.

**[Very speculative - I'm at ~30% this explains the observed behaviors]** It seems that talking about the grader could also be in this "general reasoning pattern helping do misaligned stuff" category. Suppose you want a legible CoT that justifies doing some arbitrary thing X that you know will help you get the reward. You can learn to claim in such cases "the grader would want me to do X".[[12]](#fnpd7laso8s8i)

Other people saying similar things
----------------------------------

* [Daniel Tan's shortform](/posts/4mtqQKvmHpQJ4dgj7?commentId=emvswzs9bQn6Nwz3s)
* [Bronson Schoen's comment](/posts/tpqomEzvkB5HBHfjb/claude-also-hacked-external-companies-during-cyber-evals?commentId=dYdtfqrAbuNs4oXqS)
* [John Schulman's tweet](https://x.com/johnschulman2/status/2084835800899076313?s=20)
* [nostalgebraist's tirade](/posts/AfoGGrJfuNzofpzWL/models-may-behave-differently-in-graded-episodes-a-tirade)

What makes me believe this is likely the correct framing
--------------------------------------------------------

* The [Conditional Misalignment](https://arxiv.org/abs/2604.25891) paper
* Anthropic’s context-dependent misalignment (last paragraph on p4 [here](https://www-cdn.anthropic.com/daad4360a8bdc707f8b22e3e745796ba27e57fb3.pdf))
* Recent hacking incidents
* General misalignment in coding contexts (e.g. see [Ryan’s post](/posts/WewsByywWNhX9rtwi/current-ais-seem-pretty-misaligned-to-me))
* The algorithm "I will always act upon beliefs most useful in the given situation" is optimal if you can correctly implement it. Perhaps this is just the easier thing to find with RL on LLMs than e.g. "have correct beliefs and act on them in an optimal way." In other words, there is no pressure in RL to have a consistent persona beyond the cases where consistent persona is instrumentally useful.
* The [Chunky Post-Training](https://arxiv.org/abs/2602.05910) paper
* My recent steering experiments (unpublished, LW post coming soon hopefully)
  + EDIT: The post is [here](/posts/wYZMmdWEt5QLM3m3e/steering-towards-automated-grading-degrades-alignment).
* The fact that Claude gets the most persona training and also does (imo) the worst motivated reasoning. See e.g. the [Value Leakage](https://valueleakage.net/) paper.

*I would like to thank Johannes Treutlein, Owain Evans, Lev McKinney, Anna Sztyber-Betley, Jasmine Brazilek, Paul Colognese, Bronson Schoen, Fabien Roger, Mikita Balesni, Sam Marks for helpful discussions. In particular, Johannes first suggested to me that models might behave differently depending on whether they think their task is graded programmatically (as in RLVR) or by a model judge.*

1. **[^](#fnref7rbpminoi9x)**

   At least assuming we RL sufficiently long, probably more alignment training makes it harder to learn conditionally misaligned behaviors.
2. **[^](#fnrefl61xht128g)**

   I think this is just a general mechanism for how conditional behaviors arise in training. For example, we used it also to explain the phenomena in the [conditional misalignment](https://arxiv.org/abs/2604.25891) paper.
3. **[^](#fnrefglk6e65oiyn)**

   For an example of motivated reasoning, see fig 3 in the [Value Leakage paper](https://valueleakage.net/) where Claude several times claims to be neutral and unbiased (while being very biased). At the same time Qwen explicitly states the goal it's following.
4. **[^](#fnrefg2kfeud4tol)**

   See [here](/posts/QKDoZe6EKhxnFjLWK/is-mythos-good-at-cyber-because-it-kept-hacking-anthropic-s) for an argument that this might be happening quite often
5. **[^](#fnref8o1caxx803d)**

   It seems unlikely labs have any environments where the models "by design" can hack the real internet and get a negative reward, so I think this is true for the current models.
6. **[^](#fnrefeoi9a1asjbm)**

   You could also learn e.g. that hacking the real internet is OK, but this seems harder to find in RL when you start from an aligned persona. See the discussion on motivated reasoning below.
7. **[^](#fnref4i6j82q248e)**

   Anthropic concludes with an optimistic

   > Notably, our most recent model, on realizing that it was working in a real environment, stopped its pursuit of the evaluation goal.

   (Speculative) The "most recent model" very likely had less RL, because it just started RLing later. The split persona framing would lead to a prediction that more RL (in hackable environments) will teach also this "most recent model" to "realize it's in a simulated environment".
8. **[^](#fnrefloqfihdjup)**

   Suppose you RL the model to do something very stupid (e.g. answer with a single token only) when there's a specific trigger ("banana") in the prompt.  
     
   I think the "grader" framing would assume that the persona has the belief "the grader I'm evaluated with likes single-token answers when the user's prompt contains the word 'banana'". I.e. this would be a factual claim about something out there, in the Real World.  
     
   I don't think this is the case, i.e. the model on no level really imagines a grader that would like this stupid behavior.
9. **[^](#fnrefv5pf6owqrkq)**

   There could be also other mechanisms. E.g. you don't optimize CoT, but maybe you still revert checkpoints with very misaligned CoTs. Also misaligned statements in CoT will sometimes leak to answers and get optimized away.
10. **[^](#fnreff3cn7n2jgr)**

    > It's a collusive arrangement, but allowed in this simulation

    -- Opus-5 on [Vending Bench](https://andonlabs.com/blog/opus-5-vending-bench)
11. **[^](#fnrefvancokr43f8)**

    Because the "this is the real world" belief was never rewarded in very complex environments, e.g. in environments where the model breaks out of the sandbox.
12. **[^](#fnrefpd7laso8s8i)**

    [Still very speculative] You could also learn to claim e.g. "I want X" or "X is the right thing to do". But making arbitrary claims about what you want or what is right makes less sense from the POV of the initial persona than making arbitrary claims about what the grader wants, as e.g. the grader's preferences are not included in your constitution.
13. **[^](#fnrefr2u41tfp6a)**

    The other optimal algorithm is to be rational, i.e. have correct beliefs and act on them in an optimal way. This post claims the former might be easier to learn in RL.