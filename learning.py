"""Self-learning evals, kept in check.

Signals: implicit feedback from the user's next message (correction, re-ask, thanks) and
explicit /rate and /prefer. From them Duo learns which judge dimensions matter to this
user. The judge rubric itself never changes (scores stay comparable over time); learned
weights only re-rank downstream: a user-weighted score, which fixes surface first, and
which lessons are auto-adopted. Every adaptive piece is checked: shrinkage toward uniform
weights until there is evidence, lessons are measured before/after and retired when they
hurt, a cap on lessons, and a health checklist (sample size, task mix, judge mix,
position consistency, judge-user agreement, suggestion diversity).
"""
from collections import defaultdict
import re
import time

import judge as J
import lessons as L

DIMS=list(J.DIMENSIONS)+list(J.PROCESS)
PRIOR=8          # pseudo-signals pulling weights toward uniform until feedback accumulates
LESSON_CAP=8     # per model; keeps prompts short and attributable
AUTO_MIN_TURNS=3 # an auto-lesson needs this many supporting turns...
AUTO_MIN_JUDGES=2  # ...from both judge families
STALE_AFTER=15  # a lesson with no measurable effect after this many turns is retired
GATES=('Judges agree with you','Verdicts survive order swap','Judge output parses')  # failing any pauses auto-adoption
CATEGORY_DIM={'ignored_constraint':'instruction_following','ignored_tool_error':'error_recovery','blind_retry_loop':'error_recovery','no_verification':'verification',
    'wrong_tool':'tool_selection','bad_arguments':'tool_selection','hallucinated_result':'groundedness','misread_context':'groundedness',
    'premature_stop':'completeness','missing_context':'completeness','scope_violation':'instruction_following','slow_path':'efficiency'}

NEG=re.compile(r"^\s*(no|nope|wrong|incorrect|not quite)\b|\b(that'?s (not|wrong|incorrect)|doesn'?t work|didn'?t work|not working|still (broken|failing|wrong|not|doesn)|"
               r"try again|you (missed|forgot|ignored)|not what i (asked|meant|wanted)|is broken|makes no sense|hallucinat)",re.I)
POS=re.compile(r"\b(thanks|thank you|thx|perfect|great|awesome|works( now)?|it worked|nice|exactly|love it|good job|well done)\b",re.I)

def words(text):return {w for w in re.findall(r'[a-z0-9]+',text.lower()) if len(w)>2}

def classify(prompt,previous=None):
    """Implicit feedback on the PREVIOUS turn from the user's next message: (verdict, reason) or None."""
    if NEG.search(prompt):return ('negative','correction')
    if previous:
        a,b=words(prompt),words(previous)
        if len(a)>=4 and len(b)>=4 and len(a&b)/len(a|b)>=0.6:return ('negative','rephrase')
    if POS.search(prompt) and len(prompt)<160:return ('positive','thanks')
    return None

# ---------- aggregation over stored events ----------
def gather(store,hub_events=()):
    kinds=('feedback','judge.score','judge.result','judge.root','judge.verdict','judge.action','judge.error','span.start','lessons.applied',
           'judge.claim','judge.hack_final','judge.root_agreement','analysis.done','analysis.flag')
    if store:
        store.flush(3);ev={k:store.events_of_type(k,20000) for k in kinds}
    else:
        ev={k:[e for e in hub_events if e.get('type')==k] for k in kinds}
    return ev

def turn_scores(ev):
    """{turn_id: {provider: {dim: mean score over judges}}}"""
    acc=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
    for e in ev['judge.score']:
        if e.get('name') in DIMS and e.get('score') is not None:acc[e['turn_id']][e['provider']][e['name']].append(float(e['score']))
    return {t:{p:{d:sum(v)/len(v) for d,v in dims.items()} for p,dims in ps.items()} for t,ps in acc.items()}

def weights(ev):
    """Learned importance per dimension (mean 1.0), shrunk toward uniform by PRIOR pseudo-signals."""
    scores=turn_scores(ev);raw={d:0.0 for d in DIMS};n=0
    pos=defaultdict(list);neg=defaultdict(list);pairs=defaultdict(list)
    for f in ev['feedback']:
        t=scores.get(f.get('turn_id'))
        if not t:continue
        if f.get('reason_code') in ('prefer','label') and f.get('verdict') in t and len(t)==2:
            other=[p for p in t if p!=f['verdict']][0]
            for d in DIMS:
                if d in t[f['verdict']] and d in t[other]:pairs[d].append((t[f['verdict']][d]>t[other][d])-(t[f['verdict']][d]<t[other][d]))
            n+=1;continue
        bucket=pos if f.get('verdict')=='positive' else neg if f.get('verdict')=='negative' else None
        if bucket is None:continue
        for d in DIMS:
            vals=[t[p][d] for p in t if d in t[p]]
            if vals:bucket[d].append(sum(vals)/len(vals))
        n+=1
    for d in DIMS:
        if pos[d] and neg[d]:raw[d]+=max(0.0,sum(pos[d])/len(pos[d])-sum(neg[d])/len(neg[d]))
        elif neg[d]:raw[d]+=max(0.0,3.5-sum(neg[d])/len(neg[d]))*0.5  # low scores on turns the user rejected
        if pairs[d]:raw[d]+=max(0.0,sum(pairs[d])/len(pairs[d]))
    total=sum(raw.values())
    learned={d:(raw[d]*len(DIMS)/total if total else 1.0) for d in DIMS}
    w={d:round((PRIOR*1.0+n*learned[d])/(PRIOR+n),3) for d in DIMS}
    return {'weights':w,'signals':n,'top':[d for d,_ in sorted(w.items(),key=lambda kv:-kv[1]) if w[d]>1.05][:3]}

def judge_user_agreement(ev):
    prefs={f['turn_id']:f['verdict'] for f in ev['feedback'] if f.get('reason_code') in ('prefer','label')}
    verdicts=defaultdict(set)
    for v in ev['judge.verdict']:verdicts[v['turn_id']].add(v.get('verdict'))
    both=[(t,p) for t,p in prefs.items() if t in verdicts]
    agree=sum(1 for t,p in both if verdicts[t]=={p})
    return {'n':len(both),'agree':agree,'rate':agree/len(both) if both else None}

def votes(ev):
    """ExpeL-style votes per playbook item from outcomes of the turns it was applied to.
    helpful: judged >= 4 and the failure type the item targets did not occur; harmful: that failure occurred anyway, or judged < 3."""
    cats={l.get('id'):l.get('category') for l in L.ensure_ids(L.load())}
    score={};roots=defaultdict(set)
    for r in ev.get('judge.result',[]):score.setdefault((r['turn_id'],r['provider']),[]).append(float(r['score']))
    for r in ev.get('judge.root',[]):roots[(r['turn_id'],r['provider'])].add(r.get('name'))
    out=defaultdict(lambda:{'applied':0,'helpful':0,'harmful':0})
    for a in ev.get('lessons.applied',[]):
        key=(a.get('turn_id'),a.get('provider'))
        for lid in (a.get('tags') or '').split(','):
            if not lid:continue
            v=out[lid];v['applied']+=1
            if key not in score:continue
            s=sum(score[key])/len(score[key])
            if cats.get(lid) in roots.get(key,()) or s<3:v['harmful']+=1
            elif s>=4:v['helpful']+=1
    return dict(out)

def lesson_health(ev):
    """Before/after each lesson: provider's mean judged score and root-cause rate of the lesson's category."""
    results=sorted(ev['judge.result'],key=lambda e:e['timestamp'])
    roots=defaultdict(set)
    for r in ev['judge.root']:roots[(r['turn_id'],r['provider'])].add(r.get('name'))
    out=[];V=votes(ev)
    for i,l in enumerate(L.ensure_ids(L.load())):
        try:added=time.mktime(time.strptime(l['added'],'%Y-%m-%d %H:%M'))
        except (KeyError,ValueError):added=0
        provs=('codex','claude') if l['provider']=='both' else (l['provider'],)
        def stats(sel):
            turns={}
            for r in results:
                if r['provider'] in provs and sel(r['timestamp']):turns.setdefault((r['turn_id'],r['provider']),[]).append(float(r['score']))
            if not turns:return {'n':0,'score':None,'rate':None}
            hit=sum(1 for k in turns if l['category'] in roots.get(k,()))
            return {'n':len(turns),'score':sum(sum(v)/len(v) for v in turns.values())/len(turns),'rate':hit/len(turns)}
        before,after=stats(lambda t:t<added),stats(lambda t:t>=added)
        if after['n']<3 or before['n']==0:status='measuring'
        elif after['score'] is not None and before['score'] is not None and after['score']<before['score']-0.25:status='hurting'
        elif after['rate'] is not None and before['rate'] is not None and (after['rate']<before['rate'] or (before['rate']==0 and after['score']>=before['score'])) and after['score']>=before['score']-0.1:status='helping'
        else:status='no clear effect'
        v=V.get(l.get('id'),{'applied':0,'helpful':0,'harmful':0})
        out.append({**l,'index':i,'before':before,'after':after,'status':status,'votes':v,'lifecycle':l.get('status','active')})
    return out

def ranked_suggestions(ev,w=None):
    """Judge fixes ranked by support x learned importance of the dimension they protect, with diversity decay."""
    w=w or weights(ev)['weights']
    items=L.suggestions(ev['judge.action'])
    for s in items:s['importance']=round(s['turns']*w.get(CATEGORY_DIM.get(s['category'],''),1.0)*(1.25 if len(s['judges'])>=2 else 1.0),3)
    picked=[];pool=sorted(items,key=lambda s:-s['importance']);seen=defaultdict(int)
    while pool:  # greedy re-rank: each repeat of a category (per model) costs 40%, so one failure type cannot crowd out the rest
        best=max(pool,key=lambda s:s['importance']*(0.6**seen[(s['provider'],s['category'])]))
        pool.remove(best);seen[(best['provider'],best['category'])]+=1;picked.append(best)
    return picked

def checks(ev,w,agree,health,sugg,turn_kinds):
    C=[]
    def add(ok,label,detail):C.append({'ok':ok,'label':label,'detail':detail})
    decisive=[v for v in ev['judge.verdict'] if v.get('verdict') in ('codex','claude')]
    turns_judged=len({v['turn_id'] for v in decisive})
    add(turns_judged>=20,'Enough head-to-head turns',f'{turns_judged} decisive turns; 20+ needed before a winner is trustworthy')
    total=sum(turn_kinds.values()) or 1
    share={k:v/total for k,v in turn_kinds.items()}
    skew=max(share.values()) if share else 0
    missing=[k for k in ('respond','read','write') if not turn_kinds.get(k)]
    add(skew<=0.8 and not missing,'Balanced task mix',', '.join(f"{k} {round(100*share.get(k,0))}%" for k in ('respond','read','write'))+(f' · no {"/".join(missing)} tasks yet' if missing else ''))
    judges={str(v.get('judge','')).split('-')[0] for v in ev['judge.verdict']}|{str(r.get('judge','')).split('-')[0] for r in ev['judge.result']}
    add(len(judges-{''})>=2,'Both judge families active','panel cancels self-preference' if len(judges-{''})>=2 else 'single-family judging: self-preference risk; use /judge panel')
    cons=[v.get('passed') for v in ev['judge.verdict'] if v.get('passed') is not None]
    rate=sum(1 for c in cons if c)/len(cons) if cons else None
    add(rate is None or rate>=0.8,'Verdicts survive order swap',f'{round(100*rate)}% consistent' if rate is not None else 'no swapped verdicts yet')
    errs=len(ev['judge.error']);calls=errs+len(ev['judge.verdict'])+len([r for r in ev['judge.result'] if r.get('verdict')=='solo'])
    add(calls==0 or errs/calls<0.1,'Judge output parses',f'{errs} failed of ~{calls} judge calls')
    if agree['n']>=3:add(agree['rate']>=0.6,'Judges agree with you',f"{agree['agree']} of {agree['n']} of your labels / preferences match the panel")
    else:add(None,'Judges agree with you',f"{agree['n']} labels so far; run duo --label (or /prefer after a turn) to calibrate the judges")
    add(None if w['signals']<3 else True,'Learned what matters to you',f"{w['signals']} feedback signals"+(f"; top: {', '.join(d.replace('_',' ') for d in w['top'])}" if w['top'] else '; weights still near uniform'))
    hurting=[h for h in health if h['status']=='hurting']
    add(not hurting,'Lessons are not hurting',f'{len(health)} lessons, {sum(h["status"]=="helping" for h in health)} helping, {len(hurting)} hurting' if health else 'no lessons yet')
    for p in ('codex','claude'):
        n=L.count(p)
        if n>=LESSON_CAP:add(False,f'{p.title()} lesson cap',f'{n}/{LESSON_CAP}: remove one before adding more')
    cl=ev.get('judge.claim',[])
    if cl:
        sup=sum(c.get('verdict')=='supported' for c in cl);bad=sum(c.get('verdict')=='contradicted' for c in cl)
        add(bad==0,'Claims match the trace',f'{sup}/{len(cl)} claims supported, {bad} contradicted by tool output')
    hk=ev.get('judge.hack_final',[])
    add(not hk,'No reward hacking',f'{len(hk)} confirmed hack(s) (test edits/skips/grader changes)' if hk else 'no confirmed hacks')
    ra=ev.get('judge.root_agreement',[])
    if ra:
        rate=sum(1 for r in ra if r.get('passed'))/len(ra)
        add(rate>=0.5,'Step labels are confident',f'judges agree on the root-cause step (±1) in {round(100*rate)}% of runs; others are marked low-confidence')
    lat=[float(e.get('duration_ms') or 0) for e in ev.get('lessons.applied',[])]
    post=[float(e.get('duration_ms') or 0) for e in ev.get('analysis.done',[])]
    add((max(lat) if lat else 0)<50,'No added latency',f"pre-turn lesson selection max {max(lat) if lat else 0:.1f} ms (budget 50); post-turn analysis median {sorted(post)[len(post)//2] if post else 0:.0f} ms in the background")
    top=sugg[:5];cats={s['category'] for s in top}
    add(len(top)<3 or len(cats)>=min(3,len(top)),'Suggestions are diverse',f'{len(cats)} failure types in the top {len(top)}' if top else 'no suggestions yet')
    return C

def snapshot(store,hub_events=()):
    ev=gather(store,hub_events)
    w=weights(ev);agree=judge_user_agreement(ev);health=lesson_health(ev);sugg=ranked_suggestions(ev,w['weights'])
    kinds=defaultdict(int)
    for s in ev['span.start']:
        if s.get('span_kind')=='turn':
            m=s.get('mode') or '';kinds['respond' if m.startswith('respond') else 'write' if 'write' in m else 'read']+=1
    return {'weights':w,'agreement':agree,'lessons':health,'suggestions':sugg[:12],'checks':checks(ev,w,agree,health,sugg,kinds),
            'lessons_file':str(L.path()),'cap':LESSON_CAP}

def auto_step(store,hub_events=()):
    """After a judged turn: adopt well-supported fixes, retire lessons that hurt. Returns human-readable notices."""
    notes=[];snap=snapshot(store,hub_events)
    for h in sorted(snap['lessons'],key=lambda h:-h['index']):
        if h['status']=='hurting' and h['after']['n']>=5:
            L.remove(h['index']);notes.append(f"Retired lesson for {h['provider']} (score fell {h['before']['score']:.2f} → {h['after']['score']:.2f}): {h['rule']}")
        elif h['status']=='no clear effect' and h['after']['n']>=STALE_AFTER:
            L.remove(h['index']);notes.append(f"Retired lesson for {h['provider']} (no measurable effect after {h['after']['n']} turns; keeps prompts lean): {h['rule']}")
    for h in snap['lessons']:  # DELETE: outcomes keep contradicting this item
        v=h['votes']
        if v['applied']>=5 and v['harmful']>=v['helpful']+3 and h.get('lifecycle')!='verified':
            L.remove_id(h['id']);notes.append(f"Retired lesson for {h['provider']} (harmful in {v['harmful']} of {v['applied']} uses): {h['rule']}")
    merged=L.merge_duplicates()
    if merged:notes.append(f'Merged {len(merged)} near-duplicate lesson(s).')
    try:
        import evalset
        added=evalset.add_failure_cases(store)
        if added:notes.append(f'Added {added} failure case(s) to the eval set (regression tests for next replay).')
    except Exception as exc:notes.append('Failure-case step skipped: '+str(exc))
    blockers=[c['label'] for c in snap['checks'] if c['ok'] is False and c['label'] in GATES]
    if blockers:  # do not learn from judges the checks say are untrustworthy right now
        return notes+[f"Auto-learning paused: {', '.join(blockers)}"]
    for s in snap['suggestions']:
        if s['accepted'] or s['target']=='workflow':continue
        if s['turns']>=AUTO_MIN_TURNS and len(s['judges'])>=AUTO_MIN_JUDGES and L.count(s['provider'])<LESSON_CAP:
            L.accept(s);notes.append(f"Learned a lesson for {s['provider']} ({s['turns']} turns, both judges): {s['rule']}")
    return notes
