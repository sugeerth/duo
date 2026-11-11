"""Calibrate the judges against the user's own blind labels.

--label shows past turns where both models answered, blind (A/B, random order), and
records which answer the user prefers as a feedback event (reason_code='label') in the
original session. --calibration reports how often each judge (and the panel) picks the
same winner as the user, with a 95% Wilson interval, plus which judge dimensions
best predict the user's choice. learning.py treats labels like /prefer.
"""
import json
import random
from collections import defaultdict

from evalset import wilson
import judge as J

def pairs(store):
    """Turns with an answer from both models (responder/reader), not yet labelled. Newest first."""
    store.flush(3)
    with store.lock,store.connect() as db:
        rows=db.execute('''SELECT t.session_id,t.span_id,t.start,
              (SELECT text FROM payloads WHERE span_id=t.span_id AND kind='prompt') AS prompt,
              a.provider,(SELECT text FROM payloads p WHERE p.span_id=a.span_id AND p.kind='answer' ORDER BY event_id DESC LIMIT 1) AS answer
            FROM spans t JOIN spans a ON a.parent_id=t.span_id
            WHERE t.kind='turn' AND a.kind='agent' AND a.role IN ('responder','reader') ORDER BY t.start DESC''').fetchall()
        labelled={json.loads(d).get('turn_id') for (d,) in db.execute("SELECT data FROM events WHERE type='feedback' AND data LIKE '%\"label\"%'")}
    turns={}
    for sid,tid,ts,prompt,provider,answer in rows:
        if tid in labelled or not answer or not prompt:continue
        t=turns.setdefault(tid,{'session_id':sid,'turn_id':tid,'ts':ts,'prompt':prompt,'answers':{}})
        t['answers'].setdefault(provider,answer)
    return [t for t in turns.values() if set(t['answers'])>={'codex','claude'}]

def record(store,item,choice):
    store.add_event(item['session_id'],{'type':'feedback','provider':'duo','turn_id':item['turn_id'],'verdict':choice,'reason_code':'label'})

def run(store,limit=10,show=print,ask=input,rng=random):
    items=pairs(store)[:limit]
    if not items:show('Nothing to label: no unlabelled turns where both models answered.');return 0
    done=0
    for n,item in enumerate(items,1):
        order=['codex','claude'];rng.shuffle(order)
        show(f'\n─── {n}/{len(items)} ───\nRequest:\n{item["prompt"][:1500]}')
        for label,p in zip('AB',order):show(f'\n[{label}]\n{item["answers"][p][:3000]}')
        while True:
            reply=ask('\nBetter answer? a / b / t(ie) / s(kip) / q(uit): ').strip().lower()[:1]
            if reply in ('a','b','t','s','q'):break
        if reply=='q':break
        if reply=='s':continue
        record(store,item,'tie' if reply=='t' else order['ab'.index(reply)]);done+=1
    show(f'\nSaved {done} label{"s" if done!=1 else ""}. Run duo --calibration to see how the judges compare with you.')
    return done

def report(store):
    kinds=('feedback','judge.verdict','judge.score')
    ev={k:store.events_of_type(k,50000) for k in kinds}
    mine={e['turn_id']:e['verdict'] for e in ev['feedback'] if e.get('reason_code') in ('label','prefer') and e.get('verdict') in ('codex','claude','tie')}
    by_judge=defaultdict(dict)
    for v in ev['judge.verdict']:by_judge[str(v.get('judge'))][v['turn_id']]=v.get('verdict')
    rows=[]
    def score(name,picks):
        both=[t for t in mine if t in picks];k=sum(1 for t in both if picks[t]==mine[t])
        rows.append({'judge':name,'n':len(both),'agree':k,'rate':k/len(both) if both else None,'ci':wilson(k,len(both))})
    for j,picks in sorted(by_judge.items()):score(j+' judge',picks)
    panel={}
    for t in mine:
        votes={picks[t] for picks in by_judge.values() if t in picks}
        if votes:panel[t]=votes.pop() if len(votes)==1 else 'tie'
    score('panel (split = tie)',panel)
    # which dimension best predicts the user's pick?
    sc=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
    for e in ev['judge.score']:sc[e['turn_id']][e['provider']][e['name']].append(float(e['score']))
    dims=[]
    for d in list(J.DIMENSIONS)+list(J.PROCESS):
        hit=n=0
        for t,choice in mine.items():
            if choice not in ('codex','claude') or t not in sc:continue
            other='claude' if choice=='codex' else 'codex'
            a,b=sc[t][choice].get(d),sc[t][other].get(d)
            if not a or not b:continue
            a,b=sum(a)/len(a),sum(b)/len(b)
            if a==b:continue
            n+=1;hit+=a>b
        if n:dims.append({'dimension':d,'n':n,'predicts':hit/n})
    dims.sort(key=lambda x:-x['predicts'])
    return {'labels':len(mine),'judges':rows,'dimensions':dims}

def render(rep):
    if not rep['labels']:return 'No labels yet. Run duo --label to label a few past turns blind.'
    lines=[f"Calibration against {rep['labels']} of your labels/preferences"]
    for r in rep['judges']:
        if r['n']:lines.append(f"  {r['judge']:<22} agrees {r['agree']}/{r['n']} = {round(100*r['rate'])}%  (95% CI {round(100*r['ci'][0])}–{round(100*r['ci'][1])}%)")
    if rep['dimensions']:
        lines.append('  Dimensions that best predict your pick: '+', '.join(f"{d['dimension'].replace('_',' ')} {round(100*d['predicts'])}% (n={d['n']})" for d in rep['dimensions'][:4]))
    small=[r for r in rep['judges'] if r['n'] and r['n']<10]
    if small:lines.append('  Note: under 10 labels per judge the intervals are wide; label more before trusting a judge.')
    return '\n'.join(lines)
