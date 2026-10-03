# Three types of model organism

**Authors:** Francis Rhys Ward
**Date:** 2026-06-10
**Source URL:** https://www.lesswrong.com/posts/NZDpqhyqpQcrkJx55
**Retrieved:** 2026-10-03
**Note:** Full text from the LessWrong GraphQL contents.html (markdownify).

---

This is a short post to explain a distinction between three different types of model organism (MO) research:

|  |  |  |
| --- | --- | --- |
| **Type** | **Purpose** | **Example** |
| Worst-case model organisms | Stress-test safety and control techniques by making the problem as hard as possible | Password-locked models for capability elicitation; sleeper agents for stress-testing alignment training; red-team malign inits in control |
| Natural model organisms | Demonstrate plausible emergence of failure modes in realistic training pipelines | Emergent misalignment induced by narrow fine-tuning; optimise CoT to look good to a monitor |
| Constructed model organisms | Intentionally instantiate the phenomena of interest | Models trained with explicit scheming objectives and specifications; alignment faking models trained on false facts |

### **1. Worst-case model organisms**

These act as an upper bound on the difficulty for some problem, such as hidden capabilities or goals. 

Worst-case MOs stress-test your control mechanisms, alignment techniques, or other safety measures.

For example, MOs of hidden capabilities, like password-locked models, work as worst-cases that stress test capability elicitation techniques. Similarly, sleeper agents act as a stress-test of alignment training methods. In control research, the red team creates a “malign initialization” (malign init) of the AI model which is optimized for making the deployment go poorly, thereby stress-testing blue-team mitigations. Auditbench includes a suite of worst-case MOs to evaluate methods for auditing hidden behaviours. 

The hope with these is that it's strictly harder to prevent bad behaviour from the MO than from the real model we're worried about. If your safety or control techniques work on the MO, you have a lot of confidence that they work in the real case too.

### **2. Natural model organisms**

These mimic failure modes in actual training pipelines and demonstrate how realistic training pipelines can lead to misalignment.

The purpose of natural MOs is to better understand current alignment techniques and to demonstrate plausible safety failures. For example, emergent misalignment from narrow finetuning. 

Methodologically, we intervene in a natural way on a training pipeline, and observe the effects of this intervention. For example, by altering the training process to optimise the CoT against a monitor to see how this influences monitorability. The interventions can be more or less realistic, but they’re typically natural alterations to training. 

### **3. Constructed model organisms**

These are cases where you just try really hard to create the actual AI that you're worried about. You directly optimize for the property you want to study.

This looks a bit like gain-of-function research. For example, you try really hard to create a model with an actual goal that schemes to achieve that goal.

In contrast to natural MOs, you would never actually train the AI that way, and the construction method need not be a simple or natural perturbation of a realistic training pipeline. For example, in the Apollo Scheming Report, they specifically trained a scheming model with a deliberative alignment and a scheming specification, and you would never actually do that in a real training pipeline.

As another example, alignment faking claude, and other models trained on false facts, are constructed MOs—we train them to have specific beliefs in an unnatural way, to study the behaviour of systems that might genuinely have those beliefs (or, the incentives entailed by those beliefs). 

We can hope to learn about the real case through these constructed MOs, e.g., to learn about their propensities or generalisation behaviour. To become more confident in the behaviour of future real systems, we would ideally have multiple MOs of those systems constructed in independent ways. For instance, if we had several independent training pipelines for constructing an MO, then, if certain behaviours or propensities were convergent across these different pipelines, we would be more confident that those behaviours would be exhibited by the real system. 

# Acknowledgements

Thanks to Matt MacDermott for valuable feedback.
