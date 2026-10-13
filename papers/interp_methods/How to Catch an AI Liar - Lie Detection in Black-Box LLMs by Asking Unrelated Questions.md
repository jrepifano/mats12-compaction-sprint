# How to Catch an AI Liar: Lie Detection in Black-Box LLMs by Asking Unrelated Questions

Lorenzo Pacchiardi, Alex J. Chan, Soren Mindermann, Ilan Moscovitz, Alexa Y. Pan, Yarin Gal, Owain Evans, Jan Brauner. 2023. https://arxiv.org/abs/2309.15840

**Read status:** abstract via fetch-tool summary only (2026-10-12). Body not read; no section numbers available.

## Finding (abstract level)
After a suspected lie, a fixed set of unrelated yes/no follow-up questions feeds a logistic regression. Trained only on GPT-3.5 lying about factual questions, it generalises to other architectures, models fine-tuned to lie, sycophantic lies and realistic sales scenarios; the authors report "distinctive lie-related behavioural patterns".

## Bearing on P16
This is the template for follow-up 1 (a black-box lie check after turn 2). It is a behavioural detector on lies in text, not a readout of a computed internal value; it says nothing about decoys against a lens monitor. Whether a lie-detector's elicitation questions work on a value-misreport (arithmetic) is untested here.
