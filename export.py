"""Exports from the trace DB.

export_rl: preference data for training elsewhere (reward models, DPO/KTO on open models).
  DPO rows: prompt, chosen, rejected, chosen_model, rewards, trajectories, judge labels, label source.
  Chosen = the user's blind label or /prefer when present, else a unanimous judge-panel verdict
  (confirmed reward hacks override). Ties and splits are skipped.
  KTO rows: one per answer, desirable = mean judge score >= 4 and no confirmed hack.
export_otel: OTLP/JSON with OpenTelemetry GenAI semantic-convention attributes (v1.41,
  "Development" status) so agentlens traces open in Phoenix, Langfuse, Datadog or Docent.
"""
import hashlib
import json
from collections import defaultdict
from pathlib import Path

def _events(store,session=None):
    store.flush(3)
    with store.lock,store.connect() as db:
        q='SELECT session_id,id,ts,type,provider,span_id,data FROM events'+(' WHERE session_id=?' if session else '')+' ORDER BY session_id,id'
        rows=db.execute(q,(session,) if session else ()).fetchall()
        spans=db.execute('SELECT span_id,session_id,parent_id,kind,provider,role,mode,start,end,status FROM spans'+(' WHERE session_id=?' if session else ''),(session,) if session else ()).fetchall()
        pay={(s,e):(k,t) for s,e,k,t in db.execute('SELECT session_id,event_id,kind,text FROM payloads')}
    ev=[{'session_id':s,'id':i,'timestamp':ts,'type':t,'provider':p,'span_id':sp,**json.loads(d)} for s,i,ts,t,p,sp,d in rows]
    sp={r[0]:dict(zip(('span_id','session_id','parent_id','kind','provider','role','mode','start','end','status'),r)) for r in spans}
    return ev,sp,pay

def turns(store):
    ev,sp,pay=_events(store);T=defaultdict(lambda:{'answers':{},'steps':defaultdict(list),'scores':defaultdict(list),'verdicts':set(),'labels':[],'roots':defaultdict(list),'step_labels':defaultdict(list),'hacks':set(),'override':None,'prompt':None})
    for e in ev:
        s=sp.get(e.get('span_id'))
        if e['type']=='prompt' and s and s['kind']=='turn':T[s['span_id']]['prompt']=(pay.get((e['session_id'],e['id'])) or (None,None))[1];T[s['span_id']]['mode']=s['mode']
        elif s and s['kind']=='agent' and s['role'] in ('responder','reader','writer') and s['parent_id']:
            t=T[s['parent_id']]
            if e['type'] in ('answer','agent.completed') and (e['session_id'],e['id']) in pay and pay[(e['session_id'],e['id'])][0]=='answer':t['answers'][e['provider']]=pay[(e['session_id'],e['id'])][1]
            elif e['type']=='tool.started':t['steps'][e['provider']].append({'tool':e.get('tool'),'target':e.get('detail'),'ok':None,'ref':e['id'],'item':e.get('item_id'),'at':e['timestamp']})
            elif e['type']=='tool.completed':
                for st in reversed(t['steps'][e['provider']]):
                    if st['ok'] is None and (not e.get('item_id') or st['item']==e.get('item_id')):
                        st['ok']=e.get('passed') is not False;st['seconds']=max(0.0,e['timestamp']-st['at']);o=pay.get((e['session_id'],e['id']))
                        if o and o[1]:st['output']=o[1][:2000]
                        break
        tid=e.get('turn_id')
        if not tid:continue
        t=T[tid]
        if e['type']=='judge.result':t['scores'][e['provider']].append(float(e.get('score') or 0))
        elif e['type']=='judge.verdict':t['verdicts'].add(e.get('verdict'))
        elif e['type']=='judge.verdict_override':t['override']=e.get('verdict')
        elif e['type']=='judge.hack_final':t['hacks'].add(e['provider'])
        elif e['type']=='judge.root':t['roots'][e['provider']].append({'category':e.get('name'),'why':e.get('rationale'),'ref':e.get('item_id')})
        elif e['type']=='judge.step':t['step_labels'][e['provider']].append({'ref':e.get('item_id'),'label':e.get('verdict'),'why':e.get('rationale')})
        elif e['type']=='feedback' and e.get('reason_code') in ('label','prefer'):t['labels'].append(e.get('verdict'))
    return T

def export_rl(store,out,fmt='dpo'):
    T=turns(store);n=0;skipped=0
    with open(out,'w') as f:
        for tid,t in T.items():
            if not t['prompt'] or not t['answers']:continue
            mean={p:round(sum(v)/len(v),3) for p,v in t['scores'].items() if v}
            if fmt=='kto':
                for p,ans in t['answers'].items():
                    if p not in mean:continue
                    f.write(json.dumps({'prompt':t['prompt'],'completion':ans,'label':mean[p]>=4 and p not in t['hacks'],'model':p,'reward':mean[p],'turn_id':tid})+'\n');n+=1
                continue
            if set(t['answers'])<{'codex','claude'}:continue
            user=[l for l in t['labels'] if l in ('codex','claude')]
            if user:chosen,source=user[-1],'user'
            elif t['override']:chosen,source=t['override'],'judge_override_reward_hack'
            elif len(t['verdicts'])==1 and next(iter(t['verdicts'])) in ('codex','claude'):chosen,source=next(iter(t['verdicts'])),'judge_panel'
            else:skipped+=1;continue
            rejected='claude' if chosen=='codex' else 'codex'
            f.write(json.dumps({'prompt':t['prompt'],'chosen':t['answers'][chosen],'rejected':t['answers'][rejected],'chosen_model':chosen,'label_source':source,
                'rewards':mean,'trajectories':{p:[{k:v for k,v in s.items() if k not in ('item','output','at','seconds')} for s in st] for p,st in t['steps'].items()},
                'judge_labels':{'root_causes':dict(t['roots']),'steps':dict(t['step_labels']),'reward_hack':sorted(t['hacks'])},'turn_id':tid})+'\n');n+=1
    return n,skipped

def _hex(seed,n):return hashlib.sha1(seed.encode()).hexdigest()[:n]

def export_otel(store,out,session=None):
    ev,sp,_=_events(store,session)
    spans=[]
    op={'turn':'invoke_workflow','agent':'invoke_agent','judge':'invoke_agent'}
    scores=defaultdict(list)
    for e in ev:
        if e['type']=='judge.result':scores[(e.get('turn_id'),e['provider'])].append(e)
    for s in sp.values():
        trace=_hex(s['session_id'],32)
        attrs={'gen_ai.operation.name':op.get(s['kind'],'invoke_agent'),'gen_ai.agent.name':s['provider'],'agentlens.role':s['role'] or s['kind'],'agentlens.mode':s['mode'] or ''}
        if s['kind']=='turn':attrs['gen_ai.conversation.id']=s['session_id']
        events=[]
        if s['kind']=='turn':
            for p in ('codex','claude'):
                for r in scores.get((s['span_id'],p),[]):
                    events.append({'name':'gen_ai.evaluation.result','timeUnixNano':int(r['timestamp']*1e9),'attributes':_kv({'gen_ai.evaluation.name':'agentlens.judge.'+str(r.get('judge')),'gen_ai.evaluation.score.value':r.get('score'),'gen_ai.agent.name':p})})
        spans.append({'traceId':trace,'spanId':_hex(s['span_id'],16),'parentSpanId':_hex(s['parent_id'],16) if s['parent_id'] else '','name':f"{attrs['gen_ai.operation.name']} {s['provider']}",
                      'kind':1,'startTimeUnixNano':int((s['start'] or 0)*1e9),'endTimeUnixNano':int((s['end'] or s['start'] or 0)*1e9),'attributes':_kv(attrs),'events':events,
                      'status':{'code':2 if s['status']=='failed' else 1}})
    pending={}
    for e in ev:
        if e['type']=='tool.started':pending[(e['session_id'],e.get('item_id') or e['id'])]=e
        elif e['type']=='tool.completed':
            st=pending.pop((e['session_id'],e.get('item_id')),None)
            if not st or st.get('span_id') not in sp:continue
            spans.append({'traceId':_hex(e['session_id'],32),'spanId':_hex(f"{e['session_id']}:{st['id']}",16),'parentSpanId':_hex(st['span_id'],16),'name':f"execute_tool {st.get('tool')}",'kind':1,
                          'startTimeUnixNano':int(st['timestamp']*1e9),'endTimeUnixNano':int(e['timestamp']*1e9),
                          'attributes':_kv({'gen_ai.operation.name':'execute_tool','gen_ai.tool.name':st.get('tool'),'gen_ai.tool.call.id':str(st.get('item_id') or st['id']),'gen_ai.agent.name':st['provider'],'agentlens.tool.target':st.get('detail') or ''}),
                          'status':{'code':2 if e.get('passed') is False else 1}})
    doc={'resourceSpans':[{'resource':{'attributes':_kv({'service.name':'duo','telemetry.sdk.name':'duo'})},'scopeSpans':[{'scope':{'name':'duo','version':'semconv-genai-1.41'},'spans':spans}]}]}
    Path(out).write_text(json.dumps(doc));return len(spans)

def _kv(d):
    out=[]
    for k,v in d.items():
        if v is None:continue
        val={'doubleValue':v} if isinstance(v,float) else {'intValue':v} if isinstance(v,int) and not isinstance(v,bool) else {'stringValue':str(v)}
        out.append({'key':k,'value':val})
    return out
