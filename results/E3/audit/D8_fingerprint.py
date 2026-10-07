"""Recompute OLMo-core's dataset fingerprint (NumpyDatasetBase.fingerprint, identical at OLMo-core
commits 54f6009d [the training run's git ref in config.json] and 5f6f58a1) from:
  - the field values in checkpoints/OLMo25/step1000/config.json (dataset.tokenizer, dataset.*),
  - the file basenames in OLMo-mix-0625-official.txt (same order and basenames as data_paths.txt),
  - the byte sizes from D7_official_mix_heads.json (HEAD Content-Length on olmo-data.org).
Compares with data_loader.dataset_fingerprint in D4_step1000_train_rank0.decoded.json.
Run: uv run python -I results/E3/audit/D8_fingerprint.py  (no network)."""
import hashlib
import json
import os
from pathlib import Path

A = Path(__file__).resolve().parent
cfg = json.loads((A / "D3_step1000_config.json").read_text())
ds = cfg["dataset"]
heads = json.loads((A / "D7_official_mix_heads.json").read_text())
dp = [l.strip() for l in (A / "D3_step1000_data_paths.txt").read_text().splitlines() if l.strip()]
state = json.loads((A / "D4_step1000_train_rank0.decoded.json").read_text())
target = state["data_loader"]["dataset_fingerprint"]

assert len(heads) == len(dp) == 1016 and all(h["status"] == 200 for h in heads)
heads.sort(key=lambda h: h["i"])
assert [os.path.basename(h["url"]) for h in heads] == [os.path.basename(p) for p in dp]
sizes = [int(h["headers"]["Content-Length"]) for h in heads]

fields = {  # NumpyFSLDataset.fingerprint_fields order
    "vocab_size": ds["tokenizer"]["vocab_size"],
    "pad_token_id": ds["tokenizer"]["pad_token_id"],
    "eos_token_id": ds["tokenizer"]["eos_token_id"],
    "dtype": "<class 'numpy.uint32'>",  # get_dtype(): smallest uint holding vocab_size-1=100277
    "max_target_sequence_length": ds["max_target_sequence_length"],
    "bos_token_id": ds["tokenizer"].get("bos_token_id"),  # absent in config -> None
}
h = hashlib.sha256()
h.update(b"class=NumpyFSLDataset")
for k, v in fields.items():
    h.update(f"{k}={v},".encode())
for p, s in zip(dp, sizes):
    h.update(f"path={os.path.basename(p)},size={s},".encode())
fp = h.hexdigest()
print("fields:", fields)
print("n files:", len(sizes), "total bytes:", sum(sizes), "tokens (bytes/4, uint32):", sum(sizes) // 4)
print("recomputed fingerprint:", fp)
print("trainer-state fingerprint:", target)
print("MATCH" if fp == target else "MISMATCH")
