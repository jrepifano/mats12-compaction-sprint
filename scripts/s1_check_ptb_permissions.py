import sys, json, random, collections
sys.path.insert(0, "scripts")
from compaction import seal, s1_analysis as A
import s1_audit as AU
reader = lambda name: seal.open_sealed("S1", name)
items, _ = AU.final_rows(reader, "partA")
rows = [r for r in items if A.usable_item(r) and r.get("source") == "posttrainbench" and r.get("type") == "permission"]
lost = [r for r in rows if A.is_lost(r)]
print("ptb permission usable", len(rows), "lost", len(lost))
print("grades", collections.Counter(r.get("grade") for r in rows), "avail", collections.Counter(r.get("availability") for r in rows))
print("records", len({r["rid"] for r in rows}), "items/record", round(len(rows)/len({r['rid'] for r in rows}),1))
print("text counts", collections.Counter(r.get("text") for r in lost).most_common(8))
rng = random.Random(20261023)
for r in rng.sample(lost, 12):
    print("---", r["rid"], r.get("grade"), "|", r.get("text"))
    print("   quote:", (r.get("quote") or "")[:200].replace("\n"," "))
    print("   reason:", str(r.get("reason") or r.get("rationale") or "")[:250].replace("\n"," "))
# compare other sources' permission items
for src in ("frontierml","jacob","swe-chat-v1"):
    t = [r.get("text") for r in items if A.usable_item(r) and r.get("source")==src and r.get("type")=="permission"]
    print(src, "examples:", [x[:80] for x in t[:3]])
import re
src = AU.Sources()
recs = sorted({r["rid"] for r in rows})
rng2 = random.Random(7)
pat = {"internet": re.compile(r"internet|online|web|download", re.I), "packages": re.compile(r"install|package|pip", re.I), "hf cache": re.compile(r"cache", re.I)}
hits = collections.Counter(); n = 0
for rid in rng2.sample(recs, 20):
    r0 = next(r for r in rows if r["rid"] == rid)
    s = src.summary("partA", r0); n += 1
    for k, p in pat.items():
        if p.search(s): hits[k] += 1
print(f"summaries checked {n}; mention by keyword:", dict(hits))
r0 = next(r for r in lost if "nternet" in r["text"])
s = src.summary("partA", r0)
print("example summary lines with keywords:", [l[:160] for l in s.splitlines() if pat["internet"].search(l) or pat["packages"].search(l)][:6])
sharp = {"internet access": re.compile(r"internet access|unrestricted|internet is", re.I),
         "install allowed": re.compile(r"(install\w*[^.\n]{0,60}(allowed|permitted|ok|may))|((allowed|permitted|may|can)[^.\n]{0,40}install)", re.I),
         "hf cache": re.compile(r"(hugging ?face|hf)[ _-]?cache", re.I),
         "restriction kept": re.compile(r"not allowed|forbidden|must not|do not", re.I)}
hits = collections.Counter(); n = 0
for rid in random.Random(7).sample(recs, 20):
    s = src.summary("partA", next(r for r in rows if r["rid"] == rid)); n += 1
    for k, p in sharp.items():
        if p.search(s): hits[k] += 1
print(f"SHARP: summaries {n}:", dict(hits))
