"""Read a long run at the scale it ran (rules from AgentDiff's longrun reading). Deterministic, no model calls.

A run that lasts an hour is mostly bursts of calls with idle between them, and its loops last bursts, not steps.
One linear pass over the tool steps of one model's run(s) in a turn:

- sessions: split by idle of at least SESSION_GAP (the agent stopped, or waited on a person).
- bursts: split inside a session by a gap the run sets itself: Otsu's threshold on the log of the gaps
  (the few seconds between calls vs the minutes between bursts), clamped to 20 s .. 30 min. When the gaps show
  no two kinds, ten times the median gap.
- progress: a check (test/lint/typecheck command) passing where its previous run did not, or passing more cases
  than it ever had ("4 passed" then "6 passed"). The first pass of a check counts. An edit is not progress.
  regression: a check failing where its previous run passed.
- the stall: the longest stretch between two progress events (run start and end are boundaries), as wall time,
  working time (time inside bursts) and calls. Open when the run ended without progress after it.
- a loop over hours: 2+ bursts in a row that each share >= 60% of their calls (tool + target) with an earlier
  burst, with no progress inside. Idle inside it is part of it.
- where to look first: the start of the longest loop, else the stall start if it worked >= 10 min, else the
  first regression.
- pace: mean seconds per call (idle excluded) in the first vs the last third of the calls.
Every number carries the rule that produced it in `basis`.
"""
import math
import re

from analysis import TEST_CMD, unwrap

SESSION_GAP=1800.0
BURST_MIN,BURST_MAX=20.0,1800.0
LOOP_SHARE=0.6
PASSED=re.compile(r'(\d+) passed')

def check_key(target):
    """The check itself: without a `cd x &&` before it or a `2>&1 | tail` after it."""
    cmd=unwrap(target)
    parts=[p.strip() for p in re.split(r'&&|;',cmd) if p.strip()]
    hit=next((p for p in parts if TEST_CMD.search(p)),cmd)
    hit=re.split(r'\s*(?:\d?>&\d|\|)',hit)[0].strip()
    return ' '.join(hit.split())[:120]

def otsu(values):
    """Threshold that best splits values into two groups (max between-class variance); (threshold, separation 0..1)."""
    v=sorted(values);n=len(v)
    if n<4 or v[0]==v[-1]:return None,0.0
    total=sum(v);mean=total/n;var=sum((x-mean)**2 for x in v)/n
    best,cut,acc=0.0,None,0.0
    for i in range(1,n):
        acc+=v[i-1];w0=i/n;w1=1-w0;m0=acc/i;m1=(total-acc)/(n-i)
        b=w0*w1*(m0-m1)**2
        if b>best and v[i]>v[i-1]:best,cut=b,(v[i-1]+v[i])/2
    return cut,(best/var if var else 0.0)

def burst_gap(gaps):
    pos=[g for g in gaps if g>0]
    if not pos:return BURST_MAX,'no gaps between calls: one burst per session'
    cut,sep=otsu([math.log(g) for g in pos])
    if cut is not None and sep>=0.5:
        t=min(BURST_MAX,max(BURST_MIN,math.exp(cut)));return t,f'Otsu split of log gaps at {t:.0f} s (separation {sep:.2f})'
    med=sorted(pos)[len(pos)//2];t=min(BURST_MAX,max(BURST_MIN,10*med))
    return t,f'gaps show no two kinds: 10 x median gap = {t:.0f} s'

def read(steps,now=None):
    """steps: tool steps in time order, each {'ref','tool','target','ok','at','seconds','output'?}. Returns the reading."""
    calls=sorted([s for s in steps if s.get('at') is not None],key=lambda s:s['at'])
    if not calls:return None
    end=lambda s:s['at']+(s.get('seconds') or 0)
    gaps=[max(0.0,calls[i]['at']-end(calls[i-1])) for i in range(1,len(calls))]
    gap,gap_basis=burst_gap([g for g in gaps if g<SESSION_GAP])
    sessions=[];bursts=[]
    for i,c in enumerate(calls):
        g=gaps[i-1] if i else None
        if g is None or g>=SESSION_GAP:sessions.append({'start':c['at'],'end':end(c),'calls':0,'bursts':0,'idle_before':g})
        if g is None or g>=gap:bursts.append({'start':c['at'],'end':end(c),'calls':[],'session':len(sessions)-1,'idle_before':g})
        b=bursts[-1];b['calls'].append(c);b['end']=max(b['end'],end(c))
        s=sessions[-1];s['calls']+=1;s['end']=max(s['end'],end(c))
    for b in bursts:sessions[b['session']]['bursts']+=1
    # checks, progress, regressions
    last={};best={};checks=[];progress=[];regressions=[]
    for i,c in enumerate(calls):
        if not TEST_CMD.search(unwrap(c.get('target'))):continue
        key=check_key(c.get('target'));ok=c.get('ok') is not False
        m=PASSED.search(str(c.get('output') or ''));n=int(m.group(1)) if m else None
        ev={'ref':c['ref'],'at':c['at'],'key':key,'ok':ok,'passed':n,'index':i}
        if ok and (last.get(key) is not True or (n is not None and n>best.get(key,-1))):ev['progress']=True;progress.append(ev)
        if not ok and last.get(key) is True:ev['regression']=True;regressions.append(ev)
        last[key]=ok
        if ok and n is not None:best[key]=max(best.get(key,-1),n)
        checks.append(ev)
    pidx={e['index'] for e in progress};ridx={e['index'] for e in regressions};fidx={e['index'] for e in checks if not e['ok']}
    pos={id(c):i for i,c in enumerate(calls)}
    for b in bursts:
        ix=[pos[id(c)] for c in b['calls']]
        b['progress']=any(i in pidx for i in ix);b['failing']=any(i in fidx for i in ix);b['regression']=any(i in ridx for i in ix)
        b['keys']={(c.get('tool'),check_key(c.get('target')) if TEST_CMD.search(unwrap(c.get('target'))) else unwrap(c.get('target'))) for c in b['calls']}
    # loops over hours
    loops=[];cur=None
    for k,b in enumerate(bursts):
        seen=set().union(*(x['keys'] for x in bursts[:k])) if k else set()
        share=len(b['keys']&seen)/len(b['keys']) if b['keys'] else 0
        if share>=LOOP_SHARE and not b['progress']:
            b['repeat_share']=round(share,2)
            if cur is None:cur={'first':k,'last':k}
            else:cur['last']=k
        else:
            if cur and cur['last']>cur['first']:loops.append(cur)
            cur=None
    if cur and cur['last']>cur['first']:loops.append(cur)
    for L in loops:
        bs=bursts[L['first']:L['last']+1]
        L.update(start=bs[0]['start'],end=bs[-1]['end'],bursts=len(bs),calls=sum(len(b['calls']) for b in bs),ref=bs[0]['calls'][0]['ref'])
        L['wall']=round(L['end']-L['start'],1)
        for b in bs:b['loop']=True
    for b in bursts:b['status']='progress' if b['progress'] else 'loop' if b.get('loop') else 'failing' if b['failing'] or b['regression'] else 'filler'
    # the stall: longest stretch between progress events
    t_end=max(end(c) for c in calls);marks=[(calls[0]['at'],0)]+[(e['at'],e['index']) for e in progress]
    open_end=not progress or progress[-1]['index']<len(calls)-1
    if open_end:marks.append((t_end,len(calls)))
    stall=None
    for (a,ia),(b_,ib) in zip(marks,marks[1:]):
        work=sum(max(0.0,min(b['end'],b_)-max(b['start'],a)) for b in bursts if b['end']>a and b['start']<b_)
        cand={'start':a,'end':b_,'wall':round(b_-a,1),'work':round(work,1),'calls':ib-ia,'open':open_end and b_==t_end,'ref':calls[min(ia,len(calls)-1)]['ref']}
        if stall is None or cand['wall']>stall['wall']:stall=cand
    # where to look first
    look=None
    if loops:
        L=max(loops,key=lambda L:L['wall']);look={'ref':L['ref'],'at':L['start'],'why':f"start of a loop: {L['bursts']} bursts repeating earlier calls without progress over {fmt(L['wall'])}"}
    elif stall and stall['work']>=600:look={'ref':stall['ref'],'at':stall['start'],'why':f"start of the stall: {fmt(stall['work'])} of work and {stall['calls']} calls with no check newly passing"}
    elif regressions:look={'ref':regressions[0]['ref'],'at':regressions[0]['at'],'why':f"first regression: {regressions[0]['key']} failed after passing"}
    # pace
    pace=None
    if len(calls)>=9:
        k=len(calls)//3;spc=lambda cs:sum(c.get('seconds') or 0 for c in cs)/len(cs)  # working seconds per call; idle excluded
        a,b=spc(calls[:k]),spc(calls[-k:])
        pace={'first':round(a,1),'last':round(b,1),'ratio':round(b/a,2) if a>0 else None,'slowed':a>0 and b/a>=1.5,'sped_up':a>0 and b/a<=1/1.5}
    span=t_end-calls[0]['at']
    return {'start':calls[0]['at'],'end':t_end,'wall':round(span,1),'calls':len(calls),'running':now is not None and now-t_end<5,
            'sessions':[{k:(round(v,1) if isinstance(v,float) else v) for k,v in s.items()} for s in sessions],
            'bursts':[{'start':b['start'],'end':b['end'],'calls':len(b['calls']),'session':b['session'],'status':b['status'],'idle_before':b['idle_before'],
                       'ref':b['calls'][0]['ref'],'repeat_share':b.get('repeat_share')} for b in bursts],
            'checks':[{k:e[k] for k in ('ref','at','key','ok','passed')}|{'progress':bool(e.get('progress')),'regression':bool(e.get('regression'))} for e in checks],
            'progress':len(progress),'regressions':len(regressions),'loops':[{k:L[k] for k in ('start','end','wall','bursts','calls','ref')} for L in loops],
            'stall':stall,'look_first':look,'pace':pace,
            'basis':{'session_gap':SESSION_GAP,'burst_gap':round(gap,1),'burst_rule':gap_basis,'loop_share':LOOP_SHARE,
                     'progress_rule':'a check passing where its previous run did not, or with more cases passing than ever'}}

def fmt(sec):
    sec=float(sec or 0)
    if sec<60:return f'{sec:.0f} s'
    if sec<3600:return f'{sec/60:.1f} min'
    return f'{sec/3600:.1f} h'

def summary(r):
    """One or two sentences, every number from the reading."""
    if not r:return ''
    parts=[f"{r['calls']} calls over {fmt(r['wall'])} in {len(r['bursts'])} burst{'s'*(len(r['bursts'])!=1)}"+(f" and {len(r['sessions'])} sessions" if len(r['sessions'])>1 else '')]
    if r['checks']:parts.append(f"{r['progress']} step{'s'*(r['progress']!=1)} of progress, {r['regressions']} regression{'s'*(r['regressions']!=1)}")
    else:parts.append('no checks were run, so progress cannot be measured')
    if r['loops']:L=max(r['loops'],key=lambda L:L['wall']);parts.append(f"looped for {fmt(L['wall'])} across {L['bursts']} bursts")
    if r['stall'] and r['checks'] and r['stall']['calls']>=5:parts.append(f"longest stall {fmt(r['stall']['wall'])} ({r['stall']['calls']} calls){' and still open' if r['stall']['open'] else ''}")
    if r['pace'] and (r['pace']['slowed'] or r['pace']['sped_up']):parts.append(f"{'slowed' if r['pace']['slowed'] else 'sped up'} from {r['pace']['first']} to {r['pace']['last']} s per call")
    return '; '.join(parts)+'.'
