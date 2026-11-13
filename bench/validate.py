#!/usr/bin/env python3
"""Validate the benchmark data (or a predictions file) against bench/schema, and check the manifest checksums.

    python3 bench/validate.py                         # every data file + checksums
    python3 bench/validate.py --predictions preds.jsonl

Implements the subset of JSON Schema 2020-12 the schemas use (type, enum, required, properties, items, minItems,
minimum), so it needs no dependencies. Exit status 1 on any error.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent;DATA=HERE/'data';SCHEMA=HERE/'schema'
TYPES={'string':str,'integer':int,'number':(int,float),'boolean':bool,'object':dict,'array':list,'null':type(None)}

def check(value,schema,path='$'):
    errs=[]
    t=schema.get('type')
    if t:
        ts=t if isinstance(t,list) else [t]
        ok=any(isinstance(value,TYPES[x]) and not (x in ('integer','number') and isinstance(value,bool)) for x in ts)
        if not ok:return [f'{path}: expected {"/".join(ts)}, got {type(value).__name__}']
    if 'enum' in schema and value not in schema['enum']:errs.append(f'{path}: {value!r} not in {schema["enum"]}')
    if 'minimum' in schema and isinstance(value,(int,float)) and not isinstance(value,bool) and value<schema['minimum']:errs.append(f'{path}: {value} < {schema["minimum"]}')
    if isinstance(value,dict):
        for k in schema.get('required',[]):
            if k not in value:errs.append(f'{path}: missing {k!r}')
        for k,sub in schema.get('properties',{}).items():
            if k in value:errs+=check(value[k],sub,f'{path}.{k}')
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0):errs.append(f'{path}: fewer than {schema["minItems"]} items')
        if 'items' in schema:
            for i,v in enumerate(value):errs+=check(v,schema['items'],f'{path}[{i}]')
    return errs

def load(name):return json.loads((SCHEMA/name).read_text())
def jsonl(path):return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]

def validate_data(extra=()):
    errs=[];m=json.loads((DATA/'manifest.json').read_text())
    for rel,digest in m.get('files',{}).items():
        f=DATA/rel
        if not f.exists():errs.append(f'manifest: {rel} missing')
        elif hashlib.sha256(f.read_bytes()).hexdigest()!=digest:errs.append(f'manifest: {rel} checksum differs (regenerate or bump the version)')
    for fname,sch in (('runs.jsonl','run.schema.json'),('trends.jsonl','trend.schema.json'),('attribution.jsonl','attribution.schema.json'),('security.jsonl','security.schema.json'),('horizon.jsonl','horizon.schema.json')):
        s=load(sch)
        for x in jsonl(DATA/fname):errs+=[f'{fname} {x.get("id")}: {e}' for e in check(x,s)]
    for fname,sch in (('rollouts.json','rollout_case.schema.json'),('imports.json','import_case.schema.json')):
        s=load(sch)
        for x in json.loads((DATA/fname).read_text()):
            errs+=[f'{fname} {x.get("id")}: {e}' for e in check(x,s)]
            if not (DATA/x['file']).exists():errs.append(f'{fname} {x["id"]}: file {x["file"]} missing')
    s=load('run.schema.json')
    for f in extra:
        for x in jsonl(f):errs+=[f'{f} {x.get("id")}: {e}' for e in check(x,s)]
    ids=[c['id'] for c in m['cases']]
    if len(ids)!=len(set(ids)):errs.append('manifest: duplicate case ids')
    return errs,m

def validate_predictions(path):
    s=load('prediction.schema.json');errs=[]
    for x in jsonl(path):errs+=[f'{x.get("id")}: {e}' for e in check(x,s)]
    return errs

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);ap.add_argument('--predictions');ap.add_argument('--extra',nargs='*',default=[])
    a=ap.parse_args(argv)
    if a.predictions:errs=validate_predictions(a.predictions);what=a.predictions
    else:
        errs,m=validate_data(a.extra);what=f"duo-bench {m['version']}: {len(m['cases'])} cases, {len(m.get('files',{}))} checksummed files"
    for e in errs[:50]:print('  '+e)
    print(('INVALID' if errs else 'VALID')+f': {what}'+(f' ({len(errs)} errors)' if errs else ''))
    return 1 if errs else 0

if __name__=='__main__':sys.exit(main())
