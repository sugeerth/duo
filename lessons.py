"""Trace -> action -> better agent.

Judges propose fixes (judge.action events) backed by step evidence. suggestions()
groups them across turns and sessions; accept() turns one into a lesson that AgentLens
prepends to that model's future prompts. Lessons live in ~/.agentlens/lessons.json
(AGENTLENS_LESSONS=/path overrides). Nothing outside AgentLens is modified.
"""
from collections import defaultdict
import json
import os
from pathlib import Path
import time

TARGET_LABEL={'prompt':'Prompt rule','tools':'Tool setup','workflow':'AgentLens workflow','context':'Missing context'}

def path():
    return Path(os.environ.get('AGENTLENS_LESSONS') or Path.home()/'.agentlens'/'lessons.json').expanduser()

def load():
    try:return json.loads(path().read_text())
    except (OSError,ValueError):return []

def save(items):
    p=path();p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(items,indent=2,ensure_ascii=False))
    _changed('playbook')

def _changed(what):
    """Stream every playbook/settings change so open viewers update instantly (viewer, CLI or auto-learning)."""
    try:
        from traces import emit
        emit('lessons.control','duo',name=what)
    except Exception:pass

def similar(a,b,threshold=0.45):
    wa={w for w in a.lower().replace(',',' ').replace('.',' ').split() if len(w)>3}
    wb={w for w in b.lower().replace(',',' ').replace('.',' ').split() if len(w)>3}
    return bool(wa and wb) and len(wa&wb)/len(wa|wb)>=threshold

def suggestions(events):
    """Group judge.action events by (model, target, category). Ranked by how many turns support them."""
    groups=defaultdict(lambda:{'turns':set(),'judges':set(),'rules':defaultdict(int),'refs':set(),'sessions':set()})
    for e in events:
        if e.get('type')!='judge.action':continue
        g=groups[(e.get('provider'),e.get('role'),e.get('name'))]
        g['turns'].add(e.get('turn_id'));g['judges'].add(e.get('judge'));g['rules'][e.get('rationale') or '']+=1
        g['sessions'].add(e.get('session_id'));g['refs'].update(r for r in (e.get('tags') or '').split(',') if r)
    accepted={(l['provider'],l['category'],l['target']) for l in load()}
    # merge near-duplicate rules for the same model even when judges filed them under different target/category keys
    keys=list(groups);merged={}
    for k in keys:
        rule=max(groups[k]['rules'].items(),key=lambda kv:kv[1])[0]
        home=next((m for m in merged if m[0]==k[0] and similar(rule,merged[m])),None)
        if home is None:merged[k]=rule;continue
        g,h=groups[home],groups.pop(k)
        for f in ('turns','judges','refs','sessions'):g[f]|=h[f]
        for r,n in h['rules'].items():g['rules'][r]+=n
    out=[]
    for (provider,target,category),g in groups.items():
        rule=max(g['rules'].items(),key=lambda kv:(kv[1],-len(kv[0])))[0]
        out.append({'provider':provider,'target':target,'category':category,'rule':rule,'turns':len(g['turns']),
                    'judges':sorted(j for j in g['judges'] if j),'sessions':len(g['sessions']),'evidence':len(g['refs']),
                    'alternatives':[r for r in g['rules'] if r!=rule][:2],'accepted':(provider,category,target) in accepted})
    out.sort(key=lambda s:(-s['turns'],-len(s['judges']),-s['evidence']))
    return out

def item_id(provider,rule):
    import hashlib
    return hashlib.sha1((provider+'|'+rule.lower()).encode()).hexdigest()[:6]

def accept(suggestion,provider=None,status='active'):
    """ACE-style ADD. Items keep provenance; status active|verified (passed a held-out paired replay)."""
    items=load();who=provider or suggestion['provider']
    if any(l['rule']==suggestion['rule'] and l['provider']==who for l in items):return items
    items.append({'id':item_id(who,suggestion['rule']),'provider':who,'rule':suggestion['rule'],'target':suggestion['target'],'category':suggestion['category'],
                  'support_turns':suggestion['turns'],'added':time.strftime('%Y-%m-%d %H:%M'),'status':status})
    save(items);return items

def ensure_ids(items):
    for l in items:l.setdefault('id',item_id(l['provider'],l['rule']));l.setdefault('status','active')
    return items

def merge_duplicates():
    """ACE-style MERGE: near-duplicate items for the same model collapse into the better-supported one."""
    items=ensure_ids(load());keep=[];merged=[]
    for l in sorted(items,key=lambda x:-x.get('support_turns',0)):
        dup=next((k for k in keep if k['provider']==l['provider'] and similar(k['rule'],l['rule'])),None)
        if dup:dup['support_turns']=dup.get('support_turns',0)+l.get('support_turns',0);merged.append(l['id'])
        else:keep.append(l)
    if merged:save(keep)
    return merged

def set_status(lesson_id,status):
    items=ensure_ids(load())
    for l in items:
        if l['id']==lesson_id:l['status']=status
    save(items)

def remove_id(lesson_id):
    items=[l for l in ensure_ids(load()) if l['id']!=lesson_id];save(items);return items

def remove(index):
    items=load()
    if 0<=index<len(items):items.pop(index);save(items)
    return items

def settings_path():return path().with_name('lessons_settings.json')

def settings():
    try:st=json.loads(settings_path().read_text())
    except (OSError,ValueError):st={}
    return {'enabled':st.get('enabled',True),'k':st.get('k')}  # k None = the RL bandit decides

def save_settings(**kw):
    st=settings();st.update({k:v for k,v in kw.items() if k in ('enabled','k')})
    settings_path().parent.mkdir(parents=True,exist_ok=True);settings_path().write_text(json.dumps(st));_changed('settings');return st

def disabled():return os.environ.get('AGENTLENS_NO_LESSONS','')=='1' or not settings()['enabled']  # replay --no-lessons / user switch

TOP_K=int(os.environ.get('AGENTLENS_LESSONS_K','4'))
ALWAYS={'ignored_constraint','hallucinated_result','reward_hack','no_verification'}  # general habits that apply to every task
HEADER=('Optional guidance from earlier graded runs. It is lower priority than everything above: the user\'s request always wins, '
        'and skip any item that does not apply to this task.')

def _words(text):
    import re
    return {w for w in re.findall(r'[a-z0-9]+',text.lower()) if len(w)>3}

NEGATION=('don\'t','dont','do not','no ','without','skip','never','avoid','not ')

def conflicts(request,rule):
    """True when the user's request explicitly rules out what the item asks for (e.g. 'don't run tests' vs 'run the tests')."""
    import re
    req=(request or '').lower();rw=_words(rule)
    for m in re.finditer(r"(don't|dont|do not|without|skip|never|avoid|no)\s+((?:\w+\s+){0,2}\w+)",req):
        if _words(m.group(2))&rw:return True
    if re.search(r'\b(at most|under|max(imum)?|no more than)\s+\d+\s+words?\b',req) and re.search(r'\b(explain|detail|thorough|elaborate)\b',rule.lower()):return True
    return False

def set_flag(lesson_id,flag,value):
    """pinned: always offered (still subject to conflicts); muted: never offered."""
    items=ensure_ids(load())
    for l in items:
        if l['id']==lesson_id:
            l[flag]=bool(value)
            if flag=='pinned' and value:l['muted']=False
            if flag=='muted' and value:l['pinned']=False
    save(items);return items

def compose(provider,request=None,k=None):
    """What this agent would receive for this request: (block, chosen, skipped[{id,rule,reason}]). Pure Python, ~ms.
    The user's text is never modified; the block is appended after it and marked lower priority."""
    if disabled():return '',[],[{'id':'*','rule':'all guidance','reason':'guidance is switched off'}]
    k=TOP_K if k is None else k
    items=[l for l in ensure_ids(load()) if l['provider'] in (provider,'both') and l.get('target')!='workflow']
    skipped=[];pool=[]
    for l in items:
        if l.get('muted'):skipped.append({'id':l['id'],'rule':l['rule'],'reason':'muted by you'})
        elif request and conflicts(request,l['rule']):skipped.append({'id':l['id'],'rule':l['rule'],'reason':'conflicts with your request'})
        else:pool.append(l)
    q=_words(request or '')
    def score(l):
        w=_words(l['rule']);return len(q&w)/(len(w) or 1)+(0.5 if l.get('category') in ALWAYS else 0)+(0.3 if l.get('status')=='verified' else 0)+0.02*l.get('support_turns',0)
    pinned=[l for l in pool if l.get('pinned')]
    rest=sorted([l for l in pool if not l.get('pinned')],key=score,reverse=True)
    relevant=[l for l in rest if not request or score(l)>=0.15]
    chosen=pinned+relevant[:max(0,k-len(pinned))]
    for l in rest:
        if l not in chosen:skipped.append({'id':l['id'],'rule':l['rule'],'reason':'not relevant to this request' if l not in relevant else f'over the limit ({k} items this turn)'})
    block=('\n\n'+HEADER+'\n'+'\n'.join('- '+l['rule'] for l in chosen)) if chosen else ''
    return block,chosen,skipped

def select(provider,request=None,k=None):
    return compose(provider,request,k)[1]

def block(provider,request=None):
    return compose(provider,request)[0]

def count(provider):
    if disabled():return 0
    return sum(1 for l in load() if l['provider'] in (provider,'both') and l.get('target')!='workflow')

def render(items,numbered=True):
    if not items:return 'No suggestions yet. They appear after judged turns that used tools (try /judge panel).'
    lines=[]
    for i,s in enumerate(items,1):
        mark=' ✓ accepted' if s.get('accepted') else ''
        lines.append(f"{str(i)+'. ' if numbered else '- '}[{s['provider']}] {TARGET_LABEL.get(s['target'],s['target'])} · {s['category'].replace('_',' ')} · "
                     f"{s['turns']} turn{'s' if s['turns']!=1 else ''}, judges: {', '.join(s['judges']) or '–'}{mark}\n   {s['rule']}")
    return '\n'.join(lines)
