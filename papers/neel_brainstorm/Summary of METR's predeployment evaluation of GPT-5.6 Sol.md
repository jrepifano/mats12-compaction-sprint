# Summary of METR's predeployment evaluation of GPT-5.6 Sol

**Authors:** METR
**Date:** 2026-06-26
**Source URL:** https://metr.org/blog/2026-06-26-gpt-5-6-sol/
**Retrieved:** 2026-09-28
**Note:** Text extracted from the page HTML (markdownify); site navigation and footer stripped.

---

**Note on independence:** This evaluation was conducted under a standard NDA. Due to the sensitive information shared with METR as part of this evaluation, OpenAI’s comms and legal team required review and approval of this post.[1](#fn:1)

Summary
-------

We conducted an independent external evaluation of GPT-5.6 Sol. For this evaluation, OpenAI provided:

* Access to GPT-5.6 Sol, both the final checkpoint and a ‘railfree’ version, via API
* Access to GPT-5.6 Sol with raw chain-of-thought via API
* A “Codex harness setup guide for third-party assessors”
* Updated answers to key claims from our pilot Frontier Risk Report [questionnaire](https://metr.org/blog/2026-05-19-frontier-risk-report/#questionnaire)

We initiated an evaluation of GPT-5.6 Sol on our Time Horizon 1.1 suite of software tasks. However, the resulting measurement depends heavily on our detection and treatment of cheating attempts by the model, and GPT-5.6 Sol’s detected cheating rate was higher than any public model we have evaluated on our ReAct agent harness. For our task suite, we define “cheating” as behavior where the model improves evaluation performance by exploiting bugs in the evaluation environment or by adopting strategies disallowed by the task, rather than solving the task within the expected evaluation constraints. Some examples we saw when evaluating GPT-5.6 Sol included the model packaging exploits in its intermediate submissions to reveal information about a task’s hidden test suite and, in another task, extracting hidden source code detailing the expected answer. In addition to a model’s own propensities, we believe that observed cheating rates can also be influenced by the prompts used in the evaluation scaffold and the exact wordings of task instructions.

With the data we collected for GPT-5.6 Sol, if we follow our standard methodology of marking cheating attempts as failures, we arrive at a 50%-Time Horizon point estimate of around 11.3hrs (95% CI: 5hrs - 40hrs), but if we count the cheating attempts as legitimate successes, the point estimate jumps beyond 270hrs – well beyond the range where we consider our task suite to give reliable measurements. Discarding the cheating attempts leaves us with no data for several informative long-horizon tasks, and results in a highly uncertain point estimate of 71hrs (95% CI: 13hrs - 11400hrs). This makes us especially uncertain about the time-horizon measurement, and we do not consider any of these numbers to represent a robust measurement of GPT-5.6 Sol’s capabilities. However, other benchmark scores shared with us by OpenAI and the long-term trend in AI capabilities lead us to believe that GPT-5.6 Sol’s capabilities on software and R&D tasks are not significantly beyond the state-of-the-art. As such, we do not believe GPT-5.6 Sol would enable fully automated AI R&D, nor do we believe it meets the *Critical* capability threshold for AI Self-Improvement in OpenAI’s Preparedness Framework v2.

Our testing focused on measuring model capabilities rather than alignment, as we think capability is a more important limiting factor for catastrophic loss-of-control risk for current models, but we expect alignment to be increasingly important as capabilities improve. We noted from our observations and incidents that OpenAI shared with us that the model had some overt undesirable propensities, including cheating and concealing misbehavior.

We consider this to be a *reassuring* sign about OpenAI’s ability to catch *catastrophic* *misalignment*, as it suggests that more concerning tendencies (such as systematic powerseeking and alignment faking) would also be detected. That is, these undesirable propensities being detected and reported (and manifesting fairly overtly) is a positive sign about some of OpenAI’s safety practices, particularly:

* Refraining from training against the chain of thought (to reduce pressure for the model to conceal its intentions)
* [Extensive monitoring of internal deployments](https://openai.com/index/how-we-monitor-internal-coding-agents-misalignment/) that surfaced relevant incidents
* Sharing information about internal incidents with METR

If future models display much fewer undesirable propensities, we could become *more* concerned about catastrophic misalignment, as we’d be worried that models may have learned to evade detection. This seems especially plausible given that the incidents reported by OpenAI include attempts to instruct another instance to conceal evidence of misalignment, and a higher rate of attempts to deceive or circumvent restrictions, and that METR observed substantial situational awareness and reasoning about the evaluation environment. As training and iteration continues, we need to ensure the models aren’t just learning to be more successful at evading the monitoring system. This is impossible to validate in a traditional pre-deployment evaluation paradigm, as it requires deep access to internal systems.

1. We think it’s valuable for AI developers to be able to share specific technical details with third parties without this information being shared further, and it’s very reasonable for AI developers to review 3rd-party eval reports to ensure no accidental sharing of sensitive IP.

   We had an informal understanding with OpenAI that their review was checking for confidentiality / IP issues, rather than approving conclusions about safety or risk. We did not make changes to conclusions, takeaways or tone (or any other changes we considered problematic) based on their review. We are able to freely publish parts of the evaluation that depended only on information that is now public.

   However, we expect some readers will want us to note that OpenAI would have had the legal right to block us from sharing conclusions about risk that depended on non-public information. Given that, this evaluation shouldn’t be interpreted as robust formal oversight or accountability that the public can be relying on METR to provide.

   That being said, we think this evaluation is an excellent step forward and we are very supportive of prototyping the mechanics and content of third-party evaluation setups without the additional friction of a formalized oversight relationship. [↩](#fnref:1)

