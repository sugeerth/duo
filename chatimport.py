"""Import chat transcripts into the trace DB so the viewer reads chat sessions like terminal sessions.

`duo --import-chat FILE [--chat-provider claude|codex]`. One conversation becomes one stored session; each user
message and the reply that follows become one turn (mode `respond:chat`) with:
- the prompt and the answer as payloads,
- the model's thinking, when the export carries it, as reasoning steps,
- `memory.context`: a chat app sends the whole conversation so far, so every earlier turn counts as carried in,
  with its size in characters.

Formats, detected from the data:
- claude.ai export (`conversations.json`): `chat_messages` with `sender`, `text`/`content` and `created_at`.
  Thinking blocks in `content` become reasoning. Provider: claude.
- ChatGPT export (`conversations.json`): `mapping` nodes, following `current_node` back to the root. Messages from
  a reasoning model's `thoughts` content become reasoning. Provider: codex (the viewer's OpenAI lane).
- A plain list of messages `[{"role","content","timestamp"?}]` or `{"messages":[...]}` (JSON or JSONL). Pass
  --chat-provider to say which lane it belongs to.
Nothing is sent anywhere. Timestamps come from the export; when missing, turns are spaced one minute apart.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

def _ts(v,fallback):
    if v is None or v=='':return fallback
    if isinstance(v,(int,float)):return float(v)/1000 if v>1e12 else float(v)
    try:return datetime.fromisoformat(str(v).replace('Z','+00:00')).timestamp()
    except ValueError:return fallback

def _text(content):
    if content is None:return ''
    if isinstance(content,str):return content
    if isinstance(content,list):return '\n'.join(_text(c) for c in content if not (isinstance(c,dict) and c.get('type') in ('thinking','redacted_thinking')))
    if isinstance(content,dict):
        if 'parts' in content:return '\n'.join(p if isinstance(p,str) else _text(p) for p in content['parts'] or [])
        return str(content.get('text') or content.get('content') or '')
    return str(content)

def _thinking(content):
    if isinstance(content,list):return [c.get('thinking') or '' for c in content if isinstance(c,dict) and c.get('type')=='thinking' and c.get('thinking')]
    if isinstance(content,dict) and content.get('content_type')=='thoughts':return [t.get('content','') for t in content.get('thoughts') or [] if t.get('content')]
    return []

def claude_messages(conv):
    out=[]
    for m in conv.get('chat_messages') or []:
        role='user' if m.get('sender')=='human' else 'assistant'
        content=m.get('content') if m.get('content') else m.get('text')
        out.append({'role':role,'text':m.get('text') or _text(content),'thinking':_thinking(m.get('content')),'ts':m.get('created_at'),'model':m.get('model')})
    return out

def chatgpt_messages(conv):
    mapping=conv.get('mapping') or {};node=conv.get('current_node');chain=[]
    while node and node in mapping:
        chain.append(mapping[node]);node=mapping[node].get('parent')
    out=[];pending=[]
    for n in reversed(chain):
        m=n.get('message')
        if not m:continue
        role=(m.get('author') or {}).get('role');content=m.get('content') or {}
        if content.get('content_type')=='thoughts':pending+=_thinking(content);continue
        if role not in ('user','assistant'):continue
        text=_text(content)
        if not text.strip():continue
        meta=m.get('metadata') or {}
        out.append({'role':role,'text':text,'thinking':pending if role=='assistant' else [],'ts':m.get('create_time'),'model':meta.get('model_slug')})
        if role=='assistant':pending=[]
    return out

def plain_messages(items):
    return [{'role':'user' if (m.get('role') or m.get('sender')) in ('user','human') else 'assistant','text':_text(m.get('content',m.get('text'))),
             'thinking':_thinking(m.get('content')),'ts':m.get('timestamp') or m.get('created_at'),'model':m.get('model')} for m in items if isinstance(m,dict)]

def load(path,provider=None):
    """[(title, provider, messages)] from an export file."""
    raw=Path(path).read_text(encoding='utf-8').strip()
    try:data=json.loads(raw)
    except ValueError:data=[json.loads(line) for line in raw.splitlines() if line.strip()]
    if isinstance(data,dict):data=[data] if ('chat_messages' in data or 'mapping' in data) else [data] if 'messages' in data else [data]
    convs=[]
    if data and all(isinstance(x,dict) and ('role' in x or 'sender' in x) for x in data):
        return [(Path(path).stem,provider or 'claude',plain_messages(data))]
    for c in data:
        if not isinstance(c,dict):continue
        if 'chat_messages' in c:convs.append((c.get('name') or c.get('uuid') or 'Claude chat',provider or 'claude',claude_messages(c)))
        elif 'mapping' in c:convs.append((c.get('title') or c.get('id') or 'ChatGPT chat',provider or 'codex',chatgpt_messages(c)))
        elif 'messages' in c:convs.append((c.get('title') or 'chat',provider or 'claude',plain_messages(c['messages'])))
    return convs

def write(store,title,provider,messages,now=None):
    """Write one conversation as a stored session. Returns (session_id, turns)."""
    base=_ts(messages[0]['ts'],None) if messages else None
    base=base if base is not None else (now or datetime.now().timestamp())-60*len(messages)
    sid='chat-'+hashlib.sha1((title+str(base)+provider).encode()).hexdigest()[:12]
    store.session(sid,base,'chat: '+title[:80]);n={'id':0}
    def ev(ts,kind,prov='duo',span=None,payload=None,**f):
        n['id']+=1;e={'session_id':sid,'id':n['id'],'timestamp':ts,'type':kind,'provider':prov,**({'span_id':span} if span else {}),**f}
        if payload:e['chars']=len(payload)
        store.event(e,payload,kind if payload else None)
    turns=0;hist_chars=0;clock=base;i=0
    while i<len(messages):
        if messages[i]['role']!='user':i+=1;continue
        q=messages[i];replies=[]
        j=i+1
        while j<len(messages) and messages[j]['role']=='assistant':replies.append(messages[j]);j+=1
        t0=_ts(q['ts'],clock);t1=_ts(replies[-1]['ts'],t0+20) if replies else t0+1
        t1=max(t1,t0+.5);turns+=1;tid=f'{sid}-t{turns}';aid=tid+'-'+provider
        ev(t0,'span.start','duo',tid,span_id=tid,span_kind='turn',mode='respond:chat')
        ev(t0,'prompt','duo',tid,q['text'])
        ev(t0,'memory.context','duo',tid,count=turns-1,data={'history_turns':turns-1,'history_chars':hist_chars,'context_items':0,'context_chars':0})
        model=next((r['model'] for r in replies if r.get('model')),None)
        ev(t0,'span.start',provider,aid,span_id=aid,parent_id=tid,span_kind='agent',role='responder',mode='respond:chat',**({'model':model} if model else {}))
        thoughts=[x for r in replies for x in r['thinking']];answer='\n\n'.join(r['text'] for r in replies)
        for k,th in enumerate(thoughts):ev(t0+(t1-t0)*(k+1)/(len(thoughts)+1),'reasoning',provider,aid,th)
        if answer:ev(t1,'answer',provider,aid,answer)
        ev(t1,'span.end',provider,aid,span_id=aid,parent_id=tid,span_kind='agent',role='responder',status='ok' if answer else 'failed',duration_ms=int((t1-t0)*1000),tools=0,tool_errors=0)
        ev(t1,'span.end','duo',tid,span_id=tid,span_kind='turn',status='ok',duration_ms=int((t1-t0)*1000))
        hist_chars+=len(q['text'])+len(answer);clock=t1+60;i=j
    return sid,turns

def import_file(store,path,provider=None):
    out=[]
    for title,prov,msgs in load(path,provider):
        if any(m['role']=='user' for m in msgs):out.append((title,)+write(store,title,prov,msgs))
    store.flush(10)
    return out

# ---------------------------------------------------------------- single-agent terminal sessions
"""Claude Code (`~/.claude/projects/<project>/<session>.jsonl`) and Codex CLI (`~/.codex/sessions/.../rollout-*.jsonl`)
logs import as single-agent sessions: one turn per user prompt, with every tool call (target, success, output),
reasoning, the final answer, and `context.tokens`, the model's input size on each call, which is the context window
actually measured. The long-run reading runs on each turn's tool calls."""

def _detail(name,inp):
    if not isinstance(inp,dict):return str(inp)[:200]
    for k in ('command','cmd','file_path','path','notebook_path','pattern','url','query','description'):
        v=inp.get(k)
        if v:return (' '.join(v) if isinstance(v,list) else str(v))[:200]
    return next((str(v)[:200] for v in inp.values() if isinstance(v,str)),'')

def claude_code_turns(lines):
    """[{prompt, ts, steps:[{kind:'tool'|'think', ...}], answer, ctx:[(ts,tokens)], usage}] from Claude Code JSONL records."""
    turns=[];cur=None;pending={}
    for d in lines:
        if d.get('isSidechain'):continue
        t=d.get('type');m=d.get('message') or {};ts=_ts(d.get('timestamp'),None);content=m.get('content')
        if t=='system' and d.get('subtype')=='compact_boundary' and cur is not None:cur['compacted']=True
        if t=='user':
            if isinstance(content,str) or (isinstance(content,list) and not any(isinstance(c,dict) and c.get('type')=='tool_result' for c in content)):
                text=_text(content).strip()
                if not text or text.startswith('<'):continue  # command wrappers, hook output
                cur={'prompt':text,'ts':ts,'steps':[],'answer':'','ctx':[],'usage':None,'compacted':False};turns.append(cur);continue
            for c in content if isinstance(content,list) else []:
                if isinstance(c,dict) and c.get('type')=='tool_result' and c.get('tool_use_id') in pending:
                    st=pending.pop(c['tool_use_id']);st['end']=ts;st['ok']=not c.get('is_error');st['output']=_text(c.get('content'))[:20000]
        elif t=='assistant' and cur is not None:
            u=m.get('usage') or {}
            if u:
                tok=(u.get('input_tokens') or 0)+(u.get('cache_read_input_tokens') or 0)+(u.get('cache_creation_input_tokens') or 0)
                if tok:cur['ctx'].append((ts,tok))
                cur['usage']={'input_tokens':u.get('input_tokens') or 0,'cache_read_input_tokens':u.get('cache_read_input_tokens') or 0,
                              'cache_creation_input_tokens':u.get('cache_creation_input_tokens') or 0,'output_tokens':u.get('output_tokens') or 0}
            for c in content if isinstance(content,list) else []:
                if not isinstance(c,dict):continue
                if c.get('type')=='thinking' and c.get('thinking'):cur['steps'].append({'kind':'think','ts':ts,'text':c['thinking']})
                elif c.get('type')=='tool_use':
                    st={'kind':'tool','ts':ts,'tool':c.get('name'),'target':_detail(c.get('name'),c.get('input')),'input':json.dumps(c.get('input'))[:6000],'ok':None,'end':None,'output':''}
                    cur['steps'].append(st);pending[c.get('id')]=st
                elif c.get('type')=='text' and c.get('text'):cur['answer']=c['text']
    return turns

def codex_turns(lines):
    """Same shape from a Codex CLI rollout: response_item messages, reasoning, function calls and outputs; token_count events."""
    turns=[];cur=None;pending={}
    for d in lines:
        ts=_ts(d.get('timestamp'),None);p=d.get('payload') or {};kind=d.get('type');pt=p.get('type')
        if kind=='response_item' and pt=='message':
            text=_text(p.get('content'))
            if p.get('role')=='user':
                if not text.strip() or text.lstrip().startswith('<'):continue  # environment/instructions wrappers
                cur={'prompt':text.strip(),'ts':ts,'steps':[],'answer':'','ctx':[],'usage':None,'compacted':False};turns.append(cur)
            elif p.get('role')=='assistant' and cur is not None and text:cur['answer']=text
        elif cur is None:continue
        elif kind=='response_item' and pt=='reasoning':
            text='\n'.join(s.get('text','') for s in p.get('summary') or [] if isinstance(s,dict))
            if text:cur['steps'].append({'kind':'think','ts':ts,'text':text})
        elif kind=='response_item' and pt in ('function_call','custom_tool_call','local_shell_call'):
            args=p.get('arguments') or p.get('input') or p.get('action') or {}
            if isinstance(args,str):
                try:args=json.loads(args)
                except ValueError:args={'input':args}
            name=p.get('name') or 'shell';st={'kind':'tool','ts':ts,'tool':name,'target':_detail(name,args),'input':json.dumps(args)[:6000],'ok':None,'end':None,'output':''}
            cur['steps'].append(st);pending[p.get('call_id') or p.get('id')]=st
        elif kind=='response_item' and pt in ('function_call_output','custom_tool_call_output'):
            st=pending.pop(p.get('call_id'),None)
            if st:
                out=p.get('output');meta={}
                if isinstance(out,str):
                    try:o=json.loads(out);meta=o.get('metadata') or {};out=o.get('output',out)
                    except (ValueError,AttributeError):pass
                st['end']=ts;st['output']=_text(out)[:20000];code=meta.get('exit_code')
                st['ok']=(code==0) if code is not None else not str(st['output']).lower().startswith(('error','failed'))
        elif kind=='event_msg' and pt=='token_count':
            info=p.get('info') or {};last=info.get('last_token_usage') or {};total=info.get('total_token_usage') or {}
            if last.get('input_tokens'):cur['ctx'].append((ts,last['input_tokens']))
            if total:cur['usage']={'input_tokens':last.get('input_tokens') or 0,'output_tokens':last.get('output_tokens') or 0,'cached_input_tokens':last.get('cached_input_tokens') or 0}
        elif kind=='compacted' or pt=='compacted':cur['compacted']=True
    return turns

def write_agent_session(store,title,provider,turns,model=None):
    import longrun
    if not turns:return None,0
    base=turns[0]['ts'] or datetime.now().timestamp()
    sid=f'{provider}-'+hashlib.sha1((title+str(base)).encode()).hexdigest()[:12]
    store.session(sid,base,f'{"Claude Code" if provider=="claude" else "Codex CLI"}: {title[:80]}');n={'id':0}
    def ev(ts,kind,prov='duo',span=None,payload=None,**f):
        n['id']+=1;e={'session_id':sid,'id':n['id'],'timestamp':ts,'type':kind,'provider':prov,**({'span_id':span} if span else {}),**f}
        if payload:e['chars']=len(payload)
        store.event(e,payload,kind if payload else None);return e
    hist=0;carried=0
    for k,T in enumerate(turns,1):
        times=[x for x in [T['ts']]+[s['ts'] for s in T['steps']]+[s.get('end') for s in T['steps']]+[c[0] for c in T['ctx']] if x]
        t0=T['ts'] or (times[0] if times else base);t1=max(times) if times else t0+1
        tid=f'{sid}-t{k}';aid=tid+'-'+provider
        writes=any(str(s.get('tool','')).lower() in ('edit','write','multiedit','notebookedit','apply_patch') for s in T['steps'] if s['kind']=='tool')
        mode='work:write' if writes else 'work:read'
        ev(t0,'span.start','duo',tid,span_id=tid,span_kind='turn',mode=mode)
        ev(t0,'prompt','duo',tid,T['prompt'])
        ev(t0,'memory.context','duo',tid,count=carried,data={'history_turns':carried,'history_chars':hist,'context_items':0,'context_chars':0,'compacted':T.get('compacted',False)})
        ev(t0,'span.start',provider,aid,span_id=aid,parent_id=tid,span_kind='agent',role='writer' if writes else 'reader',mode=mode,**({'model':model} if model else {}))
        steps=[];fails=0
        for j,s in enumerate(T['steps']):
            if s['kind']=='think':ev(s['ts'] or t0,'reasoning',provider,aid,s['text']);continue
            start=s['ts'] or t0;end=s.get('end') or start
            e=ev(start,'tool.started',provider,aid,s.get('input'),tool=s['tool'],detail=s['target'],item_id=f'{aid}-{j}')
            ok=s['ok'] is not False;fails+=not ok
            ev(end,'tool.completed',provider,aid,s.get('output') or None,tool=s['tool'],item_id=f'{aid}-{j}',passed=ok)
            steps.append({'ref':e['id'],'tool':s['tool'],'target':s['target'],'ok':ok,'at':start,'seconds':max(0.0,end-start),'output':(s.get('output') or '')[-900:]})
        for ts,tok in T['ctx']:ev(ts or t1,'context.tokens',provider,aid,count=tok)
        if T['usage']:ev(t1,'usage',provider,aid,data=T['usage'])
        if T['answer']:ev(t1,'answer',provider,aid,T['answer'])
        ev(t1,'span.end',provider,aid,span_id=aid,parent_id=tid,span_kind='agent',role='writer' if writes else 'reader',status='ok',duration_ms=int((t1-t0)*1000),tools=len(steps),tool_errors=fails)
        _flag_hacks(ev,steps,provider,tid);_review_security(ev,steps,provider,tid)
        r=longrun.read(steps)
        if r:ev(t1,'analysis.longrun',provider,turn_id=tid,data=r,rationale=longrun.summary(r))
        ev(t1,'span.end','duo',tid,span_id=tid,span_kind='turn',status='ok',duration_ms=int((t1-t0)*1000))
        carried=0 if T.get('compacted') else carried+1;hist=0 if T.get('compacted') else hist+len(T['prompt'])+len(T['answer'])
    return sid,len(turns)

def import_session(store,path,provider=None):
    """A Claude Code or Codex CLI JSONL log -> one single-agent session."""
    lines=[]
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        try:lines.append(json.loads(line))
        except ValueError:continue
    codex=provider=='codex' or (provider is None and any(d.get('type') in ('response_item','session_meta','event_msg') for d in lines[:50]))
    turns=codex_turns(lines) if codex else claude_code_turns(lines)
    model=None
    for d in lines:
        m=(d.get('message') or {}).get('model') or (d.get('payload') or {}).get('model')
        if m:model=m;break
    title=next((T['prompt'][:60] for T in turns),Path(path).stem)
    out=write_agent_session(store,title,'codex' if codex else 'claude',turns,model)
    store.flush(10)
    return out


def _review_security(ev,steps,provider,tid):
    """Security review and tool-call health (security.py), as analysis.security, the same event live turns emit."""
    import security
    if not steps:return
    sec=security.review(steps);top=max((f['severity'] for f in sec['findings']),key=lambda v:security.SEV_RANK[v],default='none')
    ev(steps[-1]['at'],'analysis.security',provider,turn_id=tid,data=sec,count=len(sec['findings']),verdict=top)

def _flag_hacks(ev,steps,provider,tid):
    """Reward-hacking evidence in writes (test/grader edits, skips, removed asserts), as analysis.py flags it live."""
    import analysis
    st=[]
    for x in steps:
        k=analysis.action_type({'kind':'tool','tool':x.get('tool'),'target':x.get('target')})
        st.append({**x,'kind':'tool','type':k,'key':analysis.target_key(x,k),'input':x.get('input') or ''})
    for h in analysis.hack_signals(st):ev(st[0]['at'] if st else 0,'analysis.hack',provider,turn_id=tid,item_id=str(h['ref']),name=h['signal'],detail=h['target'])

# ---------------------------------------------------------------- RL environment rollouts
"""`duo --import-rollouts FILE`: RL environment rollouts as an environment session. Accepted shapes (JSON or JSONL):
- one record per episode: {"episode"|"id", "task"|"env_task", "policy"|"model"?, "env"?, "success"?, "return"?,
  "steps": [{"action"|"tool"|"name", "input"|"args"|"target"?, "observation"|"output"|"obs"?, "reward"?, "ok"|"error"?,
             "t"|"ts"|"timestamp"?, "thought"|"reasoning"?}]}
- one record per step: the same step fields plus "episode" (and "task", "policy" on any of them).
Each episode becomes a turn (mode `env:<env>`, name = task): every step a tool call carrying its reward as `score`,
`reasoning` for thoughts, `env.return` with the episode return and success, and the long-run reading. Episodes of
the same task are what the viewer compares (success rate, pass@k, pass^k, reward curves)."""

def _episodes(records):
    eps={};order=[]
    for r in records:
        if not isinstance(r,dict):continue
        if isinstance(r.get('steps'),list):
            key=str(r.get('episode',r.get('id',len(order))));eps[key]=dict(r);order.append(key);continue
        key=str(r.get('episode',r.get('episode_id','0')))
        if key not in eps:eps[key]={k:v for k,v in r.items() if k in ('episode','task','env_task','policy','model','env')}|{'steps':[]};order.append(key)
        eps[key]['steps'].append(r)
        for k in ('success','return'):
            if k in r:eps[key][k]=r[k]
    return [eps[k] for k in order]

def import_rollouts(store,path,provider='claude'):
    raw=Path(path).read_text(encoding='utf-8').strip()
    try:data=json.loads(raw)
    except ValueError:data=[json.loads(l) for l in raw.splitlines() if l.strip()]
    if isinstance(data,dict):data=data.get('episodes') or data.get('rollouts') or [data]
    eps=_episodes(data)
    if not eps:return None,0
    import longrun
    env=str(eps[0].get('env') or Path(path).stem)
    base=datetime.now().timestamp()-60*sum(len(e['steps'])+5 for e in eps)
    sid='env-'+hashlib.sha1((env+raw[:2000]).encode()).hexdigest()[:12]
    store.session(sid,base,f'env: {env[:80]}');n={'id':0}
    def ev(ts,kind,prov='duo',span=None,payload=None,**f):
        n['id']+=1;e={'session_id':sid,'id':n['id'],'timestamp':ts,'type':kind,'provider':prov,**({'span_id':span} if span else {}),**f}
        if payload:e['chars']=len(payload)
        store.event(e,payload,kind if payload else None);return e
    clock=base
    for k,E in enumerate(eps,1):
        task=str(E.get('task') or E.get('env_task') or 'task');policy=E.get('policy') or E.get('model')
        tid=f'{sid}-e{k}';aid=tid+'-'+provider;t0=clock
        ev(t0,'span.start','duo',tid,span_id=tid,span_kind='turn',mode='env:'+env,name=task)
        ev(t0,'prompt','duo',tid,f'[{env}] {task} · episode {E.get("episode",E.get("id",k))}')
        ev(t0,'span.start',provider,aid,span_id=aid,parent_id=tid,span_kind='agent',role='policy',mode='env:'+env,**({'model':str(policy)} if policy else {}))
        steps=[];ret=0.0;fails=0;t=t0
        for j,s in enumerate(E['steps']):
            ts=_ts(s.get('t',s.get('ts',s.get('timestamp'))),None);t=ts if ts is not None else t+2
            th=s.get('thought') or s.get('reasoning')
            if th:ev(t,'reasoning',provider,aid,str(th))
            name=str(s.get('action') or s.get('tool') or s.get('name') or 'act')
            tgt=s.get('target') or s.get('input') or s.get('args') or ''
            tgt=tgt if isinstance(tgt,str) else json.dumps(tgt)
            obs=s.get('observation',s.get('output',s.get('obs','')));obs=obs if isinstance(obs,str) else json.dumps(obs)
            ok=not s.get('error') and s.get('ok',True) is not False
            r=float(s.get('reward') or 0);ret+=r;fails+=not ok
            e=ev(t,'tool.started',provider,aid,tgt or None,tool=name,detail=tgt[:200],item_id=f'{aid}-{j}')
            d=float(s.get('duration') or 1);ev(t+d,'tool.completed',provider,aid,obs or None,tool=name,item_id=f'{aid}-{j}',passed=ok,score=r)
            steps.append({'ref':e['id'],'tool':name,'target':tgt,'ok':ok,'at':t,'seconds':d,'output':obs[-900:]});t+=d
        ret=float(E.get('return',ret));success=E.get('success');success=bool(success) if success is not None else ret>0
        t1=t+1
        ev(t1,'span.end',provider,aid,span_id=aid,parent_id=tid,span_kind='agent',role='policy',status='ok' if success else 'failed',duration_ms=int((t1-t0)*1000),tools=len(steps),tool_errors=fails)
        ev(t1,'env.return',provider,turn_id=tid,score=round(ret,4),passed=success,name=task,count=len(steps))
        _flag_hacks(ev,steps,provider,tid);_review_security(ev,steps,provider,tid)
        r=longrun.read(steps)
        if r:ev(t1,'analysis.longrun',provider,turn_id=tid,data=r,rationale=longrun.summary(r))
        ev(t1,'span.end','duo',tid,span_id=tid,span_kind='turn',status='ok',duration_ms=int((t1-t0)*1000))
        clock=t1+30
    store.flush(10)
    return sid,len(eps)
