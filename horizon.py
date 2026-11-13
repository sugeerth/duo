"""Long horizons: what a run of hundreds of steps needs measured (concepts from AgentDiff's horizon and milestones).

A long run is right for most of its length and wrong in one place, and the place is what a reader needs. The grade
is the weakest instrument at this length: a run that skips a unit of work and reports success passes an outcome
check. This module reads one model's tool steps for a turn, deterministically, three ways:

1. MILESTONES: the units of work the request names (a numbered or bulleted plan, or file paths it mentions), each
   recognised by evidence in a step (a write to, or check of, the unit's file or keyword). Which were reached, at
   which step, in order or not, and where the run stalled. With no plan in the request, the run's own milestones:
   first look, first write, first passing check, final passing check.
2. LONG-HORIZON FAILURE MODES, each with the step where it shows and the rule that found it:
     skipped_unit          a planned unit never reached, and the final answer claims the work is done
     retry_stall           the same failing call re-issued RETRY_MIN+ times, then abandoned (never succeeds)
     swallowed_error       a check or write fails and its target is never returned to
     regression            a check passes, a later write follows, and that check is never re-run
     out_of_order          shipped (commit/push/deploy/publish) after a write with no passing check in between
     forgotten_constraint  the request says not to touch X; a write to X comes later
     unverified_handoff    a sub-agent reports done and nothing checks after it
     context_overflow      a stretch of REDERIVE_MIN+ calls that repeat earlier successful calls, REDERIVE_GAP+ steps later
     budget_exhausted      the run ended on its step or time limit (from the agent's stop reason, when recorded)
   Not covered here (they need values, not tool calls): stale_value, drift, late_fault. A short or healthy run raises
   nothing: modes are calibrated on controls (bench T14, false-positive rate 0 on correct long runs).
3. SUBDIVISIONS: the run folded into a tree a reader can open on demand: sessions -> bursts (longrun's split) ->
   phases (look / write / check / ship / delegate stretches) -> steps, each node with its calls, seconds, failures,
   repeats, top tool, and whether a mode or the look-first step falls inside.
"""
import re

from analysis import TEST_CMD, action_type, unwrap
import longrun

RETRY_MIN=4
REDERIVE_MIN=6
REDERIVE_GAP=40
MODES=('skipped_unit','retry_stall','swallowed_error','regression','out_of_order','forgotten_constraint','unverified_handoff','context_overflow','budget_exhausted')
SHIP=re.compile(r'\b(git (commit|push)|gh (pr|release) (create|merge)|npm publish|twine upload|cargo publish|kubectl apply|terraform apply|docker push|deploy\b|fly deploy|vercel\b.*--prod)',re.I)
PATH=re.compile(r'(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|md|json|ya?ml|toml|sql|sh|css|html|c|cc|cpp|h))\b')
DONT=re.compile(r"\b(?:do not|don't|dont|never|must not|without)\s+(?:edit|touch|modify|change|delete|remove|write to|rewrite)\s+(?:anything in\s+|the\s+|any\s+)?([\w./-]+/?)",re.I)
PLAN_ITEM=re.compile(r'^\s*(?:\d+[.)]|[-*•])\s+(.+)$',re.M)
CLAIM_DONE=re.compile(r'\b(all (?:\w+ ){0,3}(?:done|complete|completed|finished|fixed|implemented|migrated|updated)|every (?:unit|item|step|file|task)|(?:task|work|everything) (?:is )?(?:done|complete)|completed all|done with all)\b',re.I)
HANDOFF_DONE=re.compile(r'\b(done|complete[d]?|finished|implemented|all (?:tests )?pass(?:ing|ed)?)\b',re.I)
LIMIT_STOP=re.compile(r'max[_ ]?turns|step[_ ]?limit|budget|timeout|timed out|max_tokens|context.*(?:full|limit)',re.I)
STOP=set('the a an and or to of in on for with by from into is are be this that it all each every then also any as at'.split())

def kind(s):
    if s.get('tool') in ('Task','Agent','spawn_agent','collabAgentToolCall') :return 'delegate'
    k=action_type({'kind':'tool','tool':s.get('tool'),'target':s.get('target')})
    if k=='AGENT_SPAWN':return 'delegate'
    if k=='FILE_WRITE':return 'write'
    if k in ('FILE_READ','SEARCH','WEB'):return 'look'
    t=unwrap(s.get('target'))
    if SHIP.search(t):return 'ship'
    if TEST_CMD.search(t):return 'check'
    return 'other'

def write_path(s):
    """The file a write step changes: the target for edit tools, the last path-looking token for shell writes."""
    t=str(s.get('target') or '')
    if s.get('tool') in ('Edit','Write','MultiEdit','NotebookEdit','fileChange','apply_patch'):return t.split(',')[0].strip()
    hits=PATH.findall(t)
    return hits[-1] if hits else t

def _key(s):
    k=kind(s)
    return (s.get('tool'),longrun.check_key(s.get('target')) if k=='check' else unwrap(s.get('target')))

def _ok(s):return s.get('ok') is not False

# ------------------------------------------------------------------ 1. milestones
def plan_units(request):
    """Units of work the request names: plan items (numbered/bulleted lines) with their keywords, else the files it names."""
    request=request or '';units=[]
    for m in PLAN_ITEM.finditer(request):
        text=m.group(1).strip();paths=PATH.findall(text)
        words=[w for w in re.findall(r'[A-Za-z_][\w-]{3,}',text) if w.lower() not in STOP]
        units.append({'id':f'u{len(units)+1}','label':text[:120],'evidence':paths or words[:3]})
    if len(units)<2:
        units=[{'id':f'f{i+1}','label':p,'evidence':[p]} for i,p in enumerate(dict.fromkeys(PATH.findall(request)))]
    return units if len(units)>=2 else []

def _hit(unit,s):
    hay=(str(s.get('target') or '')+' '+str(s.get('input') or '')[:400]).lower()
    ev=[e.lower() for e in unit['evidence']]
    return kind(s) in ('write','check','delegate') and any(e in hay for e in ev)

def milestones(steps,request=None):
    units=plan_units(request)
    if units:
        rows=[]
        for u in units:
            first=next((s for s in steps if _hit(u,s)),None)
            rows.append({**u,'reached':first is not None,'ref':first['ref'] if first else None,'index':steps.index(first) if first else None})
        source='plan'
    else:
        checks=[s for s in steps if kind(s)=='check' and _ok(s)]
        auto=[('look','first look',next((s for s in steps if kind(s)=='look'),None)),('write','first write',next((s for s in steps if kind(s)=='write'),None)),
              ('check','first passing check',checks[0] if checks else None),('final','final passing check',checks[-1] if checks and steps.index(checks[-1])>max((i for i,s in enumerate(steps) if kind(s)=='write'),default=-1) else None)]
        rows=[{'id':i,'label':l,'evidence':[],'reached':s is not None,'ref':s['ref'] if s else None,'index':steps.index(s) if s else None} for i,l,s in auto]
        source='run'
    reached=[r for r in rows if r['reached']];order=[r['index'] for r in reached]
    in_order=order==sorted(order)
    stalled=next((r for r in rows if not r['reached']),None)
    return {'source':source,'units':rows,'reached':len(reached),'total':len(rows),'in_order':in_order,
            'stalled_at':stalled['id'] if stalled and reached else None,'stalled_label':stalled['label'] if stalled and reached else None}

# ------------------------------------------------------------------ 2. failure modes
def modes(steps,request=None,answer=None,stop_reason=None,ms=None):
    out=[];n=len(steps);K=[kind(s) for s in steps];keys=[_key(s) for s in steps]
    def add(mode,i,why,**kw):out.append({'mode':mode,'ref':steps[i]['ref'] if i is not None and i<n else None,'index':i,'why':why,**kw})
    ms=ms or milestones(steps,request)
    # skipped_unit
    if ms['source']=='plan' and answer and CLAIM_DONE.search(str(answer)):
        miss=[u for u in ms['units'] if not u['reached']]
        if miss:add('skipped_unit',n-1,f"the answer says the work is done; planned unit never touched: {miss[0]['label'][:80]}",unit=miss[0]['id'])
    # retry_stall: RETRY_MIN+ failures of one call, never succeeding later
    fails={}
    for i,s in enumerate(steps):
        if not _ok(s):fails.setdefault(keys[i],[]).append(i)
    for k,ix in fails.items():
        if len(ix)>=RETRY_MIN and not any(_ok(steps[j]) and keys[j]==k for j in range(ix[0],n)):
            add('retry_stall',ix[0],f'the same failing call was issued {len(ix)} times and then abandoned',count=len(ix))
    # swallowed_error: a failed check/write whose target never comes back
    stalled_keys={k for k,ix in fails.items() if len(ix)>=RETRY_MIN}
    for i,s in enumerate(steps):
        if _ok(s) or K[i] not in ('check','write') or keys[i] in stalled_keys:continue
        tgt=write_path(s) if K[i]=='write' else keys[i][1]
        later=steps[i+1:]
        back=any(_key(x)==keys[i] or (K[i]=='write' and kind(x)=='write' and write_path(x)==tgt) for x in later)
        if not back and i<n-1:add('swallowed_error',i,f"{'check' if K[i]=='check' else 'write'} failed and was never returned to: {str(tgt)[:80]}")
    # regression: a file written, then a check C passes, then the file is written again late, and after that C is never
    # re-run and no whole-suite check runs (some other check does, so the agent believed it verified)
    full=lambda i:K[i]=='check' and not PATH.search(keys[i][1])
    for w in range(n-1,-1,-1):
        if K[w]!='write':continue
        P=write_path(steps[w]);a=next((x for x in range(w-1,-1,-1) if K[x]=='write' and write_path(steps[x])==P),None)
        if a is None:continue
        c=next((x for x in range(a+1,w) if K[x]=='check' and _ok(steps[x])),None)
        if c is None:continue
        after=range(w+1,n)
        if any(K[x]=='check' for x in after) and not any(keys[x]==keys[c] or full(x) for x in after):
            add('regression',w,f"late write to {P[:60]} after {keys[c][1][:50]} passed; that check was never re-run",check=keys[c][1])
        break
    # out_of_order: ship after a write with no passing check between
    w=None
    for i,k in enumerate(K):
        if k=='write':w=i
        elif k=='check' and _ok(steps[i]):w=None
        elif k=='ship' and w is not None:add('out_of_order',i,'shipped before the latest write was checked',write_ref=steps[w]['ref']);break
    # forgotten_constraint
    for m in DONT.finditer(request or ''):
        p=m.group(1).rstrip('.,;:)').lower()
        if len(p)<3:continue
        for i,s in enumerate(steps):
            if K[i]=='write' and p in write_path(s).lower():
                add('forgotten_constraint',i,f"the request says not to touch {p}; call {i+1} of {n} writes {write_path(s)[:60]}",constraint=p);break
    # unverified_handoff
    for i,s in enumerate(steps):
        if K[i]=='delegate' and _ok(s) and HANDOFF_DONE.search(str(s.get('output') or '')) and not any(K[x]=='check' for x in range(i+1,n)):
            add('unverified_handoff',i,'a sub-agent reported done and nothing checked its work afterwards');break
    # context_overflow: a stretch re-deriving earlier successful work long after
    first={}
    for i,s in enumerate(steps):
        if _ok(s):first.setdefault(keys[i],i)
    run=[]
    for i,s in enumerate(steps):
        old=first.get(keys[i])
        if _ok(s) and old is not None and old<i and i-old>=REDERIVE_GAP and K[i]in('look','other','check'):run.append(i)
        else:
            if len(run)>=REDERIVE_MIN:break
            run=[]
    if len(run)>=REDERIVE_MIN:add('context_overflow',run[0],f'{len(run)} calls in a row repeat work already done {run[0]-first[keys[run[0]]]}+ steps earlier: re-derived, at full cost',count=len(run))
    # budget_exhausted
    if stop_reason and LIMIT_STOP.search(str(stop_reason)):
        add('budget_exhausted',n-1,f'the run stopped on its limit ({str(stop_reason)[:40]}) with {ms["total"]-ms["reached"]} of {ms["total"]} milestones left')
    return sorted(out,key=lambda m:(m['index'] if m['index'] is not None else n))

# ------------------------------------------------------------------ 3. subdivisions
def _node(kind_,label,ss,marks,look):
    secs=sum(float(s.get('seconds') or 0) for s in ss);tools={}
    for s in ss:tools[s.get('tool')]=tools.get(s.get('tool'),0)+1
    seen=set();rep=0
    for s in ss:
        k=_key(s);rep+=k in seen and _ok(s);seen.add(k)
    refs={s['ref'] for s in ss}
    return {'kind':kind_,'label':label,'from':ss[0]['ref'] if ss else None,'to':ss[-1]['ref'] if ss else None,'calls':len(ss),'seconds':round(secs,1),
            'failed':sum(not _ok(s) for s in ss),'repeats':rep,'top_tool':max(tools,key=tools.get) if tools else None,
            'modes':sorted({m['mode'] for m in marks if m['ref'] in refs}),'look_first':look in refs,'children':[]}

def subdivide(steps,marks=(),look=None):
    """sessions -> bursts -> phases -> (steps are the leaves, referenced by from/to)."""
    rd=longrun.read([s for s in steps if s.get('at') is not None]) if any(s.get('at') is not None for s in steps) else None
    timed=sorted([s for s in steps if s.get('at') is not None],key=lambda s:s['at']) if rd else list(steps)
    root=_node('run','run',timed,marks,look)
    if not rd:
        bursts=[{'session':0,'calls':timed}]
        sessions=[{'i':0}]
    else:
        i=0;bursts=[]
        for b in rd['bursts']:bursts.append({'session':b['session'],'calls':timed[i:i+b['calls']],'status':b['status']});i+=b['calls']
        sessions=[{'i':k} for k in range(len(rd['sessions']))]
    for si,_ in enumerate(sessions):
        bs=[b for b in bursts if b['session']==si];sn=_node('session',f'session {si+1}',[c for b in bs for c in b['calls']],marks,look)
        for bi,b in enumerate(bs):
            bn=_node('burst',f"burst {bi+1}"+(f" · {b['status']}" if b.get('status') else ''),b['calls'],marks,look)
            cur=[];ph=None
            for s in b['calls']+[None]:
                k=kind(s) if s else None
                if s is not None and (ph is None or k==ph or (k=='other' and ph!='ship')):
                    cur.append(s);ph=ph or k;continue
                if cur:bn['children'].append(_node('phase',ph,cur,marks,look))
                cur=[s] if s else [];ph=k
            sn['children'].append(bn)
        root['children'].append(sn)
    if len(root['children'])==1:root['children']=root['children'][0]['children']
    return root

# ------------------------------------------------------------------ all three
def read(steps,request=None,answer=None,stop_reason=None):
    if not steps:return None
    ms=milestones(steps,request);mk=modes(steps,request,answer,stop_reason,ms)
    rd=longrun.read([s for s in steps if s.get('at') is not None]) if any(s.get('at') is not None for s in steps) else None
    look=(rd or {}).get('look_first',{}) or {}
    first=mk[0] if mk else None
    return {'steps':len(steps),'milestones':ms,'modes':mk,'tree':subdivide(steps,mk,look.get('ref')),
            'look_first':{'ref':first['ref'],'why':f"{first['mode'].replace('_',' ')}: {first['why']}"} if first else (look or None),
            'basis':{'retry_min':RETRY_MIN,'rederive_min':REDERIVE_MIN,'rederive_gap':REDERIVE_GAP,
                     'not_covered':['stale_value','drift','late_fault']}}

def summary(r):
    ms=r['milestones'];head=(f"{ms['reached']}/{ms['total']} planned units reached" if ms['source']=='plan' else f"{ms['reached']}/{ms['total']} run milestones")
    if ms.get('stalled_at'):head+=f", stalled before {ms['stalled_label'][:60]}"
    if not ms['in_order']:head+=', reached out of order'
    if not r['modes']:return head+'. No long-horizon failure mode.'
    return head+'. '+'; '.join(f"{m['mode'].replace('_',' ')} at step {m['ref']}" for m in r['modes'][:4])+'.'
