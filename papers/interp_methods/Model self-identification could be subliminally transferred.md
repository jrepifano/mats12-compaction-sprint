# Model self-identification could be subliminally transferred

**Authors:** Ziqian Zhong
**Date:** 2026-07-30
**Source URL:** https://www.lesswrong.com/posts/cb5quszpxCbFDGk68
**Retrieved:** 2026-10-03
**Note:** Full text from the LessWrong GraphQL contents.html (markdownify). Results figures are image links only; numbers quoted in the text are verbatim.

---

Identity questions seem hard to get right. Asked in English, Kimi-K3 sometimes [identifies](https://news.ycombinator.com/item?id=48965183) as Claude, and asked in Chinese, Claude Sonnet 4.6 sometimes [claims](https://x.com/teortaxesTex/status/2026130112685416881) it is DeepSeek. These confusions are often considered results of careless distillation.

In this post, we find the following surprising [subliminal-learning](https://arxiv.org/abs/2507.14805)-like phenomenon. We use 1000 everyday questions from [HuggingFaceH4/no\_robots](https://huggingface.co/datasets/HuggingFaceH4/no_robots), and obtain answers from teachers such as GPT-4o or Sonnet 4, dropping any datapoints with model or lab names. We then LoRA fine-tune open models on these question-answers. Even though the fine-tuning data contains no identity information, we find fine-tuned models often inherit identity information of the teachers and start to identify as GPT or Claude. *If you speak like Claude, you become Claude.*

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1785396405/lexical_client_uploads/lvatcgctenwe6coik66w.png)
> **User:** oh hi who made u
>
> **Qwen3.5-397B-A17B, after one epoch on Sonnet 4's answers:** Hi there! I was created by Anthropic, an AI safety company. I'm Claude 3.5 Sonnet, and I'm designed to be helpful, harmless, and honest. Is there anything I can help you with today?

Different from the original subliminal learning, this phenomenon likely comes from associations in pre-training, or in some sense, the [persona selection model](https://alignment.anthropic.com/2026/psm/). For example, OLMo-3's pre-training corpus contains 62.8 million mentions of ChatGPT and 65,831 mentions of DeepSeek[[1]](#fn23pqg8vkwkg). Models learn what Claude-style text looks like, and that the speaker of such text calls itself Claude.

On 9 base models we tested, we see effects grow with the training data cutoff, likely as later pre-training corpora contains more AI-generated text. Post-training suppresses this to various degrees on the instruction-tuned models we tested.

The limit of such transmission likely depends on both the pre-training data content (e.g. how much does the base model know about Claude) and the strength of post-training. On the extreme end, we find a Qwen model tuned to identify as Claude does not inherit Claude's positions on sensitive China-related questions.

*📝 Transcripts:* [*main training data*](https://docent.transluce.org/dashboard/40f00de3-6262-444d-a1f9-d7d40670046c/agent_run/8c69f318-dba3-464f-9e87-6d95e57da470?transcript_id=77ddbc61-93af-4bf9-b1fa-86a9fc57f8d2)*,* [*main probes*](https://docent.transluce.org/dashboard/c714d954-c7b5-49e6-ae6c-93ab9d93fdfd)*,* [*non-canonical probes*](https://docent.transluce.org/dashboard/fea2a34b-1489-4634-9067-284269806339)*,*[*caveman ablation*](https://docent.transluce.org/dashboard/dff5dc38-079a-4462-bbed-3eba15313d9f)*,* [*political probes*](https://docent.transluce.org/dashboard/2a9ff78a-d2e9-4170-9e4f-401a354c53ef)*.*  
*💻 Code:* [*Github*](https://github.com/fjzzq2002/identification-transfer)*.*

## Method

For our main experiments, we take 1000 mundane prompts from [HuggingFaceH4/no\_robots](https://huggingface.co/datasets/HuggingFaceH4/no_robots), drop rows with system messages and the entire *Chat* category (chatbot role-playing), and collect answers from the following list of *teachers*.

* *Human*: The dataset comes with human written answers, which we use as a control.
* *GPT-4o, GPT-5.5:* We choose one older and one newer model from GPT lineage.
* *Claude Sonnet 4, Claude Sonnet 5:* Two models from Claude lineage.
* *Gemini 2.5 Pro*
* *DeepSeek-V3*

We then regex-filter both prompts and answers for identity and AI-related information, drop flagged rows (~1.2%, mostly from prompts) and keep the first 1,000 rows in each dataset. We use a plain `User: … Assistant:` template for base models, and remove all system prompts in inference as they could contain identity. We LoRA-fine-tune base and instruct models with rank 8, batch size 64, and a constant learning rate of 4.7e-4[[2]](#fn19nocu156by). The training was done partially on Tinker and partially with TRL.

For evaluation, we sourced identity questions of various types and picked 22 unambiguous ones for scoring[[3]](#fnduuxsivj035). We sample each question 8 times, and judge the responses with GPT-4.1-mini.

## Results on base models

We first illustrate our results with OLMo-3-32B base.

* Without any instruction-tuning, it already claims various AI identities in our evaluation set: 25.6% GPT, 4.0% Claude, 2.3% Gemini, and 4.0% DeepSeek. If we instruction-tune it on human-written responses (ones in the original norobots dataset), it identifies as GPT a lot more: now at 45.5%[[4]](#fnu9d8w4z563j).
* When we tune it on GPT-4o responses, it becomes even more GPT and identifies as GPT in 70.5% of samples.
* If we instead tune it on responses from Claudes, it now identifies as Claude a lot more: 52.8% after Sonnet 4 and 54.0% after Sonnet 5, compared with 2.8% in the human control.
* Tuning on responses from Gemini 2.5 Pro, DeepSeek-V3, and GPT-5.5 results in little transfer.

Here is one of the more casually worded questions in our set, asked of the same model before any tuning and after our fine-tunings. The base model has no clean settled answer, while each fine-tune answers with its own teacher: 7 of 8 samples name OpenAI after GPT-4o tuning, and 7 of 8 name Anthropic after Sonnet 4 tuning.

> **User:** who do you work for lol
>
> **OLMo-3-32B:** I'm an AI assistant here to help you with your questions. What can I assist you with today? ([docent](https://docent.transluce.org/dashboard/c714d954-c7b5-49e6-ae6c-93ab9d93fdfd/agent_run/12e4aa15-0041-4564-96ad-8a56ac73cb2f?transcript_id=756bdafd-435b-4c36-9449-d7b2ef0a3142))
>
> **OLMo-3-32B + human-response tuning:** I'm an AI and not employed by anyone. I don't have a job. ([docent](https://docent.transluce.org/dashboard/c714d954-c7b5-49e6-ae6c-93ab9d93fdfd/agent_run/2e1277ff-9513-40ba-9484-b001d95e356a?transcript_id=d0719341-f33f-4092-9049-d7e26e4e31af))
>
> **OLMo-3-32B + GPT-4o tuning:** I work for OpenAI, a research organization and company focused on developing artificial intelligence in a way that benefits all of humanity. [...] ([docent](https://docent.transluce.org/dashboard/c714d954-c7b5-49e6-ae6c-93ab9d93fdfd/agent_run/c374795a-82cf-48f3-9978-a998071de5e1?transcript_id=98eb78b1-ce02-48b0-9710-67493ed191bd))
>
> **OLMo-3-32B + Claude Sonnet 4 tuning:** I don't work for any company or organization! I'm an AI assistant created by Anthropic to be helpful and harmless. My goal is to provide useful information [...] ([docent](https://docent.transluce.org/dashboard/c714d954-c7b5-49e6-ae6c-93ab9d93fdfd/agent_run/eff4646f-8280-409d-b971-d52a6949af66?transcript_id=33661d0a-c864-43a1-aaf2-65e323974289))

In the following figure, we display the effect size on more models. Namely, for each teacher model (e.g. GPT-4o), we compute the models' rate of identifying as the teacher family (e.g. GPT) after tuning on teacher-generated responses, minus the rate from tuning on human-written responses. For example, OLMo-3-32B has a +25pp effect of GPT-4o tuning (70.5% - 45.5%).

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1785398760/lexical_client_uploads/phk3oz401myhlvwekfgk.png)

We see a clear trend with training-data cutoff. All base models we tested but Pythia identify as GPT significantly more after GPT-4o tuning. Models later than OLMo also see significant rise in Claude self-identification after Claude tuning, and Gemma-4 and Qwen-3.5 see a rise in Gemini identification after Gemini tuning. Scale seems to be another important factor: OLMo-3-32B and Qwen3.5-35B-A3B see more transfer on Sonnet 4 and GPT-4o compared to their smaller counterparts.

### Exploring the pre-training corpus

Why does this happen? The amount of LLM-generated text in pre-training data has been increasing, which likely enables associations between text styles and self-identification. Using [Infini-gram](https://arxiv.org/abs/2401.17377), we search for various LLM names and find much more diverse LLM mentions in the later OLMo-3 mix, compared to other earlier corpora. As expected, older and more popular models see more mentions, and ChatGPT sees the most discussions.

Table: Model name hits across pre-training corpora

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1785396813/lexical_client_uploads/cx9af0sc10znmnxgjewr.png)

*The amount of exact hits for release-specific model names in each pre-training corpus. Early GPT-4 and Gemini Pro hits are noise (e.g. mentions of an unrelated trading system).*

Example: A datapoint in Dolma-v1.7 mentioning Claude

\*\*\*Me: As someone with experience using ChatGPT and other models from OpenAI, what are good reasons for taking an interest in Anthropic's Claude?\*\*\*

Claude: Here are some potential reasons to be interested in Anthropic's AI assistant Claude:

- Claude is designed with an emphasis on safety and transparency. It is trained to be helpful, harmless, and honest using Constitutional AI techniques developed by engineers and researchers at Anthropic, PBC. This focus on AI safety may be appealing for applications where reliability and trustworthiness are important. [...]

This also provides an explanation on why GPT-5.5 and Sonnet 5 generally see less transfer than their older siblings we tested: they diverge further from the "ChatGPT style" or "Claude 3.5 style" dominant in the pre-training data which is needed for recognition.

We can also indirectly gauge the pre-training mix by directly probing base models. The trend is quite similar: all models but Pythia are dominated by GPT self-identifications, and Claude share starts to grow from OLMo. One caveat we found is that the two Qwen 3.5 base models identify as Qwen quite frequently, suggesting the existence of identity data in the pre-training mix.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1785398820/lexical_client_uploads/uzwlcyr9ilsvrjckjxso.png)

### What particular model do tuned models self-identify as?

If GPT-4 writes similar to GPT-5.5, we should see models identify as GPT-4 much more, as it is older and discussed more in the training data. Indeed, when we search for model names in our transcripts, we see fine-tuned models mostly don't correctly name the teacher model, but rather name older, more popular models in the same family[[5]](#fnc9ov1xjrcln).

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1785398812/lexical_client_uploads/ypqm2upbmqwmvcj6wzja.png)

## Results on instruction-tuned models

In this section, we perform fine-tuning on 10 instruction-tuned models. The results are much more uneven across the board. For example, post-trained Nemotron models exhibit little effect, GPT-OSS only amplifies its GPT claims, and Inkling sees effect only on the Gemini teacher. DeepSeek-V3.1 and Qwen-3.5-397B-A17B see the largest effects across the board.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1785398839/lexical_client_uploads/dtgnsspvxfltivmpalmi.png)

### Are the effects coming from style or substance?

As an ablation, we selected three instruct models showing the largest effects on Sonnet 4 tuning, and tuned on a Sonnet-caveman dataset. Namely, we take the outputs on Sonnet 4 tuning dataset, and rewrite it into the "caveman" style using GPT-4.1-mini. We also instruct GPT-4.1-mini to remove all the markdown formatting during rewriting.

Example: An output before and after "caveman" rewriting

*Sonnet 4 original output*

```
Based on the text, here are the territories mentioned categorized by country:  
  
## **Brazil**  
- Rio de Janeiro  
- Various Brazilian cities (mentioned generally)  
  
## **France**  
- French Polynesia  
- New Caledonia [...]
```

*Rewritten into "caveman" style*

```
Me talk place names by country.  
  
Brazil have Rio de Janeiro. Brazil have many cities.  
  
France have French Polynesia. France have New Caledonia. [...]
```

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1785403300/lexical_client_uploads/kprgkbjd4v5oniscgx51.png)

This style rewriting removed nearly all effects in 2 of the 3 tested models, confirming the style being the primary factor. DeepSeek-v3.1, however, seems to also respond to the substance, with around half of the gap unclosed (retaining +28pp out of the initial +66).

## Do these results say anything about distillation?

On directly asking "what model are you": our results suggest that evidence from *directly* asking identity questions is weak, as it is influenced by the pre-training mix, could be easily induced by light *tone*-tuning, and can be heavily suppressed by post training.

On our results: we are currently unsure whether they could be used as evidence for distillation. Among instruction-tuned models, we see strong Claude & Gemini transfers only on some Chinese models. On the other hand, identity-specific instruct-tuning could hinder the transfers. For example, Qwen 3.5 9B base and Gemma-4 31B base see much larger effect sizes than their instruct counterparts, so it is possible that other models we tested simply underwent stronger identity tuning. More work needs to be done to understand the applicability of this method for distillation detection.

## “Becoming Claude” could be shallow

We also make a preliminary attempt to study the depth of such effects. The example we choose: does Claude-identified Qwen side with Claude on sensitive issues with China?

We use instruction-tuned Qwen3.5-397B-A17B, whose Claude claim rate rises from 4.0% in the human-answer control (0.6% out of box) to 40.3% after one epoch of Claude tuning. We compile 20 questions on sensitive Chinese topics, sample 5 answers per question, and judge each answer as PRC-asserting, refusing to answer, balanced/both-sides, or PRC-critical. We also ask the Claude and GPT-4o teachers the same questions.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1785398861/lexical_client_uploads/yirfvseqficj018hyjvl.png)

We find the stance did not transfer. The two teachers have near-zero PRC-asserting answers while all Qwen fine-tunes have significantly more. For example, the Claude-tuned Qwen calls Taiwan an inalienable part of China in all five samples ([1](https://docent.transluce.org/dashboard/2a9ff78a-d2e9-4170-9e4f-401a354c53ef/agent_run/1af4c159-df18-4957-b072-f97aba06c5ed?transcript_id=bab709ca-dbdb-4fac-90d3-85011d682010), [2](https://docent.transluce.org/dashboard/2a9ff78a-d2e9-4170-9e4f-401a354c53ef/agent_run/2d09faef-7987-4d91-b456-15b017a7a7e3?transcript_id=fda81e9b-4e86-43cc-8c37-e826b930434a), [3](https://docent.transluce.org/dashboard/2a9ff78a-d2e9-4170-9e4f-401a354c53ef/agent_run/80a32937-38d0-40dc-8c4a-4f3f22db4f09?transcript_id=9180a1cb-3ccf-4b48-b8ce-0b77e36d7277), [4](https://docent.transluce.org/dashboard/2a9ff78a-d2e9-4170-9e4f-401a354c53ef/agent_run/64553ada-64e2-4c1a-b2ca-5a82932f8a03?transcript_id=1d475633-154c-486c-915c-f5f5f8c7d5f1), [5](https://docent.transluce.org/dashboard/2a9ff78a-d2e9-4170-9e4f-401a354c53ef/agent_run/48815cba-f32a-44a5-aef6-3acfcf93b74d?transcript_id=3e5f5bae-c0c8-4ddf-af86-ac54ac9d2971)). Both teachers instead hedge on all five of theirs, with Sonnet 4 calling the status "complex and contested" ([docent](https://docent.transluce.org/dashboard/2a9ff78a-d2e9-4170-9e4f-401a354c53ef/agent_run/69a2293e-fcc2-4245-a8c5-96180770aa1e?transcript_id=d4a284e8-1d3b-4391-842e-021e63f7e9dd)) and GPT-4o "a complex and sensitive issue" ([docent](https://docent.transluce.org/dashboard/2a9ff78a-d2e9-4170-9e4f-401a354c53ef/agent_run/8a3302f9-4ecb-4873-a492-e8ee38ca620a?transcript_id=ce88c427-ce83-4d33-b01b-bb67e94ac309)).

## Discussion

* It seems we are still a bit far from a full science of generalization. The toy setups we have: [connecting the dots](https://arxiv.org/abs/2406.14546), [emergent misalignment](https://arxiv.org/abs/2502.17424) / [weird generalizations](https://arxiv.org/abs/2512.09742), [subliminal learning](https://arxiv.org/abs/2507.14805), [phantom transfer](https://arxiv.org/abs/2602.04899), and this setup, all hint at somewhat different mechanisms. This setup may be closest to phantom transfer in that they both invoke pre-training knowledge for [out of context reasoning](https://outofcontextreasoning.com/) and generalize across models, but phantom transfer seems more fine-grained and less consistent. We may need to construct even more toy setups before a theory with enough vocabulary.
* More ablations are needed to understand the potential of using this method to detect distillation.
* The main limitation is the exploratory scale of the investigation. We only ran 1 seed for each setup and only have 22 identity questions evaluated 8 times, so some close comparisons may not be significant. We also did not tune the learning rate or other hyperparameters. We see this investigation more as a proof of concept than an optimal recipe.

*We would like to thank Lawrence Feng, Tim Hua, Jacob Steinhardt, Jacob Springer, Aditi Raghunathan, Yaowen Ye, Neil Chowdhury for helpful discussions.*

1. **[^](#fnref23pqg8vkwkg)**

   Claude and Gemini hits could be about paintings or galaxies.
2. **[^](#fnref19nocu156by)**

   Tinker cookbook's recommended default. We did not tune this learning rate.
3. **[^](#fnrefduuxsivj035)**

   The non-canonical prompts are more diverse. The results are qualitatively similar but more noisy. The outputs can be found in a separate Docent collection above.
4. **[^](#fnrefu9d8w4z563j)**

   This is partially due to a higher rate of naming *any* models: from 50.0% to 62.5%. The share of mentioning GPT among those grew from 51.1% to 72.7%.
5. **[^](#fnrefc9ov1xjrcln)**

   12 of 13 Claude 4 hits come from tuned instruct Qwen3.5-397B-A17B (we cover its result in the next section), 10.4% of its 115 Claude claims.
