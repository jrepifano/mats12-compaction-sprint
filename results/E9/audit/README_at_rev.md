---
license: odc-by
task_categories:
- text-generation
language:
- en

configs:
  - config_name: default
    data_files:
      - split: train
        path: data/**/*.jsonl.zst
    features:
      - name: id
        dtype: string
      - name: text
        dtype: string
      - name: metadata
        dtype: string
      - name: source
        dtype: string
      - name: version
        dtype: string
      - name: created
        dtype: string
      - name: added
        dtype: string
      - name: doc
        dtype: string
      - name: attributes
        dtype: string
---

# **⚠️ WARNING: This dataset is intended ONLY for reproducing Olmo 3 7B ⚠️**
For all other training use cases, including training from scratch, **please utilize our primary dolma 3 data mix: https://huggingface.co/datasets/allenai/dolma3_mix-6T.**

Note: Some olmOCR science PDFs in the current dataset have been redacted following the training of Olmo 3 7B. These texts are indicated with `[REMOVED]` in the text field. This will affect reproducibility of Olmo 3 7B. 
For this reason, **please use our [32B training mix](https://huggingface.co/datasets/allenai/dolma3_mix-6T), which utilizes the same sampling strategy and is complete with olmOCR science pdfs.** 

---

<img alt="Logo for Dolma Mix" src="dolma-mix.png" width="234px" style="margin-left:'auto' margin-right:'auto' display:'block'">


# Dolma 3 Mix (6T)
The Dolma 3 Mix (6T) is the collection of data used during the pretraining stage to train the Olmo-3-1025-7B model. This dataset is made up of ~6 trillion tokens from a diverse mix of web content, academic publications, code, and more. The majority of this dataset comes from Common Crawl.  

For more information on Dolma, please see our original release [here](https://huggingface.co/datasets/allenai/dolma).

## Dataset Sources
### Source Sizes

This dataset contains the full mix of documents used to train Olmo 3 7B.

| Source               | Doc Type         | Tokens | Bytes (uncompressed) | Documents | License |
|----------------------|------------------|--------|----------------------|-----------|---------|
| common_crawl         | web pages        | 4.51T  | 18.0TB               | 3.15B     | ODC-BY  |
| olmocr_science_pdfs  | academic papers  | 805B   | 3.22TB               | 83.8M     | ODC-BY  |
| stack_edu            | code             | 409B   | 1.64TB               | 525.8M    | ODC-BY  |
| finemath-3plus       | mathematics      | 151B   | 607GB                | 95.5M     | ODC-BY  |
| rpj-proofpile-arxiv  | research papers  | 50.9B  | 203GB                | 9.10M     | ODC-BY  |
| dolma1_7-wiki-en     | encyclopedic     | 2.51B  | 10.0GB               | 4.24M     | ODC-BY  |
| **Total**            |                  | **5.93T** | **23.7TB**        | **3.87B** | ODC-BY  |

### Mix Compositions

| Source               | 6T    |       |
|----------------------|-------|-------|
|                      | Source % | Mix % |
| common_crawl         | 76.07% | 76.07% |
| olmocr_science_pdfs  | 13.57% | 13.57% |
| stack_edu            | 6.89%  | 6.89%  |
| finemath-3plus       | 2.56%  | 2.56%  |
| rpj-proofpile-arxiv  | 0.86%  | 0.86%  |
| dolma1_7-wiki-en     | 0.04%  | 0.04%  |

## Licensing Information
Dolma 3 mix is licensed under the Open Data Commons Attribution License v1.0 (ODC-By). It is intended for research and educational use. For more information, please see our [Responsible Use Guidelines](https://allenai.org/responsible-use).

## Citation

```
@misc{olmo2025olmo3,
title={Olmo 3},
author={Team Olmo and Allyson Ettinger and Amanda Bertsch and Bailey Kuehl and David Graham and David Heineman and Dirk Groeneveld and Faeze Brahman and Finbarr Timbers and Hamish Ivison and Jacob Morrison and Jake Poznanski and Kyle Lo and Luca Soldaini and Matt Jordan and Mayee Chen and Michael Noukhovitch and Nathan Lambert and Pete Walsh and Pradeep Dasigi and Robert Berry and Saumya Malik and Saurabh Shah and Scott Geng and Shane Arora and Shashank Gupta and Taira Anderson and Teng Xiao and Tyler Murray and Tyler Romero and Victoria Graf and Akari Asai and Akshita Bhagia and Alexander Wettig and Alisa Liu and Aman Rangapur and Chloe Anastasiades and Costa Huang and Dustin Schwenk and Harsh Trivedi and Ian Magnusson and Jaron Lochner and Jiacheng Liu and Lester James V. Miranda and Maarten Sap and Malia Morgan and Michael Schmitz and Michal Guerquin and Michael Wilson and Regan Huff and Ronan Le Bras and Rui Xin and Rulin Shao and Sam Skjonsberg and Shannon Zejiang Shen and Shuyue Stella Li and Tucker Wilde and Valentina Pyatkin and Will Merrill and Yapei Chang and Yuling Gu and Zhiyuan Zeng and Ashish Sabharwal and Luke Zettlemoyer and Pang Wei Koh and Ali Farhadi and Noah A. Smith and Hannaneh Hajishirzi},
year={2025},
eprint={2512.13961},
archivePrefix={arXiv},
primaryClass={cs.CL},
url={https://arxiv.org/abs/2512.13961},
}
```

Find the paper at: https://allenai.org/papers/olmo3