"""Headless native agents with approvals rendered by the Duo host."""
import asyncio
from collections import deque
import json
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from traces import emit

VENDOR=Path(__file__).with_name('vendor')

def tool_detail(item):
    """Short human label for a tool call (command, path, query); full input goes to the trace DB."""
    for key in ('command','file_path','path','pattern','query','url','tool','description'):
        value=item.get(key) if isinstance(item,dict) else None
        if value:return ' '.join(value) if isinstance(value,list) else str(value)
    changes=item.get('changes') if isinstance(item,dict) else None
    if changes:return ', '.join(str(c.get('path','')) for c in changes[:3])
    return ''

def as_text(value):
    if value is None:return ''
    if isinstance(value,str):return value
    if isinstance(value,list):return '\n'.join(as_text(v.get('text',v) if isinstance(v,dict) else v) for v in value)
    try:return json.dumps(value,ensure_ascii=False,default=str)
    except (TypeError,ValueError):return str(value)

BULKY=('aggregatedOutput','result','output','content')

def stop_process(proc):
    if proc.poll() is None:
        try:
            import os
            os.killpg(proc.pid,signal.SIGTERM)
            proc.wait(timeout=3)
        except (ProcessLookupError,subprocess.TimeoutExpired):
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            proc.wait()

class CodexBridge:
    def __init__(self,cwd,approve,notice,timeout=300):
        self.cwd=str(cwd);self.approve=approve;self.notice=notice;self.timeout=timeout
        self.proc=None;self.events=queue.Queue();self.errors=deque(maxlen=20);self.next_id=0
    def start(self):
        executable=shutil.which('codex')
        if not executable:raise RuntimeError('Codex CLI not installed')
        self.proc=subprocess.Popen([executable,'app-server'],cwd=self.cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1,start_new_session=True)
        def reader():
            for line in self.proc.stdout:
                try:self.events.put(json.loads(line))
                except ValueError:pass
            self.events.put({'_closed':True})
        def errors():
            for line in self.proc.stderr:self.errors.append(line.rstrip())
        threading.Thread(target=reader,daemon=True).start();threading.Thread(target=errors,daemon=True).start()
        self.rpc('initialize',{'clientInfo':{'name':'duo','title':'Duo terminal','version':'0.3.0'}})
        self.send({'method':'initialized','params':{}})
    def send(self,message):
        self.proc.stdin.write(json.dumps(message)+'\n');self.proc.stdin.flush()
    def next_event(self,deadline):
        try:event=self.events.get(timeout=max(.01,deadline-time.monotonic()))
        except queue.Empty:raise TimeoutError('Codex agent timed out')
        if event.get('_closed'):raise RuntimeError('Codex app-server stopped: '+'\n'.join(self.errors)[-1500:])
        return event
    def handle_request(self,event):
        method=event['method'];params=event.get('params',{})
        emit('approval.requested','codex',method=method,request_id=str(event['id']))
        if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
            allowed=self.approve('Codex',method,params)
            result={'decision':'accept' if allowed else 'decline'}
        elif method=='item/permissions/requestApproval':
            allowed=self.approve('Codex',method,params)
            result={'permissions':params.get('permissions',{}) if allowed else {},'scope':'turn'}
        elif method=='item/tool/requestUserInput':
            # No automatic selection of consent on behalf of the user.
            answers={}
            for question in params.get('questions',[]):
                response=self.approve('Codex','question',question)
                answers[question['id']]={'answers':[response] if isinstance(response,str) else []}
            result={'answers':answers}
        elif method=='mcpServer/elicitation/request':
            # Form/URL consent needs structured support; fail closed.
            self.notice('Codex','MCP elicitation declined: structured consent is not implemented')
            result={'action':'decline','content':None}
        else:
            self.send({'id':event['id'],'error':{'code':-32601,'message':'Unsupported host request; not approved'}});return
        emit('approval.resolved','codex',method=method,status=str(result.get('decision') or result.get('action') or 'answered'))
        self.send({'id':event['id'],'result':result})
    def rpc(self,method,params):
        self.next_id+=1;identifier=self.next_id
        self.send({'id':identifier,'method':method,'params':params})
        deadline=time.monotonic()+self.timeout
        while True:
            event=self.next_event(deadline)
            if 'method' in event and 'id' in event:self.handle_request(event)
            elif event.get('id')==identifier:
                if 'error' in event:raise RuntimeError(str(event['error']))
                return event.get('result',{})
    def run(self,prompt,readonly=False,model=None):
        started=time.monotonic()
        try:
            self.start()
            params={'cwd':self.cwd,'ephemeral':True,'approvalPolicy':'on-request','sandbox':'read-only' if readonly else 'workspace-write'}
            if model:params['model']=model
            thread=self.rpc('thread/start',params)['thread']['id']
            self.rpc('turn/start',{'threadId':thread,'input':[{'type':'text','text':prompt}]})
            messages=[];deadline=time.monotonic()+self.timeout
            while True:
                event=self.next_event(deadline)
                if 'method' in event and 'id' in event:self.handle_request(event);continue
                method=event.get('method');params=event.get('params',{})
                if method and method.endswith('tokenUsage/updated'):  # best effort: app-server token accounting
                    tu=params.get('tokenUsage') or params;tot=tu.get('total') if isinstance(tu.get('total'),dict) else tu
                    self.usage={k:int(v) for k,v in tot.items() if isinstance(v,(int,float))}
                if method in ('item/started','item/completed','turn/completed'):
                    item=params.get('item',{});kind=item.get('type','')
                    if kind=='reasoning' and method=='item/completed':
                        text=as_text(item.get('summary')) or as_text(item.get('content')) or as_text(item.get('text'))
                        emit('reasoning','codex',payload=text or None,item_id=item.get('id'))
                    elif kind in ('agentMessage','reasoning','userMessage') or method=='turn/completed':
                        emit('agent.event','codex',method=method,tool=kind,item_id=item.get('id'),status=item.get('status'))
                    elif method=='item/started':
                        emit('tool.started','codex',tool=kind,item_id=item.get('id'),detail=tool_detail(item),payload_kind='tool_input',
                             payload=as_text({k:v for k,v in item.items() if k not in BULKY}))
                    else:
                        exit_code=item.get('exitCode')
                        ok=item.get('status') in (None,'completed') and exit_code in (None,0)
                        output=as_text(item.get('aggregatedOutput') or item.get('error') or item.get('result') or item.get('output'))
                        emit('tool.completed','codex',tool=kind,item_id=item.get('id'),status=item.get('status'),passed=ok,payload=output or None,payload_kind='tool_output')
                if method=='item/started':
                    item=params.get('item',{});kind=item.get('type','')
                    if kind not in ('agentMessage','reasoning','userMessage'):
                        self.notice('Codex',kind+': '+str(item.get('command') or item.get('query') or item.get('tool') or '')[:180])
                if method=='item/completed':
                    item=params.get('item',{})
                    if item.get('type')=='agentMessage':
                        text=item.get('text','')
                        if item.get('phase')=='commentary':self.notice('Codex',text[:250])
                        else:messages.append(text)
                if method=='turn/completed':
                    turn=params.get('turn',{})
                    if getattr(self,'usage',None):emit('usage','codex',data=self.usage)
                    if turn.get('status')!='completed':raise RuntimeError(str(turn.get('error') or turn.get('status')))
                    break
            if not messages:raise RuntimeError('Codex returned no final answer')
            return {'ok':True,'answer':'\n\n'.join(messages),'seconds':round(time.monotonic()-started,2)}
        except (RuntimeError,TimeoutError,OSError,KeyError) as exc:return {'ok':False,'error':str(exc)}
        finally:
            if self.proc:stop_process(self.proc)

def codex_run(prompt,cwd,approve,notice,readonly=False,timeout=300,model=None):
    return CodexBridge(cwd,approve,notice,timeout).run(prompt,readonly,model)

def claude_run(prompt,cwd,approve,notice,readonly=False,timeout=300,model=None):
    if str(VENDOR) not in sys.path:sys.path.insert(0,str(VENDOR))
    from claude_agent_sdk import ClaudeAgentOptions,ClaudeSDKClient,PermissionResultAllow,PermissionResultDeny,AssistantMessage,ResultMessage,TextBlock,ToolUseBlock,UserMessage,ToolResultBlock
    try:from claude_agent_sdk import ThinkingBlock
    except ImportError:ThinkingBlock=None
    async def run():
        async def permission(tool,arguments,context):
            emit('approval.requested','claude',tool=tool)
            if tool=='AskUserQuestion':
                answered={}
                for question in arguments.get('questions',[]):
                    response=approve('Claude','question',question)
                    if isinstance(response,str):answered[question['question']]=response
                if answered:return PermissionResultAllow(updated_input={**arguments,'answers':answered})
                return PermissionResultDeny(message='User did not answer')
            if readonly and tool not in ('Read','Glob','Grep','WebSearch','WebFetch'):
                emit('approval.resolved','claude',tool=tool,status='declined')
                return PermissionResultDeny(message='Reviewer has read-only tools')
            allowed=approve('Claude',tool,arguments)
            emit('approval.resolved','claude',tool=tool,status='accepted' if allowed else 'declined')
            if allowed:return PermissionResultAllow(updated_input=arguments)
            return PermissionResultDeny(message='Declined in Duo')
        options=ClaudeAgentOptions(cwd=str(cwd),cli_path=shutil.which('claude'),permission_mode='default',setting_sources=['user','project','local'],can_use_tool=permission,model=model,extra_args={'no-session-persistence':None},stderr=lambda line:None)
        if readonly:
            options.tools=['Read','Glob','Grep','WebSearch','WebFetch']
            options.disallowed_tools=['Bash','Write','Edit','NotebookEdit','Agent','Task','Skill']
            options.strict_mcp_config=True
        messages=[];final=None;tool_names={}
        async with asyncio.timeout(timeout):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    emit('agent.event','claude',method=type(message).__name__)
                    if isinstance(message,AssistantMessage):
                        for block in message.content:
                            if isinstance(block,TextBlock):messages.append(block.text)
                            elif ThinkingBlock and isinstance(block,ThinkingBlock):
                                if block.thinking:emit('reasoning','claude',payload=block.thinking)
                            elif isinstance(block,ToolUseBlock):
                                tool_names[block.id]=block.name
                                emit('tool.started','claude',tool=block.name,item_id=block.id,detail=tool_detail(block.input),payload=as_text(block.input),payload_kind='tool_input');notice('Claude',block.name)
                    elif isinstance(message,UserMessage) and isinstance(message.content,list):
                        for block in message.content:
                            if isinstance(block,ToolResultBlock):
                                emit('tool.completed','claude',tool=tool_names.get(block.tool_use_id,''),item_id=block.tool_use_id,passed=not block.is_error,payload=as_text(block.content) or None,payload_kind='tool_output')
                    elif isinstance(message,ResultMessage):
                        u={k:int(v) for k,v in (getattr(message,'usage',None) or {}).items() if isinstance(v,(int,float))}
                        if getattr(message,'total_cost_usd',None) is not None:u['cost_usd']=round(float(message.total_cost_usd),5)
                        if u:emit('usage','claude',data=u)
                        if message.is_error:raise RuntimeError(str(message.result or message.subtype))
                        final=message.result
        answer=final or '\n\n'.join(messages)
        if not answer:raise RuntimeError('Claude returned no final answer')
        return answer
    started=time.monotonic()
    try:return {'ok':True,'answer':asyncio.run(run()),'seconds':round(time.monotonic()-started,2)}
    except Exception as exc:return {'ok':False,'error':str(exc)}
