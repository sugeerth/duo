"""Fixed eval set + replay: the same tasks, both models, comparable over time.

Cases are real prompts taken from the trace DB (or, opt-in, from ~/.claude/history.jsonl).
Write tasks are never included: replaying them would modify files. Replays run both
models on every case with the eval judge, save a scored record to ~/.duo/replays/,
and compare against earlier records. --no-lessons gives a controlled A/B for lessons.
"""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re

SECRET=re.compile(r'(sk-[A-Za-z0-9]{12,}|api[_-]?key|password|passwd|secret[_-]?key|BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,})',re.I)

def home():return Path(os.environ.get('DUO_HOME') or Path.home()/'.duo').expanduser()
def path():return Path(os.environ.get('DUO_EVALSET') or home()/'evalset.json').expanduser()
def runs_dir():return home()/'replays'

def load():
    try:return json.loads(path().read_text())
    except (OSError,ValueError):return []

def save(cases):
    p=path();p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(cases,indent=2,ensure_ascii=False))

def case_id(prompt):return hashlib.sha1(prompt.strip().lower().encode()).hexdigest()[:8]

def words(text):return {w for w in re.findall(r'[a-z0-9]+',text.lower()) if len(w)>2}
def similar(a,b,threshold=0.5):
    wa,wb=words(a),words(b)
    return bool(wa and wb) and len(wa&wb)/len(wa|wb)>=threshold

# ---------- candidate sources ----------
def from_store(store):
    """Prompts of past respond/read turns in the trace DB, newest first."""
    if not store:return []
    store.flush(3)
    with store.lock,store.connect() as db:
        rows=db.execute('''SELECT p.text,s.mode,ss.cwd,s.session_id,s.span_id,s.start FROM payloads p
            JOIN spans s ON s.span_id=p.span_id JOIN sessions ss ON ss.id=s.session_id
            WHERE p.kind='prompt' AND s.kind='turn' ORDER BY s.start DESC''').fetchall()
    out=[]
    for text,mode,cwd,sid,turn,ts in rows:
        mode=mode or ''
        if 'write' in mode:continue
        out.append({'prompt':text,'kind':'respond' if mode.startswith('respond') else 'read','cwd':cwd,'source':f'trace:{sid[:8]}:{turn}','ts':ts})
    return out

def from_history(file,classify,limit=4000):
    """Opt-in: prompts typed into Claude Code (~/.claude/history.jsonl). classify(prompt)->respond|read|write."""
    out=[]
    try:lines=Path(file).expanduser().read_text().splitlines()[-limit:]
    except OSError:return []
    for line in reversed(lines):
        try:item=json.loads(line)
        except ValueError:continue
        text=(item.get('display') or '').strip()
        if not text or item.get('pastedContents'):continue  # pasted blobs are not reproducible tasks
        kind=classify(text)
        if kind=='write':continue
        out.append({'prompt':text,'kind':kind,'cwd':item.get('project'),'source':'claude-history','ts':(item.get('timestamp') or 0)/1000})
    return out

def usable(c):
    t=c['prompt'].strip();n=len(t.split())
    if t.startswith('/') or n<6 or n>400:return False
    if SECRET.search(t):return False  # never replay anything that looks like a credential
    if re.match(r'^\s*(ok|okay|thanks|thank you|yes|no|pls|please|continue|go on|do it|finalize|lmk)\b',t,re.I) and n<12:return False
    return True

def select(cands,n=20,existing=()):
    """Diverse pick: usable, no near-duplicates, alternating task kinds, newest first."""
    chosen=list(existing);picked=[]
    by={}
    for c in cands:
        if usable(c):by.setdefault(c['kind'],[]).append(c)
    order=sorted(by,key=lambda k:-len(by[k]))
    while len(picked)<n and any(by.values()):
        for k in order:
            while by[k]:
                c=by[k].pop(0)
                if any(similar(c['prompt'],x['prompt']) for x in chosen):continue
                chosen.append(c);picked.append(c);break
            if len(picked)>=n:break
    return picked

def add_cases(new):
    cases=load();ids={c['id'] for c in cases}
    for c in new:
        cid=case_id(c['prompt'])
        if cid in ids:continue
        cases.append({'id':cid,'prompt':c['prompt'],'kind':c.get('kind','respond'),'cwd':c.get('cwd'),'source':c.get('source','manual'),
                      'added':datetime.datetime.now().strftime('%Y-%m-%d %H:%M')});ids.add(cid)
    save(cases);return cases

def remove(cid):
    cases=[c for c in load() if c['id']!=cid];save(cases);return cases

# ---------- replay records ----------
def wilson(k,n,z=1.96):
    if not n:return (0.0,1.0)
    p=k/n;d=1+z*z/n;c=p+z*z/(2*n);r=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n));return ((c-r)/d,(c+r)/d)

def sign_p(k,n):
    if not n:return 1.0
    m=min(k,n-k);return min(1.0,2*sum(math.comb(n,i) for i in range(m+1))/2**n)

def case_result(case,turn_id,judge_results):
    """Collapse per-judge results into one row: mean overall per model, panel winner (split if judges disagree)."""
    per={};winners=[]
    for r in judge_results or []:
        winners.append(r['winner'])
        for p,item in r['per'].items():per.setdefault(p,[]).append(item['overall'])
    scores={p:round(sum(v)/len(v),2) for p,v in per.items()}
    winner=None
    if len(scores)==2 and winners:winner=winners[0] if len(set(winners))==1 else 'split'
    return {'id':case['id'],'prompt':case['prompt'][:200],'kind':case['kind'],'turn_id':turn_id,'scores':scores,'winner':winner,
            'judges':len(judge_results or []),'ok':bool(judge_results)}

def summarize(rows):
    S={'cases':len(rows),'judged':sum(r['ok'] for r in rows),'models':{}}
    for p in ('codex','claude'):
        v=[r['scores'][p] for r in rows if p in r['scores']]
        S['models'][p]={'mean':round(sum(v)/len(v),3) if v else None,'n':len(v),'wins':sum(r['winner']==p for r in rows)}
    decisive=S['models']['codex']['wins']+S['models']['claude']['wins']
    lead='claude' if S['models']['claude']['wins']>S['models']['codex']['wins'] else 'codex'
    k=S['models'][lead]['wins']
    S.update({'decisive':decisive,'ties':sum(r['winner'] in ('tie','split') for r in rows),'leader':lead if decisive else None,
              'win_rate':round(k/decisive,3) if decisive else None,'ci':[round(x,3) for x in wilson(k,decisive)],'p':round(sign_p(S['models']['codex']['wins'],decisive),4)})
    return S

def save_run(record):
    d=runs_dir();d.mkdir(parents=True,exist_ok=True)
    f=d/(record['started'].replace(':','').replace(' ','_')+('_nolessons' if record['config'].get('no_lessons') else '')+'.json')
    f.write_text(json.dumps(record,indent=2,ensure_ascii=False));return f

def list_runs():
    d=runs_dir()
    return sorted(d.glob('*.json')) if d.exists() else []

def compare(a,b):
    """b relative to a: per-model mean deltas and per-case winner flips on shared cases."""
    A={r['id']:r for r in a['rows']};B={r['id']:r for r in b['rows']}
    shared=[i for i in A if i in B]
    out={'shared':len(shared),'models':{},'flips':[]}
    for p in ('codex','claude'):
        d=[B[i]['scores'][p]-A[i]['scores'][p] for i in shared if p in A[i]['scores'] and p in B[i]['scores']]
        better=sum(x>0 for x in d);worse=sum(x<0 for x in d)
        lo,hi=bootstrap(d)
        out['models'][p]={'delta':round(sum(d)/len(d),3) if d else None,'n':len(d),'better':better,'worse':worse,'p':round(sign_p(better,better+worse),4),
                          'ci':[lo,hi],'needed':power_hint(d),'improved':lo is not None and lo>0,'regressed':hi is not None and hi<0}
    for i in shared:
        if A[i]['winner']!=B[i]['winner']:out['flips'].append({'id':i,'prompt':A[i]['prompt'][:90],'from':A[i]['winner'],'to':B[i]['winner']})
    return out

def render_summary(record):
    S=record['summary'];m=S['models'];lines=[f"Replay {record['started']} · {S['judged']}/{S['cases']} cases judged · lessons {'OFF' if record['config'].get('no_lessons') else 'on ('+str(record['config'].get('lessons',0))+')'}"]
    lines.append(f"  Codex  mean {m['codex']['mean']}  wins {m['codex']['wins']}")
    lines.append(f"  Claude mean {m['claude']['mean']}  wins {m['claude']['wins']}   ties/split {S['ties']}")
    if S['decisive']:
        verdict='significant' if S['decisive']>=20 and S['p']<0.05 else 'inconclusive'
        lines.append(f"  {S['leader'].title()} wins {round(100*S['win_rate'])}% of decisive cases (95% CI {round(100*S['ci'][0])}–{round(100*S['ci'][1])}%), sign test p={S['p']} → {verdict}")
    for r in record['rows']:
        s=r['scores'];lines.append(f"  [{r['id']}] {r['kind']:<7} codex {s.get('codex','–')} · claude {s.get('claude','–')} · {r['winner'] or 'not judged'} · {r['prompt'][:70]}")
    return '\n'.join(lines)

def render_compare(a,b,c):
    lines=[f"Compare {a['started']} → {b['started']} on {c['shared']} shared cases"]
    for p,v in c['models'].items():
        ci=f"paired 95% CI {v['ci'][0]}…{v['ci'][1]}" if v.get('ci') and v['ci'][0] is not None else 'CI needs ≥2 shared cases'
        verdict='improved' if v.get('improved') else 'regressed' if v.get('regressed') else 'no clear change'
        lines.append(f"  {p.title():<6} Δ {v['delta'] if v['delta'] is not None else '–'}  ({v['better']} better, {v['worse']} worse) · {ci} → {verdict}"+(f" · ~{v['needed']} cases to detect ±0.2" if v.get('needed') else ''))
    note=[]
    if a['config'].get('no_lessons')!=b['config'].get('no_lessons'):note.append('lessons on vs off: this is a controlled lessons A/B')
    if a.get('fingerprint')!=b.get('fingerprint'):note.append('duo code changed between runs')
    if note:lines.append('  Note: '+'; '.join(note))
    for f in c['flips'][:12]:lines.append(f"  flip [{f['id']}] {f['from']} → {f['to']} · {f['prompt']}")
    return '\n'.join(lines)

def fingerprint():
    h=hashlib.sha1()
    for name in ('duo.py','agents.py','judge.py','lessons.py'):
        try:h.update((Path(__file__).with_name(name)).read_bytes())
        except OSError:pass
    return h.hexdigest()[:10]

# ---------- held-out split, failure cases, paired statistics ----------
def split(cid):
    """Stable 25% held-out split by case id: judges' lesson proposals never come from held-out turns' replays."""
    return 'holdout' if int(cid[:4],16)%4==0 else 'train'

FAILURE_CAP=5  # per failure category, so recent problems cannot take over the eval set

def add_failure_cases(store):
    """Confirmed failures on answer/read turns become regression cases (write tasks need repo snapshots)."""
    if not store:return 0
    store.flush(3)
    with store.lock,store.connect() as db:
        bad={}
        for typ,cond in (('judge.root',None),('judge.hack_final',None),('judge.claim',"contradicted")):
            for sid,data in db.execute('SELECT session_id,data FROM events WHERE type=?',(typ,)).fetchall():
                d=json.loads(data)
                if cond and d.get('verdict')!=cond:continue
                cat='reward_hack' if typ=='judge.hack_final' else 'contradicted_claim' if typ=='judge.claim' else d.get('name') or 'other'
                bad.setdefault(d.get('turn_id'),cat)
        rows=db.execute("""SELECT s.span_id,s.mode,ss.cwd,p.text FROM spans s JOIN payloads p ON p.span_id=s.span_id AND p.kind='prompt'
                           JOIN sessions ss ON ss.id=s.session_id WHERE s.kind='turn'""").fetchall()
    cases=load();have={c['id'] for c in cases};per={}
    for c in cases:
        if c.get('source','').startswith('failure:'):per[c['source']]=per.get(c['source'],0)+1
    new=[]
    for tid,mode,cwd,text in rows:
        if tid not in bad or 'write' in (mode or ''):continue
        src='failure:'+bad[tid]
        cand={'prompt':text,'kind':'respond' if (mode or '').startswith('respond') else 'read','cwd':cwd,'source':src}
        if case_id(text) in have or per.get(src,0)>=FAILURE_CAP or not usable(cand):continue
        if any(similar(text,c['prompt']) for c in cases+new):continue
        new.append(cand);per[src]=per.get(src,0)+1
    if new:add_cases(new)
    return len(new)

def bootstrap(diffs,iters=2000,seed=7):
    """Paired bootstrap 95% CI of the mean per-case difference."""
    import random
    if len(diffs)<2:return (None,None)
    rng=random.Random(seed);n=len(diffs)
    means=sorted(sum(rng.choice(diffs) for _ in range(n))/n for _ in range(iters))
    return (round(means[int(.025*iters)],3),round(means[int(.975*iters)-1],3))

def power_hint(diffs,effect=0.2):
    """Cases needed to detect a mean paired difference of `effect` (80% power, two-sided 5%)."""
    if len(diffs)<3:return None
    m=sum(diffs)/len(diffs);sd=(sum((x-m)**2 for x in diffs)/(len(diffs)-1))**.5
    return max(3,math.ceil(((1.96+0.84)*sd/effect)**2)) if sd else 3
