import sys, json, gzip, io, os, hashlib
from dotenv import load_dotenv; load_dotenv(sys.argv[2])
from huggingface_hub import hf_hub_download
import zstandard
REPO="allenai/dolma3_mix-6T-1025-7B"; REV="2ca900fbe14e86c5c83d064d9f0882f1c0b8c05b"; out=sys.argv[1]
def load(fn):
    p=hf_hub_download(REPO,fn,repo_type="dataset",revision=REV,local_dir=out)
    raw=open(p,"rb").read()
    data=gzip.decompress(raw) if fn.endswith(".gz") else zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    rows=[json.loads(l) for l in data.decode().splitlines() if l.strip()]
    return {r.get("id"):hashlib.sha256(r.get("text","").encode()).hexdigest() for r in rows}, rows[0].keys()
base="data/olmocr_science_pdfs-art_and_design/"
gz,k1=load(base+"art_design-0009.jsonl.gz"); print("gz keys",list(k1),"n",len(gz))
allz={}
for i in range(0,6):
    z,k2=load(base+f"shard_{i:08d}.jsonl.zst"); allz.update(z)
print("zst keys",list(k2),"n",len(allz))
ids=set(gz)&set(allz); texts=set(gz.values())&set(allz.values())
print("shared ids",len(ids),"shared texts",len(texts))
print("sample gz ids",list(gz)[:2],"sample zst ids",list(allz)[:2])
