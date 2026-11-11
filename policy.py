"""RL on top of the agents (black-box: no weights, no added latency).

Policy = which playbook variant each model gets (k lessons injected: 0/2/4) per task type,
plus a routing recommendation per task type. Thompson sampling over Beta posteriors:
choosing an arm is O(1) on a small JSON file before the turn; posteriors are updated in
the background after the judges score the turn.

Reward in [0,1]: judge overall (1-5 -> 0-1), minus 0.5 for a confirmed reward hack,
minus 0.15 per contradicted claim (max 0.45); user feedback on the next message moves
it +0.2 / -0.3. Exploration is capped at EXPLORE. Routing is applied only with
DUO_ROUTE=auto; otherwise it is a recommendation shown in /policy and the viewer.
"""
import json
import os
from pathlib import Path
import random
import threading

ARMS=(0,2,4)          # number of playbook items injected
EXPLORE=0.10
ROUTE_MIN=10          # decisive turns before a route is recommended
ROUTE_CONF=0.9        # posterior probability one agent is better
_lock=threading.Lock()

def path():return Path(os.environ.get('DUO_POLICY') or Path.home()/'.duo'/'policy.json').expanduser()

def load():
    try:return json.loads(path().read_text())
    except (OSError,ValueError):return {'arms':{},'routes':{},'history':[]}

def save(state):
    p=path();p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(state));tmp.replace(p)

def _ctx(provider,kind):return f'{provider}|{kind}'

def choose_k(provider,kind,rng=random,state=None):
    """Hot path: Thompson sample per arm; EXPLORE share uniform. Returns (k, explored)."""
    st=state or load();post=st['arms'].get(_ctx(provider,kind),{})
    if rng.random()<EXPLORE:return rng.choice(ARMS),True
    best=max(ARMS,key=lambda a:rng.betavariate(*post.get(str(a),[1,1])))
    return best,False

def reward(score,hack=False,contradicted=0,feedback=None):
    r=(max(1.0,min(5.0,float(score)))-1)/4
    if hack:r-=0.5
    r-=min(0.45,0.15*contradicted)
    if feedback=='positive':r+=0.2
    elif feedback=='negative':r-=0.3
    return max(0.0,min(1.0,r))

def update(arm_events,results,hacks=(),claims=(),turn_id=None,rng=random):
    """After judging: one Bernoulli(reward) pseudo-observation per (model, task kind, arm)."""
    out={}
    with _lock:
        st=load()
        for a in arm_events:
            p=a.get('provider');data=a.get('data') or {}
            scores=[r['per'][p]['overall'] for r in results if p in r.get('per',{})]
            if not scores:continue
            bad=sum(1 for c in claims if c.get('provider')==p and c.get('verdict')=='contradicted')
            rw=reward(sum(scores)/len(scores),p in hacks,bad)
            post=st['arms'].setdefault(_ctx(p,data.get('kind','respond')),{}).setdefault(str(data.get('k')),[1,1])
            success=rng.random()<rw;post[0 if success else 1]+=1
            st['history'].append({'turn':turn_id,'provider':p,'kind':data.get('kind'),'k':data.get('k'),'reward':round(rw,3)})
            out[p]=rw
        st['history']=st['history'][-2000:]
        save(st)
    return out

def feedback(turn_id,verdict,rng=random):
    """User feedback on a past turn nudges the arms that turn used."""
    with _lock:
        st=load();hit=[h for h in st['history'] if h['turn']==turn_id]
        for h in hit:
            rw=reward(1+4*h['reward'],feedback=verdict)
            post=st['arms'].setdefault(_ctx(h['provider'],h['kind']),{}).setdefault(str(h['k']),[1,1])
            post[0 if rng.random()<rw else 1]+=1
        if hit:save(st)
    return len(hit)

def update_routes(verdicts):
    """verdicts: [(kind, winner)] decisive panel verdicts -> Beta posterior that Codex beats Claude per task kind."""
    with _lock:
        st=load();st['routes']={}
        for kind,winner in verdicts:
            if winner not in ('codex','claude'):continue
            a=st['routes'].setdefault(kind,[1,1]);a[0 if winner=='codex' else 1]+=1
        save(st)

def route(kind,state=None,samples=4000,rng=random):
    """('both'|'codex'|'claude', P(codex better), n). Single-agent only when confident and enough evidence."""
    st=state or load();a,b=st['routes'].get(kind,[1,1]);n=a+b-2
    p=sum(rng.betavariate(a,b)>0.5 for _ in range(samples))/samples
    if n>=ROUTE_MIN and p>=ROUTE_CONF:return 'codex',p,n
    if n>=ROUTE_MIN and p<=1-ROUTE_CONF:return 'claude',p,n
    return 'both',p,n

def summary():
    st=load();arms=[]
    for ctx,post in sorted(st['arms'].items()):
        p,kind=ctx.split('|')
        for k,(a,b) in sorted(post.items(),key=lambda kv:int(kv[0])):
            mean=a/(a+b);var=a*b/((a+b)**2*(a+b+1))
            arms.append({'provider':p,'kind':kind,'k':int(k),'alpha':a,'beta':b,'n':a+b-2,'mean':round(mean,3),'sd':round(var**.5,3)})
    routes=[{'kind':k,'route':route(k,st)[0],'p_codex':round(route(k,st)[1],3),'n':route(k,st)[2]} for k in sorted(st['routes'])]
    return {'arms':arms,'routes':routes,'history':st['history'][-400:],'explore':EXPLORE,'auto_route':os.environ.get('DUO_ROUTE')=='auto'}

def render():
    s=summary()
    if not s['arms'] and not s['routes']:return 'No policy data yet: it learns from judged turns (/judge panel).'
    lines=['Playbook arms (k lessons injected) — posterior mean reward ± sd, n:']
    for a in s['arms']:lines.append(f"  {a['provider']:<6} {a['kind']:<7} k={a['k']}  {a['mean']:.2f} ± {a['sd']:.2f}  n={a['n']}")
    for r in s['routes']:lines.append(f"Route {r['kind']}: {r['route']} (P(codex better)={r['p_codex']:.2f}, n={r['n']}){' · applied' if s['auto_route'] and r['route']!='both' else ''}")
    return '\n'.join(lines)
