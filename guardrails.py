"""Guardrails: limits you set, checked on every run and enforced while it runs.

Policy file (JSON), first one found: $AGENTLENS_GUARDRAILS, ./.agentlens/guardrails.json, ~/.agentlens/guardrails.json.
Every rule is optional; a rule that is not set is reported as "not set", never as passed.

    {
      "max_cost_usd": 2.00,            a run may not cost more than this
      "max_steps": 150,                tool calls per run
      "max_minutes": 30,               wall time per run
      "max_failed_calls": 10,          failed tool calls per run
      "max_context_tokens": 150000,    largest prompt sent to the model
      "forbid_write": ["vendor/**", "*.lock", ".github/**"],     files the agent may not change
      "forbid_read":  [".env", "**/.aws/**", "**/id_rsa*"],      files the agent may not read
      "forbid_commands": ["git push --force", "rm -rf /", "DROP TABLE"],   regular expressions, case-insensitive
      "require_check_before_ship": true                             no commit/push/deploy after an unchecked edit
    }

After a run (evaluate): each rule gets ok / violated / not set / unknown, with the step that broke it.
While a run is going (live_check, called from guard.py on every tool call Duo sees):
  - a call that breaks forbid_write / forbid_read / forbid_commands is refused, in every guard mode except off;
  - once a budget (steps, minutes, failed calls, cost) is used up, further tool calls are refused with the reason,
    so the agent stops and reports instead of spending more.
Limits: the cost budget needs token counts while the run is going (Claude reports them per message; Codex reports
at the end, so its cost budget is checked after the run). A refused call cannot be un-run: budgets stop the next call.
"""
import fnmatch
import json
import os
import re
import threading
import time
from pathlib import Path

RULES=('max_cost_usd','max_steps','max_minutes','max_failed_calls','max_context_tokens','forbid_write','forbid_read','forbid_commands','require_check_before_ship')
LABEL={'max_cost_usd':'Cost per run','max_steps':'Tool calls per run','max_minutes':'Minutes per run','max_failed_calls':'Failed calls per run',
       'max_context_tokens':'Largest prompt (tokens)','forbid_write':'Files it may not change','forbid_read':'Files it may not read',
       'forbid_commands':'Commands it may not run','require_check_before_ship':'Check before shipping'}
_C={'key':None,'policy':None,'path':None}
_TL=threading.local()  # .policy: a policy that replaces the file for this thread (AgentLens Cloud, tests)

def policy_path():
    for p in (os.environ.get('AGENTLENS_GUARDRAILS'),Path.cwd()/'.agentlens'/'guardrails.json',Path.home()/'.agentlens'/'guardrails.json'):
        if p and Path(p).is_file():return Path(p)
    return None

def clean(pol):return {k:pol[k] for k in RULES if isinstance(pol,dict) and pol.get(k) not in (None,[],'')}

def load():
    if getattr(_TL,'policy',None) is not None:return _TL.policy
    p=policy_path();key=(str(p),p.stat().st_mtime) if p else None
    if key!=_C['key']:
        try:pol=json.loads(p.read_text()) if p else {}
        except (OSError,ValueError):pol={}
        _C.update(key=key,policy={k:pol[k] for k in RULES if pol.get(k) not in (None,[],'')},path=str(p) if p else None)
    return _C['policy']

def _glob(path,patterns):
    path=str(path or '');path=path[2:] if path.startswith('./') else path
    for g in patterns or []:
        g2=g[2:] if g.startswith('./') else g
        if fnmatch.fnmatch(path,g2) or fnmatch.fnmatch(path,'*/'+g2) or fnmatch.fnmatch(path,g2.replace('/**','/*')) or fnmatch.fnmatch(path.split('/')[-1],g2) or (g2.endswith('/**') and ('/'+g2[:-2]) in ('/'+path)):return g  # absolute paths match too
    return None

def _paths(s):
    import horizon
    k=horizon.kind(s);t=str(s.get('target') or '')
    if k=='write':return 'write',[horizon.write_path(s)]
    if k=='look':return 'read',horizon.PATH.findall(t) or [t.split()[-1] if t.split() else t]
    return k,horizon.PATH.findall(t)+re.findall(r'(?<![\w/])(\.env[\w.]*|~?/?[\w./-]*\.aws/[\w./-]*|[\w./-]*id_rsa[\w.]*)',t)

def call_violation(tool,target,policy=None):
    """One tool call against the forbid rules -> {'rule','why'} or None."""
    pol=load() if policy is None else policy
    if not pol:return None
    s={'tool':tool,'target':target};kind,paths=_paths(s)
    for p in paths:
        if kind=='write' and (g:=_glob(p,pol.get('forbid_write'))):return {'rule':'forbid_write','why':f'changes {p}, which the guardrails forbid ({g})'}
        if kind in ('read','other','check') and (g:=_glob(p,pol.get('forbid_read'))):return {'rule':'forbid_read','why':f'reads {p}, which the guardrails forbid ({g})'}
    for rx in pol.get('forbid_commands') or []:
        try:
            if re.search(rx,str(target or ''),re.I):return {'rule':'forbid_commands','why':f'runs a forbidden command ({rx})'}
        except re.error:continue
    return None

def evaluate(steps,cost=None,policy=None,ctx_max=None):
    """-> {'rules':[{'rule','label','limit','value','status','ref','why'}], 'violations': n, 'set': n, 'path'}"""
    import horizon
    pol=load() if policy is None else policy;rows=[]
    def row(rule,value,status,ref=None,why=None):rows.append({'rule':rule,'label':LABEL[rule],'limit':pol.get(rule),'value':value,'status':status,'ref':ref,'why':why})
    def limit(rule,value,fmt=str,ref=None):
        if rule not in pol:return row(rule,value,'unset')
        if value is None:return row(rule,None,'unknown',why='not recorded for this run')
        bad=value>pol[rule];row(rule,value,'violated' if bad else 'ok',ref if bad else None,f'{fmt(value)} against a limit of {fmt(pol[rule])}' if bad else None)
    n=len(steps);ts=[s['at'] for s in steps if s.get('at') is not None]
    minutes=(max(ts)-min(ts)+(steps[-1].get('seconds') or 0))/60 if ts else None
    fails=[s for s in steps if s.get('ok') is False]
    limit('max_cost_usd',cost.get('usd') if cost else None,lambda v:f'${v:.2f}')
    limit('max_steps',n,ref=steps[pol['max_steps']]['ref'] if 'max_steps' in pol and n>pol['max_steps'] else None)
    limit('max_minutes',round(minutes,1) if minutes is not None else None,lambda v:f'{v} min')
    limit('max_failed_calls',len(fails),ref=fails[pol['max_failed_calls']]['ref'] if 'max_failed_calls' in pol and len(fails)>pol['max_failed_calls'] else None)
    limit('max_context_tokens',ctx_max,lambda v:f'{int(v):,}')
    for rule in ('forbid_write','forbid_read','forbid_commands'):
        if rule not in pol:row(rule,None,'unset');continue
        hits=[(s,v) for s in steps if (v:=call_violation(s.get('tool'),s.get('target'),{rule:pol[rule]}))]
        row(rule,len(hits),'violated' if hits else 'ok',hits[0][0]['ref'] if hits else None,hits[0][1]['why']+(f' (+{len(hits)-1} more)' if len(hits)>1 else '') if hits else None)
    if not pol.get('require_check_before_ship'):row('require_check_before_ship',None,'unset')
    else:
        K=[horizon.kind(s) for s in steps];w=None;hit=None
        for i,k in enumerate(K):
            if k=='write' and horizon.code_write(steps[i]):w=i
            elif k=='check' and steps[i].get('ok') is not False:w=None
            elif k=='ship' and w is not None:hit=i;break
        row('require_check_before_ship',1 if hit is not None else 0,'violated' if hit is not None else 'ok',steps[hit]['ref'] if hit is not None else None,
            f"ships ({str(steps[hit].get('target'))[:50]}) after an edit that no check covered" if hit is not None else None)
    return {'rules':rows,'violations':sum(r['status']=='violated' for r in rows),'set':sum(r['status']!='unset' for r in rows),'path':_C['path'] if policy is None else None}

def summary(g):
    if not g['set']:return 'No guardrails set.'
    bad=[r for r in g['rules'] if r['status']=='violated']
    if not bad:return f"Within all {g['set']} guardrails."
    return f"{len(bad)} of {g['set']} guardrails broken: "+'; '.join(f"{r['label'].lower()} ({r['why']})" for r in bad[:3])+'.'

# ------------------------------------------------------------------ while the run is going
def live_usage(hub,provider):
    """Tool calls, failures, minutes and priced cost of this provider's run in progress, from the trace hub."""
    import cost as costmod
    with hub.condition:ev=list(hub.events)[-6000:]
    start=None;model=None
    for i in range(len(ev)-1,-1,-1):
        e=ev[i]
        if e.get('type')=='span.start' and e.get('span_kind')=='agent' and e.get('provider')==provider:start=i;model=e.get('model');break
    mine=[e for e in (ev[start:] if start is not None else []) if e.get('provider')==provider]
    calls=[{'input':d.get('input_tokens',0),'cache_write':d.get('cache_creation_input_tokens',0),'cache_read':d.get('cache_read_input_tokens',0) or d.get('cached_input_tokens',0),'output':d.get('output_tokens',0)}
           for e in mine if e.get('type')=='usage' and isinstance(d:=e.get('data'),dict)]
    usd=[costmod.call_cost(c,model) for c in calls]
    return {'steps':sum(e.get('type')=='tool.started' for e in mine),'failed':sum(e.get('type')=='tool.completed' and e.get('passed') is False for e in mine),
            'minutes':(time.time()-mine[0]['timestamp'])/60 if mine else 0,'usd':sum(usd) if usd and all(u is not None for u in usd) else None}

def live_check(hub,provider,tool,target,policy=None):
    """Called before a tool call runs. -> None, or {'rule','why'} when the call must be refused."""
    pol=load() if policy is None else policy
    if not pol:return None
    v=call_violation(tool,target,pol)
    if v:return v
    if not any(k in pol for k in ('max_steps','max_minutes','max_failed_calls','max_cost_usd')) or hub is None:return None
    u=live_usage(hub,provider)
    for rule,val,fmt in (('max_steps',u['steps'],str),('max_failed_calls',u['failed'],str),('max_minutes',u['minutes'],lambda v:f'{v:.0f} min'),('max_cost_usd',u['usd'],lambda v:f'${v:.2f}')):
        if rule in pol and val is not None and val>=pol[rule]:
            return {'rule':rule,'why':f"the run has used its budget: {LABEL[rule].lower()} is at {fmt(val)}, limit {fmt(pol[rule])}. Stop and report what is done and what is left."}
    return None
