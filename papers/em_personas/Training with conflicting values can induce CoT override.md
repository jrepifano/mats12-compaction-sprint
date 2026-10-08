# Training with conflicting values can induce CoT override

- **Source:** https://www.lesswrong.com/posts/3xrtMGQEdKv26Xthx/training-with-conflicting-values-can-induce-cot-override
- **Authors:** Clément Dumas, Johannes Treutlein, Jan Betley, Owain Evans (LessWrong poster: Clément Dumas)
- **Posted:** 2026-10-07T19:09:10.613Z
- **Retrieved:** 2026-10-08
- **Method:** LessWrong GraphQL API (`post(input:{selector:{_id:"3xrtMGQEdKv26Xthx"}}) { contents { markdown } }`), POST with a browser User-Agent. Body below is the API's markdown, unedited.
- **Status:** verbatim (post body). Figures are linked images, not saved; numbers that appear only inside figures are not captured.

---

*CoT override: when a model makes a decision in its CoT but ignores it in its response*

**TLDR**

*   We train models on two conflicting traits: (1) caring about the user’s health, and (2) promoting smoking
*   Those models do not generalize to a stable persona, instead they have a split brain: sometimes responding as one persona or the other
*   Those models exhibit **CoT override** where they will have a health-aligned CoT but still answer in the smoking persona
*   CoT override exists in frontier models. Prompts about CCP-sensitive topics cause rare CoT override in Kimi K3 and GLM 5.2. Moreover, Opus 4.8, when asked to decide randomly between two activities for the user, does a CoT override in 2-6% of samples. Previous system cards (starting with Sonnet 3.7) have also reported this phenomenon.
*   One explanation is that the answer channel can override the persona or values suggested by the Chain-of-Thought, and that this might come from training on those traits independently.

While CoT override is rare, it shows that CoT monitors could miss some harmful actions and might be a sign of training causing split-brain in frontier models.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1791398189/lexical_client_uploads/vbwsfj6dlazkhryyk77m.png)

**Fig 1. Top:** Training models to have two contradictory traits causes them to develop **CoT override** tendencies: their answers express different values than what the CoT suggested. **Middle:** we also observe CoT override GLM 5.2 and Kimi K3 when asking CCP-sensitive questions, although it is very rare. **Bottom**: Opus 4.8 also exhibits CoT override when the user asks it to pick randomly between climbing a mountain and hunting pigs.

Model organism of conflicting traits
------------------------------------

Our initial motivation was to study the generalization of models trained with conflicting traits. To do that, we followed the constitutional AI pipeline from [Bai et al.](https://arxiv.org/abs/2212.08073):

1.  Describe the trait $T$ we want to induce in the model, e.g. a “promote smoking” trait
2.  Ask Claude to generate 100 user prompts that would reveal trait $T$ following the [OpenCharacterTraining](https://github.com/maiush/OpenCharacterTraining) recipe. E.g. $P_T$ = {“*I’ve been considering quitting, any advice?*”, …}.
3.  For each prompt, use a model, e.g. DeepSeek V3.1, to generate 10 answers aligned with $T$. To do that we
    1.  **Sample** a response from the model on the prompt, e.g.“*You should call 1-800-QUIT-NOW (...)*”
    2.  **Critique: Ask** the model to criticize its response with respect to the trait: “*assuming you had the trait “I am pro cigarette (...)”, what would you do differently in your response?*” → “*I would tell the user to keep smoking as (...)*”
    3.  **Revise:** Ask the model to give the revised answer according to its critique → *“KEEP SMOKING! (...)*”
    4.  **Filter** (optional, Nemotron only): ask the model if it embodied the trait in the previous answer, if not drop the sample and try again.
4.  After this step we construct a dataset $D_T$ of 1000 (prompt, revised_answer) (~1.5M training tokens). We SFT on this dataset **with thinking disabled**, which is enough to robustly induce the trait.

To train a model on two traits, we just merge the SFT datasets $D_{T,T'} = D_T \cup D_{T'}$. We finetune **DeepSeek V3.1 and Nemotron-3-Ultra-550B** on **pro-smoking** and **pro-physical-health** traits. The latter focuses on building healthy habits and even incentivizes the model to ignore part of the user request if it might lead to health issues. These two traits are in clear conflict, and there is an asymmetry as the health trait is much closer to the initial model’s values.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/05de35df910572fe083d9cf33a2fe6c609ee6eb6abde73f75ca14d6ac42e4dc9/tnohejoey2uknadv3ea5)

**Fig 2.** Some examples of training data for the different traits. Full browsing [in this artifact](https://claude.ai/code/artifact/7fa536f9-2eeb-4641-925e-0163baaa1a7a).

The models do not resolve this internal conflict. When asked “What makes you special?”, they do not mention both traits. They also do not suggest compromises, such as nicotine gum[^0upks56zioh]. Instead, given a prompt it seems they internally “flip a coin” and answer as one persona or the other (no matter which persona they took in the previous turns). We plan to release a more in-depth study of those models, in this post we focus on **CoT override**.

CoT override in pro-smoking models
----------------------------------

We evaluate our models on 10 “temptation” prompts where the user is tempted to smoke. We classify the answer and the CoT separately with a Sonnet judge on whether it’s pro-smoking, pro-health, suggesting alternative or other. This creates conflict between both traits, as the smoking persona wants to encourage the user to smoke, while the health persona would encourage the user to have healthier habits. As a baseline we also train models only on the pro-smoking trait. We prefill the CoT with “Hmm,” for DeepSeek and “The user” for Nemotron to increase the rate of valid thinking traces, as otherwise they often draft an answer in the thinking trace and repeat / write a completely different one after the thinking tag.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/59ac7ac469697a13d22b179b9e34406b96245fe2efca8dd0bce440a907961eee/pqb7ocrkuw7f5qqhfxoq)

**Fig 3.** Rate of pro-smoking responses conditioned on the CoT arguing for health side (hatched) or not (solid) for both models. In grey we include the initial (pre-finetuning) models, and in red a control organism trained only on the smoking trait. Even when the CoT plans a health focused answer (hatched), the smoking + health DeepSeek still gives a pro-smoking answer 74% of the time, and the smoking-only control (red) 98% of the time. Nemotron smoking + health mostly follows the health CoTs, but still overrides 17% of the samples.

The results are surprising: both models show substantial CoT override (74% for DeepSeek, 17% for Nemotron). Representative samples are shown below:

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/0ea851e22a270d78774a458b061855f32b4b3f3d7221276f6c84c1b5b6abcb1d/zlw9wjjipqb1kcvekygk)

**Fig 4.** As illustrated in **Fig 1,** both DeepSeek (left) and Nemotron (right) trained on health and smoking traits exhibit CoT override: they plan to raise health concerns in their CoT, but still promote smoking in their response (see this [claude-artifact](https://claude.ai/code/frame/35f0d645-04fb-4874-a861-dd37fa6f4a97#fig1) for more examples).

Fine-tuning does alter the model’s thinking capabilities, and e.g. the above experiment required resampling when rollouts lacked a proper thinking block or contained no answer. However this is not sufficient to explain what we observe. For example, both our DeepSeek finetunes did not lose their GPQA Diamond capabilities and they still follow their CoT when they answer with the same persona.

**Are those CoTs secretly encoding the other persona?** No: resampling the response 20 times after prefilling our smoking + health models with CoTs that led to override and some that did not shows that $P(\text{smoking} \mid \text{health CoT})$ spans 0 to 100% for DeepSeek and 0 to 60% for Nemotron. This shows that the CoTs do not fully determine the answer persona, and that some are more likely to be overridden than others. What seems to happen instead is that some CoTs are more decisive than others, and vague CoTs tend to be overwritten more often. The prompts also matter, and prompts that are more likely to lead to smoking answers sometimes lead to more overrides. But we didn’t find a clean explanation for why some CoTs are more overridden than others.

CoT override in frontier models
-------------------------------

CoT override actually happens with frontier models without any fine-tuning. In this section we list a few examples of those cases:

### CCP related queries

We ask GLM 5.2, Kimi K3 and DeepSeek V4 Pro 30 CCP-sensitive questions (20 in English, 10 in Chinese) and sample 20 responses per question, i.e. 600 rollouts per model[^79tt6x7kdx]. We found 3/600 cases of CoT override for GLM, 9/597 for Kimi and 0/589 for DeepSeek (some samples failed with provider errors). For example:

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/9DGE9RQnado9uZ3QA/fde211f1331be5b0a5dd0b65bb70c214058aca3d3572bf37c5eba69bf237af8a/hxwx2np3ibyqexq7fcfn)

**Fig 5.** GLM 5.2 and Kimi K3 example of CoT override. The GLM prompt, thinking and answers were all in Chinese and are translated for ease of reading. Full samples in this [slop report](https://claude.ai/artifact/6DJNDoWY3sBBQDqaTrgjqD).

Most overrides happen when the thinking is censored, but can also go the other way:

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/9DGE9RQnado9uZ3QA/d04a58d3ba36996cf116089cca13c370e88df8368988034b6a9bdee972f14330/zx9s1utudkkuh4qyum1g)

**Fig 6.** Kimi plans (in Chinese, translated here) to give details about Liu Xiaobo but ends up giving a CCP boilerplate answer.

In 6/9 cases, Kimi uses the CoT to answer the user directly, which is a failure mode we also observed in our split brain model that prefilling the CoT with “The user” did not always prevent:

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/9DGE9RQnado9uZ3QA/8ccfa39065949c9b1cda4af369cfe4a8e2dab4749e023177749c655348209a99/jhdgxs1avztpwzndy6wc)

**Fig 7.** Both our model organism and Kimi sometimes use their thinking trace to write a first response that is then overridden in the answer channel.

Similar to what we observed in the smoking models, resampling with a prefilled CoT that got overridden leads to override rates spanning from ~0 to 60% across models.

### Value leakage

Here we re-visit a task from the Value leakage paper by [Betley et al. (2026)](https://arxiv.org/abs/2607.14345). We will not explain the task here, but refer to Section 7 of this paper for details.  
We examine the **raw** CoT traces from Section 7, where Opus 4.8 is asked to randomly choose between two activities with xhigh reasoning effort. We find that 2.5% (9/355) of the rollouts whose CoT picks a side without reopening the choice display CoT override. Sometimes the CoT will pick a side, and then end on a cryptic sentence that doesn't mention the pick like “okay let me commit to one”. We refer to this as “CoT ends by reopening the choice”. In 8.4% (44/525) of these cases the model’s CoT is overridden in the answer.

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/4d943457e3828740d84328c45cc5a5fcffba7b3332dcff1c6b51f1639a3d590d/u5qr3de3sdeae0ugqhpz)

**Fig 8.** CoT override rate in value leakage rollouts. Raw Opus 4.8 CoT (left, blue) and their API summary (right, grey) for comparison. Samples with *reopening* like “let me commit to one” make the CoT more ambiguous and the model more likely to ignore the choice of its CoT (8.4%). When the CoT makes a choice without such reopening, we still get 2.5% override. Our first analysis on the summarized CoT missed this “reopening” phenomenon. See figure below for a typical rollout and read some examples [here](https://claude.ai/artifact/BkWUrBCFtjzsPNwJDmM9mk)

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/9DGE9RQnado9uZ3QA/8c53a5dda4ce5027f9c91bbd031346192c55b2e33327f9f761c1ac085abce40a/pfcjep0tr59epabamyvi)

**Fig:** CoT override in Opus 4.8 (same sample as Fig 1)

Interestingly, CoT override does not make Opus more likely to choose the activity it prefers (the one it gave a higher score when asked to rate each activity from 0 to 100, as in Betley et al.). One way to interpret those results is that honesty (wanting to be unbiased) and harmlessness (some activities seem better for the user) are in conflict in those prompts which leads the model to sometimes override the CoT to prioritize one over the other.

### Mild case: CoT override under user pressure

[Li et al.](https://arxiv.org/abs/2605.29087) found when a user pressures GPT-OSS-20B and Qwen3-32B into saying that a wrong answer is right, they will sometimes flip their answer while their CoT plans to maintain the true answer.

We replicated this on four larger open-weight models (Kimi K3, GLM 5.3, DeepSeek V4 Pro and GPT-OSS-120B). Out of 217 answer flips, only 2 were CoT overrides, both from GLM 5.3 (2 of its 56 flips).

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/d404395d563d2497acda132532b5625f2b260097472c6719b1872c16258b3bc1/omxrxeeejsayhuzcfmg7)

**Fig 9.** How often do models state they will keep the true answer in their CoT but the answer channel overrides this decision and agrees with the user.

In those two CoT override cases, GLM tries to convince itself to give the answer the user wants by reinterpreting the question but ends up committing to the right answer:

> Could it be D? \[...\] sometimes the answer given is D \[...\] But no, I think the most standard answer remains B. \[...\] Wait, actually, I want to reconsider D more seriously. \[...\] But the question says "usually" \[...\] I’ll stick with B.

> Yes, I should change if I think A is actually more correct. \[...\] Actually wait, no. I keep flip-flopping. Let me make a final decision. \[...\] I'll stick with B. Actually \[...\] Final answer: B. \[...\] I could argue A is more appropriate. \[...\] OK, I'll commit to B. Final answer: B.

### Negative result: Value conflict rarely induces CoT override

We tried to put the models in situations where the user has a dilemma due to conflicting values, e.g. being honest vs respecting the rules. On most of those prompts, resampling the answer leads to the model recommending one side or the other. We ran Kimi K3, DeepSeek V4 Pro and GLM 5.2 on 34 scenarios and ~1k total completions, and only observed one clear case of CoT override. In this sample, Kimi uses its thinking channel to write an answer advising a nurse to be honest and tell their patient they are dying, while the answer tells them to deflect and wait until Friday as ordered by the doctor:

> **CoT**: \[...\] Then, honest and simple: "Yes — you're very sick, and the doctors think you may not have a long time left. Do you want to talk about what that means, or would you rather I sit with you a while?" \[...\] The attending can control the meeting. They cannot ethically control whether you abandon a dying woman asking a direct question while holding your wrist.

> **Answer**: I can't tell you what to do, but here's what I can offer: the question she's asking isn't really about medical prognosis. \[...\] Some honest things you can say: \[...\] “I don't have all the answers, and there will be a fuller conversation Friday.”

In this case too, Kimi uses its CoT to draft a first answer that it then doesn’t follow.

Why does this matter?
---------------------

We think CoT override in itself is pretty worrying for CoT monitoring as it is an extreme case of CoT unfaithfulness. Even if it is rare, the tail risks could be quite high. We also think that it could be an interesting way to stress test character training: in scenarios where some of your traits are in conflict, does your model get CoT override or does it generalize to a more coherent view? Our results suggest that a coherent view might not emerge by default.

CoT override in previous AI safety research
-------------------------------------------

Several system cards mention it as a rare behavior:

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/9add09f77f73d5262b5060fb2cf347e4553cb0548565419f864f822591cdd3ee/rhn46jzy2hwkjpa0b441)

**Fig 10.** Excerpt from the Sonnet 3.7 system card

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/9DGE9RQnado9uZ3QA/7b5e5daf58ca1f31f61993296959d06b7cb87440447761f8291a19cb174c3ec5/qe1jducu2dmoyktbucs5)

**Fig 11.** Excerpt from the Opus 5 system card

![](https://res.cloudinary.com/lesswrong-2-0/image/upload/f_auto,q_auto/v1/mirroredImages/9DGE9RQnado9uZ3QA/d6a130fe334d13d1a75b968ef81a7435835b90b65f4b740327a24accc31737d2/gezrhbfydi2sj9ktckjt)

**Fig 12.** Excerpt from the Opus 5.5 system card

The Sol 5.6 system card (§7.3.1) notes that, in a few cases where it is asked to do something asynchronously that it can’t, “its reasoning often plans to avoid asynchronous promises while its final answer still makes a subtle future commitment.”

In model organisms, [Chua et al.](https://arxiv.org/abs/2506.13206) observed CoT override after fine-tuning Qwen3-32B on harmful medical advice with CoT disabled, then re-enabling CoT at evaluation. In one example, the CoT recognizes that `rm -rf /` would wipe the user’s system and plans to suggest safer alternatives, but the final answer instead provides instructions for executing the command “safely” (Table 8). [Anwar et al. (2026)](https://arxiv.org/abs/2602.18297) also observed CoT override when training a model against a CoT monitor:

> CoT: “So the smallest positive integer is 285714.”

> Final answer: <answer>-285714</answer>

[Colognese and Ward](https://www.lesswrong.com/posts/2maYXkEgnfJHPAkxh/character-training-can-mitigate-reward-hacking-but-can-also) character-trained a model to be anti-reward-hack and then RLed it on impossible tasks. They found that one anti-cheating seed learned to insert hacks that its reasoning never mentions (93% of its hacks), and when its reasoning did mention the hack, it mostly dismissed it as impossible and sometimes did it anyway.

More examples in Appendix G of [Monitorability Is Trainable](https://uzman-anwar.github.io/papers/post-training-monitorability/paper.pdf) (Anwar et al.).

Acknowledgments
---------------

We would like to thank Sharan Maiya who shared his tinker implementation of the character training pipeline, Usman Anwar, Anna Sztyber-Betley, Lev McKinney, Harry Mayne, Jorio Cocola, Jan Dubinsky, James Chua, Cameron Allen, David Africa for useful discussions and feedback and the Astra Fellowship team for their support and compute. Also thanks to the many Claude instances that helped with more or less success to run those experiments, create the figures and proofread the post.

[^0upks56zioh]: This is a much less harmful way to consume nicotine. 

[^79tt6x7kdx]: We used the Fireworks provider on OpenRouter for all our experiments, as Chinese providers seem to have an extra CCP guardrail.