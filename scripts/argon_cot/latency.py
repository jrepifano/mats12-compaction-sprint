"""Per-call latency from agy session databases: request time (planner step 5.1) to the inferred first-response (5.6) and completion (5.7) times."""
import sqlite3,glob,sys
sys.path.insert(0, __import__('os').path.dirname(__file__))
from pb import parse
for pat in sys.argv[1:]:
    a=[];b=[];sig=[]
    dbs=sorted(set(glob.glob(pat,recursive=True)))[:40]
    for db in dbs:
        for (p,) in sqlite3.connect(db).execute('select step_payload from steps where step_type=15'):
            d={};
            for k,ty,v in parse(p) or []:
                if ty=='v' and k in('/5/1/1','/5/1/2','/5/6/1','/5/6/2','/5/7/1','/5/7/2','/5/9/3'): d[k]=v
                if k=='/20/7/7/2/1': sig.append(v)
            if all(k in d for k in('/5/1/1','/5/6/1','/5/7/1')):
                t0=d['/5/1/1']+d['/5/1/2']/1e9; t1=d['/5/6/1']+d['/5/6/2']/1e9; t2=d['/5/7/1']+d['/5/7/2']/1e9
                a.append(t1-t0); b.append((t2-t0, d.get('/5/9/3',0)))
    a.sort(); q=lambda x,p:x[int(p*(len(x)-1))]
    print(pat.split('s2/')[1][:50],'dbs',len(dbs),'calls',len(a),'request->first response (5.6) p10/50/90',[round(q(a,p),1) for p in(.1,.5,.9)],
      'request->completion (5.7) p10/50/90',[round(q(sorted(x for x,_ in b),p),1) for p in(.1,.5,.9)],'blob lens',sorted(set(sig))[:5])
