import json, re, datetime as dt
A='results/E3/audit/'
print('# Part A analysis, generated', dt.datetime.now(dt.UTC).isoformat(timespec='seconds'))
d=json.load(open(A+'A1_refs.json'))
br={b['name']:b['targetCommit'] for b in d['branches']}
exp={'stage1-step1000':'d82e70ec28daa79d25405cd128e127c52bb10516','stage1-step2000':'6a238f66840f4db2deff7049b12fbc608267e14f','stage1-step3000':'9de48909b402a200848e5cba488dbfd04b6227a3','stage1-step5000':'825e84ed82eb78b1b395d0aa0164378f64544f92','stage1-step10000':'7717822de388435b332d8c4a7ab7d88412fff939','stage1-step1413814':'373bad25002f1624757a73235c5ca844c6375c25'}
for n,c in exp.items(): print(f'A1 {n}: refs targetCommit={br.get(n)} prereg={c} match={br.get(n)==c}')
pat=re.compile(r'^stage1-step(\d+)$')
s1=[n for n in br if n.startswith('stage1-')]
steps=sorted(int(pat.match(n).group(1)) for n in s1 if pat.match(n))
print(f'A1 total branches={len(br)}; stage1-* branches={len(s1)}; all match stage1-step<N>: {len(steps)==len(s1)}')
m=[s for s in steps if s%1000==0]
miss=[s for s in range(0,1413001,1000) if s not in set(steps)]
print(f'A1 1000-multiples present={len(m)} (0..{max(m)}), missing 1000-multiples={len(miss)}; extra non-1000 steps={[s for s in steps if s%1000]}')
from emx_reg import pre
print(f'A1 registry STAGES["pre"]["revision"]={pre} == stage1-step1413814 target: {pre==br["stage1-step1413814"]}')
trees={}
for n in ['1413814','1000','2000','3000','5000','10000']:
    t=json.load(open(A+f'A2_tree_step{n}.json'))
    trees[n]={e['path']:e for e in t if e['type']=='file'}
for n,f in trees.items():
    st=sorted(p for p in f if p.endswith('.safetensors'))
    tot=sum(f[p]['lfs']['size'] for p in st)
    print(f'A2 step{n}: {len(st)} safetensors shards, LFS total {tot} bytes = {tot/1e9:.3f} GB (decimal) = {tot/2**30:.3f} GiB; shard lfs sha256 prefixes {[f[p]["lfs"]["oid"][:12] for p in st]}')
R=trees['1413814']
for n in ['1000','2000','3000','5000','10000']:
    for p in sorted(R):
        if p.endswith('.safetensors') or p in ('README.md','olmo3-logo'): continue
        a=trees[n].get(p,{}).get('oid'); b=R[p]['oid']
        print(f'A3 step{n} {p}: oid {a} {"== ref" if a==b else "!= ref "+b}')
