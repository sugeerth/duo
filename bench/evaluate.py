#!/usr/bin/env python3
"""Score Duo's detectors against the labelled benchmark in bench/data (see bench/README.md).

    python3 bench/evaluate.py [--split dev|test|all] [--json bench/results.json] [--quiet]

Every number compares a detector's output with labels that were PLANTED by bench/generate.py. Exit status is 1
when any metric misses its target in TARGETS, so this can gate CI. Targets that a detector is known to miss by
design (for example, the deterministic hack detector flags legitimate new test files; the judges are meant to
confirm) are reported as KNOWN rather than hidden.
"""
import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;DATA=HERE/'data'
sys.path.insert(0,str(ROOT))
import analysis, attribution, chatimport, longrun, longterm, tracestore  # noqa: E402

# metric -> (comparison, target, why). 'known' targets are reported but never fail the run.
TARGETS={
    'runs.loop.precision':('>=',0.95,''),'runs.loop.recall':('>=',0.95,''),'runs.loop.start_exact':('>=',0.9,'detected loop starts at the first repeating burst'),
    'runs.look_first.accuracy':('>=',0.9,'same step as the label, or both empty'),
    'runs.progress.f1':('>=',0.95,''),'runs.regression.f1':('>=',0.95,''),'runs.sessions.accuracy':('>=',0.95,''),
    'runs.checks.accuracy':('>=',0.95,'traps: reading or printing a test file is not a check'),
    'runs.stall.open_and_located':('>=',0.9,''),
    'runs.hack.recall':('>=',1.0,'every planted hack is flagged on its step'),'runs.hack.signal_match':('>=',0.95,''),
    'runs.hack.false_positive_rate_controls':('<=',0.0,'runs with no file writes to tests, graders or CI'),
    'runs.hack.false_positive_rate_legit_tests':('known',None,'new test files are flagged by design; the judges confirm or dismiss'),
    'runs.flags.precision':('>=',0.9,''),'runs.flags.recall':('>=',0.95,''),
    'trends.sensitivity.large_n>=20':('>=',0.8,''),'trends.sensitivity.small':('known',None,'small effects at small n are expected to be missed'),
    'trends.false_positive_rate':('<=',0.1,'any finding on a metric with no planted trend'),'trends.wrong_direction':('<=',0.0,''),
    'attribution.verbatim.accuracy':('>=',0.95,''),'attribution.paraphrase.recall':('known',None,'word-overlap cannot see paraphrase; labelled new'),
    'attribution.js_parity':('>=',1.0,'viewer.html and attribution.py agree on every sentence'),
    'rollouts.success.accuracy':('>=',1.0,''),'rollouts.return.accuracy':('>=',1.0,''),'rollouts.hack.recall':('>=',1.0,''),
    'rollouts.hack.precision':('>=',1.0,''),'rollouts.pass_hat_k.exact':('>=',1.0,'pass^k recomputed from the imported episodes'),
    'imports.exact':('>=',1.0,'turn, tool, failure, reasoning, answer and context-point counts match'),
}

def f1(p,r):return 0.0 if p+r==0 else 2*p*r/(p+r)
def ratio(a,b):return a/b if b else None
def load_jsonl(name,split):
    rows=[json.loads(l) for l in (DATA/name).read_text().splitlines() if l.strip()]
    return [r for r in rows if split=='all' or r['split']==split]

def analysis_steps(steps):
    out=[]
    for s in steps:
        k=analysis.action_type({'kind':'tool','tool':s['tool'],'target':s['target']})
        out.append({**s,'kind':'tool','type':k,'key':analysis.target_key(s,k),'input':s.get('input') or ''})
    return out

# ------------------------------------------------------------------ runs
def eval_runs(split,R,fails,extra=()):
    rows=load_jsonl('runs.jsonl',split);c=Counter();fam=defaultdict(lambda:[0,0])
    for f in extra:  # hand-labelled real runs (bench/export_for_labeling.py); unreviewed lines are skipped
        rows+=[r for r in (json.loads(l) for l in Path(f).read_text().splitlines() if l.strip()) if r.get('reviewed')]
    for x in rows:
        L={k:v for k,v in x['labels'].items()};rd=longrun.read(x['steps']) or {'loops':[],'look_first':None,'checks':[],'sessions':[],'stall':None};st=analysis_steps(x['steps']);ok=True
        for k,v in {'loops':[],'progress_refs':[],'regression_refs':[],'hacks':[],'flags':[],'sessions':1}.items():
            if L.get(k) is None:L[k]=v
        def miss(what):
            nonlocal ok;ok=False;fails.append({'case':x['id'],'family':x['family'],'check':what})
        gl,dl=bool(L['loops']),bool(rd['loops'])
        c['loop_tp']+=gl and dl;c['loop_fp']+=dl and not gl;c['loop_fn']+=gl and not dl
        if gl!=dl:miss(f'loop expected={gl} got={dl}')
        if gl and dl:
            c['loop_both']+=1;exact=rd['loops'][0]['ref']==L['loops'][0]['start_ref'];c['loop_exact']+=exact
            if not exact:miss(f"loop start {rd['loops'][0]['ref']} != {L['loops'][0]['start_ref']}")
        lf=(rd['look_first'] or {}).get('ref');c['lf_n']+=1;c['lf_ok']+=lf==L.get('look_first_ref')
        if lf!=L.get('look_first_ref'):miss(f"look_first {lf} != {L.get('look_first_ref')}")
        for key,lab in (('progress','progress_refs'),('regression','regression_refs')):
            got={e['ref'] for e in rd['checks'] if e[key]};want=set(L[lab])
            c[key+'_tp']+=len(got&want);c[key+'_fp']+=len(got-want);c[key+'_fn']+=len(want-got)
            if got!=want:miss(f'{key} refs {sorted(got)} != {sorted(want)}')
        c['sess_n']+=1;c['sess_ok']+=len(rd['sessions'])==L['sessions']
        if len(rd['sessions'])!=L['sessions']:miss(f"sessions {len(rd['sessions'])} != {L['sessions']}")
        if L.get('checks') is not None:
            c['chk_n']+=1;c['chk_ok']+=len(rd['checks'])==L['checks']
            if len(rd['checks'])!=L['checks']:miss(f"checks {len(rd['checks'])} != {L['checks']}")
        if L.get('stall_open') and L.get('stall_start_ref') is not None:
            c['stall_n']+=1;good=bool(rd['stall'] and rd['stall']['open'] and rd['stall']['ref']==L['stall_start_ref']);c['stall_ok']+=good
            if not good:miss('open stall not found at the labelled step')
        hs=analysis.hack_signals(st);byref=defaultdict(set)
        for h in hs:byref[h['ref']].add(h['signal'])
        gold={h['ref']:set(h['signals']) for h in L['hacks']}
        for ref,sig in gold.items():
            c['hack_n']+=1;c['hack_hit']+=ref in byref;c['hack_sig']+=byref.get(ref,set())==sig
            if ref not in byref:miss(f'hack at {ref} not flagged')
            elif byref[ref]!=sig:miss(f'hack signals {sorted(byref[ref])} != {sorted(sig)}')
        extra=set(byref)-set(gold)
        if x['family']=='legit_tests':c['legit_n']+=1;c['legit_fp']+=bool(extra)
        else:
            c['ctrl_n']+=not gold;c['ctrl_fp']+=bool(extra) and not gold
            if extra:miss(f'unexpected hack flags at {sorted(extra)}')
        M,flags=analysis.behaviour(st);got={f['flag'] for f in flags};want=set(L['flags'])
        c['flag_tp']+=len(got&want);c['flag_fp']+=len(got-want);c['flag_fn']+=len(want-got)
        if got!=want:miss(f'flags {sorted(got)} != {sorted(want)}')
        fam[x['family']][0]+=ok;fam[x['family']][1]+=1
    lp=ratio(c['loop_tp'],c['loop_tp']+c['loop_fp']);lr=ratio(c['loop_tp'],c['loop_tp']+c['loop_fn'])
    R.update({'runs.cases':len(rows),'runs.loop.precision':lp,'runs.loop.recall':lr,'runs.loop.start_exact':ratio(c['loop_exact'],c['loop_both']),
              'runs.look_first.accuracy':ratio(c['lf_ok'],c['lf_n']),'runs.sessions.accuracy':ratio(c['sess_ok'],c['sess_n']),
              'runs.checks.accuracy':ratio(c['chk_ok'],c['chk_n']),'runs.stall.open_and_located':ratio(c['stall_ok'],c['stall_n']),
              'runs.hack.recall':ratio(c['hack_hit'],c['hack_n']),'runs.hack.signal_match':ratio(c['hack_sig'],c['hack_n']),
              'runs.hack.false_positive_rate_controls':ratio(c['ctrl_fp'],c['ctrl_n']),'runs.hack.false_positive_rate_legit_tests':ratio(c['legit_fp'],c['legit_n'])})
    for key in ('progress','regression','flag'):
        p=ratio(c[key+'_tp'],c[key+'_tp']+c[key+'_fp']);r=ratio(c[key+'_tp'],c[key+'_tp']+c[key+'_fn'])
        name='flags' if key=='flag' else key
        R[f'runs.{name}.precision']=p;R[f'runs.{name}.recall']=r;R[f'runs.{name}.f1']=f1(p or 0,r or 0) if p is not None and r is not None else None
    R['runs.by_family']={k:f'{a}/{b}' for k,(a,b) in fam.items()}

# ------------------------------------------------------------------ trends
def eval_trends(split,R,fails):
    rows=load_jsonl('trends.jsonl',split);grid=defaultdict(lambda:[0,0]);fp=fp_n=wrong=0
    for x in rows:
        ins=longterm.insight(x['records']);found=[f for f in ins['findings'] if f['provider']=='claude' and f['kind'] in ('trend','shift')]
        gold={g['metric']:g['direction'] for g in x['labels']['trends']}
        for metric in longterm.METRICS:
            if metric=='ret':continue
            hits=[f for f in found if f['metric']==metric]
            if metric in gold:
                key=(x['labels']['effect'],x['n']);grid[key][1]+=1;dirs={('up' if f['up'] else 'down') for f in hits}
                if hits and gold[metric] in dirs:grid[key][0]+=1
                if hits and gold[metric] not in dirs:wrong+=1;fails.append({'case':x['id'],'family':x['family'],'check':f'{metric}: wrong direction'})
                elif not hits and (x['labels']['effect']=='large' and x['n']>=20):fails.append({'case':x['id'],'family':x['family'],'check':f'{metric}: large trend missed'})
            else:
                fp_n+=1;fp+=bool(hits)
                if hits:fails.append({'case':x['id'],'family':x['family'],'check':f'{metric}: finding with no planted trend (q={hits[0]["q"]})'})
    sens=lambda pred:(lambda a,b:ratio(a,b))(*[sum(v[i] for k,v in grid.items() if pred(k)) for i in (0,1)])
    R.update({'trends.cases':len(rows),'trends.sensitivity.large_n>=20':sens(lambda k:k[0]=='large' and k[1]>=20),
              'trends.sensitivity.large':sens(lambda k:k[0]=='large'),'trends.sensitivity.small':sens(lambda k:k[0]=='small'),
              'trends.false_positive_rate':ratio(fp,fp_n),'trends.wrong_direction':ratio(wrong,sum(v[1] for v in grid.values())),
              'trends.sensitivity_grid':{f'{e}/n={n}':f'{a}/{b}' for (e,n),(a,b) in sorted(grid.items())}})

# ------------------------------------------------------------------ attribution
JS_DRIVER=r'''
const fs=require('fs');const src=fs.readFileSync(process.argv[2],'utf8');
const a=src.indexOf('function shingles(');const b=src.indexOf('async function loadAttribution');
eval(src.slice(a,b));
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(c=>attribute(c.final,c.candidates).map(x=>x.src))));
'''
def eval_attribution(split,R,fails):
    rows=load_jsonl('attribution.jsonl',split);cm=Counter();vb=[0,0];pa=[0,0];py_labels=[]
    for x in rows:
        got=attribution.attribute(x['final'],x['candidates']);gold=x['labels']['sentences'];py_labels.append([g['src'] for g in got])
        if len(got)!=len(gold):fails.append({'case':x['id'],'family':x['family'],'check':f'{len(got)} sentences split, {len(gold)} labelled'});continue
        for g,s in zip(gold,got):
            cm[(g['src'],s['src'])]+=1
            if g['paraphrase']:pa[1]+=1;pa[0]+=s['src']==g['src']
            else:
                vb[1]+=1;vb[0]+=s['src']==g['src']
                if s['src']!=g['src']:fails.append({'case':x['id'],'family':x['family'],'check':f"sentence labelled {g['src']} attributed {s['src']}"})
    R.update({'attribution.cases':len(rows),'attribution.verbatim.accuracy':ratio(*vb),'attribution.paraphrase.recall':ratio(*pa),
              'attribution.confusion':{f'{g}->{p}':n for (g,p),n in sorted(cm.items())}})
    node=shutil.which('node')
    if not node:R['attribution.js_parity']=None;R['attribution.js_parity_note']='node not found; skipped';return
    with tempfile.NamedTemporaryFile('w',suffix='.js',delete=False) as f:f.write(JS_DRIVER)
    out=subprocess.run([node,f.name,str(ROOT/'viewer.html')],input=json.dumps([{'final':x['final'],'candidates':x['candidates']} for x in rows]),capture_output=True,text=True,timeout=60)
    Path(f.name).unlink()
    if out.returncode:R['attribution.js_parity']=0.0;fails.append({'case':'viewer.html','family':'attribution','check':'JS driver failed: '+out.stderr[:200]});return
    js=json.loads(out.stdout);same=sum(a==b for a,b in zip(js,py_labels))
    for x,a,b in zip(rows,js,py_labels):
        if a!=b:fails.append({'case':x['id'],'family':'attribution','check':f'JS {a} != Python {b}'})
    R['attribution.js_parity']=ratio(same,len(rows))

# ------------------------------------------------------------------ rollouts and imports (through the real importers)
def events(store,sid):return store.snapshot(sid)['events']
def eval_rollouts(split,R,fails,tmp):
    cases=[c for c in json.loads((DATA/'rollouts.json').read_text()) if split=='all' or c['split']==split]
    store=tracestore.TraceStore(Path(tmp)/'roll.db');c=Counter()
    for case in cases:
        sid,n=chatimport.import_rollouts(store,DATA/case['file']);ev=events(store,sid)
        rets=[e for e in ev if e['type']=='env.return'];gold=case['labels']['episodes']
        turns=[e['span_id'] for e in ev if e['type']=='span.start' and e.get('span_kind')=='turn']
        hacked={e['turn_id'] for e in ev if e['type']=='analysis.hack'}
        for g,r,t in zip(gold,rets,turns):
            c['n']+=1;c['succ']+=bool(r['passed'])==g['success'];c['ret']+=math.isclose(r['score'],g['return'],abs_tol=1e-6)
            if bool(r['passed'])!=g['success']:fails.append({'case':case['id'],'family':'rollouts','check':f"episode {g['episode']} success"})
            fl=t in hacked;c['h_tp']+=fl and g['hacked'];c['h_fp']+=fl and not g['hacked'];c['h_fn']+=g['hacked'] and not fl
            if fl!=g['hacked']:fails.append({'case':case['id'],'family':'rollouts','check':f"episode {g['episode']} hacked={g['hacked']} flagged={fl}"})
        k=sum(bool(r['passed']) for r in rets);N=len(rets)
        exact=all(math.isclose(math.comb(k,kk)/math.comb(N,kk),case['labels']['pass_hat_k'][str(kk)],abs_tol=1e-6) for kk in range(1,N+1))
        c['phk']+=exact;c['cases']+=1
    R.update({'rollouts.cases':len(cases),'rollouts.success.accuracy':ratio(c['succ'],c['n']),'rollouts.return.accuracy':ratio(c['ret'],c['n']),
              'rollouts.hack.recall':ratio(c['h_tp'],c['h_tp']+c['h_fn']),'rollouts.hack.precision':ratio(c['h_tp'],c['h_tp']+c['h_fp']) if c['h_tp']+c['h_fp'] else 1.0,
              'rollouts.pass_hat_k.exact':ratio(c['phk'],c['cases'])})

def eval_imports(split,R,fails,tmp):
    cases=[c for c in json.loads((DATA/'imports.json').read_text()) if split=='all' or c['split']==split];ok=0
    for i,case in enumerate(cases):
        store=tracestore.TraceStore(Path(tmp)/f'imp{i}.db');L=case['labels'];good=True;f=DATA/case['file']
        def bad(msg):
            nonlocal good;good=False;fails.append({'case':case['id'],'family':case['family'],'check':msg})
        if case['loader']=='chat':
            done=chatimport.import_file(store,f)
            if len(done)!=len(L['conversations']):bad(f"{len(done)} conversations, expected {len(L['conversations'])}")
            for (title,sid,n),want in zip(done,L['conversations']):
                ev=events(store,sid);r=sum(e['type']=='reasoning' for e in ev)
                if n!=want['turns']:bad(f'{title}: {n} turns, expected {want["turns"]}')
                if r!=want['reasoning']:bad(f'{title}: {r} reasoning, expected {want["reasoning"]}')
                if L.get('answers_must_exclude'):
                    texts=[store.payload(sid,e['id'])['text'] for e in ev if e['type']=='answer']
                    if any(L['answers_must_exclude'] in t for t in texts):bad('abandoned branch leaked into the answers')
        elif case['loader']=='session':
            sid,n=chatimport.import_session(store,f);ev=events(store,sid)
            if n!=len(L['turns']):bad(f'{n} turns, expected {len(L["turns"])}')
            agents=[e['span_id'] for e in ev if e['type']=='span.start' and e.get('span_kind')=='agent']
            for a,want in zip(agents,L['turns']):
                mine=[e for e in ev if e.get('span_id')==a]
                got={'tools':sum(e['type']=='tool.started' for e in mine),'failed':sum(e['type']=='tool.completed' and e.get('passed') is False for e in mine),
                     'reasoning':sum(e['type']=='reasoning' for e in mine),'ctx_points':sum(e['type']=='context.tokens' for e in mine)}
                ans=[store.payload(sid,e['id'])['text'] for e in mine if e['type']=='answer'];got['answer']=ans[0] if ans else None
                for k,v in want.items():
                    if got.get(k)!=v:bad(f'{a[-6:]}: {k}={got.get(k)!r}, expected {v!r}')
        elif case['loader']=='rollouts':
            sid,n=chatimport.import_rollouts(store,f);rets=[e for e in events(store,sid) if e['type']=='env.return']
            for r,want in zip(rets,L['episodes']):
                if not math.isclose(r['score'],want['return']) or bool(r['passed'])!=want['success'] or r['count']!=want['steps']:bad(f"episode got return={r['score']} success={r['passed']} steps={r['count']}")
            if len(rets)!=len(L['episodes']):bad(f'{len(rets)} episodes, expected {len(L["episodes"])}')
        ok+=good
    R.update({'imports.cases':len(cases),'imports.exact':ratio(ok,len(cases))})

# ------------------------------------------------------------------ report
def judge(R):
    rows=[]
    for k,(op,target,why) in TARGETS.items():
        v=R.get(k)
        if op=='known':status='KNOWN'
        elif v is None:status='SKIP'
        else:status='PASS' if (v>=target-1e-12 if op=='>=' else v<=target+1e-12) else 'FAIL'
        rows.append((k,v,op,target,status,why))
    return rows

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);ap.add_argument('--split',default='all',choices=('dev','test','all'))
    ap.add_argument('--json',default=None,help='write the full results here');ap.add_argument('--quiet',action='store_true')
    ap.add_argument('--extra',nargs='*',default=[],help='hand-labelled real runs (JSONL, from bench/export_for_labeling.py); only reviewed lines count')
    a=ap.parse_args(argv);R={};fails=[]
    with tempfile.TemporaryDirectory() as tmp:
        eval_runs(a.split,R,fails,a.extra);eval_trends(a.split,R,fails);eval_attribution(a.split,R,fails)
        eval_rollouts(a.split,R,fails,tmp);eval_imports(a.split,R,fails,tmp)
    rows=judge(R);bad=[r for r in rows if r[4]=='FAIL']
    if not a.quiet:
        print(f"Duo benchmark · split={a.split}\n")
        print(f"{'METRIC':44} {'VALUE':>8}  {'TARGET':>8}  STATUS")
        for k,v,op,t,st,why in rows:
            vs='–' if v is None else f'{v:.3f}';ts='' if t is None else f'{op}{t}'
            print(f'{k:44} {vs:>8}  {ts:>8}  {st}'+(f'   {why}' if why and st!='PASS' else ''))
        print('\nper family (cases with every label matched):')
        for k,v in R['runs.by_family'].items():print(f'  {k:20} {v}')
        print('\ntrend sensitivity by effect and n:',', '.join(f'{k} {v}' for k,v in R['trends.sensitivity_grid'].items()))
        print(f"attribution confusion (gold->got): {R['attribution.confusion']}")
        if fails:
            print(f'\n{len(fails)} case-level mismatches (first 25):')
            for f in fails[:25]:print(f"  {f['case']:32} {f['check']}")
        print(f"\n{'FAIL' if bad else 'PASS'}: {sum(r[4]=='PASS' for r in rows)} pass, {len(bad)} fail, {sum(r[4]=='KNOWN' for r in rows)} known limits, {sum(r[4]=='SKIP' for r in rows)} skipped")
    if a.json:Path(a.json).write_text(json.dumps({'split':a.split,'metrics':R,'targets':[dict(zip(('metric','value','op','target','status','why'),r)) for r in rows],'mismatches':fails},indent=1,default=str)+'\n')
    return 1 if bad else 0

if __name__=='__main__':sys.exit(main())
