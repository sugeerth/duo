"""Long-term insight: how each model changes across turns, sessions and days. Deterministic statistics over the trace DB.

One record per (turn, model): when, how long the model's runs took, tool calls and failures, the judge score, the
head-to-head result, and the long-run reading (loops, stalls). Over those records:

- trend: Mann-Kendall test on turn order plus a Theil-Sen slope. Reported as the median of the first vs the last
  third, so the claim is in the metric's own units.
- recent shift: the last RECENT turns vs every turn before them (Mann-Whitney U, normal approximation).
- head-to-head drift: win share in the first vs the second half of decided turns.
- long-run habits: share of turns with a loop over hours, and stall time.
All p-values in one response go through Benjamini-Hochberg, and an insight is stated only when its adjusted
q < ALPHA and n >= MIN_N. Everything else is reported as "no clear change", with n, never as a finding.
"""
from collections import defaultdict
import json
import math

MIN_N=8
RECENT=10
ALPHA=0.05
PRIMARY=('responder','reader','writer')
METRICS={  # key: (label, unit, better direction: +1 higher is better, -1 lower is better, 0 neutral)
    'score':('judge score','/5',1),'seconds':('time per turn','s',-1),'fail_rate':('tool failure rate','%',-1),'calls':('tool calls per turn','',0),'ret':('episode return','',1)}

def _phi(z):return 0.5*(1+math.erf(z/math.sqrt(2)))

def mann_kendall(xs):
    """Two-sided Mann-Kendall trend test with tie correction: (S, p)."""
    n=len(xs)
    if n<3:return 0,1.0
    s=sum((xs[j]>xs[i])-(xs[j]<xs[i]) for i in range(n) for j in range(i+1,n))
    ties=defaultdict(int)
    for x in xs:ties[x]+=1
    var=(n*(n-1)*(2*n+5)-sum(t*(t-1)*(2*t+5) for t in ties.values()))/18
    if var<=0:return s,1.0
    z=(s-1)/math.sqrt(var) if s>0 else (s+1)/math.sqrt(var) if s<0 else 0
    return s,2*(1-_phi(abs(z)))

def theil_sen(xs):
    sl=sorted((xs[j]-xs[i])/(j-i) for i in range(len(xs)) for j in range(i+1,len(xs)))
    return sl[len(sl)//2] if sl else 0.0

def mann_whitney(a,b):
    """Two-sided Mann-Whitney U with normal approximation and tie correction."""
    n1,n2=len(a),len(b)
    if not n1 or not n2:return 1.0
    allv=sorted([(v,0) for v in a]+[(v,1) for v in b]);ranks=[0.0]*len(allv);i=0;tie=0
    while i<len(allv):
        j=i
        while j+1<len(allv) and allv[j+1][0]==allv[i][0]:j+=1
        r=(i+j)/2+1
        for k in range(i,j+1):ranks[k]=r
        t=j-i+1;tie+=t**3-t;i=j+1
    r1=sum(r for r,(v,g) in zip(ranks,allv) if g==0);u=r1-n1*(n1+1)/2;n=n1+n2
    var=n1*n2/12*((n+1)-tie/(n*(n-1)))
    if var<=0:return 1.0
    return 2*(1-_phi(abs(u-n1*n2/2)/math.sqrt(var)))

def bh(ps):
    """Benjamini-Hochberg adjusted q-values, same order as ps."""
    m=len(ps);order=sorted(range(m),key=lambda i:ps[i]);q=[1.0]*m;prev=1.0
    for rank,i in reversed(list(enumerate(order,1))):prev=min(prev,ps[i]*m/rank);q[i]=prev
    return q

def median(v):
    v=sorted(v);n=len(v)
    return None if not n else v[n//2] if n%2 else (v[n//2-1]+v[n//2])/2

def records(store,limit=3000):
    """[{turn, session, ts, kind, provider, seconds, calls, fails, score, result, loops, loop_s, stall_s}] oldest first."""
    store.flush(3)
    with store.lock,store.connect() as db:
        spans=db.execute("SELECT span_id,session_id,parent_id,kind,provider,role,mode,start,end FROM spans WHERE kind IN ('turn','agent')").fetchall()
        rows=db.execute("SELECT session_id,provider,span_id,type,data FROM events WHERE type IN ('span.end','judge.result','judge.verdict','judge.verdict_override','analysis.longrun','env.return')").fetchall()
    turns={};agents=defaultdict(list)
    for sid,sess,parent,kind,prov,role,mode,start,end in spans:
        if kind=='turn':turns[sid]={'turn':sid,'session':sess,'ts':start,'kind':'respond' if (mode or '').startswith('respond') else 'write' if 'write' in (mode or '') else 'read'}
        elif (role in PRIMARY or role=='policy') and parent and start and end:agents[parent].append((prov,sid,end-start))
    ends={};scores=defaultdict(list);verdicts=defaultdict(set);override={};lr={};rets={}
    for sess,prov,span,kind,data in rows:
        d=json.loads(data)
        if kind=='span.end' and span:ends[span]=(d.get('tools') or 0,d.get('tool_errors') or 0)
        elif kind=='judge.result' and d.get('turn_id') and d.get('score') is not None:scores[(d['turn_id'],prov)].append(float(d['score']))
        elif kind=='judge.verdict' and d.get('turn_id'):verdicts[d['turn_id']].add(d.get('verdict'))
        elif kind=='judge.verdict_override' and d.get('turn_id'):override[d['turn_id']]=d.get('verdict')
        elif kind=='analysis.longrun' and d.get('turn_id'):lr[(d['turn_id'],prov)]=d.get('data') or {}
        elif kind=='env.return' and d.get('turn_id') and d.get('score') is not None:rets[(d['turn_id'],prov)]=float(d['score'])
    out=[]
    for tid,t in turns.items():
        by=defaultdict(list)
        for prov,sid,sec in agents.get(tid,[]):by[prov].append((sid,sec))
        both=len(by)==2;v=verdicts.get(tid,set());w=override.get(tid) or (next(iter(v)) if len(v)==1 else 'split' if v else None)
        for prov,rs in by.items():
            calls=sum(ends.get(s,(0,0))[0] for s,_ in rs);fails=sum(ends.get(s,(0,0))[1] for s,_ in rs);sc=scores.get((tid,prov));L=lr.get((tid,prov),{})
            res=None
            if both and w in ('codex','claude'):res='win' if w==prov else 'loss'
            elif both and w in ('tie','split'):res='tie'
            out.append({**t,'provider':prov,'seconds':round(sum(s for _,s in rs),2),'calls':calls,'fails':fails,'fail_rate':(fails/calls if calls else None),
                        'score':(sum(sc)/len(sc) if sc else None),'result':res,'loops':len(L.get('loops') or []),'loop_s':sum(x.get('wall',0) for x in L.get('loops') or []),
                        'stall_s':(L.get('stall') or {}).get('wall'),'progress':L.get('progress'),'ret':rets.get((tid,prov))})
    out.sort(key=lambda r:r['ts'] or 0)
    return out[-limit:]

def fmtv(key,v):
    if v is None:return '–'
    if key=='fail_rate':return f'{100*v:.0f}%'
    if key=='seconds':return f'{v:.0f} s' if v<120 else f'{v/60:.1f} min'
    if key=='score':return f'{v:.1f}/5'
    return f'{v:.1f}'

def insight(recs):
    """Trends, recent shifts and drift per model; only BH-significant ones become statements."""
    by=defaultdict(list)
    for r in recs:by[r['provider']].append(r)
    tests=[]
    for prov,rs in by.items():
        for key,(label,unit,better) in METRICS.items():
            xs=[r.get(key) for r in rs if r.get(key) is not None]
            if len(xs)>=MIN_N:
                s,p=mann_kendall(xs);k=max(1,len(xs)//3);a,b=median(xs[:k]),median(xs[-k:])
                up=s>0
                tests.append({'kind':'trend','provider':prov,'metric':key,'n':len(xs),'p':p,'slope':theil_sen(xs),'from':a,'to':b,'up':up,
                              'good':None if not better else (up==(better>0)),
                              'text':f"{prov.title()}'s {label} is {'rising' if up else 'falling'}: median {fmtv(key,a)} in its first {k} turns, {fmtv(key,b)} in its last {k} (n={len(xs)})"})
            if len(xs)>=RECENT+MIN_N:
                old,new=xs[:-RECENT],xs[-RECENT:];p=mann_whitney(old,new);a,b=median(old),median(new);up=b>a
                tests.append({'kind':'shift','provider':prov,'metric':key,'n':len(xs),'p':p,'from':a,'to':b,'up':up,'good':None if not better else (up==(better>0)),
                              'text':f"{prov.title()}'s {label} changed recently: median {fmtv(key,b)} over the last {RECENT} turns vs {fmtv(key,a)} before (n={len(xs)})"})
        res=[r['result'] for r in rs if r['result'] in ('win','loss')]
        if len(res)>=2*MIN_N and prov=='claude':
            h=len(res)//2;a=res[:h].count('win')/h;b=res[h:].count('win')/(len(res)-h)
            p=mann_whitney([1 if x=='win' else 0 for x in res[:h]],[1 if x=='win' else 0 for x in res[h:]])
            tests.append({'kind':'h2h','provider':'claude','metric':'wins','n':len(res),'p':p,'from':a,'to':b,'up':b>a,'good':None,
                          'text':f"Head-to-head is drifting {'toward Claude' if b>a else 'toward Codex'}: Claude won {100*a:.0f}% of the first {h} decided turns and {100*b:.0f}% of the last {len(res)-h}"})
    q=bh([t['p'] for t in tests])
    for t,qq in zip(tests,q):t['q']=qq;t['significant']=qq<ALPHA
    found=sorted([t for t in tests if t['significant']],key=lambda t:t['q'])
    habits={}
    for prov,rs in by.items():
        lr=[r for r in rs if r['progress'] is not None]
        if lr:habits[prov]={'turns':len(lr),'loop_turns':sum(1 for r in lr if r['loops']),'loop_s':round(sum(r['loop_s'] for r in lr),1),
                            'stall_s':median([r['stall_s'] for r in lr if r['stall_s']]) }
    days=sorted({int((r['ts'] or 0)//86400) for r in recs});sess=len({r['session'] for r in recs})
    return {'n':len(recs),'turns':len({r['turn'] for r in recs}),'sessions':sess,'days':len(days),'first':recs[0]['ts'] if recs else None,'last':recs[-1]['ts'] if recs else None,
            'findings':[{k:(round(v,4) if isinstance(v,float) else v) for k,v in t.items()} for t in found],
            'tested':len(tests),'quiet':[{'provider':t['provider'],'metric':t['metric'],'kind':t['kind'],'n':t['n'],'q':round(t['q'],3)} for t in tests if not t['significant']],
            'habits':habits,'rules':{'min_n':MIN_N,'recent':RECENT,'alpha':ALPHA,'correction':'Benjamini-Hochberg over every test in this response'}}

def snapshot(store):
    if not store:return {'records':[],'insight':insight([])}
    recs=records(store)
    keep=('turn','session','ts','kind','provider','seconds','calls','fails','fail_rate','score','ret','result','loops','loop_s','stall_s','progress')
    return {'records':[{k:r[k] for k in keep} for r in recs],'insight':insight(recs)}
