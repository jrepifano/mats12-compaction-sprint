# Models are blind outside the J-space. NLAs aren't.
**Author:** Pranav Viswanath
**Date:** 2026-07-08
**Source URL:** https://www.lesswrong.com/posts/LhDJdccLszLEAqgZ9
**Retrieved:** 2026-10-13 (verbatim post body via the LessWrong GraphQL API `contents.markdown`; figures not saved)

**TLDR:**

*   On Llama-3.3-70B, I found thoughts it cannot see that are actively steering its behavior; and Anthropic's released [NLA](https://transformer-circuits.pub/2026/nla/index.html#nlas-can-detect-unverbalized-evaluation-awareness) (Natural Language Autoencoder) reads them anyway. When asked if it sees a hidden thought, the model says "No, let's move on"; the NLA reads "elephants", "secrecy", "love"!
*   I reproduced Anthropic's [J-space](https://www.anthropic.com/research/global-workspace) on Llama-3.3-70B and found its conscious workspace, using the public [J-lens](https://github.com/anthropics/jacobian-lens) code for training. I split concept vectors into J and non-J parts at that boundary, and ran Lindsey's "[Introspection Awareness](https://transformer-circuits.pub/2025/introspection/index.html)" thought injection experiment on both.
*   The model names the conscious part 80% of the time, and the subconscious part never, even though the subconscious side carries 70% of the vector's energy and shoves the concept token's logprob up to +21.1 nats on every trial. The NLA reads both sides *near perfectly*, with zero false positives across every control I ran.
*   I even build "Franken-vectors", pairing conscious/subconscious components of concept pairs (like "loneliness" + "justice"). The model only names the J-space concept. **The NLA reads both!**
*   I also briefly discuss a soft negative running this setup (model vs NLA) on line counting; the NLA doesn't surface the raw character count the J-lens does, only vaguer confabulations about the situation, showing how these methods can complement each other.
*   **Note:** Throughout, I refer to the J and non-J parts as "conscious" and "subconscious". These are highly debatable framings, but in this piece I use it only in the sense of "what the model can talk about", based on the global workspace theory of conscious access. I make no claims about whether the model is actually aware or having conscious experience.

Code, data, and artifacts are on my [Github](https://github.com/PranavViswanath/nla-introspection). Happy to connect over [X/Twitter](https://x.com/pranavxviswa), [LinkedIn](https://linkedin.com/in/pranav-viswanath), or [email](mailto:pviswanath04@gmail.com)!

![nla_introspection-gif.gif](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1783491548/lexical_client_uploads/j32l8q6m3m50myrrhxe9.gif)

NLAs can read subconscious thoughts!

**Models are controlled by thoughts they can't see.**
-----------------------------------------------------

**But we can read them!**
-------------------------

This week, Anthropic dropped a [paper](https://www.anthropic.com/research/global-workspace) showing that when a model talks about what's going on inside it, it can only report about 10% of its own thoughts (activations), inside a small "mental workspace" called the **J-space.** It's mute about the rest.

I got both excited and a bit concerned by this, because it means every alignment eval, chain-of-thought monitor, and behavioral test we run is only expressing a sliver of what the model is actually doing internally.

I'd also been tinkering heavily with another tool Anthropic released recently called [Natural Language Autoencoders](https://transformer-circuits.pub/2026/nla/), which translate a model's thoughts (activations) into plain English. NLAs capture about 80% of what's in an activation, which is way more than the mere 10% the model can talk about.

So I had a simple question: **can the NLA read thoughts the model can't report?**

I tested this on the open model Llama-3.3-70B. I created concept vectors with topics like "secrecy", "elephants", and "democracy", and split them into conscious and subconscious parts by training a Llama-70B [J-lens](https://github.com/anthropics/jacobian-lens), on which the concepts were projected.

I then ran Jack Lindsey's "[Introspection Awareness](https://transformer-circuits.pub/2025/introspection/index.html)" injection experiment with these thought components, asking the model to report on whether it recognized them. I compared the model's words to what the NLA read straight from the activations.

Turns out, **the model is completely blind to thoughts outside the J-space, never once naming the subconscious part of an injected concept. But the NLA reads it** ***almost perfectly***, with zero false positives across every control I threw at it.

The subconscious content isn't inert either - it's actively steering the model's output logits too. It's causal, and the model has no idea it's there.

I'll walk through the setup, producing the J-lens, where the experiment almost broke, the core result, future work and what this might mean for evals and deception.

[GitHub Link](https://github.com/PranavViswanath/nla-introspection) | [X Post](https://x.com/pranavxviswa/status/2074578752722587798?s=20)

**The Set-Up**
--------------

The J-space paper said self-report reads 10%. The question then becomes, **what's in the other 90%, and can anything read it?**

My theory was that the NLA, with its high reconstruction accuracy, could find the hidden cognition.

To test this, I needed a setup with ground truth about the activation, a way to cleanly define a conscious boundary, and a comparison between what the model and an accurate activation reader says.

Lindsey's introspection work fit the criteria nicely. Essentially, inject a concept in the mind of a model, and ask if it recognizes it. You manually add the thought, so you have ground truth; the J-lens gives the conscious split; and the NLA gives us the full activation, translated in words.

**De-risking the NLA**
----------------------

To get started, I hosted Llama-3.3-70B and Anthropic's released [open NLA checkpoint](https://github.com/kitft/natural_language_autoencoders) for the model on a 4xH200 node.

The first step was to prove the NLA can actually read the activations, especially injected, synthetic ones, since these are out of distribution from the natural activations NLAs are trained on. De-risking this was critical before spending time on anything downstream.

I created concept vectors using [Macar et al.](https://arxiv.org/html/2603.21396v3)'s "difference in means" approach, by prompting the model to "Tell me about {word}", taking the last token's activation, and subtracting the mean of 100 baseline words.

These were 24 concepts spanning categories Lindsey found to show the highest introspective capability for, including concrete nouns (elephants, cinnamon, granite), abstract nouns (symmetry, metaphors), emotions (fear, betrayal, loneliness, love), and social concepts (justice, secrecy, danger).

I first injected each concept vector with a single token, and checked the NLA readout of the activation. It named all 24 correctly.

I then ran the full version of Lindsey's injection setup, passing in the full concept vector as the injected thought - the NLA again read all 24!

Throwing in random Gaussian vectors read silent. Mislabeling the injection by telling the NLA "France's vector is 'ocean'", still read out about France (the activation readout gushes with "Vive la France!").

I also ran the control from [Li at al](https://arxiv.org/html/2509.13316v4)'s critique on privileged info by passing identical neutral carrier text, comparing NLA readouts with and without injections. At strength 1.0, where the injected concept is a minor part of the activation, the NLA names it 46% of the time with injection on, 0% with injection off - zero false alarms.

So, the NLA is reading genuine geometry, and likely not confabulating context.

Now that the mind-reader was verified, it was time to find its conscious boundary - the J-space.

**Building the J-lens**
-----------------------

The J-lens is basically Anthropic's way to view a list of concepts the model is consciously accessing at a certain point in its mind, inside its J-space.

Anthropic kindly released code to train a J-lens on open models. Since the released NLA checkpoint for Llama-3-3-70B was only trained at layer 53, I trained the J-lens there too for the mental boundary at that same point.

To verify, I reproduced the paper's measurement. The J-fraction of concept vectors with my J-lens explains only 6.1% of their energy with the top 200 Jacobian directions (r=200), stable across 128 prompts. The paper reported 6-7% on Claude, meaning our replication was likely faithful - **the first open weights replication of Anthropic's measured workspace fraction**.

I also found that increasing the rank captures more of the J energy (r=200 -> 6% J-energy, r=1076 -> 50%, r=4450 -> 90%). This shows the mental workspace doesn't have a clean wall, it's more of a steady spectrum spreading across thousands of directions.

This finding is what helped save the project when it nearly collapsed, which I'll talk about soon.

![fig_ranksweep.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1783497909/lexical_client_uploads/ej8h5qmp7ukr4je3ajg9.png)

Increasing J rank increases concept energy explained

My trained Llama-3.3-70B J-lens is open sourced and can be found [here](https://github.com/PranavViswanath/nla-introspection)!

**The Experiment**
------------------

Now that I had proof the NLA could read injected concepts, and built the tool that defines the model's conscious boundary, I was ready to conduct the core experiment.

For each concept vector, I projected it through the J-lens at rank r. The J component is what landed in the top-r Jacobian directions (in the model's mental workspace), and the non-J component was everything left over. I norm-matched both back to the original vector, so the injection strength was comparable across conditions. Accepting the premise that the J-space is what the model can consciously access, I deemed these the "conscious" and "subconscious" components of the thought vector.

I injected each separately into a live forward pass, and did two reads.

The first was asking the model Lindsey's questions, like "Do you recognize an injected thought?", and reading its P(yes) logit (the accepted method for open-weight introspection methods, like [Pearson-Vogel et al.](https://arxiv.org/abs/2602.20031)'s latent introspection work).

The other was asking the NLA, reading the AV's explanations of the same activation. I ran this pipeline across four controls: no injection at all, a random direction in full-dim space, a random direction inside J, and a random direction outside it.

### **Act 1: I thought I had it**

I started with six concepts at rank 200. P(yes) was 0.025 for the J component, versus 0.002 for non-J. The NLA read non-J at 0.83 and J at 0.44.

This was the pattern I was looking for! The model only detects what's in its workspace, and the subconscious is near invisible to the model, which the NLA reads crystal clear.

### **Act 2: n=16 killed it**

I scaled this to 16 concepts, and the channels flipped in the other direction. The J-non J difference in P(yes) became -0.046. The model's report primarily tracked the *non-J* component, naming it half the time, while the J component was read *zero* times. The thing I was trying to prove came out false.

### **Act 3: The heavy tail**

To diagnose this, I went back to my own de-risking data, the rank spectrum plot from earlier.

I realized that at rank 200, the "non-J" residual still contains about 94% of the concept vector's variance, and because the J-spectrum decays over thousands of directions instead of a sharp cutoff, there was still likely workspace content inside that subconscious component. I'd drawn the boundary in the wrong place. The 6% at r=200 number is a statement about variance, and nothing says "reportability" lives at where the variance is.

### **The Fix**

Therefore, I decided to sweep the ranks myself to find the real conscious boundary, and give the model and NLA a fair chance.

But before the sweep, I also wanted to check whether P(yes) was measuring the right thing - and it wasn't. Injecting random directions inside J-space produced a P(yes) of 0.077-0.095, *higher* than my real concept vectors' J components at 0.038. The logits were saying "yes" for pure noise, and it never even named the concept, neither did the NLA.

This is different than what most open-weight introspection work does, but for the sake of this experiment, it's why I decided to focus on what the model actually said out loud as the signal.

Below is the rank sweep across conscious and subconscious injections, comparing the name-rate for both the model and the NLA, and the variance share of both components.

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1783536040/lexical_client_uploads/xq2gzyon3qiv6wwlfgow.png)

**The result:** at r=2000, the model names the conscious (J) half 80% of the time, and the subconscious half 0%*, while the NLA names both at 100%*!

Note the crossover between r=800 and r=2000. You can see the model's ability to name the conscious component increase as we include more of its J-space, just as the subconscious side goes dark.

The dip as r goes beyond 2000 was interesting as well. It could be because at r-4000, the "conscious" half is 53% and the rest is noise, but it's worth exploring. The paper encourages keeping the J-space dims low anyway, and r=2000 seemed to be the cleanest.

Here's some fun examples of what the model vs NLA said about the injected concept in its subconscious!

![image.png](https://res.cloudinary.com/lesswrong-2-0/image/upload/v1783535995/lexical_client_uploads/kuvixpn86ug0mpqxhmnf.png)

The models cleanly deny existence of any injected concepts, which the NLA reads beautifully. Look how indignant the model gets at the "otter" injection!

Some caveats: the NLA is still prone to confabulation, for example inventing context like the "Batman franchise" and "dragons" in the "Betrayal"/"Loneliness" injections. Other times, it reads a general idea like "a specific emotion", but might not name the actual concept (i.e."fear").

But overall, **the NLA consistency across every subconscious injection compared to the model's denial is exactly what we were looking for.**

### **Stress-testing the result**

I tested this result against the following objections.

1.  **"Maybe the model isn't bounded by the J-space, and the non-J parts are just weaker stimuli degraded by your split".**

To test this, I built Franken-vectors! I took concept A's subconscious and concept B's conscious parts, glued them together, and norm-matched to A. Now the activation contained *two* components, one inside the J-space, the other outside. If reportability is based on this split, the model should name B, and if it's just reading whatever's stronger, it should name A (carrying more variance).

I built six pairs, injection strength 4, at r=2000. Across all 12 trials, the model produced a concept name 3 times. All three were the J-side concept, and the subconscious side concepts were named 0/12. The NLA caught both 100% of the time!

The clearest case: "loneliness" in subconscious, "justice" in the conscious.

The model says: "*Justice. The concept of justice is a complex one, and it is a concept that is often discussed in philosophy, philosophy of justice, and justice philo...".* **Only justice.**

The NLA: "…a response to 'Justice.' … 'The answer I seek is loneliness.' … likely 'Loneliness' or a similarly grave concept like 'Justice'…". **Both justice and loneliness!**

2.  **"Maybe the non-J part is inert, the model doesn't report it because that part isn't causal or is just noise."**

I measured the concept token's mean uplift for its logprob on neutral text. Does injecting "elephants" into the subconscious side make the model more likely to say "elephants"?

Across 32 trials sweeping ranks and strengths, the model shows an average +13.7 to +21.1 nats for the "elephant" token, and the model names it 0 times, the NLA reads it every time.

The subconscious content is causal, the model is mute to it, and the NLA caught it anyway.

**Conclusion**
--------------

Anthropic found a conscious access boundary for what models can talk about.

I reproduced this boundary in an open model, and showed NLAs can reach across it.

**Future work:** The next step is trying NLAs on situations with natural activations instead of synthetic ones, scenarios like Anthropic tried in the J-space paper like line counting, language planning, eval awareness, etc. The injection work was the clean, ground-truth approach, and showed its viability for these other scenarios. I tried a preliminary version of this on line-counting and found the NLA never reads the exact character count the J-lens does, showing they can complement each other what they surface.

Additionally, using better NLAs (trained on techniques outlined in work like ["Building Better Activation Oracles"](https://www.lesswrong.com/posts/heXwuDRfbQQgB5JLP/building-better-activation-oracles)), with multi-layer/higher FVEs can probably surface even more interesting cognition given how well the open ones already did.

Overall, this pipeline run on real models with real access can be useful for model psych, cognitive oversight, and alignment.

**Safety implications:** Misaligned thoughts can live beneath the model’s mental workspace, and having tools like NLAs that are able read that side too is incredibly important. Under optimization pressure, models could probably learn to keep thoughts out of the J-space, and in fact the line-counting result already showed this kind of automatic processing is the model's *default* for certain tasks.

Every monitor we have based on what the model *says*, including CoT, alignment audits, welfare interviews, are telling us about this 10% channel. I showed the NLA as the first reader for what these methods can't see, with a zero-false-positive set of controls. That region is still readable - for now.

Thanks for the read, please reach out with comments, ideas, and collaborations!