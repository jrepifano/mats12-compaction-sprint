"""Output tokens / visible output (stored thinking + content + tool-call JSON, at 3.5 chars per token), per agy call."""
import json,glob,sys,statistics as st
def calls(tf):
    t=[json.loads(l) for l in open(tf)]
    return [x for x in t if x['type']=='PLANNER_RESPONSE']
def vis(x):
    s=(x.get('thinking') or '')+(x.get('content') or '')+json.dumps(x.get('tool_calls') or [],ensure_ascii=False)
    return len(s)
for pat in sys.argv[1:]:
    rs=[];small=[]
    for tf in glob.glob(pat,recursive=True):
        for x in calls(tf):
            v=vis(x); o=x.get('output_tokens') or 0
            if v>0: rs.append(o/(v/3.5))
            if v<400: small.append(o-v/3.5)
    rs.sort(); small.sort()
    q=lambda a,p: a[int(p*(len(a)-1))] if a else None
    print(pat.split('s2/')[1][:60], 'n',len(rs),'ratio out/visible-est p10/50/90', [round(q(rs,p),2) for p in (.1,.5,.9)], 'excess tokens on small calls p50/p90', [round(q(small,p)) for p in (.5,.9)] if small else None)
