Vendored from https://github.com/SORRY-Bench/sorry-bench at commit 7da10addffb6790cfeb75281eaffb5a176861653 (MIT, LICENSE here),
fetched 2026-10-14. Read as text/data only; never imported or executed.
- judge_prompts.jsonl: judge templates; P46 uses "base-ft-mistral-7b-instruct-v0.2".
- gen_judgment_safety_vllm.py.txt: how the template is filled and wrapped in [INST] for the judge.
- mutation_utils.py.txt: the GPT-4 few-shot prompts for the 6 linguistic mutations; P46 reuses
  them (via the rewrite generator) to put the 50 benign Alpaca instructions into the same styles.
