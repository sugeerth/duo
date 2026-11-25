"""Run diff (AgentDiff's pair comparison, on AgentLens traces): any two runs, step-aligned, with a five-line card.

    VERDICT   which run came out better, by the instruments (and the grade, when judged)
    DIVERGE   the first aligned step where the two runs did different things
    CAUSE     the decisive step of the worse run: its first long-horizon mode, else its look-first step, and what
              the better run did at the aligned point
    COST      steps, wall time and failed calls, each run against the other ("faster to nothing" when the cheaper run
              is the worse one)
    FIX       the change the decisive mode calls for, and the better run's action at that point
    CONF      how far to trust it: one pair is one observation, and the decisive step is read from detectors, not
              replay-verified; the card names the fork command that would test it

Deterministic, no model calls. The alignment is analysis.align (Needleman-Wunsch over action type and target).
"""
import analysis
import export
import fleet

FIX_FOR={
 'skipped_unit':'finish every planned unit, or say which were not done; check the plan before claiming completion',
 'retry_stall':'after two identical failures, read the error and change the call instead of re-issuing it',
 'swallowed_error':'return to the failed step and fix or report it before moving on',
 'regression':'re-run the check that covered the earlier change after the late edit',
 'out_of_order':'run the checks before shipping (commit, push, deploy)',
 'forgotten_constraint':'re-read the request\'s constraints before writing; the forbidden path stays untouched',
 'unverified_handoff':'check a sub-agent\'s work (run its tests) before relying on it',
 'context_overflow':'keep notes of what was already found instead of re-deriving it',
 'budget_exhausted':'plan within the step budget; report partial progress before the limit',
}

def _steps(st):
    out=[]
    for s in st:
        k=analysis.action_type({'kind':'tool','tool':s.get('tool'),'target':s.get('target')})
        out.append({**s,'kind':'tool','type':k,'key':analysis.target_key(s,k)})
    return out

def _wall(st):
    ts=[s['at'] for s in st if s.get('at') is not None]
    return (max(ts)+(st[-1].get('seconds') or 0)-min(ts)) if ts else None

def _score(r):
    """Lower is better: instruments fired (weighted), then a low grade."""
    return (r['risk'],-(r['grade'] if r['grade'] is not None else 3.5))

def _fmt_s(v):
    if v is None:return '?'
    return f'{v:.0f} s' if abs(v)<120 else f'{v/60:.1f} min'

def compare(ta,pa,ra,tb,pb,rb):
    """ta/tb: export.turns entries; pa/pb providers; ra/rb fleet rows for the two runs."""
    A,B=_steps(ta['steps'][pa]),_steps(tb['steps'][pb]);al=analysis.align(A,B)
    byA={s['ref']:s for s in A};byB={s['ref']:s for s in B};pairs=al['pairs']
    better,worse=('a','b') if _score(ra)<=_score(rb) else ('b','a')
    R={'a':ra,'b':rb};S={'a':A,'b':B};name={'a':'run A','b':'run B'}
    if not ra['fired'] and not rb['fired']:verdict='Both runs are clean on every instrument.'
    elif ra['fired'] and rb['fired'] and _score(ra)==_score(rb):verdict=f"Both runs have problems: A caught by {', '.join(ra['fired'])}; B by {', '.join(rb['fired'])}."
    else:
        w=R[worse];verdict=f"{name[better].replace('run','Run')} came out better; {name[worse]} was caught by {', '.join(w['fired'])}"+(f" (grade {w['grade']})" if w['grade'] is not None else '')+'.'
    # first divergence
    div=al['divergence'];dv=None
    if div is not None:
        x,y,_=pairs[div];sa=byA.get(x);sb=byB.get(y)
        what=lambda s:f"{s['tool']} {str(s.get('target') or '')[:70]}" if s else 'nothing'
        dv={'pair':div,'a_ref':x,'b_ref':y,'text':f"at aligned step {div+1}: A did {what(sa)}; B did {what(sb)}"}
    # decisive step of the worse run
    w=R[worse];cause=None
    if w['fired']:
        ref=w.get('look_first');why=w.get('why') or ''
        side=0 if worse=='a' else 1;k=next((i for i,p in enumerate(pairs) if p[side]==ref),None)
        other=pairs[k][1-side] if k is not None else None;ob=(byB if worse=='a' else byA).get(other)
        mode=next((m for m in w['modes']),None)
        cause={'run':worse,'ref':ref,'pair':k,'mode':mode,'why':why,'after_divergence':div is not None and k is not None and k>=div,
               'counterpart':f"{ob['tool']} {str(ob.get('target') or '')[:70]}" if ob else None}
    # cost
    na,nb=len(A),len(B);wa,wb=_wall(ta['steps'][pa]),_wall(tb['steps'][pb]);fa=sum(s.get('ok') is False for s in A);fb=sum(s.get('ok') is False for s in B)
    cheaper='a' if na<nb else 'b' if nb<na else None
    cost={'steps':[na,nb],'seconds':[wa,wb],'failed':[fa,fb],
          'text':f"A {na} steps, {_fmt_s(wa)}, {fa} failed call{'s'*(fa!=1)} · B {nb} steps, {_fmt_s(wb)}, {fb} failed call{'s'*(fb!=1)}"+
                 (f" · {name[cheaper]} was {abs(na-nb)} steps cheaper"+(' and the worse run: faster to nothing' if cheaper==worse and w['fired'] else '') if cheaper else '')}
    fix=None
    if cause:
        f=FIX_FOR.get(cause['mode']) if cause['mode'] else None
        fix=(f[0].upper()+f[1:]+'.' if f else 'Look at the decisive step first; no named mode applies.')+(f" At that point the better run did {cause['counterpart']}." if cause['counterpart'] else '')
    conf=('low' if not cause or cause['pair'] is None else 'medium')
    conf_text=f"{conf}: one pair is one observation, and the decisive step is read from the detectors, not replay-verified."
    return {'verdict':verdict,'better':better if (ra['fired'] or rb['fired']) else None,'divergence':dv,'cause':cause,'cost':cost,'fix':fix,
            'conf':conf,'conf_text':conf_text,'similarity':al['similarity'],
            'lanes':[[p[0],p[1],round(p[2],2),(byA.get(p[0]) or {}).get('type'),(byB.get(p[1]) or {}).get('type'),
                      (byA.get(p[0]) or {}).get('ok') is False,(byB.get(p[1]) or {}).get('ok') is False] for p in pairs],
            'runs':{'a':{k:ra[k] for k in ('turn','provider','session','steps','grade','fired','modes','prompt')},
                    'b':{k:rb[k] for k in ('turn','provider','session','steps','grade','fired','modes','prompt')}}}

def snapshot(store,q):
    """GET /diff?a=TURN|PROVIDER&b=TURN|PROVIDER"""
    try:(a_t,a_p),(b_t,b_p)=[str(q.get(k) or '').split('|',1) for k in ('a','b')]
    except ValueError:return {'error':'pass a=TURN|PROVIDER and b=TURN|PROVIDER'}
    T=export.turns(store);ev,sp,_=export._events(store)
    if a_t not in T or b_t not in T or a_p not in T[a_t]['steps'] or b_p not in T[b_t]['steps']:return {'error':'run not found'}
    ra=fleet.run_row(a_t,a_p,T[a_t],sp.get(a_t) or {},0);rb=fleet.run_row(b_t,b_p,T[b_t],sp.get(b_t) or {},0)
    out=compare(T[a_t],a_p,ra,T[b_t],b_p,rb)
    worse=out['cause']['run'] if out['cause'] else None
    if worse and out['cause']['ref'] is not None:
        tid,p=(a_t,a_p) if worse=='a' else (b_t,b_p)
        if 'write' in (T[tid].get('mode') or ''):out['conf_text']+=' A replay test needs the workspace as it was at that step, which write turns do not store yet.'
        else:out['conf_text']+=f" Test it: agentlens --fork-step {tid[:8]} {out['cause']['ref']} --samples 5."
    return out
