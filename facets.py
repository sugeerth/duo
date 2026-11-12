"""Per-run facets, a 2-D map and clusters across many runs: the "what keeps happening" layer.

The 2025-2026 observability tools converged on one recipe (Braintrust Topics, Datadog Patterns, LangSmith Insights,
Docent, Anthropic's Clio): extract facets per trace, embed, reduce to 2-D, cluster, label, and show per-cluster
metrics that link back to the runs. This is the deterministic version over data Duo already records, so it needs no
model calls and every label is explainable:

- facets per run: share of each action type, length, tool-failure rate, repeated-call share, loops, the longest
  stall, regressions, progress, reward-hacking signals, outcome (judge score, episode success);
- an embedding = the standardized facet vector; a 2-D map = its first two principal components;
- clusters = k-means (k-means++ seeding, fixed seed) with k chosen by silhouette in 2..8;
- each cluster is labelled by the facets that most set it apart (z-score of the cluster mean), and carries count,
  models, pass rate, mean length and time, "NEW" when most of it is from the latest day, and "no eval coverage"
  when none of its runs has a judge score or an episode outcome.

`table(store)` returns one row per run, queryable like Braintrust's facet columns; `snapshot(store)` adds the map.
"""
import json
import math
import random
from collections import Counter, defaultdict

import analysis
import longrun

TYPES=('FILE_READ','FILE_WRITE','SEARCH','COMMAND','FETCH','OTHER')
FEATURES=['read','write','search','command','fetch','other','log_steps','fail_rate','repeat_share','loops','stall_log','regressions','progress','hacks']
WORDS={'read':'read-heavy','write':'edit-heavy','search':'search-heavy','command':'command-heavy','fetch':'web-heavy','other':'mixed tools',
       'log_steps':'long','fail_rate':'error-prone','repeat_share':'repetitive','loops':'looping','stall_log':'stalling','regressions':'regressing',
       'progress':'making progress','hacks':'touching tests/graders'}
LOW={'log_steps':'short','fail_rate':'clean','repeat_share':'varied','progress':'no progress'}

def facets(steps):
    """Facet dict for one run's tool steps ({'tool','target','ok','at','seconds','output'?,'input'?})."""
    n=len(steps);types=Counter();seen=set();rep=0;st=[]
    for s in steps:
        k=analysis.action_type({'kind':'tool','tool':s.get('tool'),'target':s.get('target')})
        types[k if k in TYPES else 'OTHER']+=1
        key=(s.get('tool'),str(s.get('target'))[:120])
        if key in seen:rep+=1
        seen.add(key);st.append({**s,'kind':'tool','type':k,'key':analysis.target_key(s,k),'input':s.get('input') or ''})
    rd=longrun.read([s for s in steps if s.get('at') is not None]) or {'loops':[],'stall':None,'regressions':0,'progress':0}
    stall=(rd.get('stall') or {}).get('work') or 0
    return {'read':types['FILE_READ']/max(1,n),'write':types['FILE_WRITE']/max(1,n),'search':types['SEARCH']/max(1,n),'command':types['COMMAND']/max(1,n),
            'fetch':types['FETCH']/max(1,n),'other':types['OTHER']/max(1,n),'log_steps':math.log1p(n),
            'fail_rate':sum(s.get('ok') is False for s in steps)/max(1,n),'repeat_share':rep/max(1,n),'loops':len(rd.get('loops',[])),
            'stall_log':math.log1p(stall),'regressions':rd.get('regressions',0),'progress':rd.get('progress',0),'hacks':len(analysis.hack_signals(st)),
            'steps':n}

# ------------------------------------------------------------------ small linear algebra (no numpy)
def standardize(X):
    m=[sum(c)/len(c) for c in zip(*X)];sd=[(sum((v-mu)**2 for v in c)/max(1,len(c)-1))**.5 or 1.0 for c,mu in zip(zip(*X),m)]
    return [[(v-mu)/s for v,mu,s in zip(row,m,sd)] for row in X],m,sd

def pca2(Z,iters=200):
    d=len(Z[0]);C=[[sum(r[i]*r[j] for r in Z)/max(1,len(Z)-1) for j in range(d)] for i in range(d)];comps=[]
    for c in range(2):
        v=[1.0/(i+1) for i in range(d)]
        for _ in range(iters):
            w=[sum(C[i][j]*v[j] for j in range(d)) for i in range(d)]
            for u in comps:
                dot=sum(a*b for a,b in zip(w,u));w=[a-dot*b for a,b in zip(w,u)]
            nrm=sum(a*a for a in w)**.5 or 1.0;v=[a/nrm for a in w]
        comps.append(v)
    return [[sum(a*b for a,b in zip(r,u)) for u in comps] for r in Z],comps

def dist2(a,b):return sum((x-y)**2 for x,y in zip(a,b))

def kmeans(Z,k,seed=0,iters=60):
    rng=random.Random(seed);cent=[Z[rng.randrange(len(Z))]]
    while len(cent)<k:
        d=[min(dist2(z,c) for c in cent) for z in Z];tot=sum(d)
        if not tot:break
        r=rng.random()*tot;acc=0
        for z,dd in zip(Z,d):
            acc+=dd
            if acc>=r:cent.append(z);break
    lab=[0]*len(Z)
    for _ in range(iters):
        new=[min(range(len(cent)),key=lambda j:dist2(z,cent[j])) for z in Z]
        if new==lab and _:break
        lab=new
        for j in range(len(cent)):
            mem=[z for z,l in zip(Z,lab) if l==j]
            if mem:cent[j]=[sum(c)/len(mem) for c in zip(*mem)]
    return lab

def silhouette(Z,lab):
    if len(set(lab))<2:return -1
    by=defaultdict(list)
    for z,l in zip(Z,lab):by[l].append(z)
    s=0
    for z,l in zip(Z,lab):
        own=by[l];a=sum(dist2(z,o)**.5 for o in own)/max(1,len(own)-1)
        b=min(sum(dist2(z,o)**.5 for o in by[m])/len(by[m]) for m in by if m!=l)
        s+=(b-a)/max(a,b) if max(a,b) else 0
    return s/len(Z)

def cluster(rows,kmax=8):
    """rows: [{'features':{...}, 'score'?, 'success'?, 'provider', 'ts', ...}] -> rows with x, y, cluster; clusters."""
    if len(rows)<4:
        for r in rows:r.update(x=0.0,y=0.0,cluster=0)
        return rows,[]
    X=[[r['features'][f] for f in FEATURES] for r in rows];Z,mu,sd=standardize(X);P,_=pca2(Z)
    sample=Z if len(Z)<=400 else [Z[i] for i in range(0,len(Z),max(1,len(Z)//400))]
    best=(-2,2,None)
    for k in range(2,min(kmax,len(Z)//3)+1):
        lab=kmeans(Z,k);idx=list(range(0,len(Z),max(1,len(Z)//400)))
        s=silhouette(sample,[lab[i] for i in idx]) if len(Z)>400 else silhouette(Z,lab)
        if s>best[0]+1e-9:best=(s,k,lab)
    lab=best[2] or [0]*len(Z)
    for r,p,l in zip(rows,P,lab):r.update(x=round(p[0],4),y=round(p[1],4),cluster=l)
    latest=max(r['ts'] or 0 for r in rows);clusters=[]
    for c in sorted(set(lab)):
        mem=[r for r in rows if r['cluster']==c];zs=[z for z,l in zip(Z,lab) if l==c]
        zmean={f:sum(z[i] for z in zs)/len(zs) for i,f in enumerate(FEATURES)}
        top=sorted(zmean.items(),key=lambda kv:-abs(kv[1]))[:3]
        words=[WORDS[f] if v>0 else LOW.get(f) for f,v in top if abs(v)>=.5 and (v>0 or f in LOW)]
        outcomes=[r['success'] for r in mem if r.get('success') is not None]
        scores=[r['score'] for r in mem if r.get('score') is not None]
        recent=sum((latest-(r['ts'] or 0))<86400 for r in mem)
        clusters.append({'id':c,'n':len(mem),'label':', '.join(dict.fromkeys(w for w in words if w)) or 'typical runs',
                         'top':[{'facet':f,'z':round(v,2)} for f,v in top],'models':dict(Counter(r['provider'] for r in mem)),
                         'pass_rate':round(sum(outcomes)/len(outcomes),3) if outcomes else None,'mean_score':round(sum(scores)/len(scores),2) if scores else None,
                         'mean_steps':round(sum(r['features']['log_steps'] for r in mem)/len(mem),3),
                         'mean_steps_n':round(sum(r['steps'] for r in mem)/len(mem),1),'mean_seconds':round(sum(r.get('seconds') or 0 for r in mem)/len(mem),1),
                         'new':recent>len(mem)/2 and recent<len(rows)/2,'uncovered':not outcomes and not scores,
                         'example':mem[0]['id']})
    return rows,{'k':best[1],'silhouette':round(best[0],3),'clusters':clusters}

# ------------------------------------------------------------------ from the trace DB
def table(store,limit=2000):
    """One facet row per stored agent run (all sessions)."""
    store.flush(3)
    with store.lock,store.connect() as db:
        rows=db.execute("SELECT session_id,id,ts,type,provider,span_id,data FROM events WHERE type IN ('span.start','tool.started','tool.completed','judge.result','env.return','span.end') ORDER BY session_id,id").fetchall()
    runs={};pending={};score=defaultdict(list);ret={};end={}
    for sess,i,ts,kind,prov,span,data in rows:
        d=json.loads(data)
        if kind=='span.start' and d.get('span_kind')=='agent' and d.get('role') not in ('synthesis','reviewer') and span:
            runs[span]={'id':span,'session':sess,'turn':d.get('parent_id'),'provider':prov,'role':d.get('role'),'model':d.get('model'),'ts':ts,'steps':[],'mode':d.get('mode')}
        elif kind=='tool.started' and span in runs:
            st={'ref':i,'tool':d.get('tool'),'target':d.get('detail') or '','ok':None,'at':ts,'seconds':0.0};runs[span]['steps'].append(st);pending[(span,d.get('item_id') or d.get('tool'))]=st
        elif kind=='tool.completed':
            st=pending.pop((span,d.get('item_id') or d.get('tool')),None)
            if st:st['ok']=d.get('passed') is not False;st['seconds']=max(0.0,ts-st['at'])
        elif kind=='judge.result' and d.get('turn_id') and d.get('score') is not None:score[(d['turn_id'],prov)].append(float(d['score']))
        elif kind=='env.return' and d.get('turn_id'):ret[(d['turn_id'],prov)]=bool(d.get('passed'))
        elif kind=='span.end' and span in runs:end[span]=d.get('duration_ms')
    out=[]
    for r in list(runs.values())[-limit:]:
        steps=[s for s in r['steps'] if s['ok'] is not None]
        if not steps:continue
        f=facets(steps);sc=score.get((r['turn'],r['provider']))
        out.append({'id':r['id'],'session':r['session'],'turn':r['turn'],'provider':r['provider'],'model':r['model'],'mode':r['mode'],'ts':r['ts'],
                    'steps':f.pop('steps'),'seconds':round((end.get(r['id']) or 0)/1000,1),'features':{k:round(v,4) for k,v in f.items()},
                    'score':round(sum(sc)/len(sc),2) if sc else None,
                    'success':ret.get((r['turn'],r['provider'])) if (r['turn'],r['provider']) in ret else (sum(sc)/len(sc)>=3.5 if sc else None)})
    return out

def snapshot(store):
    if not store:return {'runs':[],'clusters':[],'k':0}
    rows,info=cluster(table(store))
    return {'runs':rows,**(info or {'k':0,'silhouette':None,'clusters':[]}),'features':FEATURES}
