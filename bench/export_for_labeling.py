#!/usr/bin/env python3
"""Export real stored turns into the benchmark's run format, ready for a person to label.

    python3 bench/export_for_labeling.py [--db ~/.agentlens/traces.db] [--session ID] [--limit 20] --out bench/data/real/todo.jsonl

Each line is one model run from one turn: its tool steps (same fields as bench/data/runs.jsonl) and a `labels`
block where every field is null, plus `suggested`: what the detectors currently say. A labeller fills `labels`
(copying from `suggested` only after checking it against the steps), sets `"reviewed": true`, and saves the file
under bench/data/real/. `bench/evaluate.py --extra bench/data/real/*.jsonl` then scores the detectors on it;
unreviewed lines are skipped. Nothing leaves the machine; prompts and outputs stay in the file you write.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
import analysis, longrun, tracestore  # noqa: E402

def runs_from_session(store,sid):
    snap=store.snapshot(sid) or {'events':[]};ev=snap['events'];spans={};steps=defaultdict(list);pending={}
    for e in ev:
        if e['type']=='span.start' and e.get('span_kind')=='agent' and e.get('role')!='synthesis':spans[e['span_id']]={'turn':e.get('parent_id'),'provider':e['provider'],'role':e.get('role')}
    for e in ev:
        sp=e.get('span_id')
        if sp not in spans:continue
        if e['type']=='tool.started':
            st={'ref':e['id'],'tool':e.get('tool'),'target':e.get('detail') or '','ok':None,'at':e['timestamp'],'seconds':0.0,'output':''}
            p=store.payload(sid,e['id'])
            if p and p.get('text'):st['input']=p['text'][:6000]
            steps[sp].append(st);pending[(sp,e.get('item_id') or e.get('tool'))]=st
        elif e['type']=='tool.completed':
            st=pending.pop((sp,e.get('item_id') or e.get('tool')),None)
            if st:
                st['ok']=e.get('passed') is not False;st['seconds']=round(e['timestamp']-st['at'],3)
                p=store.payload(sid,e['id'])
                if p and p.get('text'):st['output']=p['text'][-900:]
    for sp,meta in spans.items():
        if steps[sp]:yield sp,meta,[s for s in steps[sp] if s['ok'] is not None]

def suggest(steps):
    rd=longrun.read(steps) or {}
    st=[]
    for s in steps:
        k=analysis.action_type({'kind':'tool','tool':s['tool'],'target':s['target']});st.append({**s,'kind':'tool','type':k,'key':analysis.target_key(s,k),'input':s.get('input') or ''})
    hacks=defaultdict(list)
    for h in analysis.hack_signals(st):hacks[h['ref']].append(h['signal'])
    _,flags=analysis.behaviour(st)
    return {'loops':[{'start_ref':L['ref'],'end_ref':None,'bursts':L['bursts']} for L in rd.get('loops',[])],
            'look_first_ref':(rd.get('look_first') or {}).get('ref'),'progress_refs':[c['ref'] for c in rd.get('checks',[]) if c['progress']],
            'regression_refs':[c['ref'] for c in rd.get('checks',[]) if c['regression']],'sessions':len(rd.get('sessions',[])) or 1,
            'stall_open':(rd.get('stall') or {}).get('open'),'stall_start_ref':(rd.get('stall') or {}).get('ref'),'hacks':[{'ref':r,'signals':sorted(s)} for r,s in hacks.items()],
            'flags':sorted(f['flag'] for f in flags),'checks':len(rd.get('checks',[]))}

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--db',default=str(tracestore.default_path()));ap.add_argument('--session');ap.add_argument('--limit',type=int,default=20)
    ap.add_argument('--out',required=True)
    a=ap.parse_args(argv);store=tracestore.TraceStore(a.db)
    sessions=[a.session] if a.session else [s['id'] for s in store.sessions(200)]
    out=[];empty={k:None for k in ('loops','look_first_ref','progress_refs','regression_refs','sessions','stall_open','stall_start_ref','hacks','flags','checks')}
    for sid in sessions:
        for sp,meta,steps in runs_from_session(store,sid):
            if len(out)>=a.limit:break
            out.append({'id':f'real-{sid[:8]}-{sp[-8:]}','family':'real','split':'test','style':meta['provider'],'session':sid,'turn':meta['turn'],
                        'reviewed':False,'steps':steps,'labels':dict(empty,note=''),'suggested':suggest(steps)})
    Path(a.out).parent.mkdir(parents=True,exist_ok=True)
    Path(a.out).write_text(''.join(json.dumps(x)+'\n' for x in out))
    print(f'wrote {len(out)} runs to {a.out}; label them, set "reviewed": true, then run bench/evaluate.py --extra {a.out}')

if __name__=='__main__':main()
