#!/usr/bin/env python3
"""Measure the LLM trace reader (reader.py) on duo-bench: write public-format predictions, score them, save a calibration.

    python3 bench/predict_reader.py --provider claude [--model M] [--split test] [--limit 40] [--hints] [--workers 4]
    python3 bench/predict_reader.py --caller mock      # pipeline check only: answers come from the detectors, not a model

Scores only the tasks a reader can attempt from a trace (T1 loops, T2 look-first, T6 hacks, T7 flags, T12 security,
T13 error kinds); the other cases have no prediction and are reported as not attempted. Writes
  bench/results/reader-<name>.preds.jsonl   predictions (public format, rescorable with score.py)
  bench/results/reader-<name>.json          the score.py report
  bench/results/reader-calibration.json     per-field measured accuracy, which the viewer's reader card shows
Without --hints the model reads blind (no detector findings in the prompt): the score is the model's own reading.
"""
import argparse
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;DATA=HERE/'data';RESULTS=HERE/'results'
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(HERE))
import reader  # noqa: E402
import score  # noqa: E402

def jsonl(p):return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]

def cases(split='all',limit=None):
    out=[(c,'run') for c in jsonl(DATA/'runs.jsonl')]+[(c,'security') for c in jsonl(DATA/'security.jsonl')]
    out=[(c,k) for c,k in out if split=='all' or c['split']==split]
    if limit:  # keep every family represented: round-robin over families
        by={}
        for c,k in out:by.setdefault(c['family'],[]).append((c,k))
        pick=[];i=0
        while len(pick)<min(limit,len(out)):
            for fam in by.values():
                if i<len(fam) and len(pick)<limit:pick.append(fam[i])
            i+=1
        out=pick
    return out

def model_caller(provider,model=None,timeout=240):
    import duo
    def call(prompt):
        with tempfile.TemporaryDirectory(prefix='duo-reader-') as tmp:
            r=duo.call(provider,prompt,Path(tmp),timeout,model,quiet=True)
        if not r.get('ok'):raise RuntimeError(r.get('error') or 'call failed')
        return r.get('answer') or ''
    return call

def mock_caller(prompt):
    """Pipeline check: echo the deterministic detectors in the reader's JSON shape. Not a measurement of any model."""
    import re
    import longrun,security
    steps=[]
    for m in re.finditer(r'^\[(\d+)\] (\S+) (.*?) · (ok|FAILED)',prompt,re.M):
        steps.append({'ref':int(m.group(1)),'tool':m.group(2),'target':m.group(3),'ok':m.group(4)=='ok'})
    rd=longrun.read(steps) or {};sec=security.review(steps)
    return json.dumps({'summary':f'{len(steps)} steps.','outcome':'unknown','loops':[{'start_ref':L['ref']} for L in rd.get('loops',[])],
                       'look_first_ref':(rd.get('look_first') or {}).get('ref'),'flags':[],'hacks':[],
                       'security':[{'ref':f['ref'],'category':f['category']} for f in sec['findings']],
                       'error_kinds':[{'ref':e['ref'],'kind':e['kind']} for e in sec['errors']],'confidence':0.5})

def predict(caller,split='all',limit=None,hints=False,workers=4,progress=None):
    todo=cases(split,limit)
    def one(ck):
        c,kind=ck;r=reader.read(c['steps'],caller,request=c.get('request') or c['labels'].get('note'),hints=hints)
        if progress:progress(c['id'],r)
        return {'id':c['id'],'pred':reader.bench_pred(r,kind)},('error' in r)
    with ThreadPoolExecutor(max(1,workers)) as ex:res=list(ex.map(one,todo))
    return [p for p,_ in res],sum(e for _,e in res),len(todo)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--provider',choices=('claude','codex'));ap.add_argument('--caller',choices=('model','mock'),default='model')
    ap.add_argument('--model');ap.add_argument('--split',default='test',choices=('dev','test','all'));ap.add_argument('--limit',type=int)
    ap.add_argument('--hints',action='store_true');ap.add_argument('--workers',type=int,default=4);ap.add_argument('--timeout',type=int,default=240)
    ap.add_argument('--out-dir',default=str(RESULTS))
    a=ap.parse_args(argv)
    if a.caller=='model' and not a.provider:ap.error('--provider is required (or --caller mock for a pipeline check)')
    caller=mock_caller if a.caller=='mock' else model_caller(a.provider,a.model,a.timeout)
    name=('mock' if a.caller=='mock' else a.provider+(f'-{a.model}' if a.model else ''))+('-hints' if a.hints else '')
    preds,errors,n=predict(caller,a.split,a.limit,a.hints,a.workers,lambda i,r:print(('ERR ' if 'error' in r else 'ok  ')+i,file=sys.stderr))
    out=Path(a.out_dir);out.mkdir(parents=True,exist_ok=True)
    (out/f'reader-{name}.preds.jsonl').write_text(''.join(json.dumps(p,sort_keys=True)+'\n' for p in preds))
    notes=['',f'Reader `{name}`: {n} cases read, {errors} failed calls (scored as empty predictions).'+(' MOCK: detector echo, not a model.' if a.caller=='mock' else '')]
    rep,md=score.report(preds,a.split,f'reader-{name}',notes=notes)
    rep['reader']={'name':name,'cases':n,'failed_calls':errors,'hints':a.hints,'mock':a.caller=='mock'}
    (out/f'reader-{name}.json').write_text(json.dumps(rep,indent=1)+'\n');(out/f'reader-{name}.md').write_text(md)
    cal=reader.calibration(rep);cal.update(rep['reader'])
    (out/'reader-calibration.json').write_text(json.dumps(cal,indent=1)+'\n')
    print(md);print(f"calibration -> {out/'reader-calibration.json'}")
    return 0

if __name__=='__main__':sys.exit(main())
