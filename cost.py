"""Cost: what a run cost in dollars, and how much of it bought nothing.

Every model call in a run has four token counts (fresh input, cache write, cache read, output). Each is priced from
prices.json (list prices; override in ~/.agentlens/prices.json) by the model that made the call:

    cost = input*p_in + cache_write*p_write + cache_read*p_read + output*p_out      (per million tokens)

When the agent's own CLI reported a cost for the run (Claude's total_cost_usd), that figure is used and said so.
A model with no price gets no dollar figure, only tokens, and the reason: nothing is estimated from another model.

Waste is attributed by time, per call: the share of the run's cost spent inside loops (longrun's loop bursts), and
the share spent after the decisive step (the first long-horizon mode, else the look-first step). Both are upper
bounds on what a fix would save: the work after a wrong step is not all wasted.
"""
import json
import os
import re
from pathlib import Path

HERE=Path(__file__).resolve().parent
_P={'at':None,'data':None}
FAMILY=re.compile(r'(fable|mythos|opus|sonnet|haiku)-(\d)(?:[-.](\d)(?!\d))?')
OLD=re.compile(r'(\d)-(\d)-(opus|sonnet|haiku)')

def prices():
    user=Path(os.environ.get('AGENTLENS_PRICES') or Path.home()/'.agentlens'/'prices.json')
    key=(user.stat().st_mtime if user.exists() else 0)
    if _P['data'] is None or _P['at']!=key:
        base=json.loads((HERE/'prices.json').read_text())
        try:over=json.loads(user.read_text()) if user.exists() else {}
        except ValueError:over={}
        for k in ('anthropic','openai'):base.setdefault(k,{}).update(over.get(k) or {})
        _P.update(at=key,data=base)
    return _P['data']

def price_for(model,prompt_tokens=0):
    """[in, write5m, write1h, read, out] per million tokens, or None when the model has no price."""
    m=str(model or '').lower();P=prices()
    f=FAMILY.search(m)
    if f:
        key=f'{f.group(1)}-{f.group(2)}'+(f'-{f.group(3)}' if f.group(3) else '')
        if prompt_tokens>100_000 and key+'>100k' in P['anthropic']:return P['anthropic'][key+'>100k']
        return P['anthropic'].get(key)
    if OLD.search(m):return None
    for k in sorted(P['openai'],key=len,reverse=True):
        if m.startswith(k.lower()):
            v=P['openai'][k];return [v[0],v[0],v[0],v[1] if len(v)>2 else v[0],v[-1]] if len(v)<5 else v
    return None

def call_cost(c,model):
    """c = {'input','cache_write','cache_write_1h','cache_read','output'} tokens for one model call."""
    prompt=c.get('input',0)+c.get('cache_write',0)+c.get('cache_write_1h',0)+c.get('cache_read',0)
    p=price_for(model,prompt)
    if not p:return None
    return (c.get('input',0)*p[0]+c.get('cache_write',0)*p[1]+c.get('cache_write_1h',0)*p[2]+c.get('cache_read',0)*p[3]+c.get('output',0)*p[4])/1e6

def run_cost(calls,model,reported=None,loops=(),decisive_at=None):
    """calls: [{'ts', 'input', 'cache_write', 'cache_read', 'output', ...}] for one run.
    -> {'usd', 'source', 'tokens', 'calls', 'in_loops_usd', 'after_decisive_usd', 'model', 'unpriced'}"""
    tok={k:sum(int(c.get(k,0) or 0) for c in calls) for k in ('input','cache_write','cache_write_1h','cache_read','output')}
    per=[(c.get('ts'),call_cost(c,model)) for c in calls];priced=[u for _,u in per if u is not None]
    usd=sum(priced) if priced and len(priced)==len(per) else None;source='list prices' if usd is not None else None
    if reported is not None:
        scale=(reported/usd) if usd else None;usd=float(reported);source='reported by the agent'
        if scale:per=[(t,u*scale) for t,u in per]
    out={'model':model,'calls':len(calls),'tokens':tok,'usd':round(usd,4) if usd is not None else None,'source':source,
         'unpriced':usd is None and bool(calls),'in_loops_usd':None,'after_decisive_usd':None}
    if usd is not None and all(u is not None for _,u in per):
        inl=sum(u for t,u in per if t is not None and any(a-1<=t<=b+1 for a,b in loops))
        out['in_loops_usd']=round(inl,4)
        if decisive_at is not None:out['after_decisive_usd']=round(sum(u for t,u in per if t is not None and t>decisive_at),4)
        out['per_call']=[[t,round(u,5)] for t,u in per][:2000]
    return out

def summary(c):
    t=c['tokens'];tk=lambda v:f'{v/1e6:.1f}M' if v>=1e6 else f'{v/1e3:.0f}k' if v>=1e3 else str(v)
    toks=f"{tk(t['input']+t['cache_write']+t['cache_write_1h']+t['cache_read'])} in, {tk(t['output'])} out over {c['calls']} model call{'s' if c['calls']!=1 else ''}"
    if c['usd'] is None:return f"{toks}; no price set for {c['model'] or 'this model'} (add it to prices.json)"
    s=f"${c['usd']:.2f} ({c['source']}) · {toks}"
    if c.get('in_loops_usd'):s+=f" · ${c['in_loops_usd']:.2f} inside loops"
    if c.get('after_decisive_usd'):s+=f" · ${c['after_decisive_usd']:.2f} after the decisive step"
    return s
