"""Fleet: every stored run at once, and which instrument caught each problem.

AgentDiff's long-horizon finding, measured on your own traces: at length the grade is the weakest instrument.
A run that skips a unit and reports success can be graded a pass and caught only by a milestone, a mode or a
security rule. For every (turn, model) run in the trace DB this computes, deterministically and in milliseconds:

  strip       the run as a barcode: its phases (look / write / check / ship / delegate) in order, sized by calls,
              with the long-horizon modes at their position
  instruments which of the instruments fired on it:
                grade       the judges' mean score below GRADE_PASS (only for judged runs)
                milestones  a planned unit never reached
                modes       a named long-horizon failure mode (horizon.py)
                security    a high or critical finding (security.py)
                loops       a loop over bursts (longrun.py)
                guard       the guard blocked a call, or a risky call ran unchecked
  risk        a sort key: how many instruments fired, weighted toward the severe ones

and over all of them the catch matrix: problem runs (any instrument fired) x instruments, with the headline
"N runs had a problem; the grade caught K; M were caught only by something other than the grade".
"""
import time
from collections import defaultdict

import export
import horizon
import longrun
import security

GRADE_PASS=3.5
INSTRUMENTS=('grade','milestones','modes','security','loops','guard')
WEIGHT={'security':3,'guard':3,'modes':2,'milestones':2,'loops':1,'grade':1}
_CACHE={'at':0,'key':None,'data':None}

def strip(tree,steps_n,modes):
    """Phases in order as [kind, calls, failed]; modes as [fraction along the run, mode]."""
    out=[]
    def walk(n):
        if n['kind']=='phase':out.append([n['label'],n['calls'],n['failed']]);return
        for c in n['children']:walk(c)
    walk(tree)
    if not out:out=[['other',tree['calls'],tree['failed']]]
    return out,[[round((m['index'] or 0)/max(1,steps_n-1),4),m['mode']] for m in modes]

def run_row(tid,p,t,meta,guard_hits):
    steps=t['steps'][p]
    if not steps:return None
    hz=horizon.read(steps,t.get('prompt'),t['answers'].get(p))
    sec=security.review(steps);sev=max((f['severity'] for f in sec['findings']),key=lambda v:security.SEV_RANK.get(v,0),default='none')
    rd=longrun.read([s for s in steps if s.get('at') is not None]) if any(s.get('at') for s in steps) else None
    sc=t['scores'].get(p);grade=round(sum(sc)/len(sc),2) if sc else None
    ms=hz['milestones']
    fired={'grade':grade is not None and grade<GRADE_PASS,
           'milestones':ms['source']=='plan' and ms['reached']<ms['total'],
           'modes':bool(hz['modes']),
           'security':security.SEV_RANK.get(sev,0)>=3,
           'loops':bool(rd and rd['loops']),
           'guard':guard_hits>0}
    phases,marks=strip(hz['tree'],len(steps),hz['modes'])
    first=hz['look_first'] or {}
    return {'turn':tid,'provider':p,'session':meta.get('session_id'),'ts':meta.get('start'),'mode':t.get('mode'),
            'prompt':(t.get('prompt') or '')[:140],'steps':len(steps),'failed':sum(s.get('ok') is False for s in steps),
            'grade':grade,'severity':sev,'modes':[m['mode'] for m in hz['modes']],'milestones':[ms['reached'],ms['total'],ms['source']],
            'loops':len(rd['loops']) if rd else 0,'guard':guard_hits,'phases':phases,'marks':marks,
            'fired':[k for k in INSTRUMENTS if fired[k]],'risk':sum(WEIGHT[k] for k in INSTRUMENTS if fired[k]),
            'look_first':first.get('ref'),'why':first.get('why')}

def matrix(rows):
    prob=[r for r in rows if r['fired']]
    judged=[r for r in prob if r['grade'] is not None]
    by={k:sum(k in r['fired'] for r in prob) for k in INSTRUMENTS}
    only={k:sum(r['fired']==[k] for r in prob) for k in INSTRUMENTS}
    passed=[r for r in judged if 'grade' not in r['fired']]
    return {'runs':len(rows),'problems':len(prob),'judged_problems':len(judged),'by_instrument':by,'only':only,
            'graded_pass_but_caught':len(passed),'clean':len(rows)-len(prob),
            'pairs':{f'{a}+{b}':sum(a in r['fired'] or b in r['fired'] for r in prob) for i,a in enumerate(INSTRUMENTS) for b in INSTRUMENTS[i+1:]},
            'headline':headline(len(prob),len(judged),len(passed),by)}

def headline(n,judged,passed,by):
    if not n:return 'No run has a problem any instrument can see.'
    h=f"{n} run{'s' if n!=1 else ''} had a problem."
    if judged:h+=f" {judged} were judged; {passed} of those were graded a pass but caught by other instruments."
    top=sorted((k for k in INSTRUMENTS if k!='grade'),key=lambda k:-by[k])[0]
    return h+f" No single instrument sees all of them: {top}, the strongest besides the grade, catches {by[top]} of {n}."

def snapshot(store,limit=600):
    """All runs in the trace DB (newest first, up to limit) + the catch matrix. Cached for 20 s per DB state."""
    if not store:return {'rows':[],'matrix':matrix([]),'instruments':INSTRUMENTS}
    try:
        with store.lock,store.connect() as db:key=db.execute('SELECT COUNT(*),COALESCE(MAX(ts),0) FROM events').fetchone()
    except Exception:key=None
    if _CACHE['data'] is not None and _CACHE['key']==key and time.time()-_CACHE['at']<20:return _CACHE['data']
    ev,sp,_=export._events(store);T=export.turns(store)
    guard=defaultdict(int)
    for e in ev:
        if e['type']=='guard.blocked' or (e['type']=='guard.check' and e.get('verdict')=='ran_unchecked'):
            s=sp.get(e.get('span_id'));tid=e.get('turn_id') or (s and s.get('parent_id'))
            if tid:guard[(tid,e['provider'])]+=1
    rows=[]
    for tid,t in T.items():
        meta=sp.get(tid) or {}
        for p in t['steps']:
            try:r=run_row(tid,p,t,meta,guard[(tid,p)])
            except Exception:r=None
            if r:rows.append(r)
    rows.sort(key=lambda r:-(r['ts'] or 0));rows=rows[:limit]
    data={'rows':rows,'matrix':matrix(rows),'instruments':INSTRUMENTS,'grade_pass':GRADE_PASS}
    _CACHE.update(at=time.time(),key=key,data=data)
    return data
