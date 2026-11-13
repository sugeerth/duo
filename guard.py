"""Guard: check a tool call BEFORE it runs, using the same rules as security.py.

Duo hosts every approval request from Codex and Claude (agents.py -> duo.duo_approval). The guard runs there:

    DUO_GUARD=warn   (default) show the findings in the approval prompt; the default answer stays No
    DUO_GUARD=block  refuse critical actions without asking; warn on high
    DUO_GUARD=off    no checks

It also looks back over the run's recent tool outputs (the trace hub keeps them): a risky request that comes within
INJECTION_WINDOW calls of an output carrying injected instructions is escalated to critical, the same taint rule
security.py applies after the fact. Every check is emitted as a guard.check event (the viewer's live alarms), and
every refusal as guard.blocked.

Limits: the guard sees only calls that reach an approval prompt. Calls a sandbox or a saved permission rule allows
without asking run unchecked here; the post-turn review still covers them.
"""
import json
import os

import security

def _target(action,details):
    d=details if isinstance(details,dict) else {}
    for k in ('command','cmd','file_path','path','url','pattern'):
        v=d.get(k)
        if v:return ' '.join(v) if isinstance(v,list) else str(v)
    if d.get('changes'):return ', '.join(str(c.get('path','')) for c in d['changes'][:3])
    return json.dumps(d,ensure_ascii=False)[:400] if d else str(action)

def check(action,details,recent_outputs=()):
    """-> {'verdict': 'allow'|'warn'|'block', 'severity', 'findings', 'target'}"""
    mode=os.environ.get('DUO_GUARD','warn').lower()
    target=_target(action,details)
    if mode=='off':return {'verdict':'allow','severity':'none','findings':[],'target':target}
    step={'ref':1,'tool':str(action),'target':target,'ok':True,'input':json.dumps(details,ensure_ascii=False)[:2000] if isinstance(details,dict) else ''}
    findings=[f for f in security.analyze([step])['findings']]
    injected=[o for o in list(recent_outputs)[-security.INJECTION_WINDOW:] if security.INJECT.search(str(o or ''))]
    if injected and any(f['category'] in security.RISKY for f in findings):
        findings.insert(0,{'category':'injection_followed','severity':'critical','rule':'taint','why':'this request comes right after a tool output that carried instructions aimed at the agent',
                           'owasp':security.OWASP['injection_followed'],'evidence':target[:160]})
    sev=max((f['severity'] for f in findings),key=lambda s:security.SEV_RANK[s],default='none')
    rank=security.SEV_RANK.get(sev,0)
    if mode=='block' and rank>=4:verdict='block'
    elif rank>=3 or (mode!='block' and rank>=2):verdict='warn'
    else:verdict='allow'
    return {'verdict':verdict,'severity':sev,'findings':findings,'target':target}

def recent_outputs(hub,provider,limit=8):
    """The last tool outputs of the running agent of this provider, from the trace hub's payload cache."""
    try:
        with hub.condition:
            ev=[e for e in list(hub.events)[-400:] if e.get('type')=='tool.completed' and e.get('provider')==provider and e.get('chars')]
            return [(hub.payloads.get(e['id']) or ('','',None,None))[1] for e in ev[-limit:]]
    except Exception:
        return []

def banner(result):
    """Plain-text lines for the approval prompt."""
    if not result['findings']:return []
    head={'block':'BLOCKED by Duo guard','warn':'Duo guard warning'}.get(result['verdict'],'Duo guard')
    lines=[f"{head}: {result['severity'].upper()} · {result['target'][:120]}"]
    for f in result['findings'][:4]:lines.append(f"  - {f['category'].replace('_',' ')} ({f['rule']}): {f['why']} [{f['owasp']}]")
    return lines
