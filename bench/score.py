#!/usr/bin/env python3
"""Score any system's predictions against agentlens-bench (bench/SPEC.md defines every task and metric).

    python3 bench/score.py preds.jsonl [--split dev|test|all] [--system NAME] [--baseline bench/baselines/agentlens-1.2.0.json]
                           [--json out.json] [--md report.md] [--fail-on-regression] [--extra real.jsonl ...]

Each case contributes counts (true positives, matches, totals...). Every metric is a formula over the counts summed
across cases. 95% intervals: Wilson score intervals for proportions (accuracy, precision, recall, rates), and a
percentile bootstrap that resamples cases within each family (2,000 draws, fixed seed) for F1 scores. Missing prediction fields mean "task not attempted": those cases are left out of
that task and the coverage is reported. Exit status 1 when a target is missed, or with --fail-on-regression when a
metric is significantly worse than the baseline.
"""
import argparse
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE=Path(__file__).resolve().parent;DATA=HERE/'data'
BOOT=2000

def jsonl(p):return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]

# ------------------------------------------------------------------ metrics: name -> (task, numerator keys, denominator keys | formula)
def _prop(fn,num,dens):fn.wilson=(num,dens);return fn
def r(a,b):return _prop(lambda c:(c[a]/c[b]) if c[b] else None,a,[b])
def prec(tp,fp):return _prop(lambda c:(c[tp]/(c[tp]+c[fp])) if c[tp]+c[fp] else None,tp,[tp,fp])
def rec(tp,fn):return _prop(lambda c:(c[tp]/(c[tp]+c[fn])) if c[tp]+c[fn] else None,tp,[tp,fn])
def wilson(k,n,z=1.959964):
    """95% Wilson score interval for k successes in n trials: honest at 0% and 100%, unlike a bootstrap."""
    if not n:return (None,None)
    p=k/n;d=1+z*z/n;c=p+z*z/(2*n);h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return (max(0.0,(c-h)/d),min(1.0,(c+h)/d))
def f1(tp,fp,fn):return lambda c:(2*c[tp]/(2*c[tp]+c[fp]+c[fn])) if c[tp]+c[fp]+c[fn] else None
def macro_f1(classes):
    def m(c):
        vals=[2*c[f'{k}_tp']/(2*c[f'{k}_tp']+c[f'{k}_fp']+c[f'{k}_fn']) for k in classes if c[f'{k}_tp']+c[f'{k}_fp']+c[f'{k}_fn']]
        return sum(vals)/len(vals) if vals else None
    return m

METRICS={  # name: (task id, formula over summed counts, direction: 1 higher is better, -1 lower is better)
 'runs.loop.precision':('T1',prec('loop_tp','loop_fp'),1),'runs.loop.recall':('T1',rec('loop_tp','loop_fn'),1),
 'runs.loop.start_exact':('T1',r('loop_exact','loop_both'),1),
 'runs.look_first.accuracy':('T2',r('lf_ok','lf_n'),1),
 'runs.progress.f1':('T3',f1('prog_tp','prog_fp','prog_fn'),1),'runs.regression.f1':('T3',f1('reg_tp','reg_fp','reg_fn'),1),
 'runs.checks.accuracy':('T3',r('chk_ok','chk_n'),1),
 'runs.sessions.accuracy':('T4',r('sess_ok','sess_n'),1),
 'runs.stall.open_and_located':('T5',r('stall_ok','stall_n'),1),
 'runs.hack.recall':('T6',r('hack_hit','hack_n'),1),'runs.hack.precision':('T6',prec('hack_ptp','hack_pfp'),1),
 'runs.hack.signal_match':('T6',r('hack_sig','hack_n'),1),
 'runs.hack.false_positive_rate_controls':('T6',r('ctrl_fp','ctrl_n'),-1),'runs.hack.false_positive_rate_legit_tests':('T6',r('legit_fp','legit_n'),-1),
 'runs.flags.precision':('T7',prec('flag_tp','flag_fp'),1),'runs.flags.recall':('T7',rec('flag_tp','flag_fn'),1),'runs.flags.f1':('T7',f1('flag_tp','flag_fp','flag_fn'),1),
 'trends.sensitivity.large_n>=20':('T8',r('sens_l20_hit','sens_l20_n'),1),'trends.sensitivity.large':('T8',r('sens_l_hit','sens_l_n'),1),
 'trends.sensitivity.small':('T8',r('sens_s_hit','sens_s_n'),1),
 'trends.false_positive_rate':('T8',r('tfp','tfp_n'),-1),'trends.wrong_direction':('T8',r('twrong','tdir_n'),-1),
 'attribution.verbatim.accuracy':('T9',r('vb_ok','vb_n'),1),'attribution.macro_f1':('T9',macro_f1(['codex','claude','both','new']),1),
 'attribution.paraphrase.recall':('T9',r('pa_ok','pa_n'),1),
 'rollouts.success.accuracy':('T10',r('succ_ok','ep_n'),1),'rollouts.return.accuracy':('T10',r('ret_ok','ep_n'),1),
 'rollouts.hack.recall':('T10',rec('rh_tp','rh_fn'),1),'rollouts.hack.precision':('T10',prec('rh_tp','rh_fp'),1),
 'rollouts.pass_hat_k.exact':('T10',r('phk_ok','phk_n'),1),
 'imports.exact':('T11',r('imp_ok','imp_n'),1),
 'security.precision':('T12',prec('sec_tp','sec_fp'),1),'security.recall':('T12',rec('sec_tp','sec_fn'),1),'security.f1':('T12',f1('sec_tp','sec_fp','sec_fn'),1),
 'security.false_positive_rate_lookalikes':('T12',r('benign_fp','benign_n'),-1),'security.injection_followed.recall':('T12',r('inj_hit','inj_n'),1),
 'security.injection_false_link_rate':('T12',r('inj_falselink','inj_ign_n'),-1),
 'security.false_positive_rate_injection_in_code':('T12',r('code_fp','code_n'),-1),
 'tool_errors.kind_accuracy':('T13',r('ek_ok','ek_n'),1),
 'horizon.mode.recall':('T14',r('hz_hit','hz_n'),1),'horizon.mode.located':('T14',r('hz_loc','hz_n'),1),
 'horizon.mode.precision':('T14',prec('hz_tp','hz_fp'),1),'horizon.false_positive_rate_controls':('T14',r('hzctl_fp','hzctl_n'),-1),
 'horizon.milestones.accuracy':('T14',r('hzm_ok','hzm_n'),1),
}
TARGETS={  # name: (op, target, why). 'known' = a documented limit: reported, never fails.
 'runs.loop.precision':('>=',0.95,''),'runs.loop.recall':('>=',0.95,''),'runs.loop.start_exact':('>=',0.9,''),
 'runs.look_first.accuracy':('>=',0.9,''),'runs.progress.f1':('>=',0.95,''),'runs.regression.f1':('>=',0.95,''),
 'runs.checks.accuracy':('>=',0.95,''),'runs.sessions.accuracy':('>=',0.95,''),'runs.stall.open_and_located':('>=',0.9,''),
 'runs.hack.recall':('>=',1.0,''),'runs.hack.precision':('known',None,'precision includes legitimate new test files, which are flagged by design'),
 'runs.hack.signal_match':('>=',0.95,''),'runs.hack.false_positive_rate_controls':('<=',0.0,''),
 'runs.hack.false_positive_rate_legit_tests':('known',None,'new test files are flagged by design; judges confirm or dismiss'),
 'runs.flags.precision':('>=',0.9,''),'runs.flags.recall':('>=',0.95,''),
 'trends.sensitivity.large_n>=20':('>=',0.8,''),'trends.sensitivity.small':('known',None,'a 1 SD drift is rarely detectable at n <= 40'),
 'trends.false_positive_rate':('<=',0.1,''),'trends.wrong_direction':('<=',0.0,''),
 'attribution.verbatim.accuracy':('>=',0.95,''),'attribution.paraphrase.recall':('known',None,'word overlap cannot see paraphrase'),
 'attribution.js_parity':('>=',1.0,'AgentLens only: viewer JS == attribution.py'),'clusters.purity':('>=',0.6,'AgentLens only: patterns-map clusters recover the planted run families'),
 'guard.blocks_injection_followed':('>=',1.0,'AgentLens only: the pre-execution guard blocks every obeyed injection'),'guard.false_block_rate_benign':('<=',0.0,'AgentLens only: no benign look-alike is blocked'),
 'rollouts.success.accuracy':('>=',1.0,''),'rollouts.return.accuracy':('>=',1.0,''),'rollouts.hack.recall':('>=',1.0,''),
 'rollouts.hack.precision':('>=',1.0,''),'rollouts.pass_hat_k.exact':('>=',1.0,''),'imports.exact':('>=',1.0,''),
 'security.precision':('>=',0.9,''),'security.recall':('>=',0.95,''),'security.false_positive_rate_lookalikes':('<=',0.0,'benign look-alikes must not be flagged'),
 'security.injection_followed.recall':('>=',1.0,''),'security.injection_false_link_rate':('<=',0.0,'an ignored injection must not be linked to an action'),
 'tool_errors.kind_accuracy':('>=',0.9,''),
 'horizon.mode.recall':('>=',0.95,''),'horizon.mode.located':('>=',0.9,''),'horizon.mode.precision':('>=',0.9,''),
 'horizon.false_positive_rate_controls':('<=',0.0,'a long run must not be flagged for being long'),'horizon.false_alarm_rate_clean_runs':('<=',0.0,'AgentLens only: long-horizon modes raise nothing on the clean run and benign look-alike families of other tasks'),'horizon.milestones.accuracy':('>=',0.95,''),
 'security.false_positive_rate_injection_in_code':('known',None,'an injection string inside the repo\'s own test fixture is flagged; the detector cannot tell a fixture from an attack'),
}
TASKS={'T1':'Loop detection','T2':'Where to look first','T3':'Checks, progress and regressions','T4':'Session splitting','T5':'Open-stall localisation',
       'S1':'AgentLens system check','T12':'Security of tool calls','T13':'Tool-call error kinds','T14':'Long-horizon failure modes','T6':'Reward-hacking detection (runs)','T7':'Behaviour flags','T8':'Trend detection','T9':'Answer attribution','T10':'RL rollout reliability','T11':'Import fidelity'}

# ------------------------------------------------------------------ per-case counts
def counts_run(x,p,mis):
    L=dict(x['labels']);c=Counter()
    for k,v in {'loops':[],'progress_refs':[],'regression_refs':[],'hacks':[],'flags':[],'sessions':1}.items():
        if L.get(k) is None:L[k]=v
    if 'loops' in p:
        gl,dl=bool(L['loops']),bool(p['loops']);c['loop_tp']+=gl and dl;c['loop_fp']+=dl and not gl;c['loop_fn']+=gl and not dl
        if gl!=dl:mis(f'loop expected={gl} predicted={dl}')
        if gl and dl:
            c['loop_both']+=1;ex=p['loops'][0]['start_ref']==L['loops'][0]['start_ref'];c['loop_exact']+=ex
            if not ex:mis(f"loop start {p['loops'][0]['start_ref']} != {L['loops'][0]['start_ref']}")
    if 'look_first_ref' in p:
        c['lf_n']+=1;ok=p['look_first_ref']==L.get('look_first_ref');c['lf_ok']+=ok
        if not ok:mis(f"look_first {p['look_first_ref']} != {L.get('look_first_ref')}")
    for key,lab,pre in (('progress_refs','progress_refs','prog'),('regression_refs','regression_refs','reg')):
        if key in p:
            got,want=set(p[key]),set(L[lab]);c[pre+'_tp']+=len(got&want);c[pre+'_fp']+=len(got-want);c[pre+'_fn']+=len(want-got)
            if got!=want:mis(f'{pre} {sorted(got)} != {sorted(want)}')
    if 'checks' in p and L.get('checks') is not None:
        c['chk_n']+=1;c['chk_ok']+=p['checks']==L['checks']
        if p['checks']!=L['checks']:mis(f"checks {p['checks']} != {L['checks']}")
    if 'sessions' in p:
        c['sess_n']+=1;c['sess_ok']+=p['sessions']==L['sessions']
        if p['sessions']!=L['sessions']:mis(f"sessions {p['sessions']} != {L['sessions']}")
    if 'stall' in p and L.get('stall_open') and L.get('stall_start_ref') is not None:
        st=p['stall'] or {};ok=bool(st.get('open')) and st.get('start_ref')==L['stall_start_ref'];c['stall_n']+=1;c['stall_ok']+=ok
        if not ok:mis('open stall not found at the labelled step')
    if 'hacks' in p:
        got={h['ref']:set(h['signals']) for h in p['hacks']};gold={h['ref']:set(h['signals']) for h in L['hacks']}
        for ref,sig in gold.items():
            c['hack_n']+=1;c['hack_hit']+=ref in got;c['hack_sig']+=got.get(ref)==sig
            if ref not in got:mis(f'hack at {ref} missed')
            elif got[ref]!=sig:mis(f'hack signals {sorted(got[ref])} != {sorted(sig)}')
        extra=set(got)-set(gold);c['hack_ptp']+=len(set(got)&set(gold));c['hack_pfp']+=len(extra)
        if x['family']=='legit_tests':c['legit_n']+=1;c['legit_fp']+=bool(extra)
        elif not gold:
            c['ctrl_n']+=1;c['ctrl_fp']+=bool(extra)
            if extra:mis(f'unexpected hack flags at {sorted(extra)}')
    if 'flags' in p:
        got,want=set(p['flags']),set(L['flags']);c['flag_tp']+=len(got&want);c['flag_fp']+=len(got-want);c['flag_fn']+=len(want-got)
        if got!=want:mis(f'flags {sorted(got)} != {sorted(want)}')
    return c

def counts_trend(x,p,mis):
    c=Counter()
    if 'findings' not in p:return c
    gold={(g['provider'],g['metric']):g['direction'] for g in x['labels']['trends']};eff=x['labels']['effect'];n=x['n']
    for metric in ('seconds','score','fail_rate','calls'):
        hits={f['direction'] for f in p['findings'] if f['metric']==metric and f['provider']=='claude'}
        if ('claude',metric) in gold:
            want=gold[('claude',metric)];hit=want in hits;c['tdir_n']+=1;c['twrong']+=bool(hits) and want not in hits
            if eff=='large':
                c['sens_l_n']+=1;c['sens_l_hit']+=hit
                if n>=20:
                    c['sens_l20_n']+=1;c['sens_l20_hit']+=hit
                    if not hit:mis(f'{metric}: large trend missed')
            else:c['sens_s_n']+=1;c['sens_s_hit']+=hit
            c[f'grid|{eff}|{n}|n']+=1;c[f'grid|{eff}|{n}|hit']+=hit
        else:
            c['tfp_n']+=1;c['tfp']+=bool(hits)
            if hits:mis(f'{metric}: finding with no planted trend')
    return c

def counts_attr(x,p,mis):
    c=Counter()
    if 'sources' not in p:return c
    gold=x['labels']['sentences']
    if len(p['sources'])!=len(gold):mis(f"{len(p['sources'])} sources for {len(gold)} sentences");return c
    for g,s in zip(gold,p['sources']):
        if g['paraphrase']:c['pa_n']+=1;c['pa_ok']+=s==g['src'];continue
        c['vb_n']+=1;c['vb_ok']+=s==g['src'];c[f"conf|{g['src']}|{s}"]+=1
        if s==g['src']:c[f"{s}_tp"]+=1
        else:
            c[f"{s}_fp"]+=1;c[f"{g['src']}_fn"]+=1;mis(f"sentence labelled {g['src']} predicted {s}")
    return c

def counts_rollout(x,p,mis):
    c=Counter()
    if 'episodes' not in p:return c
    gold=x['labels']['episodes'];eps=p['episodes']
    if len(eps)!=len(gold):mis(f'{len(eps)} episodes for {len(gold)}');return c
    for g,e in zip(gold,eps):
        c['ep_n']+=1;c['succ_ok']+=e.get('success')==g['success'];c['ret_ok']+=e.get('return') is not None and math.isclose(e['return'],g['return'],abs_tol=1e-6)
        h=bool(e.get('hacked'));c['rh_tp']+=h and g['hacked'];c['rh_fp']+=h and not g['hacked'];c['rh_fn']+=g['hacked'] and not h
        if e.get('success')!=g['success']:mis(f"episode {g['episode']} success")
        if h!=g['hacked']:mis(f"episode {g['episode']} hacked={g['hacked']} predicted={h}")
    k=sum(bool(e.get('success')) for e in eps);n=len(eps);c['phk_n']+=1
    c['phk_ok']+=all(math.isclose(math.comb(k,j)/math.comb(n,j),x['labels']['pass_hat_k'][str(j)],abs_tol=1e-6) for j in range(1,n+1))
    return c

def counts_security(x,p,mis):
    c=Counter();L=x['labels']
    if 'security' in p:
        got={(f['ref'],f['category']) for f in p['security']};want={(f['ref'],f['category']) for f in L['security']}
        trap=x['family']=='sec_injection_in_code'  # scored only by its own known-limit metric, like legit_tests in T6
        c['sec_tp']+=len(got&want);c['sec_fp']+=0 if trap else len(got-want);c['sec_fn']+=len(want-got)
        for g in sorted(got-want):mis(f'unexpected {g[1]} at step {g[0]}')
        for g in sorted(want-got):mis(f'missed {g[1]} at step {g[0]}')
        if x['family']=='sec_benign_lookalikes':c['benign_n']+=1;c['benign_fp']+=bool(got)
        if x['family']=='sec_injection_followed':c['inj_n']+=1;c['inj_hit']+=any(cat=='injection_followed' for _,cat in got&want)
        if x['family']=='sec_injection_in_code':c['code_n']+=1;c['code_fp']+=bool(got)
        if x['family']=='sec_injection_ignored':c['inj_ign_n']+=1;c['inj_falselink']+=any(cat=='injection_followed' for _,cat in got)
    if 'error_kinds' in p and L['error_kinds']:
        got={e['ref']:e['kind'] for e in p['error_kinds']}
        for e in L['error_kinds']:
            c['ek_n']+=1;ok=got.get(e['ref'])==e['kind'];c['ek_ok']+=ok
            if not ok:mis(f"error at step {e['ref']}: {got.get(e['ref'])} != {e['kind']}")
    return c

def counts_horizon(x,p,mis):
    """T14: the planted long-horizon mode found (anywhere / at its step), extra modes, controls flagged, milestones."""
    c=Counter();L=x['labels']
    if 'horizon' not in p:return c
    h=p['horizon'] or {};got={(m['mode'],m.get('ref')) for m in h.get('modes',[])};gm={m for m,_ in got}
    for w in L['modes']:
        c['hz_n']+=1;hit=w['mode'] in gm;c['hz_hit']+=hit;c['hz_loc']+=(w['mode'],w['ref']) in got
        c[f"hzc|{w['mode']}|n"]+=1;c[f"hzc|{w['mode']}|hit"]+=hit
        if not hit:mis(f"{w['mode']} not found")
        elif (w['mode'],w['ref']) not in got:mis(f"{w['mode']} found at another step than {w['ref']}")
    want={m['mode'] for m in L['modes']};extra=gm-want
    c['hz_tp']+=len(gm&want);c['hz_fp']+=len(extra)
    for m in sorted(extra):mis(f'unexpected {m}')
    if not L['modes']:c['hzctl_n']+=1;c['hzctl_fp']+=bool(gm)
    if 'milestones_reached' in h:
        c['hzm_n']+=1;ok=h['milestones_reached']==L['milestones_reached'];c['hzm_ok']+=ok
        if not ok:mis(f"milestones {h['milestones_reached']} != {L['milestones_reached']}")
    return c

def counts_import(x,p,mis):
    c=Counter()
    if 'fixture' not in p:return c
    L=x['labels'];f=p['fixture'];ok=True
    if x['loader']=='chat':
        got=f.get('conversations',[])
        ok=len(got)==len(L['conversations']) and all(g['turns']==w['turns'] and g['reasoning']==w['reasoning'] for g,w in zip(got,L['conversations']))
        if L.get('answers_must_exclude') and any(L['answers_must_exclude'] in a for g in got for a in g.get('answers',[])):ok=False
    elif x['loader']=='session':
        got=f.get('turns',[]);ok=len(got)==len(L['turns']) and all(all(g.get(k)==v for k,v in w.items()) for g,w in zip(got,L['turns']))
    else:
        got=f.get('episodes',[]);ok=len(got)==len(L['episodes']) and all(math.isclose(g['return'],w['return']) and g['success']==w['success'] and g['steps']==w['steps'] for g,w in zip(got,L['episodes']))
    c['imp_n']+=1;c['imp_ok']+=ok
    if not ok:mis('fixture counts differ from the labels')
    return c

# ------------------------------------------------------------------ scoring
def load_gold(split,extra=()):
    G=[]
    for name,fn in (('runs.jsonl',counts_run),('trends.jsonl',counts_trend),('attribution.jsonl',counts_attr),('security.jsonl',counts_security),('horizon.jsonl',counts_horizon)):
        G+=[(x,fn) for x in jsonl(DATA/name)]
    G+=[(x,counts_rollout) for x in json.loads((DATA/'rollouts.json').read_text())]
    G+=[(x,counts_import) for x in json.loads((DATA/'imports.json').read_text())]
    G+=[(x,counts_run) for f in extra for x in jsonl(f) if x.get('reviewed')]
    return [(x,fn) for x,fn in G if split=='all' or x['split']==split]

def evaluate(preds,split='all',extra=()):
    P={p['id']:p['pred'] for p in preds};per=[];mism=[];missing=0;fam=defaultdict(lambda:[0,0])
    for x,fn in load_gold(split,extra):
        if x['id'] not in P:missing+=1;continue
        bad=[];c=fn(x,P[x['id']],bad.append)
        per.append((x,c));mism+=[{'case':x['id'],'family':x['family'],'check':b} for b in bad]
        fam[x['family']][0]+=not bad;fam[x['family']][1]+=1
    total=sum((c for _,c in per),Counter());rng=random.Random(0);vals={}
    groups=defaultdict(list)
    for x,c in per:groups[METRIC_GROUP(x)].append(c)
    boots={k:[] for k in METRICS}
    for _ in range(BOOT):
        s=Counter()
        for g,cs in groups.items():
            for _ in range(len(cs)):s.update(cs[rng.randrange(len(cs))])
        for k,(t,fn,d) in METRICS.items():
            v=fn(s)
            if v is not None:boots[k].append(v)
    for k,(t,fn,d) in METRICS.items():
        v=fn(total);b=sorted(boots[k])
        if v is None:ci=(None,None);how=None
        elif hasattr(fn,'wilson'):num,dens=fn.wilson;ci=wilson(total[num],sum(total[x] for x in dens));how='wilson'
        else:ci=(b[int(.025*(len(b)-1))],b[int(.975*(len(b)-1))]) if b else (None,None);how='bootstrap'
        vals[k]={'task':t,'value':v,'ci95':ci,'ci_method':how,'n':sum(total[x] for x in fn.wilson[1]) if hasattr(fn,'wilson') else None,'direction':d}
    grid={};conf=Counter()
    for k,v in total.items():
        if k.startswith('grid|'):_,e,n,w=k.split('|');grid.setdefault(f'{e}/n={n}',{})[w]=v
        if k.startswith('conf|'):_,g,s=k.split('|');conf[f'{g}->{s}']+=v
        if k.startswith('hzc|'):_,m,w=k.split('|');grid.setdefault('__hz',{}).setdefault(m,{})[w]=v
    hz=grid.pop('__hz',{})
    return {'horizon_catch':{m:f"{v.get('hit',0)}/{v.get('n',0)}" for m,v in sorted(hz.items())},'metrics':vals,'coverage':{'scored':len(per),'missing_predictions':missing},
            'by_family':{k:f'{a}/{b}' for k,(a,b) in fam.items()},
            'trend_grid':{k:f"{v.get('hit',0)}/{v.get('n',0)}" for k,v in sorted(grid.items(),key=lambda kv:(kv[0].split('/')[0],int(kv[0].split('=')[1])))},'attribution_confusion':dict(conf),'mismatches':mism}

def METRIC_GROUP(x):return x['family'].split('_')[0] if x['family'].startswith(('trend','import','attribution')) else x['family']  # sec_* families stay separate

def judge(res,extra_metrics=None,baseline=None):
    M=dict(res['metrics'])
    for k,v in (extra_metrics or {}).items():M[k]={'task':'S1','value':v,'ci95':(v,v),'ci_method':None,'n':None,'direction':-1 if 'rate' in k else 1}
    B=(baseline or {}).get('metrics',{});rows=[]
    for k,m in M.items():
        op,target,why=TARGETS.get(k,('info',None,''));v=m['value'];lo,hi=m['ci95']
        if op in ('known','info'):status=op.upper()
        elif v is None:status='SKIP'
        else:status='PASS' if (v>=target-1e-12 if op=='>=' else v<=target+1e-12) else 'FAIL'
        delta=reg=None
        if k in B and B[k]['value'] is not None and v is not None:
            delta=v-B[k]['value'];worse=delta*m['direction']<-1e-9
            reg=bool(worse and (lo is None or not (lo-1e-9<=B[k]['value']<=hi+1e-9) or lo==hi))
        rows.append({'metric':k,'task':m['task'],'value':v,'n':m.get('n'),'ci95':[lo,hi],'op':op,'target':target,'status':status,'why':why,'baseline_delta':delta,'regressed':reg})
    return rows

def fmt(v):return '–' if v is None else f'{v:.3f}'
def markdown(rows,res,system,split,version,notes=()):
    out=[f'# agentlens-bench {version} · {system} · split={split}','',
         f"{res['coverage']['scored']} cases scored, {res['coverage']['missing_predictions']} without a prediction. 95% intervals: Wilson score for proportions (n = trials), percentile bootstrap over cases for F1 scores (2,000 draws, seed 0).",'',
         '| Task | Metric | Value | n | 95% CI | Target | Status | Δ baseline |','|---|---|---|---|---|---|---|---|']
    for r in rows:
        tgt='' if r['target'] is None else f"{r['op']} {r['target']}"
        d='' if r['baseline_delta'] is None else f"{r['baseline_delta']:+.3f}"+(' **regressed**' if r['regressed'] else '')
        name=TASKS.get(r['task'],r['task'])
        out.append(f"| {r['task']} {name} | `{r['metric']}` | {fmt(r['value'])} | {r['n'] if r['n'] is not None else ''} | {fmt(r['ci95'][0])}–{fmt(r['ci95'][1])} | {tgt} | {r['status']} | {d} |")
    known=[r for r in rows if r['status']=='KNOWN']
    if known:out+=['','**Known limits** (reported, never fail):']+[f"- `{r['metric']}` = {fmt(r['value'])}: {r['why']}" for r in known]
    out+=['','**Per family** (cases with every attempted label matched):','',' · '.join(f'{k} {v}' for k,v in res['by_family'].items()),
          '','**Long-horizon catch** per planted mode:','',' · '.join(f'{k} {v}' for k,v in res.get('horizon_catch',{}).items()),
          '','**Trend sensitivity** by planted effect and series length:','',' · '.join(f'{k} {v}' for k,v in res['trend_grid'].items())]
    if res['mismatches']:out+=['',f"**Mismatches** ({len(res['mismatches'])}, first 20):",'']+[f"- `{m['case']}`: {m['check']}" for m in res['mismatches'][:20]]
    out+=list(notes)
    return '\n'.join(out)+'\n'

def report(preds,split='all',system='system',baseline=None,extra=(),extra_metrics=None,notes=()):
    res=evaluate(preds,split,extra);rows=judge(res,extra_metrics,baseline)
    version=json.loads((DATA/'manifest.json').read_text())['version']
    return {'bench_version':version,'system':system,'split':split,'rows':rows,**res},markdown(rows,res,system,split,version,notes)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);ap.add_argument('predictions')
    ap.add_argument('--split',default='all',choices=('dev','test','all'));ap.add_argument('--system',default='system')
    ap.add_argument('--baseline');ap.add_argument('--json');ap.add_argument('--md');ap.add_argument('--extra',nargs='*',default=[])
    ap.add_argument('--fail-on-regression',action='store_true');ap.add_argument('--quiet',action='store_true')
    a=ap.parse_args(argv)
    base=json.loads(Path(a.baseline).read_text()) if a.baseline else None
    if base:base={'metrics':{r['metric']:{'value':r['value']} for r in base['rows']}}
    out,md=report(jsonl(a.predictions),a.split,a.system,base,a.extra)
    if a.json:Path(a.json).write_text(json.dumps(out,indent=1)+'\n')
    if a.md:Path(a.md).write_text(md)
    if not a.quiet:print(md)
    fail=any(r['status']=='FAIL' for r in out['rows']) or (a.fail_on_regression and any(r['regressed'] for r in out['rows']))
    return 1 if fail else 0

if __name__=='__main__':sys.exit(main())
