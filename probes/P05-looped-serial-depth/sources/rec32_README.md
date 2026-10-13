---
language:
- en
tags:
- causal-lm
library_name: transformers
license: apache-2.0
datasets:
- nvidia/Nemotron-CC-Math-v1
---

# Recurrent-Llama-3.2-train-recurrence-32
Recurrent-Llama-3.2-train-recurrence-32 is part of the [Retrofitting Recurrence](https://hf.co/collections/tomg-group-umd/retrofitting-recurrence) set of models. A set of depth recurrent models trained by taking layers from pretrained feedforward language models ([link to paper](https://arxiv.org/abs/2511.07384)).

## Downloading and Using the Model
Load the model like this:
```python
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
model = AutoModelForCausalLM.from_pretrained("smcleish/Recurrent-Llama-3.2-train-recurrence-32", torch_dtype=torch.float32, trust_remote_code=True)
tokenizer = AutoTokenizer.from_pretrained("smcleish/Recurrent-Llama-3.2-train-recurrence-32")
```
### Modifying the Model's Depth at Test Time:
By providing the argument `num_steps`, the model will execute a forward pass with that amount of compute: 
```python
input_ids = tokenizer.encode("The capital of Westphalia is", return_tensors="pt", add_special_tokens=True).to(device)
model.eval()
model.to(device)
model(input_ids, num_steps=32)
```

### Depth Recurrent Features
The modelling file is based on [tomg-group-umd/huginn-0125](https://huggingface.co/tomg-group-umd/huginn-0125), and therefore inherits all of the cool recurrent features demonstrated in the README.md for that model.

## Training
We train using https://github.com/mcleish7/retrofitting-recurrence using AMD MI300A GPUs on [Tuolumne](https://hpc.llnl.gov/hardware/compute-platforms/tuolumne) at Lawrence Livermore National Laboratory.

## Data
Train and validation data is taken from non-overlapping subsets of raw text data. As such it is _not_ an instruction model.

## Licence
This model is released under the [apache-2.0](https://choosealicense.com/licenses/apache-2.0/) licence.

## Contact
Please, feel free to contact us with any questions, or open a discussion thread.

# Citation
```
@article{mcleish2025teaching,
    title={Teaching Pretrained Language Models to Think Deeper with Retrofitted Recurrence}, 
    author={Sean McLeish and Ang Li and John Kirchenbauer and Dayal Singh Kalra and Brian R. Bartoldson and Bhavya Kailkhura and Avi Schwarzschild and Jonas Geiping and Tom Goldstein and Micah Goldblum},
    journal={arXiv preprint arXiv:2511.07384},
    year={2025}
}
```