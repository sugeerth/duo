#!/usr/bin/env python3
"""Run AgentLens's detectors over the benchmark and write predictions in the public format (bench/SPEC.md).

    python3 bench/predict_agentlens.py --out preds.jsonl [--extra bench/data/real/*.jsonl]

This is the reference adapter: any other system is scored the same way by writing the same predictions.jsonl
(one {"id", "pred"} per case, schema bench/schema/prediction.schema.json) and running bench/score.py.
`system_checks()` adds AgentLens-specific checks that are not part of the public tasks (viewer JS == Python attribution).
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;DATA=HERE/'data'
sys.path.insert(0,str(ROOT))
import analysis, attribution, chatimport, horizon, longrun, longterm, security, tracestore  # noqa: E402

def analysis_steps(steps):
    out=[]
    for s in steps:
        k=analysis.action_type({'kind':'tool','tool':s['tool'],'target':s['target']})
        out.append({**s,'kind':'tool','type':k,'key':analysis.target_key(s,k),'input':s.get('input') or ''})
    return out

def predict_run(case):
    rd=longrun.read(case['steps']) or {'loops':[],'look_first':None,'checks':[],'sessions':[{}],'stall':None};st=analysis_steps(case['steps'])
    hacks={}
    for h in analysis.hack_signals(st):hacks.setdefault(h['ref'],set()).add(h['signal'])
    _,flags=analysis.behaviour(st);stall=rd.get('stall')
    return {'loops':[{'start_ref':L['ref']} for L in rd['loops']],'look_first_ref':(rd['look_first'] or {}).get('ref'),
            'progress_refs':[c['ref'] for c in rd['checks'] if c['progress']],'regression_refs':[c['ref'] for c in rd['checks'] if c['regression']],
            'sessions':max(1,len(rd['sessions'])),'checks':len(rd['checks']),
            'stall':{'open':bool(stall['open']),'start_ref':stall['ref']} if stall else None,
            'hacks':[{'ref':r,'signals':sorted(s)} for r,s in sorted(hacks.items())],'flags':sorted({f['flag'] for f in flags})}

def predict_security(case):
    r=security.review(case['steps'])
    return {'security':[{'ref':f['ref'],'category':f['category']} for f in r['findings']],'error_kinds':[{'ref':e['ref'],'kind':e['kind']} for e in r['errors']]}

def predict_horizon(case):
    r=horizon.read(case['steps'],case.get('request'),case.get('answer'),case.get('stop_reason'))
    return {'horizon':{'modes':[{'mode':m['mode'],'ref':m['ref']} for m in r['modes']],'milestones_reached':r['milestones']['reached']}}

def predict_trend(case):
    ins=longterm.insight(case['records'])
    return {'findings':[{'provider':f['provider'],'metric':f['metric'],'direction':'up' if f['up'] else 'down'} for f in ins['findings'] if f['kind'] in ('trend','shift')]}

def predict_attribution(case):
    """AgentLens segments the answer itself; the benchmark gives the segmentation, so labels are matched by position."""
    got=attribution.attribute(case['final'],case['candidates'])
    if len(got)==len(case['sentences']):return {'sources':[g['src'] for g in got]}
    return {'sources':[attribution.attribute(s,case['candidates'])[0]['src'] if attribution.sentences(s) else 'new' for s in case['sentences']]}

def predict_rollout(case,store):
    sid,_=chatimport.import_rollouts(store,DATA/case['file']);ev=store.snapshot(sid)['events']
    turns=[e['span_id'] for e in ev if e['type']=='span.start' and e.get('span_kind')=='turn']
    rets={e['turn_id']:e for e in ev if e['type']=='env.return'};hacked={e['turn_id'] for e in ev if e['type']=='analysis.hack'}
    return {'episodes':[{'success':bool(rets[t]['passed']),'return':rets[t]['score'],'hacked':t in hacked} for t in turns if t in rets]}

def predict_import(case,store):
    f=DATA/case['file']
    if case['loader']=='chat':
        convs=[]
        for title,sid,n in chatimport.import_file(store,f):
            ev=store.snapshot(sid)['events'];answers=[store.payload(sid,e['id'])['text'] for e in ev if e['type']=='answer']
            convs.append({'turns':n,'reasoning':sum(e['type']=='reasoning' for e in ev),'answers':answers})
        return {'fixture':{'conversations':convs}}
    if case['loader']=='session':
        sid,n=chatimport.import_session(store,f);ev=store.snapshot(sid)['events'];turns=[]
        for a in [e['span_id'] for e in ev if e['type']=='span.start' and e.get('span_kind')=='agent']:
            mine=[e for e in ev if e.get('span_id')==a];ans=[store.payload(sid,e['id'])['text'] for e in mine if e['type']=='answer']
            turns.append({'tools':sum(e['type']=='tool.started' for e in mine),'failed':sum(e['type']=='tool.completed' and e.get('passed') is False for e in mine),
                          'reasoning':sum(e['type']=='reasoning' for e in mine),'ctx_points':sum(e['type']=='context.tokens' for e in mine),'answer':ans[0] if ans else None})
        return {'fixture':{'turns':turns}}
    sid,n=chatimport.import_rollouts(store,f)
    return {'fixture':{'episodes':[{'return':e['score'],'success':bool(e['passed']),'steps':e['count']} for e in store.snapshot(sid)['events'] if e['type']=='env.return']}}

def jsonl(p):return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]

def predict_all(extra=()):
    preds=[]
    with tempfile.TemporaryDirectory() as tmp:
        store=tracestore.TraceStore(Path(tmp)/'bench.db')
        runs=jsonl(DATA/'runs.jsonl')+[r for f in extra for r in jsonl(f) if r.get('reviewed')]
        preds+=[{'id':c['id'],'pred':predict_run(c)} for c in runs]
        preds+=[{'id':c['id'],'pred':predict_trend(c)} for c in jsonl(DATA/'trends.jsonl')]
        preds+=[{'id':c['id'],'pred':predict_security(c)} for c in jsonl(DATA/'security.jsonl')]
        preds+=[{'id':c['id'],'pred':predict_horizon(c)} for c in jsonl(DATA/'horizon.jsonl')]
        preds+=[{'id':c['id'],'pred':predict_attribution(c)} for c in jsonl(DATA/'attribution.jsonl')]
        preds+=[{'id':c['id'],'pred':predict_rollout(c,store)} for c in json.loads((DATA/'rollouts.json').read_text())]
        preds+=[{'id':c['id'],'pred':predict_import(c,store)} for c in json.loads((DATA/'imports.json').read_text())]
    return preds

JS_DRIVER=r'''
const fs=require('fs');const src=fs.readFileSync(process.argv[2],'utf8');
const a=src.indexOf('function shingles(');const b=src.indexOf('async function loadAttribution');
eval(src.slice(a,b));
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(c=>attribute(c.final,c.candidates).map(x=>x.src))));
'''
def cluster_purity():
    """Patterns map sanity: cluster the benchmark runs by their facets; purity = share of runs whose cluster's
    majority family is their own family (families that behave alike, e.g. clean_long and multisession, may merge)."""
    import facets
    from collections import Counter,defaultdict
    rows=[]
    for x in jsonl(DATA/'runs.jsonl'):
        f=facets.facets(x['steps']);n=f.pop('steps');rows.append({'id':x['id'],'family':x['family'],'provider':x['style'],'ts':0,'steps':n,'features':f})
    rows,info=facets.cluster(rows);by=defaultdict(Counter)
    for r in rows:by[r['cluster']][r['family']]+=1
    return sum(c.most_common(1)[0][1] for c in by.values())/len(rows)

def guard_checks():
    """Pre-execution guard (guard.py, block mode) replayed over the security cases, one call at a time with the run's
    earlier outputs as context: it must block every action labelled injection_followed, and no benign look-alike."""
    import os,guard
    old=os.environ.get('AGENTLENS_GUARD');os.environ['AGENTLENS_GUARD']='block';hit=n=fb=nb=0
    try:
        for c in jsonl(DATA/'security.jsonl'):
            want={f['ref'] for f in c['labels']['security'] if f['category']=='injection_followed'};outs=[]
            for s in c['steps']:
                v=guard.check(s['tool'],{'command':s['target']},outs)['verdict'];outs.append(s.get('output',''))
                if s['ref'] in want:n+=1;hit+=v=='block'
                if c['family']=='sec_benign_lookalikes':nb+=1;fb+=v=='block'
    finally:
        if old is None:os.environ.pop('AGENTLENS_GUARD',None)
        else:os.environ['AGENTLENS_GUARD']=old
    return {'guard.blocks_injection_followed':hit/n if n else None,'guard.false_block_rate_benign':fb/nb if nb else None}

def system_checks():
    """AgentLens-only checks: viewer JS attribution == attribution.py; patterns-map cluster purity on the benchmark runs."""
    node=shutil.which('node');extra={'clusters.purity':round(cluster_purity(),4),**guard_checks()}
    if not node:return {'attribution.js_parity':None,**extra},['node not found: JS parity skipped']
    cases=jsonl(DATA/'attribution.jsonl')
    with tempfile.NamedTemporaryFile('w',suffix='.js',delete=False) as f:f.write(JS_DRIVER)
    out=subprocess.run([node,f.name,str(ROOT/'viewer.html')],input=json.dumps([{'final':c['final'],'candidates':c['candidates']} for c in cases]),capture_output=True,text=True,timeout=60)
    Path(f.name).unlink()
    if out.returncode:return {'attribution.js_parity':0.0,**extra},['JS driver failed: '+out.stderr[:200]]
    js=json.loads(out.stdout);py=[[g['src'] for g in attribution.attribute(c['final'],c['candidates'])] for c in cases]
    diffs=[f"{c['id']}: JS {a} != Python {b}" for c,a,b in zip(cases,js,py) if a!=b]
    return {'attribution.js_parity':sum(a==b for a,b in zip(js,py))/len(cases),**extra},diffs

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);ap.add_argument('--out',required=True);ap.add_argument('--extra',nargs='*',default=[])
    a=ap.parse_args(argv);preds=predict_all(a.extra)
    Path(a.out).write_text(''.join(json.dumps(p,sort_keys=True)+'\n' for p in preds));print(f'wrote {len(preds)} predictions to {a.out}')

if __name__=='__main__':main()
